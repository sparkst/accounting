"""Tests for the brokerage summary report.

REQ-005a..g visibility (Option 1 — CLI sanity-check report).
TDD: every test references the not-yet-implemented function and is asserted
RED before implementation begins. See proposals/brokerage-visibility/PLAN-option1.md.
"""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.models.base import Base
from src.models.brokerage import (
    Account,
    BrokerageTransaction,
    PositionSnapshot,
    RealizedGainLoss,
)
from src.models.enums import (
    AccountType,
    Broker,
    BrokerageTxStatus,
    CanonicalAction,
    Entity,
    GainLossTerm,
)
from src.models.history import AccountBalanceSnapshot

# ── Fixture ──────────────────────────────────────────────────────────────


# Pin "today" so date-relative seeds are deterministic across runs.
TODAY = date(2026, 5, 6)


@pytest.fixture()
def session(monkeypatch: pytest.MonkeyPatch) -> Session:
    """In-memory SQLite session pre-seeded with the canonical fixture.

    See PLAN-option1.md TASK-01 for the seed schedule.
    """
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    s = SessionLocal()

    # Pin "today" everywhere the report code reads it from.
    import src.reports.brokerage_summary as report_mod

    monkeypatch.setattr(report_mod, "_today", lambda: TODAY, raising=False)

    _seed_fixture(s)
    yield s
    s.close()


def _ts(d: date, hour: int = 12) -> datetime:
    return datetime(d.year, d.month, d.day, hour, 0, 0)


def _seed_fixture(s: Session) -> None:
    """Seed all 10 accounts + 7 transactions + 16 snapshots + 5 realized lots."""
    today_dt = _ts(TODAY)
    today_minus_30 = _ts(TODAY - timedelta(days=30))
    today_minus_10 = _ts(TODAY - timedelta(days=10))

    accounts: dict[str, Account] = {}

    def _mk_acct(
        key: str,
        broker: Broker,
        account_number: str,
        account_type: AccountType,
        entity: Entity,
        tax_sheltered: bool = False,
        is_plan_wrapper: bool = False,
        parent_key: str | None = None,
        account_name: str | None = None,
    ) -> Account:
        a = Account(
            broker=broker.value,
            account_number=account_number,
            account_name=account_name,
            account_type=account_type.value,
            entity=entity.value,
            tax_sheltered=tax_sheltered,
            is_plan_wrapper=is_plan_wrapper,
            parent_account_id=accounts[parent_key].id if parent_key else None,
        )
        s.add(a)
        s.flush()
        accounts[key] = a
        return a

    _mk_acct("A1", Broker.FIDELITY, "Z23257759", AccountType.TOD, Entity.PERSONAL)
    _mk_acct(
        "A2",
        Broker.SCHWAB,
        "12345678",
        AccountType.TRAD_IRA,
        Entity.SPARKRY,
        tax_sheltered=True,
    )
    _mk_acct("A3", Broker.ETRADE, "87654321", AccountType.TAXABLE, Entity.BLACKLINE)
    _mk_acct(
        "A4",
        Broker.FIDELITY,
        "89766",
        AccountType.K401,
        Entity.PERSONAL,
        tax_sheltered=True,
        is_plan_wrapper=True,
        account_name="Fidelity 401k Plan",
    )
    _mk_acct(
        "A5",
        Broker.FIDELITY,
        "653373015",
        AccountType.BROKERAGELINK,
        Entity.PERSONAL,
        tax_sheltered=True,
        parent_key="A4",
    )
    _mk_acct(
        "A6", Broker.VANGUARD, "32628019", AccountType.TRAD_IRA, Entity.PERSONAL,
        tax_sheltered=True,
    )
    _mk_acct("A7", Broker.SCHWAB, "99999999", AccountType.TAXABLE, Entity.PERSONAL)
    _mk_acct("A8", Broker.VANGUARD, "DUPTEST", AccountType.TAXABLE, Entity.PERSONAL)
    _mk_acct("A9", Broker.VANGUARD, "NULLDUP", AccountType.TAXABLE, Entity.PERSONAL)
    _mk_acct("A10", Broker.ETRADE, "BADSYM", AccountType.TAXABLE, Entity.PERSONAL)

    s.commit()

    # ── Position snapshots ──────────────────────────────────────────────
    def _snap(
        acct_key: str,
        as_of: datetime,
        symbol: str | None,
        market_value: Decimal | None,
        description: str | None = None,
        quantity: Decimal | None = None,
        source_row_hash: str | None = None,
    ) -> None:
        ps = PositionSnapshot(
            account_id=accounts[acct_key].id,
            as_of=as_of,
            symbol=symbol,
            description=description,
            quantity=quantity if quantity is not None else Decimal("1"),
            market_value=market_value,
            source_file="test.csv",
            source_row_hash=source_row_hash or f"{acct_key}_{symbol}_{as_of.isoformat()}",
            raw_data={"seed": True},
        )
        s.add(ps)

    # A1: Fidelity TOD — AAPL + FDRXX** (cash sleeve with ** suffix)
    _snap("A1", today_dt, "AAPL", Decimal("10000"))
    _snap("A1", today_dt, "FDRXX**", Decimal("200"), description="HELD IN MONEY MARKET")

    # A2: Schwab IRA — 6 positions
    _snap("A2", today_dt, "VTI", Decimal("5000"))
    _snap("A2", today_dt, "VWO", Decimal("3000"))
    _snap("A2", today_dt, "VOO", Decimal("2500"))
    _snap("A2", today_dt, "BND", Decimal("1000"))
    _snap("A2", today_dt, "SWTXX", Decimal("300"), description="SCHWAB MUNICIPAL MONEY INV")
    _snap("A2", today_dt, "SPAXX", Decimal("200"))

    # A3: E*TRADE — 6 latest, 2 prior, 1 zero-qty
    _snap("A3", today_dt, "MSFT", Decimal("4000"))
    _snap("A3", today_dt, "GOOGL", Decimal("3500"))
    _snap("A3", today_dt, "TSLA", Decimal("2000"))
    _snap("A3", today_dt, "VMFXX", Decimal("100"))
    _snap("A3", today_dt, "VMSXX", Decimal("50"))
    _snap("A3", today_dt, None, Decimal("500"), description="Vanguard 500 Index Portfolio")
    _snap("A3", today_dt, "OLDPOS", Decimal("0"), quantity=Decimal("0"))
    _snap("A3", today_minus_30, "MSFT", Decimal("3500"))
    _snap("A3", today_minus_30, "GOOGL", Decimal("3000"))

    # A4: plan wrapper — NULL-symbol BROKERAGELINK aggregate
    _snap("A4", today_dt, None, Decimal("50000"), description="BROKERAGELINK")

    # A5: child of A4 — real positions
    _snap("A5", today_dt, "VTSAX", Decimal("40000"))
    _snap("A5", today_dt, "VTIAX", Decimal("10000"))

    # A6: zero snapshots — intentionally none

    # A7: stale (today-10)
    _snap("A7", today_minus_10, "JNK", Decimal("500"))

    # A8: DUPTEST — two rows for (MGK, today, $63166.16) with different hashes
    _snap("A8", today_dt, "MGK", Decimal("63166.16"), source_row_hash="A8_MGK_dup1")
    _snap("A8", today_dt, "MGK", Decimal("63166.16"), source_row_hash="A8_MGK_dup2")

    # A9: NULLDUP — three NULL-symbol rows, different descriptions, same as_of
    _snap("A9", today_dt, None, Decimal("30000"), description="Fund A", source_row_hash="A9_FundA")
    _snap("A9", today_dt, None, Decimal("20000"), description="Fund B", source_row_hash="A9_FundB")
    _snap("A9", today_dt, None, Decimal("10000"), description="Fund C", source_row_hash="A9_FundC")

    # A10: BADSYM — TOTAL summary row + Generated-at footer row (defensive filter targets)
    _snap("A10", today_dt, "TOTAL", Decimal("3000000"))
    _snap("A10", today_dt, "Generated at May 4 2026 02:47 PM ET", Decimal("0"),
          source_row_hash="A10_generated")

    s.commit()

    # ── Transactions ────────────────────────────────────────────────────
    def _tx(
        key: str,
        acct_key: str,
        trade_date: date,
        canonical_action: CanonicalAction,
        action: str,
        symbol: str | None = None,
        quantity: Decimal | None = None,
        amount: Decimal | None = None,
        status: BrokerageTxStatus = BrokerageTxStatus.IMPORTED,
        is_synthetic: bool = False,
        paired_id: str | None = None,
    ) -> BrokerageTransaction:
        tx = BrokerageTransaction(
            account_id=accounts[acct_key].id,
            trade_date=trade_date,
            action=action,
            canonical_action=canonical_action.value,
            symbol=symbol,
            quantity=quantity,
            amount=amount,
            status=status.value,
            is_synthetic=is_synthetic,
            paired_transaction_id=paired_id,
            source_file="test.csv",
            source_row_hash=f"tx_{key}",
            raw_data={"seed": True},
        )
        s.add(tx)
        s.flush()
        return tx

    _tx("T1", "A1", TODAY - timedelta(days=1), CanonicalAction.BUY, "BUY", "AAPL", Decimal("100"), Decimal("-15000"))
    _tx("T2", "A1", TODAY - timedelta(days=2), CanonicalAction.SELL, "SELL", "AAPL", Decimal("50"), Decimal("8000"))
    t3 = _tx("T3", "A2", TODAY - timedelta(days=3), CanonicalAction.DIVIDEND_ORDINARY, "Dividend", "VTI", None, Decimal("25"))
    t4 = _tx("T4", "A2", TODAY - timedelta(days=3), CanonicalAction.REINVEST, "Reinvest", "VTI", Decimal("0.5"), Decimal("-25"), paired_id=t3.id)
    # Set the reciprocal pair
    t3.paired_transaction_id = t4.id
    _tx("T5", "A2", TODAY - timedelta(days=30), CanonicalAction.BUY, "BUY", "ZZZ", Decimal("10"), Decimal("-100"))
    _tx("T6", "A3", TODAY - timedelta(days=1), CanonicalAction.DIVIDEND_ORDINARY, "Dividend", "MSFT", None, Decimal("12"), status=BrokerageTxStatus.REJECTED)
    _tx("T7", "A3", TODAY - timedelta(days=1), CanonicalAction.SELL, "SELL", "OLDPOS", Decimal("100"), Decimal("0"))

    s.commit()

    # ── Realized G/L ────────────────────────────────────────────────────
    def _gl(
        acct_key: str,
        symbol: str,
        closed: date,
        opened: date | None,
        gain_loss: Decimal,
        lt_gain_loss: Decimal | None = None,
        st_gain_loss: Decimal | None = None,
        term: GainLossTerm | None = None,
        wash_sale: bool = False,
        disallowed_loss: Decimal | None = None,
        suffix: str = "",
    ) -> None:
        rgl = RealizedGainLoss(
            account_id=accounts[acct_key].id,
            symbol=symbol,
            closed_date=closed,
            opened_date=opened,
            quantity=Decimal("1"),
            proceeds=Decimal("100") if gain_loss >= 0 else Decimal("0"),
            cost_basis=Decimal("100") - gain_loss if gain_loss >= 0 else Decimal("100"),
            gain_loss=gain_loss,
            lt_gain_loss=lt_gain_loss,
            st_gain_loss=st_gain_loss,
            term=term.value if term else None,
            wash_sale=wash_sale,
            disallowed_loss=disallowed_loss,
            source_file="test.csv",
            source_row_hash=f"gl_{acct_key}_{symbol}_{closed.isoformat()}{suffix}",
            raw_data={"seed": True},
        )
        s.add(rgl)

    # 2024 lots
    _gl("A1", "AAPL", date(2024, 6, 15), date(2024, 1, 15), Decimal("500"),
        st_gain_loss=Decimal("500"), term=GainLossTerm.SHORT)
    _gl("A2", "VTI", date(2024, 12, 20), date(2023, 1, 15), Decimal("1200"),
        lt_gain_loss=Decimal("1200"), term=GainLossTerm.LONG)
    _gl("A2", "SPLIT_LOT", date(2024, 8, 1), date(2024, 2, 1), Decimal("900"),
        lt_gain_loss=Decimal("700"), st_gain_loss=Decimal("200"), term=GainLossTerm.LONG)

    # 2024 date-inference lot: term=None, lt/st=None, opened/closed >365 days → LT
    _gl("A1", "INFDATE", date(2024, 1, 2), date(2022, 1, 1), Decimal("300"),
        suffix="_dateinfer")

    # 2025 lots
    _gl("A2", "WASH", date(2025, 1, 10), date(2024, 11, 15), Decimal("-100"),
        term=GainLossTerm.SHORT, wash_sale=True, disallowed_loss=Decimal("50"))
    _gl("A3", "ANCIENT", date(2025, 3, 5), None, Decimal("50"))

    s.commit()


