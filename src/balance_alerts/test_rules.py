"""Tests for balance-milestone rules (REQ-BAL-001..006).

The pure `evaluate_account` path needs no DB; `compute_balance_alerts` is covered
with an in-memory SQLite session.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.balance_alerts.rules import (
    SEV2,
    SEV3,
    SEV_INFO,
    BalanceAlert,
    _credit_milestones_up_to,
    classify,
    compute_balance_alerts,
    evaluate_account,
)
from src.models.base import Base

# Import models BEFORE create_all so their tables register on Base.metadata.
from src.models.brokerage import Account
from src.models.plaid import PlaidAccountBalanceSnapshot as Snap

OCC = "2026-06-14"

_ENGINE = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
Base.metadata.create_all(_ENGINE)
_Session = sessionmaker(bind=_ENGINE)


@pytest.fixture()
def session() -> Generator[Session, None, None]:
    s = _Session()
    yield s
    s.query(Snap).delete()
    s.query(Account).delete()
    s.commit()
    s.close()


def _eval(
    ptype: str,
    sub: str | None,
    baseline: object,
    current: object,
    account_id: str = "acct-1",
    account_name: str = "Test Acct",
    entity: str = "sparkry",
) -> list[BalanceAlert]:
    return evaluate_account(
        account_id=account_id,
        account_name=account_name,
        entity=entity,
        plaid_account_type=ptype,
        plaid_account_subtype=sub,
        baseline=None if baseline is None else Decimal(str(baseline)),
        current=Decimal(str(current)),
        occurrence_date=OCC,
    )


# --- classification ---------------------------------------------------------

@pytest.mark.parametrize(
    "ptype,sub,expected",
    [
        ("depository", "checking", "checking"),
        ("depository", "savings", "savings"),
        ("depository", "money market", "savings"),
        ("depository", None, "savings"),
        ("credit", "credit card", "credit"),
        ("investment", "brokerage", "investment"),
        ("brokerage", None, "investment"),
        ("loan", "mortgage", None),
        ("other", None, "savings"),
    ],
)
def test_classify(ptype: str, sub: str | None, expected: str | None) -> None:
    assert classify(ptype, sub) == expected


# --- REQ-BAL-005: null baseline never fires --------------------------------

def test_null_baseline_never_fires() -> None:
    assert _eval("depository", "checking", None, 0) == []


def test_loan_muted() -> None:
    # REQ-BAL-004: loans muted even on a huge move.
    assert _eval("loan", "mortgage", 500000, 0) == []


# --- REQ-BAL-001: checking downward milestones + severity ------------------

def test_checking_crosses_single_milestone_info() -> None:
    alerts = _eval("depository", "checking", 12000, 9000)  # crosses 10k only
    assert len(alerts) == 1
    assert alerts[0].level == "10000"
    assert alerts[0].severity == SEV_INFO


def test_checking_crosses_multiple_milestones_at_once() -> None:
    # 12000 → 800 crosses 10k, 5k, 1k (not 0). REQ-BAL-001.
    alerts = _eval("depository", "checking", 12000, 800)
    levels = sorted(a.level for a in alerts if a.level is not None)
    assert levels == ["1000", "10000", "5000"]
    sev = {a.level: a.severity for a in alerts}
    assert sev["10000"] == SEV_INFO
    assert sev["5000"] == SEV_INFO
    assert sev["1000"] == SEV3


def test_checking_overdraft_is_sev2() -> None:
    # crossing $0 (overdraft) → sev2 (most urgent).
    alerts = _eval("depository", "checking", 500, -50)
    assert len(alerts) == 1
    assert alerts[0].level == "0"
    assert alerts[0].severity == SEV2


def test_checking_no_fire_when_not_crossing() -> None:
    # Drops but stays above all milestones.
    assert _eval("depository", "checking", 30000, 25000) == []


def test_checking_rising_does_not_fire() -> None:
    # Upward move on a checking account never fires (REQ-BAL-005 directional).
    assert _eval("depository", "checking", 800, 6000) == []


def test_checking_at_exactly_milestone_fires() -> None:
    # current == level counts as crossed (<=).
    alerts = _eval("depository", "checking", 1500, 1000)
    assert [a.level for a in alerts] == ["1000"]


# --- REQ-BAL-002: savings floor --------------------------------------------

def test_savings_below_100_sev3() -> None:
    alerts = _eval("depository", "savings", 500, 50)
    assert len(alerts) == 1
    assert alerts[0].level == "100"
    assert alerts[0].severity == SEV3
    assert alerts[0].kind == "savings"


def test_savings_no_fire_above_floor() -> None:
    assert _eval("depository", "savings", 500, 200) == []


def test_savings_only_one_floor_not_checking_ladder() -> None:
    # A savings account dropping to $0 fires once (the $100 floor), not the
    # checking ladder.
    alerts = _eval("depository", "savings", 5000, 0)
    assert [a.level for a in alerts] == ["100"]


# --- REQ-BAL-003: credit upward milestones ---------------------------------

def test_credit_reaches_10k_info() -> None:
    alerts = _eval("credit", "credit card", 9000, 10500)
    assert len(alerts) == 1
    assert alerts[0].level == "10000"
    assert alerts[0].severity == SEV_INFO


def test_credit_crosses_two_bands_at_once() -> None:
    # 9k → 21k crosses 10k AND 20k. REQ-BAL-003.
    alerts = _eval("credit", "credit card", 9000, 21000)
    levels = sorted((a.level for a in alerts if a.level is not None), key=int)
    assert levels == ["10000", "20000"]
    sev = {a.level: a.severity for a in alerts}
    assert sev["10000"] == SEV_INFO
    assert sev["20000"] == SEV3  # ≥$20k → sev3


def test_credit_paydown_does_not_fire() -> None:
    assert _eval("credit", "credit card", 21000, 5000) == []


def test_credit_no_fire_within_band() -> None:
    assert _eval("credit", "credit card", 10500, 11000) == []


# --- REQ-BAL-004: investment drift AND-logic --------------------------------

def test_investment_drift_requires_both_pct_and_abs() -> None:
    # 2% on $3M is $60k (abs ok) but pct < 15% → no fire (AND).
    assert _eval("investment", "brokerage", 3000000, 2940000) == []


def test_investment_drift_pct_ok_abs_too_small() -> None:
    # 50% drop on $1000 = $500 (pct ok) but abs < $25k → no fire.
    assert _eval("investment", "brokerage", 1000, 500) == []


def test_investment_drift_both_exceeded_fires_sev3() -> None:
    # 18% drop on $3.77M = ~$700k. Both thresholds blown.
    alerts = _eval("investment", "brokerage", 3771585.60, 3071156.06)
    assert len(alerts) == 1
    assert alerts[0].alert_type == "balance_drift"
    assert alerts[0].severity == SEV3
    assert alerts[0].level is None


def test_investment_both_zero_no_fire() -> None:
    assert _eval("investment", "brokerage", 0, 0) == []


# --- REQ-BAL-006: dedup key shape + occurrence date -------------------------

def test_alert_key_and_occurrence_shape() -> None:
    alerts = _eval("depository", "checking", 1500, 1000, account_id="abc")
    a = alerts[0]
    assert a.alert_key == "balance:abc:checking:1000"
    assert a.occurrence_date == OCC


def test_decimal_constructed_from_str_no_float_error() -> None:
    # Float-boundary values must survive (Decimal(str(...))).
    alerts = _eval("depository", "checking", "1000.10", "999.95")
    assert alerts[0].new_balance == "999.95"


# --- REQ-BAL-006: multi-day re-fire semantics (the anti-nag guarantee) ------

def test_stay_below_does_not_refire() -> None:
    # Day 2: already below $1k yesterday (900) and stays below (850). No crossing
    # → no re-fire. Proves "no daily nag" at the rule level, not just via dedup.
    assert _eval("depository", "checking", 900, 850) == []


def test_refire_after_recovery_next_day() -> None:
    # After recovering above $1k (baseline 1200) a fresh dip below re-fires $1k.
    alerts = _eval("depository", "checking", 1200, 950)
    assert [a.level for a in alerts] == ["1000"]


def test_credit_cap_bounds_generated_milestones() -> None:
    # A corrupt/huge Plaid value must not spin an unbounded band list.
    bands = _credit_milestones_up_to(Decimal("50000000"))
    assert bands[-1] == Decimal("10000000")  # capped at $10M
    assert len(bands) == 1000  # bounded


# --- compute_balance_alerts DB layer ---------------------------------------

def test_compute_balance_alerts_reads_prior_day(session: Session) -> None:
    """Integration: latest vs prior-calendar-day snapshot drives the crossing."""
    session.add(
        Account(
            id="acc-chk",
            broker="chase",
            account_number="x-1",
            account_name="Sparkry checking",
            account_type="checking",
            entity="sparkry",
        )
    )
    session.add(
        Snap(
            account_id="acc-chk",
            snapshot_date=date(2026, 6, 13),
            plaid_account_type="depository",
            plaid_account_subtype="checking",
            current_balance=Decimal("1500.00"),
            pulled_at=datetime(2026, 6, 13, 5, 0),
            raw_data={},
        )
    )
    session.add(
        Snap(
            account_id="acc-chk",
            snapshot_date=date(2026, 6, 14),
            plaid_account_type="depository",
            plaid_account_subtype="checking",
            current_balance=Decimal("900.00"),
            pulled_at=datetime(2026, 6, 14, 5, 0),
            raw_data={},
        )
    )
    session.commit()

    alerts = compute_balance_alerts(date(2026, 6, 14), session)
    levels = sorted(a.level for a in alerts if a.level is not None)
    assert levels == ["1000"]  # crossed 1k, not 5k/10k (was already below those)
    assert alerts[0].account_name == "Sparkry checking"
    assert alerts[0].occurrence_date == "2026-06-14"


def test_compute_no_prior_day_row_no_alert(session: Session) -> None:
    session.add(
        Account(
            id="acc-only",
            broker="chase",
            account_number="x-2",
            account_name="Lonely",
            account_type="checking",
            entity="sparkry",
        )
    )
    # Only one snapshot — no prior calendar day → no baseline → no alert.
    session.add(
        Snap(
            account_id="acc-only",
            snapshot_date=date(2026, 6, 14),
            plaid_account_type="depository",
            plaid_account_subtype="checking",
            current_balance=Decimal("50.00"),
            pulled_at=datetime(2026, 6, 14, 5, 0),
            raw_data={},
        )
    )
    session.commit()
    assert compute_balance_alerts(date(2026, 6, 14), session) == []


def _snap(account_id: str, d: date, bal: str, ptype: str = "depository",
          sub: str | None = "checking") -> Snap:
    return Snap(
        account_id=account_id,
        snapshot_date=d,
        plaid_account_type=ptype,
        plaid_account_subtype=sub,
        current_balance=Decimal(bal),
        pulled_at=datetime(2026, 6, 14, 5, 0),
        raw_data={},
    )


def test_gap_account_within_7d_falls_back_and_fires_with_gap_note(session: Session) -> None:
    """REQ-FIX-PLD-003: a snapshot gap (Fri→Mon, no row on latest-1) falls back to
    the most recent snapshot within 7 days instead of muting the crossing — a
    data gap must not permanently swallow milestone alerts.
    """
    session.add(
        Account(id="gap", broker="chase", account_number="g-1",
                account_name="Gap Checking", account_type="checking", entity="sparkry")
    )
    session.add(_snap("gap", date(2026, 6, 10), "5000.00"))   # earlier row exists
    session.add(_snap("gap", date(2026, 6, 14), "200.00"))    # no June 11-13 rows
    session.commit()
    alerts = compute_balance_alerts(date(2026, 6, 14), session)
    assert len(alerts) == 1
    assert alerts[0].baseline_gap_days == 4
    assert "(4d data gap" in alerts[0].message


def test_gap_account_beyond_7d_still_does_not_fire(session: Session) -> None:
    """REQ-FIX-PLD-003: the fallback window is bounded at 7 days — beyond that,
    REQ-BAL-005's null-baseline clause still governs (no fire)."""
    session.add(
        Account(id="gap8", broker="chase", account_number="g-2",
                account_name="Gap8 Checking", account_type="checking", entity="sparkry")
    )
    session.add(_snap("gap8", date(2026, 6, 5), "5000.00"))   # 9 days before latest
    session.add(_snap("gap8", date(2026, 6, 14), "200.00"))
    session.commit()
    assert compute_balance_alerts(date(2026, 6, 14), session) == []


