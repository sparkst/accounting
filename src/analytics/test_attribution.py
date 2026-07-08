"""Tests for net-worth attribution (REQ-NWA-001)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.analytics.attribution import compute_networth_attribution
from src.models import brokerage as _b  # noqa: F401
from src.models import history as _h  # noqa: F401
from src.models import plaid as _p  # noqa: F401
from src.models.base import Base
from src.models.brokerage import Account, BrokerageTransaction, PositionSnapshot
from src.models.enums import (
    AccountType,
    Broker,
    BrokerageTxStatus,
    CanonicalAction,
    CashFlowType,
    Entity,
)
from src.models.history import AccountBalanceSnapshot


def _session() -> Any:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _acct(s: Any, number: str) -> Account:
    a = Account(
        broker=Broker.SCHWAB.value, account_number=number,
        account_type=AccountType.TAXABLE.value, entity=Entity.PERSONAL.value,
    )
    s.add(a)
    s.flush()
    return a


def test_attribution_decomposes_and_ties_out() -> None:
    """REQ-NWA-001: a deposit + price move + newly-tracked account decompose so
    market + flows + coverage ≡ ΔNW, and each component recovers its value."""
    s = _session()
    start = date(2026, 1, 1)
    end = date(2026, 3, 1)

    # Account A: present at start (balance 1000), grows to 1200 by end (price move).
    a = _acct(s, "A")
    s.add(AccountBalanceSnapshot(
        account_id=a.id, raw_account_name="a", as_of=date(2026, 1, 1),
        balance=Decimal("1000.00"), source="x", source_row_hash="a1",
    ))
    s.add(AccountBalanceSnapshot(
        account_id=a.id, raw_account_name="a", as_of=date(2026, 2, 15),
        balance=Decimal("1200.00"), source="x", source_row_hash="a2",
    ))
    # A external deposit of 300 inside the window.
    s.add(BrokerageTransaction(
        account_id=a.id, trade_date=date(2026, 2, 1), action="Deposit",
        canonical_action=CanonicalAction.TRANSFER.value, amount=Decimal("300.00"),
        cash_flow_type=CashFlowType.EXTERNAL_IN.value,
        status=BrokerageTxStatus.IMPORTED.value, source_file="f",
        source_row_hash="tx1", raw_data={},
    ))
    # Account B: newly tracked mid-window (first snapshot 2026-02-10, value 500).
    b = _acct(s, "B")
    s.add(AccountBalanceSnapshot(
        account_id=b.id, raw_account_name="b", as_of=date(2026, 2, 10),
        balance=Decimal("500.00"), source="x", source_row_hash="b1",
    ))
    s.commit()

    r = compute_networth_attribution(s, start, end)
    # NW(start) = A only = 1000. NW(end) = A 1200 + B 500 = 1700. ΔNW = 700.
    assert r.nw_start == Decimal("1000.00")
    assert r.nw_end == Decimal("1700.00")
    assert r.delta_nw == Decimal("700.00")
    # Flows = +300 (the deposit). Coverage = +500 (B newly tracked, first value).
    assert r.net_flows == Decimal("300.00")
    assert r.coverage_change == Decimal("500.00")
    # Market = ΔNW − flows − coverage = 700 − 300 − 500 = −100.
    assert r.market_effect == Decimal("-100.00")
    # Identity holds.
    assert r.market_effect + r.net_flows + r.coverage_change == r.delta_nw
    assert r.flow_tx_count == 1
    assert r.new_account_count == 1


def test_weekly_line_format() -> None:
    """REQ-NWA-001 / REQ-WBR-002: weekly line tie-out string."""
    s = _session()
    a = _acct(s, "A")
    s.add(AccountBalanceSnapshot(
        account_id=a.id, raw_account_name="a", as_of=date(2026, 1, 1),
        balance=Decimal("1000.00"), source="x", source_row_hash="a1",
    ))
    s.add(PositionSnapshot(
        account_id=a.id, as_of=datetime(2026, 3, 1), symbol="ZZZ",
        quantity=Decimal("1"), market_value=Decimal("1500.00"),
        source_file="f", source_row_hash="p1", raw_data={},
    ))
    s.commit()
    r = compute_networth_attribution(s, date(2026, 1, 1), date(2026, 3, 1))
    line = r.format_weekly_line()
    assert line.startswith("NW Δ $")
    assert "market $" in line and "flows $" in line and "coverage $" in line
