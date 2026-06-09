"""Standardize Stripe processing-fee rows: one category + correct entity.

The fee rows were created needs_review, so the reclassify pass split identical
fees across LEGAL_AND_PROFESSIONAL and SUPPLIES and reassigned ~$356 of Sparkry
fees to BlackLine. The adapter now ships fees AUTO_CLASSIFIED as
OTHER_EXPENSE / payment_processing; this corrects the existing rows.

Entity is derived from the Stripe ACCOUNT embedded in each charge id, so the fee
lands on the business whose account actually incurred it:
  ...AKTsgqDK5M (acct_1RNzfQ, parent/Substack) -> sparkry
  ...A6Im2mQkXF (acct_1RnR1r, Sparkry sub)      -> sparkry
  ...LR9NCMKS8x (acct_1Ryyhh, BlackLine)        -> blackline

DRY-RUN by default; --apply to commit.
"""

from __future__ import annotations

import argparse

from src.models.audit_event import AuditEvent
from src.models.transaction import Transaction

try:
    from src.db.connection import get_session
except ImportError:  # pragma: no cover
    from src.db.session import get_session  # type: ignore

ACTOR = "reclassify:stripe-fees-2026-06"
ACCOUNT_ENTITY = {
    "AKTsgqDK5M": "sparkry",
    "A6Im2mQkXF": "sparkry",
    "LR9NCMKS8x": "blackline",
}


def _entity_for(desc: str, current: str) -> str:
    for suffix, entity in ACCOUNT_ENTITY.items():
        if suffix in desc:
            return entity
    return current  # account not recognized — leave entity untouched


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
        rows = (
            session.query(Transaction)
            .filter(
                Transaction.source == "stripe",
                Transaction.description.like("Stripe processing fee%"),
                Transaction.status.notin_(("rejected", "split_parent")),
            )
            .all()
        )

        moved = {"sparkry": 0, "blackline": 0}
        print(f"Stripe processing-fee rows: {len(rows)}")
        for t in rows:
            target_entity = _entity_for(t.description or "", t.entity)
            if t.entity != target_entity:
                _log(session, t.id, "entity", t.entity, target_entity)
                moved[target_entity] = moved.get(target_entity, 0) + 1
                t.entity = target_entity
            if t.tax_category != "OTHER_EXPENSE":
                _log(session, t.id, "tax_category", t.tax_category, "OTHER_EXPENSE")
                t.tax_category = "OTHER_EXPENSE"
            if t.tax_subcategory != "payment_processing":
                _log(session, t.id, "tax_subcategory", t.tax_subcategory, "payment_processing")
                t.tax_subcategory = "payment_processing"
            if t.status != "confirmed":
                _log(session, t.id, "status", t.status, "confirmed")
                t.status = "confirmed"

        print(f"  entity reassigned -> {moved}")
        if args.apply:
            session.commit()
            print(f"APPLIED: {len(rows)} fee rows standardized.")
        else:
            session.rollback()
            print(f"DRY-RUN: {len(rows)} fee rows would be standardized. Re-run with --apply.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
