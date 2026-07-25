"""Tests for scripts/remediate_bo_classification.py.

REQ-ID: REQ-FIX-BO-001  Inbound Plaid processor-payout legs are reclassified to
                        direction=transfer / tax_category=NULL /
                        deductible_pct=0.0 with the amount untouched — but ONLY
                        when the revenue they settle is already in the register.
                        A payout with no backing revenue is reported as blocked,
                        never flipped (that would erase real income).
REQ-ID: REQ-FIX-BO-002  Gmail receipts carrying an income tax_category are
                        rejected with review_reason "superseded_by_plaid" when a
                        non-rejected Plaid twin exists; a receipt with no twin is
                        reported for manual re-categorisation, never rejected.
REQ-ID: REQ-FIX-BO-003  DRY-RUN by default (and it really does not write), an
                        AuditEvent per field change, per-row savepoints, splits
                        skipped, and idempotent (a second run reports zero).
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

import scripts.remediate_bo_classification as remediation
import src.models.plaid as _plaid  # noqa: F401  # registers PlaidItem for FK resolution
from scripts.remediate_bo_classification import (
    GMAIL_SUPERSEDE_REASON,
    find_payout_rows,
    payout_backing_source,
    remediate,
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

SHOPIFY_PAYOUT_DESC = (
    "ORIG CO NAME:Shopify ORIG ID:4270465600 DESC DATE: CO ENTRY DESCR:Shopify"
)
STRIPE_PAYOUT_DESC = "ORIG CO NAME:Stripe Payment-S ORIG ID:1800948598 DESC DATE:"


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
    amount: Decimal = Decimal("86.47"),
    date: str = "2026-04-13",
    description: str = SHOPIFY_PAYOUT_DESC,
    direction: str = Direction.INCOME.value,
    tax_category: str | None = TaxCategory.SALES_INCOME.value,
    status: str = TransactionStatus.AUTO_CLASSIFIED.value,
    source: str = Source.PLAID.value,
    entity: str = Entity.BLACKLINE.value,
    parent_id: str | None = None,
    deductible_pct: float = 1.0,
    raw_data: dict | None = None,
) -> Transaction:
    tx = Transaction(
        id=str(uuid.uuid4()),
        source=source,
        source_id=str(uuid.uuid4()),
        source_hash=str(uuid.uuid4()),
        date=date,
        description=description,
        amount=amount,
        currency="USD",
        entity=entity,
        direction=direction,
        tax_category=tax_category,
        deductible_pct=deductible_pct,
        status=status,
        confidence=0.9,
        raw_data=raw_data if raw_data is not None else {"name": description},
        parent_id=parent_id,
    )
    db.add(tx)
    db.commit()
    return tx


def _backing_order(db: Session, *, date: str = "2026-04-05") -> Transaction:
    """The Shopify order a payout settles — order #1023 in production."""
    return _tx(
        db,
        source=Source.SHOPIFY.value,
        date=date,
        description="Shopify Order #1023 — Scott Kizer",
        amount=Decimal("89.36"),
    )


def _gmail_receipt(
    db: Session,
    *,
    amount: Decimal = Decimal("-24.27"),
    date: str = "2026-06-18",
) -> Transaction:
    """An ElevenLabs receipt misclassified as SUBSCRIPTION_INCOME."""
    return _tx(
        db,
        source=Source.GMAIL_N8N.value,
        entity=Entity.SPARKRY.value,
        date=date,
        description="[object Object]",
        amount=amount,
        direction=Direction.INCOME.value,
        tax_category=TaxCategory.SUBSCRIPTION_INCOME.value,
        raw_data={"subject": "Your receipt from Eleven Labs Inc."},
    )


def _plaid_twin(
    db: Session,
    *,
    amount: Decimal = Decimal("-24.27"),
    date: str = "2026-06-18",
) -> Transaction:
    """The card charge Plaid already booked correctly as an expense."""
    return _tx(
        db,
        source=Source.PLAID.value,
        entity=Entity.SPARKRY.value,
        date=date,
        description="Elevenlabs.io",
        amount=amount,
        direction=Direction.EXPENSE.value,
        tax_category=TaxCategory.OFFICE_EXPENSE.value,
    )


# ── REQ-FIX-BO-001: payout legs ──────────────────────────────────────────────


def test_payout_with_backing_revenue_becomes_transfer(db: Session) -> None:
    _backing_order(db)
    payout = _tx(db)

    result = remediate(db, apply=True)

    assert len(result.payouts) == 1
    db.refresh(payout)
    assert payout.direction == Direction.TRANSFER.value
    assert payout.tax_category is None
    assert payout.deductible_pct == 0.0


def test_payout_amount_is_never_touched(db: Session) -> None:
    _backing_order(db)
    payout = _tx(db)

    remediate(db, apply=True)

    db.refresh(payout)
    assert payout.amount == Decimal("86.47")