@pytest.fixture()
def empty_session(monkeypatch: pytest.MonkeyPatch) -> Session:
    """Session with all tables but no data. _today() pinned to TODAY."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    s = SessionLocal()
    import src.reports.brokerage_summary as report_mod
    monkeypatch.setattr(report_mod, "_today", lambda: TODAY, raising=False)
    yield s
    s.close()


# ── Helper: assert read-only invariant ─────────────────────────────────


def _assert_clean(s: Session) -> None:
    """Read-only invariant: a function may not mutate the session."""
    assert not s.dirty, f"session.dirty: {s.dirty}"
    assert not s.new, f"session.new: {s.new}"
    assert not s.deleted, f"session.deleted: {s.deleted}"


# ───────────────────────────────────────────────────────────────────────
# P2-A: _is_cash_symbol / _is_suspect_symbol unit tests
# ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("symbol,expected", [
    ("CASH", True),
    ("cash", True),   # case-normalised
    ("SPAXX", True),
    ("FDRXX", True),
    ("FDRXX**", True),   # ** suffix on Fidelity
    ("FCASH", True),
    ("VMFXX", True),
    ("VMSXX", True),
    ("SWVXX", True),
    ("SWLXX", True),
    ("SWTXX", True),
    ("MMDA1", True),
    ("AAPL", False),
    ("VTI", False),
    (None, False),
    ("", False),
])
def test_is_cash_symbol_parametrized(symbol: str | None, expected: bool) -> None:
    from src.reports.brokerage_summary import _is_cash_symbol
    assert _is_cash_symbol(symbol) is expected


@pytest.mark.parametrize("symbol,expected", [
    ("TOTAL", True),
    ("total", True),   # upper() comparison
    ("Generated at May 4 2026 02:47 PM ET", True),
    ("Generated ", True),
    ("AAPL", False),
    ("VTI", False),
    (None, False),
    ("", False),
    ("TotalReturn", False),   # 'total' is EXACT not prefix
])
def test_is_suspect_symbol_parametrized(symbol: str | None, expected: bool) -> None:
    from src.reports.brokerage_summary import _is_suspect_symbol
    assert _is_suspect_symbol(symbol) is expected


# ───────────────────────────────────────────────────────────────────────
# TASK-02: _latest_position_snapshots
# ───────────────────────────────────────────────────────────────────────


def test_latest_position_snapshots_dedupes_same_account_symbol_as_of(session: Session) -> None:
    from src.reports.brokerage_summary import _latest_position_snapshots

    subq = _latest_position_snapshots(session)
    rows = session.query(PositionSnapshot).join(
        subq, PositionSnapshot.id == subq.c.id
    ).all()

    # A8 DUPTEST (MGK) appears exactly once.
    a8_mgk = [r for r in rows if r.symbol == "MGK"]
    assert len(a8_mgk) == 1, f"expected 1 MGK row, got {len(a8_mgk)}"
    assert a8_mgk[0].market_value == Decimal("63166.16")
    _assert_clean(session)


def test_latest_position_snapshots_distinguishes_null_symbol_by_description(session: Session) -> None:
    from src.reports.brokerage_summary import _latest_position_snapshots

    subq = _latest_position_snapshots(session)
    rows = session.query(PositionSnapshot).join(
        subq, PositionSnapshot.id == subq.c.id
    ).all()

    # A9 NULLDUP: 3 NULL-symbol rows with different descriptions all present.
    a9_descriptions = {r.description for r in rows if r.description in {"Fund A", "Fund B", "Fund C"}}
    assert a9_descriptions == {"Fund A", "Fund B", "Fund C"}
    _assert_clean(session)


def test_latest_position_snapshots_filters_TOTAL(session: Session) -> None:
    from src.reports.brokerage_summary import _latest_position_snapshots

    subq = _latest_position_snapshots(session)
    rows = session.query(PositionSnapshot).join(
        subq, PositionSnapshot.id == subq.c.id
    ).all()

    assert not any(r.symbol == "TOTAL" for r in rows), "TOTAL row leaked through"
    _assert_clean(session)


def test_latest_position_snapshots_filters_generated_at(session: Session) -> None:
    """P1-E: 'Generated at ...' rows are excluded from latest snapshots."""
    from src.reports.brokerage_summary import _latest_position_snapshots

    subq = _latest_position_snapshots(session)
    rows = session.query(PositionSnapshot).join(
        subq, PositionSnapshot.id == subq.c.id
    ).all()

    assert not any(
        r.symbol is not None and r.symbol.startswith("Generated ")
        for r in rows
    ), "Generated-at row leaked through latest snapshot filter"
    _assert_clean(session)


def test_latest_position_snapshots_null_null_sentinel(session: Session) -> None:
    """P1-B: a snap with symbol=None AND description=None still appears in results
    and contributes to compute_net_worth total (NULL/NULL join sentinel fix)."""
    from src.models.brokerage import Account
    from src.reports.brokerage_summary import _latest_snapshot_rows, compute_net_worth

    accounts = session.query(Account).all()
    a3_id = next(a.id for a in accounts if a.account_number == "87654321")  # A3

    snap_null = PositionSnapshot(
        account_id=a3_id,
        as_of=datetime(TODAY.year, TODAY.month, TODAY.day, 14, 0, 0),
        symbol=None,
        description=None,
        quantity=Decimal("1"),
        market_value=Decimal("99"),
        source_file="test.csv",
        source_row_hash="p1b_null_null",
        raw_data={},
    )
    session.add(snap_null)
    session.commit()
    session.expire_all()

    rows = _latest_snapshot_rows(session)
    null_null = [r for r in rows if r.symbol is None and r.description is None and r.account_id == a3_id]
    assert len(null_null) >= 1, "NULL/NULL snap missing from _latest_snapshot_rows"

    nw = compute_net_worth(session)
    # A3 is blackline entity, not plan_wrapper — its $99 row should be in the total
    # (the pre-existing A3 total is $10150, add $99 → $10249).
    assert nw["total"] >= Decimal("99"), "NULL/NULL snap not contributing to net_worth total"


def test_latest_position_snapshots_no_coalesce_collision(session: Session) -> None:
    """P1-H: symbol='UNIQUE_X' and (symbol=None, description='UNIQUE_X') must
    both appear — they share the same COALESCE value but differ by has_symbol flag."""
    from src.models.brokerage import Account
    from src.reports.brokerage_summary import _latest_snapshot_rows

    # Add two extra rows to the existing session.
    accounts = session.query(Account).all()
    a1_id = next(a.id for a in accounts if a.account_number == "Z23257759")  # A1
    as_of = datetime(TODAY.year, TODAY.month, TODAY.day, 14, 0, 0)

    snap1 = PositionSnapshot(
        account_id=a1_id, as_of=as_of, symbol="UNIQUE_X", description=None,
        quantity=Decimal("1"), market_value=Decimal("10"),
        source_file="test.csv", source_row_hash="p1h_sym", raw_data={},
    )
    snap2 = PositionSnapshot(
        account_id=a1_id, as_of=as_of, symbol=None, description="UNIQUE_X",
        quantity=Decimal("1"), market_value=Decimal("20"),
        source_file="test.csv", source_row_hash="p1h_desc", raw_data={},
    )
    session.add(snap1)
    session.add(snap2)
    session.commit()
    session.expire_all()

    rows = _latest_snapshot_rows(session)
    unique_x_sym = [r for r in rows if r.symbol == "UNIQUE_X"]
    unique_x_desc = [r for r in rows if r.symbol is None and r.description == "UNIQUE_X"]
    assert len(unique_x_sym) == 1, "symbol='UNIQUE_X' row missing"
    assert len(unique_x_desc) == 1, "description='UNIQUE_X' row missing"


def test_latest_position_snapshots_returns_only_latest_for_dual_dated(session: Session) -> None:
    from src.reports.brokerage_summary import _latest_position_snapshots

    subq = _latest_position_snapshots(session)
    rows = session.query(PositionSnapshot).join(
        subq, PositionSnapshot.id == subq.c.id
    ).all()

    # A3 has MSFT both today and today-30; only the today snapshot ($4000) returned.
    a3_msft = [r for r in rows if r.symbol == "MSFT"]
    assert len(a3_msft) == 1
    assert a3_msft[0].market_value == Decimal("4000")
    _assert_clean(session)


# ───────────────────────────────────────────────────────────────────────
# TASK-03: compute_net_worth
# ───────────────────────────────────────────────────────────────────────


def test_compute_net_worth_excludes_plan_wrapper(session: Session) -> None:
    from src.reports.brokerage_summary import compute_net_worth

    nw = compute_net_worth(session)
    # A4 wrapper $50,000 NOT in totals; A5 child $50,000 IS in totals.
    # Naive sum would include both = $50k extra.
    assert nw["plan_wrapper_excluded_count"] == 1
    _assert_clean(session)


def test_compute_net_worth_total(session: Session) -> None:
    from src.reports.brokerage_summary import compute_net_worth

    nw = compute_net_worth(session)
    expected_total = (
        Decimal("10000") + Decimal("200")  # A1
        + Decimal("5000") + Decimal("3000") + Decimal("2500") + Decimal("1000") + Decimal("300") + Decimal("200")  # A2
        + Decimal("4000") + Decimal("3500") + Decimal("2000") + Decimal("100") + Decimal("50") + Decimal("500")  # A3 (OLDPOS qty=0 excluded)
        # A4 wrapper excluded
        + Decimal("40000") + Decimal("10000")  # A5
        # A6 no snapshots
        + Decimal("500")  # A7 stale but still counts
        + Decimal("63166.16")  # A8 deduplicated
        + Decimal("30000") + Decimal("20000") + Decimal("10000")  # A9 three distinct NULL rows
        # A10 TOTAL filtered
    )
    assert nw["total"] == expected_total, f"got {nw['total']}, expected {expected_total}"
    _assert_clean(session)


def test_compute_net_worth_by_broker(session: Session) -> None:
    from src.reports.brokerage_summary import compute_net_worth

    nw = compute_net_worth(session)
    assert nw["by_broker"]["fidelity"] == Decimal("60200")  # A1+A5; A4 excluded
    assert nw["by_broker"]["schwab"] == Decimal("12500")  # A2 + A7
    assert nw["by_broker"]["etrade"] == Decimal("10150")  # A3 (OLDPOS excluded, A10 TOTAL excluded)
    assert nw["by_broker"]["vanguard"] == Decimal("123166.16")  # A8 + A9
    _assert_clean(session)


def test_compute_net_worth_by_entity(session: Session) -> None:
    from src.reports.brokerage_summary import compute_net_worth

    nw = compute_net_worth(session)
    # personal: A1 + A5 + A7 + A8 + A9
    assert nw["by_entity"]["personal"] == Decimal("60200") + Decimal("500") + Decimal("63166.16") + Decimal("60000")
    assert nw["by_entity"]["sparkry"] == Decimal("12000")  # A2
    assert nw["by_entity"]["blackline"] == Decimal("10150")  # A3
    _assert_clean(session)


def test_compute_net_worth_zero_snapshot_count(session: Session) -> None:
    from src.reports.brokerage_summary import compute_net_worth

    nw = compute_net_worth(session)
    assert nw["zero_snapshot_account_count"] == 1  # A6
    _assert_clean(session)


def test_compute_net_worth_as_of_range(session: Session) -> None:
    from src.reports.brokerage_summary import compute_net_worth

    nw = compute_net_worth(session)
    assert nw["as_of_min"] == TODAY - timedelta(days=10)  # A7
    assert nw["as_of_max"] == TODAY
    _assert_clean(session)


def test_compute_net_worth_empty_db(empty_session: Session) -> None:
    from src.reports.brokerage_summary import compute_net_worth

    nw = compute_net_worth(empty_session)
    assert nw["total"] == Decimal("0")
    assert nw["by_broker"] == {}
    assert nw["by_entity"] == {}
    assert nw["as_of_min"] is None
    assert nw["as_of_max"] is None
    assert nw["zero_snapshot_account_count"] == 0
    assert nw["plan_wrapper_excluded_count"] == 0
    _assert_clean(empty_session)


# ───────────────────────────────────────────────────────────────────────
# TASK-04: get_account_summary
# ───────────────────────────────────────────────────────────────────────


def test_get_account_summary_includes_all_accounts(session: Session) -> None:
    from src.reports.brokerage_summary import get_account_summary

    rows = get_account_summary(session)
    assert len(rows) == 10
    _assert_clean(session)


def test_get_account_summary_masks_account_numbers(session: Session) -> None:
    from src.reports.brokerage_summary import _mask_account_number, get_account_summary

    rows = get_account_summary(session)
    a1 = next(r for r in rows if "7759" in r["account_number_masked"])
    assert a1["account_number_masked"] == "****7759"
    assert _mask_account_number("12") == "****"
    assert _mask_account_number("Z23257759") == "****7759"
    _assert_clean(session)


def test_get_account_summary_dedup_accounts(session: Session) -> None:
    from src.reports.brokerage_summary import get_account_summary

    rows = get_account_summary(session)
    a8 = next(r for r in rows if r["account_number_masked"] == "****TEST")
    assert a8["market_value"] == Decimal("63166.16")
    _assert_clean(session)


def test_get_account_summary_zero_snapshot_account(session: Session) -> None:
    from src.reports.brokerage_summary import get_account_summary

    rows = get_account_summary(session)
    a6 = next(r for r in rows if r["account_number_masked"] == "****8019")
    assert a6["as_of"] is None
    assert a6["market_value"] == Decimal("0")
    _assert_clean(session)


def test_get_account_summary_flags_plan_wrapper(session: Session) -> None:
    from src.reports.brokerage_summary import get_account_summary

    rows = get_account_summary(session)
    a4 = next(r for r in rows if r["account_number_masked"] == "****9766")
    assert a4["is_plan_wrapper"] is True
    _assert_clean(session)


def test_get_account_summary_empty_db(empty_session: Session) -> None:
    from src.reports.brokerage_summary import get_account_summary

    assert get_account_summary(empty_session) == []
    _assert_clean(empty_session)


# ───────────────────────────────────────────────────────────────────────
# TASK-04b: tie-out
# ───────────────────────────────────────────────────────────────────────


def test_tie_out_net_worth_vs_account_summary(session: Session) -> None:
    from src.reports.brokerage_summary import compute_net_worth, get_account_summary

    nw = compute_net_worth(session)
    rows = get_account_summary(session)
    summed = sum(
        r["market_value"]
        for r in rows
        if r["as_of"] is not None and not r["is_plan_wrapper"]
    )
    assert nw["total"] == summed, f"nw={nw['total']}, summed={summed}"
    _assert_clean(session)


# ───────────────────────────────────────────────────────────────────────
# TASK-05: get_top_holdings
# ───────────────────────────────────────────────────────────────────────


def test_get_top_holdings_folds_cash_sleeves(session: Session) -> None:
    from src.reports.brokerage_summary import compute_net_worth, get_top_holdings

    nw = compute_net_worth(session)
    rows = get_top_holdings(session, net_worth_total=nw["total"], n=None)
    cash_rows = [r for r in rows if r.get("is_cash_sleeve")]
    assert len(cash_rows) == 1
    assert cash_rows[0]["total_market_value"] == (
        Decimal("200") + Decimal("300") + Decimal("200") + Decimal("100") + Decimal("50")
    )
    _assert_clean(session)


def test_get_top_holdings_excludes_TOTAL_and_wrapper(session: Session) -> None:
    from src.reports.brokerage_summary import compute_net_worth, get_top_holdings

    nw = compute_net_worth(session)
    rows = get_top_holdings(session, net_worth_total=nw["total"], n=None)
    assert not any(r["symbol"] == "TOTAL" for r in rows)
    # Wrapper's NULL-symbol BROKERAGELINK should NOT appear.
    assert not any(r.get("description") == "BROKERAGELINK" for r in rows)
    _assert_clean(session)


def test_get_top_holdings_dedupes_account(session: Session) -> None:
    from src.reports.brokerage_summary import compute_net_worth, get_top_holdings

    nw = compute_net_worth(session)
    rows = get_top_holdings(session, net_worth_total=nw["total"], n=None)
    mgk = [r for r in rows if r["symbol"] == "MGK"]
    assert len(mgk) == 1
    assert mgk[0]["total_market_value"] == Decimal("63166.16")
    _assert_clean(session)


def test_get_top_holdings_keeps_null_symbol_by_description(session: Session) -> None:
    from src.reports.brokerage_summary import compute_net_worth, get_top_holdings

    nw = compute_net_worth(session)
    rows = get_top_holdings(session, net_worth_total=nw["total"], n=None)
    descriptions = {r.get("description") for r in rows if r["symbol"] is None}
    # Three NULLDUP funds + Vanguard 500 Index Portfolio (A3) — A4 wrapper excluded
    assert "Fund A" in descriptions
    assert "Fund B" in descriptions
    assert "Fund C" in descriptions
    assert "Vanguard 500 Index Portfolio" in descriptions
    assert "BROKERAGELINK" not in descriptions
    _assert_clean(session)


def test_get_top_holdings_excludes_zero_quantity(session: Session) -> None:
    from src.reports.brokerage_summary import compute_net_worth, get_top_holdings

    nw = compute_net_worth(session)
    rows = get_top_holdings(session, net_worth_total=nw["total"], n=None)
    assert not any(r["symbol"] == "OLDPOS" for r in rows)
    _assert_clean(session)


def test_get_top_holdings_truncates(session: Session) -> None:
    from src.reports.brokerage_summary import compute_net_worth, get_top_holdings

    nw = compute_net_worth(session)
    rows = get_top_holdings(session, net_worth_total=nw["total"], n=3)
    assert len(rows) == 3
    _assert_clean(session)


def test_get_top_holdings_pct_sums_to_one_when_unbounded(session: Session) -> None:
    from src.reports.brokerage_summary import compute_net_worth, get_top_holdings

    nw = compute_net_worth(session)
    rows = get_top_holdings(session, net_worth_total=nw["total"], n=None)
    total_pct = sum(r["pct_of_net_worth"] for r in rows)
    assert abs(total_pct - Decimal("1")) < Decimal("0.01"), f"pct sum was {total_pct}"
    _assert_clean(session)


def test_get_top_holdings_empty_db(empty_session: Session) -> None:
    from src.reports.brokerage_summary import get_top_holdings

    assert get_top_holdings(empty_session, net_worth_total=Decimal("0"), n=10) == []
    _assert_clean(empty_session)


# ───────────────────────────────────────────────────────────────────────
# TASK-06: get_recent_transactions
# ───────────────────────────────────────────────────────────────────────


def test_get_recent_transactions_window(session: Session) -> None:
    from src.reports.brokerage_summary import get_recent_transactions

    rows = get_recent_transactions(session, days=14)
    symbols = [r["symbol"] for r in rows]
    assert "AAPL" in symbols  # T1 today-1 BUY
    assert "ZZZ" not in symbols  # T5 today-30 out of window
    _assert_clean(session)


def test_get_recent_transactions_excludes_rejected(session: Session) -> None:
    from src.reports.brokerage_summary import get_recent_transactions

    rows = get_recent_transactions(session, days=14)
    # T6 (REJECTED dividend on MSFT today-1) must not appear.
    rejected = [r for r in rows if r["symbol"] == "MSFT" and r["canonical_action"] == "dividend_ordinary"]
    assert rejected == []
    _assert_clean(session)


def test_get_recent_transactions_suppresses_reinvest_partner(session: Session) -> None:
    from src.reports.brokerage_summary import get_recent_transactions

    rows = get_recent_transactions(session, days=14)
    reinvests = [r for r in rows if r["canonical_action"] == "reinvest"]
    assert reinvests == []  # T4 suppressed
    dividends = [r for r in rows if r["canonical_action"] == "dividend_ordinary" and r["symbol"] == "VTI"]
    assert len(dividends) == 1  # T3 still shown
    _assert_clean(session)


def test_get_recent_transactions_wider_window(session: Session) -> None:
    from src.reports.brokerage_summary import get_recent_transactions

    rows = get_recent_transactions(session, days=60)
    assert any(r["symbol"] == "ZZZ" for r in rows)
    _assert_clean(session)


# ───────────────────────────────────────────────────────────────────────
# TASK-07: get_realized_gl_summary
# ───────────────────────────────────────────────────────────────────────


def test_realized_gl_2024_buckets(session: Session) -> None:
    from src.reports.brokerage_summary import get_realized_gl_summary

    summary = get_realized_gl_summary(session)
    by_year = summary["by_year"]
    assert by_year[2024]["short_term"] == Decimal("700")  # AAPL $500 + SPLIT_LOT $200
    # VTI $1200 + SPLIT_LOT $700 + INFDATE $300 (date-inferred LT: >365 days)
    assert by_year[2024]["long_term"] == Decimal("2200")
    assert by_year[2024]["total"] == Decimal("2900")
    assert by_year[2024]["lots"] == 4
    _assert_clean(session)


def test_realized_gl_2025_buckets(session: Session) -> None:
    from src.reports.brokerage_summary import get_realized_gl_summary

    summary = get_realized_gl_summary(session)
    by_year = summary["by_year"]
    assert by_year[2025]["short_term"] == Decimal("-100")  # WASH
    assert by_year[2025]["unknown"] == Decimal("50")  # ANCIENT
    assert by_year[2025]["lots"] == 2
    _assert_clean(session)


def test_realized_gl_wash_sale_summary(session: Session) -> None:
    from src.reports.brokerage_summary import get_realized_gl_summary

    summary = get_realized_gl_summary(session)
    assert summary["wash_sales"]["lots"] == 1
    assert summary["wash_sales"]["total_disallowed_loss"] == Decimal("50")
    _assert_clean(session)


def test_realized_gl_empty(empty_session: Session) -> None:
    from src.reports.brokerage_summary import get_realized_gl_summary

    summary = get_realized_gl_summary(empty_session)
    assert summary["by_year"] == {}
    assert summary["wash_sales"]["lots"] == 0
    assert summary["wash_sales"]["total_disallowed_loss"] == Decimal("0")
    _assert_clean(empty_session)


# ───────────────────────────────────────────────────────────────────────
# TASK-08: compute_data_integrity
# ───────────────────────────────────────────────────────────────────────


def test_data_integrity_counts(session: Session) -> None:
    from src.reports.brokerage_summary import compute_data_integrity

    di = compute_data_integrity(session)
    assert di["accounts"] == 10
    assert di["transactions"] == 7
    assert di["position_snapshots"] == 28  # +1 for Generated-at row in A10
    assert di["realized_lots"] == 6  # +1 for INFDATE date-inference lot
    _assert_clean(session)


def test_data_integrity_stale(session: Session) -> None:
    from src.reports.brokerage_summary import compute_data_integrity

    di = compute_data_integrity(session)
    assert di["stale_snapshot_accounts"] == 1  # A7 (today-10)
    _assert_clean(session)


def test_data_integrity_suspect_symbols(session: Session) -> None:
    from src.reports.brokerage_summary import compute_data_integrity

    di = compute_data_integrity(session)
    assert di["suspect_symbols"] == 2  # A10 TOTAL + A10 Generated-at row
    _assert_clean(session)


def test_data_integrity_duplicate_groups(session: Session) -> None:
    from src.reports.brokerage_summary import compute_data_integrity

    di = compute_data_integrity(session)
    # A8 MGK has 2 rows = 1 duplicate group. A9's 3 NULL rows have different
    # descriptions so are NOT duplicates. So expected = 1.
    assert di["duplicate_position_groups"] == 1
    # No transaction duplicates in the canonical fixture.
    assert di["duplicate_transaction_groups"] == 0
    _assert_clean(session)


def test_data_integrity_duplicate_transactions_detected(session: Session) -> None:
    from src.reports.brokerage_summary import compute_data_integrity

    # Insert a duplicate transaction group: same account, date, action, symbol, amount.
    accounts = session.query(Account).all()
    a1_id = next(a.id for a in accounts if a.account_number == "Z23257759")
    for hash_suffix in ("dup1", "dup2"):
        tx = BrokerageTransaction(
            account_id=a1_id,
            trade_date=TODAY,
            action="DUPCHECK",
            canonical_action=CanonicalAction.OTHER.value,
            symbol="ZZZ",
            amount=Decimal("100"),
            status=BrokerageTxStatus.IMPORTED.value,
            is_synthetic=False,
            source_file="dup_test.csv",
            source_row_hash=f"dup_test_{hash_suffix}",
            raw_data={},
        )
        session.add(tx)
    session.commit()
    session.expire_all()  # drop the new objects from the dirty/new tracking

    di = compute_data_integrity(session)
    assert di["duplicate_transaction_groups"] == 1


def test_data_integrity_no_orphans_in_clean_fixture(session: Session) -> None:
    from src.reports.brokerage_summary import compute_data_integrity

    di = compute_data_integrity(session)
    assert di["orphan_transactions"] == 0
    assert di["orphan_snapshots"] == 0
    _assert_clean(session)


def test_compute_data_integrity_empty_db(empty_session: Session) -> None:
    """P1-G: empty DB returns all-zero counts without crashing."""
    from src.reports.brokerage_summary import compute_data_integrity

    di = compute_data_integrity(empty_session)
    assert di["accounts"] == 0
    assert di["transactions"] == 0
    assert di["position_snapshots"] == 0
    assert di["realized_lots"] == 0
    assert di["orphan_transactions"] == 0
    assert di["orphan_snapshots"] == 0
    assert di["stale_snapshot_accounts"] == 0
    assert di["suspect_symbols"] == 0
    assert di["duplicate_position_groups"] == 0
    assert di["duplicate_transaction_groups"] == 0
    _assert_clean(empty_session)


def test_get_recent_transactions_empty_db(empty_session: Session) -> None:
    """P1-G: empty DB returns [] without crashing."""
    from src.reports.brokerage_summary import get_recent_transactions

    rows = get_recent_transactions(empty_session, days=14)
    assert rows == []
    _assert_clean(empty_session)


# ───────────────────────────────────────────────────────────────────────
# TASK-09: render_report
# ───────────────────────────────────────────────────────────────────────


def test_render_report_has_all_section_headers(session: Session) -> None:
    from src.reports.brokerage_summary import (
        compute_data_integrity,
        compute_net_worth,
        get_account_summary,
        get_realized_gl_summary,
        get_recent_transactions,
        get_top_holdings,
        render_report,
    )

    nw = compute_net_worth(session)
    data = {
        "net_worth": nw,
        "accounts": get_account_summary(session),
        "top_holdings": get_top_holdings(session, net_worth_total=nw["total"], n=10),
        "recent_transactions": get_recent_transactions(session, days=14),
        "realized_gl": get_realized_gl_summary(session),
        "data_integrity": compute_data_integrity(session),
    }
    out = render_report(data)
    assert "Net Worth" in out
    assert "Accounts" in out
    assert "Top Holdings" in out
    assert "Recent Transactions" in out
    assert "Realized G/L" in out
    assert "Data Integrity" in out


def test_render_report_currency_precision() -> None:
    from src.reports.brokerage_summary import _format_currency

    assert _format_currency(Decimal("12345.678")) == "$12,345.68"
    assert _format_currency(Decimal("0")) == "$0.00"
    assert _format_currency(Decimal("-100.5")) == "-$100.50"


def test_render_report_empty() -> None:
    from src.reports.brokerage_summary import render_report

    out = render_report({})
    assert "No brokerage data ingested yet" in out


def test_render_report_no_wash_sales_message() -> None:
    from src.reports.brokerage_summary import render_report

    data: dict[str, Any] = {
        "net_worth": {"total": Decimal("100"), "by_broker": {"fidelity": Decimal("100")},
                      "by_entity": {"personal": Decimal("100")}, "as_of_min": TODAY,
                      "as_of_max": TODAY, "zero_snapshot_account_count": 0,
                      "plan_wrapper_excluded_count": 0},
        "accounts": [],
        "top_holdings": [],
        "recent_transactions": [],
        "realized_gl": {"by_year": {}, "wash_sales": {"lots": 0, "total_disallowed_loss": Decimal("0")}},
        "data_integrity": {"accounts": 1, "transactions": 0, "position_snapshots": 1,
                           "realized_lots": 0, "orphan_transactions": 0, "orphan_snapshots": 0,
                           "stale_snapshot_accounts": 0, "suspect_symbols": 0,
                           "duplicate_position_groups": 0, "duplicate_transaction_groups": 0},
    }
    out = render_report(data)
    assert "1099-B" in out  # the "data not yet ingested" message references 1099-B


def test_render_report_wash_sale_with_lots() -> None:
    from src.reports.brokerage_summary import render_report

    data: dict[str, Any] = {
        "net_worth": {"total": Decimal("100"), "by_broker": {}, "by_entity": {},
                      "as_of_min": None, "as_of_max": None,
                      "zero_snapshot_account_count": 0, "plan_wrapper_excluded_count": 0},
        "accounts": [],
        "top_holdings": [],
        "recent_transactions": [],
        "realized_gl": {"by_year": {}, "wash_sales": {"lots": 3, "total_disallowed_loss": Decimal("125.50")}},
        "data_integrity": {"accounts": 0, "transactions": 0, "position_snapshots": 0,
                           "realized_lots": 0, "orphan_transactions": 0, "orphan_snapshots": 0,
                           "stale_snapshot_accounts": 0, "suspect_symbols": 0,
                           "duplicate_position_groups": 0, "duplicate_transaction_groups": 0},
    }
    out = render_report(data)
    assert "3 lots" in out
    assert "125.50" in out


def test_render_report_shows_warning_when_suspect_symbols_present(session: Session) -> None:
    """R2 P2-008: warning indicator appears when data_integrity has suspect_symbols > 0."""
    from src.reports.brokerage_summary import render_report

    data: dict[str, Any] = {
        "net_worth": {"total": Decimal("100"), "by_broker": {"fidelity": Decimal("100")},
                      "by_entity": {"personal": Decimal("100")}, "as_of_min": TODAY,
                      "as_of_max": TODAY, "zero_snapshot_account_count": 0,
                      "plan_wrapper_excluded_count": 0},
        "accounts": [],
        "top_holdings": [],
        "recent_transactions": [],
        "realized_gl": {"by_year": {}, "wash_sales": {"lots": 0, "total_disallowed_loss": Decimal("0")}},
        "data_integrity": {"accounts": 1, "transactions": 0, "position_snapshots": 2,
                           "realized_lots": 0, "orphan_transactions": 0, "orphan_snapshots": 0,
                           "stale_snapshot_accounts": 0, "suspect_symbols": 2,
                           "duplicate_position_groups": 0, "duplicate_transaction_groups": 0},
    }
    out = render_report(data)
    assert "⚠" in out, "Warning symbol should appear when suspect_symbols > 0"


def test_render_report_includes_per_broker_subtotal(session: Session) -> None:
    from src.reports.brokerage_summary import (
        compute_data_integrity,
        compute_net_worth,
        get_account_summary,
        get_realized_gl_summary,
        get_recent_transactions,
        get_top_holdings,
        render_report,
    )

    nw = compute_net_worth(session)
    data = {
        "net_worth": nw,
        "accounts": get_account_summary(session),
        "top_holdings": get_top_holdings(session, net_worth_total=nw["total"], n=10),
        "recent_transactions": get_recent_transactions(session, days=14),
        "realized_gl": get_realized_gl_summary(session),
        "data_integrity": compute_data_integrity(session),
    }
    out = render_report(data)
    assert "fidelity" in out.lower()
    assert "schwab" in out.lower()


# ───────────────────────────────────────────────────────────────────────
# TASK-10: main()
# ───────────────────────────────────────────────────────────────────────


def test_main_runs_against_fixture_db(tmp_path: Path, session: Session, capsys: pytest.CaptureFixture[str]) -> None:
    from src.reports.brokerage_summary import main

    # Persist fixture to a real sqlite file for main() to open via path.
    db_path = tmp_path / "test.db"
    sqlite_url = f"sqlite:///{db_path}"
    src_engine = session.get_bind()
    # Copy schema + data using a simple dump approach: re-create tables on dst.
    from sqlalchemy import create_engine as _ce
    dst_engine = _ce(sqlite_url)
    Base.metadata.create_all(dst_engine)
    with src_engine.connect() as src_conn, dst_engine.begin() as dst_conn:
        for table in Base.metadata.sorted_tables:
            rows = list(src_conn.execute(table.select()))
            if rows:
                dst_conn.execute(table.insert(), [dict(r._mapping) for r in rows])

    rc = main(["--db", str(db_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Net Worth" in out


def test_main_missing_db_exits_nonzero(capsys: pytest.CaptureFixture[str]) -> None:
    from src.reports.brokerage_summary import main

    rc = main(["--db", "/tmp/does-not-exist-xyz/x.db"])
    assert rc != 0


def test_main_perf_under_2s(tmp_path: Path, session: Session) -> None:
    from src.reports.brokerage_summary import main

    db_path = tmp_path / "perf.db"
    sqlite_url = f"sqlite:///{db_path}"
    src_engine = session.get_bind()
    from sqlalchemy import create_engine as _ce
    dst_engine = _ce(sqlite_url)
    Base.metadata.create_all(dst_engine)
    with src_engine.connect() as src_conn, dst_engine.begin() as dst_conn:
        for table in Base.metadata.sorted_tables:
            rows = list(src_conn.execute(table.select()))
            if rows:
                dst_conn.execute(table.insert(), [dict(r._mapping) for r in rows])

    start = time.monotonic()
    rc = main(["--db", str(db_path)])
    elapsed = time.monotonic() - start
    assert rc == 0
    assert elapsed < 2.0


def test_shim_direct_invocation_works(tmp_path: Path, session: Session) -> None:
    """Run scripts/brokerage_summary.py via subprocess to confirm the shim's
    sys.path setup makes `from src.reports.brokerage_summary import main` resolve."""
    db_path = tmp_path / "shim.db"
    sqlite_url = f"sqlite:///{db_path}"
    src_engine = session.get_bind()
    from sqlalchemy import create_engine as _ce
    dst_engine = _ce(sqlite_url)
    Base.metadata.create_all(dst_engine)
    with src_engine.connect() as src_conn, dst_engine.begin() as dst_conn:
        for table in Base.metadata.sorted_tables:
            rows = list(src_conn.execute(table.select()))
            if rows:
                dst_conn.execute(table.insert(), [dict(r._mapping) for r in rows])

    project_root = Path(__file__).resolve().parents[2]
    script_path = project_root / "scripts" / "brokerage_summary.py"
    assert script_path.exists(), f"shim missing at {script_path}"

    proc = subprocess.run(
        [sys.executable, str(script_path), "--db", str(db_path)],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(project_root),
    )
    assert proc.returncode == 0, f"shim failed: stderr={proc.stderr}"
    assert "Net Worth" in proc.stdout


# ───────────────────────────────────────────────────────────────────────
# Phase-4 follow-up: merge AccountBalanceSnapshot into net-worth views
# ───────────────────────────────────────────────────────────────────────


def _make_account(
    s: Session,
    *,
    id: str,
    broker: str = Broker.VANGUARD.value,
    account_number: str | None = None,
    account_type: str = AccountType.TAXABLE.value,
    entity: str = Entity.PERSONAL.value,
    is_plan_wrapper: bool = False,
) -> Account:
    a = Account(
        id=id,
        broker=broker,
        account_number=account_number or f"acct-{id}",
        account_type=account_type,
        entity=entity,
        tax_sheltered=False,
        is_plan_wrapper=is_plan_wrapper,
    )
    s.add(a)
    return a


def _make_position(
    s: Session,
    *,
    account_id: str,
    as_of: date,
    market_value: Decimal,
    symbol: str = "VTI",
) -> None:
    s.add(PositionSnapshot(
        account_id=account_id,
        as_of=_ts(as_of),
        symbol=symbol,
        description=symbol,
        quantity=Decimal("10"),
        price=market_value / Decimal("10"),
        market_value=market_value,
        source_file=f"test-{account_id}.csv",
        source_row_hash=f"hash-{account_id}-{symbol}-{as_of}",
        raw_data={},
    ))


def _make_balance(
    s: Session,
    *,
    account_id: str | None,
    raw_account_name: str,
    as_of: date,
    balance: Decimal,
    source: str = "test_source",
) -> None:
    s.add(AccountBalanceSnapshot(
        account_id=account_id,
        raw_account_name=raw_account_name,
        as_of=as_of,
        balance=balance,
        source=source,
        source_row_hash=f"abs-hash-{account_id}-{raw_account_name}-{as_of}",
    ))


# ── _per_account_value (T1) ────────────────────────────────────────────


def test_per_account_value_position_only(empty_session: Session) -> None:
    from src.reports.brokerage_summary import _per_account_value
    s = empty_session
    _make_account(s, id="acct-pos-only", broker=Broker.VANGUARD.value)
    _make_position(s, account_id="acct-pos-only", as_of=TODAY,
                   market_value=Decimal("1234.56"))
    s.commit()

    result = _per_account_value(s)
    assert "acct-pos-only" in result
    assert result["acct-pos-only"]["market_value"] == Decimal("1234.56")
    assert result["acct-pos-only"]["source"] == "position"
    assert result["acct-pos-only"]["as_of"] == TODAY
    _assert_clean(s)


def test_per_account_value_balance_only(empty_session: Session) -> None:
    from src.reports.brokerage_summary import _per_account_value
    s = empty_session
    _make_account(s, id="acct-bal-only", broker=Broker.NW_MUTUAL.value,
                  account_type=AccountType.OTHER.value)
    _make_balance(s, account_id="acct-bal-only",
                  raw_account_name="NW Mutual 12345",
                  as_of=TODAY, balance=Decimal("9876.54"))
    s.commit()

    result = _per_account_value(s)
    assert "acct-bal-only" in result
    assert result["acct-bal-only"]["market_value"] == Decimal("9876.54")
    assert result["acct-bal-only"]["source"] == "balance"
    _assert_clean(s)


def test_per_account_value_position_wins_over_balance(empty_session: Session) -> None:
    from src.reports.brokerage_summary import _per_account_value
    s = empty_session
    _make_account(s, id="acct-both")
    _make_position(s, account_id="acct-both", as_of=TODAY,
                   market_value=Decimal("100000"))
    _make_balance(s, account_id="acct-both", raw_account_name="x",
                  as_of=TODAY, balance=Decimal("90000"))
    s.commit()

    result = _per_account_value(s)
    assert result["acct-both"]["market_value"] == Decimal("100000")
    assert result["acct-both"]["source"] == "position"
    _assert_clean(s)


def test_per_account_value_no_snapshots_excluded(empty_session: Session) -> None:
    from src.reports.brokerage_summary import _per_account_value
    s = empty_session
    _make_account(s, id="acct-nothing")
    s.commit()
    assert "acct-nothing" not in _per_account_value(s)
    _assert_clean(s)


def test_per_account_value_picks_latest_balance(empty_session: Session) -> None:
    from src.reports.brokerage_summary import _per_account_value
    s = empty_session
    _make_account(s, id="acct-many-bal", broker=Broker.NW_MUTUAL.value,
                  account_type=AccountType.OTHER.value)
    _make_balance(s, account_id="acct-many-bal", raw_account_name="x",
                  as_of=date(2024, 1, 1), balance=Decimal("100"))
    _make_balance(s, account_id="acct-many-bal", raw_account_name="x",
                  as_of=date(2026, 5, 7), balance=Decimal("999"))
    s.commit()

    result = _per_account_value(s)
    assert result["acct-many-bal"]["market_value"] == Decimal("999")
    assert result["acct-many-bal"]["as_of"] == date(2026, 5, 7)
    _assert_clean(s)


def test_per_account_value_picks_latest_position(empty_session: Session) -> None:
    """FIX-4: Two PositionSnapshots for one account at different dates —
    _per_account_value must return the later date's value."""
    from src.reports.brokerage_summary import _per_account_value
    s = empty_session
    _make_account(s, id="acct-pos-dates")
    _make_position(s, account_id="acct-pos-dates", as_of=date(2025, 1, 1),
                   market_value=Decimal("500"), symbol="VTI")
    _make_position(s, account_id="acct-pos-dates", as_of=TODAY,
                   market_value=Decimal("750"), symbol="VTI")
    s.commit()

    result = _per_account_value(s)
    assert "acct-pos-dates" in result
    assert result["acct-pos-dates"]["market_value"] == Decimal("750"), \
        "Latest date's market_value must win"
    assert result["acct-pos-dates"]["as_of"] == TODAY, \
        "as_of must reflect the later snapshot date"
    assert result["acct-pos-dates"]["source"] == "position"
    _assert_clean(s)


