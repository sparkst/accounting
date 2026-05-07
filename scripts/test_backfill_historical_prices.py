"""Tests for scripts/backfill_historical_prices.py."""

from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from scripts.backfill_historical_prices import (
    BENCHMARK_SYMBOLS,
    _persist_rows,
    backfill,
    discover_symbols,
)
from src.adapters.yfinance_prices import HistoricalPriceRow
from src.models.base import Base
from src.models.brokerage import Account, BrokerageTransaction, PositionSnapshot
from src.models.history import HistoricalPrice


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _):  # type: ignore[no-untyped-def]
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return Session(bind=engine)


def _make_account(s: Session) -> Account:
    a = Account(
        broker="fidelity",
        account_number="X1",
        account_type="taxable",
        entity="personal",
        tax_sheltered=False,
    )
    s.add(a)
    s.flush()
    return a


def _make_pos(a: Account, sym: str | None, hash_: str) -> PositionSnapshot:
    return PositionSnapshot(
        account_id=a.id,
        as_of=date(2024, 6, 1),
        symbol=sym,
        quantity=Decimal("1"),
        price=Decimal("1"),
        source_file="seed.csv",
        source_row_hash=hash_,
        raw_data={},
    )


def test_discover_symbols_pulls_from_position_snapshot_and_transactions(
    session: Session,
) -> None:
    a = _make_account(session)
    session.add(_make_pos(a, "aapl", "h-aapl"))
    session.add(
        BrokerageTransaction(
            account_id=a.id,
            trade_date=date(2024, 5, 1),
            action="BUY",
            canonical_action="buy",
            symbol="msft",
            source_file="seed",
            source_row_hash="h1",
            raw_data={},
        )
    )
    session.commit()

    symbols = discover_symbols(session)
    assert "AAPL" in symbols
    assert "MSFT" in symbols


def test_discover_symbols_skips_total_and_generated(session: Session) -> None:
    a = _make_account(session)
    for i, sym in enumerate(("TOTAL", "Generated_2024", "  ", None)):
        session.add(_make_pos(a, sym, f"h-{i}"))
    session.commit()
    assert discover_symbols(session) == set()


def test_persist_rows_inserts_and_skips_existing(session: Session) -> None:
    rows: list[HistoricalPriceRow] = [
        {"symbol": "SPY", "trade_date": date(2024, 6, 1), "close": Decimal("520.55"),
         "open": None, "high": None, "low": None, "volume": None},
        {"symbol": "SPY", "trade_date": date(2024, 6, 2), "close": Decimal("521.10"),
         "open": None, "high": None, "low": None, "volume": None},
    ]
    inserted, skipped = _persist_rows(session, rows)
    session.commit()
    assert inserted == 2
    assert skipped == 0

    # Re-run: both already present.
    inserted2, skipped2 = _persist_rows(session, rows)
    assert inserted2 == 0
    assert skipped2 == 2


def test_backfill_uses_yfinance_adapter_and_persists(session: Session) -> None:
    fake_rows: list[HistoricalPriceRow] = [
        {"symbol": "SPY", "trade_date": date(2024, 6, 1), "close": Decimal("520.55"),
         "open": None, "high": None, "low": None, "volume": None},
        {"symbol": "SPY", "trade_date": date(2024, 6, 2), "close": Decimal("521.10"),
         "open": None, "high": None, "low": None, "volume": None},
    ]
    with patch("scripts.backfill_historical_prices.fetch_eod", return_value=fake_rows):
        summary = backfill(session, ["SPY"], start=date(2024, 6, 1), end=date(2024, 6, 30), apply=True)

    assert summary["SPY"]["fetched"] == 2
    assert summary["SPY"]["inserted"] == 2
    assert session.query(HistoricalPrice).count() == 2


def test_backfill_dry_run_writes_nothing(session: Session) -> None:
    fake_rows: list[HistoricalPriceRow] = [
        {"symbol": "SPY", "trade_date": date(2024, 6, 1), "close": Decimal("520.55"),
         "open": None, "high": None, "low": None, "volume": None},
    ]
    with patch("scripts.backfill_historical_prices.fetch_eod", return_value=fake_rows):
        summary = backfill(session, ["SPY"], start=date(2024, 6, 1), end=date(2024, 6, 30), apply=False)
    assert summary["SPY"]["fetched"] == 1
    assert summary["SPY"]["inserted"] == 0  # dry-run doesn't write
    assert session.query(HistoricalPrice).count() == 0


def test_benchmark_symbols_set() -> None:
    assert "SPY" in BENCHMARK_SYMBOLS
    assert "VTI" in BENCHMARK_SYMBOLS
