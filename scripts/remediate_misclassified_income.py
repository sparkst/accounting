"""One-off remediation: fix income-misclassified rows surfaced by the Plaid backfill.

Two defects corrected (both inflated B&O gross via the abs(amount) tax aggregation):

  1. Authoritative-signed OUTFLOWS tagged as income.
     Plaid/bank rows with amount < 0 but an income tax_category (e.g. the Amex
     "CLAUDE.AI SUBSCRIPTION" -220.60 charge keyword-matched to
     SUBSCRIPTION_INCOME). Corrected to direction=expense, tax_category=
     OTHER_EXPENSE, status=needs_review — mirroring the new _reconcile_sign()
     classifier guard so this row and a future re-ingest agree.

  2. Processor PAYOUT deposits tagged as income (double-count).
     Positive Plaid deposits named SHOPIFYPMT / STRIPE / PAYPAL — the bank
     landing of orders/charges already counted on the processor side. Corrected
     to direction=transfer, tax_category=NULL, status=needs_review
     (reconciliation, not gross receipts).

Every field change writes an AuditEvent row. DRY-RUN by default; pass --apply to
commit. Idempotent: re-running after --apply matches zero rows.
"""

from __future__ import annotations

import argparse

from src.db.connection import get_session
from src.models.audit_event import AuditEvent
from src.models.transaction import Transaction

INCOME_CATEGORIES = (
    "CONSULTING_INCOME",
    "SUBSCRIPTION_INCOME",
    "SALES_INCOME",
    "WHOLESALE_INCOME",
)
AUTHORITATIVE_SIGN_SOURCES = ("plaid", "bank_csv")
EXCLUDED_STATUSES = ("rejected", "split_parent")
PAYOUT_NAME_MARKERS = ("SHOPIFYPMT", "STRIPE", "PAYPAL")
ACTOR = "remediation:sign-guard-backfill-2026-06"


def _log(session, txn_id: str, field: str, old, new) -> None:
    session.add(
        AuditEvent(
            transaction_id=txn_id,
            field_changed=field,
            old_value=None if old is None else str(old),
            new_value=None if new is None else str(new),
            changed_by=ACTOR,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Commit changes (default: dry-run).")
    args = parser.parse_args()

    session = get_session()
    try:
        # ── Defect 1: negative-amount income on authoritative-signed sources ──
        outflows = (
            session.query(Transaction)
            .filter(
                Transaction.tax_category.in_(INCOME_CATEGORIES),
                Transaction.amount < 0,
                Transaction.source.in_(AUTHORITATIVE_SIGN_SOURCES),
                Transaction.status.notin_(EXCLUDED_STATUSES),
            )
            .all()
        )

        # ── Defect 2: positive processor-payout deposits (SHOPIFYPMT/STRIPE) ──
        # Match by name regardless of current category/direction so we also
        # re-lock rows left half-corrected (transfer + needs_review) that the
        # reclassify pass would otherwise revert. Exclude already-confirmed so
        # the script stays idempotent.
        candidates = (
            session.query(Transaction)
            .filter(
                Transaction.source == "plaid",
                Transaction.amount > 0,
                Transaction.status.notin_(EXCLUDED_STATUSES + ("confirmed",)),
            )
            .all()
        )
        payouts = []
        for t in candidates:
            name = str((t.raw_data or {}).get("name") or "").upper()
            if any(marker in name for marker in PAYOUT_NAME_MARKERS):
                payouts.append(t)

        print(f"Defect 1 (outflow mis-tagged income): {len(outflows)} rows")
        for t in outflows:
            print(f"  {t.date} {t.source:9} {float(t.amount):>10.2f} {t.tax_category} -> OTHER_EXPENSE/expense")
            _log(session, t.id, "tax_category", t.tax_category, "OTHER_EXPENSE")
            _log(session, t.id, "direction", t.direction, "expense")
            _log(session, t.id, "status", t.status, "needs_review")
            t.tax_category = "OTHER_EXPENSE"
            t.direction = "expense"
            t.status = "needs_review"
            t.deductible_pct = 1.0
            t.review_reason = (
                "Sign/category mismatch remediation: outflow was mis-tagged as "
                "income; reset to expense for review."
            )

        print(f"\nDefect 2 (processor payout deposit tagged income): {len(payouts)} rows")
        for t in payouts:
            name = str((t.raw_data or {}).get("name") or "")[:40]
            print(f"  {t.date} {t.source:9} {float(t.amount):>10.2f} {t.tax_category} -> transfer  [{name}]")
            _log(session, t.id, "tax_category", t.tax_category, None)
            _log(session, t.id, "direction", t.direction, "transfer")
            # CONFIRMED (not needs_review): the ingest reclassify pass only
            # re-runs classification on needs_review rows, and a "shopify"
            # vendor rule would re-tag these positive deposits as income. Lock
            # them so the correction is durable.
            _log(session, t.id, "status", t.status, "confirmed")
            t.tax_category = None
            t.direction = "transfer"
            t.status = "confirmed"
            t.review_reason = (
                "Reconciliation remediation: processor payout deposit mirrors "
                "orders/charges already counted on the processor side; reset to "
                "transfer (not gross receipts)."
            )

        # ── Defect 3: Shopify refunds tagged with an income category ──
        # _parse_refund historically set tax_category=SALES_INCOME on refunds
        # (negative, direction=expense). The abs() tax aggregation then ADDED
        # them to B&O gross. Re-tag to OTHER_EXPENSE so gross = actual sales.
        refunds = (
            session.query(Transaction)
            .filter(
                Transaction.source == "shopify",
                Transaction.amount < 0,
                Transaction.tax_category.in_(INCOME_CATEGORIES),
                Transaction.status.notin_(EXCLUDED_STATUSES),
            )
            .all()
        )
        print(f"\nDefect 3 (Shopify refund tagged income): {len(refunds)} rows")
        for t in refunds:
            print(f"  {t.date} {t.source:9} {float(t.amount):>10.2f} {t.tax_category} -> OTHER_EXPENSE")
            _log(session, t.id, "tax_category", t.tax_category, "OTHER_EXPENSE")
            if t.direction != "expense":
                _log(session, t.id, "direction", t.direction, "expense")
                t.direction = "expense"
            # CONFIRMED so the reclassify pass can't re-tag via the shopify rule.
            if t.status == "needs_review":
                _log(session, t.id, "status", t.status, "confirmed")
                t.status = "confirmed"
            t.tax_category = "OTHER_EXPENSE"
            t.review_reason = (
                "Refund remediation: Shopify refund is contra-revenue, not "
                "income; re-tagged OTHER_EXPENSE so it no longer inflates B&O gross."
            )

        total = len(outflows) + len(payouts) + len(refunds)
        if args.apply:
            session.commit()
            print(f"\nAPPLIED: {total} transactions corrected, audit rows written.")
        else:
            session.rollback()
            print(f"\nDRY-RUN: {total} transactions would be corrected. Re-run with --apply to commit.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
