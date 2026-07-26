# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> Cash-basis accounting system for Travis Sparks. Three entities: Sparkry LLC, BlackLine MTB Apparel LLC, Personal.

## Project Status

Production runs on the Hetzner box `ubuntu-4gb-nbg1-2` (Ubuntu 24.04), public at `https://books.sparkry.ai` via Cloudflare Tunnel + Cloudflare Access (Google SSO) — migrated off the MacBook/launchd stack June 2026 (spec `docs/superpowers/specs/2026-06-01-accounting-hetzner-migration-design.md`). All core features implemented: transaction ingestion, classification, invoicing, tax exports, reconciliation, and dashboard.

**Design spec:** `docs/superpowers/specs/2026-03-15-accounting-system-design.md`
**Requirements:** `requirements/current.md` (≈45 REQ-IDs incl. REQ-WC-001..019)

Most recent shipped scope: **Wealth → Cloudflare migration** (REQ-WC-001..019 + REQ-WC-013a) — brokerage UI live at `https://internal.sparkry.ai/wealth/*` on Cloudflare Pages + D1, cash-basis register stays local. Plaid Phase 1 (REQ-025..029, commit `36ea4b7`) shipped earlier: item lifecycle, daily balance sync via launchd, stale-Item alerting, reconciliation summary, AuditEvent entity-mode extension. **Plaid Phase 2** (REQ-PT-001..017) adds transaction ingestion into the register: adapter `src/adapters/plaid_transactions.py`, CLI `scripts/plaid_transactions_sync.py`, manual endpoint `POST /api/plaid/items/{id}/sync-transactions`. Plaid is **sole source of truth** per linked account — CSV supersede (`status="rejected"`, `review_reason="superseded_by_plaid"`) and `bank_csv` skip both key off the `payment_method` label (the register has no account FK). `/transactions/sync` is cursor-based; pending→posted reconcile keys off `pending_transaction_id`.

**Current scope:** Maintenance after **Program 2026-07** (shipped 2026-07-08): 54 audited bug fixes + 10 features — Plaid balance-sync repair (`/accounts/get`), alert-delivery reliability (retry+sweep+delivery-health), tax/B&O correctness (Shopify payout P0, pre-tax basis), payment-link/AR integrity, ingestion/learning-loop fixes, total-return wealth analytics + investment-policy dashboard (`/wealth/policy`), WBR/close/sellability/tax-forecast reports, auto-confirm ≥0.90, AR chaser (draft-for-approval), vision statement-ingestion shadow pipeline. See `requirements/current.md` § Program 2026-07 (incl. the FUP-01..10 follow-ups table) and `docs/superpowers/plans/2026-07-07-remediation-feature-program.md`. Local dashboard `/brokerage` route renamed to `/wealth` 2026-05-12.

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

# EA alert dispatch (DRY-RUN default; --apply to POST to n8n)
doppler run -- python scripts/alerts_dispatch.py            # dry-run, today
doppler run -- python scripts/alerts_dispatch.py --apply    # send
doppler run -- python scripts/alerts_dispatch.py --date 2026-06-30  # test a date

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

## Quark — your Ferengi CFO

`Quark` is a persona skill (`~/.claude/skills/quark/`) that runs the books as a
colorful DS9-Ferengi CFO + wealth advisor + draft-only stock-picker across all
three entities. It triggers on "Quark", "ask my CFO", "run it by Quark", or any
P&L / runway / tax-posture / net-worth / deal-pressure-test / investment ask.

**Lazy-loads the latest financials on every invocation.** Production data lives on
the Hetzner box; the local DB is stale. `scripts/quark_refresh.sh` syncs every
account on the box (the sanctioned Plaid jobs) and pulls a **read-only** snapshot
to `data/accounting.live.db` (gitignored), which Quark reads via `DATABASE_PATH`
— never touching the local source-of-truth DB.

```bash
bash scripts/quark_refresh.sh           # full: sync accounts on the box, then pull (~1 min)
bash scripts/quark_refresh.sh --quick   # snapshot only, no sync (~15s)
```

