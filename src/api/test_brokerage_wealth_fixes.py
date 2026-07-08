"""Endpoint tests for the WS4 wealth-analytics fixes.

REQ-FIX-WLT-004 (per-name cutoff dedup), 005 (holdings forward-fill),
006 (benchmark anchor + staleness), 008 (Plaid freshness in missing-accounts).
Each test drives the route function directly against an in-memory session.
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
from src.models.history import AccountAlias, AccountBalanceSnapshot, HistoricalPrice
from src.models.plaid import PlaidAccountBalanceSnapshot


def _session() -> Any:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _account(s: Any, number: str, *, name: str | None = None) -> Account:
    a = Account(
        broker=Broker.VANGUARD.value,
        account_number=number,
        account_name=name,
        account_type=AccountType.TAXABLE.value,
        entity=Entity.PERSONAL.value,
    )
    s.add(a)
    s.flush()
    return a


# ── REQ-FIX-WLT-004 ────────────────────────────────────────────────────


def test_alias_cutoff_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    """REQ-FIX-WLT-004: tier-2 cutoff = earliest PositionSnapshot of aliased acct."""
    from src.api.routes.brokerage import _alias_cutoff_by_raw_name

    s = _session()
    a = _account(s, "1111")
    s.add(
        PositionSnapshot(
            account_id=a.id, as_of=datetime(2024, 1, 1), symbol="ZZZ",
            quantity=Decimal("1"), market_value=Decimal("10"),
            source_file="f", source_row_hash="h", raw_data={},
        )
    )
    s.add(AccountAlias(raw_account_name="legacy amy", account_id=a.id))
    s.commit()
    cutoffs = _alias_cutoff_by_raw_name(s)
    assert cutoffs == {"legacy amy": date(2024, 1, 1)}


def test_per_name_cutoff_not_global(monkeypatch: pytest.MonkeyPatch) -> None:
    """REQ-FIX-WLT-004: a legacy row survives a *sibling* account's cutover; it is
    only zeroed at its own aliased account's onboarding date."""
    import src.api.routes.brokerage as bmod

    monkeypatch.setattr(bmod, "_today", lambda: date(2025, 6, 1))
    s = _session()

    # Account A (aliased target) onboards LATE — first position 2024-01-01.
    a = _account(s, "AAAA")
    s.add(
        PositionSnapshot(
            account_id=a.id, as_of=datetime(2024, 1, 1), symbol="NOPX",
            quantity=Decimal("1"), market_value=Decimal("1000.00"),
            source_file="f", source_row_hash="ha", raw_data={},
        )
    )
    s.add(AccountAlias(raw_account_name="legacy x", account_id=a.id))

    # Account B (unrelated) has an EARLY balance snapshot (2021) — the old global
    # cutoff would wrongly zero the legacy row here.
    b = _account(s, "BBBB")
    s.add(
        AccountBalanceSnapshot(
            account_id=b.id, raw_account_name="B rollup", as_of=date(2021, 1, 1),
            balance=Decimal("500.00"), source="xlsx", source_row_hash="hb",
        )
    )

    # Unmatched legacy row (account_id NULL), 2020 onward.
    s.add(
        AccountBalanceSnapshot(
            account_id=None, raw_account_name="Legacy X", as_of=date(2020, 1, 1),
            balance=Decimal("100.00"), source="xlsx", source_row_hash="hl",
        )
    )
    s.commit()

    points = bmod.networth_history(
        include_unmatched=True,
        granularity="monthly",
        tags_include=None,
        tags_exclude=None,
        account_ids=None,
        session=s,
    )
    by_date = {p["as_of"]: p["balance_total"] for p in points}
    # 2023-06-30: A has no snapshot ≤ then (0); B forward-fills 500; legacy still
    # active (cutoff 2024 > 2023) → 500 + 100 = 600.
    assert by_date[date(2023, 6, 30)] == Decimal("600.00")
    # 2025-06-01 (today, appended): A=1000, B=500, legacy excluded (2024 ≤ 2025).
    assert by_date[date(2025, 6, 1)] == Decimal("1500.00")