def test_per_account_value_none_market_value_falls_back_to_abs(
    empty_session: Session,
) -> None:
    """FIX-5: Account with one PositionSnapshot where market_value=None AND
    one AccountBalanceSnapshot with a real balance — result must be source='balance'."""
    from src.reports.brokerage_summary import _per_account_value
    s = empty_session
    _make_account(s, id="acct-null-mv", broker=Broker.NW_MUTUAL.value,
                  account_type=AccountType.OTHER.value)
    # PositionSnapshot with null market_value — filtered out by _latest_position_snapshots
    s.add(PositionSnapshot(
        account_id="acct-null-mv",
        as_of=_ts(TODAY),
        symbol="X",
        description="X",
        quantity=Decimal("1"),
        market_value=None,
        source_file="test.csv",
        source_row_hash="null-mv-snap",
        raw_data={},
    ))
    _make_balance(s, account_id="acct-null-mv", raw_account_name="NW Mutual X",
                  as_of=TODAY, balance=Decimal("4321.00"))
    s.commit()

    result = _per_account_value(s)
    assert "acct-null-mv" in result, \
        "Account must appear via ABS fallback when PositionSnapshot has null market_value"
    assert result["acct-null-mv"]["source"] == "balance"
    assert result["acct-null-mv"]["market_value"] == Decimal("4321.00")
    _assert_clean(s)


