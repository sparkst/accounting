"""Split-safe re-pricing tests (REQ-FIX-WLT-002).

A stock split with an ex-date between the position snapshot and the target date
must not create a value cliff: quantity(snapshot) is scaled by the cumulative
split ratio before multiplying by the post-split close. Symbols with no split
rows behave exactly as before (ratio 1).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.models import brokerage as _brokerage_models  # noqa: F401
from src.models import history as _history_models  # noqa: F401
from src.models import plaid as _plaid_models  # noqa: F401
from src.models.base import Base
from src.models.brokerage import Account, PositionSnapshot
from src.models.enums import AccountType, Broker, Entity
from src.models.history import HistoricalPrice, StockSplit
from src.reports.brokerage_summary import _load_history_state, _per_account_value_at


def _session() -> Any:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _add_account(s: Any) -> Account:
    a = Account(
        broker=Broker.SCHWAB.value,
        account_number="0001",
        account_type=AccountType.TAXABLE.value,
        entity=Entity.PERSONAL.value,
    )
    s.add(a)
    s.flush()
    return a


def test_split_between_snapshot_and_target_is_cliff_free() -> None:
    """REQ-FIX-WLT-002: a 2:1 split in (snapshot, target] doesn't halve value."""
    s = _session()
    a = _add_account(s)
    snap_date = date(2026, 6, 1)
    target = date(2026, 6, 8)
    # Snapshot: 10 shares, pre-split.
    s.add(
        PositionSnapshot(
            account_id=a.id,
            as_of=datetime(2026, 6, 1),
            symbol="SPLT",
            quantity=Decimal("10"),
            price=Decimal("100"),
            market_value=Decimal("1000.00"),
            source_file="t.csv",
            source_row_hash="h1",
            raw_data={},
        )
    )
    # Prices AS YFINANCE STORES THEM (P1-001): auto_adjust=False Close is
    # split-adjusted to the PRESENT share scale — the snapshot-day row is 50
    # (nominal 100 ÷ the later 2:1 split), the target-day row is 50 (nominal).
    s.add(HistoricalPrice(symbol="SPLT", trade_date=snap_date, close=Decimal("50")))
    s.add(HistoricalPrice(symbol="SPLT", trade_date=target, close=Decimal("50")))
    # 2:1 forward split ex-date within the window.
    s.add(StockSplit(symbol="SPLT", ex_date=date(2026, 6, 4), ratio=Decimal("2.000000")))
    s.commit()

    state = _load_history_state(s)
    per_account = _per_account_value_at(s, target, history_state=state)
    # 10 shares * 2 (all splits after snapshot) * 50 = 1000 — no cliff.
    assert per_account[a.id]["market_value"] == Decimal("1000")


def test_pre_split_historical_target_reprices_to_nominal() -> None:
    """P1-001 regression (the AMZN-20:1 shape): a target date BEFORE the split,
    valued from a snapshot also before the split, must reprice to the true
    nominal value. The stored close on that date is split-adjusted to present
    (nominal ÷ ratio), so the quantity scale-up by ALL later splits must apply
    even though the split ex-date is after the target."""
    s = _session()
    a = _add_account(s)
    s.add(
        PositionSnapshot(
            account_id=a.id,
            as_of=datetime(2026, 5, 1),
            symbol="SPLT",
            quantity=Decimal("10"),
            price=Decimal("100"),
            market_value=Decimal("1000.00"),
            source_file="t.csv",
            source_row_hash="h1b",
            raw_data={},
        )
    )
    target = date(2026, 5, 15)  # BEFORE the 2026-06-04 split
    # Stored close on the pre-split target date: nominal $100 ÷ 2 = 50.
    s.add(HistoricalPrice(symbol="SPLT", trade_date=target, close=Decimal("50")))
    s.add(StockSplit(symbol="SPLT", ex_date=date(2026, 6, 4), ratio=Decimal("2.000000")))
    s.commit()

    state = _load_history_state(s)
    per_account = _per_account_value_at(s, target, history_state=state)
    # True nominal value on 2026-05-15: 10 shares × $100 = $1000.
    # (The pre-fix bounded ratio gave 10 × 1 × 50 = $500 — a 2x cliff.)
    assert per_account[a.id]["market_value"] == Decimal("1000")


def test_split_symbol_casing_mismatch_still_matches() -> None:
    """P3-411: split stored uppercase, snapshot symbol lowercase — must match."""
    s = _session()
    a = _add_account(s)
    s.add(
        PositionSnapshot(
            account_id=a.id,
            as_of=datetime(2026, 6, 1),
            symbol="splt",
            quantity=Decimal("10"),
            price=Decimal("100"),
            market_value=Decimal("1000.00"),
            source_file="t.csv",
            source_row_hash="h1c",
            raw_data={},
        )
    )
    target = date(2026, 6, 8)
    s.add(HistoricalPrice(symbol="SPLT", trade_date=target, close=Decimal("50")))
    s.add(StockSplit(symbol="SPLT", ex_date=date(2026, 6, 4), ratio=Decimal("2.000000")))
    s.commit()

    state = _load_history_state(s)
    per_account = _per_account_value_at(s, target, history_state=state)
    assert per_account[a.id]["market_value"] == Decimal("1000")


def test_no_split_symbol_unchanged() -> None:
    """REQ-FIX-WLT-002: symbols without split rows use ratio 1 (unchanged)."""
    s = _session()
    a = _add_account(s)
    target = date(2026, 6, 8)
    s.add(
        PositionSnapshot(
            account_id=a.id,
            as_of=datetime(2026, 6, 1),
            symbol="NOSP",
            quantity=Decimal("10"),
            price=Decimal("100"),
            market_value=Decimal("1000.00"),
            source_file="t.csv",
            source_row_hash="h2",
            raw_data={},
        )
    )
    s.add(HistoricalPrice(symbol="NOSP", trade_date=date(2026, 6, 1), close=Decimal("100")))
    s.add(HistoricalPrice(symbol="NOSP", trade_date=target, close=Decimal("110")))
    s.commit()

    state = _load_history_state(s)
    per_account = _per_account_value_at(s, target, history_state=state)
    assert per_account[a.id]["market_value"] == Decimal("1100")


@pytest.mark.parametrize("stale_gap_days", [30])
def test_price_staleness_bound_falls_back_to_stored(stale_gap_days: int) -> None:
    """REQ-FIX-WLT-006: a price older than the 7-day window is not used for reprice."""
    s = _session()
    a = _add_account(s)
    target = date(2026, 6, 30)
    s.add(
        PositionSnapshot(
            account_id=a.id,
            as_of=datetime(2026, 6, 1),
            symbol="STALE",
            quantity=Decimal("10"),
            price=Decimal("100"),
            market_value=Decimal("1000.00"),
            source_file="t.csv",
            source_row_hash="h3",
            raw_data={},
        )
    )
    # Only a stale price 30 days before target → beyond the 7-day rollback window.
    s.add(HistoricalPrice(symbol="STALE", trade_date=date(2026, 5, 31), close=Decimal("999")))
    s.commit()

    state = _load_history_state(s)
    per_account = _per_account_value_at(s, target, history_state=state)
    # Falls back to the stored snapshot market_value (not the stale close).
    assert per_account[a.id]["market_value"] == Decimal("1000.00")
