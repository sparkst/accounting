"""Tests for scripts/backfill_adjusted_closes.py (REQ-FIX-WLT-001 / -002).

All fetchers are injected fakes — no network. Verifies adj_close backfill onto
existing historical_price rows, stock_split upsert from the splits API, DRY-RUN
safety, and idempotent re-runs.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from scripts.backfill_adjusted_closes import (
    backfill_adjusted,
    discover_symbols,
)
from src.adapters.yfinance_prices import HistoricalPriceRow
from src.models.base import Base
from src.models.history import HistoricalPrice, StockSplit


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _):  # type: ignore[no-untyped-def]
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return Session(bind=engine)


def _seed_price(
    session: Session, symbol: str, d: date, close: str, adj: str | None = None
) -> None:
    session.add(
        HistoricalPrice(
            symbol=symbol,
            trade_date=d,
            close=Decimal(close),
            adj_close=Decimal(adj) if adj is not None else None,
            source="yfinance",
        )
    )


def _price_row(symbol: str, d: date, close: str, adj: str | None) -> HistoricalPriceRow:
    return {
        "symbol": symbol,
        "trade_date": d,
        "close": Decimal(close),
        "adj_close": Decimal(adj) if adj is not None else None,
        "open": None,
        "high": None,
        "low": None,
        "volume": None,
    }


def test_discover_symbols_from_historical_price(session: Session) -> None:
    _seed_price(session, "SPY", date(2024, 6, 1), "520.00")
    _seed_price(session, "aapl", date(2024, 6, 1), "190.00")
    session.commit()
    assert discover_symbols(session) == ["AAPL", "SPY"]


def test_backfill_updates_adj_close_and_writes_splits(session: Session) -> None:
    """REQ-FIX-WLT-001/-002: adj_close populated on matching rows; splits upserted."""
    _seed_price(session, "AAPL", date(2024, 6, 1), "190.00", adj=None)
    _seed_price(session, "AAPL", date(2024, 6, 2), "191.00", adj=None)
    session.commit()

    fake_prices = [
        _price_row("AAPL", date(2024, 6, 1), "190.00", "188.50"),
        _price_row("AAPL", date(2024, 6, 2), "191.00", "189.40"),
        # A date not in the DB — must be ignored, not inserted.
        _price_row("AAPL", date(2024, 6, 3), "192.00", "190.10"),
    ]
    fake_splits = [(date(2020, 8, 31), Decimal("4.000000"))]

    summary = backfill_adjusted(
        session,
        ["AAPL"],
        start=date(2020, 1, 1),
        end=date(2024, 6, 30),
        dry_run=False,
        fetch_prices=lambda syms, s, e: fake_prices,
        fetch_splits=lambda sym: fake_splits,
    )

    assert summary["AAPL"]["adj_updated"] == 2
    assert summary["AAPL"]["splits_written"] == 1

    r1 = session.get(HistoricalPrice, ("AAPL", date(2024, 6, 1)))
    r2 = session.get(HistoricalPrice, ("AAPL", date(2024, 6, 2)))
    assert r1 is not None and r1.adj_close == Decimal("188.50")
    assert r1.close == Decimal("190.00")  # raw close untouched
    assert r2 is not None and r2.adj_close == Decimal("189.40")
    # No phantom row inserted for the un-seeded date.
    assert session.get(HistoricalPrice, ("AAPL", date(2024, 6, 3))) is None
    assert session.query(HistoricalPrice).count() == 2

    split = session.get(StockSplit, ("AAPL", date(2020, 8, 31)))
    assert split is not None and split.ratio == Decimal("4.000000")
    assert split.source == "yfinance"


def test_dry_run_writes_nothing(session: Session) -> None:
    _seed_price(session, "AAPL", date(2024, 6, 1), "190.00", adj=None)
    session.commit()

    summary = backfill_adjusted(
        session,
        ["AAPL"],
        start=date(2020, 1, 1),
        end=date(2024, 6, 30),
        dry_run=True,
        fetch_prices=lambda syms, s, e: [
            _price_row("AAPL", date(2024, 6, 1), "190.00", "188.50")
        ],
        fetch_splits=lambda sym: [(date(2020, 8, 31), Decimal("4.000000"))],
    )
    # Summary reports what *would* change...
    assert summary["AAPL"]["adj_updated"] == 1
    assert summary["AAPL"]["splits_written"] == 1
    session.expire_all()
    # ...but nothing is persisted.
    r = session.get(HistoricalPrice, ("AAPL", date(2024, 6, 1)))
    assert r is not None and r.adj_close is None
    assert session.query(StockSplit).count() == 0


def test_idempotent_reruns(session: Session) -> None:
    """Re-running overwrites adj_close (sanctioned) and leaves one split row."""
    _seed_price(session, "AAPL", date(2024, 6, 1), "190.00", adj=None)
    session.commit()

    kwargs = dict(
        start=date(2020, 1, 1),
        end=date(2024, 6, 30),
        dry_run=False,
        fetch_prices=lambda syms, s, e: [
            _price_row("AAPL", date(2024, 6, 1), "190.00", "188.50")
        ],
        fetch_splits=lambda sym: [(date(2020, 8, 31), Decimal("4.000000"))],
    )

    backfill_adjusted(session, ["AAPL"], **kwargs)  # type: ignore[arg-type]
    second = backfill_adjusted(session, ["AAPL"], **kwargs)  # type: ignore[arg-type]

    # adj_close still set; only one split row (no duplicate insert).
    r = session.get(HistoricalPrice, ("AAPL", date(2024, 6, 1)))
    assert r is not None and r.adj_close == Decimal("188.50")
    assert session.query(StockSplit).count() == 1
    # Second run's split is unchanged → not counted as written.
    assert second["AAPL"]["splits_written"] == 0
    assert second["AAPL"]["adj_updated"] == 1  # adj is overwritten idempotently


def test_per_symbol_error_isolation(session: Session) -> None:
    """A fetch failure on one symbol must not abort the batch."""
    _seed_price(session, "GOOD", date(2024, 6, 1), "10.00", adj=None)
    session.commit()

    def fake_prices(syms: list[str], s: date, e: date) -> list[HistoricalPriceRow]:
        if syms == ["BAD"]:
            raise RuntimeError("boom")
        return [_price_row("GOOD", date(2024, 6, 1), "10.00", "9.50")]

    summary = backfill_adjusted(
        session,
        ["GOOD", "BAD"],
        start=date(2020, 1, 1),
        end=date(2024, 6, 30),
        dry_run=False,
        fetch_prices=fake_prices,
        fetch_splits=lambda sym: [],
    )
    assert summary["GOOD"]["adj_updated"] == 1
    assert summary["BAD"]["errored"] == 1
    r = session.get(HistoricalPrice, ("GOOD", date(2024, 6, 1)))
    assert r is not None and r.adj_close == Decimal("9.50")
