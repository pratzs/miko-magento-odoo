# -*- coding: utf-8 -*-
"""Magento products, customers, orders, stock and shipments.

Where this differs from the other connectors, Magento is the reason:

* **Configurable products are not the sellable thing.** A `configurable` product
  is a shell; its `simple` children carry the SKU, price and stock. So the
  sellable unit imported into Odoo is the simple product, and a configurable is
  imported as the template that groups them. Treating a configurable as sellable
  is how a connector ends up ordering something that has no stock record.
* **SKU is the business key.** Stock is written to `/products/{sku}/stockItems/`
  and SKUs routinely contain slashes and spaces, so every one is URL-encoded.
* **Tax has no name.** Magento exposes `tax_percent` on the order item and no
  label, so mapping is keyed on the rate with a generated label. It still refuses
  to guess: an unmapped rate stops the order rather than under-billing.
* **Shipments are real objects**, unlike WooCommerce. `POST /order/{id}/ship`
  with the item quantities and the tracking number, which is much closer to
  Shopify's model, and it returns a shipment id worth keeping.
"""
import logging

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

SELLABLE = ('simple', 'virtual', 'downloadable')


def money(value):
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    magento_increment_id = fields.Char(
        readonly=True, copy=False, index=True,
        help="The order number as the customer sees it in Magento.")
    magento_total = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        help="What Magento said the customer was charged.")
    magento_total_matches = fields.Boolean(
        readonly=True, copy=False, default=True,
        help="False when the Odoo total does not agree with the Magento total. "
             "Not safe to invoice until the difference is understood.")


class ProductProduct(models.Model):
    _inherit = 'product.product'

    magento_sku = fields.Char(
        readonly=True, copy=False, index=True,
        help="Magento addresses stock and several other endpoints by SKU rather "
             "than by id, so it is kept alongside the mapping.")
    magento_stock_item_id = fields.Char(
        readonly=True, copy=False,
        help="Magento's stock item id for this SKU. Required to write a quantity.")


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    magento_shipment_id = fields.Char(
        readonly=True, copy=False,
        help="The Magento shipment this delivery created. Its presence is what "
             "stops the same delivery being shipped twice.")

    def _action_done(self):
        result = super()._action_done()
        for picking in self:
            try:
                picking._miko_ship_magento()
            except Exception:        # noqa: BLE001
                # A delivery must always complete in Odoo. A shop being
                # unreachable cannot be allowed to block the warehouse.
                _logger.exception(
                    "miko_magento: could not create shipment for %s", picking.name)
        return result

    def _miko_ship_magento(self):
        self.ensure_one()
        if self.picking_type_id.code != 'outgoing' or self.state != 'done':
            return False
        if self.magento_shipment_id:
            return False
        order = (self.sale_id if 'sale_id' in self._fields
                 else self.env['sale.order'].browse())
        if not order:
            return False
        channel = self.env['miko.ecommerce.mapping']._channel_for(order)
        if not channel or channel.platform != 'magento' or not channel.export_fulfilments:
            return False
        tracking = (self.carrier_tracking_ref
                    if 'carrier_tracking_ref' in self._fields else None)
        carrier = self.carrier_id.name if 'carrier_id' in self._fields and self.carrier_id else ''
        shipment = channel.create_magento_shipment(order, (tracking or '').strip(), carrier)
        if shipment:
            self.magento_shipment_id = str(shipment)
            return True
        return False


