# V4: Comprehensive System Review — Consolidated Requirements

> Consolidated from 4 review sources:
> 1. **CFO Gap Analysis** (Quark) — business needs, missing features
> 2. **UX Critics** (ux-knowledge, ui-expert, ui-ux-pro) — usability, accessibility, patterns
> 3. **Systems Review** (LaForge: Security, Reliability, Ops, Perf, Data Integrity)
> 4. **Playwright Walkthrough** — end-to-end flow verification
>
> Date: 2026-03-24 | Approved: All P0-P3 for inclusion

---

## Sprint Plan (per LaForge recommendation)

### Sprint 1: Foundation — Data Integrity + Reliability P0s
### Sprint 2: Performance + Operational Hardening
### Sprint 3: New Features + UX Polish

---

## Sprint 1: Foundation (P0 fixes + critical P1s)

### S1-001: Fix LLM usage logging (dead letter)
**Source:** LaForge P0-1
**File:** src/classification/llm_classifier.py
**Problem:** LLM calls never write to LLMUsageLog. Cost tracking shows $0 regardless of actual usage.
**Fix:** Add LLMUsageLog writes after each Claude API call.
**Tests:** Verify LLMUsageLog row created after classification.

### S1-002: Add SQLite backup automation
**Source:** LaForge P0-2, CFO P0-2
**Problem:** Single SQLite file, no backup script, no cron, no restore procedure.
**Fix:** Create backup script (WAL checkpoint + `.backup` copy), LaunchAgent for daily execution, restore documentation.
**Deliverables:** `scripts/backup.sh`, `scripts/restore.sh`, LaunchAgent plist, README section.

### S1-003: Add Alembic migration framework
**Source:** LaForge P0-3
**Problem:** 4 hand-written migration scripts, no version tracking. CLAUDE.md references Alembic but it's not installed.
**Fix:** Install Alembic, create initial migration from current schema, convert existing hand-written migrations, add migration instructions to CLAUDE.md.

### S1-004: Fix duplicate /reconcile/unlink route
**Source:** LaForge P0-4
**File:** src/api/routes/reconciliation.py:275+308
**Problem:** Route defined twice, FastAPI silently shadows. Second definition is dead code.
**Fix:** Remove the duplicate definition. Keep the one with proper error handling.

### S1-005: Fix session double-close in reclassify
**Source:** LaForge P0-5
**File:** src/api/routes/ingest.py:271-287
**Problem:** UnboundLocalError on exception path when session variable not yet assigned.
**Fix:** Add proper try/except/finally with session existence check.

### S1-006: Switch currency fields to Decimal
**Source:** LaForge P0-9
**Files:** src/models/transaction.py:94-105, src/utils/currency.py
**Problem:** amount_foreign and exchange_rate use Float, causing rounding errors in tax calculations.
**Fix:** Migrate to Decimal/Numeric columns. Add Alembic migration (depends on S1-003).

### S1-007: Add DB-level DELETE trigger on transactions
**Source:** LaForge P0-10
**Problem:** Never-delete rule is convention only — no enforcement at DB level.
**Fix:** Add SQLite trigger that prevents DELETE on transactions table (RAISE ABORT).

### S1-008: Fix bank CSV dedup (row-number based)
**Source:** LaForge P0-11
**File:** src/adapters/bank_csv.py:495
**Problem:** Dedup uses row number, so re-exporting with different sort creates duplicates.
**Fix:** Compute source_hash from date+amount+description instead of row position.

### S1-009: Add API authentication
**Source:** LaForge P1 (Security), CFO P0-1
**Problem:** Zero authentication. Anyone on localhost can read/modify all financial data.
**Fix:** Add API key authentication middleware. Store key in .env, validate on every request.

### S1-010: Fix sign convention inconsistency
**Source:** LaForge P1 (Data), UX P2
**Problem:** Income stored as negative in some contexts, positive in others. Expenses similar.
**Fix:** Audit and normalize: income positive, expenses negative, consistent across all endpoints.

