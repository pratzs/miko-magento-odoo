# Magento 2 Odoo Connector: Orders, Stock, Shipments (Miko)

Two-way sync between a Magento 2 store and Odoo, on a schedule. Built on the
Magento 2 REST API with an integration access token.

Requires the free **E-Commerce Connector Engine (Miko)** (`miko_ecommerce_core`),
which Odoo installs automatically.

| | |
|---|---|
| Module | `miko_magento` |
| Series | 16.0, 17.0, 18.0, 19.0 |
| Licence | OPL-1 |
| Price | USD 429 |
| Tests | 55, all four series |

## Written around how Magento actually works

- **A configurable product is a shell.** Its simple children carry the SKU, price
  and stock, so those are what reach Odoo. On orders, the configurable parent is
  skipped so the order is not doubled.
- **SKU is the business key.** Stock is written to `/products/{sku}/stockItems/`,
  and SKUs contain slashes and spaces, so every one is URL encoded.
- **Shipments are real objects.** A validated Odoo delivery creates a Magento
  shipment with its tracking number, and the id is kept so it can never ship twice.
- **Messages carry placeholders.** `%1`-style parameters are substituted, so an
  error names the SKU instead of leaving a gap.

## Not built yet

Product and customer **export** (Odoo to Magento). The direction setting offers
only Magento to Odoo rather than a choice that silently does nothing.

## Testing

```bash
python3 _dev/build_versions.py && ../_odoo-portfolio/certify.sh miko-magento-odoo miko_magento 55 miko-ecommerce-core-odoo
```

## Support

support@tripsterdevelopers.com
