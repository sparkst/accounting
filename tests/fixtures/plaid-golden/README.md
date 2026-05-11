# Plaid golden output (M0j capture)

**Captured:** 2026-05-11 with `PLAID_ENV=sandbox`.

## Schema-only comparison contract

Step 7g of the wealth-Cloudflare migration runbook compares these goldens
against the Workers-on-Cloudflare implementation. Plaid endpoint comparison is
**schema-only**, NOT value-level — sandbox and production return different
values for the same call.

The diff asserts:

1. Response shape (JSON structure: keys, types, nesting depth).
2. Immutable fields, if present in the response, are **byte-identical** for
   the same Plaid Item across captures:
   - `item_id` (Plaid-assigned, stable for the lifetime of the connection)
   - `institution_id` (Plaid-assigned, stable per institution)
   - `status` (string enum from `PLAID_ITEM_STATUSES`)

Volatile fields (timestamps, balances, request_id) are NOT compared.

## Captured fixtures

| Endpoint | File | Notes |
|---|---|---|
| GET /api/plaid/items | `items.json` | Empty `[]` at capture time — no items linked. Step 7g assertion is therefore "returns valid JSON array shape" only. |
| GET /api/plaid/reconciliation/summary | `reconciliation_summary.json` | Empty `[]` at capture time — depends on linked items. |

If new Plaid items are linked between M0j and cutover, re-run M0j and commit
the refreshed goldens; the step 7g schema-only assertion remains stable.
