"""Tests for scripts/backfill_historical_prices.py."""

from datetime import date, timedelta
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
from src.models.history import HistoricalPrice, StockSplit


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
         "adj_close": None, "open": None, "high": None, "low": None, "volume": None},
        {"symbol": "SPY", "trade_date": date(2024, 6, 2), "close": Decimal("521.10"),
         "adj_close": None, "open": None, "high": None, "low": None, "volume": None},
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
         "adj_close": None, "open": None, "high": None, "low": None, "volume": None},
        {"symbol": "SPY", "trade_date": date(2024, 6, 2), "close": Decimal("521.10"),
         "adj_close": None, "open": None, "high": None, "low": None, "volume": None},
    ]
    with patch("scripts.backfill_historical_prices.fetch_eod", return_value=fake_rows):
        summary = backfill(session, ["SPY"], start=date(2024, 6, 1), end=date(2024, 6, 30), dry_run=False)

    assert summary["SPY"]["fetched"] == 2
    assert summary["SPY"]["inserted"] == 2
    assert session.query(HistoricalPrice).count() == 2


def test_backfill_dry_run_writes_nothing(session: Session) -> None:
    fake_rows: list[HistoricalPriceRow] = [
        {"symbol": "SPY", "trade_date": date(2024, 6, 1), "close": Decimal("520.55"),
         "adj_close": None, "open": None, "high": None, "low": None, "volume": None},
    ]
    with patch("scripts.backfill_historical_prices.fetch_eod", return_value=fake_rows):
        summary = backfill(session, ["SPY"], start=date(2024, 6, 1), end=date(2024, 6, 30), dry_run=True)
    assert summary["SPY"]["fetched"] == 1
    assert summary["SPY"]["inserted"] == 0  # dry-run doesn't write
    assert session.query(HistoricalPrice).count() == 0


def test_benchmark_symbols_set() -> None:
    assert "SPY" in BENCHMARK_SYMBOLS
    assert "VTI" in BENCHMARK_SYMBOLS


def test_backfill_isolates_one_failing_symbol(session: Session) -> None:
    """A yfinance error on one symbol must not abort the batch; other symbols
    still process and the failure is recorded in the per-symbol summary."""
    from src.models.ingestion_log import IngestionLog

    def fake_fetch(symbols: list[str], start, end):  # type: ignore[no-untyped-def]
        if symbols == ["BAD"]:
            raise RuntimeError("simulated network error")
        return [
            {
                "symbol": symbols[0], "trade_date": date(2024, 6, 1),
                "close": Decimal("100"),
                "open": None, "high": None, "low": None, "volume": None,
            }
        ]

    with patch("scripts.backfill_historical_prices.fetch_eod", side_effect=fake_fetch):
        summary = backfill(
            session,
            ["GOOD", "BAD"],
            start=date(2024, 6, 1),
            end=date(2024, 6, 30),
            dry_run=False,
        )

    assert summary["GOOD"]["inserted"] == 1
    assert summary["BAD"]["errored"] == 1
    assert summary["BAD"]["fetched"] == 0
    # Verify the GOOD row actually survived the commit, not just that the
    # in-memory counter was incremented.
    assert session.query(HistoricalPrice).filter_by(symbol="GOOD").count() == 1

    log = session.query(IngestionLog).filter_by(source="yfinance_backfill").one()
    assert log.records_failed == 1
    assert log.status == "partial_failure"


def test_backfill_apply_writes_ingestion_log_even_when_called_programmatically(
    session: Session,
) -> None:
    """The audit row must land regardless of whether main() wraps the call."""
    from src.models.ingestion_log import IngestionLog

    fake_rows = [
        {
            "symbol": "SPY", "trade_date": date(2024, 6, 1),
            "close": Decimal("520"),
            "open": None, "high": None, "low": None, "volume": None,
        }
    ]
    with patch("scripts.backfill_historical_prices.fetch_eod", return_value=fake_rows):
        backfill(
            session,
            ["SPY"],
            start=date(2024, 6, 1),
            end=date(2024, 6, 30),
            dry_run=False,
        )

    logs = session.query(IngestionLog).filter_by(source="yfinance_backfill").all()
    assert len(logs) == 1
    assert logs[0].status == "success"
    assert logs[0].records_processed == 1


def test_backfill_dry_run_does_not_write_ingestion_log(session: Session) -> None:
    """Dry-run must remain audit-silent so we don't pollute the log table."""
    from src.models.ingestion_log import IngestionLog

    fake_rows = [
        {
            "symbol": "SPY", "trade_date": date(2024, 6, 1),
            "close": Decimal("520"),
            "open": None, "high": None, "low": None, "volume": None,
        }
    ]
    with patch("scripts.backfill_historical_prices.fetch_eod", return_value=fake_rows):
        backfill(
            session, ["SPY"], start=date(2024, 6, 1), end=date(2024, 6, 30),
            dry_run=True,
        )

    assert (
        session.query(IngestionLog).filter_by(source="yfinance_backfill").count() == 0
    )


