"""Tests for scripts/exclude_test_order_1026.py.

REQ-ID: REQ-HYG-1026-001  Order #1026 (raw_data.name == "#1026") and its matching
                          refund are rejected with a review_reason citing issue
                          #73; the rows are NEVER deleted (status flip only).
REQ-ID: REQ-HYG-1026-003  DRY-RUN by default, an AuditEvent per field change,
                          per-row savepoints, idempotent (a second run reports
                          zero changes), and the rejected rows drop out of P&L.

The refund is linked to its order by raw_data.order_id (Shopify nests refunds
inside the order), with the "Shopify Refund for #1026" description as a fallback.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from scripts.exclude_test_order_1026 import (
    ORDER_NAME,
    REASON,
    exclude,
    find_order_rows,
    find_refund_rows,
)
from src.models.audit_event import AuditEvent
from src.models.base import Base
from src.models.enums import (
    Direction,
    Entity,
    Source,
    TaxCategory,
    TransactionStatus,
)
from src.models.transaction import Transaction
from src.reports.pl_engine import compute_entity_pl

_ORDER_ID = 5001026000000


@pytest.fixture
def db() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _):  # type: ignore[no-untyped-def]
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _tx(
    db: Session,
    *,
    source_id: str,
    raw_data: dict,
    description: str,
    amount: Decimal,
    direction: str,
    tax_category: str,
    date: str = "2026-02-01",
    status: str = TransactionStatus.NEEDS_REVIEW.value,
    parent_id: str | None = None,
) -> Transaction:
    tx = Transaction(
        id=str(uuid.uuid4()),
        source=Source.SHOPIFY.value,
        source_id=source_id,
        source_hash=str(uuid.uuid4()),
        date=date,
        description=description,
        amount=amount,
        currency="USD",
        entity=Entity.BLACKLINE.value,
        direction=direction,
        tax_category=tax_category,
        status=status,
        confidence=0.8,
        raw_data=raw_data,
        parent_id=parent_id,
    )
    db.add(tx)
    db.commit()
    return tx


def _order_1026(db: Session, **kw) -> Transaction:
    return _tx(
        db,
        source_id=f"order_{_ORDER_ID}",
        raw_data={"id": _ORDER_ID, "name": ORDER_NAME,
                  "customer": {"first_name": "Travis", "last_name": "Sparks"}},
        description=f"Shopify Order {ORDER_NAME} (Travis Sparks)",
        amount=Decimal("32.76"),
        direction=Direction.INCOME.value,
        tax_category=TaxCategory.PERSONAL_NON_DEDUCTIBLE.value,
        **kw,
    )


def _refund_1026(db: Session, *, with_order_id: bool = True, **kw) -> Transaction:
    raw = {"id": 9009}
    if with_order_id:
        raw["order_id"] = _ORDER_ID
    return _tx(
        db,
        source_id="refund_9009",
        raw_data=raw,
        description=f"Shopify Refund for {ORDER_NAME}",
        amount=Decimal("-32.76"),
        direction=Direction.EXPENSE.value,
        tax_category=TaxCategory.PERSONAL_NON_DEDUCTIBLE.value,
        **kw,
    )


# ── REQ-HYG-1026-001: order + refund rejected, never deleted ─────────────────


def test_order_and_refund_rejected_with_reason(db: Session) -> None:
    order = _order_1026(db)
    refund = _refund_1026(db)

    result = exclude(db, apply=True)

    assert len(result.orders) == 1
    assert len(result.refunds) == 1
    db.refresh(order)
    db.refresh(refund)
    assert order.status == TransactionStatus.REJECTED.value
    assert order.review_reason == REASON
    assert refund.status == TransactionStatus.REJECTED.value
    assert refund.review_reason == REASON


def test_rows_are_never_deleted(db: Session) -> None:
    _order_1026(db)
    _refund_1026(db)
    exclude(db, apply=True)
    assert db.query(Transaction).count() == 2


def test_refund_linked_by_description_when_order_id_absent(db: Session) -> None:
    """A refund whose raw_data has no order_id still matches on the description."""
    _order_1026(db)
    refund = _refund_1026(db, with_order_id=False)

    result = exclude(db, apply=True)

    assert refund.id in result.refunds
    db.refresh(refund)
    assert refund.status == TransactionStatus.REJECTED.value


def test_refund_linked_by_order_id_when_description_differs(db: Session) -> None:
    """Order id is the primary tie; matches even if the description was edited."""
    _order_1026(db)
    refund = _refund_1026(db)
    refund.description = "hand-edited note"
    db.commit()

    result = exclude(db, apply=True)

    assert refund.id in result.refunds


def test_refund_excluded_even_when_order_row_already_rejected(db: Session) -> None:
    """find_order_ids is status-agnostic, so a refund still links after the order
    was excluded on a prior run."""
    _order_1026(db, status=TransactionStatus.REJECTED.value)
    refund = _refund_1026(db)

    result = exclude(db, apply=True)

    assert result.orders == []            # order already excluded
    assert refund.id in result.refunds


def test_unrelated_blackline_order_untouched(db: Session) -> None:
    keeper = _tx(
        db,
        source_id="order_5001042000000",
        raw_data={"id": 5001042000000, "name": "#1042"},
        description="Shopify Order #1042 (Jane Doe)",
        amount=Decimal("85.00"),
        direction=Direction.INCOME.value,
        tax_category=TaxCategory.SALES_INCOME.value,
    )
    _order_1026(db)

    exclude(db, apply=True)

    db.refresh(keeper)
    assert keeper.status == TransactionStatus.NEEDS_REVIEW.value


def test_split_rows_are_skipped_and_reported(db: Session) -> None:
    """A split child of #1026 must not be half-rejected (breaks the sum
    invariant); it is skipped and reported."""
    parent = _order_1026(db, status=TransactionStatus.SPLIT_PARENT.value)
    child = _tx(
        db,
        source_id=f"order_{_ORDER_ID}_split1",
        raw_data={"id": _ORDER_ID, "name": ORDER_NAME},
        description=f"Shopify Order {ORDER_NAME} split",
        amount=Decimal("32.76"),
        direction=Direction.INCOME.value,
        tax_category=TaxCategory.SALES_INCOME.value,
        parent_id=parent.id,
    )

    result = exclude(db, apply=True)

    # The split_parent is excluded by the status filter; the child is caught as a
    # candidate (same raw_data.name) but skipped because it has a parent_id.
    assert child.id in result.skipped_splits
    assert result.orders == []
    db.refresh(child)
    assert child.status == TransactionStatus.NEEDS_REVIEW.value


# ── REQ-HYG-1026-003: dry-run, audit trail, idempotency, P&L ─────────────────


def test_dry_run_writes_nothing(db: Session) -> None:
    order = _order_1026(db)
    refund = _refund_1026(db)

    result = exclude(db, apply=False)

    # The plan is reported in full...
    assert len(result.orders) == 1
    assert len(result.refunds) == 1
    # ...but nothing survives the rollback, audit rows included.
    db.expire_all()
    assert db.get(Transaction, order.id).status == TransactionStatus.NEEDS_REVIEW.value
    assert db.get(Transaction, refund.id).status == TransactionStatus.NEEDS_REVIEW.value
    assert db.query(AuditEvent).count() == 0


def test_audit_event_per_field_change(db: Session) -> None:
    order = _order_1026(db)
    _refund_1026(db)

    exclude(db, apply=True)

    fields = sorted(
        e.field_changed
        for e in db.query(AuditEvent).filter_by(transaction_id=order.id).all()
    )
    assert fields == ["review_reason", "status"]
    assert all(
        e.changed_by == "reject:test-order-1026-2026-08"
        for e in db.query(AuditEvent).all()
    )


def test_second_run_is_a_no_op(db: Session) -> None:
    _order_1026(db)
    _refund_1026(db)

    first = exclude(db, apply=True)
    assert first.total_changes == 2

    audit_count = db.query(AuditEvent).count()
    second = exclude(db, apply=True)

    assert second.total_changes == 0
    assert find_order_rows(db) == []
    assert find_refund_rows(db, {str(_ORDER_ID)}) == []
    assert db.query(AuditEvent).count() == audit_count


def test_one_bad_row_does_not_halt_the_batch(db: Session) -> None:
    _order_1026(db)
    refund = _refund_1026(db)

    real_flush = Session.flush

    def _flush(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if refund in self.dirty:
            raise RuntimeError("simulated write failure")
        return real_flush(self, *args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Session, "flush", _flush)
        result = exclude(db, apply=True)

    assert len(result.orders) == 1
    assert len(result.failed) == 1
    db.expire_all()
    order = find_order_rows(db)
    assert order == []  # the order still committed


def test_rejected_rows_drop_out_of_pl(db: Session) -> None:
    """The point of the correction: after exclusion, #1026 contributes nothing to
    BlackLine P&L (no revenue from the order, no expense from the refund)."""
    _order_1026(db)
    _refund_1026(db)

    before = compute_entity_pl(db, "2026-02-01", "2026-02-28", Entity.BLACKLINE.value)
    assert before.revenue == Decimal("32.76")
    assert before.expenses == Decimal("32.76")

    exclude(db, apply=True)

    after = compute_entity_pl(db, "2026-02-01", "2026-02-28", Entity.BLACKLINE.value)
    assert after.revenue == Decimal("0")
    assert after.expenses == Decimal("0")
