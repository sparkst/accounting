"""Reclassify Stripe PAYOUT objects that were mislabeled as income.

A Stripe payout (``object: payout``, ``po_...``) is money moving Stripe→bank — a
transfer, never a sale. 13 such payouts (all from the top-level Travis/Substack
account, ``po_..AKTsgqDK5M..``) were created as needs_review by the adapter,
then the ingest reclassify pass sent them to the Tier-3 LLM, which booked them
as ``entity=blackline / SALES_INCOME / income`` — $2,603.27 of phantom BlackLine
sales. The adapter now ships payouts AUTO_CLASSIFIED so this can't recur; this
script corrects the existing rows.

Each row → direction=transfer, tax_category=NULL, entity=sparkry (their real
account), status=confirmed (locked). DRY-RUN by default; --apply to commit.
"""

from __future__ import annotations

import argparse

from src.models.audit_event import AuditEvent
from src.models.transaction import Transaction

try:
    from src.db.connection import get_session
except ImportError:  # pragma: no cover
    from src.db.session import get_session  # type: ignore

ACTOR = "reclassify:stripe-payouts-2026-06"
EXCLUDED = ("rejected", "split_parent")


def _log(session, txn_id, field, old, new):
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
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="Commit (default: dry-run).")
    args = ap.parse_args()

    session = get_session()
    try:
        candidates = (
            session.query(Transaction)
            .filter(
                Transaction.source == "stripe",
                Transaction.status.notin_(EXCLUDED),
            )
            .all()
        )
        rows = [
            t
            for t in candidates
            if (t.raw_data or {}).get("object") == "payout"
            and (t.direction != "transfer" or t.tax_category is not None)
        ]

        print(f"Stripe payouts mislabeled as income: {len(rows)}")
        total = 0.0
        for t in rows:
            total += float(t.amount or 0)
            print(f"  {t.date} {t.entity:9} {float(t.amount):>9.2f} "
                  f"{t.direction}/{t.tax_category} -> transfer/None (sparkry)")
            _log(session, t.id, "direction", t.direction, "transfer")
            _log(session, t.id, "tax_category", t.tax_category, None)
            if t.entity != "sparkry":
                _log(session, t.id, "entity", t.entity, "sparkry")
                t.entity = "sparkry"
            if t.status != "confirmed":
                _log(session, t.id, "status", t.status, "confirmed")
            t.direction = "transfer"
            t.tax_category = None
            t.status = "confirmed"
            t.review_reason = None

        print(f"\nTotal moved out of income: ${total:,.2f}")
        if args.apply:
            session.commit()
            print(f"APPLIED: {len(rows)} payouts reclassified to transfer.")
        else:
            session.rollback()
            print(f"DRY-RUN: {len(rows)} payouts would be reclassified. Re-run with --apply.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