# ── compute_net_worth (T2) ─────────────────────────────────────────────


def test_compute_net_worth_includes_balance_only_brokers(empty_session: Session) -> None:
    from src.reports.brokerage_summary import compute_net_worth
    s = empty_session
    # FG annuity: balance-only broker.
    _make_account(s, id="fg1", broker=Broker.FG_ANNUITY.value,
                  account_type=AccountType.OTHER.value, account_number="MZ152585")
    _make_balance(s, account_id="fg1", raw_account_name="FG Annuity MZ152585",
                  as_of=TODAY, balance=Decimal("660218.55"))
    # Schwab: position-tracked broker.
    _make_account(s, id="sch1", broker=Broker.SCHWAB.value, account_number="0001")
    _make_position(s, account_id="sch1", as_of=TODAY,
                   market_value=Decimal("100000"))
    s.commit()

    nw = compute_net_worth(s)
    assert nw["total"] == Decimal("760218.55")
    assert nw["by_broker"][Broker.FG_ANNUITY.value] == Decimal("660218.55")
    assert nw["by_broker"][Broker.SCHWAB.value] == Decimal("100000")
    _assert_clean(s)


def test_compute_net_worth_position_wins_for_dual_source_account(
    empty_session: Session,
) -> None:
    from src.reports.brokerage_summary import compute_net_worth
    s = empty_session
    _make_account(s, id="dual")
    _make_position(s, account_id="dual", as_of=TODAY,
                   market_value=Decimal("500"))
    _make_balance(s, account_id="dual", raw_account_name="x",
                  as_of=TODAY, balance=Decimal("700"))
    s.commit()
    nw = compute_net_worth(s)
    assert nw["total"] == Decimal("500"), \
        "PositionSnapshot must win when both sources exist"
    _assert_clean(s)


