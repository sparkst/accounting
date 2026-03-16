---
name: ingest
description: Run all adapters, process new records, report results
user_invocable: true
---

# INGEST — Run All Adapters

## Steps

1. Back up `data/accounting.db` to `data/accounting.db.bak`
2. Run pending Alembic migrations if any
3. Execute each adapter in order, collecting results:
   - Gmail/n8n adapter
   - Deduction email adapter
   - Stripe adapter (Sparkry + BlackLine)
   - Shopify adapter (BlackLine)
   - Photo receipt adapter (if new images detected)
4. Run deduplication pass on newly ingested records
5. Run classification on any unclassified transactions
6. Report summary: records ingested, classified, needs_review, failures

## Error Handling

- Each adapter runs independently — one failure doesn't block others
- Per-record error isolation within each adapter
- Auth/connectivity failures halt that adapter and are reported prominently

## Implementation

TODO: Wire up once adapters exist at `src/adapters/`

```bash
# Planned invocation
cd /Users/travis/SGDrive/dev/accounting
source .venv/bin/activate
python -m src.cli ingest
```
