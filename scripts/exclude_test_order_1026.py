"""Exclude BlackLine Shopify order #1026 and its refund as test data.

Issue #73 (Travis ruling 2026-08-31, Option C): order #1026 (buyer "Travis
Sparks", $32.76) and its matching refund were TEST orders, not real
transactions. They were auto-tagged PERSONAL_NON_DEDUCTIBLE inside the BlackLine
entity, which is wrong on both axes. The correct treatment is to exclude BOTH rows from
the books entirely, with no P&L impact on either entity.

Exclusion is a status flip to ``rejected`` (the register never DELETEs; see the
"never delete transactions" house rule), with a ``review_reason`` recording why
and an ``AuditEvent`` per field change. DRY-RUN by default; ``--apply`` commits.
Idempotent: an already-rejected row is skipped, so a second run reports zero
changes.

The refund is linked to its order the same way the adapter renders it: by the
parent order id carried in the refund's ``raw_data.order_id`` (Shopify nests
refunds inside the order), with the human-readable ``description`` "Shopify
Refund for #1026" as a fallback tie when the id is absent.

Companion prevention: ``src/adapters/shopify_adapter.py`` now rejects any order
Shopify itself flags as a test order (``order.test=true``) at ingest, so a future
test purchase never books (REQ-HYG-1026-002). This script is the one-off
correction for the rows that predate that guard (REQ-HYG-1026-001).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy.orm import Session  # noqa: E402

from src.db.connection import SessionLocal, init_db  # noqa: E402
from src.models.audit_event import AuditEvent  # noqa: E402
from src.models.enums import Entity, Source, TransactionStatus  # noqa: E402
from src.models.transaction import Transaction  # noqa: E402

ACTOR = "reject:test-order-1026-2026-08"
ORDER_NAME = "#1026"
REFUND_DESCRIPTION = f"Shopify Refund for {ORDER_NAME}"
REASON = (
    "Test order, not a real transaction (buyer Travis Sparks on BlackLine's own "
    "store, fully refunded). Excluded from the books per issue #73 "
    "(Travis ruling 2026-08-31, Option C)."
)

_EXCLUDED_STATUSES = (
    TransactionStatus.REJECTED.value,
    TransactionStatus.SPLIT_PARENT.value,
)


@dataclass
class Result:
    """Outcome of an exclusion pass."""

    orders: list[str] = field(default_factory=list)          # rejected order tx ids
    refunds: list[str] = field(default_factory=list)         # rejected refund tx ids
    skipped_splits: list[str] = field(default_factory=list)  # split rows left alone
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def total_changes(self) -> int:
        return len(self.orders) + len(self.refunds)


def find_order_ids(session: Session) -> set[str]:
    """Shopify numeric order ids for every #1026 order row, ANY status.

    Kept status-agnostic so a refund still links even when its order row was
    already excluded on a prior run.
    """
    rows = (
        session.query(Transaction)
        .filter(
            Transaction.entity == Entity.BLACKLINE.value,
            Transaction.source == Source.SHOPIFY.value,
        )
        .all()
    )
    ids: set[str] = set()
    for t in rows:
        raw = t.raw_data or {}
        if raw.get("name") == ORDER_NAME and raw.get("id") is not None:
            ids.add(str(raw["id"]))
    return ids


def find_order_rows(session: Session) -> list[Transaction]:
    """Non-excluded #1026 order rows (raw_data.name == '#1026')."""
    candidates = (
        session.query(Transaction)
        .filter(
            Transaction.entity == Entity.BLACKLINE.value,
            Transaction.source == Source.SHOPIFY.value,
            Transaction.status.notin_(_EXCLUDED_STATUSES),
        )
        .all()
    )
    return [t for t in candidates if (t.raw_data or {}).get("name") == ORDER_NAME]