def test_compute_net_worth_excludes_plan_wrapper_balance_source(
    empty_session: Session,
) -> None:
    from src.reports.brokerage_summary import compute_net_worth
    s = empty_session
    _make_account(s, id="wrap", broker=Broker.FIDELITY.value,
                  is_plan_wrapper=True)
    _make_balance(s, account_id="wrap", raw_account_name="MS 401K",
                  as_of=TODAY, balance=Decimal("150000"))
    s.commit()
    nw = compute_net_worth(s)
    assert nw["total"] == Decimal("0")
    assert nw["plan_wrapper_excluded_count"] == 1
    _assert_clean(s)


def test_compute_net_worth_zero_snapshot_excludes_balance_only_accounts(
    empty_session: Session,
) -> None:
    """An account with ANY snapshot (position OR balance) is NOT zero-snapshot."""
    from src.reports.brokerage_summary import compute_net_worth
    s = empty_session
    _make_account(s, id="empty1")  # zero snapshots
    _make_account(s, id="bal1", broker=Broker.NW_MUTUAL.value,
                  account_type=AccountType.OTHER.value)
    _make_balance(s, account_id="bal1", raw_account_name="x",
                  as_of=TODAY, balance=Decimal("10"))
    s.commit()
    nw = compute_net_worth(s)
    assert nw["zero_snapshot_account_count"] == 1, \
        "Only the truly-empty account should count; bal1 has an ABS"
    assert nw["total"] == Decimal("10"), \
        "bal1's ABS balance must contribute to the total"
    _assert_clean(s)