def test_payout_without_backing_revenue_is_blocked_not_flipped(db: Session) -> None:
    """The 2026-07-20 case: Shopify ingestion stalled, so no orders back it."""
    payout = _tx(db, date="2026-07-20", amount=Decimal("2711.39"))

    result = remediate(db, apply=True)

    assert result.payouts == []
    assert len(result.blocked_payouts) == 1
    db.refresh(payout)
    assert payout.direction == Direction.INCOME.value
    assert payout.tax_category == TaxCategory.SALES_INCOME.value


def test_backing_revenue_outside_lookback_window_does_not_unblock(db: Session) -> None:
    _backing_order(db, date="2026-02-01")  # >14d before the payout
    _tx(db)

    result = remediate(db, apply=True)

    assert result.payouts == []
    assert len(result.blocked_payouts) == 1


def test_backing_revenue_must_be_the_payouts_own_processor(db: Session) -> None:
    """A Shopify order must not unblock a STRIPE payout."""
    _backing_order(db, date="2026-07-02")
    _tx(
        db,
        date="2026-07-06",
        amount=Decimal("502.19"),
        description=STRIPE_PAYOUT_DESC,
        entity=Entity.BLACKLINE.value,
    )

    result = remediate(db, apply=True)

    assert result.payouts == []
    assert len(result.blocked_payouts) == 1


def test_rejected_backing_revenue_does_not_count(db: Session) -> None:
    _backing_order(db).status = TransactionStatus.REJECTED.value
    db.commit()
    _tx(db)

    result = remediate(db, apply=True)

    assert result.payouts == []
    assert len(result.blocked_payouts) == 1


def test_shopify_fee_debit_is_not_a_payout(db: Session) -> None:
    """A bare "Shopify" expense (-$42.65 on 2026-06-02) is a real deduction."""
    _backing_order(db, date="2026-06-01")
    fee = _tx(
        db,
        date="2026-06-02",
        description="Shopify",
        amount=Decimal("-42.65"),
        direction=Direction.EXPENSE.value,
        tax_category=TaxCategory.SUPPLIES.value,
    )

    result = remediate(db, apply=True)

    assert result.payouts == []
    db.refresh(fee)
    assert fee.direction == Direction.EXPENSE.value
    assert fee.tax_category == TaxCategory.SUPPLIES.value


def test_outbound_row_with_payout_descriptor_is_out_of_scope(db: Session) -> None:
    """Negative amount = money out; never reclassify it as a settlement."""
    _backing_order(db)
    debit = _tx(db, amount=Decimal("-86.47"), direction=Direction.EXPENSE.value)

    result = remediate(db, apply=True)

    assert result.payouts == []
    db.refresh(debit)
    assert debit.direction == Direction.EXPENSE.value


def test_legacy_shopifypmt_descriptor_still_matches(db: Session) -> None:
    tx = _tx(db, description="ORIG CO NAME:SHOPIFY ORIG ID:SHOPIFYPMT DESC DATE:260309")
    assert payout_backing_source(tx) == Source.SHOPIFY.value


def test_payout_matched_on_raw_name_when_description_edited(db: Session) -> None:
    _backing_order(db)
    payout = _tx(
        db,
        description="Shopify deposit (renamed by hand)",
        raw_data={"name": SHOPIFY_PAYOUT_DESC},
    )

    result = remediate(db, apply=True)

    assert len(result.payouts) == 1
    db.refresh(payout)
    assert payout.direction == Direction.TRANSFER.value


def test_split_payout_is_skipped_not_reclassified(db: Session) -> None:
    _backing_order(db)
    parent = _tx(db, status=TransactionStatus.SPLIT_PARENT.value)
    child = _tx(db, parent_id=parent.id)

    result = remediate(db, apply=True)

    assert result.payouts == []
    assert len(result.skipped_splits) == 2
    db.refresh(child)
    assert child.direction == Direction.INCOME.value


# ── REQ-FIX-BO-002: Gmail receipts superseded by Plaid ───────────────────────


def test_gmail_income_receipt_with_twin_is_rejected(db: Session) -> None:
    receipt = _gmail_receipt(db)
    _plaid_twin(db)

    result = remediate(db, apply=True)

    assert len(result.gmail_supersedes) == 1
    db.refresh(receipt)
    assert receipt.status == TransactionStatus.REJECTED.value
    assert receipt.review_reason == GMAIL_SUPERSEDE_REASON


def test_gmail_receipt_is_never_deleted(db: Session) -> None:
    receipt = _gmail_receipt(db)
    _plaid_twin(db)

    remediate(db, apply=True)

    assert db.query(Transaction).filter(Transaction.id == receipt.id).one() is not None


def test_plaid_twin_is_left_alone(db: Session) -> None:
    _gmail_receipt(db)
    twin = _plaid_twin(db)

    remediate(db, apply=True)

    db.refresh(twin)
    assert twin.status == TransactionStatus.AUTO_CLASSIFIED.value
    assert twin.tax_category == TaxCategory.OFFICE_EXPENSE.value


