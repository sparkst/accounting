"""Tests for Phase 3 history ORM models."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.models.base import Base
from src.models.brokerage import Account
from src.models.enums import AccountType, Broker, Entity
from src.models.history import (
    AccountBalanceSnapshot,
    CostBasisLot,
    ExpectedAccount,
    HistoricalPrice,
)


@pytest.fixture
def session() -> Session:
    """Fresh in-memory SQLite with FK enforcement."""
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _):  # type: ignore[no-untyped-def]
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return Session(bind=engine)


def _make_account(session: Session) -> Account:
    acct = Account(
        broker=Broker.FIDELITY.value,
        account_number="Z23257759",
        account_name="Individual - TOD",
        account_type=AccountType.TOD.value,
        entity=Entity.PERSONAL.value,
        tax_sheltered=False,
    )
    session.add(acct)
    session.flush()
    return acct


# HistoricalPrice ----------------------------------------------------------


def test_historical_price_round_trip(session: Session) -> None:
    p = HistoricalPrice(
        symbol="SPY", trade_date=date(2024, 6, 1), close=Decimal("520.55")
    )
    session.add(p)
    session.commit()
    fetched = session.query(HistoricalPrice).one()
    assert fetched.symbol == "SPY"
    assert fetched.close == Decimal("520.55")
    assert fetched.source == "yfinance"  # server default


def test_historical_price_pk_blocks_duplicate(session: Session) -> None:
    session.add(HistoricalPrice(symbol="SPY", trade_date=date(2024, 6, 1), close=Decimal("1")))
    session.commit()
    session.add(HistoricalPrice(symbol="SPY", trade_date=date(2024, 6, 1), close=Decimal("2")))
    with pytest.raises(IntegrityError):
        session.commit()


def test_historical_price_same_symbol_different_date(session: Session) -> None:
    session.add(HistoricalPrice(symbol="SPY", trade_date=date(2024, 6, 1), close=Decimal("1")))
    session.add(HistoricalPrice(symbol="SPY", trade_date=date(2024, 6, 2), close=Decimal("2")))
    session.commit()
    assert session.query(HistoricalPrice).count() == 2


# AccountBalanceSnapshot ---------------------------------------------------


def test_account_balance_snapshot_with_account_id(session: Session) -> None:
    acct = _make_account(session)
    snap = AccountBalanceSnapshot(
        account_id=acct.id,
        raw_account_name="Individual - TOD",
        as_of=date(2023, 11, 2),
        balance=Decimal("1234567.89"),
        source="xlsx_2024",
        source_row_hash="hash-xyz",
    )
    session.add(snap)
    session.commit()
    fetched = session.query(AccountBalanceSnapshot).one()
    assert fetched.balance == Decimal("1234567.89")
    assert fetched.account is not None
    assert fetched.account.id == acct.id


def test_account_balance_snapshot_unmatched_allows_null_account_id(session: Session) -> None:
    snap = AccountBalanceSnapshot(
        account_id=None,
        raw_account_name="Some XLSX label that doesn't match",
        as_of=date(2018, 1, 20),
        balance=Decimal("100000"),
        source="xlsx_2024",
        source_row_hash="hash-orphan",
    )
    session.add(snap)
    session.commit()
    assert session.query(AccountBalanceSnapshot).count() == 1


def test_account_balance_snapshot_unique_by_rawname_date_source(session: Session) -> None:
    snap = AccountBalanceSnapshot(
        account_id=None,
        raw_account_name="Same Name",
        as_of=date(2018, 1, 20),
        balance=Decimal("1"),
        source="xlsx_2024",
        source_row_hash="h1",
    )
    session.add(snap)
    session.commit()
    dup = AccountBalanceSnapshot(
        account_id=None,
        raw_account_name="Same Name",
        as_of=date(2018, 1, 20),
        balance=Decimal("2"),
        source="xlsx_2024",
        source_row_hash="h2",
    )
    session.add(dup)
    with pytest.raises(IntegrityError):
        session.commit()


# ExpectedAccount ----------------------------------------------------------


def test_expected_account_default_status_unconfirmed(session: Session) -> None:
    e = ExpectedAccount(
        institution="Vanguard",
        account_name="Travis Roth IRA",
        last_4="9844",
        source="xlsx",
    )
    session.add(e)
    session.commit()
    fetched = session.query(ExpectedAccount).one()
    assert fetched.status == "unconfirmed"


def test_expected_account_status_check_constraint(session: Session) -> None:
    e = ExpectedAccount(
        institution="Vanguard",
        account_name="X",
        status="banana",
        source="xlsx",
    )
    session.add(e)
    with pytest.raises(IntegrityError):
        session.commit()


def test_expected_account_natural_key_unique(session: Session) -> None:
    e1 = ExpectedAccount(
        institution="Vanguard", account_name="Roth", last_4="9844", source="xlsx"
    )
    session.add(e1)
    session.commit()
    e2 = ExpectedAccount(
        institution="Vanguard", account_name="Roth", last_4="9844", source="credit_karma"
    )
    session.add(e2)
    with pytest.raises(IntegrityError):
        session.commit()


def test_expected_account_resolves_to_account(session: Session) -> None:
    acct = _make_account(session)
    e = ExpectedAccount(
        institution="Fidelity",
        account_name="Individual - TOD",
        source="manual",
        resolved_account_id=acct.id,
    )
    session.add(e)
    session.commit()
    fetched = session.query(ExpectedAccount).one()
    assert fetched.resolved_account is not None
    assert fetched.resolved_account.id == acct.id


# CostBasisLot -------------------------------------------------------------


def test_cost_basis_lot_round_trip(session: Session) -> None:
    lot = CostBasisLot(
        raw_account_name="TD Ameritrade",
        symbol="AMZN",
        security_name="AMAZON.COM INC",
        open_date=date(2009, 4, 7),
        quantity=Decimal("8.4108"),
        cost_per_share=Decimal("54.9139"),
        cost_total=Decimal("461.87"),
        source="xlsx_td_gainloss",
        source_row_hash="abc123",
    )
    session.add(lot)
    session.commit()
    fetched = session.query(CostBasisLot).one()
    assert fetched.symbol == "AMZN"
    assert fetched.cost_total == Decimal("461.87")


def test_cost_basis_lot_dedup_by_row_hash(session: Session) -> None:
    base = dict(
        raw_account_name="TD Ameritrade",
        symbol="AMZN",
        open_date=date(2009, 4, 7),
        quantity=Decimal("1"),
        cost_per_share=Decimal("1"),
        cost_total=Decimal("1"),
        source="xlsx_td_gainloss",
        source_row_hash="dedup-hash",
    )
    session.add(CostBasisLot(**base))
    session.commit()
    session.add(CostBasisLot(**base))
    with pytest.raises(IntegrityError):
        session.commit()