def test_gap_account_exactly_7d_boundary_still_fires(session: Session) -> None:
    """REQ-FIX-PLD-003 boundary: a baseline exactly 7 days before the latest
    snapshot is the last INCLUDED day of the fallback window (>=, not >)."""
    session.add(
        Account(id="gap7", broker="chase", account_number="g-7",
                account_name="Gap7 Checking", account_type="checking", entity="sparkry")
    )
    session.add(_snap("gap7", date(2026, 6, 7), "5000.00"))   # exactly 7 days before latest
    session.add(_snap("gap7", date(2026, 6, 14), "200.00"))
    session.commit()
    alerts = compute_balance_alerts(date(2026, 6, 14), session)
    assert len(alerts) == 1
    assert alerts[0].baseline_gap_days == 7


def test_gap_account_exactly_8d_boundary_does_not_fire(session: Session) -> None:
    """REQ-FIX-PLD-003 boundary: a baseline exactly 8 days before the latest
    snapshot is the first EXCLUDED day of the fallback window — pins the
    inclusive/exclusive edge in the opposite direction from the 7d test
    above so a future off-by-one window widening/narrowing is caught."""
    session.add(
        Account(id="gap8b", broker="chase", account_number="g-8b",
                account_name="Gap8b Checking", account_type="checking", entity="sparkry")
    )
    session.add(_snap("gap8b", date(2026, 6, 6), "5000.00"))  # exactly 8 days before latest
    session.add(_snap("gap8b", date(2026, 6, 14), "200.00"))
    session.commit()
    assert compute_balance_alerts(date(2026, 6, 14), session) == []