def test_twin_matched_within_date_window(db: Session) -> None:
    _gmail_receipt(db, date="2026-06-18")
    _plaid_twin(db, date="2026-06-20")

    result = remediate(db, apply=True)

    assert len(result.gmail_supersedes) == 1


def test_gmail_receipt_without_twin_is_reported_not_rejected(db: Session) -> None:
    receipt = _gmail_receipt(db)

    result = remediate(db, apply=True)

    assert result.gmail_supersedes == []
    assert len(result.unmatched_gmail) == 1
    db.refresh(receipt)
    assert receipt.status == TransactionStatus.AUTO_CLASSIFIED.value


def test_twin_with_different_amount_does_not_match(db: Session) -> None:
    _gmail_receipt(db, amount=Decimal("-24.27"))
    _plaid_twin(db, amount=Decimal("-24.28"))

    result = remediate(db, apply=True)

    assert result.gmail_supersedes == []
    assert len(result.unmatched_gmail) == 1


def test_positive_gmail_row_is_out_of_scope(db: Session) -> None:
    """Real Gmail-sourced income (if any) must not be swept up."""
    income = _tx(
        db,
        source=Source.GMAIL_N8N.value,
        entity=Entity.SPARKRY.value,
        amount=Decimal("500.00"),
        direction=Direction.INCOME.value,
        tax_category=TaxCategory.CONSULTING_INCOME.value,
        description="Client payment",
    )

    result = remediate(db, apply=True)

    assert result.gmail_supersedes == []
    db.refresh(income)
    assert income.status == TransactionStatus.AUTO_CLASSIFIED.value


def test_gmail_expense_row_is_out_of_scope(db: Session) -> None:
    """Correctly-categorised Gmail expenses are untouched."""
    expense = _tx(
        db,
        source=Source.GMAIL_N8N.value,
        entity=Entity.SPARKRY.value,
        amount=Decimal("-24.27"),
        direction=Direction.EXPENSE.value,
        tax_category=TaxCategory.OFFICE_EXPENSE.value,
    )
    _plaid_twin(db)

    result = remediate(db, apply=True)

    assert result.gmail_supersedes == []
    db.refresh(expense)
    assert expense.status == TransactionStatus.AUTO_CLASSIFIED.value


# ── REQ-FIX-BO-003: invariants ───────────────────────────────────────────────


def test_dry_run_writes_nothing(db: Session) -> None:
    """The pysqlite savepoint-autocommit trap: a dry run must not commit."""
    _backing_order(db)
    payout = _tx(db)
    receipt = _gmail_receipt(db)
    _plaid_twin(db)

    result = remediate(db, apply=False)

    assert result.total_changes == 2  # planned...
    db.refresh(payout)
    db.refresh(receipt)
    assert payout.direction == Direction.INCOME.value  # ...but not written
    assert payout.tax_category == TaxCategory.SALES_INCOME.value
    assert receipt.status == TransactionStatus.AUTO_CLASSIFIED.value
    assert db.query(AuditEvent).count() == 0


def test_audit_event_written_per_field_change(db: Session) -> None:
    _backing_order(db)
    payout = _tx(db)

    remediate(db, apply=True)

    events = db.query(AuditEvent).filter(AuditEvent.transaction_id == payout.id).all()
    changed = {event.field_changed for event in events}
    assert changed == {"direction", "tax_category", "deductible_pct"}
    assert all(event.changed_by.startswith("remediation:bo-classification") for event in events)


def test_second_run_is_idempotent(db: Session) -> None:
    _backing_order(db)
    _tx(db)
    _gmail_receipt(db)
    _plaid_twin(db)

    first = remediate(db, apply=True)
    second = remediate(db, apply=True)

    assert first.total_changes == 2
    assert second.total_changes == 0


def test_already_rejected_rows_are_out_of_scope(db: Session) -> None:
    _backing_order(db)
    _tx(db, status=TransactionStatus.REJECTED.value)

    result = remediate(db, apply=True)

    assert result.payouts == []
    assert find_payout_rows(db) == []


def test_one_bad_row_does_not_halt_the_batch(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-row savepoint isolation: a failing row is recorded, others proceed.

    The failure is injected at ``_audit`` so it raises INSIDE the row's
    savepoint — the batch must record it and carry on to the next row.
    """
    _backing_order(db)
    good = _tx(db, date="2026-04-12")
    doomed = _tx(db, date="2026-04-13", amount=Decimal("86.47"))

    real_audit = remediation._audit

    def _exploding_audit(session: Session, tx: Transaction, **kwargs: object) -> None:
        if tx.id == doomed.id:
            raise RuntimeError("injected failure")
        real_audit(session, tx, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(remediation, "_audit", _exploding_audit)

    result = remediate(db, apply=True)

    assert len(result.failed) == 1
    assert len(result.payouts) == 1
    db.refresh(good)
    db.refresh(doomed)
    assert good.direction == Direction.TRANSFER.value
    assert doomed.direction == Direction.INCOME.value
