from odoo import models, fields, api, _


class UrbenchatManualSyncWizard(models.TransientModel):
    """
    Simple wizard to pick which configs to sync,
    accessible from the Sync History menu.
    """
    _name = 'urbenchat.manual.sync.wizard'
    _description = 'UrbanChat Manual Sync Wizard'

    config_ids = fields.Many2many(
        'urbenchat.sheet.config',
        string='Configurations to Sync',
        domain=[('active', '=', True)],
    )

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        configs = self.env['urbenchat.sheet.config'].search([('active', '=', True)])
        if 'config_ids' in fields_list:
            res['config_ids'] = [(6, 0, configs.ids)]
        return res

    def action_sync(self):
        results = []
        for cfg in self.config_ids:
            try:
                r = cfg._do_sync(triggered_by='manual')
                results.append(
                    f"• {cfg.name}: {r['leads_created']} created, "
                    f"{r['leads_skipped']} skipped, {r['leads_error']} errors"
                )
            except Exception as e:
                results.append(f"• {cfg.name}: FAILED — {e}")

        message = '\n'.join(results) or 'No configs selected.'
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Manual Sync Complete'),
                'message': message,
                'type': 'success',
                'sticky': True,
            },
        }
