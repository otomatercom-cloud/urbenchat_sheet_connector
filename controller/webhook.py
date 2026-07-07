import json
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class UrbenchatWebhookController(http.Controller):
    """
    Optional HTTP endpoint for real-time lead push.
    POST /urbenchat/lead with JSON body matching your column mapping.

    Example payload:
    {
        "full_name": "Rahul Kumar",
        "phone_number": "9876543210",
        "email": "rahul@example.com",
        "ad_id": "120204838763420041",
        "course": "CA Foundation",
        "city": "Ernakulam"
    }

    To use from n8n:
        HTTP Request node → POST https://your-odoo.com/urbenchat/lead
        Body: JSON from the webhook trigger
    """

    @http.route('/urbenchat/lead', type='json', auth='public', methods=['POST'], csrf=False)
    def receive_lead(self, **kwargs):
        try:
            body = request.jsonrequest
            _logger.info('UrbanChat webhook received: %s', body)

            # Resolve config to get column mapping & defaults
            # We use the first active config (or match by a config_key field if needed)
            Config = request.env['urbenchat.sheet.config'].sudo()
            config = Config.search([('active', '=', True)], limit=1)
            if not config:
                return {'success': False, 'error': 'No active sheet configuration.'}

            # Build a row dict using the config's column mapping
            row = {}
            for col_field in ['col_name', 'col_phone', 'col_email', 'col_ad_id',
                               'col_course', 'col_city', 'col_unique_key']:
                col_name = getattr(config, col_field, '') or ''
                if col_name and col_name in body:
                    row[col_name] = body[col_name]

            # Build ad_id lookup
            AdMap = request.env['urbenchat.ad.mapping'].sudo()
            ad_map = {m.ad_id: m for m in AdMap.search([('active', '=', True)])}

            result = config._process_row(row, ad_map)
            return {'success': True, 'result': result}

        except Exception as e:
            _logger.error('UrbanChat webhook error: %s', e)
            return {'success': False, 'error': str(e)}

    @http.route('/urbenchat/ping', type='http', auth='public', methods=['GET'], csrf=False)
    def ping(self):
        return request.make_response('UrbanChat Connector OK', headers=[('Content-Type', 'text/plain')])
