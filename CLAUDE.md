# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> Cash-basis accounting system for Travis Sparks. Three entities: Sparkry AI LLC, BlackLine MTB LLC, Personal.

## Project Status

Production system deployed locally via launchd + Caddy reverse proxy, accessible at `https://macbook.ancon-cliff.ts.net`. All core features implemented: transaction ingestion, classification, invoicing, tax exports, reconciliation, and dashboard.

**Design spec:** `docs/superpowers/specs/2026-03-15-accounting-system-design.md`
**Requirements:** `requirements/current.md` (≈45 REQ-IDs incl. REQ-WC-001..019)

Most recent shipped scope: **Plaid Phase 1** (REQ-025..029, commit `36ea4b7`): item lifecycle, daily balance sync via launchd, stale-Item alerting, reconciliation summary, AuditEvent entity-mode extension.

**Current scope:** **Wealth → Cloudflare migration** in execute phase (REQ-WC-001..019 + REQ-WC-013a). M0 orchestrator prerequisites complete (commits `0917c73`..`3092c68`); 7 team-lead worktrees pending. Targets brokerage+Plaid at `https://internal.sparkry.ai/wealth/*` on Cloudflare Workers+D1+Pages with cash-basis register staying local.

**Migration spec:** `docs/superpowers/specs/2026-05-10-wealth-cloudflare-migration.md`
**Migration runbook:** `docs/superpowers/plans/2026-05-10-wealth-cloudflare-migration-runbook.md`

---

## Development Commands

```bash
# Python environment (deps live in pyproject.toml)
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Secrets (Doppler — never use .env files)
doppler setup --project accounting --config dev    # first-time setup
doppler run -- uvicorn src.api.main:app --reload --port 8000   # dev server with secrets
doppler run -- pytest                              # tests with secrets

# Quality gates (run before committing)
pytest
ruff check src/
mypy src/

# Run a single test
pytest src/adapters/test_gmail_n8n.py -v

# Alembic migrations
alembic current                                         # show current DB revision
alembic upgrade head                                    # apply all pending migrations
alembic revision --autogenerate -m "describe change"   # generate migration from model changes
alembic downgrade -1                                    # roll back one migration

# Dashboard (dev)
cd dashboard && npm install && npm run dev  # localhost:5173

# Dashboard (production build — required for launchd service)
cd dashboard && npm run build

# Brokerage Phase 3 importers (all DRY-RUN by default; pass --apply to write)
python -m src.adapters.xlsx_savings_plan import-balances --file <xlsx>   # historical balance snapshots
python -m src.adapters.xlsx_savings_plan import-prices   --file <xlsx>   # XLSX price seed
python -m src.adapters.xlsx_savings_plan import-lots     --file <xlsx>   # TD/SB cost-basis lots
python -m scripts.backfill_historical_prices --years 10                  # yfinance EOD backfill
python -m scripts.seed_expected_accounts seed --file <xlsx>              # expected_account seed
python -m scripts.seed_expected_accounts confirm                         # interactive active/closed walkthrough; offers Account creation for unmapped institutions
python -m scripts.seed_account_tags                                      # default tag rules per account

# Brokerage Phase 4 per-institution adapters (DRY-RUN default; --apply to write)
# Migration p4ext1enum0xt must be applied first (extends Broker + AccountType enums).
python -m src.adapters.vanguard_csv    import-positions --file <csv>     # Vanguard OFX-style CSV (brokerage 6-col + 529 5-col)
python -m src.adapters.fg_pdf          import-pdf       --file <pdf>     # F&G annuity annual/portal PDF
python -m src.adapters.nw_mutual_xlsx  import-balances  --file <xlsx>    # NW Mutual whole-life policies
python -m src.adapters.gsk_pdf         import-pdf       --file <pdf>     # GSK cash-balance pension PDF
python -m src.adapters.ft_pdf          import-statements --dir <path>    # Franklin Templeton year-end statements
```

### Optional: SQLite MCP for ad-hoc inspection

For interactive inspection during debugging, register a SQLite MCP and let Claude query without writing throwaway scripts.

**Important — the existing PreToolUse mutation guard in `.claude/settings.json` only matches `Bash` tool calls. It does NOT intercept MCP tool invocations.** Off-the-shelf SQLite MCPs (e.g., `mcp-server-sqlite-npx`, the official reference server) expose `write_query` alongside `read_query` with no `--read-only` flag, so pointing one at the live DB lets the model mutate it.

The safe pattern is to point the MCP at a **snapshot copy**, not the live DB:

