---
name: classify
description: Run classification on unclassified transactions
user_invocable: true
---

# CLASSIFY — Run Classification Engine

## Steps

1. Query register for transactions with `status = 'unclassified'` or `status = 'needs_review'` with no prior classification attempt
2. Run 3-tier classification:
   - Tier 1: Vendor rules lookup (deterministic)
   - Tier 2: Pattern matching (structural rules per source)
   - Tier 3: Claude API for remaining items (budget-capped)
3. Items with confidence >= 0.7 → `auto_classified`
4. Items with confidence < 0.7 → `needs_review` with reason
5. Report: auto-classified count, needs_review count, LLM calls used

## Implementation

TODO: Wire up once classification engine exists at `src/classification/`

```bash
cd /Users/travis/SGDrive/dev/accounting
source .venv/bin/activate
python -m src.cli classify
```