Guardrails baked into the skill: **read-only** (SELECT only; the only sanctioned
write is the box's own daily sync) and **draft-only** (never sends/pays/trades —
mirrors `qdecide`). Requires Tailscale SSH to the box (`travis@ubuntu` +
`root@ubuntu` for the sync trigger); falls back to the stale local DB and says so
if the box is unreachable.

---

## Architecture

**Data flow:** Sources → Adapters (Python) → Classification (3-tier) → SQLite Register → Dashboard (SvelteKit) / Tax Exports

- **Adapters** (`src/adapters/`): One per data source. Each normalizes to a common Transaction schema. Per-record error isolation — one bad record never halts a batch.
- **Classification** (`src/classification/`): Tier 1 vendor rules (instant) → Tier 2 pattern matching → Tier 3 Gemini API (`gemini-2.5-flash-lite`). Items below 0.7 confidence route to `needs_review`.
- **Learning loop**: Every human interaction (confirm, edit, correct) creates/updates a VendorRule. The system suggests aggressively; humans confirm.
- **Invoicing** (`src/invoicing/`): Invoice generation (calendar-based + flat-rate), PDF rendering (WeasyPrint), email delivery (Resend), Stripe payment link creation. Double-billing guards on both invoice types.
- **Tax Documents** (`src/tax_docs/`): Tax document intake and processing.
- **Brokerage** (`src/adapters/{schwab,fidelity,etrade,vanguard}_csv.py`, `src/models/brokerage.py`, `src/api/routes/brokerage.py`): Broker statement CSV ingestion with position snapshots; surfaced in dashboard at `/wealth` (frontend route renamed 2026-05-12; backend API paths still `/api/brokerage/*`).
- **Brokerage history** (`src/models/history.py`): Phase 3 schema sitting alongside the live brokerage tables — `HistoricalPrice` (yfinance daily EOD), `AccountBalanceSnapshot` (XLSX historical aggregates), `ExpectedAccount` (manually-curated coverage list driving the missing-accounts panel), `CostBasisLot` (lot-level historical data), `AccountTag` (free-text tags for filter chips). Endpoints under `/api/brokerage/`: `networth-history`, `networth-history-benchmark`, `holdings/{symbol}/history`, `missing-accounts`, `PUT /accounts/{id}/tags`, `PATCH /accounts/{id}` (partial-update of `account_name`, `beneficiary`, `notes`), `GET /accounts/{id}/detail` (full per-account dossier: metadata + tags + latest 10 positions/balances + transaction count by action + lifetime realized G/L summary + recent IngestionLog rows). Per-account detail page at `/wealth/accounts/<id>`. `networth-history?include_unmatched=true` runs the canonical **per-name effective-cutoff** dedup against legacy XLSX rows (REQ-FIX-WLT-004): the predicate lives in `src/utils/networth_dedup.py::unmatched_active_at` (tier-1 matched-name first date ∪ tier-2 `account_alias` cutoff), shared with the sparkry-crm D1 port via the SHA-guarded fixture `tests/fixtures/wealth-parity/networth_dedup_cases.json` — the two implementations now mirror exactly (the prior local global-cutoff divergence is fixed). Also under `/api/brokerage/`: `policy` (REQ-IPD concentration/glide/excise + bold-bets cap), `bold-bets` (REQ-BBT sleeve), `networth-attribution` (REQ-NWA market/flows/coverage decomposition). `HistoricalPrice` carries `adj_close` (total-return series) alongside raw `close`, and `stock_split` drives split-safe re-pricing (REQ-FIX-WLT-001/002).
- **Reports** (`src/reports/`): Weekly P&L generator, run via systemd timer (`weekly-pl-report.timer`); writes `reports/weekly-pl-latest.txt` (served at `/reports/*`).
- **Utilities** (`src/utils/`): Reconciliation engine and shared helpers.
- **Dashboard** (`dashboard/`): SvelteKit frontend calling FastAPI backend. Apple design principles. Keyboard-first (y=confirm, e=edit, s=split, d=duplicate, j/k=navigate).

