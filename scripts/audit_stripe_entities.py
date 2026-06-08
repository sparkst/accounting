"""Audit every Stripe/Shopify row's entity against its authoritative source.

The Tier-3 LLM reassigned the entity on Stripe rows that were created
needs_review — parent/Substack (Sparkry) charges, fees, and payouts were
relabeled BlackLine. The engine now preserves the adapter's entity, but existing
rows need correcting.

Stripe entity is derived from the account id embedded in the object id:
  ...AKTsgqDK5M (acct_1RNzfQ, parent/Substack) -> sparkry
  ...A6Im2mQkXF (acct_1RnR1r, Sparkry sub)      -> sparkry
  ...LR9NCMKS8x (acct_1Ryyhh, BlackLine)        -> blackline
Shopify rows are always BlackLine.

Reports every mismatch; with --apply, corrects the entity (audited). Also fixes
the one known category error: the $1,375 Stripe charge is Sparkry CONSULTING
income (user-confirmed via the invoicing/XternalSource record), not a sale.

DRY-RUN by default; --apply to commit.
"""

from __future__ import annotations

import argparse
from collections import Counter

from src.models.audit_event import AuditEvent
from src.models.transaction import Transaction

try:
    from src.db.connection import get_session
except ImportError:  # pragma: no cover
    from src.db.session import get_session  # type: ignore

ACTOR = "audit:stripe-entity-2026-06"
ACCOUNT_ENTITY = {
    "AKTsgqDK5M": "sparkry",
    "A6Im2mQkXF": "sparkry",
    "LR9NCMKS8x": "blackline",
}


def _search_blob(t: Transaction) -> str:
    raw = t.raw_data or {}
    return " ".join(
        str(x)
        for x in (
            t.source_id,
            raw.get("id"),
            raw.get("stripe_charge_id"),
            raw.get("fee_for_charge"),
            t.description,
        )
        if x
    )


def _derived_entity(t: Transaction) -> str | None:
    if t.source == "shopify":
        return "blackline"
    if t.source != "stripe":
        return None
    # Product override: a "Black Line MTB Apparel" sale belongs to BlackLine even
    # when it settled in Sparkry's Stripe account (commingled payment). Product
    # identity beats the collecting account for these.
    if "black line mtb apparel" in (t.description or "").lower():
        return "blackline"
    blob = _search_blob(t)
    for suffix, entity in ACCOUNT_ENTITY.items():
        if suffix in blob:
            return entity
    return None  # account not identifiable


def _log(session, txn_id, field, old, new):
    session.add(AuditEvent(
        transaction_id=txn_id, field_changed=field,
        old_value=None if old is None else str(old),
        new_value=None if new is None else str(new), changed_by=ACTOR,
    ))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="Commit (default: dry-run).")
    args = ap.parse_args()

    session = get_session()
    try:
        rows = (
            session.query(Transaction)
            .filter(
                Transaction.source.in_(("stripe", "shopify")),
                Transaction.status.notin_(("rejected", "split_parent")),
            )
            .all()
        )

        mismatches = []
        unknown = 0
        for t in rows:
            derived = _derived_entity(t)
            if derived is None:
                unknown += 1
                continue
            if t.entity != derived:
                mismatches.append((t, derived))

        print(f"Scanned {len(rows)} stripe/shopify rows; {unknown} unidentifiable account.")
        print(f"Entity MISMATCHES: {len(mismatches)}")
        by_move: Counter = Counter()
        for t, derived in mismatches:
            by_move[f"{t.entity}->{derived} ({t.source})"] += 1
        for k, n in sorted(by_move.items()):
            print(f"  {k}: {n}")

        # Apply entity corrections.
        for t, derived in mismatches:
            _log(session, t.id, "entity", t.entity, derived)
            t.entity = derived

        # Known category correction: the single SALES_INCOME Stripe charge of
        # $1,375 is Sparkry CONSULTING income (user-confirmed).
        cat_fixed = 0
        for t in rows:
            if (
                t.source == "stripe"
                and (t.raw_data or {}).get("object") == "charge"
                and t.tax_category == "SALES_INCOME"
                and abs(float(t.amount or 0) - 1375.0) < 0.01
            ):
                _log(session, t.id, "tax_category", t.tax_category, "CONSULTING_INCOME")
                t.tax_category = "CONSULTING_INCOME"
                cat_fixed += 1
        print(f"$1,375 charge → CONSULTING_INCOME: {cat_fixed}")

        if args.apply:
            session.commit()
            print(f"APPLIED: {len(mismatches)} entity fixes + {cat_fixed} category fix.")
        else:
            session.rollback()
            print("DRY-RUN: re-run with --apply to commit.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
