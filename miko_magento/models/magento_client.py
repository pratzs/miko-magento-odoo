# -*- coding: utf-8 -*-
"""The Magento 2 REST client.

Magento is its own shape again, and the differences are not cosmetic:

* **Auth is a Bearer integration token**, created in Magento admin under System,
  Integrations. Not a key pair, not OAuth 1 in practice for server-to-server.
* **Pagination is `searchCriteria`**, not page numbers or cursors. The response
  carries `items` plus a `total_count`, so the walk knows when to stop without
  guessing. Sorted by `entity_id` ascending, because sorting by anything that
  changes during the walk lets a record move between pages and be read twice or
  skipped.
* **SKU is the business key**, not the numeric id. Several endpoints address
  products by SKU, and SKUs contain slashes and spaces often enough that they
  must be URL-encoded every time.
* **Errors are `{"message": "...", "parameters": {...}}`** with the message
  carrying `%1`-style placeholders that have to be substituted to read properly.
* **It is the merchant's own server**, so it can be slow, behind a WAF, or
  answering with HTML from nginx rather than JSON from Magento.
"""
import json
import logging
import re
import time
import urllib.parse

import requests

from odoo import _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

TIMEOUT = 60          # Magento is heavier than most; a big catalogue page is slow
MAX_ATTEMPTS = 5
RETRY_STATUS = {408, 429, 500, 502, 503, 504}
PAGE_SIZE = 50

# Integration tokens are long hex-ish strings; scrub anything that looks like one
# out of logs and error messages.
BEARER_RE = re.compile(r'(Bearer\s+)[A-Za-z0-9._\-]+', re.I)


def redact(text):
    """Never let an integration token reach a log or an error message."""
    if not text:
        return text
    return BEARER_RE.sub(lambda m: m.group(1) + '***', str(text))


class MagentoError(UserError):
    """A Magento problem stated in terms the user can act on."""


def substitute(message, parameters):
    """Magento messages carry %1, %2 placeholders. Fill them in.

    Left raw, a merchant sees "The product that was requested doesn't exist.
    Verify the product and try again." with no SKU in it, which is unhelpful
    precisely when they need help.
    """
    text = message or ''
    if isinstance(parameters, dict):
        for key, value in parameters.items():
            text = text.replace('%%%s' % key, str(value))
    elif isinstance(parameters, (list, tuple)):
        for index, value in enumerate(parameters, start=1):
            text = text.replace('%%%d' % index, str(value))
    return text


