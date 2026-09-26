# -*- coding: utf-8 -*-
{
    'name': 'Magento 2 Odoo Connector: Orders, Stock, Shipments (Miko)',
    'version': '19.0.1.0.0',
    'summary': 'Two-way Magento 2 Odoo sync on a schedule: import orders, '
               'products and customers, publish stock and create shipments',
    'description': """
Connect a Magento 2 store to Odoo and keep both sides in step, on a schedule.

Built on the Magento 2 REST API with an integration access token, and written
around how Magento actually models a shop rather than how another platform does.
Configurable products are shells, so the sellable simple products are what reach
Odoo. SKU is the business key, so it is carried and URL encoded everywhere stock
is written. Shipments are real objects, so a validated Odoo delivery creates a
Magento shipment with its tracking number.

What it will not do matters as much as what it will. It never imports the same
order twice. It never invents a tax: an unmapped rate stops the order rather than
quietly producing an invoice short by the tax. And it checks every imported order
against the total Magento actually charged, leaving anything that disagrees as a
quotation with the difference spelled out.
""",
    'author': 'Tripster Developers',
    'website': 'https://miko.co.nz/odoo/magento-connector',
    'category': 'eCommerce',
    'license': 'OPL-1',
    'depends': ['miko_ecommerce_core', 'sale_management', 'stock', 'account'],
    'external_dependencies': {'python': ['requests']},
    'data': [
        'security/ir.model.access.csv',
        'data/ir_cron.xml',
        'views/miko_magento_views.xml',
    ],
    'images': ['images/banner.gif', 'images/banner.png'],
    'price': 429.00,
    'currency': 'USD',
    'application': False,
    'installable': True,
    'support': 'support@tripsterdevelopers.com',
}
