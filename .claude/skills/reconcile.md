---
name: reconcile
description: Run reconciliation checks and surface discrepancies
user_invocable: true
---

# RECONCILE — Cross-Source Reconciliation

## Steps

1. Check Stripe payouts appear in bank statements
2. Check Shopify payouts appear in bank statements
3. Check reimbursable expenses have matching reimbursement within 30 days (flag overdue)
4. Monthly total sanity check — flag any month with 3x deviation from average
5. Report all unmatched items and discrepancies

## Implementation

TODO: Wire up once reconciliation logic exists at `src/utils/reconciliation.py`

```bash
cd /Users/travis/SGDrive/dev/accounting
source .venv/bin/activate
python -m src.cli reconcile
```
