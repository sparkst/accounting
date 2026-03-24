# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> Cash-basis accounting system for Travis Sparks. Three entities: Sparkry AI LLC, BlackLine MTB LLC, Personal.

## Project Status

This is a greenfield project. The design spec and requirements exist but no source code has been written yet. Start by reading the design spec and requirements before implementing.

**Design spec:** `docs/superpowers/specs/2026-03-15-accounting-system-design.md`
**Requirements:** `requirements/current.md` (24 REQ-IDs)

---

## Development Commands

```bash
# Python environment
python -m venv .venv && source .venv/bin/activate
pip install fastapi uvicorn stripe shopify-python anthropic sqlalchemy aiosqlite ruff mypy pytest

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

# API server
uvicorn src.api.main:app --reload --port 8000

# Dashboard
cd dashboard && npm install && npm run dev  # localhost:5173
```

---

## Architecture

**Data flow:** Sources → Adapters (Python) → Classification (3-tier) → SQLite Register → Dashboard (SvelteKit) / Tax Exports

- **Adapters** (`src/adapters/`): One per data source. Each normalizes to a common Transaction schema. Per-record error isolation — one bad record never halts a batch.
- **Classification** (`src/classification/`): Tier 1 vendor rules (instant) → Tier 2 pattern matching → Tier 3 Claude API. Items below 0.7 confidence route to `needs_review`.
- **Learning loop**: Every human interaction (confirm, edit, correct) creates/updates a VendorRule. The system suggests aggressively; humans confirm.
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
- **API keys in `.env`** (gitignored): `STRIPE_API_KEY`, `STRIPE_ACCOUNT_SPARKRY`, `STRIPE_ACCOUNT_BLACKLINE`, `SHOPIFY_API_KEY`, `SHOPIFY_STORE_URL`, `ANTHROPIC_API_KEY`

---

## File Layout (Planned)

```
requirements/        — PRD with REQ-IDs
src/adapters/        — One adapter per data source (tests co-located as test_*.py)
src/classification/  — 3-tier classification engine
src/models/          — SQLAlchemy models (Transaction, VendorRule, IngestionLog, etc.)
src/db/              — Schema, Alembic migrations, connection
src/api/             — FastAPI routes for dashboard
src/export/          — Tax export formatters (FreeTaxUSA, TaxAct, B&O)
dashboard/           — SvelteKit frontend
data/                — SQLite DB, CSV drop zone (GITIGNORED)
```

---

## Testing

- **TDD**: Write failing test with REQ-ID first, then implement
- **Co-locate tests**: `test_*.py` alongside source files
- **Quality gates**: `pytest && ruff check && mypy`
- Test classification with known transaction fixtures
- Test dedup with intentional duplicate scenarios
- Test export formats against expected CSV structure


Tax categories, data source details, data model, and adapter specs are all in the design spec — read it when working on those areas.

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

- **`TransactionOut.fix_income_sign` (transactions.py line 116–127)**: At the response layer, if `direction == "income"` and `amount < 0`, the amount is flipped to `abs(amount)`. This corrects Gmail income transactions that were stored negative before classification set direction=income. The DB is NOT modified — only the JSON response.
- **`income_total` / `expense_total` in list response (transactions.py line 477–494)**: Aggregation always uses `func.abs(amount)`, then `expense_total` is returned as `-raw_expense` (negative). Frontend receives a signed pair: positive income, negative expenses.
- **Tax summary / export (`tax_export.py`)**: Uses `abs(amt) * deductible_pct` everywhere — sign is irrelevant to the calculation because direction is used to classify income vs expense, not the amount sign.

### Frontend behavior

- **`formatAmount(amount)` (categories.ts)**: Positive → `$X`, Negative → `(X)` (parenthetical). The `amountClass` helper colors positives green, negatives red.
- **`TransactionCard`**: Displays `formatCurrency(transaction.amount)` verbatim (no sign flip). The API's `fix_income_sign` ensures income arrives positive. Expense editing stores negative on save: `amountSign === 'expense' ? -parsed : parsed`.
- **Financials page**: Receives `gross_income` (positive), `total_expenses` (positive from API), `net_profit` from tax-summary endpoint. The `operatingExpenses` derived value calls `Math.abs(totalExpenses)` as a safety measure.

### No bugs found

The convention is consistent end-to-end. The one place that could cause confusion — Gmail income stored negative before classification — is correctly handled by `fix_income_sign` at the API response layer. Tax-summary aggregation is immune because it uses `abs()` throughout and relies on `direction` for income/expense categorisation.