# ── get_account_summary (T3) ───────────────────────────────────────────


def test_get_account_summary_balance_only_account_has_as_of(
    empty_session: Session,
) -> None:
    from src.reports.brokerage_summary import get_account_summary
    s = empty_session
    _make_account(s, id="nw1", broker=Broker.NW_MUTUAL.value,
                  account_type=AccountType.OTHER.value, account_number="17399215")
    _make_balance(s, account_id="nw1", raw_account_name="NW Mutual 17399215",
                  as_of=date(2026, 5, 7), balance=Decimal("7280.48"))
    s.commit()

    rows = get_account_summary(s)
    nw_rows = [r for r in rows if r["account_id"] == "nw1"]
    assert len(nw_rows) == 1
    assert nw_rows[0]["as_of"] == date(2026, 5, 7), \
        "Balance-only account must have non-null as_of"
    assert nw_rows[0]["market_value"] == Decimal("7280.48")
    _assert_clean(s)


def test_get_account_summary_dual_source_uses_position(
    empty_session: Session,
) -> None:
    from src.reports.brokerage_summary import get_account_summary
    s = empty_session
    _make_account(s, id="dual2")
    _make_position(s, account_id="dual2", as_of=date(2026, 5, 7),
                   market_value=Decimal("123"))
    _make_balance(s, account_id="dual2", raw_account_name="x",
                  as_of=date(2026, 5, 1), balance=Decimal("999"))
    s.commit()
    rows = get_account_summary(s)
    dual = next(r for r in rows if r["account_id"] == "dual2")
    assert dual["market_value"] == Decimal("123")
    assert dual["as_of"] == date(2026, 5, 7)
    _assert_clean(s)


