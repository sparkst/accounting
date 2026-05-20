#!/usr/bin/env python3
"""Auto-confirm high-confidence auto-classified transactions.

Confirms transactions where:
- status = auto_classified
- confidence >= threshold (default 0.90)
- entity and tax_category are both set

Creates AuditEvent for each confirmation with changed_by='auto_confirm_script'.
Prints a summary report to stdout.

Usage:
    python3 scripts/auto-confirm-high-confidence.py              # dry run (default)
    python3 scripts/auto-confirm-high-confidence.py --commit     # actually confirm
    python3 scripts/auto-confirm-high-confidence.py --threshold 0.95  # higher bar
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from sqlalchemy import func

from src.db.connection import SessionLocal, init_db
from src.models.audit_event import AuditEvent
from src.models.enums import TransactionStatus
from src.models.transaction import Transaction


def main() -> None:
    parser = argparse.ArgumentParser(description="Auto-confirm high-confidence transactions")
    parser.add_argument("--threshold", type=float, default=0.90, help="Minimum confidence (default: 0.90)")
    parser.add_argument("--commit", action="store_true", help="Actually confirm (default: dry run)")
    args = parser.parse_args()

    init_db()
    session = SessionLocal()

    try:
        # Find candidates
        candidates = (
            session.query(Transaction)
            .filter(
                Transaction.status == TransactionStatus.AUTO_CLASSIFIED.value,
                Transaction.confidence >= args.threshold,
                Transaction.entity.isnot(None),
                Transaction.tax_category.isnot(None),
            )
            .order_by(Transaction.date.desc())
            .all()
        )

        print(f"{'=' * 60}")
        print("Auto-Confirm High-Confidence Transactions")
        print(f"{'=' * 60}")
        print(f"Threshold:    >= {args.threshold}")
        print(f"Mode:         {'COMMIT' if args.commit else 'DRY RUN'}")
        print(f"Candidates:   {len(candidates)}")
        print()

        if not candidates:
            print("No transactions to confirm.")
            return

        # Group by entity for reporting
        by_entity: dict[str, list[Transaction]] = {}
        for tx in candidates:
            by_entity.setdefault(tx.entity, []).append(tx)

        for entity, txns in sorted(by_entity.items()):
            print(f"  {entity}: {len(txns)} transactions")

        print()

        # Confirm each transaction
        confirmed_count = 0
        for tx in candidates:
            if args.commit:
                old_status = tx.status
                tx.status = TransactionStatus.CONFIRMED.value
                tx.confirmed_by = "auto"
                tx.updated_at = datetime.now(UTC)

                # Audit trail
                session.add(AuditEvent(
                    transaction_id=tx.id,
                    field_changed="status",
                    old_value=old_status,
                    new_value=TransactionStatus.CONFIRMED.value,
                    changed_by="auto_confirm_script",
                ))

            confirmed_count += 1

        if args.commit:
            session.commit()
            action = "CONFIRMED"
        else:
            action = "WOULD CONFIRM (dry run)"

        print(f"{action}: {confirmed_count} transactions")
        print()

        # Remaining auto-classified
        remaining = session.query(func.count()).filter(
            Transaction.status == TransactionStatus.AUTO_CLASSIFIED.value,
        ).scalar()
        print(f"Remaining auto-classified: {remaining}")
        print(f"These need manual review by Travis (confidence < {args.threshold})")

        # Calculate new readiness
        total = session.query(func.count()).filter(
            Transaction.status != TransactionStatus.REJECTED.value,
        ).scalar()
        confirmed_total = session.query(func.count()).filter(
            Transaction.status == TransactionStatus.CONFIRMED.value,
        ).scalar()
        pct = round(confirmed_total / total * 100) if total > 0 else 0
        print(f"\nOverall readiness: {pct}% ({confirmed_total}/{total} confirmed)")

    finally:
        session.close()


if __name__ == "__main__":
    main()