```bash
# From the repo root. Use sqlite3 .backup (NOT plain cp) so WAL pages are flushed
# and the snapshot is internally consistent — a plain cp on a WAL-mode DB drops
# unflushed transactions silently.
sqlite3 data/accounting.db ".backup data/accounting.snapshot.db"
ls -la data/accounting.db data/accounting.snapshot.db   # confirm freshness
claude mcp add sqlite -s local -- npx -y mcp-server-sqlite-npx@0.8.0 "$(pwd)/data/accounting.snapshot.db"
claude mcp list                                  # verify it loaded
```

The package version is pinned so a future supply-chain change doesn't silently alter behavior. Refresh the snapshot whenever you need newer data (re-run the `sqlite3 ... .backup` command). The whole `data/` tree is gitignored, so the snapshot is too. Remove when done:

```bash
claude mcp remove sqlite
rm data/accounting.snapshot.db
```

---

## Architecture

**Data flow:** Sources → Adapters (Python) → Classification (3-tier) → SQLite Register → Dashboard (SvelteKit) / Tax Exports

- **Adapters** (`src/adapters/`): One per data source. Each normalizes to a common Transaction schema. Per-record error isolation — one bad record never halts a batch.
- **Classification** (`src/classification/`): Tier 1 vendor rules (instant) → Tier 2 pattern matching → Tier 3 Claude API. Items below 0.7 confidence route to `needs_review`.
- **Learning loop**: Every human interaction (confirm, edit, correct) creates/updates a VendorRule. The system suggests aggressively; humans confirm.
- **Invoicing** (`src/invoicing/`): Invoice generation (calendar-based + flat-rate), PDF rendering (WeasyPrint), email delivery (Resend), Stripe payment link creation. Double-billing guards on both invoice types.
- **Tax Documents** (`src/tax_docs/`): Tax document intake and processing.
- **Brokerage** (`src/adapters/{schwab,fidelity,etrade,vanguard}_csv.py`, `src/models/brokerage.py`, `src/api/routes/brokerage.py`): Broker statement CSV ingestion with position snapshots; surfaced in dashboard at `/brokerage`.
- **Brokerage history** (`src/models/history.py`): Phase 3 schema sitting alongside the live brokerage tables — `HistoricalPrice` (yfinance daily EOD), `AccountBalanceSnapshot` (XLSX historical aggregates), `ExpectedAccount` (manually-curated coverage list driving the missing-accounts panel), `CostBasisLot` (lot-level historical data), `AccountTag` (free-text tags for filter chips). Endpoints under `/api/brokerage/`: `networth-history`, `networth-history-benchmark`, `holdings/{symbol}/history`, `missing-accounts`, `PUT /accounts/{id}/tags`, `PATCH /accounts/{id}` (partial-update of `account_name`, `beneficiary`, `notes`), `GET /accounts/{id}/detail` (full per-account dossier: metadata + tags + latest 10 positions/balances + transaction count by action + lifetime realized G/L summary + recent IngestionLog rows). Per-account detail page at `/brokerage/accounts/<id>`.
- **Reports** (`src/reports/`): Weekly P&L generator, run via launchd (`com.sparkry.weekly-pl-report.plist`).
- **Utilities** (`src/utils/`): Reconciliation engine and shared helpers.
- **Dashboard** (`dashboard/`): SvelteKit frontend calling FastAPI backend. Apple design principles. Keyboard-first (y=confirm, e=edit, s=split, d=duplicate, j/k=navigate).

---

## Entities

| Entity | Tax Form | B&O |
|---|---|---|
| Sparkry AI LLC (single-member) | Schedule C | Monthly |
| BlackLine MTB LLC (partnership, Travis 100%) | Form 1065 + K-1 | Quarterly |
| Personal | 1040 Schedule A, D | N/A |

---

## Critical Rules

