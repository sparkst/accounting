"""Tests for the deterministic Plaid-vs-register tie-out (REQ-MCA-001, spec §1.2)."""

from __future__ import annotations

import itertools
from collections.abc import Generator
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import src.db.connection  # noqa: F401  (register every ORM table on Base)
from src.close.reconcile import prior_month, reconcile
from src.models.base import Base
from src.models.brokerage import Account
from src.models.enums import Direction, Entity, TaxCategory, TransactionStatus
from src.models.ingestion_log import IngestionLog
from src.models.plaid import PlaidAccountBalanceSnapshot, PlaidItem
from src.models.transaction import Transaction

_ENGINE = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
Base.metadata.create_all(_ENGINE)
_Session = sessionmaker(bind=_ENGINE)
_counter = itertools.count()

_TODAY = date(2026, 7, 7)


@pytest.fixture()
def session() -> Generator[Session, None, None]:
    s = _Session()
    yield s
    s.rollback()
    for model in (Transaction, PlaidAccountBalanceSnapshot, IngestionLog, Account, PlaidItem):
        s.query(model).delete()
    s.commit()
    s.close()


def _item(session: Session, name: str = "Chase") -> PlaidItem:
    item = PlaidItem(
        item_id=f"item-{next(_counter)}",
        institution_id="ins_1",
        institution_name=name,
        access_token_encrypted="enc",
        status="active",
    )
    session.add(item)
    session.flush()
    return item


def _account(
    session: Session,
    item: PlaidItem,
    *,
    payment_method: str,
    broker: str = "chase",
    account_type: str = "checking",
) -> Account:
    acct = Account(
        broker=broker,
        account_number=f"acct-{next(_counter)}",
        account_name=f"{broker}-{payment_method}",
        account_type=account_type,
        entity=Entity.SPARKRY.value,
        plaid_item_id=item.id,
        plaid_account_id=f"pa-{next(_counter)}",
        payment_method=payment_method,
    )
    session.add(acct)
    session.flush()
    return acct


def _snap(session: Session, acct: Account, d: date, balance: str, ptype: str = "depository") -> None:
    session.add(
        PlaidAccountBalanceSnapshot(
            account_id=acct.id,
            snapshot_date=d,
            plaid_account_type=ptype,
            current_balance=Decimal(balance),
            raw_data={},
        )
    )
    session.flush()


def _tx(
    session: Session,
    *,
    payment_method: str,
    date_: str,
    amount: str,
    status: str = TransactionStatus.CONFIRMED.value,
    pending: bool = False,
) -> Transaction:
    tx = Transaction(
        source="plaid",
        source_hash=f"h-{next(_counter)}",
        date=date_,
        description="Vendor",
        amount=Decimal(amount),
        entity=Entity.SPARKRY.value,
        tax_category=TaxCategory.OFFICE_EXPENSE.value,
        direction=Direction.EXPENSE.value,
        status=status,
        payment_method=payment_method,
        raw_data={"pending": pending},
    )
    session.add(tx)
    session.flush()
    return tx


def _log(session: Session, run_at: datetime, status: str = "success", detail: str | None = None) -> None:
    session.add(
        IngestionLog(source="plaid", run_at=run_at, status=status, error_detail=detail)
    )
    session.flush()


def test_prior_month() -> None:
    """REQ-MCA-001: default scope is the prior calendar month."""
    assert prior_month(date(2026, 7, 7)) == "2026-06"
    assert prior_month(date(2026, 1, 3)) == "2025-12"


def test_gap_day_flagged(session: Session) -> None:
    """REQ-MCA-001: a missing sync day surfaces as a coverage gap."""
    item = _item(session)
    _account(session, item, payment_method="Chase ****1")
    # Cover only June 1 → the rest of June are gaps.
    _log(session, datetime(2026, 6, 1, 5, 0, 0))
    summary = reconcile(session, "2026-06", today=_TODAY)
    it = summary.items[0]
    assert it.has_gap
    assert "2026-06-02" in it.gap_days
    assert "2026-06-01" not in it.gap_days


def test_balance_tie_out_within_tolerance_passes(session: Session) -> None:
    """REQ-MCA-001: |Δ − Σ| == $0.01 is within tolerance (tie-out ok)."""
    item = _item(session)
    acct = _account(session, item, payment_method="Chase ****2")
    _snap(session, acct, date(2026, 5, 31), "1000.00")
    _snap(session, acct, date(2026, 6, 30), "1100.00")  # Δ = 100.00
    _tx(session, payment_method="Chase ****2", date_="2026-06-15", amount="99.99")  # Σ = 99.99
    summary = reconcile(session, "2026-06", today=_TODAY)
    acc = summary.items[0].accounts[0]
    assert acc.is_depository
    assert acc.balance_delta == Decimal("100.00")
    assert acc.tie_out_gap == Decimal("0.01")
    assert acc.tie_out_ok is True
    assert summary.has_discrepancy is False


