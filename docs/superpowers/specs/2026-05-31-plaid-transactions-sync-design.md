# Plaid Transactions Sync (Plaid Phase 2) — Design Spec

- **Date:** 2026-05-31
- **Status:** Draft — pending user review
- **Feature group:** `REQ-PT-*` (Plaid Transactions)
- **Builds on:** Plaid Phase 1 (`REQ-025..029`, commit `36ea4b7`) — item lifecycle, balance sync, stale-item alerting, reconciliation, AuditEvent entity-mode.
- **Related fix (already applied):** link-token `INVALID_PRODUCT` 500 — `balance` removed from `required_if_supported_products` in `src/api/routes/plaid.py` (regression test `test_link_token_request_omits_invalid_balance_product`).

---

## 1. Background & Motivation

The cash-basis register (and therefore B&O / Schedule C tax numbers) is fed by per-source
adapters: `bank_csv`, `gmail_n8n`, `stripe_adapter`, `shopify`, etc. Plaid Phase 1 added
**balance** sync only — `src/adapters/plaid_balance.py` writes `PlaidAccountBalanceSnapshot`
rows and a reconciliation summary; it does **not** create register `Transaction` rows.
`src/models/plaid.py` explicitly reserves `PlaidItem.cursor` for "Phase 2+ `/transactions/sync`".

Consequence observed 2026-05-31: the `/tax` page showed Sparkry 2026 gross income of only
$1,007 (a net loss), because recent Chase activity had not been imported. Manually exporting
and importing bank CSVs is the current path and is easy to forget.

**Goal:** auto-ingest Chase (and any Plaid-linked depository account) transactions into the
register via Plaid's incremental `/transactions/sync`, so the register stays current with no
manual step. Plaid becomes the **sole source of truth** for any account linked through it.

## 2. Scope

### In scope
- A transactions-sync adapter (`src/adapters/plaid_transactions.py`) using cursor-based
  `/transactions/sync`.
- Full-history backfill on first sync, with **CSV supersede** so already-imported bank-CSV
  rows in the covered window are retired (not deleted).
- **Sole-source enforcement:** `bank_csv` skips accounts already linked to Plaid.
- Pending-inclusive ingestion with **pending → posted reconcile** (no duplicates).
- Classification through the existing 3-tier engine; sign-convention mapping at the boundary.
- Daily launchd job + manual "Sync now" API/endpoint.
- TDD test suite, REQ-tagged.

### Out of scope (separate prerequisites — see §9)
- Flipping local Plaid to `PLAID_ENV=production`.
- Chase OAuth redirect-URI registration / tunnel.
- Any change to the Cloudflare wealth app at `internal.sparkry.ai`.

The adapter is environment-agnostic: it runs against Plaid **sandbox** for tests and
**production** for real data. Nothing in this spec requires production to be live.

## 3. Requirements (REQ-PT-*)

| REQ | Requirement |
|-----|-------------|
| **REQ-PT-001** | Sync engine calls `/transactions/sync` in a loop until `has_more=false`, processing `added`, `modified`, and `removed` for each active, non-placeholder `PlaidItem`. |
| **REQ-PT-002** | `added` upserts a register `Transaction` with `source="plaid"`, `source_id=<plaid transaction_id>`, `source_hash=compute_source_hash("plaid", transaction_id)`. Re-processing the same id is idempotent (no duplicate row). |
| **REQ-PT-003** | `modified` updates the existing row's `amount`, `date`, `description`, pending flag, and `raw_data` in place. |
| **REQ-PT-004** | `removed` marks the existing row `status="rejected"` with `review_reason="plaid_removed"`. The row is never deleted (audit rule). |
| **REQ-PT-005** | Pending → posted reconcile: when a posted txn carries `pending_transaction_id` matching an existing register row, update that row in place (rewrite `source_id`/`source_hash` to the posted id, refresh amount/date/raw_data) instead of inserting. The pending id arriving in `removed` is then a no-op. |
| **REQ-PT-006** | `PlaidItem.cursor` is persisted **only** after a full successful page-loop + DB commit. A crash mid-sync re-fetches from the last good cursor; idempotency (REQ-PT-002) prevents duplicates. |
| **REQ-PT-007** | Per-row savepoint (`session.begin_nested()`): one failing transaction is logged to `result.errors` and skipped; the batch continues. |
| **REQ-PT-008** | Sign mapping: `db_amount = Decimal(str(-plaid_amount))`. Plaid `+` (outflow) → negative (expense); Plaid `−` (inflow) → positive (income). Quantized before hashing. |
| **REQ-PT-009** | Each ingested row runs through the 3-tier classifier; `confidence < 0.7` → `status="needs_review"`. `description` = Plaid `merchant_name` else `name`. Full Plaid txn JSON stored in `raw_data`. |
| **REQ-PT-010** | Entity is inherited from the mapped `Account.entity` (authoritative — overrides the classifier's entity guess). `payment_method` is stamped from `Account.payment_method` (the account's label, e.g. `"Chase ****1234"`). A txn for an unmapped Plaid account is ingested with `entity=NULL`, `payment_method=NULL`, `status="needs_review"`. |
| **REQ-PT-011** | First-sync CSV supersede, keyed on `payment_method` label (Transactions have no account FK): after backfill, existing register rows where `source != "plaid"` AND `payment_method == <mapped account's label>` AND `date` is within Plaid's covered range are marked `status="rejected"`, `review_reason="superseded_by_plaid"`, audit-logged. Never deleted. A NULL/blank account label disables supersede for that account (logged). |
| **REQ-PT-012** | Sole-source enforcement: `bank_csv` skips rows whose config `payment_method` matches a "Plaid-owned" label — i.e. an `Account` row that has both a non-null `plaid_item_id` and that `payment_method`. Skipped rows are counted/reported in `AdapterResult`, not silently dropped. |
| **REQ-PT-017** | `Account` gains a nullable `payment_method` label column (Alembic migration). `POST /map-accounts` accepts an optional `payment_method` per mapping; when creating/linking a depository account it should be set to the exact label the CSV imports used (so supersede matches history). The label is the join key for REQ-PT-010/011/012. |
| **REQ-PT-013** | Human-edit preservation: if a row is `status="confirmed"` or has human edits, `modified`/post-reconcile refresh amount/date/raw_data but preserve human-set `entity`, `direction`, `tax_category`, `tax_subcategory`. |
| **REQ-PT-014** | Daily launchd job `com.sparkry.plaid-transactions-sync.plist` runs `scripts/plaid_transactions_sync.py` (DRY-RUN default; `--apply` to commit). |
| **REQ-PT-015** | Manual `POST /api/plaid/items/{id}/sync-transactions`, rate-limited 1/min/item (reuse balance sync-now guard); auth-protected like all Plaid routes. |
| **REQ-PT-016** | `ITEM_LOGIN_REQUIRED` / stale-item conditions reuse Phase 1 alerting + relink; a failed sync sets `last_sync_status`/`last_error` and does not advance the cursor. |