### S1-011: Add CHECK constraints on enum columns
**Source:** LaForge P1 (Data)
**Problem:** entity, status, direction, tax_category accept any string — no DB-level validation.
**Fix:** Add CHECK constraints via Alembic migration (depends on S1-003).

### S1-012: Fix split amount precision
**Source:** LaForge P1 (Data)
**Problem:** Split amounts use round() instead of Decimal.quantize(), causing penny discrepancies.
**Fix:** Replace round() with Decimal quantize ROUND_HALF_UP.

---

## Sprint 2: Performance + Operational Hardening

### S2-001: Batch commits in adapters
**Source:** LaForge P0-7
**Files:** src/adapters/stripe_adapter.py:407, shopify_adapter.py:467
**Problem:** Per-record commit = per-record fsync. 10K records = 10K disk flushes.
**Fix:** Commit every 100-500 records. Add batch_size parameter.

### S2-002: Optimize vendor rule matching
**Source:** LaForge P0-6
**File:** src/classification/rules.py:43
**Problem:** Full table scan per classification: 1K txns × 1K rules = 1M regex evaluations.
**Fix:** Precompile regex patterns, cache compiled rules, index by prefix for fast lookup.

### S2-003: Batch Claude API calls
**Source:** LaForge P0-8
**File:** src/api/routes/ingest.py:201-209
**Problem:** Sequential Claude API calls, no batching. 200 unclassified = 3-10 minutes.
**Fix:** Use Claude's batch API or concurrent asyncio calls with rate limiting.

### S2-004: Optimize aggregation queries
**Source:** LaForge P1 (Perf)
**Problem:** Aggregation endpoint loads ALL transactions into Python memory (3 separate full loads).
**Fix:** Push aggregation to SQL (GROUP BY queries instead of Python loops).

### S2-005: Add missing database indexes
**Source:** LaForge P1 (Perf)
**Columns:** direction, source, (entity,date) composite, parent_id
**Fix:** Alembic migration to add indexes.

### S2-006: Add input validation on all PATCH endpoints
**Source:** LaForge P1 (Security/Reliability)
**Problem:** No deductible_pct range validation (0-1), no amount bounds.
**Fix:** Add Pydantic validators to all request schemas.

### S2-007: Add Claude API circuit breaker
**Source:** LaForge P1 (Reliability)
**Problem:** No circuit breaker — if Claude is down, all classification calls hang/fail serially.
**Fix:** Implement circuit breaker pattern (fail-fast after N consecutive failures, reset after timeout).

### S2-008: Prevent traceback leakage in 500 responses
**Source:** LaForge P1 (Security)
**Problem:** Unhandled exceptions return Python traceback to client.
**Fix:** Add global exception handler that returns generic 500 with error ID.

### S2-009: Add global error handler to FastAPI
**Source:** LaForge P2, UX P2
**Fix:** Middleware that catches all unhandled exceptions, logs with trace ID, returns clean JSON error.

### S2-010: Fix reconciliation overwrites user notes
**Source:** LaForge P2 (Data)
**Problem:** Manual match writes `reconciled:<id>` to notes field, potentially overwriting existing content.
**Fix:** Append instead of overwrite. Parse existing notes before adding reconciliation marker.

### S2-011: Add audit trail for reconciliation match/unlink
**Source:** LaForge P1 (Data)
**Problem:** No AuditEvent created when transactions are matched or unlinked.
**Fix:** Write AuditEvent with old/new values on match and unlink operations.

---

## Sprint 3: New Features + UX

### Financial Features (CFO)

### S3-001: Bank account model with running balances
**Source:** CFO P0-3
**Need:** Track checking, savings, credit card accounts. Running balances. Statement reconciliation.
**Deliverables:** Account model, account CRUD API, Accounts page in dashboard.

### S3-002: 1099 tracking dashboard
**Source:** CFO P1-4
**Need:** View/manage 1099 data. Expected vs received by payer. Amount match verification.
**Deliverables:** 1099 page, API endpoint aggregating payer_1099 fields.