def test_gap_account_1d_no_note_and_gap_field_is_1(session: Session) -> None:
    """A normal prior-calendar-day baseline carries baseline_gap_days=1 and no note."""
    session.add(
        Account(id="normal", broker="chase", account_number="g-3",
                account_name="Normal Checking", account_type="checking", entity="sparkry")
    )
    session.add(_snap("normal", date(2026, 6, 13), "1500.00"))
    session.add(_snap("normal", date(2026, 6, 14), "900.00"))
    session.commit()
    alerts = compute_balance_alerts(date(2026, 6, 14), session)
    assert len(alerts) == 1
    assert alerts[0].baseline_gap_days == 1
    assert "data gap" not in alerts[0].message  # no gap note appended


def test_stale_cached_balance_widens_gap_note_even_with_a_row_every_day(
    session: Session,
) -> None:
    """REQ-FIX-PLD-001: `/accounts/get` returns Plaid's *cached* balance — a
    row can exist for every calendar day (row-based gap = 1) while the
    underlying value itself hasn't actually refreshed in days. When Plaid's
    `balances.last_updated_datetime` says the latest snapshot's cache is N
    days old, the gap note must reflect that true cache age (>= N), not just
    the row-based gap, so a multi-day accumulated move isn't silently
    attributed to a single day."""
    session.add(
        Account(id="stale-cache", broker="chase", account_number="g-4",
                account_name="Stale Cache Checking", account_type="checking",
                entity="sparkry")
    )
    session.add(_snap("stale-cache", date(2026, 6, 13), "1500.00"))
    stale_latest = _snap("stale-cache", date(2026, 6, 14), "900.00")
    stale_latest.raw_data = {
        "balances": {"last_updated_datetime": "2026-06-10T00:00:00Z"}
    }
    session.add(stale_latest)
    session.commit()
    alerts = compute_balance_alerts(date(2026, 6, 14), session)
    assert len(alerts) == 1
    assert alerts[0].baseline_gap_days == 4  # cache age (4d), not row gap (1d)
    assert "(4d data gap" in alerts[0].message