class MagentoClient(object):
    """One authenticated conversation with one Magento store."""

    def __init__(self, base_url, token, store_code=None):
        self.base_url = (base_url or '').strip().rstrip('/')
        self.token = (token or '').strip()
        self.store_code = (store_code or '').strip()
        if not self.base_url or not self.token:
            raise MagentoError(_(
                "This store needs its address and an integration access token. "
                "Both are on the Magento tab of the store record."))
        if not self.base_url.startswith('https://'):
            raise MagentoError(_(
                "The store address must start with https://. The integration "
                "token is sent with every request, and over plain HTTP anyone on "
                "the network can read it."))
        self._session = requests.Session()
        self._session.headers.update({
            'Authorization': 'Bearer %s' % self.token,
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        })

    @property
    def api(self):
        # A store code scopes the call to one storefront; without it Magento uses
        # the default, which is what a single-store merchant wants.
        if self.store_code:
            return '%s/rest/%s/V1' % (self.base_url, self.store_code)
        return '%s/rest/V1' % self.base_url

    # ------------------------------------------------------------------
    def call(self, method, path, params=None, payload=None):
        """One REST call. Returns the decoded body. Raises MagentoError."""
        url = '%s/%s' % (self.api, path.lstrip('/'))
        last = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self._session.request(
                    method, url, params=params or {},
                    data=json.dumps(payload) if payload is not None else None,
                    timeout=TIMEOUT)
            except requests.exceptions.Timeout:
                last = _("Magento did not respond within %s seconds.") % TIMEOUT
            except requests.exceptions.SSLError as err:
                raise MagentoError(_(
                    "The store's HTTPS certificate could not be verified: %s\n\n"
                    "Fix the certificate on the Magento site. Ignoring it would "
                    "mean sending the integration token to whoever answered.")
                    % redact(err))
            except requests.exceptions.RequestException as err:
                last = _("Could not reach Magento: %s") % redact(err)
            else:
                fatal = self._fatal(response)
                if fatal:
                    raise MagentoError(fatal)
                if response.status_code in RETRY_STATUS:
                    last = _("Magento returned HTTP %s.") % response.status_code
                    self._sleep(response, attempt)
                    continue
                body = self._decode(response)
                if isinstance(body, dict) and body.get('message') and \
                        response.status_code >= 400:
                    raise MagentoError(_("Magento refused the request: %s")
                                       % substitute(body['message'],
                                                    body.get('parameters')))
                return body

            time.sleep(min(2 ** attempt, 20))

        raise MagentoError(_(
            "Magento could not be reached after %(n)s attempts. Last problem: "
            "%(err)s") % {'n': MAX_ATTEMPTS, 'err': last})

    def _decode(self, response):
        try:
            return response.json()
        except ValueError:
            snippet = (response.text or '')[:200].strip().replace('\n', ' ')
            raise MagentoError(_(
                "Magento returned something that is not JSON (HTTP %(code)s).\n\n"
                "This is usually nginx, Varnish or a firewall answering instead "
                "of Magento. Check that %(api)s/store/storeConfigs opens with the "
                "token.\n\nWhat came back: %(snippet)s") % {
                    'code': response.status_code, 'api': self.api,
                    'snippet': snippet or '(empty)'})

    def _fatal(self, response):
        """Statuses that retrying cannot fix."""
        if response.status_code == 401:
            return _(
                "Magento rejected the integration token.\n\n"
                "In Magento admin go to System, Extensions, Integrations, and "
                "check the integration is still Active and has been Authorized. "
                "A token that was never activated returns exactly this.")
        if response.status_code == 403:
            return _(
                "The integration token is valid but not allowed to do that.\n\n"
                "Edit the integration's API resources in Magento admin and grant "
                "Catalog, Sales and Customers.")
        if response.status_code == 404:
            return _(
                "No Magento REST API at %s.\n\n"
                "Check the address is the site root. If the shop runs in a "
                "subfolder, include it.") % self.api
        return None

    @staticmethod
    def _sleep(response, attempt):
        retry_after = response.headers.get('Retry-After')
        try:
            time.sleep(min(float(retry_after), 30.0))
        except (TypeError, ValueError):
            time.sleep(min(2 ** attempt, 20))

    # ------------------------------------------------------------------
    @staticmethod
    def quote(value):
        """URL-encode a SKU. They contain slashes and spaces often enough."""
        return urllib.parse.quote(str(value), safe='')

    def search(self, path, filters=None, page_size=PAGE_SIZE, max_pages=2000):
        """Walk a searchCriteria collection, yielding every item.

        Sorted by entity_id ascending on purpose: Magento pages by number, so
        ordering by anything that changes mid-walk (updated_at, say) lets a record
        shift between pages and be read twice or missed entirely.
        """
        page = 1
        while page <= max_pages:
            params = {
                'searchCriteria[currentPage]': page,
                'searchCriteria[pageSize]': page_size,
                'searchCriteria[sortOrders][0][field]': 'entity_id',
                'searchCriteria[sortOrders][0][direction]': 'ASC',
            }
            for index, (field, value, condition) in enumerate(filters or []):
                group = ('searchCriteria[filter_groups][%d][filters][0]' % index)
                params['%s[field]' % group] = field
                params['%s[value]' % group] = value
                params['%s[condition_type]' % group] = condition
            body = self.call('GET', path, params=params)
            items = (body or {}).get('items')
            if items is None:
                raise MagentoError(_(
                    "Expected a searchCriteria result from %s and got something "
                    "else.") % path)
            for item in items:
                yield item
            total = (body or {}).get('total_count')
            if total is not None and page * page_size >= int(total):
                return
            if len(items) < page_size:
                return
            page += 1
        _logger.warning(
            "miko_magento: stopped paginating %s after %s pages; the sync is "
            "incomplete", path, max_pages)

    def store_config(self):
        """Identify the store. The cheapest proof the token works."""
        body = self.call('GET', 'store/storeConfigs')
        first = (body or [{}])[0] if isinstance(body, list) else {}
        return {
            'name': first.get('base_url') or self.base_url,
            'code': first.get('code'),
            'currency': first.get('default_display_currency_code'),
            'locale': first.get('locale'),
        }
