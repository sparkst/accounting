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
from src.models.history import AccountBalanceSnapshot, ExpectedAccount


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


def test_attribution_dropped_account_subtracts_start_value() -> None:
    """REQ-NWA-001 (P1-c3d): an account present at start with no snapshot by
    end (closed/stale) reduces coverage_change by its start value, is counted
    in dropped_account_count, and the M+F+C identity still holds."""
    s = _session()
    start = date(2026, 1, 1)
    end = date(2026, 3, 1)

    # Account A: present throughout, flat value (no market move, no flows).
    a = _acct(s, "A")
    s.add(AccountBalanceSnapshot(
        account_id=a.id, raw_account_name="a", as_of=date(2026, 1, 1),
        balance=Decimal("1000.00"), source="x", source_row_hash="a1",
    ))
    s.add(AccountBalanceSnapshot(
        account_id=a.id, raw_account_name="a", as_of=date(2026, 3, 1),
        balance=Decimal("1000.00"), source="x", source_row_hash="a2",
    ))
    # Account C: present at start (400), closed mid-window (2026-02-01) so it
    # drops out of coverage by end even though its balance snapshot would
    # otherwise forward-fill indefinitely.
    c = _acct(s, "C")
    s.add(AccountBalanceSnapshot(
        account_id=c.id, raw_account_name="c", as_of=date(2026, 1, 1),
        balance=Decimal("400.00"), source="x", source_row_hash="c1",
    ))
    s.add(ExpectedAccount(
        institution="test", account_name="C", status="closed", source="test",
        resolved_account_id=c.id, updated_at=datetime(2026, 2, 1),
    ))
    s.commit()

    r = compute_networth_attribution(s, start, end)
    # NW(start) = A 1000 + C 400 = 1400. NW(end) = A 1000 only = 1000.
    assert r.nw_start == Decimal("1400.00")
    assert r.nw_end == Decimal("1000.00")
    assert r.delta_nw == Decimal("-400.00")
    assert r.net_flows == Decimal("0.00")
    # Coverage is reduced by C's start value (no offsetting flow for C).
    assert r.coverage_change == Decimal("-400.00")
    assert r.dropped_account_count == 1
    assert r.new_account_count == 0
    # Market is fully explained by the drop-out, not misattributed as a price move.
    assert r.market_effect == Decimal("0.00")
    assert r.market_effect + r.net_flows + r.coverage_change == r.delta_nw


def test_attribution_new_account_nets_in_window_funding_from_coverage() -> None:
    """REQ-NWA-001 (P2-001 / P2-att1): a newly-tracked account that is also
    externally funded within the same window must not have those dollars
    double-counted between flows and coverage."""
    s = _session()
    start = date(2026, 1, 1)
    end = date(2026, 3, 1)

    # Account A: present at start (1000), unchanged by end (isolates market
    # effect for account A at 0 so any misattribution shows up cleanly).
    a = _acct(s, "A")
    s.add(AccountBalanceSnapshot(
        account_id=a.id, raw_account_name="a", as_of=date(2026, 1, 1),
        balance=Decimal("1000.00"), source="x", source_row_hash="a1",
    ))
    s.add(AccountBalanceSnapshot(
        account_id=a.id, raw_account_name="a", as_of=date(2026, 3, 1),
        balance=Decimal("1000.00"), source="x", source_row_hash="a2",
    ))
    # Account B: newly tracked mid-window (first snapshot 2026-02-10, 500),
    # fully explained by a same-day external_in of 500 — no real price move.
    b = _acct(s, "B")
    s.add(AccountBalanceSnapshot(
        account_id=b.id, raw_account_name="b", as_of=date(2026, 2, 10),
        balance=Decimal("500.00"), source="x", source_row_hash="b1",
    ))
    s.add(BrokerageTransaction(
        account_id=b.id, trade_date=date(2026, 2, 10), action="Deposit",
        canonical_action=CanonicalAction.TRANSFER.value, amount=Decimal("500.00"),
        cash_flow_type=CashFlowType.EXTERNAL_IN.value,
        status=BrokerageTxStatus.IMPORTED.value, source_file="f",
        source_row_hash="tx-b", raw_data={},
    ))
    s.commit()

    r = compute_networth_attribution(s, start, end)
    # NW(start) = A 1000. NW(end) = A 1000 + B 500 = 1500. ΔNW = 500.
    assert r.delta_nw == Decimal("500.00")
    assert r.net_flows == Decimal("500.00")
    # Coverage nets B's first value against its own in-window flow: 500 - 500 = 0.
    assert r.coverage_change == Decimal("0.00")
    assert r.new_account_count == 1
    # No real market movement anywhere — the funding fully explains ΔNW.
    assert r.market_effect == Decimal("0.00")
    assert r.market_effect + r.net_flows + r.coverage_change == r.delta_nw


