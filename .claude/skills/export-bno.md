---
name: export-bno
description: Generate WA B&O tax report for a specified entity and period
user_invocable: true
---

# EXPORT-BNO — Generate B&O Tax Report

## Arguments

- `entity`: sparkry | blackline (required)
- `period`: YYYY-MM for monthly (Sparkry) or YYYY-QN for quarterly (BlackLine)

## Steps

1. Query confirmed revenue transactions for the entity/period
2. Classify revenue by WA B&O revenue classification codes
3. Generate report with revenue totals per classification
4. Output to `data/exports/{entity}_bno_{period}.csv`
5. Display summary

## Notes

- Sparkry files monthly
- BlackLine files quarterly

## Implementation

TODO: Wire up once B&O exporter exists at `src/export/bno_tax.py`

```bash
cd /Users/travis/SGDrive/dev/accounting
source .venv/bin/activate
python -m src.cli export-bno --entity sparkry --period 2025-12
```
