---
name: health-check
description: Check all source freshness and system status
user_invocable: true
---

# HEALTH-CHECK — System Health Report

## Steps

1. Check last successful ingestion per source against staleness thresholds:
   - Gmail/n8n: warn if > 48 hours
   - Stripe: warn if > 14 days
   - Shopify: warn if > 14 days
   - Bank CSVs: warn if > 45 days
   - Brokerage: warn if > 120 days
2. Check for unresolved ingestion failures (IngestionLog with status != success)
3. Report classification stats: auto vs human vs pending percentages
4. Report upcoming tax deadlines (B&O dates, filing dates)
5. Report vendor rule stats: total rules, recently learned

## Implementation

TODO: Wire up once staleness detection exists at `src/utils/staleness.py`

```bash
cd /Users/travis/SGDrive/dev/accounting
source .venv/bin/activate
python -m src.cli health-check
```
