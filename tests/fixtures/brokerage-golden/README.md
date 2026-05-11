# Brokerage golden output (M0j capture)

**Captured:** 2026-05-11 against `data/accounting.db` via the local FastAPI
launchd service (Phase 1 code, commits `36ea4b7` + `0917c73` + `04690ed`).
Parameterized routes use `account_id=011e987e-df2d-424f-9f47-8d62900589de`
and `symbol=VMFXX`.

## Comparison contract for step 7g

Brokerage endpoint comparison is **JSON-shape + value-level**, NOT
schema-only — the same SQLite source data must produce byte-identical
JSON from the Workers/D1 implementation.

The diff asserts:

1. Response shape (keys, types, ordering of array elements where ordering
   is deterministic per spec).
2. Decimal column values are byte-identical canonical strings per
   REQ-WC-004 (e.g. `"1234.56"`, not `"1234.560000"` or `1234.56` as a JS
   number).

## Captured fixtures (11 GET endpoints + 2 mutations not captured)

| Spec endpoint | File | Bytes | Notes |
|---|---|---:|---|
| GET /api/brokerage/networth | `networth.json` | 401 | |
| GET /api/brokerage/networth-history | `networth-history.json` | 17,174 | |
| GET /api/brokerage/networth-history-benchmark?benchmark=SPY | `networth-history-benchmark_benchmark_SPY.json` | 19,217 | Allowlist: `{SPY, VTI, QQQ, BND}` per REQ-WC-010. |
| GET /api/brokerage/accounts | `accounts.json` | 7,066 | |
| GET /api/brokerage/accounts/{id}/detail | `accounts_011e987e-df2d-424f-9f47-8d62900589de_detail.json` | 3,461 | |
| GET /api/brokerage/holdings/{symbol}/history | `holdings_VMFXX_history.json` | 357 | |
| GET /api/brokerage/missing-accounts | `missing-accounts.json` | 2 | Empty `[]`. |
| GET /api/brokerage/realized-gl | `realized-gl.json` | 335 | |
| GET /api/brokerage/top-holdings | `top-holdings.json` | 1,778 | |
| GET /api/brokerage/recent-transactions | `recent-transactions.json` | 6,544 | |
| GET /api/brokerage/data-integrity | `data-integrity.json` | 237 | |
| PATCH /api/brokerage/accounts/{id} | — | — | Mutation; no golden capture. Test with synthetic payload. |
| PUT /api/brokerage/accounts/{id}/tags | — | — | Mutation; no golden capture. Test with synthetic payload. |

Total: 11 GET fixtures, 13 routes from REQ-WC-010 (2 mutations excluded).

## Staleness note (step 7g)

M0j is captured once at M0 time. Step 7g comparison may see legitimate
value diffs in brokerage endpoints because new brokerage rows may be
ingested between M0 and step 7d. Per the M0j runbook note, those diffs are
NOT comparison failures — the comparison is robust against post-M0j data
additions, not against schema/calculation drift.