### S3-003: Estimated tax payment log
**Source:** CFO P1-6
**Need:** Record actual IRS/state payments. Compare projected vs actual. Track remaining liability.
**Deliverables:** Payment model, payment CRUD API, section on Tax page.

### S3-004: Receipt attachment on transactions
**Source:** CFO P1-7
**Need:** Upload/capture receipt photo, link to transaction, OCR extraction.
**Deliverables:** File upload endpoint, receipt viewer in TransactionCard.

### S3-005: Annual close checklist
**Source:** CFO P1-8
**Need:** Tax-season workflow building on Monthly Close. All txns categorized, 1099s reconciled, deductions documented, exports generated.
**Deliverables:** Annual close page extending monthly-close pattern.

### S3-006: B&O filing wizard
**Source:** CFO P1-5
**Need:** Calculate → review → export DOR format → mark as filed. Monthly for Sparkry, quarterly for BlackLine.
**Deliverables:** Filing wizard component, filed-status tracking.

### S3-007: Budget vs actual
**Source:** CFO P2-9
**Need:** Simple annual budget by category. Comparison on Financials page.
**Deliverables:** Budget model, budget entry UI, budget vs actual visualization.

### S3-008: Client profitability report
**Source:** CFO P2-10
**Need:** Revenue minus allocated expenses per client/project.
**Deliverables:** New report page, allocation logic.

### S3-009: Multi-year trend charts
**Source:** CFO P2-11
**Need:** 3-5 year revenue/expense/profit trend visualization.
**Deliverables:** Chart component on Financials page, multi-year API query.

### S3-010: Home office deduction calculator
**Source:** CFO P2-14
**Need:** Square footage, percentage, associated expenses, simplified method option.
**Deliverables:** Calculator component on Tax page.

### UX Improvements

### S3-011: Fix Cash Flow page data mapping
**Source:** Playwright walkthrough (shows $0 for all sections)
**Problem:** Cash flow categories not mapping correctly to existing transaction data.
**Fix:** Verify operating/investing/financing mapping against actual transaction directions.

### S3-012: Mobile responsive navigation
**Source:** UX P1
**Problem:** Dropdown nav not optimized for mobile screens.
**Fix:** Hamburger menu pattern for screens under 768px.

### S3-013: Global entity context persistence
**Source:** UX P1
**Problem:** Entity selection resets when navigating between pages.
**Fix:** Store selected entity in URL params or svelte store, persist across navigation.

### S3-014: Monthly Close data-aware steps
**Source:** UX P2
**Problem:** Checklist steps show static text. Should show real counts ("3 uncategorized transactions").
**Fix:** Fetch health/transaction data and display in step descriptions.

### S3-015: Bank/CSV upload UI
**Source:** UX P2
**Problem:** No UI for importing bank CSVs or brokerage statements.
**Fix:** Upload page with drag-and-drop, file type detection, preview before import.

### S3-016: Fix AbortController for rapid filter changes
**Source:** UX P1
**Problem:** Rapid filter changes in Register cause stale data responses.
**Fix:** Add AbortController to fetch calls, abort previous on new request.

### S3-017: Global search
**Source:** UX P2
**Problem:** No way to search across all entities/pages for a specific vendor or amount.
**Fix:** Global search bar in nav with results spanning transactions, invoices, rules.

---

## Estimation Summary

| Sprint | Tasks | P0 | P1 | P2 | Est. Effort |
|--------|-------|----|----|-----|-------------|
| Sprint 1 | 12 | 8 | 4 | 0 | L (2-3 days) |
| Sprint 2 | 11 | 3 | 6 | 2 | L (2-3 days) |
| Sprint 3 | 17 | 0 | 5 | 12 | XL (5-7 days) |
| **Total** | **40** | **11** | **15** | **14** | |

---

## Review Sources

1. `dashboard/cfo-review.md` — CFO gap analysis (20 findings)
2. `dashboard/comprehensive-ux-review.md` — UX critics full review (541 lines)
3. LaForge systems review — 11 P0, 21 P1, 19 P2, 15 P3
4. Playwright screenshots — `screenshots/cfo-review-*.png`