- **SQLite is the single source of truth** (`data/accounting.db`, gitignored, backed up via SGDrive)
- **Never delete transactions** — use `status: rejected` to exclude
- **Every transaction preserves `raw_data`** from original source
- **Full audit trail**: `created_at`, `updated_at`, `confirmed_by`, plus AuditEvent table for field-level changes
- **Reimbursable expenses** (Cardinal Health) tracked as `direction: reimbursable`, linked to reimbursement when received, both net to zero on P&L
- **Amount validation**: split line items must sum to parent total
- **Reconciliation vs dedup**: Stripe/Shopify payouts matching bank deposits are reconciliation pairs, not duplicates
- **FastAPI binds to 127.0.0.1:8000** (localhost only)
- **Secrets managed via Doppler** — never use `.env` files.

  Active Doppler configs:
  - `accounting/dev` — local importers + FastAPI runtime. Pass via `doppler run --project accounting --config dev`.
  - `accounting/prd` — off-Cloudflare backup vault for CRM-shared secrets (per `sparkry-crm/CLAUDE.md` convention). Also holds `PLAID_TOKEN_ENC_KEY_MIGRATION` (read by the wealth-migration script at cutover) and the SENTRY_DSN + R2_BACKUP_WRITE_TOKEN mirrors.
  - `accounting/stg`, `accounting/dev_personal` — staging / personal contexts.

  Keys in `accounting/dev`: `STRIPE_API_KEY`, `STRIPE_RESTRICTED_KEY`, `STRIPE_ACCOUNT_SPARKRY`, `STRIPE_ACCOUNT_BLACKLINE`, `STRIPE_ACCOUNT_TRAVIS_PERSONAL`, `RESEND_API_KEY`, `SHOPIFY_API_KEY`, `SHOPIFY_STORE_URL`, `N8N_WEBHOOK_SECRET`, `API_KEY`, `PLAID_CLIENT_ID`, `PLAID_SANDBOX_SECRET`, `PLAID_PRODUCTION_SECRET`, `PLAID_ENV`, **`PLAID_FERNET_KEY`** (renamed from `PLAID_TOKEN_ENC_KEY` at wealth-migration M0c — see `src/utils/plaid_crypto.py` for the legacy-name fallback), `WEALTH_API_BASE`, `WEALTH_INTERNAL_KEY`, `WEALTH_TARGET_DEFAULT`.

---

## Critical Patterns

- **Float → Decimal**: always `Decimal(str(value))`, never `Decimal(value)` on a float — preserves the user-facing precision. Apply at the JSON/CSV/DataFrame boundary in adapters.
- **Per-row savepoint for batch ingest**: wrap the per-row insert in `with session.begin_nested():` so an `IntegrityError` rolls back only that row. Outer `session.commit()` once at end. See `src/adapters/xlsx_savings_plan.py` for the canonical pattern.
- **Hash payload quantization**: dedup hashes (`source_row_hash`) must `Decimal.quantize()` numeric components before stringifying — otherwise `Decimal('10.50')` and `Decimal('10.5')` collide-or-not based on string format and break re-import idempotency.
- **DRY-RUN default for scripts**: every importer/seeder defaults to `dry_run=True` (or `--apply` opt-in on the CLI). Programmatic callers must explicitly opt in to writing.
- **Per-record error isolation**: one bad row never halts a batch. Catch per-row, append to `result.errors`, log, continue. Test it explicitly.

---

## File Layout

```
requirements/        — PRD with REQ-IDs (incl. REQ-WC-001..019 + REQ-WC-013a for wealth migration)
src/adapters/        — One adapter per data source (tests co-located as test_*.py)
src/adapters/plaid_*.py — Plaid client, balance-sync adapter, fixtures (REQ-025..028)
src/classification/  — 3-tier classification engine
src/models/          — SQLAlchemy models (Transaction, VendorRule, Invoice, PlaidItem, PlaidAccountBalanceSnapshot, etc.)
src/db/              — Schema, Alembic migrations, connection
src/api/             — FastAPI routes for dashboard
src/api/routes/plaid.py — Plaid lifecycle + reconciliation (REQ-025/028)
src/invoicing/       — Invoice generation, PDF rendering, email, payment links
src/tax_docs/        — Tax document intake and processing
src/utils/           — Reconciliation engine and shared helpers
src/utils/plaid_crypto.py — Fernet AES; reads PLAID_FERNET_KEY (preferred) or PLAID_TOKEN_ENC_KEY (legacy fallback)
src/export/          — Tax export formatters (FreeTaxUSA, B&O)
src/reports/         — Weekly P&L report generator
scripts/             — Operational scripts (backup, deduction-scan, auto-confirm, ingest-brokerage)
scripts/plaid_balance_sync.py — Daily Plaid balance sync; invoked by com.sparkry.plaid-balance-sync.plist
dashboard/           — SvelteKit frontend (built with vite, served via vite preview)
dashboard/src/routes/admin/connections/ — Plaid Link UI + OAuth-return handler
data/                — SQLite DB, CSV drop zone (GITIGNORED). Pre-migration snapshots at `accounting.pre-wealth-migration-<ts>.db`.
tests/fixtures/{brokerage,plaid}-golden/ — M0j pre-cutover JSON snapshots for wealth-migration step 7g comparison
docs/superpowers/specs/ — Architectural specs (wealth-Cloudflare-migration, plaid-phase-1, ...)
docs/superpowers/plans/ — Execution runbooks
docs/operational/    — Operator evidence (M0h WAF + Plaid registration receipts)
.qpipeline/          — qpipeline state directory (GITIGNORED): project checkpoints, kv-ids.txt, results
```

---

## Testing