def test_get_account_summary_sort_order_across_mixed_sources(
    empty_session: Session,
) -> None:
    from src.reports.brokerage_summary import get_account_summary
    s = empty_session
    _make_account(s, id="small", broker=Broker.SCHWAB.value, account_number="aaaa")
    _make_position(s, account_id="small", as_of=TODAY,
                   market_value=Decimal("100"))
    _make_account(s, id="big-bal", broker=Broker.FG_ANNUITY.value,
                  account_type=AccountType.OTHER.value, account_number="bbbb")
    _make_balance(s, account_id="big-bal", raw_account_name="big",
                  as_of=TODAY, balance=Decimal("9999"))
    s.commit()
    rows = get_account_summary(s)
    # Filter to just our two accounts
    ours = [r for r in rows if r["account_id"] in {"small", "big-bal"}]
    assert ours[0]["account_id"] == "big-bal", \
        "Higher market_value must sort first regardless of source"
    assert ours[1]["account_id"] == "small"
    # Values and dates must also be correct, not just order.
    assert ours[0]["market_value"] == Decimal("9999"), \
        "big-bal market_value must match ABS balance"
    assert ours[0]["as_of"] == TODAY, \
        "big-bal as_of must reflect ABS as_of date"
    assert ours[1]["market_value"] == Decimal("100"), \
        "small market_value must match PositionSnapshot value"
    assert ours[1]["as_of"] == TODAY
    _assert_clean(s)