## 4. Architecture & Components

**Data flow:**
```
Plaid /transactions/sync  →  plaid_transactions.py (sync engine)
  → sign map + Decimal boundary
  → 3-tier classifier
  → register Transaction (source="plaid")   ┐
  → CSV supersede (first sync)               ├─ SQLite register → /tax B&O, dashboard
bank_csv.py: skip if Account.plaid_item_id ──┘
```

| File | Change |
|------|--------|
| `src/adapters/plaid_transactions.py` | **New.** `sync_all_active(session, apply=False) -> SyncResult` + `_sync_one_item(...)`. Mirrors `plaid_balance.py` structure and DRY-RUN convention. |
| `src/adapters/test_plaid_transactions.py` | **New.** TDD suite (§8). |
| `scripts/plaid_transactions_sync.py` | **New.** launchd CLI wrapper; mirrors `scripts/plaid_balance_sync.py`. |
| `com.sparkry.plaid-transactions-sync.plist` | **New.** Daily schedule; `doppler run -- python -m scripts.plaid_transactions_sync --apply`. Dedicated job (isolation from balance sync). |
| `src/api/routes/plaid.py` | Add `POST /items/{id}/sync-transactions` (manual sync-now). |
| `src/adapters/bank_csv.py` | Add the sole-source skip keyed on `payment_method` (REQ-PT-012). |
| `src/models/enums.py` | Add `Source.PLAID = "plaid"`. Extend `Broker` + `AccountType` to admit Chase / depository (`checking`, `savings`) — follows the `p4ext1enum0xt` precedent. |
| Alembic migration | (a) `Source` CHECK is not enum-bound on `transactions` (free `String`), so no constraint change there — just the new enum member. (b) Add nullable `Account.payment_method` column (REQ-PT-017). (c) Extend `Broker`/`AccountType` CHECK constraints — values must match enum **values**, not member names (see `alembic-migration` skill / `migration-reviewer`). |
| `src/api/routes/plaid.py` (`map-accounts`) | Accept optional `payment_method` per mapping; persist to `Account.payment_method` (REQ-PT-017). |

No new tables. `PlaidItem.cursor` already exists; `Account` gains one nullable column.

## 5. Sync Algorithm (per item)

```
cursor = item.cursor            # None on first sync → Plaid returns full history
added, modified, removed = [], [], []
loop:
    resp = client.transactions_sync(access_token, cursor=cursor)
    added += resp.added; modified += resp.modified; removed += resp.removed
    cursor = resp.next_cursor
    if not resp.has_more: break

with savepoint per row:
    for t in added:   upsert_added(t)        # REQ-PT-002, pending reconcile REQ-PT-005
    for t in modified: apply_modified(t)      # REQ-PT-003, preserve human edits REQ-PT-013
    for r in removed:  mark_removed(r)        # REQ-PT-004 (no-op if already reconciled)

if first_sync: supersede_csv_rows(account, covered_range)   # REQ-PT-011
item.cursor = cursor; item.last_sync_at = now; commit       # REQ-PT-006
```