class MikoEcommerceChannel(models.Model):
    _inherit = 'miko.ecommerce.channel'

    export_stock = fields.Boolean(
        string='Publish stock to Magento', default=False,
        help="Off by default. Writing stock into a live shop is not something a "
             "module should start doing because it was installed.")
    stock_source = fields.Selection(
        [('free', 'Available to promise'), ('on_hand', 'On hand')],
        default='free', required=True, string='Quantity to publish',
        help="Available to promise excludes stock already reserved for other "
             "orders. Publishing on hand is how the same unit gets sold twice.")
    export_fulfilments = fields.Boolean(
        string='Create shipments in Magento', default=False,
        help="When the Odoo delivery is validated, create the Magento shipment "
             "and pass the tracking number across. Off by default: this writes "
             "into a live shop.")
    magento_carrier_code = fields.Char(
        string='Carrier code', default='custom',
        help="Magento's code for the carrier on a tracking record. 'custom' works "
             "everywhere; a shop using a specific carrier module can name it here.")

    # ------------------------------------------------------------- products
    def action_import_products(self):
        for channel in self:
            channel._import_magento_products()
        return True

    def _import_magento_products(self):
        self.ensure_one()
        self._require_direction('product_direction', 'in', _("Products"))
        client = self._magento_client()
        Job = self.env['miko.ecommerce.job']
        filters = []
        if self.import_from_date:
            filters.append(('updated_at',
                            fields.Datetime.to_string(self.import_from_date), 'gteq'))
        seen = 0
        for node in client.search('products', filters):
            if node.get('type_id') not in SELLABLE:
                # A configurable is a shell with no stock record of its own; its
                # simple children are the sellable units and arrive separately.
                continue
            job = Job.enqueue(self, 'import_product', node.get('id'), node,
                              external_ref=node.get('sku'))
            try:
                product = self._job_import_product(node, job)
                job.mark_done(product)
                seen += 1
            except Exception as err:        # noqa: BLE001 - kept, not lost
                _logger.exception("miko_magento: product %s failed", node.get('sku'))
                job.mark_failed(err, blocked=job._is_blocking(err),
                                guidance=job._guidance_for(err))
        self._touch_sync()
        return seen

    def _job_import_product(self, payload, job=None):
        """Import one sellable Magento product. Shared with Retry."""
        self.ensure_one()
        Mapping = self.env['miko.ecommerce.mapping']
        Product = self.env['product.product']
        sku = (payload.get('sku') or '').strip()

        product = Mapping.find_odoo_record(self, 'product.product', payload['id'])
        if not product and sku:
            product = Product.search([('default_code', '=', sku)], limit=1)

        values = {
            'name': payload.get('name') or _('Unnamed Magento product'),
            'type': 'service' if payload.get('type_id') == 'virtual' else 'consu',
            'sale_ok': True,
        }
        if sku:
            values['default_code'] = sku
            values['magento_sku'] = sku
        price = payload.get('price')
        if price is not None:
            values['lst_price'] = money(price)
        stock_item = ((payload.get('extension_attributes') or {}).get('stock_item') or {})
        if stock_item.get('item_id'):
            values['magento_stock_item_id'] = str(stock_item['item_id'])
        values.update(self._apply_maps_in('product', payload, 'product.product'))

        if not product:
            product = Product.create(values)
            # Linked in the same breath as the create: anything in between is a
            # window where a crash leaves an orphan the next run creates again.
            Mapping.link(self, product, payload['id'], sku)
        else:
            product.write(values)
            Mapping.link(self, product, payload['id'], sku)
        return product

    # ------------------------------------------------------------ customers
    def action_import_customers(self):
        for channel in self:
            channel._import_magento_customers()
        return True

    def _import_magento_customers(self):
        self.ensure_one()
        self._require_direction('customer_direction', 'in', _("Customers"))
        client = self._magento_client()
        Job = self.env['miko.ecommerce.job']
        count = 0
        for node in client.search('customers/search'):
            job = Job.enqueue(self, 'import_customer', node.get('id'), node,
                              external_ref=node.get('email'))
            try:
                partner = self._job_import_customer(node, job)
                job.mark_done(partner)
                count += 1
            except Exception as err:        # noqa: BLE001 - kept, not lost
                _logger.exception("miko_magento: customer %s failed", node.get('id'))
                job.mark_failed(err, blocked=job._is_blocking(err),
                                guidance=job._guidance_for(err))
        self._touch_sync()
        return count

    def _job_import_customer(self, payload, job=None):
        self.ensure_one()
        return self._upsert_magento_customer(payload)

    def _upsert_magento_customer(self, node):
        Mapping = self.env['miko.ecommerce.mapping']
        Partner = self.env['res.partner']
        partner = Mapping.find_odoo_record(self, 'res.partner', node['id'])
        email = (node.get('email') or '').strip()

        if not partner and email:
            # Exact email only. Anything looser merges strangers, and contacts do
            # not come apart again once orders are attached.
            partner = Partner.search([
                ('email', '=ilike', email), ('parent_id', '=', False),
                '|', ('company_id', '=', self.company_id.id),
                     ('company_id', '=', False)], limit=1)

        values = self._magento_partner_values(node)
        if not partner:
            partner = Partner.create(values)
        else:
            # Fill what is empty, never overwrite what somebody typed in Odoo.
            partner.write({k: v for k, v in values.items() if v and not partner[k]})
        Mapping.link(self, partner, node['id'], email or partner.name)
        return partner

    def _magento_partner_values(self, node):
        name = ' '.join(p for p in [(node.get('firstname') or '').strip(),
                                    (node.get('lastname') or '').strip()] if p)
        if not name:
            # Never create a nameless contact; it is unfindable afterwards.
            name = (node.get('email') or '').strip() or _('Magento customer')
        values = {
            'name': name,
            'email': (node.get('email') or '').strip() or False,
            'customer_rank': 1,
            'company_id': self.company_id.id,
        }
        addresses = node.get('addresses') or []
        if addresses:
            values.update(self._magento_address_values(addresses[0]))
        values.update(self._apply_maps_in('customer', node, 'res.partner'))
        return values

    def _magento_address_values(self, address):
        """Magento address fields as Odoo ones, resolving country by ISO code."""
        if not address:
            return {}
        street = address.get('street') or []
        values = {
            'street': (street[0] if len(street) > 0 else '') or False,
            'street2': (street[1] if len(street) > 1 else '') or False,
            'city': (address.get('city') or '').strip() or False,
            'zip': (address.get('postcode') or '').strip() or False,
            'phone': (address.get('telephone') or '').strip() or False,
        }
        code = (address.get('country_id') or '').strip().upper()
        if code:
            country = self.env['res.country'].search([('code', '=', code)], limit=1)
            if country:
                values['country_id'] = country.id
                region = (address.get('region') or {})
                region_code = (region.get('region_code') or '').strip().upper() \
                    if isinstance(region, dict) else ''
                if region_code:
                    state = self.env['res.country.state'].search([
                        ('country_id', '=', country.id),
                        ('code', '=', region_code)], limit=1)
                    if state:
                        values['state_id'] = state.id
            else:
                _logger.warning(
                    "miko_magento: country code %s is not in Odoo; the address "
                    "was imported without it", code)
        return values

    # --------------------------------------------------------------- orders
    def action_import_orders(self):
        for channel in self:
            channel._import_magento_orders()
        return True

    def _import_magento_orders(self):
        self.ensure_one()
        client = self._magento_client()
        Job = self.env['miko.ecommerce.job']
        Mapping = self.env['miko.ecommerce.mapping']
        filters = []
        if self.import_from_date:
            filters.append(('created_at',
                            fields.Datetime.to_string(self.import_from_date), 'gteq'))
        count = 0
        for node in client.search('orders', filters):
            job = Job.enqueue(self, 'import_order', node.get('entity_id'), node,
                              external_ref=node.get('increment_id'))
            # Checked before anything is written. This one line is what makes a
            # re-run, a crashed sync or two overlapping schedules all harmless.
            if Mapping.already_imported(self, 'sale.order', node['entity_id']):
                job.mark_skipped(_("Already imported."))
                continue
            try:
                order = self._job_import_order(node, job)
                job.mark_done(order)
                count += 1
            except Exception as err:        # noqa: BLE001 - kept, not lost
                _logger.exception("miko_magento: order %s failed",
                                  node.get('increment_id'))
                job.mark_failed(err, blocked=job._is_blocking(err),
                                guidance=job._guidance_for(err))
        self._touch_sync()
        return count

    def _job_import_order(self, payload, job=None):
        self.ensure_one()
        existing = self.env['miko.ecommerce.mapping'].find_odoo_record(
            self, 'sale.order', payload.get('entity_id'))
        if existing:
            return existing
        return self._import_one_magento_order(payload)

    def _import_one_magento_order(self, node):
        self.ensure_one()
        Mapping = self.env['miko.ecommerce.mapping']
        partner = self._magento_order_partner(node)
        values = {
            'partner_id': partner.id,
            'company_id': self.company_id.id,
            'origin': node.get('increment_id') or False,
            'client_order_ref': node.get('increment_id') or False,
            'magento_increment_id': node.get('increment_id') or False,
            'magento_total': money(node.get('grand_total')),
            'date_order': self._magento_datetime(node.get('created_at')),
            'order_line': self._magento_order_lines(node),
        }
        values.update(self._apply_maps_in('order', node, 'sale.order'))
        for field, value in (('team_id', self.team_id), ('warehouse_id', self.warehouse_id),
                             ('pricelist_id', self.pricelist_id)):
            if value:
                values[field] = value.id

        order = self.env['sale.order'].create(values)
        Mapping.link(self, order, node['entity_id'], node.get('increment_id'))
        self._verify_magento_total(order, node)

        if self.auto_confirm_orders and order.magento_total_matches:
            order.action_confirm()
            if self.auto_create_invoice:
                invoice = order._create_invoices()
                if invoice and self.journal_id:
                    invoice.journal_id = self.journal_id
        return order

    def _verify_magento_total(self, order, node):
        """Refuse to pretend the numbers agree when they do not."""
        expected, actual = money(node.get('grand_total')), order.amount_total
        rounding = order.currency_id.rounding or 0.01
        if abs(expected - actual) <= max(rounding, 0.01):
            return True
        order.magento_total_matches = False
        order.message_post(body=_(
            "<p><b>This order was not imported at the price the customer paid.</b></p>"
            "<p>Magento charged %(e)s. This order adds up to %(a)s, a difference "
            "of %(d)s.</p><p>Usually a discount, a shipping fee or a tax with no "
            "Odoo equivalent configured yet. It has been left as a quotation "
            "rather than confirmed, because invoicing it would bill the wrong "
            "amount.</p>") % {'e': '%.2f' % expected, 'a': '%.2f' % actual,
                              'd': '%.2f' % (expected - actual)})
        _logger.warning("miko_magento: total mismatch on %s: magento %.2f, odoo %.2f",
                        node.get('increment_id'), expected, actual)
        return False

    @staticmethod
    def _magento_datetime(value):
        """Magento returns UTC as 'YYYY-MM-DD HH:MM:SS'."""
        if not value:
            return fields.Datetime.now()
        text = str(value).replace('T', ' ').split('.')[0].replace('Z', '').strip()
        return fields.Datetime.to_datetime(text) or fields.Datetime.now()

    def _magento_order_partner(self, node):
        """Who to bill. Never guessed, never left blank."""
        customer_id = node.get('customer_id')
        if customer_id:
            partner = self.env['miko.ecommerce.mapping'].find_odoo_record(
                self, 'res.partner', customer_id)
            if partner:
                return partner
            fetched = self._magento_client().call('GET', 'customers/%s' % customer_id)
            return self._upsert_magento_customer(fetched)
        email = (node.get('customer_email') or '').strip()
        if email:
            # Guest checkout. Magento sends is_virtual/customer_is_guest and the
            # billing address, which is enough to make a real contact.
            billing = node.get('billing_address') or {}
            return self._upsert_magento_customer({
                'id': 'guest-%s' % node['entity_id'], 'email': email,
                'firstname': node.get('customer_firstname') or billing.get('firstname'),
                'lastname': node.get('customer_lastname') or billing.get('lastname'),
                'addresses': [billing] if billing else []})
        if self.default_customer_id:
            return self.default_customer_id
        raise UserError(_(
            "Magento order %s has no customer and no email address, and this "
            "store has no fallback customer set. Set one on the store record so "
            "guest orders have somewhere to go.") % (node.get('increment_id') or ''))

    def _sol_tax_field(self):
        """sale.order.line.tax_id became tax_ids in Odoo 19."""
        return 'tax_ids' if 'tax_ids' in self.env['sale.order.line']._fields else 'tax_id'

    def _magento_order_lines(self, node):
        commands = []
        for item in node.get('items') or []:
            if item.get('product_type') not in SELLABLE:
                # Configurable parents appear alongside their simple child and
                # carry the same money; importing both doubles the order.
                continue
            commands.append((0, 0, self._magento_product_line(item)))
        shipping = money(node.get('shipping_amount'))
        if shipping:
            commands.append((0, 0, {
                'product_id': self._magento_delivery_product().id,
                'name': node.get('shipping_description') or _('Shipping'),
                'product_uom_qty': 1.0, 'price_unit': shipping,
                self._sol_tax_field(): [(6, 0, [])]}))
        return commands

    def _magento_product_line(self, item):
        product = self._resolve_magento_product(item)
        qty = float(item.get('qty_ordered') or 0.0) or 1.0
        unit = money(item.get('price'))
        discount_amount = money(item.get('discount_amount'))
        gross = unit * qty
        discount = 0.0
        if gross and discount_amount:
            # Kept as a discount rather than folded into the price, so the order
            # still shows what the product normally sells for.
            discount = round(discount_amount / gross * 100.0, 4)
        return {
            'product_id': product.id,
            'name': item.get('name') or product.display_name,
            'product_uom_qty': qty,
            'price_unit': unit,
            'discount': discount,
            self._sol_tax_field(): [(6, 0, self._resolve_magento_taxes(item).ids)],
        }

    def _resolve_magento_taxes(self, item):
        """Magento gives a rate but no label, so the label is generated."""
        percent = money(item.get('tax_percent'))
        if not percent and not money(item.get('tax_amount')):
            return self.env['account.tax'].browse()
        rate = percent / 100.0
        title = _('Tax %s%%') % ('%g' % percent)
        found = self.env['miko.ecommerce.tax'].resolve(self, {'title': title,
                                                              'rate': rate})
        if found:
            return found
        if self.unmapped_tax_policy == 'block':
            raise UserError(_(
                "Magento applied a %(rate)s%% tax and there is no Odoo tax mapped "
                "to it yet.\n\nMap it on the store's Taxes tab, then retry this "
                "order. Importing without it would produce an invoice short by "
                "the tax amount.") % {'rate': '%g' % percent})
        return self.env['account.tax'].browse()

    def _resolve_magento_product(self, item):
        """The Odoo product for a line, in order of how sure we can be."""
        Mapping = self.env['miko.ecommerce.mapping']
        Product = self.env['product.product']
        if item.get('product_id'):
            product = Mapping.find_odoo_record(self, 'product.product',
                                               item['product_id'])
            if product:
                return product
        sku = (item.get('sku') or '').strip()
        if sku:
            product = Product.search([('default_code', '=', sku)], limit=1)
            if product:
                if item.get('product_id'):
                    Mapping.link(self, product, item['product_id'], sku)
                return product
        if not self.create_missing_products:
            raise UserError(_(
                "Order line '%(title)s'%(sku)s does not match any Odoo product, "
                "and this store is set not to create products.\n\nImport the "
                "catalogue first, or set the SKU in Odoo to match Magento.") % {
                    'title': item.get('name') or '?',
                    'sku': ' (SKU %s)' % sku if sku else ''})
        product = Product.create({
            'name': item.get('name') or _('Magento product'),
            'default_code': sku or False, 'magento_sku': sku or False,
            'type': 'consu', 'list_price': money(item.get('price')), 'sale_ok': True})
        if item.get('product_id'):
            Mapping.link(self, product, item['product_id'], sku)
        _logger.info("miko_magento: created product '%s' from an order line",
                     product.display_name)
        return product

    def _magento_delivery_product(self):
        self.ensure_one()
        reference = 'MIKO-MAG-SHIP-%s' % self.id
        product = self.env['product.product'].with_context(active_test=False).search(
            [('default_code', '=', reference)], limit=1)
        if product:
            return product
        return self.env['product.product'].create({
            'name': _('Shipping (%s)') % self.name, 'default_code': reference,
            'type': 'service', 'invoice_policy': 'order', 'list_price': 0.0,
            'sale_ok': True, 'purchase_ok': False})

    # ---------------------------------------------------------------- stock
    def action_export_stock(self):
        for channel in self:
            channel._export_magento_stock()
        return True

    def _quantity_for(self, product):
        self.ensure_one()
        if self.warehouse_id:
            product = product.with_context(warehouse_id=self.warehouse_id.id)
        value = product.free_qty if self.stock_source == 'free' else product.qty_available
        return max(0, int(value or 0))

    def _export_magento_stock(self):
        self.ensure_one()
        if not self.export_stock:
            raise UserError(_(
                "Publishing stock is switched off for %s. Turn on 'Publish stock "
                "to Magento' on the store first.") % self.name)
        Job = self.env['miko.ecommerce.job']
        rows = self.env['miko.ecommerce.mapping'].search([
            ('channel_id', '=', self.id), ('model_name', '=', 'product.product')])
        sent, unknown = 0, 0
        for row in rows:
            product = self.env['product.product'].browse(row.odoo_id).exists()
            if not product or product.type != 'consu':
                continue
            if not product.magento_sku or not product.magento_stock_item_id:
                # Magento writes stock against the SKU and its stock item id.
                # Without both there is nothing to address, and guessing would
                # write to the wrong row.
                unknown += 1
                continue
            job = Job.enqueue(self, 'export_stock', row.external_id,
                              {'product_id': product.id},
                              external_ref=product.magento_sku, direction='out')
            try:
                self._job_export_stock(job.get_payload(), job)
            except Exception as err:        # noqa: BLE001 - kept, not lost
                job.mark_failed(err, blocked=job._is_blocking(err),
                                guidance=job._guidance_for(err))
            else:
                job.mark_done(product)
                sent += 1
        if unknown:
            _logger.warning(
                "miko_magento: %s product(s) have no Magento SKU or stock item "
                "id, so no quantity was sent for them", unknown)
        self._touch_sync()
        return sent

    def _job_export_stock(self, payload, job=None):
        self.ensure_one()
        product = self.env['product.product'].browse(
            (payload or {}).get('product_id') or 0).exists()
        if not product:
            raise UserError(_("The Odoo product no longer exists."))
        qty = self._quantity_for(product)
        client = self._magento_client()
        client.call('PUT', 'products/%s/stockItems/%s' % (
            client.quote(product.magento_sku), product.magento_stock_item_id),
            payload={'stockItem': {'qty': qty, 'is_in_stock': qty > 0}})
        return product

    # ------------------------------------------------------------ shipments
    def create_magento_shipment(self, order, tracking=None, carrier=None):
        """Create the Magento shipment. Returns its id, or False."""
        self.ensure_one()
        row = self.env['miko.ecommerce.mapping'].search([
            ('channel_id', '=', self.id), ('model_name', '=', 'sale.order'),
            ('odoo_id', '=', order.id)], limit=1)
        if not row:
            return False
        body = {}
        if tracking:
            body['tracks'] = [{
                'track_number': tracking,
                'title': carrier or _('Shipment'),
                'carrier_code': (self.magento_carrier_code or 'custom').strip(),
            }]
        job = self.env['miko.ecommerce.job'].enqueue(
            self, 'export_shipment', row.external_id, body,
            external_ref=order.name, direction='out')
        return self._job_export_shipment(body, job)

    def _job_export_shipment(self, payload, job=None):
        """Re-runnable, so Retry means something."""
        self.ensure_one()
        external = job.external_id if job else None
        if not external:
            raise UserError(_("This job has lost the Magento order id."))
        # An empty body ships every remaining item, which is what a validated
        # Odoo delivery of the whole order means.
        result = self._magento_client().call(
            'POST', 'order/%s/ship' % external, payload=payload or {})
        if job:
            job.mark_done()
        return result
