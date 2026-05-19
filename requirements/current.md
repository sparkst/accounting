# Accounting System Requirements

## REQ-001: Gmail/n8n Ingestion
- Acceptance: System reads JSON files from SGDrive/LIVE_SYSTEM/accounting/{keep,for-review,manual,deductions}/, extracts transaction data, links attachments by hex ID, and writes to register
- Non-Goals: Replacing n8n pipeline; system works downstream of it

## REQ-002: Deduction Email Ingestion
- Acceptance: System ingests deduction-related emails (charitable, mortgage, medical, property tax) from a new `deductions/` folder, classifies as personal deductions
- Non-Goals: Automatically categorizing all deduction subtypes without human review

## REQ-003: Stripe Integration
- Acceptance: System connects to Stripe API, pulls charges/payouts/invoices for both Sparkry and BlackLine, maps to correct entity, identifies Substack subscription income
- Non-Goals: Writing to Stripe

## REQ-004: Shopify Integration
- Acceptance: System connects to Shopify Admin API, pulls orders/refunds/fees/payouts for BlackLine MTB, all auto-tagged as BlackLine entity
- Non-Goals: Modifying Shopify data

## REQ-005: Brokerage CSV Import
- Acceptance: System imports CSV exports from E*Trade, Schwab, Vanguard, and Fidelity with correct column mapping. Tracks holdings, transactions, and realized lots in isolated brokerage tables (NOT the existing Transaction table). Goal: net-worth and performance tracking.
- Non-Goals: Automatic brokerage API connection (Plaid). Tax-form ingestion (1099-B/DIV/INT CSVs deferred to Phase 2). Flowing brokerage activity into existing P&L (Phase 2).

### REQ-005a: Account registry
- Acceptance: `account` table records each brokerage account with broker, account_number, account_type (taxable/joint/roth_ira/trad_ira/401k/403b/hsa/529/tod/brokeragelink/rsu), entity, tax_sheltered, parent_account_id (self-FK for plan wrappers), beneficiary. UNIQUE (broker, account_number).

### REQ-005b: Brokerage transactions
- Acceptance: `brokerage_transaction` table records each transaction with account_id, trade_date, settlement_date, broker-native action, canonical_action, symbol, cusip, quantity (always positive), amount (signed +in/-out), commission, fees. Reinvest dividend ↔ buy linked via `paired_transaction_id`. Synthesized partner rows flagged via `is_synthetic=True`.

### REQ-005c: Position snapshots
- Acceptance: `position_snapshot` records holdings per account per as_of timestamp. Dedup via `source_row_hash` (NOT UNIQUE on symbol — same symbol can appear in multiple buckets).

### REQ-005d: Realized gain/loss
- Acceptance: `realized_gain_loss` records closed lots with opened/closed dates, proceeds, cost_basis, unadjusted_cost_basis, lt_gain_loss, st_gain_loss, term, wash_sale, disallowed_loss.

### REQ-005e: Idempotent re-ingest
- Acceptance: Re-ingesting the same CSV file produces zero new rows. Dedup uses `source_row_hash` computed via length-framed `compute_source_hash()` from `src/utils/dedup.py`, including broker, account_number, source_file, row_index, trade_date, action, symbol, normalized quantity, normalized amount.

### REQ-005f: Per-broker CSV parsers
- Acceptance: Adapters handle BOM, CRLF, multi-section files, currency formatting (`$`, `,`, `$-`), `"as of"` dates, 2-digit years, and broker-specific column quirks per the file inventory in proposals/brokerage-ingest/PLAN.md.

### REQ-005g: Adapter contract
- Acceptance: Each broker adapter inherits `BaseAdapter`, returns `AdapterResult`, writes an `IngestionLog` row per run. Follows the `StripeAdapter` commit pattern (savepoint per record, batch commit, IngestionLog last).

## REQ-006: Bank CSV Import
- Acceptance: System imports bank statement CSVs with configurable column mapping per bank, cross-references against other sources
- Non-Goals: Real-time bank feeds

## REQ-007: Photo Receipt Processing
- Acceptance: System processes JPG/PNG/HEIC images via Claude Vision API, extracts vendor/date/line items/amounts/total, flags low-confidence extractions for review
- Non-Goals: Perfect OCR — human review expected for low confidence

## REQ-008: Deduplication
- Acceptance: SHA256-based dedup detects same-source duplicates (auto-skip) and cross-source duplicates (flag for review). No duplicate transactions in confirmed register.
- Non-Goals: Fuzzy matching on partial data

## REQ-009: Three-Tier Classification
- Acceptance: Vendor rules → pattern matching → LLM classification. Every transaction gets entity + tax_category + direction. Confidence score attached. Items below 0.7 confidence routed to needs_review.
- Non-Goals: 100% auto-classification from day one

## REQ-010: Learning Loop
- Acceptance: Every human interaction (first-time assignment, confirmation, correction, entity/category/vendor changes) creates or updates a VendorRule. System classification accuracy improves over time.
- Non-Goals: Unsupervised learning without human confirmation

## REQ-011: Line-Item Splitting
- Acceptance: Hotels and mixed receipts split into child transactions with correct tax categories and deductible percentages. Children must sum to parent total (flag if not).
- Non-Goals: Splitting every multi-line invoice

## REQ-012: Reimbursable Expense Tracking
- Acceptance: Cardinal Health pass-through expenses tracked separately. Linked to reimbursement when received. Both net to zero on P&L. Overdue reimbursements flagged after 30 days.
- Non-Goals: Auto-generating invoices to Cardinal Health

## REQ-013: Dashboard — Review Queue
- Acceptance: Web dashboard (localhost:5173) shows items needing review. Pre-filled dropdowns for entity/category. One-click confirm. Keyboard shortcuts. Sortable, filterable, searchable.
- Non-Goals: Mobile app

## REQ-014: Dashboard — Register View
- Acceptance: Full transaction list, sortable by all columns, inline editing, running totals, export buttons
- Non-Goals: Infinite scroll (pagination is fine)

## REQ-015: Dashboard — Health Dashboard
- Acceptance: Source status with staleness warnings, failure log with retry actions, classification stats, upcoming tax deadlines, account memory stats
- Non-Goals: Real-time monitoring

## REQ-016: Dashboard — Tax Summary
- Acceptance: Per-entity IRS line-item breakdown with amounts. Warning if unconfirmed transactions affect totals. B&O subtotals. Export buttons for FreeTaxUSA and TaxAct formats.
- Non-Goals: Filing taxes directly

## REQ-017: Dashboard — Accounts & Memory
- Acceptance: Editable vendor rules table, entity configuration, tax deadline calendar
- Non-Goals: Multi-user access control

