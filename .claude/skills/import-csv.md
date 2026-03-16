---
name: import-csv
description: Import a brokerage or bank CSV file
user_invocable: true
---

# IMPORT-CSV — Manual CSV Import

## Arguments

- `file`: Path to CSV file (required)
- `type`: brokerage | bank (required)
- `source`: etrade | schwab | vanguard | (bank name) — determines column mapping

## Steps

1. Validate file exists and is readable CSV
2. Detect or confirm column mapping based on source
3. Parse records, normalize to Transaction schema
4. Run dedup against existing register
5. Run classification on new records
6. Report: imported count, duplicates skipped, needs_review count

## Implementation

TODO: Wire up once CSV adapters exist at `src/adapters/brokerage_csv.py` and `src/adapters/bank_csv.py`

```bash
cd /Users/travis/SGDrive/dev/accounting
source .venv/bin/activate
python -m src.cli import-csv --file data/imports/etrade_2025.csv --type brokerage --source etrade
```