def test_null_last_updated_datetime_keeps_row_based_gap(session: Session) -> None:
    """When `last_updated_datetime` is null (the common case — most
    institutions don't populate it), the row-based gap alone still governs,
    unchanged from before REQ-FIX-PLD-001's cache-age check existed."""
    session.add(
        Account(id="null-cache", broker="chase", account_number="g-5",
                account_name="Null Cache Checking", account_type="checking",
                entity="sparkry")
    )
    session.add(_snap("null-cache", date(2026, 6, 13), "1500.00"))
    session.add(_snap("null-cache", date(2026, 6, 14), "900.00"))  # raw_data={}
    session.commit()
    alerts = compute_balance_alerts(date(2026, 6, 14), session)
    assert len(alerts) == 1
    assert alerts[0].baseline_gap_days == 1
    assert "data gap" not in alerts[0].message


def test_stale_cached_baseline_widens_gap_even_when_latest_is_fresh(
    session: Session,
) -> None:
    """REQ-FIX-PLD-001/003 regression: the BASELINE row's own Plaid-cached
    balance can be stale while the latest reading is perfectly fresh (a row
    exists for every calendar day, so the row-based gap alone is 1). The
    crossing actually compares the baseline's true cached-as-of value to the
    latest's fresh value, so the gap must fold in the baseline row's own
    cache age too — not just the latest row's — or a multi-day accumulated
    move gets silently attributed to a single day."""
    session.add(
        Account(id="stale-baseline", broker="chase", account_number="g-6",
                account_name="Stale Baseline Checking", account_type="checking",
                entity="sparkry")
    )
    stale_baseline = _snap("stale-baseline", date(2026, 6, 13), "1500.00")
    stale_baseline.raw_data = {
        "balances": {"last_updated_datetime": "2026-06-05T00:00:00Z"}
    }
    session.add(stale_baseline)
    session.add(_snap("stale-baseline", date(2026, 6, 14), "900.00"))  # fresh, raw_data={}
    session.commit()
    alerts = compute_balance_alerts(date(2026, 6, 14), session)
    assert len(alerts) == 1
    # baseline's own cache is 8d stale (6/5 -> 6/13) + 1d row gap (6/13 -> 6/14) = 9
    assert alerts[0].baseline_gap_days == 9
    assert "(9d data gap" in alerts[0].message