## REQ-018: Tax Export — FreeTaxUSA
- Acceptance: CSV export matching FreeTaxUSA import format for Schedule C, K-1 data, Schedule A deductions, 1099-B transactions
- Non-Goals: Direct API integration with FreeTaxUSA

## REQ-019: Tax Export — TaxAct
- Acceptance: CSV export matching TaxAct Business 1065 import format for BlackLine MTB partnership return
- Non-Goals: Direct API integration with TaxAct

## REQ-020: B&O Tax Reports
- Acceptance: Revenue figures for WA B&O tax filing. Monthly for Sparkry, quarterly for BlackLine. Correct revenue classification codes.
- Non-Goals: Auto-filing B&O returns

## REQ-021: Error Handling
- Acceptance: Per-record error isolation (one bad record doesn't halt batch). IngestionLog for every adapter run. Retry with backoff for transient failures. Auth failures halt and surface immediately. Staleness detection per source.
- Non-Goals: Self-healing without human awareness

## REQ-022: Reconciliation
- Acceptance: Automated checks that Stripe/Shopify payouts appear in bank statements. Flag unmatched items. Monthly total sanity checks.
- Non-Goals: Real-time reconciliation

## REQ-023: GAAP Cash-Basis Compliance
- Acceptance: Revenue recorded when received, expenses when paid. Consistent classification. Full audit trail. Reimbursables properly netted.
- Non-Goals: Accrual basis or double-entry bookkeeping

## REQ-024: Dashboard UX — Sorting, Filtering, Search
- Acceptance: All list views support column-header sorting (asc/desc toggle), date picker with presets (Today, This Week, This Month, This Quarter, YTD, Last Year, Custom), type-ahead search, stackable filters. Apple design principles: minimal, zero friction, progressive disclosure.
- Non-Goals: Saved/named filter presets (v1)

## REQ-025: Plaid Item Lifecycle
- Acceptance: Connect (link → exchange → persist with encrypted token), map Plaid accounts to existing `Account` rows, disconnect (calls `/item/remove`, overwrites encrypted token with `"REVOKED"`, frees slot), re-link in update mode for `ITEM_LOGIN_REQUIRED` recovery. CSRF/state nonce required and validated on exchange. All `/api/plaid/*` endpoints require authentication.
- Non-Goals: Real-time webhooks; multi-tenancy.

## REQ-026: Plaid Balance Daily Sync
- Acceptance: For every active Plaid Item, daily cron pulls `/accounts/balance/get`, writes `plaid_account_balance_snapshot` rows for mapped accounts, writes per-Item `IngestionLog`, classifies Plaid errors as retryable (RATE_LIMIT/INTERNAL_SERVER/INSTITUTION_DOWN/PRODUCT_NOT_READY) vs terminal (ITEM_LOGIN_REQUIRED/INVALID_CREDENTIALS/INVALID_ACCESS_TOKEN), and is idempotent on double-run via UNIQUE(account_id, snapshot_date) + per-row `begin_nested()` savepoint. Unmapped Plaid accounts upsert into `ExpectedAccount` with `status='unconfirmed'`. Non-USD accounts skipped with warning.
- Non-Goals: Transactions, holdings, real-time.

## REQ-027: Plaid Stale-Item Alerting
- Acceptance: Weekly P&L email surfaces active Plaid Items with terminal-error `last_sync_status`; Health Dashboard surfaces active Items not synced in >48h.
- Non-Goals: SMS/push alerts.

## REQ-028: Plaid Balance Reconciliation
- Acceptance: `GET /api/plaid/reconciliation/summary` returns per-account delta between latest `plaid_account_balance_snapshot.current_balance` and the existing computed `position × yfinance` total. Returns `exceeds_threshold = (abs(delta_pct) > 2.0) OR (abs(delta) > 100.00)`. Credit/loan account types are negated before aggregation.
- Non-Goals: Auto-promoting Plaid as canonical source.

## REQ-029: AuditEvent Extension for Non-Transaction Entities
- Acceptance: `audit_events` table extended with nullable `entity_id` and `entity_type` columns; `transaction_id` relaxed to nullable; CHECK constraint enforces exactly-one-of (`transaction_id`, `entity_id`) is set. All Plaid lifecycle actions (connect, map, unmap, disconnect, relink) write AuditEvents with `entity_type='plaid_item'` or `'account'`.
- Non-Goals: Backfilling historical Plaid AuditEvents.

---

# Wealth → Cloudflare Migration (REQ-WC-001..019)

The full acceptance criteria, non-goals, and test seeds for each REQ-WC-* live in
the design spec: `docs/superpowers/specs/2026-05-10-wealth-cloudflare-migration.md`.
The execution runbook is `docs/superpowers/plans/2026-05-10-wealth-cloudflare-migration-runbook.md`.
The summaries below are scan-aids; the spec is authoritative.

## REQ-WC-001: Routing isolation between CRM and Wealth groups
- Summary: SvelteKit `(crm)` and `(wealth)` route groups; ESLint `no-restricted-paths` blocks cross-group imports; rendered bundles must not contain the other group's module paths.
- Full acceptance: spec §REQ-WC-001.

## REQ-WC-002: Shared auth via Cloudflare Access + Travis-only in-app guard
- Summary: every `/wealth/*` page + `/wealth/api/brokerage/*` + `/wealth/desk/api/*` endpoint requires valid JWT (JWKS-verified) AND email claim matching `WEALTH_ALLOWED_EMAILS`; non-allowlisted → 404 (not 403); `*.pages.dev` previews covered by CF Access policy; `/wealth/api/internal/*` bypasses CF Access but enforces X-Internal-Key.
- **Auth-model correction (design spec §1, applies to all REQ-WD-* auth references):** CF Access (JWT/JWKS) is **NOT deployed** as of 2026-05-13 — live inspection during M0h confirmed zero CF Access applications configured. The actual mechanism in production is **Google OAuth + an HMAC-signed session cookie** (`requireWealthAccess` in `hooks.server.ts`), still gated on `WEALTH_ALLOWED_EMAILS`. The CF Access JWT requirement in this summary is deferred to TF-002 (parent migration spec §A4). All REQ-WD acceptance criteria that say "WEALTH_ALLOWED_EMAILS auth guard" / "cookie guard" refer to this cookie model; do not expect `Cf-Access-Authenticated-User-Email` headers. `/wealth/api/internal/*` X-Internal-Key (WEALTH_INTERNAL_KEY) applies only to script/Python callers (REQ-WC-012), never to browser-facing routes.
- Full acceptance: spec §REQ-WC-002.

## REQ-WC-003: D1 schema port preserves all CHECK + UNIQUE constraints
- Summary: 13 tables migrated to D1 with every CHECK + UNIQUE constraint preserved inline in CREATE TABLE (not ALTER); smoke test violates each.
- Full acceptance: spec §REQ-WC-003.

## REQ-WC-004: Decimal precision preserved end-to-end
- Summary: monetary/quantity values stored as TEXT canonical decimal strings in D1; converted via `decimal.js` (module-local cloned class — Decimal.set forbidden); no precision loss across 8-decimal quantities.
- Full acceptance: spec §REQ-WC-004 (incl. Python↔TS KAT requirements).

## REQ-WC-005: Plaid Item lifecycle (port of REQ-025)
- Summary: ports REQ-025 to Workers backend; atomic single-UPDATE nonce consumption (TOCTOU-safe); link_token fetched on click (not page load); 6-hour cleanup cron marks expired placeholders as `'abandoned'` (never delete); postMessage targetOrigin pinned to `https://internal.sparkry.ai`; popup-mode only.
- Full acceptance: spec §REQ-WC-005.

## REQ-WC-006: Plaid Balance daily sync via Workers Cron Trigger (port of REQ-026)
- Summary: cron `"7 10 * * *"` (UTC); per-row `db.prepare(...).bind(...).run()` (no `db.batch()`); three-layer error isolation; REVOKED rows filtered before AES-GCM decrypt; sanitized error_detail; PL-T07 CPU benchmark ≤10 ms/iteration; Workers Paid REQUIRED from day one.
- Full acceptance: spec §REQ-WC-006.

## REQ-WC-007: Stale-Item alerting (port of REQ-027) — Workers-only
- Summary: cron `"0 14 * * MON"` (UTC); Resend email to hardcoded `travis@sparkry.com`; AuditEvent per stale item; replaces local weekly P&L Plaid section (stripped at cutover).
- Full acceptance: spec §REQ-WC-007.

## REQ-WC-008: Reconciliation summary (port of REQ-028)
- Summary: same delta logic as Python (>2% or >$100; credit/loan negated; `null` when no priced positions); error responses sanitized to {error_code, error_type} — full Plaid error body never returned in HTTP response.
- Full acceptance: spec §REQ-WC-008.

## REQ-WC-009: AuditEvent entity-mode in D1
- Summary: D1 `audit_events` has `entity_id`, `entity_type` NOT NULL; no `transaction_id` column; `changed_by` widened to `String(64)`; `cf_scheduled_time` populated from `controller.scheduledTime`; D1 triggers reject DELETE on append-only tables (`audit_events`, `plaid_item`, `plaid_account_balance_snapshot`, `brokerage_transaction`, `realized_gain_loss`, `position_snapshot`, `cost_basis_lot`); LM-T0 non-destructive pre-cutover migration widens local Python `changed_by` and `PLAID_ITEM_STATUSES` for rollback compatibility.
- Full acceptance: spec §REQ-WC-009.

## REQ-WC-010: Brokerage read API parity (13 routes + benchmark allowlist)
- Summary: all 13 brokerage endpoints (incl. networth-history-benchmark with hardcoded allowlist `{SPY, VTI, QQQ, BND}`) return same JSON shape as FastAPI version; contract test compares against M0j golden output.
- **BND scope note (REQ-WD-003 clarification):** BND remains in the EOD historical-price cron allowlist (REQ-WC-013) and the networth-history-benchmark endpoint for the existing FastAPI parity surface (REQ-WC-010). However, the new `/wealth` dashboard benchmark toggle UI (REQ-WD-003) exposes only `{SPY, QQQ, VTI}` — BND is intentionally excluded from the UI toggle. Direct API calls with `benchmarks=BND` on the extended networth-history endpoint return 400 `{error_code:'invalid_benchmark'}`. This is not a contradiction: the EOD allowlist scope and the UI benchmark scope are different.
- Full acceptance: spec §REQ-WC-010.

## REQ-WC-011: Brokerage UI parity at `/wealth/*` (Svelte 5 runes only)
- Summary: feature parity with current `/brokerage/*`; Svelte 5 runes enforced via lint; `/wealth/transactions` deferred to post-cutover follow-up (TF-001).
- Full acceptance: spec §REQ-WC-011.

## REQ-WC-012: Local Python importers POST to Workers
- Summary: 7 importers gain `--target cloud` mode (default `local`); POST to `/wealth/api/internal/ingest/*` with X-Internal-Key; dedup hash (`source_row_hash`) byte-identical Python↔TS (BR-T03 KAT); max 100 rows/POST (413 otherwise); raw_data >900 KB rejected with 422 (no silent truncation).
- Full acceptance: spec §REQ-WC-012.

## REQ-WC-013: Historical-price ingestion via Workers + Twelve Data (quota-bounded)
- Summary: cron `"30 7 * * *"` (UTC); one Twelve Data API call per symbol; 600 calls/day cap (safety buffer under 800); symbol regex `^[A-Z0-9.^]{1,12}$`; ON CONFLICT DO NOTHING (default) or DO UPDATE (`?overwrite=true` on backfill); 2-year initial backfill; `apikey` query param sanitized from logs; Workers Paid REQUIRED from day one (6.25 min wall-clock at 50 symbols).
- Full acceptance: spec §REQ-WC-013.

## REQ-WC-013a: Live-quote refresh for in-app price freshness (companion to REQ-WC-013)
- Summary: new D1 table `live_quote` (UPDATE/DELETE permitted; excluded from R2 backups); endpoint `GET /wealth/api/brokerage/quotes?symbols=...` (max 50 per request → 413; regex-validated; 15-min KV-equivalent cache via `live_quote.fetched_at`); market-closed (Mon-Fri 09:30-16:00 ET, DST-aware) returns latest `historical_price.close` with `is_stale: false`; market-open + budget available fetches Twelve Data `quote` and upserts; market-open + budget exhausted returns stale with `is_stale: true` (HTTP 200, NOT 429 — degraded-mode contract). Shares the same 600/day Twelve Data budget counter as REQ-WC-013. ONE IngestionLog row per HTTP request (not per symbol).
- Full acceptance: spec §REQ-WC-013a.

## REQ-WC-014: Cutover migration is reversible within the soak window
- Summary: TypeScript `migrate-from-sqlite.ts` (in sparkry-crm); Fernet→AES-GCM re-encryption with per-column scale dispatch; 9-row byte-identity spot-check × 6 tables = 54 rows; rollback exports CURRENT D1 state (incl. post-cutover cron rows); soak window = 3 consecutive cron successes AND ≥3 calendar days; LM-T01 (drop local Plaid tables) runs AFTER soak.
- Full acceptance: spec §REQ-WC-014.

## REQ-WC-015: Local brokerage routes removed after cutover
- Summary: post-cutover the local FastAPI no longer mounts `brokerage_router` or `plaid_router`; local dashboard `/brokerage/*` removed; launchd plists DELETED (not just unloaded) from `~/Library/LaunchAgents/`; `plaid-oauth-return` Cloudflare tunnel deleted.
- Full acceptance: spec §REQ-WC-015.

## REQ-WC-016: No cross-references between CRM and Wealth in nav or layout
- Summary: grep test asserts CRM bundle contains no `/wealth/*` strings and Wealth bundle contains no `/customers`/`/work-orders`/`/invoices` strings.
- Full acceptance: spec §REQ-WC-016.

## REQ-WC-017: Workers free-tier CPU budget enforced (two-level benchmark)
- Summary: hot reconciliation handler ≤ 8 ms CPU on local `wrangler dev` AND ≤ 250 ms wall-clock on staging deploy; ingest handler 100-row payload ≤ 10 ms; cron wall-clock budgets (R2 backup ≤15 min, Plaid sync ≤30 s, Twelve Data ≤8 min) all on Workers Paid; post-deploy CPU verification via Cloudflare dashboard mandatory.
- Full acceptance: spec §REQ-WC-017.

## REQ-WC-018: Daily D1 → R2 backup (NDJSON with FK-aware restore order)
- Summary: cron `"0 12 * * *"` (UTC) on Workers Paid; paginated NDJSON to `sparkry-crm-backups/wealth/daily/<table>/<YYYY-MM-DD>/<chunk-NNN>.ndjson`; `plaid_item.access_token_encrypted` redacted to `'BACKUP_REDACTED'` sentinel; two-token split (WRITE-only backup, separate LIST+DELETE prune token); 30-day retention via separate prune cron; restore script re-inserts in explicit FK-parent-first order with SELECT-based cross-table integrity checks (no PRAGMA in D1); decimal columns bound as JS strings (never coerced to `number`).
- Full acceptance: spec §REQ-WC-018.

## REQ-WC-019: All Workers Pages + Cron Worker Secrets enumerated and provisioned
- Summary: 9 Pages Secrets (PLAID_CLIENT_ID, PLAID_SANDBOX_SECRET, PLAID_PRODUCTION_SECRET, PLAID_ENV, PLAID_TOKEN_ENC_KEY, TWELVE_DATA_API_KEY, WEALTH_INTERNAL_KEY, WEALTH_ALLOWED_EMAILS, RESEND_API_KEY) + 10 cron Worker secrets (separate surface; includes SENTRY_DSN, R2_BACKUP_WRITE_TOKEN); WEALTH_KV binding declared in both `wrangler.toml` and `wrangler.worker.toml`; Doppler-side mirrors in `accounting/dev` for importer (WEALTH_API_BASE, WEALTH_INTERNAL_KEY, WEALTH_TARGET_DEFAULT); WEALTH_INTERNAL_KEY rotation procedure with 5-minute two-key overlap window via WEALTH_KV.
- Full acceptance: spec §REQ-WC-019.

---

# Wealth Dashboard Polish (REQ-WD-001..008)

> Round added 2026-05-13. Builds on REQ-WC-001..019. Scope: visual + interaction polish on the `/wealth` dashboard now that Plaid daily data is accumulating. Implementation in `sparkry-crm` (CF Pages + Workers + D1). Full design: `docs/superpowers/specs/2026-05-13-wealth-dashboard-polish-design.md`.

## REQ-WD-001: Net-worth chart time-range selector
- Acceptance: `/wealth` net-worth chart shows range buttons {1W, 2W, 1Mo, 3Mo, YTD, 1y, 3y, 5y, 10y, All, Custom}; Custom opens an inline date-pair picker; selected range persists across navigation via `sessionStorage` (keys `wd:nw:range` for selected range, `wd:nw:accounts` for per-account selection, `wd:nw:benchmarks` for benchmark toggle state, `wd:nw:overlays` for symbol overlay selections); server endpoint `/wealth/api/brokerage/networth-history` accepts `range=<slug>` or `start=YYYY-MM-DD&end=YYYY-MM-DD` and returns the existing JSON shape narrowed to the range; invalid range or invalid date pair returns HTTP 400 with `{error_code: 'invalid_range'}`; unit tests cover each range slug + Custom + invalid input.
- Non-Goals: User-defined custom presets beyond the listed slugs.

## REQ-WD-002: Per-account chart series overlay
- Acceptance: A multi-select dropdown above the net-worth chart lists every active `account` row grouped by broker; each selected account is rendered as an additional line series colored deterministically by `hash(account_id) → palette[16]`; the aggregate "Net worth" series is always present; selection persists via `sessionStorage` (key `wd:nw:accounts`); a "Clear" affordance resets to aggregate-only; **two distinct limits (not a contradiction):** the server hard-caps the `accounts` param at 16 (>16 → HTTP 400 `{error_code:'invalid_account_id'}`), while the UI imposes a softer **8 total visible series** readability cap (accounts+benchmarks+overlays; a 9th attempt shows an `aria-live='polite'` "Maximum 8 series visible at once" message — see design spec §4.1). Verification is a **correctness test, not a timing assertion**: 16 accounts × long history via mock D1 must return all 16 per-account series with the correct date range and no missing points (the REQ-WC-017 ≤250ms figure is a staging-soak target, not a CI gate). Legend doubles as the visibility toggle.
- Non-Goals: Per-account benchmarks; per-account export.

## REQ-WD-003: Daily-data integration + live "today" augmentation + benchmarks/holdings overlay
- Acceptance: Net-worth series uses the canonical two-tier merge of `account_balance_snapshot` (XLSX seeds) + `plaid_account_balance_snapshot` (Plaid daily) per the dedup algorithm specified in **design spec §2.1** (NOT "existing code" — this Plaid merge is a Phase D obligation; `loadHistoryState` does not yet query `plaid_account_balance_snapshot`). Merge invariants (financial-correctness, see design spec §2.1): (a) **Plaid wins** — when both sources have a row for the same `account_id` + same date, the Plaid row overrides the XLSX seed; (b) **liability sign negation** — balances where `plaid_account_balance_snapshot.plaid_account_type` ∈ {`credit`,`loan`} are stored positive (amount owed) and MUST be negated before adding to net worth; use `plaid_account_type`, never `account.account_type` (which lacks these values); (c) **scale normalization** — `plaid_account_balance_snapshot.current_balance` (scale 4) MUST be quantized to scale 2 (ROUND_HALF_UP) before merging with scale-2 `account_balance_snapshot.balance`. Today's right-most point is augmented at chart-load by calling `/wealth/api/brokerage/repriced-today` (see REQ-WD-008) so positions × latest quote replace yesterday's snapshot for current day; for investment accounts (accounts with `position_snapshot` rows) the repriced positions×quote value **replaces** any today-dated `plaid_account_balance_snapshot` row for that account — that row is excluded from the merge to prevent double-counting; cash-only accounts with no position_snapshot rows continue using their most-recent plaid_account_balance_snapshot balance — repriced-today does not refresh Plaid balances; toggle group {S&P 500 (SPY), NASDAQ (QQQ), Total Market (VTI)} shown above the chart, each backed by `historical_price` rows from the REQ-WC-013 EOD allowlist; **BND is in the EOD data allowlist but is NOT a UI toggle option** — API calls with `benchmarks=BND` return 400 `{error_code:'invalid_benchmark'}` (see BND scope note under REQ-WC-010); raw index tickers (^GSPC, ^IXIC, ^DJI) are NOT in the allowlist and are out of scope; arbitrary-symbol overlay: type-ahead lists every symbol held in any active account, selection adds that symbol's `historical_price` series rescaled to percent-of-start so it's comparable on the same y-axis as % change; benchmarks and holding overlays use a secondary y-axis (% change) when at least one is active; pct_change[0] for every benchmark/overlay series is always "0.0000" (anchor at first date in window); a range change requires a new API request — clients must not reuse cached series; non-trading-day gaps are forward-filled from most-recent prior close.
- Non-Goals: Index funds not in the existing benchmark allowlist (specifically ^GSPC, ^IXIC, ^DJI); non-held symbols.

## REQ-WD-004: Realized G&L cards backed by real data
- Acceptance: New CF route `/wealth/api/brokerage/realized-gains?year=<YYYY>` returns `{year, ytd: bool, proceeds: decimal-string, cost: decimal-string, net: decimal-string, lot_count: int, term_breakdown: {st: decimal-string, lt: decimal-string, unknown: decimal-string}, wash_sale_count: int, total_disallowed_loss: decimal-string, coverage_warnings: Array<{account_id: string, broker: string, message: string} | {scope: 'year', message: string}>}` (coverage_warnings is a discriminated union: account-scoped warnings carry account_id+broker; the year-scoped warning for mixed NULL/non-NULL term columns carries `{scope:'year', message}` and MUST NOT fabricate account_id/broker — see design spec §3.2) computed from `realized_gain_loss` rows where proceeds/cost/net are aggregated **in TypeScript via Decimal.js after fetching the rows — NOT via SQL `SUM()` on TEXT columns** (D1/SQLite silently casts TEXT to IEEE-754 double under SUM(), violating the REQ-WC-004 decimal invariant); net = SUM(proceeds) − SUM(cost_basis), term split via `term` column; `/wealth` page renders 3 cards: current calendar year YTD + prior 2 full calendar years; numbers render as USD with `Intl.NumberFormat`, negative net shown as parenthetical red; "Lots, wash-sale checks, 1099-B" link routes to `/wealth/realized` (see design spec §3.4 for content shape and column definitions); zero NaN/undefined rendered when an account has no realized lots — empty state shows "$0.00" with `lot_count: 0`; decimal precision preserved per REQ-WC-004 (string in JSON, never JS number for the values stored). Full response shape and coverage_warnings triggers: see design spec §3.2.
- Non-Goals: Wash-sale recomputation (existing `wash_sale` flag is used as-is); 1099-B PDF generation.

## REQ-WD-005: All `/wealth` links route to real sub-pages
- Acceptance: Audit every `<a href>` and route-call on `/wealth/+page.svelte`; each must resolve to a real SvelteKit route returning HTTP 200 under the WEALTH_ALLOWED_EMAILS auth guard; "Lots, wash-sale checks, 1099-B" → `/wealth/realized` (new, see design spec §3.4 for content shape and column definitions); dead links are either deleted or routed; a Vitest+Miniflare **integration** test (`tests/integration/wealth-link-audit.test.ts`, not a pure SSR unit test — it makes real HTTP round-trips through the Pages-function boundary) enumerates every linked URL on `/wealth` and asserts 200 OK with a Miniflare-injected session cookie for the WEALTH_ALLOWED_EMAILS user (auth must be genuine, not mocked; a 302 redirect is a failure); CI fails if any link returns non-200 or is a fragment-only `#`.
- Non-Goals: External links (treated as opaque).

## REQ-WD-006: Top Holdings table with current prices + day Δ%
- Acceptance: `/wealth` Top Holdings table columns: Symbol | Description | Shares | Price | Market Value | Day Δ% | Account count. Price source order (do NOT invert): `live_quote.price` if its `fetched_at` is within the intraday cache TTL (15 min) of now, **else** `historical_price.close` for the latest `trade_date`; a stale/absent live_quote is what triggers the `repriced-today` refresh (see REQ-WD-008), it is not itself the read path. Market Value = `quantize_balance(shares × price)` computed with Decimal.js (`new Decimal(shares).mul(new Decimal(price))`, ROUND_HALF_UP scale 2) — never `Number`/`parseFloat` (REQ-WC-004); null/empty `shares` or `price` → em-dash, never pass null to the Decimal constructor. Day Δ% = `(price − prior_close) / prior_close × 100` (Decimal.js, `toFixed(4)`) where `prior_close` is the most recent `historical_price.close` strictly before today's session open, using **today's US/Eastern calendar date** (`Intl.DateTimeFormat` `America/New_York`, NOT the UTC date) for the `trade_date < ?` boundary; **guard: if `prior_close` is NULL, empty, or zero → Day Δ% is null → em-dash (never divide by zero, never NaN/Infinity)**; Δ% must not rely on color alone (WCAG 1.4.1): ▲ + sr-only "increased" for Δ% > 0 (green); ▼ + sr-only "decreased" for Δ% < 0 (red); for Δ% exactly 0, render `+0.00%` in neutral color with **no glyph and no sr-only direction text**; if any column's data is unavailable, render an em-dash, never NaN; refresh triggers same as REQ-WD-008.
- Non-Goals: Real-time streaming; bid/ask spreads.

## REQ-WD-007: Sortable table column headings (with drill-down for the home page)
- Acceptance: Every `<table>` on `/wealth`, `/wealth/holdings`, `/wealth/accounts`, `/wealth/transactions`, `/wealth/realized` renders explicit `<th>` headings (no header-less tables); each sortable `<th>` uses its **implicit `columnheader` role** (do NOT add `role="button"` — invalid ARIA per ARIA in HTML §2.5); keyboard-focusable via `tabindex="0"` on the implicit `columnheader` `<th>` element; `aria-sort={none|ascending|descending}` reflects sort state; toggles asc → desc → none on click; current sort column shown with ▲/▼ glyph + accessible label; the `/wealth` home page's mini-tables (Top Holdings, Accounts summary, etc.) instead route on cell-click or header-click to the corresponding full-page route with `?sort=<col>&dir=<asc|desc>` query params; full pages read those params on SSR and pre-sort; shift-click adds a secondary sort key (nice-to-have, not blocking). Full ARIA and keyboard spec: see design spec §4.5.
- Non-Goals: Server-side sort for tables already small enough to render fully client-side (<500 rows).

## REQ-WD-008: "Refreshing prices…" banner is functional
- Acceptance: New CF route `POST /wealth/api/brokerage/repriced-today` runs an on-demand quote refresh: for every symbol held in any active account whose latest `live_quote` or `historical_price` row is staler than {5 min during US/Eastern weekday 09:30–16:00, 24h otherwise}, fetch the current quote via Twelve Data `/quote` endpoint (extract last-trade price as `data.close ?? data.price` — `close` is primary, `price` is the fallback; never read `price` alone, it may be absent in the actual response), upsert into `live_quote` table (NOT `historical_price` — `live_quote` is the purpose-built intraday cache per REQ-WC-013a; `historical_price` is immutable EOD), and return the canonical shape `{refreshed: int, skipped: int, errors: int, aborted: bool, latest_as_of: timestamp|null, stale_symbols: string[], error_code: string|null}` (`latest_as_of` is null when `refreshed===0`; `stale_symbols` is always present and the client uses `stale_symbols.length===0` as its polling-termination condition; `aborted` is `true` only when the 25s wall-clock cap fired this invocation; `error_code` is null on success and one of `'quota_exhausted'` (budget pool exhausted), `'fetch_error'` (all failures were Twelve Data network/HTTP), `'db_error'` (all failures were D1 writes), or `'partial_error'` (mixed fetch+D1 failures) on degraded 200 paths — the client treats any non-null `error_code` as a refresh failure); pre-checks the shared 600/day Twelve Data budget via `getDailyTwelveDataCount()` before any API calls — this counter MUST be `COALESCE(SUM(records_processed),0)` over today's `ingestion_log` rows where `source='twelve_data'` (actual symbols fetched), **NOT `COUNT(*)`** of invocation rows (a single run fetching 3 symbols increments the budget by 3, not 1); processes at most `REPRICED_TODAY_BATCH_SIZE` (3) symbols per invocation (client polls for remaining stale symbols — see design spec §6.7 for rationale); per-symbol D1 write failures are isolated (increment errors, continue batch); the route is rate-limited to 1 call per 30s per email-hash via WEALTH_KV; concurrent calls return HTTP 202 `{status: 'in_progress'}` immediately (no polling inside Worker) — the `stale_symbols.length===0` termination test applies **only to HTTP 200 responses**; on HTTP 202 the client treats the response as non-terminal, waits its client-side retry interval, and re-requests (it must NOT read `stale_symbols` off a 202 body, which has none); idempotent (when all symbols are fresh: no KV lock acquired, no IngestionLog row written, no Twelve Data calls); writes one `ingestion_log` row (source='twelve_data') and one `audit_events` row (entity_type='repriced_today_run', entity_id=per-run UUID) per invocation that actually calls Twelve Data (not on quota_exhausted or idempotent paths); `/wealth` page renders the "Refreshing prices…" banner from page-load until the call resolves, then re-renders the chart's today-point and Top Holdings; on failure, banner switches to "Could not refresh prices — showing last known" with a manual retry button; route enforces WEALTH_ALLOWED_EMAILS cookie guard only — WEALTH_INTERNAL_KEY is NOT applicable (this is a browser-facing endpoint, not a script-facing internal route; the WEALTH_INTERNAL_KEY pattern applies only to Python/script callers per REQ-WC-012). Full design and idempotency spec: see design spec §3.3.
- Non-Goals: Backfill of historical missing prices (covered by REQ-WC-013 cron).

---

# Plaid Sync Reliability & Intra-day Tiering (REQ-PS-001..003)

> Round added 2026-05-18. Driven by a production finding: REQ-WC-006 specified a daily Plaid balance cron, the code is correct (`plaid-balance-sync.ts` iterates every active PlaidItem; `worker.ts scheduled()` dispatches `"7 10 * * *"`), and the cron Worker config (`wrangler.worker.toml` → `sparkry-crm-cron`) is correct — but **CI only runs `wrangler pages deploy`; it never deploys the cron Worker**, so the nightly sync only ever shipped via manual deploys and runs unreliably (Plaid data observed ~4–10 days stale; account_count bursting 17→27→35; the tracked "F3 daily-sync coverage gaps"). This round makes the daily baseline reliable, adds the user's intended intra-day tier, and adds active baseline-drift alerting. Implementation in `sparkry-crm`. **Data-freshness tiers: (A) reliable nightly Plaid baseline, or matched `account_balance_snapshot` for non-Plaid/manual accounts → (B) on-login async Plaid intra-day refresh for stale Items → (C) positions×live_quote today-point for accounts Plaid does not live-update — Tier C is the existing REQ-WD-008 `repriced-today` path invoked by the REQ-WD-003 chart augmentation, NOT REQ-WD-003 itself.** REQ-PS-003 (baseline-drift alerting) is a **cross-cutting concern**, not a freshness tier: it runs after every Tier-A and Tier-B Plaid write.

## REQ-PS-001: Cron Worker is deployed by CI (Tier A reliability)
- Acceptance: the CI `deploy` job (`.github/workflows/ci.yml`) MUST deploy the cron Worker in addition to the Pages app. **Ordering & partial-failure:** the cron-Worker deploy (`wrangler deploy --config wrangler.worker.toml`, target `sparkry-crm-cron`) runs **before** `wrangler pages deploy` so the backend that the frontend depends on is never older than the frontend; a non-zero exit of EITHER step fails the job. The split deploy is intentionally NOT atomic (we are pre-customer, no rollback automation required this round) — but on cron-Worker deploy failure the job MUST stop before the Pages deploy (fail-fast, leaving prod fully on the prior version). The cron Worker's `[triggers] crons` continue to include `"7 10 * * *"` (REQ-WC-006 Plaid daily sync), `"0 14 * * MON"`, `"30 7 * * *"`, `"0 12 * * *"`, `"0 */6 * * *"`, and the two CRM crons.
- Smoke verification (split by what is CI-assertable vs operational):
  - **CI-assertable post-deploy:** the Cloudflare API confirms script `sparkry-crm-cron` exists and its registered cron triggers include `"7 10 * * *"` (assert the trigger is registered — NOT that it has fired yet, since a fresh deploy has no history).
  - **Operational gate (not a CI pass/fail):** ≥25h after deploy, a D1 query confirms an `ingestion_log` row with `source='plaid-balance-sync'` (the cron's per-Item log) written within the last 25h — i.e. the cron actually fired. Documented as a runbook check, not blocking CI.
- Regression guard (must defeat the exact original bug): a CI lint step parses `.github/workflows/ci.yml` (structured YAML, not a bare string grep) and FAILS the build unless the `deploy` job contains a step whose `run` invokes `wrangler deploy` with `--config wrangler.worker.toml`, that step has **no `if:` condition** that could skip it on a push to `main`, and is not commented/`if: false`. An integration test stubs `wrangler` to exit 1 and asserts the deploy job fails.
- Non-Goals: changing cron schedules or the **per-Item Plaid fetch/classify/write algorithm** of REQ-WC-006 (that fetch logic is unchanged — but an *additive* post-write drift-alert hook per REQ-PS-003 IS appended to the cron path; adding a downstream call is not a change to the fetch/classify/write logic); backfilling Plaid snapshots for days the cron missed before this fix; automated Pages rollback (pre-customer).

## REQ-PS-002: On-login async Plaid intra-day refresh (Tier B)
- **Schema prerequisite:** add a `fetched_at` column (INTEGER epoch-ms, NOT NULL, `DEFAULT 0`) to `plaid_account_balance_snapshot`. The default MUST be the constant `0` (a "pre-migration / infinitely-stale" sentinel) — SQLite/D1 forbids a non-constant expression (`unixepoch()*1000`) in `ALTER TABLE ADD COLUMN DEFAULT`, so an expression default is not an option; all new writes set `fetched_at` explicitly to epoch-ms. `snapshot_date` remains a DATE (one row per account per UTC day; the REQ-WD-003 net-worth merge stays per-date); `fetched_at` records when that row's value was actually retrieved, enabling sub-day staleness and fresher-wins.
- Acceptance: on authenticated `/wealth` load, the client asynchronously POSTs `/wealth/api/brokerage/refresh-plaid-balances` (cookie-guarded; browser-facing — WEALTH_INTERNAL_KEY NOT applicable per REQ-WC-012).
  - **Eligibility:** an Item is processed iff it is *active* (`plaid_item.status='active'` AND `access_token_encrypted != 'REVOKED'`) AND it was not attempted within the last 30 min (`plaid_item.last_attempted_at < now − 1800000`, set before each Plaid call) AND its newest mapped-account snapshot's `fetched_at` is older than `PLAID_LOGIN_REFRESH_TTL_S` (constant, default `14400` = 4h; hardcoded, not a secret). The 30-min per-Item cooldown — NOT a `last_sync_status` string filter — is the runaway-cost bound: `plaid_item.last_sync_status` is coarse (D1 CHECK allows only `ok|error|pending|institution_down`, never raw Plaid codes), so a persistently failing Item (terminal OR retryable) is re-attempted at most ~once / 30 min (≤48 Plaid calls/day worst case; self-heals once the Item is re-linked). On a Plaid terminal error the Item's `last_sync_status` is set to `'error'` (a CHECK-valid value, matching the existing `plaid-balance-sync.ts` convention — NOT the raw Plaid code, which the CHECK would reject). Items fresher than the TTL or inside the cooldown are counted in `skipped_items`.
  - **Write model (distinct from the cron's double-run idempotency — this is the P0 fix):** the login refresh runs the REQ-WC-006 fetch path (`/accounts/balance/get`, same retryable-vs-terminal classification, per-row isolated D1 `run()` with try/catch — the REQ-WC-006 three-layer isolation, NOT a Python `begin_nested()` savepoint) but its DB write is `ON CONFLICT(account_id, snapshot_date) DO UPDATE SET current_balance=excluded.current_balance, plaid_account_type=excluded.plaid_account_type, fetched_at=excluded.fetched_at WHERE excluded.fetched_at > plaid_account_balance_snapshot.fetched_at` (fresher value wins). The nightly cron keeps its insert-or-ignore double-run protection; the login path MUST upsert-if-fresher so a refresh later the same day actually replaces the morning baseline (a plain insert-or-ignore would silently drop the fresher intra-day value and the feature would deliver nothing).
  - **Non-blocking:** the page renders immediately from last-known data; on resolve with `refreshed_items>0` it calls the existing `chartStore.bumpRefreshCount()` (the established chart re-fetch trigger — internal `$state` of the chart store, surfaced only via that method) AND re-runs the existing `fetchTopHoldings()`/`fetchNetWorth()`/`fetchAccounts()` calls. **No new page-level `$state` and no component prop** is introduced, and there is **no `<TopHoldingsTable>` component** (top holdings is a direct `fetchTopHoldings()` call) — this mirrors exactly what the REQ-WD-008 RefreshingBanner `onRefreshed` callback already does today. A Miniflare integration test with a simulated 5s Plaid latency asserts the chart renders from last-known data within 2s of navigation, before the POST resolves.
  - **Concurrency (best-effort, NOT exclusive — KV has no atomic CAS):** WEALTH_KV key `plaid_refresh:<sha256(email)>` written with `expirationTtl: 30` (≈ the 25 s wall-clock cap + buffer; deleted on completion so the normal case clears immediately; a crashed Worker auto-recovers in ≤30 s rather than blocking logins for ~95 s). True double-fire by two cold-start requests in the same instant is possible and accepted for a single low-frequency operator; the real cost bounds are the 4 h stale-gate + 30-min per-Item cooldown. A call that finds the lock held returns HTTP 202 `{status:'in_progress'}`; the client waits 5 s (`CLIENT_RETRY_MS = 5000`, the same interval the REQ-WD-008 banner uses) and re-issues once — if still 202, it renders last-known and stops (no infinite poll).
  - **Wall-clock cap:** 25s cap (mirror REQ-WD-008). On cap, remaining Items are skipped and the response sets `aborted:true`; the client treats `aborted:true` as a non-fatal partial result (no auto-retry).
  - **Idempotent / no-op:** when zero Items are eligible (all fresh, none active, or Plaid unconfigured): return HTTP 200 `{refreshed_items:0, skipped_items:N, errors:0, aborted:false, error_code:null}`; no KV lock acquired; no IngestionLog row written.
  - **Observability:** for each Item that actually calls Plaid, write one `ingestion_log` row (`source='plaid_login_refresh'`, `records_processed`=account rows written, `status`='success'|'error'); write one `audit_events` row per non-idempotent run using the real schema columns: `entity_type='plaid_login_refresh_run'`, `entity_id`=per-run UUID, `field_changed='run'`, `new_value=JSON.stringify({refreshed_items,skipped_items,errors,aborted})`, `changed_by='system:plaid_login_refresh'` (matches design §3.2 — `field_changed` is non-nullable, so it MUST be set).
  - **Error isolation & REQ-021 reconciliation:** per-Item failures are isolated (one bad Item never blocks others or the page). REQ-021's "auth failures halt immediately" applies to script/adapter runs; in this browser-facing endpoint a per-Item terminal error is counted in `errors`, the Item's `last_sync_status` is updated, processing continues, the page is not blocked — operator notification is via the existing REQ-WC-007 stale-Item alerting.
  - Response shape `{refreshed_items:int, skipped_items:int, errors:int, aborted:bool, error_code:string|null}`. Accounts Plaid does not refresh (or non-Plaid) are unaffected and fall through to Tier C (REQ-WD-008 positions×live_quote); this endpoint does NOT touch positions or live_quote.
- Non-Goals: real-time/streaming balances; refreshing Items fresher than the TTL or inside the 30-min cooldown; blocking page render on Plaid; changing the nightly cron (REQ-PS-001/REQ-WC-006). (Terminal Plaid-error Items are NOT excluded by a status filter — `plaid_item.last_sync_status` is coarse and CHECK-limited; the 30-min cooldown bounds their cost and they self-heal once re-linked.) Window note (intentional, not a defect): with TTL=4h and cron at 10:07 UTC, an Item is login-refresh-eligible from ~14:07 UTC onward — covering afternoon/evening US sessions; the morning is already covered by the cron baseline.

## REQ-PS-003: Per-account baseline-drift alerting (cross-cutting, A′)
- Acceptance: after **every confirmed** Plaid balance write (nightly cron per REQ-WC-006 AND login refresh per REQ-PS-002), compare the just-written value against that account's **baseline** = the most-recent `plaid_account_balance_snapshot` row for the same `account_id` with `snapshot_date < today UTC` (prior-calendar-day close — the same baseline on both paths; first account with no prior-day row has no baseline → no alert). **No-op skip:** on the login path the upsert is conditional (`WHERE excluded.fetched_at > existing`); if it does not write (`D1 meta.changes === 0` — the incoming fetch was not fresher) A′ is NOT run (it must never compute drift on a value the DB does not actually hold).
  - **Sign normalization (explicit):** `normalize(b, plaid_account_type) = -b if plaid_account_type ∈ {credit, loan} else b`. Apply to BOTH sides: `delta = normalize(new) − normalize(baseline)`; `delta_pct = delta / abs(normalize(baseline)) × 100` computed with Decimal.js (ROUND_HALF_UP, scale 4). **Near-zero-baseline guard:** if `abs(normalize(baseline)) < 1.00` (covers exact zero AND tiny ~$0 balances that would otherwise yield absurd pct like 9900%) → apply only the absolute threshold (skip pct, no div-by-zero); if both normalized values are 0 → no alert. (Matches design §4.1 `bb.abs().lt(1.00)` and the test case.)
  - **Per-account-type thresholds (NOT a blanket reuse of REQ-028 — this is a temporal new-vs-prior drift, not REQ-028's cross-source Plaid-vs-positions delta; the financial review flagged that reusing 2%/$100 for investment accounts causes daily alert-fatigue on normal market moves):** `depository`/`credit`/`loan` → `abs(delta_pct) > 2.0 OR abs(delta) > 100.00` (the REQ-028 numbers — these balances should be stable). `investment` → `abs(delta_pct) > 15.0 OR abs(delta) > 25000.00` (normal market volatility is expected; alert only on gross anomalies/data errors). Thresholds live in a single named constant table.
  - On out-of-tolerance: (1) set `account.drift_flagged_at` = current epoch-ms (nullable column, add via migration; UPDATE on `account` is permitted — DELETE-guard triggers only block deletes); the reconciliation/coverage surface lists `WHERE drift_flagged_at IS NOT NULL`. **Auto-clear** to NULL on the next write whose drift vs *its* immediately-prior snapshot is **within tolerance** — i.e. the clear test reuses the exact same baseline + threshold logic as the alert test (no separate "flag-triggering snapshot" needs to be stored or reconstructed; only the timestamp column is needed; setting/clearing the column is an idempotent UPDATE, never duplicate rows). (2) Send a Resend email to `travis@sparkry.com` (subject = account name + signed delta + delta_pct). **Email dedup (cross-Worker — cron Worker and Pages Worker are separate Workers sharing WEALTH_KV):** before sending, `kv.get('drift_alert:<account_id>:<YYYY-MM-DD UTC>')`; if present, skip the email; after sending, `kv.put(key,'1',{expirationTtl:90000})` (~25h). At most one email per account per UTC day across both paths. (3) Write one `audit_events` row using the **actual** schema columns (`audit_events` has `field_changed`/`old_value`/`new_value`/`changed_by` — there is NO `action`/`changed_fields` column): `entity_type='account'`, `entity_id=account_id`, `field_changed='balance_drift'`, `old_value=String(baseline)`, `new_value=JSON.stringify({baseline,new_balance,delta,delta_pct,account_type,threshold_triggered:'pct'|'abs'|'both'})` (`delta_pct` is `null` when the zero-baseline abs-only path fired), `changed_by='system:balance_drift'` — matching the existing `plaid-balance-sync.ts` audit convention. Audit rows accumulate (not deduped); only the email is deduped.
  - **Net-worth interaction (no double-count, no silent loss):** the out-of-tolerance account is never excluded from net worth and login is never blocked. NOTE for investment accounts: net worth already uses positions×live_quote (REQ-WD-003/008), NOT the Plaid snapshot — so the alert email MUST state "net-worth display uses repriced positions for this account; the Plaid balance shown is the drift trigger, not the net-worth input" to prevent operator confusion when the flagged delta does not move the displayed total.
- Non-Goals: auto-correcting/auto-promoting balances; excluding the flagged account from net worth; blocking login or the page; alerting on first-import (no baseline); a manual-dismiss UI (auto-clear on next in-tolerance snapshot suffices this round).
