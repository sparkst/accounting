"""Tests for scripts/backfill_chase6380_payment_method.py — REQ-PC-B6.

One-time audited backfill of blank ``payment_method`` on Plaid register rows
belonging to the personal Chase …6380 feed. DRY-RUN default, AuditEvent per
change, idempotent second run, derived (never hardcoded) row selection, and a
refuse-to-guess account resolution.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

import src.models.plaid as _plaid  # noqa: F401 — registers PlaidItem for FK resolution
from scripts.backfill_chase6380_payment_method import (
    ACTOR,
    CHASE_6380_PAYMENT_METHOD,
    backfill,
)
from src.models.audit_event import AuditEvent
from src.models.base import Base
from src.models.brokerage import Account
from src.models.enums import Entity, Source
from src.models.transaction import Transaction


@pytest.fixture
def db() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _):  # type: ignore[no-untyped-def]
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


_PLAID_ACCT = "rJLQP5OJrealchasefeed6380"


def _make_account(db: Session, *, payment_method: str | None = CHASE_6380_PAYMENT_METHOD) -> Account:
    acct = Account(
        broker="chase",
        account_number="****6380",
        account_name="Chase Freedom 6380",
        account_type="credit_card",
        entity=Entity.PERSONAL.value,
        payment_method=payment_method,
        plaid_account_id=_PLAID_ACCT,
    )
    db.add(acct)
    db.commit()
    return acct


def _make_tx(
    db: Session,
    *,
    payment_method: str | None = None,
    account_id: str = _PLAID_ACCT,
    source: str = Source.PLAID.value,
) -> Transaction:
    tx = Transaction(
        source=source,
        source_id=f"tx_{uuid.uuid4().hex[:10]}",
        source_hash=uuid.uuid4().hex,
        date="2026-07-01",
        description="COFFEE SHOP",
        amount=Decimal("-4.50"),
        currency="USD",
        entity=Entity.PERSONAL.value,
        payment_method=payment_method,
        status="confirmed",
        confidence=1.0,
        raw_data={"account_id": account_id},
    )
    db.add(tx)
    db.commit()
    return tx


def test_apply_stamps_blank_rows_with_audit(db: Session) -> None:
    _make_account(db)
    blank1 = _make_tx(db, payment_method=None)
    blank2 = _make_tx(db, payment_method="")
    already = _make_tx(db, payment_method=CHASE_6380_PAYMENT_METHOD)
    other_feed = _make_tx(db, payment_method=None, account_id="someOtherPlaidAcct")
    non_plaid = _make_tx(db, payment_method=None, source="bank_csv")

    result = backfill(db, apply=True)

    assert result.resolution_error is None
    assert result.total_changes == 2
    for tx in (blank1, blank2):
        db.refresh(tx)
        assert tx.payment_method == CHASE_6380_PAYMENT_METHOD
        audit = db.query(AuditEvent).filter_by(
            transaction_id=tx.id, field_changed="payment_method"
        ).one()
        assert audit.new_value == CHASE_6380_PAYMENT_METHOD
        assert audit.changed_by == ACTOR
    # Untouched rows.
    for tx in (other_feed, non_plaid):
        db.refresh(tx)
        assert tx.payment_method is None
    db.refresh(already)
    assert db.query(AuditEvent).filter_by(transaction_id=already.id).count() == 0


def test_dry_run_default_rolls_back(db: Session) -> None:
    _make_account(db)
    blank = _make_tx(db, payment_method=None)

    result = backfill(db)  # apply defaults False

    assert result.total_changes == 1  # reported…
    db.refresh(blank)
    assert blank.payment_method is None  # …but rolled back
    assert db.query(AuditEvent).count() == 0


def test_second_run_reports_zero_changes(db: Session) -> None:
    _make_account(db)
    _make_tx(db, payment_method=None)

    assert backfill(db, apply=True).total_changes == 1
    assert backfill(db, apply=True).total_changes == 0


def test_refuses_when_account_label_missing(db: Session) -> None:
    """The Account must ALREADY carry payment_method='chase_6380' (from the
    remediation script) — the backfill never guesses which account owns the
    label."""
    _make_account(db, payment_method=None)
    _make_tx(db, payment_method=None)

    result = backfill(db, apply=True)
    assert result.resolution_error is not None
    assert result.total_changes == 0
    assert db.query(AuditEvent).count() == 0


def test_refuses_when_account_has_no_plaid_account_id(db: Session) -> None:
    acct = _make_account(db)
    acct.plaid_account_id = None
    db.commit()
    _make_tx(db, payment_method=None)

    result = backfill(db, apply=True)
    assert result.resolution_error is not None
    assert result.total_changes == 0