def test_get_account_summary_plan_wrapper_still_returned(
    empty_session: Session,
) -> None:
    """Plan-wrapper accounts ARE returned by get_account_summary (the renderer
    is responsible for visually flagging) — even when their value comes from
    AccountBalanceSnapshot."""
    from src.reports.brokerage_summary import get_account_summary
    s = empty_session
    _make_account(s, id="wrap2", broker=Broker.FIDELITY.value,
                  is_plan_wrapper=True, account_number="wrap")
    _make_balance(s, account_id="wrap2", raw_account_name="x",
                  as_of=TODAY, balance=Decimal("500"))
    s.commit()
    rows = get_account_summary(s)
    wrap = next(r for r in rows if r["account_id"] == "wrap2")
    assert wrap["is_plan_wrapper"] is True
    assert wrap["market_value"] == Decimal("500")
    _assert_clean(s)


def test_compute_net_worth_plan_wrapper_excluded_regardless_of_source(
    empty_session: Session,
) -> None:
    """FIX-8: plan-wrapper exclusion fires whether the value comes from
    PositionSnapshot, AccountBalanceSnapshot, or both.  A sibling non-wrapper
    account with its own ABS must still be counted.

    Assertions:
    - wrapper excluded → total excludes wrapper's value
    - sibling included → total = sibling ABS balance
    - plan_wrapper_excluded_count == 1
    """
    from src.reports.brokerage_summary import compute_net_worth
    s = empty_session
    # Wrapper with BOTH sources — exclusion must fire regardless.
    _make_account(s, id="wrap-both", broker=Broker.FIDELITY.value,
                  is_plan_wrapper=True, account_number="wp01")
    _make_position(s, account_id="wrap-both", as_of=TODAY,
                   market_value=Decimal("200000"))
    _make_balance(s, account_id="wrap-both", raw_account_name="Fidelity 401K",
                  as_of=TODAY, balance=Decimal("195000"))
    # Non-wrapper sibling with ABS only.
    _make_account(s, id="sibling", broker=Broker.NW_MUTUAL.value,
                  account_type=AccountType.OTHER.value, account_number="sb01")
    _make_balance(s, account_id="sibling", raw_account_name="NW Mutual sibling",
                  as_of=TODAY, balance=Decimal("55000"))
    s.commit()

    nw = compute_net_worth(s)
    assert nw["plan_wrapper_excluded_count"] == 1, \
        "Exactly one plan-wrapper account exists"
    assert nw["total"] == Decimal("55000"), \
        "Only the non-wrapper sibling ABS contributes; wrapper is excluded"
    _assert_clean(s)