# --- REQ-FIX-ALR-008: $0 strict crossing -----------------------------------


def test_checking_zero_exact_does_not_fire_but_below_1k_does() -> None:
    # baseline $1,500 -> current $0.00 crosses the $1k floor (fires) but must
    # NOT fire the $0 floor — an exact zero balance is not an overdraft.
    alerts = _eval("depository", "checking", 1500, "0.00")
    assert [a.level for a in alerts] == ["1000"]


def test_checking_negative_cent_fires_zero_floor_sev2() -> None:
    alerts = _eval("depository", "checking", 1500, "-0.01")
    levels = {a.level: a.severity for a in alerts}
    assert levels["1000"] == SEV3
    assert levels["0"] == SEV2


def test_checking_subcent_residual_does_not_fire_zero_floor() -> None:
    # Snapshots store Numeric(18,4); a scale-4 residual like -0.0001 quantizes
    # to $0.00 and must not fire an overdraft that would render as "$0.00".
    alerts = _eval("depository", "checking", 1500, "-0.0001")
    assert "0" not in {a.level for a in alerts}
    # ...but it still crosses the $1k floor like an exact zero does.
    assert {a.level for a in alerts} == {"1000"}


def test_one_account_exception_does_not_block_others(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for the per-account try/except in compute_balance_alerts:
    a raise while processing one account (e.g. a corrupted row) must not
    blank alert computation for every other account."""
    import src.balance_alerts.rules as rules_mod

    for aid, name in (("bad", "Bad Acct"), ("good", "Good Acct")):
        session.add(
            Account(id=aid, broker="chase", account_number=f"k-{aid}",
                     account_name=name, account_type="checking", entity="sparkry")
        )
        session.add(_snap(aid, date(2026, 6, 13), "1500.00"))
    session.add(_snap("bad", date(2026, 6, 14), "900.00"))
    session.add(_snap("good", date(2026, 6, 14), "900.00"))
    session.commit()

    orig_evaluate_account = rules_mod.evaluate_account

    def _evaluate_account(*, account_id: str, **kwargs: object) -> list[BalanceAlert]:
        if account_id == "bad":
            raise RuntimeError("boom")
        return orig_evaluate_account(account_id=account_id, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(rules_mod, "evaluate_account", _evaluate_account)

    alerts = rules_mod.compute_balance_alerts(date(2026, 6, 14), session)

    assert len(alerts) == 1
    assert alerts[0].account_name == "Good Acct"


def test_multiple_accounts_attributed_correctly(session: Session) -> None:
    for aid, name in (("m1", "Sparkry"), ("m2", "BlackLine")):
        session.add(
            Account(id=aid, broker="chase", account_number=f"k-{aid}",
                    account_name=name, account_type="checking", entity="sparkry")
        )
        session.add(_snap(aid, date(2026, 6, 13), "1500.00"))
    session.add(_snap("m1", date(2026, 6, 14), "900.00"))   # m1 crosses 1k
    session.add(_snap("m2", date(2026, 6, 14), "1400.00"))  # m2 no crossing
    session.commit()
    alerts = compute_balance_alerts(date(2026, 6, 14), session)
    assert len(alerts) == 1
    assert alerts[0].account_name == "Sparkry"
    assert alerts[0].level == "1000"
