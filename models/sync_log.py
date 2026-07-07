from odoo import fields, models


class UrbenchatSyncLog(models.Model):
    """
    One record per sync run. Linked to a sheet config.
    """
    _name = 'urbenchat.sync.log'
    _description = 'UrbanChat Sheet Sync Log'
    _order = 'sync_date desc'

    config_id = fields.Many2one(
        'urbenchat.sheet.config',
        string='Sheet Config',
        ondelete='cascade',
    )
    sync_date = fields.Datetime(
        string='Sync Date',
        default=fields.Datetime.now,
        readonly=True,
    )
    rows_fetched = fields.Integer(string='Rows Fetched', default=0)
    leads_created = fields.Integer(string='Leads Created', default=0)
    leads_skipped = fields.Integer(string='Skipped (duplicate)', default=0)
    leads_error = fields.Integer(string='Errors', default=0)
    state = fields.Selection(
        [
            ('success', 'Success'),
            ('partial', 'Partial (some errors)'),
            ('failed', 'Failed'),
        ],
        string='State',
        default='success',
    )
    error_details = fields.Text(string='Error Details')
    triggered_by = fields.Selection(
        [('cron', 'Cron Job'), ('manual', 'Manual')],
        string='Triggered By',
        default='manual',
    )