---

## Entities

| Entity | Tax Form | B&O |
|---|---|---|
| Sparkry LLC (single-member) | Schedule C | Monthly |
| BlackLine MTB Apparel LLC (2-partner LLC: Travis 100% vested + Emerson 0% vested profits interest; org. June 2025) | Form 1065 + K-1 (TaxAct Business) | Quarterly |
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
- **$0.00 Shopify orders are usually CORRECT, not a broken ingest.** BlackLine comps product regularly (contest prizes, photoshoot models, collaborators). Those go out as `source_name: "shopify_draft_order"` with a 100%-of-line-items `total_discounts`, empty `payment_gateway_names`, and `total_price: "0.00"` — the adapter stores $0 because $0 was received, so they contribute nothing to B&O gross receipts, correctly. Verified for #1017/#1018/#1019/#1020 (Feb 2026) on 2026-07-25; that batch was twice mistaken for an ingestion defect. Before treating a $0 order as a bug, check `total_discounts` vs `total_line_items_price` and `discount_applications[].title`. **Separately**: goods bought for resale and then given away may owe WA **use tax on their cost** (WAC 458-20-178) — a different line from B&O, and not currently tracked anywhere in this system.
- **FastAPI binds to 127.0.0.1:8000** (localhost only)
- **Secrets managed via Doppler** — never use `.env` files.

  Active Doppler configs:
  - `accounting/dev` — local importers + FastAPI runtime. Pass via `doppler run --project accounting --config dev`.
  - `accounting/prd` — off-Cloudflare backup vault for CRM-shared secrets (per `sparkry-crm/CLAUDE.md` convention). Also holds `PLAID_TOKEN_ENC_KEY_MIGRATION` (read by the wealth-migration script at cutover) and the SENTRY_DSN + R2_BACKUP_WRITE_TOKEN mirrors.
  - `accounting/srv` — Hetzner server runtime config; selected by the Doppler **service token** in root-600 `/etc/accounting/doppler.env` (no `--config` flag), injected via `doppler run -- env -u DOPPLER_TOKEN <cmd>`. (Per the Hetzner migration: never `accounting/dev` / `accounting/prd` on the box.)
  - `accounting/stg`, `accounting/dev_personal` — staging / personal contexts.

  Keys in `accounting/dev`: `STRIPE_API_KEY`, `STRIPE_RESTRICTED_KEY`, `STRIPE_ACCOUNT_SPARKRY`, `STRIPE_ACCOUNT_BLACKLINE`, `STRIPE_ACCOUNT_TRAVIS_PERSONAL`, `RESEND_API_KEY`, `SHOPIFY_API_KEY`, `SHOPIFY_STORE_URL`, `N8N_WEBHOOK_SECRET`, `API_KEY`, `PLAID_CLIENT_ID`, `PLAID_SANDBOX_SECRET`, `PLAID_PRODUCTION_SECRET`, `PLAID_ENV`, **`PLAID_FERNET_KEY`** (renamed from `PLAID_TOKEN_ENC_KEY` at wealth-migration M0c — see `src/utils/plaid_crypto.py` for the legacy-name fallback), `WEALTH_API_BASE`, `WEALTH_INTERNAL_KEY`, `WEALTH_TARGET_DEFAULT`, **`N8N_ALERTS_WEBHOOK_URL`**, **`N8N_ALERTS_WEBHOOK_SECRET`**, **`ALERT_FROM_EMAIL`**, **`ALERT_TO_EMAIL`** (EA alert routing — provisioned in `dev` + `srv` 2026-07-07), `REPORT_TO_EMAIL` (WBR/close/tax report recipient), `GEMINI_API_KEY`, `OPENAI_API_KEY`, `VISION_PROVIDER`, `CLOSE_NARRATIVE_LLM` (runtime-LLM config for the close agent + vision shadow pipeline, program 2026-07).

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
scripts/plaid_balance_sync.py — Daily Plaid balance sync; box unit `plaid-balance-sync.timer` LIVE (04:00 UTC, writes `plaid_account_balance_snapshot` rows — the prior-day baseline the balance-milestone alerts cross against)
scripts/balance_alerts_dispatch.py — Daily balance-milestone alerts + account-pulse digest (REQ-BAL-001..010); box unit `accounting-balance-alerts.timer` LIVE (14:00 UTC, `--apply --digest` → n8n severity webhook). See `src/balance_alerts/`.
scripts/plaid_transactions_sync.py — Daily Plaid transactions sync (Phase 2); box unit `plaid-transactions-sync.timer` LIVE (daily 05:00 UTC, writes real txns — Amex + Chase). Job exits non-zero if any Item errors, so a single `ITEM_LOGIN_REQUIRED` (re-auth needed) trips the daily OnFailure alert even though the other Items synced.
scripts/adapter_sync.py — Scheduled Stripe/Shopify ingest (REQ-FIX-ING-020); box units `accounting-stripe-sync.timer` (05:20 UTC) + `accounting-shopify-sync.timer` (05:30 UTC), both LIVE. Wraps `POST /api/ingest/run`, which returns HTTP 200 even on adapter failure — the wrapper converts embedded `errors`/`records_failed` into a non-zero exit so `OnFailure=` actually alerts. Both adapters had silently not run since 2026-06-08 before this existed.
scripts/uptime_check.sh — local serving-stack health probe (`accounting-uptime-check.timer`, every 5 min → alert on failure)
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

