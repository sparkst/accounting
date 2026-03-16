---
name: review
description: Interactive CLI review of needs_review items
user_invocable: true
---

# REVIEW — Interactive Review Queue

## Steps

1. Query register for transactions with `status = 'needs_review'`, ordered by priority:
   - Failures/errors first
   - Possible duplicates
   - Low confidence classifications
   - First-time vendors
   - Reimbursables awaiting linking
2. For each item, display: date, vendor, amount, pre-filled entity/category, confidence, reason
3. Accept keyboard input: y=confirm, e=edit, s=split, d=mark duplicate, n=skip
4. Every action feeds back into VendorRule learning loop
5. Report: confirmed count, edited count, remaining count

## Notes

Primary review workflow is the SvelteKit dashboard at localhost:5173. This CLI review is a backup for quick triage without the browser.

## Implementation

TODO: Wire up once review logic exists

```bash
cd /Users/travis/SGDrive/dev/accounting
source .venv/bin/activate
python -m src.cli review
```
