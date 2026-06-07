"""Reclassify Shopify monthly PLATFORM/HOSTING fees as Supplies/ecommerce_platform.

The recurring "SHOPIFY* <digits>" card charge is BlackLine's website-hosting
expense, not sales income. The generic \\bshopify\\b vendor rule (SALES_INCOME)
mis-bucketed it; the sign-guard then parked it in OTHER_EXPENSE. Per the
system's taxonomy these belong in SUPPLIES with subcategory ecommerce_platform
("Shopify fees, WooCommerce") — same as Render/Vercel hosting.

Two actions (DRY-RUN by default; --apply to commit):
  1. Re-tag existing "SHOPIFY*" expense rows → SUPPLIES / ecommerce_platform,
     status=confirmed (user-directed), with an AuditEvent per field change.
  2. Ensure a VendorRule exists so future "SHOPIFY*" charges auto-classify
     (pattern shopify\\s*\\*, examples=41 to outrank the \\bshopify\\b income rule).
"""

from __future__ import annotations

import argparse

from src.models.audit_event import AuditEvent
from src.models.transaction import Transaction
from src.models.vendor_rule import VendorRule

try:
    from src.db.connection import get_session
except ImportError:  # pragma: no cover
    from src.db.session import get_session  # type: ignore

ACTOR = "reclassify:shopify-hosting-2026-06"
RULE_PATTERN = r"shopify\s*\*"


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
        # ── 1. Re-tag existing SHOPIFY* hosting charges ──
        candidates = (
            session.query(Transaction)
            .filter(
                Transaction.amount < 0,
                Transaction.status.notin_(("rejected", "split_parent")),
            )
            .all()
        )
        rows = []
        for t in candidates:
            name = str((t.raw_data or {}).get("name") or "").upper()
            is_shopify_fee = "SHOPIFY*" in name.replace(" ", "") or "SHOPIFY *" in name
            already_correct = (
                t.tax_category == "SUPPLIES" and t.tax_subcategory == "ecommerce_platform"
            )
            if is_shopify_fee and not already_correct:
                rows.append(t)

        print(f"Shopify hosting charges to re-tag: {len(rows)}")
        for t in rows:
            name = str((t.raw_data or {}).get("name") or "")[:30]
            print(f"  {t.date} {t.entity:9} {float(t.amount):>8.2f} "
                  f"{t.tax_category}/{t.tax_subcategory} -> SUPPLIES/ecommerce_platform  [{name}]")
            _log(session, t.id, "tax_category", t.tax_category, "SUPPLIES")
            _log(session, t.id, "tax_subcategory", t.tax_subcategory, "ecommerce_platform")
            if t.status != "confirmed":
                _log(session, t.id, "status", t.status, "confirmed")
            t.tax_category = "SUPPLIES"
            t.tax_subcategory = "ecommerce_platform"
            t.direction = "expense"
            t.status = "confirmed"
            t.review_reason = None

        # ── 2. Ensure the future-proofing vendor rule exists ──
        existing = (
            session.query(VendorRule)
            .filter(
                VendorRule.vendor_pattern == RULE_PATTERN,
                VendorRule.entity == "blackline",
            )
            .first()
        )
        if existing is None:
            print(f"Vendor rule {RULE_PATTERN!r} (blackline) -> SUPPLIES/ecommerce_platform: CREATE")
            session.add(
                VendorRule(
                    vendor_pattern=RULE_PATTERN,
                    entity="blackline",
                    tax_category="SUPPLIES",
                    tax_subcategory="ecommerce_platform",
                    direction="expense",
                    deductible_pct=1.0,
                    confidence=0.97,
                    source="human",
                    examples=41,
                )
            )
        else:
            print(f"Vendor rule {RULE_PATTERN!r} (blackline): already present")

        if args.apply:
            session.commit()
            print(f"\nAPPLIED: {len(rows)} rows re-tagged + vendor rule ensured.")
        else:
            session.rollback()
            print(f"\nDRY-RUN: {len(rows)} rows would be re-tagged. Re-run with --apply.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