def test_req_wd_009b_present_day_total_invariant_with_alias_seeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REQ-WD-009(b) end-to-end invariant (P3-002): with the account_alias
    seeded (the supported, documented configuration), the present-day total
    is NOT inflated by a legacy raw-name row that rolled into a matched
    account — the modern account's own value is the whole story at `today`.

    This is the checked-in end-to-end guard the review flagged as missing
    (previously only the isolated predicate/SHA fixture in
    test_networth_dedup.py exercised this invariant, never the actual
    networth_history endpoint).
    """
    import src.api.routes.brokerage as bmod

    today = date(2025, 6, 1)
    monkeypatch.setattr(bmod, "_today", lambda: today)
    s = _session()

    # Modern matched account, onboarded 2024-01-01, current value 1000.
    a = _account(s, "MODERN1")
    s.add(
        PositionSnapshot(
            account_id=a.id, as_of=datetime(2024, 1, 1), symbol="NOPX",
            quantity=Decimal("1"), market_value=Decimal("1000.00"),
            source_file="f", source_row_hash="modern1", raw_data={},
        )
    )
    # The legacy raw name is aliased to the modern account (tier-2 coverage).
    s.add(AccountAlias(raw_account_name="legacy rollup name", account_id=a.id))

    # Legacy unmatched history under a DIFFERENT label, predating the modern
    # account's onboarding — same real-world money, tracked pre-2024 as XLSX
    # rollup rows with no Account FK.
    s.add(
        AccountBalanceSnapshot(
            account_id=None, raw_account_name="Legacy Rollup Name",
            as_of=date(2020, 1, 1), balance=Decimal("400.00"),
            source="xlsx", source_row_hash="legacy1",
        )
    )
    s.commit()

    points = bmod.networth_history(
        include_unmatched=True,
        granularity="monthly",
        tags_include=None,
        tags_exclude=None,
        account_ids=None,
        session=s,
    )
    by_date = {p["as_of"]: p["balance_total"] for p in points}
    # At `today`, the legacy row is excluded (cutoff 2024-01-01 <= today) —
    # present-day total is exactly the modern account's value, not inflated
    # by the legacy row it superseded.
    assert by_date[today] == Decimal("1000.00")


def test_no_alias_cross_label_rollover_double_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P3-002 (documented gap): a legacy raw name that rolled into a matched
    account under a DIFFERENT label, with NO account_alias row recorded (an
    incomplete alias seed), has no tier-1 or tier-2 cutoff and is included in
    full at every date — double-counting present-day net worth. This locks in
    the known regression risk the review flagged so a future alias-seed gap
    is caught here first, not just noticed in production net worth.
    """
    import src.api.routes.brokerage as bmod

    today = date(2025, 6, 1)
    monkeypatch.setattr(bmod, "_today", lambda: today)
    s = _session()

    # Modern matched account, onboarded 2024-01-01, current value 1000 — the
    # real successor to the legacy row below, but under a different label and
    # with NO account_alias row (the incomplete-seed scenario).
    a = _account(s, "MODERN2")
    s.add(
        PositionSnapshot(
            account_id=a.id, as_of=datetime(2024, 1, 1), symbol="NOPX",
            quantity=Decimal("1"), market_value=Decimal("1000.00"),
            source_file="f", source_row_hash="modern2", raw_data={},
        )
    )
    # No AccountAlias row for this raw name — the gap.
    s.add(
        AccountBalanceSnapshot(
            account_id=None, raw_account_name="Old Unmapped Label",
            as_of=date(2020, 1, 1), balance=Decimal("400.00"),
            source="xlsx", source_row_hash="legacy2",
        )
    )
    s.commit()

    points = bmod.networth_history(
        include_unmatched=True,
        granularity="monthly",
        tags_include=None,
        tags_exclude=None,
        account_ids=None,
        session=s,
    )
    by_date = {p["as_of"]: p["balance_total"] for p in points}
    # Documents the current (unfixed) double-count: 1000 (modern) + 400
    # (legacy, forward-filled with no cutoff) = 1400, not the true 1000.
    # If this ever starts asserting 1000.00, the defense-in-depth gap has
    # been closed — update this test to assert the correct value then.
    assert by_date[today] == Decimal("1400.00")


def test_uncovered_raw_name_with_no_cutoff_logs_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """P3-002: an unmatched raw name with neither a tier-1 nor tier-2 cutoff
    logs a WARNING naming it, so an incomplete alias seed fails loud instead
    of silently double-counting (the review's defense-in-depth ask)."""
    import logging

    import src.api.routes.brokerage as bmod

    monkeypatch.setattr(bmod, "_today", lambda: date(2025, 6, 1))
    s = _session()
    s.add(
        AccountBalanceSnapshot(
            account_id=None, raw_account_name="Totally Unmapped Legacy",
            as_of=date(2020, 1, 1), balance=Decimal("50.00"),
            source="xlsx", source_row_hash="uncovered1",
        )
    )
    s.commit()

    with caplog.at_level(logging.WARNING, logger="src.api.routes.brokerage"):
        bmod.networth_history(
            include_unmatched=True,
            granularity="monthly",
            tags_include=None,
            tags_exclude=None,
            account_ids=None,
            session=s,
        )

    assert any(
        "no dedup cutoff" in rec.getMessage()
        and "Totally Unmapped Legacy" in rec.getMessage()
        for rec in caplog.records
    )


# ── REQ-FIX-WLT-005 ────────────────────────────────────────────────────