def test_balance_discrepancy_flagged(session: Session) -> None:
    """REQ-MCA-001: |Δ − Σ| > $0.01 is a discrepancy with both numbers."""
    item = _item(session)
    acct = _account(session, item, payment_method="Chase ****3")
    _snap(session, acct, date(2026, 5, 31), "1000.00")
    _snap(session, acct, date(2026, 6, 30), "1100.00")  # Δ = 100.00
    _tx(session, payment_method="Chase ****3", date_="2026-06-15", amount="50.00")  # Σ = 50.00
    summary = reconcile(session, "2026-06", today=_TODAY)
    acc = summary.items[0].accounts[0]
    assert acc.tie_out_ok is False
    assert acc.tie_out_gap == Decimal("50.00")
    assert summary.has_discrepancy is True


def test_balance_tie_out_tolerates_snapshot_date_skew(session: Session) -> None:
    """P2-c3f: skewed snapshot dates (not on month boundaries) don't false-flag.

    Baseline lands several days before the month, and the in-month snapshot
    isn't the last day — a transaction dated outside the exact snapshot
    window must not be counted against the tie-out, or a correct ledger
    would be flagged as a discrepancy.
    """
    item = _item(session)
    acct = _account(session, item, payment_method="Chase ****6")
    _snap(session, acct, date(2026, 5, 27), "1000.00")  # baseline, 5 days before June
    _snap(session, acct, date(2026, 6, 20), "1050.00")  # latest, 10 days before month end
    # Inside the exact snapshot window (5/28..6/20): counts toward Σ.
    _tx(session, payment_method="Chase ****6", date_="2026-06-10", amount="50.00")
    # Outside the snapshot window (after 6/20, still within the calendar
    # month) — must NOT be counted, or Δ(100) vs naive-month-Σ(70) would
    # falsely flag a $30 discrepancy.
    _tx(session, payment_method="Chase ****6", date_="2026-06-25", amount="20.00")
    summary = reconcile(session, "2026-06", today=_TODAY)
    acc = summary.items[0].accounts[0]
    assert acc.balance_delta == Decimal("50.00")
    assert acc.tie_out_gap == Decimal("0.00")
    assert acc.tie_out_ok is True
    # Full calendar-month register aggregate is still reported for display.
    assert acc.register_sum == Decimal("70.00")
    assert acc.register_count == 2


def test_credit_card_reports_flows_no_tie_out(session: Session) -> None:
    """REQ-MCA-001: credit-card accounts report flows only — no balance tie-out."""
    item = _item(session, name="Amex")
    acct = _account(
        session, item, payment_method="Amex ****4", broker="amex", account_type="credit_card"
    )
    _snap(session, acct, date(2026, 6, 30), "500.00", ptype="credit")
    _tx(session, payment_method="Amex ****4", date_="2026-06-10", amount="-75.00")
    summary = reconcile(session, "2026-06", today=_TODAY)
    acc = summary.items[0].accounts[0]
    assert acc.tie_out_ok is None
    assert acc.register_count == 1
    assert "credit-card" in acc.note


def test_stuck_pending_older_than_7_days(session: Session) -> None:
    """REQ-MCA-001: a pending Plaid row older than 7 days is listed as stuck."""
    item = _item(session)
    _account(session, item, payment_method="Chase ****5")
    _tx(session, payment_method="Chase ****5", date_="2026-06-10", amount="-20.00", pending=True)
    _tx(session, payment_method="Chase ****5", date_="2026-07-05", amount="-30.00", pending=True)
    summary = reconcile(session, "2026-06", today=_TODAY)
    stuck_dates = {s.date for s in summary.stuck_pending}
    assert "2026-06-10" in stuck_dates  # 27 days old → stuck
    assert "2026-07-05" not in stuck_dates  # 2 days old → not stuck


def test_needs_review_backlog_and_unmapped(session: Session) -> None:
    """REQ-MCA-001: backlog depth per entity and unmapped-account callouts surface."""
    item = _item(session)
    _account(session, item, payment_method="Chase ****6")
    _tx(
        session, payment_method="Chase ****6", date_="2026-06-01", amount="-5.00",
        status=TransactionStatus.NEEDS_REVIEW.value,
    )
    _log(session, datetime(2026, 6, 2, 5, 0, 0), detail="unmapped account: pa-999 (Chase)")
    summary = reconcile(session, "2026-06", today=_TODAY)
    assert any(b.entity == Entity.SPARKRY.value and b.count == 1 for b in summary.needs_review_backlog)
    assert any("unmapped" in u for u in summary.unmapped_accounts)


def test_disconnected_item_skipped(session: Session) -> None:
    """REQ-MCA-001: a disconnected PlaidItem is excluded from the tie-out."""
    item = _item(session)
    item.status = "disconnected"
    session.flush()
    summary = reconcile(session, "2026-06", today=_TODAY)
    assert summary.items == []
