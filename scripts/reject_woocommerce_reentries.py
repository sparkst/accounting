"""Reject Shopify orders that are WooCommerce 2025 sales re-entered only to ship.

Andrew Sullivan (#1015) and Hudson Hollatz (#1016) were paid via WooCommerce in
2025; they were re-created in Shopify in Jan 2026 solely to fulfill/ship, so they
are NOT 2026 sales. Mark them rejected (never deleted) so they leave 2026 income.

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

ACTOR = "reject:woocommerce-reentry-2026-06"
ORDER_NAMES = {"#1015", "#1016"}
REASON = "WooCommerce 2025 sale re-entered in Shopify only to ship; not 2026 revenue."


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="Commit (default: dry-run).")
    args = ap.parse_args()

    session = get_session()
    try:
        candidates = (
            session.query(Transaction)
            .filter(
                Transaction.entity == "blackline",
                Transaction.source == "shopify",
                Transaction.status.notin_(("rejected", "split_parent")),
            )
            .all()
        )
        rows = [t for t in candidates if (t.raw_data or {}).get("name") in ORDER_NAMES]

        print(f"Shopify re-entries to reject: {len(rows)}")
        for t in rows:
            name = (t.raw_data or {}).get("name")
            print(f"  {t.date} {name} {float(t.amount):.2f} {t.status} -> rejected")
            session.add(
                AuditEvent(
                    transaction_id=t.id,
                    field_changed="status",
                    old_value=str(t.status),
                    new_value="rejected",
                    changed_by=ACTOR,
                )
            )
            t.status = "rejected"
            t.review_reason = REASON

        if args.apply:
            session.commit()
            print(f"APPLIED: {len(rows)} orders rejected.")
        else:
            session.rollback()
            print(f"DRY-RUN: {len(rows)} orders would be rejected. Re-run with --apply.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