def test_holdings_forward_fill_no_sawtooth() -> None:
    """REQ-FIX-WLT-005: two accounts on alternating days sum via forward-fill;
    current_* = Σ of each account's latest snapshot, not one date bucket."""
    from src.api.routes.brokerage import holding_history

    s = _session()
    p1 = _account(s, "P1")
    p2 = _account(s, "P2")
    s.add(
        PositionSnapshot(
            account_id=p1.id, as_of=datetime(2026, 1, 1), symbol="ZZZ",
            quantity=Decimal("1"), market_value=Decimal("100.00"),
            cost_basis=Decimal("80.00"), source_file="f", source_row_hash="h1",
            raw_data={},
        )
    )
    s.add(
        PositionSnapshot(
            account_id=p2.id, as_of=datetime(2026, 1, 2), symbol="ZZZ",
            quantity=Decimal("2"), market_value=Decimal("200.00"),
            cost_basis=Decimal("150.00"), source_file="f", source_row_hash="h2",
            raw_data={},
        )
    )
    s.commit()

    resp = holding_history(symbol="ZZZ", session=s)
    assert resp["current_value"] == Decimal("300.00")  # 100 + 200
    assert resp["current_quantity"] == Decimal("3")
    assert resp["cost_basis"] == Decimal("230.00")
    series = {p["as_of"]: p["market_value"] for p in resp["value_series"]}
    assert series[date(2026, 1, 1)] == Decimal("100.00")  # only P1 yet
    # P1 forward-filled (100) + P2 (200) = 300 — no drop to a single account.
    assert series[date(2026, 1, 2)] == Decimal("300.00")
    assert set(resp["per_account_as_of"].values()) == {date(2026, 1, 1), date(2026, 1, 2)}


# ── REQ-FIX-WLT-006 ────────────────────────────────────────────────────


def test_benchmark_anchor_and_staleness(monkeypatch: pytest.MonkeyPatch) -> None:
    """REQ-FIX-WLT-006: portfolio predating bench prices anchors at the first
    jointly-valued date (earlier points None); a >7-day price gap yields None."""
    import src.api.routes.brokerage as bmod

    monkeypatch.setattr(bmod, "_today", lambda: date(2026, 3, 7))
    s = _session()
    a = _account(s, "ACCT")
    # Portfolio value present from early January (weekly Saturdays exist earlier
    # than any benchmark price).
    s.add(
        AccountBalanceSnapshot(
            account_id=a.id, raw_account_name="acct", as_of=date(2026, 1, 1),
            balance=Decimal("1000.00"), source="xlsx", source_row_hash="hb",
        )
    )
    # Benchmark prices (with adj_close) begin 2026-02-07, then a >30-day gap.
    for d, px in [
        (date(2026, 2, 7), "400"),
        (date(2026, 2, 14), "410"),
        # gap until 2026-03-07 (>7 days) → intermediate weekly dates gap to None.
        (date(2026, 3, 7), "420"),
    ]:
        s.add(
            HistoricalPrice(
                symbol="SPY", trade_date=d, close=Decimal(px), adj_close=Decimal(px)
            )
        )
    s.commit()

    resp = bmod.networth_history_benchmark(
        benchmark="SPY",
        granularity="weekly",
        tags_include=None,
        tags_exclude=None,
        account_ids=None,
        session=s,
    )
    assert resp["benchmark_basis"] == "total_return"
    assert resp["anchor_date"] is not None and resp["anchor_date"] >= date(2026, 2, 7)
    by_date = {p["as_of"]: p["benchmark_value"] for p in resp["series"]}
    # A Saturday before any benchmark price → None (no flatline / no anchor yet).
    early = min(by_date)
    assert early < date(2026, 2, 7)
    assert by_date[early] is None
    # A weekly date inside the >7-day gap (e.g. 2026-02-28) → None gap.
    assert by_date.get(date(2026, 2, 28)) is None


# ── REQ-FIX-WLT-008 ────────────────────────────────────────────────────


def test_missing_accounts_plaid_freshness(monkeypatch: pytest.MonkeyPatch) -> None:
    """REQ-FIX-WLT-008: an account fed only by plaid_account_balance_snapshot is
    not reported missing when fresh, and carries freshness_source='plaid'."""
    import src.api.routes.brokerage as bmod
    from src.models.history import ExpectedAccount

    monkeypatch.setattr(bmod, "_today", lambda: date(2026, 6, 30))
    s = _session()
    a = _account(s, "PLAIDACCT")
    s.add(
        ExpectedAccount(
            institution="Chase", account_name="Checking", status="active",
            source="manual", resolved_account_id=a.id,
        )
    )
    # Only a Plaid snapshot — fresh (yesterday).
    s.add(
        PlaidAccountBalanceSnapshot(
            account_id=a.id, snapshot_date=date(2026, 6, 29),
            plaid_account_type="depository", current_balance=Decimal("2500.00"),
            raw_data={},
        )
    )
    s.commit()

    fresh = bmod.missing_accounts(stale_days=60, session=s)
    assert all(r["resolved_account_id"] != a.id for r in fresh)  # not missing

    # Age the plaid snapshot past the stale window → surfaces with source=plaid.
    s.query(PlaidAccountBalanceSnapshot).update(
        {PlaidAccountBalanceSnapshot.snapshot_date: date(2026, 1, 1)}
    )
    s.commit()
    stale = bmod.missing_accounts(stale_days=60, session=s)
    row = next(r for r in stale if r["resolved_account_id"] == a.id)
    assert row["freshness_source"] == "plaid"
    assert row["last_seen_days_ago"] == (date(2026, 6, 30) - date(2026, 1, 1)).days