**Idempotency / crash safety:** the cursor advances in the DB only after the commit. A
re-run from the previous cursor re-delivers the same `added` items, but `source_hash`
uniqueness makes upserts no-ops.

## 6. Sign Convention & Classification

- **Boundary rule:** `Decimal(str(value))`, never `Decimal(float)`. `db_amount = -plaid_amount`.
  Quantize numeric components before computing `source_hash` (project hash-quantization rule).
- **Classification:** reuse the existing 3-tier engine exactly as other adapters do; the
  learning loop (VendorRule creation on human confirm/edit) applies unchanged.
- **`raw_data`:** the complete Plaid transaction object, preserved verbatim (audit rule).

## 7. CSV Supersede & Sole-Source Enforcement

**The join key is `payment_method`, not an Account FK.** Register `Transaction` rows have no
`account_id`; the only per-account discriminator on existing CSV rows is the free-text
`payment_method` label (e.g. `"Chase ****1234"`). So both behaviors below match on that label.

- **Arming:** mapping a Plaid account to an `Account` (`POST /map-accounts`) sets
  `Account.plaid_item_id` / `plaid_account_id` **and** `Account.payment_method` (the label).
  Setting the label is what arms supersede + skip. The label MUST equal what the CSV imports
  used for that account, or supersede won't find the history (data-hygiene requirement,
  surfaced to the operator at mapping time).
- **Backfill supersede (REQ-PT-011):** runs once, when `item.cursor` was `None` at the start
  of the sync. Marks `status="rejected"` on rows where `source != "plaid"` AND
  `payment_method == <label>` AND `date ∈ [covered_min, covered_max]`. Other labels, other
  sources' rows with a different label, and out-of-range rows are untouched.
- **Ongoing skip (REQ-PT-012):** before inserting a CSV row, `bank_csv` checks whether its
  config `payment_method` is "Plaid-owned" (an `Account` exists with that `payment_method` and
  a non-null `plaid_item_id`). If so the row is skipped and counted (logged in `AdapterResult`,
  never silently dropped).
- **Disconnect:** disconnecting a Plaid item leaves superseded CSV rows `rejected` and the
  account label intact (so re-link resumes cleanly). Manual re-activation if ever needed.
  Documented, not automated.

## 8. Testing (TDD, REQ-tagged)

Mocked Plaid client (sandbox-style fixtures), co-located `test_plaid_transactions.py`:

- REQ-PT-002 added → one row; re-run → still one row (idempotent).
- REQ-PT-003 modified → in-place update.
- REQ-PT-004 removed → `status="rejected"`, not deleted.
- REQ-PT-005 pending then posted (`pending_transaction_id`) → single row, no dup; pending id in `removed` is a no-op.
- REQ-PT-006 cursor persisted only after commit; simulated mid-sync crash re-syncs without dups.
- REQ-PT-007 one malformed txn isolated; batch completes.
- REQ-PT-008 outflow → negative; inflow → positive; `Decimal(str())` precision.
- REQ-PT-009 low-confidence → `needs_review`; description merchant_name-first; raw_data preserved.
- REQ-PT-010 mapped account → entity inherited; unmapped → `entity=NULL` + `needs_review`.
- REQ-PT-011 first sync marks overlapping CSV rows `superseded_by_plaid`; out-of-range/other-account rows untouched.
- REQ-PT-012 `bank_csv` skips Plaid-linked account; reported as skipped.
- REQ-PT-013 confirmed row keeps human classification through a `modified`.
- REQ-PT-015 sync-now rate-limit (1/min/item); auth required.

Quality gates: `pytest && ruff check src/ && mypy src/`.

## 9. Prerequisites (dependencies — not built here)

These gate **go-live with real Chase data**, not the adapter code or its tests:

1. **Production Plaid:** local `PLAID_ENV=production`, valid `PLAID_PRODUCTION_SECRET`,
   `transactions` product approved in the Plaid dashboard.
2. **Chase OAuth:** Chase is OAuth-only; requires a registered https redirect URI reachable
   by the local box (the `/admin/connections` "Cloudflare tunnel" note). Production Plaid has
   historically run on `internal.sparkry.ai`; the open ops decision is whether local linking
   gets its own redirect URI or linking happens elsewhere with the access token shared into
   the local vault. **Resolve before go-live; tracked as an ops task, not a code task.**

## 10. Open Questions / Risks

- **Account model fit:** `Account` lives in `src/models/brokerage.py` and is brokerage-oriented
  (`broker`, `account_type`, `tax_sheltered`). Using it for Chase checking needs enum
  extension (§4). Acceptable; it is already the canonical Plaid-mapping target.
- **Transfers:** internal transfers between linked accounts could appear as two transactions
  (out of A, into B). Classification should treat them as `direction="transfer"` (non-P&L).
  Existing transfer handling + reconciliation rules apply; called out so tests cover it.
- **Volume:** full-history backfill on first sync may be large; per-row savepoints keep it safe
  but the first run is slower. Acceptable for a one-time backfill.
