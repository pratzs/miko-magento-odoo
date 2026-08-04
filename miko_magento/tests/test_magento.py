# -*- coding: utf-8 -*-
"""Tests for the Magento 2 connector.

Nothing here touches the network. The HTTP client runs against a stubbed
transport and the import code against recorded payloads, so the suite gives the
same answer offline as in CI.

Written around the ways a connector loses money: importing twice, importing a
configurable shell as if it were sellable, dropping a tax, and importing a total
that is not what the customer paid.
"""
import json

from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase

from ..models.magento_client import (MagentoClient, MagentoError, redact,
                                     substitute)


class FakeResponse(object):
    def __init__(self, status_code=200, body=None, headers=None, text=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(
            body if body is not None else {})

    def json(self):
        if self._body is None:
            raise ValueError('not json')
        return self._body


class FakeClient(object):
    """Answers by looking at the method and path it was given."""

    def __init__(self, items=None, records=None):
        self.calls = []
        self.items = items or {}
        self.records = records or {}

    def call(self, method, path, params=None, payload=None):
        self.calls.append((method, path, params, payload))
        return self.records.get(path, {'entity_id': 1})

    def search(self, path, filters=None, page_size=50, max_pages=2000):
        for item in self.items.get(path, []):
            yield item

    @staticmethod
    def quote(value):
        return str(value).replace('/', '%2F')


class MagentoCase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.channel = self.env['miko.ecommerce.channel'].create({
            'name': 'Northwind Magento',
            'platform': 'magento',
            'magento_url': 'https://shop.example.com',
            'company_id': self.company.id,
        })
        self.channel.sudo().magento_token = 'abc123token'
        self.client = FakeClient()

    def _with_client(self, client=None):
        return patch.object(type(self.channel), '_magento_client',
                            return_value=client or self.client)

    def _country(self):
        return (self.company.account_fiscal_country_id or self.company.country_id
                or self.env['res.country'].search([('code', '=', 'NZ')], limit=1))

    def _tax_group(self):
        Group = self.env['account.tax.group']
        domain = [('company_id', '=', self.company.id)] if 'company_id' in Group._fields else []
        group = Group.search(domain, limit=1)
        if group:
            return group
        values = {'name': 'Miko Magento Test Group'}
        if 'company_id' in Group._fields:
            values['company_id'] = self.company.id
        if 'country_id' in Group._fields:
            values['country_id'] = self._country().id
        return Group.create(values)

    def _tax(self, name, amount):
        """country_id and tax_group_id are both NOT NULL on account_tax."""
        return self.env['account.tax'].create({
            'name': name, 'amount': amount, 'amount_type': 'percent',
            'type_tax_use': 'sale', 'company_id': self.company.id,
            'country_id': self._country().id,
            'tax_group_id': self._tax_group().id})


class TestClient(MagentoCase):

    def test_plain_http_is_refused_outright(self):
        with self.assertRaises(MagentoError) as caught:
            MagentoClient('http://shop.example.com', 'tok')
        self.assertIn('https', str(caught.exception))

    def test_a_token_is_never_written_into_an_error(self):
        out = redact("failed with Authorization: Bearer abc123SECRETtoken here")
        self.assertNotIn('abc123SECRETtoken', out)
        self.assertIn('***', out)

    def test_missing_credentials_are_refused_before_any_request(self):
        with self.assertRaises(MagentoError):
            MagentoClient('https://shop.example.com', '')

    def test_magento_placeholders_are_substituted(self):
        """Left raw the merchant sees a message with no SKU in it."""
        self.assertEqual(
            substitute("The product %1 doesn't exist.", {'1': 'WIDGET-1'}),
            "The product WIDGET-1 doesn't exist.")

    def test_placeholders_also_work_from_a_list(self):
        self.assertEqual(substitute("%1 and %2", ['a', 'b']), "a and b")

    def test_an_unactivated_integration_says_so(self):
        client = MagentoClient('https://shop.example.com', 'tok')
        with patch.object(client._session, 'request', return_value=FakeResponse(401, {})):
            with self.assertRaises(MagentoError) as caught:
                client.call('GET', 'products')
        self.assertIn('Authorized', str(caught.exception))

    def test_a_403_points_at_the_api_resources(self):
        client = MagentoClient('https://shop.example.com', 'tok')
        with patch.object(client._session, 'request', return_value=FakeResponse(403, {})):
            with self.assertRaises(MagentoError) as caught:
                client.call('GET', 'products')
        self.assertIn('API resources', str(caught.exception))

    def test_html_instead_of_json_names_the_likely_cause(self):
        client = MagentoClient('https://shop.example.com', 'tok')
        with patch.object(client._session, 'request',
                          return_value=FakeResponse(200, None, text='<html>502</html>')):
            with self.assertRaises(MagentoError) as caught:
                client.call('GET', 'products')
        self.assertIn('not JSON', str(caught.exception))

    def test_a_429_is_retried_then_succeeds(self):
        client = MagentoClient('https://shop.example.com', 'tok')
        responses = [FakeResponse(429, {}, headers={'Retry-After': '0'}),
                     FakeResponse(200, {'items': [], 'total_count': 0})]
        with patch.object(client._session, 'request', side_effect=responses), \
                patch('odoo.addons.miko_magento.models.magento_client.time.sleep'):
            body = client.call('GET', 'products')
        self.assertEqual(body['total_count'], 0)

    def test_search_stops_at_the_reported_total(self):
        client = MagentoClient('https://shop.example.com', 'tok')
        pages = [FakeResponse(200, {'items': [{'id': 1}], 'total_count': 2}),
                 FakeResponse(200, {'items': [{'id': 2}], 'total_count': 2})]
        with patch.object(client._session, 'request', side_effect=pages):
            got = list(client.search('products', page_size=1))
        self.assertEqual([r['id'] for r in got], [1, 2])

    def test_search_sorts_by_entity_id_so_records_cannot_shift(self):
        client = MagentoClient('https://shop.example.com', 'tok')
        seen = {}

        def capture(method, url, params=None, **kw):
            seen.update(params or {})
            return FakeResponse(200, {'items': [], 'total_count': 0})

        with patch.object(client._session, 'request', side_effect=capture):
            list(client.search('products'))
        self.assertEqual(seen.get('searchCriteria[sortOrders][0][field]'), 'entity_id')
        self.assertEqual(seen.get('searchCriteria[sortOrders][0][direction]'), 'ASC')

    def test_a_sku_with_a_slash_is_url_encoded(self):
        self.assertNotIn('/', MagentoClient.quote('SET/A B'))

    def test_a_store_code_scopes_the_api_path(self):
        client = MagentoClient('https://shop.example.com', 'tok', 'default')
        self.assertIn('/rest/default/V1', client.api)


class TestChannel(MagentoCase):

    def test_an_http_address_is_refused(self):
        with self.assertRaises(ValidationError):
            self.channel.magento_url = 'http://shop.example.com'

    def test_a_rest_path_is_refused(self):
        with self.assertRaises(ValidationError):
            self.channel.magento_url = 'https://shop.example.com/rest/V1'

    def _draft(self, typed):
        """An onchange runs on a draft; a saved write fires the constraint."""
        return self.env['miko.ecommerce.channel'].new({
            'name': 'Draft', 'platform': 'magento', 'magento_url': typed})

    def test_a_pasted_admin_url_is_repaired(self):
        draft = self._draft('https://shop.example.com/admin/dashboard')
        draft._onchange_magento_url()
        self.assertEqual(draft.magento_url, 'https://shop.example.com')

    def test_http_is_upgraded_while_typing(self):
        draft = self._draft('http://shop.example.com/')
        draft._onchange_magento_url()
        self.assertEqual(draft.magento_url, 'https://shop.example.com')

    def test_a_failure_is_reported_not_raised(self):
        with patch.object(type(self.channel), '_magento_client') as m:
            m.return_value.store_config.side_effect = MagentoError('nope')
            self.channel.action_test_connection()
        self.assertEqual(self.channel.connection_state, 'error')


class TestDirections(MagentoCase):

    def test_products_and_customers_are_import_only_for_now(self):
        """Export is not built for Magento, so the setting must not offer it.

        A direction that can be selected and silently does nothing is worse than
        one that is not offered: the merchant sets it, believes the catalogue is
        going out, and finds out weeks later that it never was.
        """
        for field in ('product_direction', 'customer_direction'):
            options = dict(self.channel._fields[field].selection)
            self.assertEqual(list(options), ['in'],
                             '%s must offer only the direction that exists' % field)

    def test_importing_is_allowed(self):
        self.assertTrue(
            self.channel._require_direction('product_direction', 'in', 'Products'))


class TestProducts(MagentoCase):

    def _simple(self, pid=100, sku='WIDGET-1', type_id='simple'):
        return {'id': pid, 'sku': sku, 'name': 'Alpine Widget', 'price': 89.0,
                'type_id': type_id,
                'extension_attributes': {'stock_item': {'item_id': 555, 'qty': 3}}}

    def test_a_simple_product_imports_once_and_keeps_its_sku(self):
        with self._with_client():
            product = self.channel._job_import_product(self._simple())
            again = self.channel._job_import_product(self._simple())
        self.assertEqual(product, again)
        self.assertEqual(product.magento_sku, 'WIDGET-1')
        self.assertEqual(product.magento_stock_item_id, '555')

    def test_a_configurable_shell_is_not_imported_as_sellable(self):
        """Its simple children carry the SKU, price and stock."""
        client = FakeClient(items={'products': [
            self._simple(pid=1, sku='PARENT', type_id='configurable'),
            self._simple(pid=2, sku='CHILD-1')]})
        self.channel.product_direction = 'in'
        with self._with_client(client):
            self.assertEqual(self.channel._import_magento_products(), 1)
        self.assertFalse(self.env['product.product'].search(
            [('magento_sku', '=', 'PARENT')]))

    def test_an_existing_sku_is_reused_rather_than_duplicated(self):
        existing = self.env['product.product'].create(
            {'name': 'Already here', 'default_code': 'WIDGET-1'})
        with self._with_client():
            product = self.channel._job_import_product(self._simple())
        self.assertEqual(product, existing)

    def test_a_virtual_product_becomes_a_service(self):
        with self._with_client():
            product = self.channel._job_import_product(
                self._simple(pid=101, sku='VIRT', type_id='virtual'))
        self.assertEqual(product.type, 'service')


class TestCustomers(MagentoCase):

    def _customer(self, cid=200, email='ada@example.com'):
        return {'id': cid, 'email': email, 'firstname': 'Ada', 'lastname': 'Lovelace',
                'addresses': [{'street': ['1 Queen Street', 'Level 2'],
                               'city': 'Auckland', 'postcode': '1010',
                               'country_id': 'NZ', 'telephone': '+6421000000'}]}

    def test_an_existing_contact_is_reused_on_an_exact_email_match(self):
        existing = self.env['res.partner'].create(
            {'name': 'Ada L', 'email': 'ada@example.com'})
        self.assertEqual(self.channel._upsert_magento_customer(self._customer()),
                         existing)

    def test_a_value_typed_in_odoo_is_never_overwritten(self):
        self.env['res.partner'].create(
            {'name': 'Ada L', 'email': 'ada@example.com', 'street': '99 Corrected Rd'})
        partner = self.channel._upsert_magento_customer(self._customer())
        self.assertEqual(partner.street, '99 Corrected Rd')

    def test_both_street_lines_are_carried_across(self):
        partner = self.channel._upsert_magento_customer(
            self._customer(cid=201, email='b@example.com'))
        self.assertEqual(partner.street, '1 Queen Street')
        self.assertEqual(partner.street2, 'Level 2')

    def test_the_country_is_resolved_from_the_iso_code(self):
        partner = self.channel._upsert_magento_customer(
            self._customer(cid=202, email='c@example.com'))
        self.assertEqual(partner.country_id.code, 'NZ')

    def test_a_customer_with_no_name_still_gets_a_findable_one(self):
        node = self._customer(cid=203, email='noname@example.com')
        node['firstname'] = node['lastname'] = ''
        self.assertEqual(
            self.channel._upsert_magento_customer(node).name, 'noname@example.com')


class TestOrders(MagentoCase):

    def setUp(self):
        super().setUp()
        self.product = self.env['product.product'].create(
            {'name': 'Imported Widget', 'default_code': 'WIDGET-1',
             'type': 'consu', 'list_price': 100.0})
        self.channel.default_customer_id = self.env['res.partner'].create(
            {'name': 'Magento guest'})

    def _order(self, eid=300, total='100.00', tax_percent=0, tax_amount=0,
               discount=0, shipping=0, item_type='simple'):
        return {
            'entity_id': eid, 'increment_id': '00000%s' % eid,
            'created_at': '2026-03-01 09:00:00', 'grand_total': total,
            'customer_id': 0, 'customer_email': 'buyer@example.com',
            'customer_firstname': 'Bo', 'customer_lastname': 'Yer',
            'billing_address': {'street': ['2 High St'], 'city': 'Wellington',
                                'postcode': '6011', 'country_id': 'NZ'},
            'shipping_amount': shipping,
            'items': [{'item_id': 1, 'sku': 'WIDGET-1', 'name': 'Imported Widget',
                       'product_id': 900, 'product_type': item_type,
                       'qty_ordered': 1, 'price': 100.0, 'row_total': 100.0,
                       'discount_amount': discount, 'tax_percent': tax_percent,
                       'tax_amount': tax_amount}],
        }

    def test_an_order_imports_with_the_right_line_and_total(self):
        with self._with_client():
            order = self.channel._import_one_magento_order(self._order())
        self.assertEqual(order.order_line.product_id, self.product)
        self.assertEqual(order.amount_total, 100.0)
        self.assertTrue(order.magento_total_matches)

    def test_importing_the_same_order_twice_creates_one_order(self):
        node = self._order(eid=301)
        with self._with_client():
            first = self.channel._job_import_order(node)
            again = self.channel._job_import_order(node)
        self.assertEqual(first, again)
        self.assertEqual(self.env['sale.order'].search_count(
            [('magento_increment_id', '=', '00000301')]), 1)

    def test_a_configurable_line_is_skipped_so_the_order_is_not_doubled(self):
        """The parent and its simple child carry the same money."""
        node = self._order(eid=302)
        node['items'].append(dict(node['items'][0], item_id=2, sku='PARENT',
                                  product_type='configurable'))
        with self._with_client():
            order = self.channel._import_one_magento_order(node)
        self.assertEqual(len(order.order_line), 1)

    def test_a_line_discount_is_kept_as_a_discount(self):
        with self._with_client():
            order = self.channel._import_one_magento_order(
                self._order(eid=303, total='80.00', discount=20.0))
        self.assertEqual(order.order_line.price_unit, 100.0)
        self.assertAlmostEqual(order.order_line.discount, 20.0, places=4)

    def test_a_total_that_disagrees_is_flagged_and_left_as_a_quotation(self):
        with self._with_client():
            order = self.channel._import_one_magento_order(
                self._order(eid=304, total='150.00'))
        self.assertFalse(order.magento_total_matches)
        self.assertEqual(order.state, 'draft')

    def test_a_flagged_order_is_never_auto_confirmed(self):
        self.channel.auto_confirm_orders = True
        with self._with_client():
            order = self.channel._import_one_magento_order(
                self._order(eid=305, total='150.00'))
        self.assertEqual(order.state, 'draft')

    def test_an_unmapped_tax_stops_the_import_by_default(self):
        """7.5% deliberately: a rate the demo chart also uses gets auto-suggested."""
        node = self._order(eid=306, total='107.50', tax_percent=7.5, tax_amount=7.5)
        with self._with_client():
            with self.assertRaises(UserError) as caught:
                self.channel._import_one_magento_order(node)
        self.assertIn('7.5', str(caught.exception))

    def test_a_mapped_tax_is_applied_to_the_line(self):
        tax = self._tax('Magento 7.5', 7.5)
        self.env['miko.ecommerce.tax'].create({
            'channel_id': self.channel.id, 'title': 'Tax 7.5%',
            'rate': 0.075, 'tax_id': tax.id})
        node = self._order(eid=307, total='107.50', tax_percent=7.5, tax_amount=7.5)
        with self._with_client():
            order = self.channel._import_one_magento_order(node)
        self.assertIn(tax, order.order_line[self.channel._sol_tax_field()])

    def test_ignoring_unmapped_taxes_is_possible_but_never_the_default(self):
        self.assertEqual(self.channel.unmapped_tax_policy, 'block')
        self.channel.unmapped_tax_policy = 'ignore'
        with self._with_client():
            self.assertTrue(self.channel._import_one_magento_order(
                self._order(eid=308, total='100.00', tax_percent=7.5, tax_amount=7.5)))

    def test_shipping_arrives_as_its_own_line(self):
        with self._with_client():
            order = self.channel._import_one_magento_order(
                self._order(eid=309, total='110.00', shipping=10.0))
        self.assertEqual(len(order.order_line), 2)
        self.assertAlmostEqual(order.amount_total, 110.0, places=2)

    def test_a_guest_with_an_email_becomes_a_contact(self):
        with self._with_client():
            order = self.channel._import_one_magento_order(self._order(eid=310))
        self.assertEqual(order.partner_id.email, 'buyer@example.com')

    def test_a_guest_with_no_email_and_no_fallback_says_what_to_set(self):
        self.channel.default_customer_id = False
        node = self._order(eid=311)
        node['customer_email'] = ''
        with self._with_client():
            with self.assertRaises(UserError) as caught:
                self.channel._import_one_magento_order(node)
        self.assertIn('fallback customer', str(caught.exception))

    def test_an_unknown_product_can_be_refused_instead_of_invented(self):
        self.channel.create_missing_products = False
        node = self._order(eid=312)
        node['items'][0]['sku'] = 'NOT-IN-ODOO'
        node['items'][0]['product_id'] = 9999
        with self._with_client():
            with self.assertRaises(UserError) as caught:
                self.channel._import_one_magento_order(node)
        self.assertIn('NOT-IN-ODOO', str(caught.exception))

    def test_the_order_date_comes_from_magento_not_from_now(self):
        with self._with_client():
            order = self.channel._import_one_magento_order(self._order(eid=313))
        self.assertEqual((order.date_order.year, order.date_order.month,
                          order.date_order.day), (2026, 3, 1))


class TestStockAndShipments(MagentoCase):

    def setUp(self):
        super().setUp()
        self.channel.export_stock = True
        self.product = self.env['product.product'].create(
            {'name': 'Stocked', 'type': 'consu', 'default_code': 'ST-1',
             'magento_sku': 'ST-1', 'magento_stock_item_id': '777'})

    def test_publishing_is_refused_while_the_setting_is_off(self):
        self.channel.export_stock = False
        with self.assertRaises(UserError):
            self.channel._export_magento_stock()

    def test_a_negative_quantity_is_never_sent(self):
        with patch.object(type(self.product), 'free_qty', -5):
            self.assertEqual(self.channel._quantity_for(self.product), 0)

    def test_a_product_without_a_stock_item_id_is_skipped(self):
        """Magento writes stock against the SKU and its stock item id."""
        self.product.magento_stock_item_id = False
        self.env['miko.ecommerce.mapping'].link(self.channel, self.product, 400)
        with self._with_client():
            self.assertEqual(self.channel._export_magento_stock(), 0)

    def test_a_mapped_product_is_written_to_by_sku(self):
        self.env['miko.ecommerce.mapping'].link(self.channel, self.product, 401)
        with self._with_client():
            self.assertEqual(self.channel._export_magento_stock(), 1)
        put = [c for c in self.client.calls if c[0] == 'PUT'][0]
        self.assertIn('stockItems/777', put[1])

    def test_a_shipment_carries_the_tracking_number(self):
        self.channel.export_fulfilments = True
        order = self.env['sale.order'].create(
            {'partner_id': self.env['res.partner'].create({'name': 'B'}).id})
        self.env['miko.ecommerce.mapping'].link(self.channel, order, 500, '000500')
        with self._with_client():
            self.assertTrue(self.channel.create_magento_shipment(order, '1Z999', 'UPS'))
        post = [c for c in self.client.calls if c[0] == 'POST'][0]
        self.assertIn('order/500/ship', post[1])
        self.assertEqual(post[3]['tracks'][0]['track_number'], '1Z999')

    def test_a_shipment_without_tracking_still_ships_everything(self):
        """An empty body ships every remaining item, which is what a validated
        Odoo delivery of the whole order means."""
        self.channel.export_fulfilments = True
        order = self.env['sale.order'].create(
            {'partner_id': self.env['res.partner'].create({'name': 'C'}).id})
        self.env['miko.ecommerce.mapping'].link(self.channel, order, 501, '000501')
        with self._with_client():
            self.channel.create_magento_shipment(order)
        post = [c for c in self.client.calls if c[0] == 'POST'][0]
        self.assertNotIn('tracks', post[3])

    def test_an_order_from_another_store_is_not_this_channels_business(self):
        self.channel.export_fulfilments = True
        other = self.env['sale.order'].create(
            {'partner_id': self.env['res.partner'].create({'name': 'D'}).id})
        with self._with_client():
            self.assertFalse(self.channel.create_magento_shipment(other))


class TestScheduledSync(MagentoCase):

    def test_installing_changes_nothing_until_it_is_switched_on(self):
        self.assertFalse(self.channel.auto_sync)
        ran = []
        with patch.object(type(self.channel), '_run_magento_sync',
                          side_effect=lambda: ran.append(1)):
            self.env['miko.ecommerce.channel']._cron_magento_sync()
        self.assertEqual(ran, [])

    def test_a_store_that_is_not_due_yet_is_skipped(self):
        self.channel.write({'auto_sync': True, 'sync_interval_minutes': 60,
                            'last_sync': fields.Datetime.now()})
        self.assertFalse(self.channel._magento_sync_due())

    def test_a_store_already_running_is_never_started_twice(self):
        self.channel.write({'auto_sync': True, 'sync_running': True})
        self.assertFalse(self.channel._magento_sync_due())

    def test_a_failing_sync_clears_the_running_flag(self):
        self.channel.write({'auto_sync': True, 'sync_orders': True})
        with patch.object(type(self.channel), '_import_magento_orders',
                          side_effect=Exception('boom')):
            self.channel._run_magento_sync()
        self.assertFalse(self.channel.sync_running)
        self.assertIn('Failed', self.channel.last_sync_message or '')


class TestFieldMapping(MagentoCase):

    def test_defaults_are_seeded_once_and_not_duplicated(self):
        self.channel._seed_field_maps()
        first = len(self.channel.field_map_ids)
        self.channel._seed_field_maps()
        self.assertEqual(len(self.channel.field_map_ids), first)
        self.assertTrue(first, 'Magento must seed its own defaults')
