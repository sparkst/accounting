---
name: export-tax
description: Generate tax exports (FreeTaxUSA CSV, TaxAct CSV) for a specified entity and year
user_invocable: true
---

# EXPORT-TAX — Generate Tax-Ready Exports

## Arguments

- `entity`: sparkry | blackline | personal (required)
- `year`: Tax year (default: current year - 1)
- `format`: freetaxusa | taxact | both (default: based on entity)

## Entity-Format Mapping

- **Sparkry** → FreeTaxUSA (Schedule C)
- **BlackLine** → TaxAct Business (Form 1065)
- **Personal** → FreeTaxUSA (Schedule A, D, 1099-B)

## Steps

1. Validate all transactions for the entity/year are `confirmed` — warn if any `needs_review`
2. Aggregate by tax category into IRS line-item totals
3. Generate CSV in the target format
4. Output to `data/exports/{entity}_{year}_{format}.csv`
5. Display summary with IRS line-item breakdown

## Implementation

TODO: Wire up once exporters exist at `src/export/`

```bash
cd /Users/travis/SGDrive/dev/accounting
source .venv/bin/activate
python -m src.cli export-tax --entity sparkry --year 2025
```
