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