def test_new_rows_persist_adj_close(session: Session) -> None:
    """REQ-FIX-WLT-001: adj_close on fetched rows is stored on insert."""
    fake_rows: list[HistoricalPriceRow] = [
        {"symbol": "SPY", "trade_date": date(2024, 6, 1), "close": Decimal("520.55"),
         "adj_close": Decimal("518.20"),
         "open": None, "high": None, "low": None, "volume": None},
    ]
    with patch("scripts.backfill_historical_prices.fetch_eod", return_value=fake_rows):
        backfill(session, ["SPY"], start=date(2024, 6, 1), end=date(2024, 6, 30),
                 dry_run=False)

    row = session.get(HistoricalPrice, ("SPY", date(2024, 6, 1)))
    assert row is not None
    assert row.close == Decimal("520.55")
    assert row.adj_close == Decimal("518.20")


def test_trailing_window_adj_close_refresh(session: Session) -> None:
    """REQ-FIX-WLT-001: a pre-existing row inside the trailing window gets its
    adj_close re-written from the fresh frame (ex-div restatement)."""
    # Seed a row with a stale adj_close, dated 'today-ish' so it's in the window.
    seed_day = date.today() - timedelta(days=3)
    session.add(
        HistoricalPrice(symbol="SPY", trade_date=seed_day, close=Decimal("500.00"),
                        adj_close=Decimal("400.00"), source="yfinance")
    )
    session.commit()

    fake_rows: list[HistoricalPriceRow] = [
        {"symbol": "SPY", "trade_date": seed_day, "close": Decimal("500.00"),
         "adj_close": Decimal("498.75"),  # restated
         "open": None, "high": None, "low": None, "volume": None},
    ]
    with patch("scripts.backfill_historical_prices.fetch_eod", return_value=fake_rows):
        summary = backfill(session, ["SPY"], start=date(2024, 1, 1),
                           end=date.today(), dry_run=False, refresh_adj_days=30)

    session.expire_all()
    row = session.get(HistoricalPrice, ("SPY", seed_day))
    assert row is not None
    assert row.adj_close == Decimal("498.75")  # refreshed, not the stale 400
    assert row.close == Decimal("500.00")  # raw close untouched
    assert summary["SPY"]["adj_refreshed"] == 1


def test_backfill_writes_splits_when_fetcher_supplied(session: Session) -> None:
    """REQ-FIX-WLT-002: nightly job upserts stock_split via the injected fetcher."""
    fake_rows: list[HistoricalPriceRow] = [
        {"symbol": "AAPL", "trade_date": date(2024, 6, 1), "close": Decimal("190.00"),
         "adj_close": Decimal("188.00"),
         "open": None, "high": None, "low": None, "volume": None},
    ]
    with patch("scripts.backfill_historical_prices.fetch_eod", return_value=fake_rows):
        summary = backfill(
            session, ["AAPL"], start=date(2020, 1, 1), end=date(2024, 6, 30),
            dry_run=False,
            fetch_splits=lambda sym: [(date(2020, 8, 31), Decimal("4.000000"))],
        )

    split = session.get(StockSplit, ("AAPL", date(2020, 8, 31)))
    assert split is not None
    assert split.ratio == Decimal("4.000000")
    assert summary["AAPL"]["splits_written"] == 1


def test_dry_run_skips_adj_refresh_and_splits(session: Session) -> None:
    """DRY-RUN must not refresh adj_close or write splits."""
    seed_day = date.today() - timedelta(days=2)
    session.add(
        HistoricalPrice(symbol="AAPL", trade_date=seed_day, close=Decimal("190.00"),
                        adj_close=Decimal("100.00"), source="yfinance")
    )
    session.commit()

    fake_rows: list[HistoricalPriceRow] = [
        {"symbol": "AAPL", "trade_date": seed_day, "close": Decimal("190.00"),
         "adj_close": Decimal("188.00"),
         "open": None, "high": None, "low": None, "volume": None},
    ]
    with patch("scripts.backfill_historical_prices.fetch_eod", return_value=fake_rows):
        backfill(session, ["AAPL"], start=date(2020, 1, 1), end=date.today(),
                 dry_run=True,
                 fetch_splits=lambda sym: [(date(2020, 8, 31), Decimal("4"))])

    session.expire_all()
    row = session.get(HistoricalPrice, ("AAPL", seed_day))
    assert row is not None and row.adj_close == Decimal("100.00")  # unchanged
    assert session.query(StockSplit).count() == 0