def find_refund_rows(session: Session, order_ids: set[str]) -> list[Transaction]:
    """Non-excluded refund rows tied to #1026.

    A refund matches when its ``raw_data.order_id`` is one of the #1026 order ids
    OR its ``description`` is the "Shopify Refund for #1026" string the adapter
    renders (the fallback when an id is absent). The ``refund_`` source_id marker
    (set by ``_parse_refund``) scopes the search to refunds, never orders.
    """
    candidates = (
        session.query(Transaction)
        .filter(
            Transaction.entity == Entity.BLACKLINE.value,
            Transaction.source == Source.SHOPIFY.value,
            Transaction.status.notin_(_EXCLUDED_STATUSES),
        )
        .all()
    )
    matched: list[Transaction] = []
    for t in candidates:
        if not (t.source_id or "").lower().startswith("refund_"):
            continue
        raw = t.raw_data or {}
        by_order_id = raw.get("order_id") is not None and str(raw["order_id"]) in order_ids
        by_description = (t.description or "") == REFUND_DESCRIPTION
        if by_order_id or by_description:
            matched.append(t)
    return matched


def _reject(session: Session, tx: Transaction) -> None:
    """Flip one row to rejected, stamping review_reason + an audit event each."""
    old_status = str(tx.status)
    old_reason = tx.review_reason
    session.add(
        AuditEvent(
            transaction_id=tx.id,
            field_changed="status",
            old_value=old_status,
            new_value=TransactionStatus.REJECTED.value,
            changed_by=ACTOR,
        )
    )
    session.add(
        AuditEvent(
            transaction_id=tx.id,
            field_changed="review_reason",
            old_value=old_reason,
            new_value=REASON,
            changed_by=ACTOR,
        )
    )
    tx.status = TransactionStatus.REJECTED.value
    tx.review_reason = REASON


def _force_real_transaction(session: Session) -> None:
    """Emit an explicit ``BEGIN`` before the first ``SAVEPOINT``.

    pysqlite does not emit ``BEGIN`` for a ``SAVEPOINT`` statement, so a savepoint
    opened as the first statement of a transaction runs while SQLite is still in
    autocommit mode, so SQLite then treats it as the outermost savepoint and its
    ``RELEASE`` COMMITS. Without this, a dry run's ``session.rollback()`` would
    silently keep every write. (Mirrors scripts/remediate_plaid_mirrors.py.)
    """
    connection = session.connection()
    driver_connection = getattr(connection.connection, "driver_connection", None)
    if getattr(driver_connection, "in_transaction", False):
        return
    connection.exec_driver_sql("BEGIN")


def exclude(session: Session, *, apply: bool) -> Result:
    """Reject #1026's order + refund rows. Returns a plan/result either way.

    Per-row savepoints isolate a failure so the rest of the batch still applies;
    split rows (a split parent or a split child) are skipped and reported so the
    split-sum invariant is never broken by a half-rejection.
    """
    _force_real_transaction(session)
    result = Result()
    order_ids = find_order_ids(session)
    orders = find_order_rows(session)
    refunds = find_refund_rows(session, order_ids)

    for tx, bucket in [(o, result.orders) for o in orders] + [
        (r, result.refunds) for r in refunds
    ]:
        if tx.parent_id is not None or tx.status == TransactionStatus.SPLIT_PARENT.value:
            result.skipped_splits.append(tx.id)
            continue
        try:
            with session.begin_nested():
                _reject(session, tx)
            bucket.append(tx.id)
        except Exception as exc:  # noqa: BLE001 (per-row isolation)
            result.failed.append((tx.id, str(exc)))

    if apply:
        session.commit()
    else:
        session.rollback()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="Commit (default: DRY-RUN).")
    args = parser.parse_args(argv)

    init_db()
    session = SessionLocal()
    try:
        result = exclude(session, apply=args.apply)
    finally:
        session.close()

    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(
        f"exclude_test_order_1026 {mode}: "
        f"orders={len(result.orders)} refunds={len(result.refunds)} "
        f"skipped_splits={len(result.skipped_splits)} failed={len(result.failed)}"
    )
    for tx_id, err in result.failed:
        print(f"  FAILED {tx_id}: {err}")
    if not args.apply and result.total_changes:
        print(f"  {result.total_changes} row(s) would be rejected. Re-run with --apply.")
    # A per-row failure during --apply must surface as a non-zero exit so an
    # OnFailure hook can page (mirrors remediate_plaid_mirrors.py).
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