## Production Deployment (Hetzner)

Runs on the Hetzner box `ubuntu-4gb-nbg1-2` (Ubuntu 24.04), public at **`https://books.sparkry.ai`**:
`cloudflared` tunnel → **Caddy** (`127.0.0.1:9000`, `admin off`) → uvicorn (`/api/*`→8000) / SvelteKit (`/`→5173, `/reports/*` file_server). **Cloudflare Access** gates the edge — Google SSO for humans; scoped **service tokens** for machines (`books-ingest` → `/api/ingest/*` for n8n; `books-health-ping` → `/api/health/ping` for monitoring). All services run as `travis` under **systemd**; secrets come from Doppler config **`accounting/srv`** (service token in root-600 `/etc/accounting/doppler.env`), injected with `doppler run -- env -u DOPPLER_TOKEN <cmd>` so the token reaches neither the process argv nor the app environment. Backups go to **Cloudflare R2** via `wrangler` (replaces SGDrive). The agentic-collab sandbox cohabits as a separate `collab` user; an nftables OUTPUT rule blocks `collab`→loopback app ports. The old MacBook launchd/Caddy/Tailscale stack is retired.

**Migration:** spec `docs/superpowers/specs/2026-06-01-accounting-hetzner-migration-design.md` + plan `docs/superpowers/plans/2026-06-01-accounting-hetzner-migration.md`.

