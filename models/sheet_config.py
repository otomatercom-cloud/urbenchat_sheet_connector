import csv
import io
import json
import logging
import time
import re
from datetime import datetime, timedelta

import requests

from odoo import fields, models, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# ─── Google JWT signer ───────────────────────────────────────────────────────

def _b64url(data: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def _sign_jwt(sa_info: dict) -> str:
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.backends import default_backend
    except ImportError:
        raise UserError(_(
            "The 'cryptography' package is required for Service Account auth. "
            "Install it: pip install cryptography"
        ))
    now = int(time.time())
    header  = _b64url(json.dumps({'alg': 'RS256', 'typ': 'JWT'}).encode())
    payload = _b64url(json.dumps({
        'iss':   sa_info['client_email'],
        'scope': 'https://www.googleapis.com/auth/spreadsheets.readonly',
        'aud':   'https://oauth2.googleapis.com/token',
        'iat':   now, 'exp': now + 3600,
    }).encode())
    msg = f"{header}.{payload}".encode()
    pk  = serialization.load_pem_private_key(
        sa_info['private_key'].encode(), password=None, backend=default_backend()
    )
    sig = pk.sign(msg, padding.PKCS1v15(), hashes.SHA256())
    jwt = f"{header}.{payload}.{_b64url(sig)}"
    r   = requests.post('https://oauth2.googleapis.com/token',
                        data={'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
                              'assertion': jwt}, timeout=30)
    r.raise_for_status()
    return r.json()['access_token']


# ─── Sheet Config ─────────────────────────────────────────────────────────────

class UrbenchatSheetConfig(models.Model):
    _name        = 'urbenchat.sheet.config'
    _description = 'UrbanChat Google Sheet Configuration'
    _inherit     = ['mail.thread', 'mail.activity.mixin']

    name   = fields.Char(string='Config Name', required=True, tracking=True)
    active = fields.Boolean(default=True)

    # ── Auth ──────────────────────────────────────────────────────────────────
    auth_method = fields.Selection(
        [('public', 'Published Sheet (CSV Export — no auth)'),
         ('service_account', 'Service Account JSON')],
        string='Authentication Method', default='public', required=True,
    )
    sheet_url  = fields.Char(string='Google Sheet URL')
    sheet_id   = fields.Char(string='Sheet ID (auto)', compute='_compute_sheet_id',
                              store=True, readonly=True)
    gid        = fields.Char(string='Tab GID', default='0',
                              help='The "gid" in the sheet URL. Default 0 = first tab.')
    service_account_json = fields.Text(string='Service Account JSON')

    # ── Dynamic column mapping (replaces fixed col_* fields) ─────────────────
    column_mapping_ids = fields.One2many(
        'urbenchat.column.mapping', 'config_id', string='Column Mappings',
    )

    # ── Defaults ──────────────────────────────────────────────────────────────
    skip_header_rows       = fields.Integer(string='Skip Header Rows', default=1)
    default_leads_source_id = fields.Many2one(
        'leads.sources', string='Default Lead Source',
        help='Used when no Ad ID mapping matches.'
    )
    default_lead_quality   = fields.Selection(
        [('new','New'),('first_attempt','First Attempt'),('warm','Warm'),('hot','Hot')],
        string='Default Lead Quality', default='new',
    )

    # ── Sync control ──────────────────────────────────────────────────────────
    last_sync_date  = fields.Datetime(string='Last Sync Date', readonly=True)
    sync_interval_minutes = fields.Integer(
        string='Sync Interval (minutes)',
        default=5,
        help='How often this sheet is synced automatically. Minimum 1 minute.',
    )
    next_sync_date  = fields.Datetime(
        string='Next Sync At',
        compute='_compute_next_sync_date',
        store=False,
    )
    sync_log_ids    = fields.One2many('urbenchat.sync.log', 'config_id', string='Sync History')
    sync_log_count  = fields.Integer(string='Sync Logs', compute='_compute_sync_log_count')
    synced_record_ids = fields.One2many(
        'urbenchat.synced.record', 'config_id', string='Synced Records'
    )
    synced_record_count = fields.Integer(
        string='Processed Rows', compute='_compute_synced_count'
    )
    total_leads_created = fields.Integer(string='Total Leads Created', compute='_compute_totals')
    last_sync_created   = fields.Integer(string='Leads in Last Sync',  compute='_compute_totals')

    # ── Computed ──────────────────────────────────────────────────────────────
    @api.depends('sheet_url')
    def _compute_sheet_id(self):
        for rec in self:
            rec.sheet_id = ''
            if rec.sheet_url:
                m = re.search(r'/spreadsheets/d/([a-zA-Z0-9_-]+)', rec.sheet_url)
                if m:
                    rec.sheet_id = m.group(1)

    @api.depends('last_sync_date', 'sync_interval_minutes')
    def _compute_next_sync_date(self):
        for rec in self:
            if rec.last_sync_date and rec.sync_interval_minutes:
                rec.next_sync_date = rec.last_sync_date + timedelta(
                    minutes=rec.sync_interval_minutes
                )
            else:
                rec.next_sync_date = False

    def _compute_sync_log_count(self):
        for rec in self:
            rec.sync_log_count = len(rec.sync_log_ids)

    def _compute_synced_count(self):
        for rec in self:
            rec.synced_record_count = self.env['urbenchat.synced.record'].search_count(
                [('config_id', '=', rec.id)]
            )

    def action_reset_sync_tracking(self):
        """Clear all synced record tracking so next sync re-processes everything."""
        self.ensure_one()
        self.env['urbenchat.synced.record'].search(
            [('config_id', '=', self.id)]
        ).unlink()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Sync Tracking Reset',
                'message': 'All processed row records cleared. Next sync will re-process the full sheet.',
                'type': 'warning',
                'sticky': False,
            },
        }

    def action_purge_old_logs(self):
        """Keep only the last 30 sync logs for this config, delete the rest."""
        self.ensure_one()
        Log = self.env['urbenchat.sync.log']
        all_logs = Log.search(
            [('config_id', '=', self.id)], order='sync_date desc'
        )
        to_delete = all_logs[30:]   # keep 30, delete everything older
        deleted = len(to_delete)
        to_delete.unlink()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Old Logs Purged',
                'message': f'Deleted {deleted} old sync log records. Kept last 30.',
                'type': 'success',
                'sticky': False,
            },
        }

    @api.model
    def action_purge_all_old_logs(self):
        """Global cleanup — keep last 30 logs per config. Run manually or via cron."""
        for cfg in self.search([]):
            logs = self.env['urbenchat.sync.log'].search(
                [('config_id', '=', cfg.id)], order='sync_date desc'
            )
            logs[30:].unlink()

    def _compute_totals(self):
        for rec in self:
            logs = rec.sync_log_ids
            rec.total_leads_created = sum(logs.mapped('leads_created'))
            last = logs[:1]
            rec.last_sync_created = last.leads_created if last else 0

    # ── Auto-populate default mappings on create ──────────────────────────────
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            if not rec.column_mapping_ids:
                rec._create_default_mappings()
        return records

    def _create_default_mappings(self):
        """Seed sensible defaults so the user has a starting point."""
        # Default mappings pre-filled to match common UrbanChat sheet headers.
        # User can change "Sheet Column Header" to match their actual sheet Row 1.
        defaults = [
            # sheet_column            lead_field        required  seq
            ('Nam',                   'name',            True,     10),
            ('Phone',                 'phone_number',    True,     20),
            ('Email',                 'email_address',   False,    30),
            ('Ad Id',                 'ad_id_raw',       False,    40),
            ('Interested Course',     'course_id',       False,    50),
            ('Location',              'college_name',    False,    60),
            ('Conversation Assigned to', 'lead_owner',   False,    70),
            ('Date',                  '_timestamp',      False,    80),
        ]
        for sheet_col, lead_field, required, seq in defaults:
            self.env['urbenchat.column.mapping'].create({
                'config_id':    self.id,
                'sheet_column': sheet_col,
                'lead_field':   lead_field,
                'is_required':  required,
                'sequence':     seq,
            })

    # ── Actions ───────────────────────────────────────────────────────────────
    @api.model
    def get_sync_interval(self):
        """Return the minimum sync interval (seconds) across all active configs, for dashboard auto-refresh."""
        configs = self.search([('active', '=', True)])
        if not configs:
            return 300  # default 5 min
        min_interval = min(max(c.sync_interval_minutes or 5, 1) for c in configs)
        return min_interval * 60  # convert to seconds

    def action_test_connection(self):
        self.ensure_one()
        return self.env['urbenchat.test.connection.wizard'].action_open(self.id)

    def action_sync_now(self):
        self.ensure_one()
        result = self._do_sync(triggered_by='manual')
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Sync Complete'),
                'message': _(
                    '%(c)s leads created · %(s)s re-enquiries · %(e)s errors.',
                    c=result['leads_created'], s=result['leads_skipped'], e=result['leads_error'],
                ),
                'type': 'success' if result['state'] == 'success' else 'warning',
                'sticky': False,
            },
        }

    def action_view_logs(self):
        self.ensure_one()
        return {
            'name': _('Sync Logs — %s') % self.name,
            'type': 'ir.actions.act_window',
            'res_model': 'urbenchat.sync.log',
            'view_mode': 'tree,form',
            'domain': [('config_id', '=', self.id)],
        }

    # ── Cron ──────────────────────────────────────────────────────────────────
    @api.model
    def _cron_sync_all(self):
        """
        Runs every minute. Each config syncs only when its own interval has elapsed.
        """
        now = fields.Datetime.now()
        for cfg in self.search([('active', '=', True)]):
            interval = max(cfg.sync_interval_minutes or 5, 1)
            if cfg.last_sync_date:
                due_at = cfg.last_sync_date + timedelta(minutes=interval)
                if now < due_at:
                    continue  # not due yet
            try:
                cfg._do_sync(triggered_by='cron')
            except Exception as e:
                _logger.error('UrbanChat cron sync failed for %s: %s', cfg.name, e)

    # ── Core sync ─────────────────────────────────────────────────────────────
    def _do_sync(self, triggered_by='manual'):
        self.ensure_one()
        _logger.info('UrbanChat sync START — %s', self.name)

        rows_fetched = leads_created = leads_skipped = leads_error = re_enquiries = already_done = 0
        errors = []

        try:
            rows = self._fetch_sheet_rows()
            rows_fetched = len(rows)
        except Exception as e:
            _logger.error('Sheet fetch failed: %s', e)
            self._log_sync(triggered_by=triggered_by, rows_fetched=0,
                           leads_created=0, leads_skipped=0, leads_error=1,
                           state='failed', error_details=str(e))
            raise UserError(_('Failed to fetch sheet: %s') % str(e))

        # ── Build lookups once ────────────────────────────────────────────────
        ad_map = {
            m.ad_id: m
            for m in self.env['urbenchat.ad.mapping'].search([('active', '=', True)])
        }

        # ── Load already-processed phone::ad_id keys ─────────────────────────
        # Key format:  "phone::source_campaign_id"
        # This means: same phone + same ad = processed ONCE, forever.
        # Same phone + different ad = allowed (new ad interaction).
        self.env.cr.execute(
            'SELECT unique_key FROM urbenchat_synced_record WHERE config_id = %s',
            (self.id,)
        )
        already_synced = {r[0] for r in self.env.cr.fetchall()}
        _logger.info('Loaded %d already-synced keys for config %s', len(already_synced), self.name)

        # Column mapping:  lead_field → {sheet_column, is_required, default}
        col_map = {
            cm.lead_field: {
                'sheet_column': cm.sheet_column,
                'is_required':  cm.is_required,
                'default':      cm.default_value or '',
            }
            for cm in self.column_mapping_ids
        }

        phone_col  = col_map.get('phone_number', {}).get('sheet_column', '')
        ad_id_col  = col_map.get('ad_id_raw',    {}).get('sheet_column', '')
        new_keys   = []   # (config_id, unique_key, phone, source_camp_id, result)

        for row in rows:
            phone  = (row.get(phone_col,  '') or '').strip()
            ad_val = (row.get(ad_id_col,  '') or '').strip()

            # Resolve source_campaign_id for the key
            sc_id = 0
            if ad_val and ad_val in ad_map:
                sc_id = ad_map[ad_val].source_campaign_id.id or 0

            # Composite key: phone::source_campaign_id
            unique_key = f'{phone}::{sc_id}'

            try:
                result = self._process_row(row, ad_map, col_map, already_synced)
                if result == 'created':
                    leads_created += 1
                    if phone:
                        already_synced.add(unique_key)
                        new_keys.append((self.id, unique_key, phone, sc_id, 'created'))
                elif result == 're_enquiry':
                    re_enquiries += 1
                    if phone:
                        already_synced.add(unique_key)
                        new_keys.append((self.id, unique_key, phone, sc_id, 're_enquiry'))
                elif result == 'already_synced':
                    already_done += 1
            except Exception as e:
                leads_error += 1
                err_msg = str(e)
                if len(errors) < 20 and err_msg not in errors:
                    errors.append(err_msg)
                _logger.warning('Row error: %s', e)

        # ── Bulk-insert new keys — one SQL, ON CONFLICT safe ──────────────
        if new_keys:
            self.env.cr.executemany(
                """INSERT INTO urbenchat_synced_record
                       (config_id, unique_key, phone, source_campaign_id, synced_at, result)
                   VALUES (%s, %s, %s, %s, NOW(), %s)
                   ON CONFLICT (config_id, unique_key) DO NOTHING""",
                new_keys,
            )
            _logger.info('Recorded %d new synced keys', len(new_keys))

        state = 'success'
        if leads_error and not leads_created:
            state = 'failed'
        elif leads_error:
            state = 'partial'

        self._log_sync(
            triggered_by=triggered_by,
            rows_fetched=rows_fetched,
            leads_created=leads_created,
            leads_skipped=re_enquiries,
            leads_error=leads_error,
            state=state,
            error_details=(
                f'Total errors: {leads_error}. First unique errors:\n'
                + '\n'.join(errors)
            ) if errors else False,
        )
        self.last_sync_date = fields.Datetime.now()
        return {'leads_created': leads_created, 'leads_skipped': re_enquiries,
                'leads_error': leads_error, 'state': state}

    # ── Sheet fetch ───────────────────────────────────────────────────────────
    def _fetch_sheet_rows(self):
        if not self.sheet_id:
            raise UserError(_('Could not extract Sheet ID from the URL.'))
        return (self._fetch_via_csv_export()
                if self.auth_method == 'public'
                else self._fetch_via_service_account())

    def _fetch_via_csv_export(self):
        url = (f'https://docs.google.com/spreadsheets/d/{self.sheet_id}'
               f'/export?format=csv&gid={self.gid or "0"}')
        resp = requests.get(url, timeout=60)
        if resp.status_code == 403:
            raise UserError(_(
                'Sheet returned 403. Publish it first: '
                'File → Share → Publish to web → CSV.'
            ))
        resp.raise_for_status()
        text   = resp.content.decode('utf-8-sig')
        reader = csv.DictReader(io.StringIO(text))
        rows   = list(reader)
        skip   = self.skip_header_rows - 1
        return rows[skip:] if skip > 0 else rows

    def _fetch_via_service_account(self):
        if not self.service_account_json:
            raise UserError(_('Service Account JSON is required.'))
        try:
            sa_info = json.loads(self.service_account_json)
        except json.JSONDecodeError as e:
            raise UserError(_('Invalid service account JSON: %s') % str(e))
        token = _sign_jwt(sa_info)
        url   = (f'https://sheets.googleapis.com/v4/spreadsheets/{self.sheet_id}'
                 f'/values/Sheet1')
        resp  = requests.get(url, headers={'Authorization': f'Bearer {token}'}, timeout=60)
        resp.raise_for_status()
        data  = resp.json()
        all_rows = data.get('values', [])
        if not all_rows:
            return []
        header = all_rows[0]
        return [
            {header[i]: (row[i] if i < len(row) else '')
             for i in range(len(header))}
            for row in all_rows[1:]
        ]

    # ── Row processing ────────────────────────────────────────────────────────
    def _process_row(self, row, ad_map, col_map, already_synced=None):
        """
        Correct order:
          1. Get phone
          2. Check already_synced → skip
          3. Resolve ad_id → source_campaign_id / leads_source_id
          4. Check duplicate in leads.logic → re-enquiry (with resolved campaign)
          5. Create lead
        """
        if already_synced is None:
            already_synced = set()

        def _val(lead_field):
            meta = col_map.get(lead_field)
            if not meta:
                return ''
            raw = (row.get(meta['sheet_column'], '') or '').strip()
            if not raw:
                if meta['is_required']:
                    raise ValueError(
                        f'Required field "{lead_field}" (column "{meta["sheet_column"]}")'
                        f' is empty — row skipped.'
                    )
                return meta['default']
            return raw

        # ── 1. Phone (required) ────────────────────────────────────────────
        phone = _val('phone_number')
        if not phone:
            raise ValueError('Phone number is empty — row skipped.')

        # ── 2. Resolve Ad ID → source_campaign_id + leads_source_id ─────────
        ad_id_val          = _val('ad_id_raw')
        source_campaign_id = False
        leads_source_id    = False

        if ad_id_val and ad_id_val in ad_map:
            mapping            = ad_map[ad_id_val]
            source_campaign_id = mapping.source_campaign_id.id
            leads_source_id    = mapping.leads_source_id.id
        elif self.default_leads_source_id:
            leads_source_id    = self.default_leads_source_id.id
        else:
            raise ValueError(
                f'No Ad ID mapping for "{ad_id_val}" and no default Lead Source set.'
            )

        if not leads_source_id:
            raise ValueError(f'Ad mapping for "{ad_id_val}" has no parent Lead Source.')

        # ── 3. Check already-synced using phone::source_campaign_id key ────
        # This is the core rule: same phone + same ad = skip FOREVER
        sc_id      = source_campaign_id or 0
        unique_key = f'{phone}::{sc_id}'
        if unique_key in already_synced:
            return 'already_synced'

        # ── 4. Duplicate phone with NEW ad → re-enquiry ────────────────────
        existing = self.env['leads.logic'].search(
            [('phone_number', '=', phone)], limit=1
        )
        if existing:
            self._create_re_enquiry(
                existing, row, col_map,
                source_campaign_id, leads_source_id, _val
            )
            return 're_enquiry'

        # ── 5. New lead ────────────────────────────────────────────────────
        name = _val('name') or 'Unknown'

        # Course lookup
        course_id   = False
        course_name = _val('course_id')
        if course_name:
            course    = self.env['op.course'].search([('name', 'ilike', course_name)], limit=1)
            course_id = course.id if course else False

        # Officer lookup
        lead_owner_id = False
        officer_name  = _val('lead_owner')
        if officer_name:
            emp = (self.env['hr.employee'].search([('name', '=ilike', officer_name)], limit=1)
                   or self.env['hr.employee'].search([('name', 'ilike', officer_name)], limit=1))
            if emp:
                lead_owner_id = emp.id
            else:
                _logger.warning('Officer "%s" not found in Employees', officer_name)

        DIRECT_FIELDS = {
            'email_address', 'college_name', 'last_studied_course',
            'title', 'call_response', 'academic_year', 'lead_quality',
            'incoming_source', 'country',
        }
        vals = {
            'name':               name,
            'phone_number':       phone,
            'leads_source':       leads_source_id,
            'source_campaign_id': source_campaign_id or False,
            'course_id':          course_id or False,
            'lead_owner':         lead_owner_id or False,
            'lead_quality':       self.default_lead_quality or 'new',
            'state':              'new',
        }
        for f in DIRECT_FIELDS:
            if f in col_map:
                v = _val(f)
                if v:
                    vals[f] = v

        self.env['leads.logic'].create(vals)
        return 'created'

    def _create_re_enquiry(self, existing_lead, row, col_map, source_campaign_id, leads_source_id, _val):
        """
        Creates a re-enquiry ONLY IF this exact phone + ad_id combination
        has NEVER been processed before — permanent, not per-day.

        Key:  phone::source_campaign_id  stored in urbenchat_synced_record
        Once created → never re-created regardless of how many syncs run.
        """

        course_name   = _val('course_id')
        channel_val   = ''
        location_val  = ''

        # Pull channel and location from sheet if they are mapped
        for cm in self.column_mapping_ids:
            col_val = (row.get(cm.sheet_column, '') or '').strip()
            if cm.lead_field == 'incoming_source':
                channel_val = col_val
            if cm.lead_field == 'college_name':
                location_val = col_val

        # Map channel value to digital_lead_source selection if possible
        CHANNEL_MAP = {
            'facebook':   'facebook',
            'instagram':  'instagram',
            'whatsapp':   'whatsapp_meta',
            'messenger':  'messenger',
            'linkedin':   'linkedin',
            'google':     'google',
            'youtube':    'youtube_google',
            'website':    'website',
        }
        digital_source = False
        if channel_val:
            ch_lower = channel_val.lower()
            for key, val in CHANNEL_MAP.items():
                if key in ch_lower:
                    digital_source = val
                    break

        # Try to match campaign name to the hardcoded selection values
        campaign_val = False
        if source_campaign_id:
            campaign_rec = self.env['lead.source.campaign'].browse(source_campaign_id)
            camp_name    = campaign_rec.name or ''
            # Get valid selection keys from the field
            valid_campaigns = dict(
                self.env['lead.re.enquiry']._fields['campaign'].selection
            )
            # Exact match first
            if camp_name in valid_campaigns:
                campaign_val = camp_name
            else:
                # Partial match
                for key in valid_campaigns:
                    if camp_name.lower() in key.lower() or key.lower() in camp_name.lower():
                        campaign_val = key
                        break

        # Build remarks with all available sheet data
        remarks_lines = ['📋 From UrbanChat Sheet Sync']
        if location_val:
            remarks_lines.append(f'Location: {location_val}')
        if channel_val:
            remarks_lines.append(f'Channel: {channel_val}')
        if source_campaign_id:
            camp_rec = self.env['lead.source.campaign'].browse(source_campaign_id)
            remarks_lines.append(f'Campaign: {camp_rec.name}')

        re_enquiry_vals = {
            'lead_id':            existing_lead.id,
            'enquiry_date':       fields.Date.today(),
            'leads_source':       leads_source_id or False,
            'campaign':           campaign_val or False,
            'digital_lead_source': digital_source or False,
            'course_interested':  course_name or False,
            'remarks':            '\n'.join(remarks_lines),
            'review_state':       'pending',
            'review_required':    True,
        }
        self.env['lead.re.enquiry'].create(re_enquiry_vals)

    def _log_sync(self, **kw):
        self.env['urbenchat.sync.log'].create({'config_id': self.id, **kw})

    # ── Dashboard data ────────────────────────────────────────────────────────
    @api.model
    def get_dashboard_data(self):
        """
        Fast dashboard data using raw SQL — single pass per query instead of
        dozens of search_count ORM calls.
        """
        cr    = self.env.cr
        today = fields.Date.today()
        month_start = today.replace(day=1)
        week_start  = today - timedelta(days=today.weekday())
        day14_start = today - timedelta(days=13)

        # ── 1. Collect relevant source_campaign_ids & source_ids ──────────
        cr.execute("""
            SELECT DISTINCT am.source_campaign_id, am.leads_source_id, am.adset_id
            FROM   urbenchat_ad_mapping am
            WHERE  am.active = true
              AND  am.source_campaign_id IS NOT NULL
        """)
        rows_map = cr.fetchall()
        uc_camp_ids   = list({r[0] for r in rows_map})
        uc_source_ids = list({r[1] for r in rows_map if r[1]})
        # default sources from configs
        cr.execute("SELECT default_leads_source_id FROM urbenchat_sheet_config WHERE active=true AND default_leads_source_id IS NOT NULL")
        uc_source_ids += [r[0] for r in cr.fetchall()]
        uc_source_ids  = list(set(uc_source_ids))

        if not uc_camp_ids:
            # No mappings yet — return empty structure
            return {
                'kpi': {'total':0,'today':0,'week':0,'month':0,'admissions_total':0,'admissions_month':0},
                'adset_perf': [], 'campaign_perf': [], 'best_performers': {},
                'daily_data': [], 'quality_data': {}, 'sync_logs': [],
                'total_leads': 0, 'leads_today': 0, 'leads_this_week': 0, 'leads_this_month': 0,
            }

        camp_tuple  = tuple(uc_camp_ids)
        source_tuple = tuple(uc_source_ids) if uc_source_ids else (0,)

        # ── 2. KPI counts — one query ──────────────────────────────────────
        cr.execute("""
            SELECT
                COUNT(*)                                                          AS total,
                COUNT(*) FILTER (WHERE date_of_adding = %s)                      AS today,
                COUNT(*) FILTER (WHERE date_of_adding >= %s)                     AS week,
                COUNT(*) FILTER (WHERE date_of_adding >= %s)                     AS month,
                COUNT(*) FILTER (WHERE lead_quality = 'admission')               AS adm_total,
                COUNT(*) FILTER (WHERE lead_quality = 'admission'
                                   AND date_of_adding >= %s)                     AS adm_month
            FROM leads_logic
            WHERE leads_source IN %s
        """, (today, week_start, month_start, month_start, source_tuple))
        kpi_row = cr.fetchone()
        kpi = {
            'total':             kpi_row[0] or 0,
            'today':             kpi_row[1] or 0,
            'week':              kpi_row[2] or 0,
            'month':             kpi_row[3] or 0,
            'admissions_total':  kpi_row[4] or 0,
            'admissions_month':  kpi_row[5] or 0,
        }

        # ── 3. Lead quality breakdown — one query ──────────────────────────
        cr.execute("""
            SELECT lead_quality, COUNT(*)
            FROM   leads_logic
            WHERE  leads_source IN %s
            GROUP  BY lead_quality
        """, (source_tuple,))
        quality_map = dict(cr.fetchall())
        quality_labels = {
            'new': 'New', 'first_attempt': 'First Attempt',
            'hot': 'Hot', 'warm': 'Warm', 'cold': 'Cold',
            'not_responding': 'Not Responding', 'follow_up': 'Follow Up',
            'admission': 'Admission', 'bad_lead': 'Language Barrier',
            'crash_lead': 'Crash Lead',
        }
        quality_data = {label: quality_map.get(key, 0) for key, label in quality_labels.items()}

        # ── 4. Daily trend last 14 days — one query ────────────────────────
        cr.execute("""
            SELECT
                date_of_adding,
                COUNT(*)                                          AS leads,
                COUNT(*) FILTER (WHERE lead_quality = 'admission') AS admissions
            FROM  leads_logic
            WHERE leads_source IN %s
              AND date_of_adding >= %s
            GROUP BY date_of_adding
        """, (source_tuple, day14_start))
        daily_map = {str(r[0]): {'leads': r[1], 'admissions': r[2]} for r in cr.fetchall()}
        daily_data = []
        for i in range(13, -1, -1):
            d = str(today - timedelta(days=i))
            daily_data.append({
                'date':       d,
                'leads':      daily_map.get(d, {}).get('leads', 0),
                'admissions': daily_map.get(d, {}).get('admissions', 0),
            })

        # ── 5. Adset performance — one query covering all periods ──────────
        cr.execute("""
            SELECT
                am.adset_id,
                COUNT(*)                                                              AS all_leads,
                COUNT(*) FILTER (WHERE l.lead_quality = 'admission')                 AS all_adm,
                COUNT(*) FILTER (WHERE l.lead_quality = 'hot')                       AS all_hot,
                COUNT(*) FILTER (WHERE l.lead_quality = 'warm')                      AS all_warm,
                COUNT(*) FILTER (WHERE l.date_of_adding = %(today)s)                 AS today_leads,
                COUNT(*) FILTER (WHERE l.date_of_adding = %(today)s
                                   AND l.lead_quality = 'admission')                  AS today_adm,
                COUNT(*) FILTER (WHERE l.date_of_adding = %(today)s
                                   AND l.lead_quality = 'hot')                        AS today_hot,
                COUNT(*) FILTER (WHERE l.date_of_adding = %(today)s
                                   AND l.lead_quality = 'warm')                       AS today_warm,
                COUNT(*) FILTER (WHERE l.date_of_adding >= %(week)s)                 AS week_leads,
                COUNT(*) FILTER (WHERE l.date_of_adding >= %(week)s
                                   AND l.lead_quality = 'admission')                  AS week_adm,
                COUNT(*) FILTER (WHERE l.date_of_adding >= %(week)s
                                   AND l.lead_quality = 'hot')                        AS week_hot,
                COUNT(*) FILTER (WHERE l.date_of_adding >= %(week)s
                                   AND l.lead_quality = 'warm')                       AS week_warm,
                COUNT(*) FILTER (WHERE l.date_of_adding >= %(month)s)                AS month_leads,
                COUNT(*) FILTER (WHERE l.date_of_adding >= %(month)s
                                   AND l.lead_quality = 'admission')                  AS month_adm,
                COUNT(*) FILTER (WHERE l.date_of_adding >= %(month)s
                                   AND l.lead_quality = 'hot')                        AS month_hot,
                COUNT(*) FILTER (WHERE l.date_of_adding >= %(month)s
                                   AND l.lead_quality = 'warm')                       AS month_warm
            FROM  urbenchat_ad_mapping am
            JOIN  leads_logic l ON l.source_campaign_id = am.source_campaign_id
            WHERE am.active = true
              AND am.adset_id IS NOT NULL
            GROUP BY am.adset_id
        """, {'today': today, 'week': week_start, 'month': month_start})

        adset_rows = cr.fetchall()

        # Fetch adset + campaign names in one query
        if adset_rows:
            adset_ids = [r[0] for r in adset_rows]
            cr.execute("""
                SELECT a.id, a.name, c.name
                FROM   urbenchat_meta_adset a
                LEFT JOIN urbenchat_meta_campaign c ON c.id = a.campaign_id
                WHERE  a.id IN %s
            """, (tuple(adset_ids),))
            adset_info = {r[0]: (r[1], r[2] or '—') for r in cr.fetchall()}
        else:
            adset_info = {}

        def _pdata(leads, adm, hot, warm):
            return {'leads': leads or 0, 'admissions': adm or 0,
                    'hot': hot or 0, 'warm': warm or 0}

        adset_perf = []
        for r in adset_rows:
            adset_id = r[0]
            name, camp = adset_info.get(adset_id, ('Unknown', '—'))
            adset_perf.append({
                'adset_id': adset_id,
                'adset':    name,
                'campaign': camp,
                'all':   _pdata(r[1],  r[2],  r[3],  r[4]),
                'today': _pdata(r[5],  r[6],  r[7],  r[8]),
                'week':  _pdata(r[9],  r[10], r[11], r[12]),
                'month': _pdata(r[13], r[14], r[15], r[16]),
            })
        adset_perf.sort(key=lambda x: -x['month']['leads'])

        # ── 6. Campaign rollup (in Python from adset data) ─────────────────
        camp_rollup = {}
        for row in adset_perf:
            c = row['campaign']
            if c not in camp_rollup:
                camp_rollup[c] = {
                    'campaign': c,
                    'today': {'leads': 0, 'admissions': 0},
                    'week':  {'leads': 0, 'admissions': 0},
                    'month': {'leads': 0, 'admissions': 0},
                    'all':   {'leads': 0, 'admissions': 0},
                }
            for p in ('today', 'week', 'month', 'all'):
                camp_rollup[c][p]['leads']      += row[p]['leads']
                camp_rollup[c][p]['admissions'] += row[p]['admissions']
        campaign_perf = sorted(camp_rollup.values(), key=lambda x: -x['month']['leads'])

        # ── 7. Best performers ─────────────────────────────────────────────
        def best(data, period, key):
            filtered = [r for r in data if r[period][key] > 0]
            return max(filtered, key=lambda x: x[period][key]) if filtered else None

        best_performers = {
            p: {'by_leads': best(adset_perf, p, 'leads'),
                'by_admissions': best(adset_perf, p, 'admissions')}
            for p in ('today', 'week', 'month', 'all')
        }

        # ── 8. Sync logs ───────────────────────────────────────────────────
        cr.execute("""
            SELECT l.sync_date, c.name, l.leads_created, l.leads_skipped,
                   l.leads_error, l.state
            FROM   urbenchat_sync_log l
            LEFT JOIN urbenchat_sheet_config c ON c.id = l.config_id
            ORDER  BY l.sync_date DESC
            LIMIT  8
        """)
        log_data = [{
            'date':    r[0].strftime('%d %b %Y %H:%M') if r[0] else '—',
            'config':  r[1] or '—',
            'created': r[2] or 0,
            'skipped': r[3] or 0,
            'errors':  r[4] or 0,
            'state':   r[5] or '—',
        } for r in cr.fetchall()]

        # ── 9. Sync summary — leads created vs duplicates (re-enquiries) ──
        cr.execute("""
            SELECT
                COALESCE(SUM(leads_created), 0)  AS total_created,
                COALESCE(SUM(leads_skipped), 0)  AS total_duplicates,
                COALESCE(SUM(leads_error),   0)  AS total_errors,
                COUNT(*)                          AS total_runs
            FROM urbenchat_sync_log
        """)
        sr = cr.fetchone()
        sync_summary = {
            'total_created':    sr[0] or 0,
            'total_duplicates': sr[1] or 0,
            'total_errors':     sr[2] or 0,
            'total_runs':       sr[3] or 0,
        }

        # ── 10. Per-Ad (Ad ID) lead breakdown ─────────────────────────────
        cr.execute("""
            SELECT
                am.ad_id,
                sc.name                                                           AS source_camp,
                ads.name                                                          AS adset,
                mc.name                                                           AS campaign,
                COUNT(l.id)                                                       AS total,
                COUNT(l.id) FILTER (WHERE l.lead_quality = 'admission')          AS admissions,
                COUNT(l.id) FILTER (WHERE l.lead_quality = 'hot')                AS hot,
                COUNT(l.id) FILTER (WHERE l.date_of_adding >= %(month)s)         AS month,
                COUNT(l.id) FILTER (WHERE l.date_of_adding >= %(week)s)          AS week,
                COUNT(l.id) FILTER (WHERE l.date_of_adding =  %(today)s)         AS today
            FROM  urbenchat_ad_mapping am
            LEFT JOIN lead_source_campaign sc  ON sc.id  = am.source_campaign_id
            LEFT JOIN urbenchat_meta_adset ads ON ads.id = am.adset_id
            LEFT JOIN urbenchat_meta_campaign mc ON mc.id = ads.campaign_id
            LEFT JOIN leads_logic l ON l.source_campaign_id = am.source_campaign_id
            WHERE am.active = true
            GROUP BY am.ad_id, sc.name, ads.name, mc.name
            ORDER BY total DESC
            LIMIT 100
        """, {'today': today, 'week': week_start, 'month': month_start})

        ad_perf = []
        for r in cr.fetchall():
            if (r[4] or 0) > 0:
                ad_perf.append({
                    'ad_id':       r[0] or '—',
                    'source_camp': r[1] or '—',
                    'adset':       r[2] or '—',
                    'campaign':    r[3] or '—',
                    'total':       r[4] or 0,
                    'admissions':  r[5] or 0,
                    'hot':         r[6] or 0,
                    'month':       r[7] or 0,
                    'week':        r[8] or 0,
                    'today':       r[9] or 0,
                })

        return {
            'kpi':             kpi,
            'sync_summary':    sync_summary,
            'adset_perf':      adset_perf,
            'campaign_perf':   campaign_perf,
            'ad_perf':         ad_perf,
            'best_performers': best_performers,
            'daily_data':      daily_data,
            'quality_data':    quality_data,
            'sync_logs':       log_data,
            # legacy keys
            'total_leads':      kpi['total'],
            'leads_today':      kpi['today'],
            'leads_this_week':  kpi['week'],
            'leads_this_month': kpi['month'],
        }

    # ── Drill-down: open the actual leads behind a dashboard row ───────────
    def _period_domain(self, period):
        today = fields.Date.today()
        if period == 'today':
            return [('date_of_adding', '=', today)]
        if period == 'week':
            return [('date_of_adding', '>=', today - timedelta(days=today.weekday()))]
        if period == 'month':
            return [('date_of_adding', '>=', today.replace(day=1))]
        return []  # 'all'

    def action_view_adset_leads(self, adset_id, period='month'):
        """Called from the dashboard's 'View' button on an Ad Set Performance row."""
        campaign_ids = self.env['urbenchat.ad.mapping'].search([
            ('adset_id', '=', adset_id), ('active', '=', True),
        ]).mapped('source_campaign_id').ids
        domain = [('source_campaign_id', 'in', campaign_ids or [0])] + self._period_domain(period)
        adset = self.env['urbenchat.meta.adset'].browse(adset_id)
        return {
            'type':     'ir.actions.act_window',
            'name':     _('Leads — %s') % (adset.name or 'Ad Set'),
            'res_model': 'leads.logic',
            'view_mode': 'list,form',
            'domain':    domain,
            'target':    'current',
        }

    def action_view_ad_leads(self, ad_id, period='month'):
        """Called from the dashboard's 'View' button on a Per Ad Performance row."""
        mapping = self.env['urbenchat.ad.mapping'].search([('ad_id', '=', ad_id)], limit=1)
        campaign_id = mapping.source_campaign_id.id if mapping else 0
        domain = [('source_campaign_id', '=', campaign_id)] + self._period_domain(period)
        return {
            'type':     'ir.actions.act_window',
            'name':     _('Leads — Ad %s') % ad_id,
            'res_model': 'leads.logic',
            'view_mode': 'list,form',
            'domain':    domain,
            'target':    'current',
        }
