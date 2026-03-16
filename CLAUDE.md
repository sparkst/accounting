# Accounting System — Claude Code Guidelines

> Cash-basis accounting system for Travis Sparks. Three entities: Sparkry AI LLC, BlackLine MTB LLC, Personal.

---

## Project Overview

Ingests financial data from multiple sources (email receipts, Stripe, Shopify, brokerage/bank CSVs, photo receipts, deduction emails), classifies transactions by entity and tax category, deduplicates, and produces tax-ready exports. Local-first: SQLite register, Python processing, SvelteKit dashboard at localhost:5173.

**Design spec:** `docs/superpowers/specs/2026-03-15-accounting-system-design.md`

---

## Quick Commands

- `INGEST` — Run all adapters, process new records
- `CLASSIFY` — Run classification on unclassified transactions
- `REVIEW` — Interactive CLI review of needs_review items
- `EXPORT-TAX` — Generate tax exports (FreeTaxUSA CSV, TaxAct CSV)
- `EXPORT-BNO` — Generate B&O tax report
- `RECONCILE` — Run reconciliation checks
- `IMPORT-CSV` — Import brokerage or bank CSV
- `HEALTH` — Check source freshness and system status

---

## Entities

| Entity | Structure | Tax Form | B&O |
|---|---|---|---|
| Sparkry AI LLC | Single-member LLC | Schedule C | Monthly |
| BlackLine MTB LLC | Partnership LLC (Travis 100% / Emerson 0% vested 2025) | Form 1065 + K-1 | Quarterly |
| Personal | N/A | 1040 Schedule A, D | N/A |

---

## Core Principles

### Data Integrity
- SQLite is the single source of truth (`data/accounting.db`)
- Every transaction preserves `raw_data` from the original source
- Full audit trail: `created_at`, `updated_at`, `confirmed_by`
- Never delete — use `status: rejected` to exclude

### Classification
- Three-tier: vendor rules (instant) → pattern matching → LLM (Claude API)
- Pre-fill and suggest aggressively — human confirms, not enters
- Every human interaction is a learning event (new rules, updated rules, corrections, first-time assignments)
- Reimbursable expenses (Cardinal Health) tracked separately, netted on P&L

### Error Handling
- Every adapter wraps each record individually — one bad record doesn't halt the batch
- All failures logged to `IngestionLog` with full detail
- Retryable failures get exponential backoff (3 attempts)
- Auth/connectivity failures halt the adapter and surface on dashboard immediately
- Staleness detection: warn if any source goes silent beyond expected threshold
- Reconciliation checks catch cross-source discrepancies
- Amount validation: split line items must sum to parent total

### Security
- Financial data stays local (no cloud DB)
- API keys stored in `.env` (never committed)
- SQLite DB in `data/` directory (gitignored)
- No PII in logs beyond transaction descriptions
- Stripe/Shopify API calls use read-only scopes where possible

---

## Tech Stack

- **Python 3.12+** — Adapters, classification, API server
- **FastAPI** — Backend API for dashboard
- **SQLite** — Register database
- **SvelteKit** — Dashboard frontend
- **Claude API** — LLM classification, photo receipt OCR
- **Stripe Python SDK** — Sparkry + BlackLine payment data
- **Shopify Admin API** — BlackLine ecommerce data

---

## Data Sources

| Source | Adapter | Frequency | Trigger |
|---|---|---|---|
| Gmail/n8n | `gmail_n8n.py` | Continuous (n8n runs every 12h) | Scheduled |
| Deduction emails | `deduction_email.py` | Continuous | Scheduled |
| Stripe | `stripe_adapter.py` | Daily | Scheduled |
| Shopify | `shopify_adapter.py` | Daily | Scheduled |
| Brokerage CSVs | `brokerage_csv.py` | Periodic | Manual |
| Bank CSVs | `bank_csv.py` | Periodic | Manual |
| Photo receipts | `photo_receipt.py` | As needed | Manual or watch |

---

## Dashboard UX Rules

- **Apple design:** Minimal chrome, generous whitespace, progressive disclosure
- **Zero friction:** One click to confirm, keyboard shortcuts for everything
- **Pre-fill everything:** Dropdowns pre-selected with best guess, not blank
- **All lists:** Sortable by column header, date picker with presets, type-ahead search, stackable filters
- **Learning:** Every human interaction (confirm, edit, assign, correct) feeds back into vendor rules
- **Keyboard:** y=confirm, e=edit, s=split, d=mark duplicate, j/k=navigate, /=search

---

## Tax Categories

### Business (Schedule C / 1065)
ADVERTISING, CAR_AND_TRUCK, CONTRACT_LABOR, INSURANCE, LEGAL_AND_PROFESSIONAL, OFFICE_EXPENSE, SUPPLIES, TAXES_AND_LICENSES, TRAVEL, MEALS (50%), COGS, CONSULTING_INCOME, SUBSCRIPTION_INCOME, SALES_INCOME, REIMBURSABLE

### Personal (Schedule A / Other)
CHARITABLE_CASH, CHARITABLE_STOCK, MEDICAL, STATE_LOCAL_TAX, MORTGAGE_INTEREST, INVESTMENT_INCOME, PERSONAL_NON_DEDUCTIBLE

---

## File Layout

```
requirements/        — PRD with REQ-IDs (current.md, requirements.lock.md)
src/adapters/        — One adapter per data source (tests co-located as test_*.py)
src/classification/  — 3-tier classification engine
src/models/          — SQLAlchemy/dataclass models
src/db/              — Schema, Alembic migrations, connection
src/api/             — FastAPI routes for dashboard
src/export/          — Tax export formatters
dashboard/           — SvelteKit frontend
data/                — SQLite DB, CSV drop zone (GITIGNORED, backed up via SGDrive)
```

---

## Testing

- TDD: Write failing test with REQ-ID first
- Co-locate tests: `test_*.py` alongside source
- Quality gates: `pytest && ruff check && mypy`
- Test classification with known transaction fixtures
- Test dedup with intentional duplicate scenarios
- Test export formats against expected CSV structure

---

## Research Needed (First Session)

1. **Stripe API** — Read-only scopes, webhook vs. polling, metadata fields for entity tagging
2. **Shopify Admin API** — Order/transaction endpoints, payout reconciliation
3. **FreeTaxUSA import format** — CSV column spec for Schedule C, K-1, Schedule A, 1099-B
4. **TaxAct 1065 import format** — CSV/data import capabilities
5. **WA B&O tax** — Filing format requirements, revenue classification codes
6. **GAAP cash-basis** — Confirm cash-basis is appropriate, any gotchas for partnership with loss
7. **Claude Vision API** — Best practices for receipt OCR, prompt engineering for line-item extraction

---

## Environment Setup

```bash
# Python
python -m venv .venv
source .venv/bin/activate
pip install fastapi uvicorn stripe shopify-python anthropic sqlalchemy aiosqlite ruff mypy pytest

# Dashboard
cd dashboard
npm install
npm run dev  # localhost:5173

# API Server
cd src
uvicorn api.main:app --reload --port 8000

# Environment variables (.env — never commit)
STRIPE_API_KEY_SPARKRY=sk_...
STRIPE_API_KEY_BLACKLINE=sk_...
SHOPIFY_API_KEY=...
SHOPIFY_STORE_URL=e1ygfk-r8.myshopify.com
ANTHROPIC_API_KEY=sk-ant-...
```

---

## Permissions

- Default: `acceptEdits`
- No destructive operations on financial data
- `.env` and `data/` are gitignored
- Read-only API scopes where available