def test_attribution_new_account_only_nets_pre_first_snapshot_flows() -> None:
    """REQ-NWA-001 (P2-401): a newly-tracked account's coverage contribution
    must net only the flows dated on/before its first snapshot (the ones
    actually baked into first_val) — flows dated *after* the first snapshot
    but still inside the window belong solely to `flows` and must not be
    subtracted from coverage a second time, or market_effect is manufactured
    out of thin air."""
    s = _session()
    start = date(2026, 1, 1)
    end = date(2026, 3, 1)

    # Account A: present throughout, unchanged (isolates the effect to B).
    a = _acct(s, "A")
    s.add(AccountBalanceSnapshot(
        account_id=a.id, raw_account_name="a", as_of=date(2026, 1, 1),
        balance=Decimal("1000.00"), source="x", source_row_hash="a1",
    ))
    s.add(AccountBalanceSnapshot(
        account_id=a.id, raw_account_name="a", as_of=date(2026, 3, 1),
        balance=Decimal("1000.00"), source="x", source_row_hash="a2",
    ))
    # Account B: newly tracked mid-window. First snapshot 2026-02-10 at 500,
    # which bakes in the pre-first 300 deposit (2026-02-05). A second, later
    # deposit of 200 (2026-02-15) lands after the first snapshot but is still
    # in-window; by end (2026-03-01) the balance is 700 with zero price move
    # (500 + 200 = 700 exactly).
    b = _acct(s, "B")
    s.add(AccountBalanceSnapshot(
        account_id=b.id, raw_account_name="b", as_of=date(2026, 2, 10),
        balance=Decimal("500.00"), source="x", source_row_hash="b1",
    ))
    s.add(AccountBalanceSnapshot(
        account_id=b.id, raw_account_name="b", as_of=date(2026, 3, 1),
        balance=Decimal("700.00"), source="x", source_row_hash="b2",
    ))
    s.add(BrokerageTransaction(
        account_id=b.id, trade_date=date(2026, 2, 5), action="Deposit",
        canonical_action=CanonicalAction.TRANSFER.value, amount=Decimal("300.00"),
        cash_flow_type=CashFlowType.EXTERNAL_IN.value,
        status=BrokerageTxStatus.IMPORTED.value, source_file="f",
        source_row_hash="tx-b-pre", raw_data={},
    ))
    s.add(BrokerageTransaction(
        account_id=b.id, trade_date=date(2026, 2, 15), action="Deposit",
        canonical_action=CanonicalAction.TRANSFER.value, amount=Decimal("200.00"),
        cash_flow_type=CashFlowType.EXTERNAL_IN.value,
        status=BrokerageTxStatus.IMPORTED.value, source_file="f",
        source_row_hash="tx-b-post", raw_data={},
    ))
    s.commit()

    r = compute_networth_attribution(s, start, end)
    # NW(start) = A 1000. NW(end) = A 1000 + B 700 = 1700. ΔNW = 700.
    assert r.delta_nw == Decimal("700.00")
    # Flows = both deposits: 300 + 200 = 500.
    assert r.net_flows == Decimal("500.00")
    # Coverage nets only the pre-first flow against B's first value:
    # 500 - 300 = 200. The post-first 200 stays out of coverage.
    assert r.coverage_change == Decimal("200.00")
    assert r.new_account_count == 1
    # No real market movement — before the fix this collapsed to +200 because
    # the post-first flow was double-subtracted from coverage.
    assert r.market_effect == Decimal("0.00")
    assert r.market_effect + r.net_flows + r.coverage_change == r.delta_nw


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
