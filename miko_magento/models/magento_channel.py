# -*- coding: utf-8 -*-
"""Magento credentials, direction control and the scheduled sync.

Same conservative defaults as the other connectors, for the same reason: nothing
that writes to a live shop happens until somebody switches it on.
"""
import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import config

from .magento_client import MagentoClient, MagentoError

_logger = logging.getLogger(__name__)

DIRECTIONS = [
    ('in', 'Store to Odoo'),
    ('out', 'Odoo to store'),
    ('both', 'Both ways'),
]


class MikoEcommerceChannel(models.Model):
    _inherit = 'miko.ecommerce.channel'

    platform = fields.Selection(
        selection_add=[('magento', 'Magento 2')],
        ondelete={'magento': 'set default'})

    magento_url = fields.Char(
        string='Store address',
        help="The site root, such as https://example.com. If Magento runs in a "
             "subfolder, include it. Not the /rest path.")
    magento_token = fields.Char(
        string='Integration access token', groups='base.group_system',
        help="From Magento admin: System, Extensions, Integrations. Create the "
             "integration, grant Catalog, Sales and Customers, then Activate it "
             "and copy the Access Token.\n\nStored in this database like any "
             "other setting, so treat database access as equivalent to store "
             "access, and revoke the integration if that stops being true.")
    magento_store_code = fields.Char(
        string='Store view code',
        help="Optional. Scopes calls to one store view, such as 'default'. Leave "
             "empty on a single-storefront install.")

    magento_version_note = fields.Char(string='Store view', readonly=True)

    # Import only, for now. Magento product and customer EXPORT is not built
    # yet, so offering 'out' or 'both' here would be a setting that silently does
    # nothing - which is worse than not offering it.
    product_direction = fields.Selection(
        [('in', 'Store to Odoo')], default='in', required=True, string='Products',
        help="Magento to Odoo. Pushing the catalogue the other way is not built yet.")
    customer_direction = fields.Selection(
        [('in', 'Store to Odoo')], default='in', required=True, string='Customers',
        help="Magento to Odoo. Pushing contacts the other way is not built yet.")

    auto_sync = fields.Boolean(
        string='Sync on a schedule', default=False,
        help="Off until you turn it on. Installing a connector should never "
             "start moving data on its own.")
    sync_interval_minutes = fields.Integer(string='Every (minutes)', default=60)
    sync_orders = fields.Boolean(string='Sync orders', default=True)
    sync_products = fields.Boolean(string='Sync products', default=False)
    sync_customers = fields.Boolean(string='Sync customers', default=False)
    sync_running = fields.Boolean(
        readonly=True, copy=False,
        help="Set while a scheduled run is in progress, so a long sync is never "
             "started a second time on top of itself.")
    last_sync_message = fields.Text(readonly=True, copy=False)

    # ------------------------------------------------------------------
    @api.constrains('magento_url', 'platform')
    def _check_magento_url(self):
        for channel in self:
            if channel.platform != 'magento' or not channel.magento_url:
                continue
            url = channel.magento_url.strip()
            if not url.startswith('https://'):
                raise ValidationError(_(
                    "The store address must start with https://.\n\nThe "
                    "integration token is sent with every request. Over plain "
                    "HTTP anyone on the network can read it."))
            if '/rest' in url:
                raise ValidationError(_(
                    "Use the site root, such as https://example.com. The "
                    "connector adds /rest/V1 itself."))

    @api.onchange('magento_url')
    def _onchange_magento_url(self):
        """Repair what people actually paste, rather than refusing it."""
        if not self.magento_url:
            return
        url = self.magento_url.strip().rstrip('/')
        url = re.sub(r'/(rest|admin)(/.*)?$', '', url)
        if url.startswith('http://'):
            url = 'https://' + url[len('http://'):]
        elif not url.startswith('https://') and '.' in url:
            url = 'https://' + url
        self.magento_url = url

    # ------------------------------------------------------------------
    def _magento_client(self):
        self.ensure_one()
        if self.platform != 'magento':
            raise UserError(_("%s is not a Magento store.") % self.name)
        me = self.sudo()
        return MagentoClient(self.magento_url, me.magento_token,
                             self.magento_store_code)

    def action_test_connection(self):
        """Prove the credentials work, and say precisely what failed if not."""
        for channel in self:
            if channel.platform != 'magento':
                continue
            try:
                info = channel._magento_client().store_config()
            except MagentoError as err:
                channel.write({'connection_state': 'error',
                               'connection_message': str(err)})
                continue
            except Exception as err:          # noqa: BLE001 - shown to the user
                channel.write({'connection_state': 'error',
                               'connection_message': _("Unexpected problem: %s") % err})
                continue
            channel.write({
                'connection_state': 'ok',
                'magento_version_note': info.get('code'),
                'connection_message': _(
                    "Connected to %(name)s. Store view %(code)s, selling in "
                    "%(cur)s.") % {'name': info.get('name'),
                                   'code': info.get('code') or 'default',
                                   'cur': info.get('currency') or '?'},
            })
        return True

    def _require_direction(self, setting, wanted, what):
        """Refuse politely when this is not the direction the store chose."""
        self.ensure_one()
        value = self[setting]
        if value in (wanted, 'both'):
            return True
        raise UserError(_(
            "%(what)s on %(store)s is set to '%(current)s', so it cannot be sent "
            "the other way.\n\nChange it on the store's Directions tab if that is "
            "what you want.") % {
                'what': what, 'store': self.name,
                'current': dict(DIRECTIONS).get(value, value)})

    def _default_field_maps(self):
        """Magento's own default mappings, seeded per store."""
        if self.platform != 'magento':
            return super()._default_field_maps()
        return [
            ('product', 'in', 'name', 'name', True),
            ('product', 'out', 'name', 'name', True),
            ('customer', 'in', 'email', 'email', True),
            ('customer', 'out', 'email', 'email', True),
            ('order', 'in', 'increment_id', 'client_order_ref', True),
        ]

    # ------------------------------------------------------------------
    @api.model
    def _cron_magento_sync(self):
        """Entry point for the scheduler. Never raises."""
        for channel in self.search([('platform', '=', 'magento'),
                                    ('active', '=', True),
                                    ('auto_sync', '=', True)]):
            if channel._magento_sync_due():
                channel._run_magento_sync()
        return True

    def _magento_sync_due(self):
        self.ensure_one()
        if self.sync_running:
            _logger.info("miko_magento: %s is still syncing, skipping", self.name)
            return False
        if not self.last_sync:
            return True
        minutes = max(self.sync_interval_minutes or 0, 5)
        return (fields.Datetime.now() - self.last_sync).total_seconds() / 60.0 >= minutes

    def _checkpoint(self, rollback=False):
        """Commit progress as the run goes, except under test.

        config['test_enable'], not Registry.in_test_mode(): the latter reports
        whether the registry holds a test cursor, which is False for an ordinary
        at-install TransactionCase, so trusting it aborts the transaction.
        """
        if config['test_enable']:
            return False
        if rollback:
            self.env.cr.rollback()
        else:
            self.env.cr.commit()
        return True

    def _run_magento_sync(self):
        self.ensure_one()
        self.sync_running = True
        self._checkpoint()
        done = []
        try:
            if self.sync_products:
                done.append(_("%s products") % self._import_magento_products())
            if self.sync_customers:
                done.append(_("%s customers") % self._import_magento_customers())
            if self.sync_orders:
                done.append(_("%s orders") % self._import_magento_orders())
            message = _("Synced %s.") % (", ".join(done) or _("nothing enabled"))
        except Exception as err:        # noqa: BLE001 - recorded, never raised on
            _logger.exception("miko_magento: scheduled sync failed for %s", self.name)
            self._checkpoint(rollback=True)
            message = _("Failed: %s") % err
        finally:
            # Always clears, including after a rollback, or the channel is stuck
            # as running for ever and never syncs again.
            self.sync_running = False
            self.last_sync_message = message
            self._checkpoint()
        return message

    def action_sync_now(self):
        for channel in self:
            channel._run_magento_sync()
        return True