- **TDD**: Write failing test with REQ-ID first, then implement
- **Co-locate tests**: `test_*.py` alongside source files
- **Quality gates**: `pytest && ruff check && mypy`
- Test classification with known transaction fixtures
- Test dedup with intentional duplicate scenarios
- Test export formats against expected CSV structure
- For multi-phase work (greenfield features, schema changes), use `/qpipeline thorough` — it enforces ideate → plan → execute → review-loop (to convergence) → verify → demo. Review-loops must run to actual zero P0+P1 across all 4 lenses (security, financial-correctness, code-quality, test-coverage), not be cut short.


Tax categories, data source details, data model, and adapter specs are all in the design spec — read it when working on those areas.

---

## Local Deployment

Five launchd services behind a Caddy reverse proxy over Tailscale. API plist uses `doppler run --` to inject secrets at runtime.

| Service | Plist | Port | What |
|---------|-------|------|------|
| API | `com.sparkry.accounting-api.plist` | 8000 | FastAPI via `doppler run -- uvicorn` |
| Dashboard | `com.sparkry.accounting-dashboard.plist` | 5173 | SvelteKit via `vite preview` (requires `npm run build` first) |
| Caddy | `com.sparkry.caddy-accounting.plist` | 443 | HTTPS reverse proxy |
| Backup | `com.sparkry.accounting-backup.plist` | — | Periodic SQLite backup (`scripts/backup.sh`) |
| Weekly P&L | `com.sparkry.weekly-pl-report.plist` | — | Monday P&L email (`scripts/weekly-pl-report.py`) |

```bash
# Restart a service after code changes
launchctl unload com.sparkry.accounting-api.plist && launchctl load com.sparkry.accounting-api.plist

# Dashboard changes require rebuild before restart
cd dashboard && npm run build
launchctl unload com.sparkry.accounting-dashboard.plist && launchctl load com.sparkry.accounting-dashboard.plist
```

Access via `https://macbook.ancon-cliff.ts.net` (Tailscale). Caddy routes `/api/*` → API, everything else → dashboard.

---

## Amount Sign Convention

**DB convention: expenses are negative, income is positive.**

| Direction | DB amount | Example |
|---|---|---|
| `income` | positive | `+5000.00` (Stripe charge, invoice payment) |
| `expense` | negative | `-238.03` (Gmail receipt, bank debit, Stripe refund) |
| `reimbursable` | negative | `-500.00` (expense pending reimbursement) |
| `transfer` | positive | `+4800.00` (Stripe payout — not P&L) |
| bank credit | positive | `+3000.00` (bank CSV credit column) |
| bank debit | negative | `-120.00` (bank CSV debit column) |

### Adapter behavior

- **Gmail (`gmail_n8n.py`)**: Always stores `signed_amount = -abs(amount)`. Amounts extracted from receipt bodies are always positive numbers (what was charged), so they are negated on store. Income classification is applied later by the classifier — the adapter itself treats every receipt as an expense.
- **Stripe (`stripe_adapter.py`)**: Charges and payouts stored as positive (income/transfer). Refunds explicitly stored as `-abs(amount)` (negative = expense outflow).
- **Bank CSV (`bank_csv.py`)**: Single signed-amount column passes through as-is (positive = credit/income, negative = debit/expense). Debit/credit split columns: debit → `-abs(debit_val)`, credit → `+abs(credit_val)`.

### API sign-flipping

- **`TransactionOut.fix_income_sign`**: At the response layer, if `direction == "income"` and `amount < 0`, the amount is flipped to `abs(amount)`. This corrects Gmail income transactions that were stored negative before classification set direction=income. The DB is NOT modified — only the JSON response.
- **`income_total` / `expense_total` in list response**: Aggregation always uses `func.abs(amount)`, then `expense_total` is returned as `-raw_expense` (negative). Frontend receives a signed pair: positive income, negative expenses.
- **Tax summary / export**: Uses `abs(amt) * deductible_pct` everywhere — sign is irrelevant to the calculation because direction is used to classify income vs expense, not the amount sign.

### Frontend behavior

- **`formatAmount(amount)` (categories.ts)**: Positive → `$X`, Negative → `(X)` (parenthetical). The `amountClass` helper colors positives green, negatives red.
- **`TransactionCard`**: Displays `formatCurrency(transaction.amount)` verbatim (no sign flip). The API's `fix_income_sign` ensures income arrives positive. Expense editing stores negative on save: `amountSign === 'expense' ? -parsed : parsed`.
- **Financials page**: Receives `gross_income` (positive), `total_expenses` (positive from API), `net_profit` from tax-summary endpoint. The `operatingExpenses` derived value calls `Math.abs(totalExpenses)` as a safety measure.

The convention is consistent end-to-end. Gmail income stored negative before classification is correctly handled by `fix_income_sign` at the API response layer.