| Unit | Type | What |
|------|------|------|
| `accounting-api.service` | service | FastAPI/uvicorn `127.0.0.1:8000` (boot-asserts `API_KEY` ≠ `INGEST_API_KEY` in production) |
| `accounting-dashboard.service` | service | SvelteKit `vite preview` `127.0.0.1:5173` |
| `caddy.service` | service | reverse proxy `127.0.0.1:9000` (cloudflared upstream) |
| `cloudflared.service` | service | Cloudflare Tunnel `books-accounting` (token in root-600 `/etc/cloudflared/token`) |
| `accounting-alert@.service` | template | Resend email on any unit's `OnFailure=` (hourly-deduped, sentinel in `data/.alerts`) |
| `accounting-backup.timer` | timer | daily 03:17 UTC → R2 (`scripts/backup.sh`, readback-verified, 15d rolling) |
| `accounting-backup-restore-test.timer` | timer | weekly Sun 07:00 UTC restore + row-count oracle (`scripts/backup_restore_test.py`) |
| `accounting-disk-check.timer` | timer | every 6h; `<5 GB` free → alert |
| `weekly-pl-report.timer` | timer | Mon 06:00 UTC → writes `reports/weekly-pl-latest.txt` (served at `/reports/*`) |
| `accounting-uptime-check.timer` | timer | every 5 min; local Caddy `:9000` health probe → alert on failure |
| `plaid-transactions-sync.timer` | timer | **LIVE** daily 05:00 UTC → `plaid_transactions_sync --apply` (Amex + Chase). Exits non-zero if any Item errors (e.g. `ITEM_LOGIN_REQUIRED`), tripping the OnFailure alert. |
| `plaid-balance-sync.timer` | timer | **LIVE** daily 04:00 UTC → `plaid_balance_sync --apply`; writes `plaid_account_balance_snapshot` (the prior-day baseline for balance-milestone alerts). |
| `accounting-stripe-sync.timer` | timer | **LIVE** daily 05:20 UTC → `adapter_sync --source stripe --apply` (POSTs `/api/ingest/run`). `Persistent=true` so a missed day catches up. |
| `accounting-shopify-sync.timer` | timer | **LIVE** daily 05:30 UTC → `adapter_sync --source shopify --apply`. Same wrapper; the payouts-scope 403 is an allowlisted benign error (see `scripts/adapter_sync.py`). |
| `accounting-balance-alerts.timer` | timer | **LIVE** daily 14:00 UTC → `balance_alerts_dispatch --apply --digest` → n8n severity webhook (`info`/`sev3`/`sev2` Telegram). Milestone alerts (REQ-BAL-001..010) + daily account-pulse. |
| `accounting-ea-alerts.timer` | timer | daily 14:05 UTC → EA WA B&O tax + invoice-submission reminders via n8n webhook (`scripts/alerts_dispatch.py --apply`); secrets provisioned 2026-07-07; failed-row sweep + Persistent catch-up per REQ-FIX-ALR-002/004. |
| `accounting-ar-chaser.timer` | timer | **LIVE** daily 14:15 UTC → AR reminder ladder (14/30/45d), draft-for-approval ONLY (Telegram card via severity webhook; CLI `scripts/ar_chaser.py approve <id>`); nothing emails a client without explicit approval (REQ-ARC). |
| `accounting-wbr.timer` | timer | **LIVE** Mon 06:00 PT → Weekly Business Review email (`scripts/wbr_dispatch.py`, Resend → `REPORT_TO_EMAIL`) (REQ-WBR). |
| `accounting-autoconfirm-digest.timer` | timer | **LIVE** Mon 14:10 UTC → weekly auto-confirm digest email w/ per-row undo commands (REQ-MCA-003). |
| `accounting-monthly-close.timer` | timer | **LIVE** 1st 15:00 UTC → monthly close report: Plaid-vs-register tie-out, anomaly scan, hygiene lines, via Resend (REQ-MCA-001). |
| `accounting-sellability.timer` | timer | installed, **disabled** — the sellability section is EMBEDDED in the monthly close email per REQ-SEL-001 (standalone render still available via `scripts/sellability_dispatch.py`). |
| `accounting-tax-forecast.timer` | timer | installed, **GATED — do not enable** until `config/tax_tables/2026.yaml` is verified vs the IRS Rev. Proc. (FUP-09) and `config/tax_profile.yaml` is filled on the box (REQ-TXF). |

All service units use `ProtectSystem=strict` + `ProtectHome=read-only` + a syscall/namespace sandbox; `XDG_*` / `DOPPLER_CONFIG_DIR` are redirected into `data/` so tools can write logs/cache under the read-only home.

```bash
# Box access: ssh travis@ubuntu (Tailscale SSH); ssh root@ubuntu for systemctl/firewall.
# Code is NEVER hand-edited on the box — change on the Mac → commit → push → rsync.
ssh root@ubuntu 'systemctl restart accounting-api accounting-dashboard caddy'

# Dashboard changes require a rebuild (VITE_API_KEY baked in) before restart:
cd dashboard && doppler run --config srv -- npm run build   # then re-rsync + restart accounting-dashboard
```

Access via `https://books.sparkry.ai` (Cloudflare Access → Google login). n8n delivers Gmail receipts to `POST /api/ingest/gmail`.

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
