{
    'name': 'Ads Manager',
    'version': '17.0.1.0.0',
    'summary': 'Sync leads from UrbanChat / Meta Google Sheets — Ad ID campaign mapping — performance dashboard',
    'description': """
        - Fetch leads from a Google Sheet (UrbanChat / Meta Ads export)
        - Meta Campaign → Ad Set → Ad ID → Source Campaign hierarchy
        - Admission officer auto-assignment by name
        - Duplicate leads → auto Re-Enquiry
        - OWL Dashboard: adset performance, best performer, quality chart, sync history
        - Configurable auto-sync interval (default 5 min)
    """,
    'author': 'Ajesh / Otomater',
    'category': 'Marketing',
    'license': 'LGPL-3',
    'depends': ['custom_leads', 'base', 'mail'],
    'data': [
        'security/groups.xml',
        'security/ir.model.access.csv',
        'data/cron.xml',
        'views/ad_mapping_views.xml',
        'views/sheet_config_views.xml',
        'views/sync_log_views.xml',
        'views/dashboard_views.xml',
        'views/test_connection_wizard_views.xml',
        'views/bulk_ad_wizard_views.xml',
        'views/excel_import_wizard_views.xml',
        'views/menus.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'urbenchat_sheet_connector/static/src/css/dashboard.css',
            'urbenchat_sheet_connector/static/src/xml/dashboard.xml',
            'urbenchat_sheet_connector/static/src/xml/adset_performance_view.xml',
            'urbenchat_sheet_connector/static/src/xml/per_ad_performance_view.xml',
            'urbenchat_sheet_connector/static/src/js/dashboard.js',
            'urbenchat_sheet_connector/static/src/js/adset_performance_view.js',
            'urbenchat_sheet_connector/static/src/js/per_ad_performance_view.js',
        ],
    },
    'installable': True,
    'application': True,
    'auto_install': False,
}
