"""Tests for the daily account-pulse digest (REQ-BAL-008, REQ-FIX-ALR-005, REQ-DHL-001/002)."""

from __future__ import annotations

import logging
from collections.abc import Generator
from datetime import date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.alerts.models import AlertDispatch
from src.balance_alerts import webhook as wh
from src.balance_alerts.digest import (
    build_delivery_health,
    build_pulse,
    build_wealth_lines,
    post_pulse,
    render_delivery_health,
    render_pulse,
    render_wealth_pulse,
)
from src.models.base import Base
from src.models.brokerage import Account
from src.models.history import ExpectedAccount
from src.models.ingestion_log import IngestionLog
from src.models.plaid import PlaidAccountBalanceSnapshot as Snap

_ENGINE = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
Base.metadata.create_all(_ENGINE)
_Session = sessionmaker(bind=_ENGINE)


@pytest.fixture()
def session() -> Generator[Session, None, None]:
    s = _Session()
    yield s
    s.query(Snap).delete()
    s.query(Account).delete()
    s.query(AlertDispatch).delete()
    s.query(IngestionLog).delete()
    s.query(ExpectedAccount).delete()
    s.commit()
    s.close()


def _add(
    session: Session,
    acct_id: str,
    name: str,
    ptype: str,
    sub: str | None,
    bal: str,
    d: date = date(2026, 6, 14),
) -> None:
    session.add(
        Account(
            id=acct_id,
            broker="chase",
            account_number=f"n-{acct_id}",
            account_name=name,
            account_type="checking",
            entity="sparkry",
        )
    )
    session.add(
        Snap(
            account_id=acct_id,
            snapshot_date=d,
            plaid_account_type=ptype,
            plaid_account_subtype=sub,
            current_balance=Decimal(str(bal)),
            pulled_at=datetime(2026, 6, 14, 5, 0),
            raw_data={},
        )
    )


def test_pulse_lists_all_and_flags_breached(session: Session) -> None:
    _add(session, "a", "Sparkry checking", "depository", "checking", "66318.04")  # healthy
    _add(session, "b", "BlackLine", "depository", "checking", "800.00")  # breached (<10k)
    _add(session, "c", "Mortgage", "loan", "mortgage", "41000")  # muted → excluded
    session.commit()

    lines = build_pulse(date(2026, 6, 14), session)
    names = {ln.account_name for ln in lines}
    assert names == {"Sparkry checking", "BlackLine"}  # loan excluded
    breached = {ln.account_name for ln in lines if ln.breached}
    assert breached == {"BlackLine"}


def test_breached_sorted_first(session: Session) -> None:
    _add(session, "a", "Healthy", "depository", "checking", "50000")
    _add(session, "b", "Low", "depository", "checking", "500")
    session.commit()
    lines = build_pulse(date(2026, 6, 14), session)
    assert lines[0].account_name == "Low"  # breached first


def test_render_contains_balance_and_flag(session: Session) -> None:
    _add(session, "b", "BlackLine", "depository", "checking", "800.00")
    session.commit()
    text = render_pulse(build_pulse(date(2026, 6, 14), session), date(2026, 6, 14))
    assert "BlackLine" in text
    assert "$800" in text
    assert "flagged" in text


def test_savings_and_credit_breach_flags(session: Session) -> None:
    _add(session, "s", "PenFed Savings", "depository", "savings", "50.00")  # < $100
    _add(session, "sh", "Big Savings", "depository", "savings", "9000.00")  # healthy
    _add(session, "c", "Maxed Card", "credit", "credit card", "12000.00")  # >= $10k
    _add(session, "ch", "Low Card", "credit", "credit card", "500.00")  # healthy
    session.commit()
    flags = {ln.account_name: ln.breached for ln in build_pulse(date(2026, 6, 14), session)}
    assert flags["PenFed Savings"] is True
    assert flags["Big Savings"] is False
    assert flags["Maxed Card"] is True
    assert flags["Low Card"] is False


def test_checking_breach_uses_1k_floor_not_10k(session: Session) -> None:
    # A $9k checking balance is normal — must NOT be flagged (regression on the
    # old `<= $10k` breach threshold that flagged nearly everything).
    _add(session, "n", "Normal Checking", "depository", "checking", "9000.00")
    session.commit()
    line = build_pulse(date(2026, 6, 14), session)[0]
    assert line.breached is False


def test_one_account_exception_does_not_block_pulse(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for the per-account try/except in build_pulse: a raise
    while processing one account's snapshot must not blank the daily pulse
    for every other monitored account."""
    import src.balance_alerts.digest as digest_mod
    from src.balance_alerts.rules import cache_last_updated as orig_cache_last_updated

    session.add(
        Account(id="bad", broker="chase", account_number="n-bad",
                account_name="Bad Acct", account_type="checking", entity="sparkry")
    )
    session.add(
        Snap(
            account_id="bad",
            snapshot_date=date(2026, 6, 14),
            plaid_account_type="depository",
            plaid_account_subtype="checking",
            current_balance=Decimal("500.00"),
            pulled_at=datetime(2026, 6, 14, 5, 0),
            raw_data={"trigger": "raise"},
        )
    )
    session.add(
        Account(id="good", broker="chase", account_number="n-good",
                account_name="Good Acct", account_type="checking", entity="sparkry")
    )
    session.add(
        Snap(
            account_id="good",
            snapshot_date=date(2026, 6, 14),
            plaid_account_type="depository",
            plaid_account_subtype="checking",
            current_balance=Decimal("900.00"),
            pulled_at=datetime(2026, 6, 14, 5, 0),
            raw_data={},
        )
    )
    session.commit()

    def _cache_last_updated(raw_data: object) -> date | None:
        if isinstance(raw_data, dict) and raw_data.get("trigger") == "raise":
            raise RuntimeError("boom")
        return orig_cache_last_updated(raw_data)

    monkeypatch.setattr(digest_mod, "cache_last_updated", _cache_last_updated)

    lines = digest_mod.build_pulse(date(2026, 6, 14), session)

    assert {ln.account_name for ln in lines} == {"Good Acct"}


def test_post_pulse_dry_run(session: Session) -> None:
    _add(session, "a", "Sparkry checking", "depository", "checking", "66318.04")
    session.commit()
    res = post_pulse(date(2026, 6, 14), session, apply=False)
    assert res.status == "dry_run"


def test_post_pulse_https_guard(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    _add(session, "a", "Sparkry checking", "depository", "checking", "66318.04")
    session.commit()
    monkeypatch.setenv(wh.URL_ENV, "http://insecure.example/webhook")
    monkeypatch.setenv(wh.SECRET_ENV, "s3cret")
    res = post_pulse(date(2026, 6, 14), session, apply=True)
    assert res.status == "failed"
    assert "https" in (res.error or "")


def test_post_pulse_apply_sends_dedupes_and_never_logs_secret(
    session: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _add(session, "a", "Sparkry checking", "depository", "checking", "66318.04")
    session.commit()
    calls = {"n": 0}

    class _Resp:
        status_code: int = 200

    def _fake_post(url: str, json: dict[str, object], headers: dict[str, str], timeout: float) -> _Resp:
        calls["n"] += 1
        assert headers["X-Webhook-Secret"] == "PULSE-SECRET"
        return _Resp()

    monkeypatch.setenv(wh.URL_ENV, "https://n8n.example/webhook/alert")
    monkeypatch.setenv(wh.SECRET_ENV, "PULSE-SECRET")
    monkeypatch.setattr(httpx, "post", _fake_post)
    with caplog.at_level(logging.DEBUG):
        res = post_pulse(date(2026, 6, 14), session, apply=True)
    assert res.status == "sent"
    assert session.query(AlertDispatch).filter_by(alert_type="balance_pulse").count() == 1
    assert "PULSE-SECRET" not in caplog.text

    # Second same-day run → deduped, no second POST.
    res2 = post_pulse(date(2026, 6, 14), session, apply=True)
    assert res2.status == "skipped"
    assert calls["n"] == 1


def test_post_pulse_failed_then_retry_updates_same_audit_row(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pulse that failed earlier today flips the SAME ledger row to sent on
    retry (audit-trail accuracy), not a stuck `failed` row or a duplicate."""
    _add(session, "a", "Sparkry checking", "depository", "checking", "66318.04")
    session.commit()
    monkeypatch.setenv(wh.URL_ENV, "https://n8n.example/webhook/alert")
    monkeypatch.setenv(wh.SECRET_ENV, "s")

    class _Resp:
        def __init__(self, code: int) -> None:
            self.status_code = code

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(500))
    r1 = post_pulse(date(2026, 6, 14), session, apply=True)
    assert r1.status == "failed"

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(200))
    r2 = post_pulse(date(2026, 6, 14), session, apply=True)
    assert r2.status == "sent"

    rows = session.query(AlertDispatch).filter_by(alert_type="balance_pulse").all()
    assert len(rows) == 1  # same row, not a duplicate
    assert rows[0].status == "sent"


# ── REQ-FIX-ALR-005: pulse staleness ────────────────────────────────────────


def test_stale_snapshot_renders_as_of_marker_and_stale_count(session: Session) -> None:
    today = date(2026, 7, 6)
    _add(session, "a", "Fresh Checking", "depository", "checking", "5000.00", d=today)
    _add(
        session,
        "b",
        "Stale Checking",
        "depository",
        "checking",
        "2000.00",
        d=today - timedelta(days=3),
    )
    session.commit()
    text = render_pulse(build_pulse(today, session), today)
    assert "• Fresh Checking:" in text
    assert "$5,000" in text
    assert "• Stale Checking: ⏳7/3" in text
    assert "1 stale" in text


def test_fresh_snapshot_renders_bare_no_marker(session: Session) -> None:
    today = date(2026, 7, 6)
    _add(session, "a", "Fresh Checking", "depository", "checking", "5000.00", d=today)
    session.commit()
    text = render_pulse(build_pulse(today, session), today)
    assert "⏳" not in text
    assert "0 stale" in text


def test_1_day_old_snapshot_is_not_stale(session: Session) -> None:
    """Stale is `< today - 1 day`; exactly yesterday is still current."""
    today = date(2026, 7, 6)
    _add(session, "a", "Yesterday Checking", "depository", "checking", "5000.00",
         d=today - timedelta(days=1))
    session.commit()
    text = render_pulse(build_pulse(today, session), today)
    assert "⏳" not in text
    assert "0 stale" in text


def test_two_identical_consecutive_snapshots_do_not_trigger_stale_marker(
    session: Session,
) -> None:
    """REQ-FIX-PLD-001 / REQ-FIX-ALR-005 regression: `/accounts/get` returns
    Plaid's *cached* balance, so a re-written snapshot can carry the exact
    same `current_balance` as yesterday's. Staleness must be driven purely by
    `snapshot_date` recency, never by whether the value changed — two
    consecutive daily snapshots with an identical balance must still render
    as fresh (no `⏳` marker, 0 stale)."""
    today = date(2026, 7, 6)
    yesterday = today - timedelta(days=1)
    session.add(
        Account(
            id="a",
            broker="chase",
            account_number="n-a",
            account_name="Fresh Checking",
            account_type="checking",
            entity="sparkry",
        )
    )
    for d in (yesterday, today):
        session.add(
            Snap(
                account_id="a",
                snapshot_date=d,
                plaid_account_type="depository",
                plaid_account_subtype="checking",
                current_balance=Decimal("5000.00"),
                pulled_at=datetime(2026, 6, 14, 5, 0),
                raw_data={},
            )
        )
    session.commit()
    text = render_pulse(build_pulse(today, session), today)
    assert "⏳" not in text
    assert "0 stale" in text


def test_stale_cached_balance_marks_stale_even_when_snapshot_date_is_today(
    session: Session,
) -> None:
    """REQ-FIX-PLD-001: a snapshot written today can carry a Plaid *cached*
    balance that hasn't actually refreshed in days. When
    `balances.last_updated_datetime` says so, the pulse must render the `⏳`
    marker keyed off that true cache date — snapshot_date=today alone must
    not present a days-old cached value as current."""
    today = date(2026, 7, 6)
    session.add(
        Account(
            id="a",
            broker="chase",
            account_number="n-a",
            account_name="Stale Cache Checking",
            account_type="checking",
            entity="sparkry",
        )
    )
    session.add(
        Snap(
            account_id="a",
            snapshot_date=today,
            plaid_account_type="depository",
            plaid_account_subtype="checking",
            current_balance=Decimal("5000.00"),
            pulled_at=datetime(2026, 7, 6, 5, 0),
            raw_data={"balances": {"last_updated_datetime": "2026-07-02T00:00:00Z"}},
        )
    )
    session.commit()
    text = render_pulse(build_pulse(today, session), today)
    assert "⏳7/2" in text
    assert "1 stale" in text


# ── REQ-DHL-001/002: delivery-health block ──────────────────────────────────


def _ingestion_log(source: str, run_date: date, status: str = "success") -> IngestionLog:
    return IngestionLog(
        source=source,
        status=status,
        run_at=datetime.combine(run_date, datetime.min.time()),
    )


def test_dhl_full_block_golden_text(session: Session) -> None:
    """Exact delivery-health block text with all four silent-failure fixtures
    present at once: a snapshot gap, a failed webhook POST, an unmapped-account
    skip, and a stale sync."""
    today = date(2026, 7, 6)
    _add(session, "a", "Sparkry Checking", "depository", "checking", "66318.42", d=today)
    _add(
        session,
        "b",
        "BlackLine Checking",
        "depository",
        "checking",
        "2015.10",
        d=today - timedelta(days=3),
    )
    _add(session, "c", "Blue Business Plus", "credit", "credit card", "1912.55", d=today)
    session.add(_ingestion_log("plaid_balance:Amex", today))
    session.add(_ingestion_log("plaid_balance:Chase", today - timedelta(days=3)))
    yesterday = (today - timedelta(days=1)).isoformat()
    session.add(
        AlertDispatch(
            alert_key="balance:x:checking:1000", occurrence_date=yesterday,
            alert_type="balance_milestone", entity="sparkry", subject="s1", status="sent",
        )
    )
    session.add(
        AlertDispatch(
            alert_key="balance:y:checking:1000", occurrence_date=yesterday,
            alert_type="balance_milestone", entity="sparkry", subject="s2", status="sent",
        )
    )
    session.add(
        AlertDispatch(
            alert_key="balance:z:checking:1000", occurrence_date=yesterday,
            alert_type="balance_milestone", entity="sparkry", subject="s3", status="failed",
        )
    )
    session.add(
        ExpectedAccount(
            institution="Chase", account_name="Chase Freedom", last_4="4321",
            status="unconfirmed", source="plaid",
        )
    )
    session.commit()

    lines = build_pulse(today, session)
    health = build_delivery_health(today, session, lines)
    message = render_pulse(lines, today) + "\n\n" + render_delivery_health(health)

    expected = (
        "<pre>BUSINESS ACCOUNTS\n"
        "━━━━━━━━━━━━━━\n"
        "💵 CHECKING · $68,334\n"
        "━━━━━━━━━━━━━━\n"
        "• Sparkry Checking:     $66,318\n"
        "• BlackLine Checking:    $2,015\n"
        "  ⏳7/3\n\n"
        "━━━━━━━━━━━━━━\n"
        "💳 CREDIT · $1,913\n"
        "━━━━━━━━━━━━━━\n"
        "• Blue Business Plus:    $1,913\n"
        "━━━━━━━━━━━━━━\n"
        "3 accounts · 0 flagged · 1 stale</pre>\n\n"
        "Delivery\n"
        "  sync: amex ✓0d · chase ⏳3d\n"
        "  y'day: 2 sent · 1 failed · 0 skipped\n"
        "  unmapped: Chase Freedom ·4321·\n"
        "  gap: BlackLine Checking 3d"
    )
    assert message == expected


def test_dhl_healthy_collapses_to_single_line(session: Session) -> None:
    today = date(2026, 7, 6)
    _add(session, "a", "Sparkry Checking", "depository", "checking", "66318.42", d=today)
    session.add(_ingestion_log("plaid_balance:Chase", today))
    session.commit()
    lines = build_pulse(today, session)
    health = build_delivery_health(today, session, lines)
    assert health.healthy is True
    assert render_delivery_health(health) == "Delivery ✓ syncs<24h · 0 failed · 0 unmapped"


def test_dhl_missed_snapshot_day_produces_gap_line(session: Session) -> None:
    today = date(2026, 7, 6)
    _add(session, "a", "Gap Checking", "depository", "checking", "1000.00",
         d=today - timedelta(days=2))
    session.commit()
    lines = build_pulse(today, session)
    health = build_delivery_health(today, session, lines)
    assert health.healthy is False
    text = render_delivery_health(health)
    assert "gap: Gap Checking 2d" in text
    assert "unmapped:" not in text


def test_dhl_failed_post_produces_yday_line(session: Session) -> None:
    today = date(2026, 7, 6)
    _add(session, "a", "Healthy Checking", "depository", "checking", "5000.00", d=today)
    yesterday = (today - timedelta(days=1)).isoformat()
    session.add(
        AlertDispatch(
            alert_key="balance:x:checking:1000", occurrence_date=yesterday,
            alert_type="balance_milestone", entity="sparkry", subject="s1", status="failed",
        )
    )
    session.commit()
    lines = build_pulse(today, session)
    health = build_delivery_health(today, session, lines)
    assert health.healthy is False
    text = render_delivery_health(health)
    assert "y'day: 0 sent · 1 failed · 0 skipped" in text
    assert "gap:" not in text
    assert "unmapped:" not in text


def test_dhl_unmapped_skip_produces_unmapped_line(session: Session) -> None:
    today = date(2026, 7, 6)
    _add(session, "a", "Healthy Checking", "depository", "checking", "5000.00", d=today)
    session.add(
        ExpectedAccount(
            institution="Chase", account_name="Chase Freedom", last_4="4321",
            status="unconfirmed", source="plaid",
        )
    )
    session.commit()
    lines = build_pulse(today, session)
    health = build_delivery_health(today, session, lines)
    assert health.healthy is False
    text = render_delivery_health(health)
    assert "unmapped: Chase Freedom ·4321·" in text
    assert "gap:" not in text


def test_dhl_ignored_expected_account_excluded_from_unmapped(session: Session) -> None:
    """REQ-FIX-PLD-005: an ignore-listed account never counts as unmapped."""
    today = date(2026, 7, 6)
    _add(session, "a", "Healthy Checking", "depository", "checking", "5000.00", d=today)
    session.add(
        ExpectedAccount(
            institution="Chase", account_name="Ignored Card", last_4="9999",
            status="ignored", source="plaid",
        )
    )
    session.commit()
    lines = build_pulse(today, session)
    health = build_delivery_health(today, session, lines)
    assert health.unmapped == []


def test_dhl_stale_sync_produces_sync_line_with_hourglass(session: Session) -> None:
    today = date(2026, 7, 6)
    _add(session, "a", "Healthy Checking", "depository", "checking", "5000.00", d=today)
    session.add(_ingestion_log("plaid_tx:Chase", today - timedelta(days=5)))
    session.commit()
    lines = build_pulse(today, session)
    health = build_delivery_health(today, session, lines)
    assert health.healthy is False
    text = render_delivery_health(health)
    assert "sync: chase ⏳5d" in text


# ── REQ-DFB-002/005: day-change amounts (phone format, Mon–Fri only) ────────


def _add_snap(session: Session, acct_id: str, bal: str, d: date) -> None:
    session.add(
        Snap(
            account_id=acct_id,
            snapshot_date=d,
            plaid_account_type="depository",
            plaid_account_subtype="checking",
            current_balance=Decimal(bal),
            pulled_at=datetime.combine(d, datetime.min.time()),
            raw_data={},
        )
    )


_MONDAY = date(2026, 8, 3)
_SUNDAY = date(2026, 8, 2)


def test_day_change_rendered_from_previous_snapshot(session: Session) -> None:
    _add(session, "a", "Sparkry Checking", "depository", "checking", "100135.19", d=_MONDAY)
    _add_snap(session, "a", "99000.19", _MONDAY - timedelta(days=1))
    session.commit()
    text = render_pulse(build_pulse(_MONDAY, session), _MONDAY)
    # Long name + delta overflows the amount column → tag drops to an
    # indented continuation line; the amount column never moves.
    assert "• Sparkry Checking:    $100,135\n  ▲1,135" in text


def test_day_change_negative_with_multiday_window_labeled(session: Session) -> None:
    """A previous snapshot more than 1 day older is a multi-day move — the
    delta must carry its baseline date so it is never mis-read as one day."""
    _add(session, "a", "Sparkry Checking", "depository", "checking", "900.00", d=_MONDAY)
    _add_snap(session, "a", "950.00", _MONDAY - timedelta(days=3))
    session.commit()
    text = render_pulse(build_pulse(_MONDAY, session), _MONDAY)
    # REQ-DFB-006: multi-day baselines render NO delta (the ⏳ tag owns
    # oldness) — a 3-day move must not masquerade as a day change.
    assert "▼" not in text
    assert "$900" in text


def test_no_previous_snapshot_renders_no_change_segment(session: Session) -> None:
    _add(session, "a", "Sparkry Checking", "depository", "checking", "5000.00", d=_MONDAY)
    session.commit()
    text = render_pulse(build_pulse(_MONDAY, session), _MONDAY)
    assert "• Sparkry Checking:" in text
    assert "$5,000" in text
    assert "▲" not in text
    assert "▼" not in text


def test_zero_day_change_omitted(session: Session) -> None:
    """REQ-DFB-005: (+$0.00) noise is gone — sub-dollar deltas render nothing."""
    _add(session, "a", "Sparkry Checking", "depository", "checking", "5000.00", d=_MONDAY)
    _add_snap(session, "a", "5000.00", _MONDAY - timedelta(days=1))
    session.commit()
    text = render_pulse(build_pulse(_MONDAY, session), _MONDAY)
    assert "$5,000" in text
    assert "▲" not in text


def test_weekend_renders_no_deltas(session: Session) -> None:
    """REQ-DFB-005: deltas are Mon–Fri only."""
    _add(session, "a", "Sparkry Checking", "depository", "checking", "6000.00", d=_SUNDAY)
    _add_snap(session, "a", "5000.00", _SUNDAY - timedelta(days=1))
    session.commit()
    text = render_pulse(build_pulse(_SUNDAY, session), _SUNDAY)
    assert "$6,000" in text
    assert "▲" not in text


# ── REQ-DFB-001/003/004: wealth-D1-sourced flash ────────────────────────────
# The daily flash mirrors the /wealth page: every personal account from the
# (extended) freshness payload — cash, credit, investment, loan,
# statement-fed — split from the business register message.


_WEALTH_PAYLOAD: dict[str, object] = {
    "accounts": [
        {
            "account_id": "d1-etrade",
            "account_name": "E-Trade Stocks",
            "broker": "etrade",
            "account_type": "taxable",
            "source": "plaid",
            "latest_snapshot_date": "2026-08-03",
            "plaid_account_type": "brokerage",
            "latest_balance": "2082694.0000",
            "previous_snapshot_date": "2026-08-02",
            "previous_balance": "2070000.0000",
        },
        {
            "account_id": "d1-529",
            "account_name": "Aiden 529",
            "broker": "vanguard",
            "account_type": "529",
            "source": "plaid",
            "latest_snapshot_date": "2026-08-03",
            "plaid_account_type": "investment",
            "latest_balance": "93185.0600",
            "previous_snapshot_date": None,
            "previous_balance": None,
        },
        {
            "account_id": "d1-checking",
            "account_name": "Sparks Checking",
            "broker": "chase",
            "account_type": "checking",
            "source": "plaid",
            "latest_snapshot_date": "2026-08-03",
            "plaid_account_type": "depository",
            "latest_balance": "12000.0000",
            "previous_snapshot_date": "2026-08-02",
            "previous_balance": "11000.0000",
        },
        {
            "account_id": "d1-visa",
            "account_name": "Prime Visa",
            "broker": "chase",
            "account_type": "credit_card",
            "source": "plaid",
            "latest_snapshot_date": "2026-08-03",
            "plaid_account_type": "credit",
            "latest_balance": "1500.0000",
            "previous_snapshot_date": "2026-08-02",
            "previous_balance": "1400.0000",
        },
        {
            # statement-fed (REQ-DFB-003): no plaid type; D1 account_type maps.
            "account_id": "d1-wholelife",
            "account_name": "Whole Life",
            "broker": "nw_mutual",
            "account_type": "other",
            "source": "statement",
            "latest_snapshot_date": "2026-01-15",
            "plaid_account_type": None,
            "latest_balance": "52000.00",
            "previous_snapshot_date": "2025-01-15",
            "previous_balance": "48000.00",
        },
        {
            # zero snapshots — no renderable value.
            "account_id": "d1-empty",
            "account_name": "Frozen IRA",
            "broker": "vanguard",
            "account_type": "trad_ira",
            "source": "plaid",
            "latest_snapshot_date": None,
            "plaid_account_type": None,
            "latest_balance": None,
            "previous_snapshot_date": None,
            "previous_balance": None,
        },
    ],
    "generated_at": "2026-08-02T14:00:00Z",
}


def test_wealth_lines_cover_every_account_type_with_section_kinds() -> None:
    lines, skipped = build_wealth_lines(_WEALTH_PAYLOAD)
    assert skipped == 0
    kinds = {ln.account_name: ln.kind for ln in lines}
    assert kinds == {
        "E-Trade Stocks": "stocks",
        "Aiden 529": "529",
        "Sparks Checking": "cash",
        "Prime Visa": "credit",
        "Whole Life": "other",
    }
    etrade = next(ln for ln in lines if ln.account_name == "E-Trade Stocks")
    assert etrade.balance == Decimal("2082694.0000")
    assert etrade.day_change == Decimal("12694.0000")
    assert not any(ln.hidden for ln in lines)


def test_wealth_line_malformed_row_isolated_and_counted() -> None:
    payload: dict[str, object] = {
        "accounts": [
            {"account_name": "Bad", "account_type": "taxable", "source": "plaid",
             "plaid_account_type": "investment",
             "latest_balance": "not-a-number", "latest_snapshot_date": "2026-08-02"},
            dict(_WEALTH_PAYLOAD["accounts"][0]),  # type: ignore[index]
        ]
    }
    lines, skipped = build_wealth_lines(payload)
    assert skipped == 1  # surfaced in the flash note, never silently gone
    assert {ln.account_name for ln in lines} == {"E-Trade Stocks"}


def test_render_wealth_pulse_sections_and_footer() -> None:
    today = _MONDAY
    lines, _ = build_wealth_lines(_WEALTH_PAYLOAD)
    text = render_wealth_pulse(lines, today)
    assert text.startswith("<pre>PERSONAL ACCOUNTS\n")
    assert text.endswith("</pre>")
    cash_i = text.index("💵 CASH · $12,000")
    credit_i = text.index("💳 CREDIT · $1,500")
    stocks_i = text.index("📈 STOCKS · $2,082,694")
    f29_i = text.index("📈 529s · $93,185")
    other_i = text.index("📦 OTHER · $52,000")
    nw_i = text.index("💰 NET WORTH · $2,238,379 ▲13,594")
    assert cash_i < credit_i < stocks_i < f29_i < other_i < nw_i
    # Right-aligned amount column; over-long tag drops to a continuation line.
    assert "• Prime Visa: ▲100       $1,500" in text
    assert "• E-Trade Stocks:    $2,082,694\n  ▲12,694" in text
    # Stale statement row: compact M/D tag, no multi-day delta.
    assert "• Whole Life: ⏳1/15     $52,000" in text
    # NW breakdown rows, credit sign-flipped.
    assert "• Cash:" in text and "• Credit:" in text and "-$1,500" in text
    assert "• 529s:" in text
    assert "5 accounts · 1 stale" in text
    assert "Delivery" not in text
    assert "flagged" not in text


def test_render_wealth_pulse_weekend_omits_all_deltas() -> None:
    lines, _ = build_wealth_lines(_WEALTH_PAYLOAD)
    text = render_wealth_pulse(lines, _SUNDAY)
    assert "▲" not in text
    assert "▼" not in text
    assert "💰 NET WORTH · $2,238,379" in text


def test_hidden_accounts_count_in_totals_but_render_no_row() -> None:
    """REQ-DFB-006: config/auto hides drop the row, keep the money."""
    payload: dict[str, object] = {
        "accounts": [
            dict(_WEALTH_PAYLOAD["accounts"][0]),  # type: ignore[index]
            {
                # dust — auto-hidden below $100
                "account_id": "d1-dust",
                "account_name": "Individual - TOD",
                "broker": "vanguard",
                "account_type": "tod",
                "source": "statement",
                "latest_snapshot_date": "2026-08-03",
                "plaid_account_type": None,
                "latest_balance": "49.52",
                "previous_snapshot_date": None,
                "previous_balance": None,
            },
        ]
    }
    lines, _ = build_wealth_lines(payload)
    dust = next(ln for ln in lines if ln.account_name == "Individual - TOD")
    assert dust.hidden is True
    text = render_wealth_pulse(lines, _MONDAY)
    assert "Individual - TOD" not in text
    # Section total still includes the hidden $49.52: 2,082,694.00 + 49.52
    assert "📈 STOCKS · $2,082,744" in text
    assert "1 account · 0 stale" in text  # footer counts visible only


def test_flash_config_alias_and_section_applied() -> None:
    """A configured account renders its alias in its configured section."""
    payload: dict[str, object] = {
        "accounts": [
            {
                # real config key: BrokerageLink → Fidelity 401k, RETIREMENT
                "account_id": "3c12c098-30b9-4c44-85dd-e273f5b97482",
                "account_name": "BrokerageLink",
                "broker": "fidelity",
                "account_type": "brokeragelink",
                "source": "statement",
                "latest_snapshot_date": "2026-08-03",
                "plaid_account_type": None,
                "latest_balance": "149114.79",
                "previous_snapshot_date": None,
                "previous_balance": None,
            }
        ]
    }
    lines, _ = build_wealth_lines(payload)
    assert lines[0].account_name == "Fidelity 401k"
    assert lines[0].kind == "retirement"
    text = render_wealth_pulse(lines, _MONDAY)
    assert "📈 RETIREMENT · $149,115" in text
    assert "• Fidelity 401k:" in text


def test_render_wealth_pulse_note_renders_warning_line() -> None:
    today = date(2026, 8, 2)
    lines, _ = build_wealth_lines(_WEALTH_PAYLOAD)
    text = render_wealth_pulse(lines, today, "1 malformed row(s) skipped")
    assert text.endswith("⚠️ wealth source: 1 malformed row(s) skipped</pre>")


def test_wealth_stale_line_renders_as_of_marker() -> None:
    today = _MONDAY
    payload: dict[str, object] = {
        "accounts": [
            {
                "account_id": "d1-x",
                "account_name": "Stuck IRA",
                "broker": "vanguard",
                "account_type": "trad_ira",
                "source": "plaid",
                "latest_snapshot_date": "2026-07-27",
                "plaid_account_type": "investment",
                "latest_balance": "400186.1300",
                "previous_snapshot_date": "2026-07-26",
                "previous_balance": "400000.0000",
            }
        ]
    }
    lines, _ = build_wealth_lines(payload)
    text = render_wealth_pulse(lines, today)
    assert "• Stuck IRA: ⏳7/27     $400,186" in text
    assert "1 stale" in text


def test_unpaired_previous_balance_renders_no_day_change() -> None:
    """P1 (2026-08-02 review): a previous_balance without its snapshot date
    must not render a delta — it would read as a same-day change."""
    payload: dict[str, object] = {
        "accounts": [
            {
                "account_id": "d1-x",
                "account_name": "Odd Row",
                "broker": "vanguard",
                "account_type": "trad_ira",
                "source": "plaid",
                "latest_snapshot_date": "2026-08-02",
                "plaid_account_type": "investment",
                "latest_balance": "1000.0000",
                "previous_snapshot_date": None,
                "previous_balance": "900.0000",
            }
        ]
    }
    lines, skipped = build_wealth_lines(payload)
    assert skipped == 0
    assert lines[0].day_change is None
    text = render_wealth_pulse(lines, _MONDAY)
    assert "▲" not in text


def test_unparseable_previous_pair_keeps_line_without_day_change() -> None:
    """A bad baseline drops only the delta, never the whole account line."""
    payload: dict[str, object] = {
        "accounts": [
            {
                "account_id": "d1-x",
                "account_name": "Bad Baseline",
                "broker": "vanguard",
                "account_type": "trad_ira",
                "source": "plaid",
                "latest_snapshot_date": "2026-08-02",
                "plaid_account_type": "investment",
                "latest_balance": "1000.0000",
                "previous_snapshot_date": "2026-08-02",
                "previous_balance": "not-a-number",
            }
        ]
    }
    lines, skipped = build_wealth_lines(payload)
    assert skipped == 0
    assert [ln.account_name for ln in lines] == ["Bad Baseline"]
    assert lines[0].day_change is None


class _CapturingPost:
    """Capture every webhook POST body, keyed by message title."""

    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    def __call__(
        self, url: str, json: dict[str, object], headers: dict[str, str], timeout: float
    ) -> object:
        self.payloads.append(dict(json))

        class _Resp:
            status_code = 200

        return _Resp()

    def by_title_prefix(self, prefix: str) -> dict[str, object] | None:
        for p in self.payloads:
            if str(p.get("title", "")).startswith(prefix):
                return p
        return None


def _webhook_env(monkeypatch: pytest.MonkeyPatch) -> _CapturingPost:
    cap = _CapturingPost()
    monkeypatch.setenv(wh.URL_ENV, "https://n8n.example/webhook/alert")
    monkeypatch.setenv(wh.SECRET_ENV, "s3cret")
    monkeypatch.setattr(httpx, "post", cap)
    return cap


def test_post_pulse_sends_wealth_and_business_as_separate_messages(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.balance_alerts.digest as digest_mod

    today = _MONDAY
    _add(session, "loc-chk", "Sparkry Checking", "depository", "checking", "100135.19", d=today)
    _add(session, "loc-inv", "Joint Tenant", "investment", "brokerage", "2000000.00",
         d=date(2026, 7, 27))
    session.commit()

    monkeypatch.setattr(
        digest_mod, "fetch_wealth_freshness", lambda: ("ok", _WEALTH_PAYLOAD)
    )
    cap = _webhook_env(monkeypatch)

    res = post_pulse(today, session, apply=True)
    assert res.status == "sent"
    assert len(cap.payloads) == 2

    wealth = cap.by_title_prefix("📊 Wealth Snapshot")
    assert wealth is not None
    wmsg = str(wealth["message"])
    assert "• E-Trade Stocks:    $2,082,694\n  ▲12,694" in wmsg
    assert "Sparks Checking" in wmsg
    assert "Joint Tenant" not in wmsg  # frozen local line replaced
    assert "Sparkry Checking" not in wmsg  # business never in the wealth flash
    assert "Delivery" not in wmsg

    business = cap.by_title_prefix("🏢 Business Accounts")
    assert business is not None
    bmsg = str(business["message"])
    assert "• Sparkry Checking:    $100,135" in bmsg
    assert "Joint Tenant" not in bmsg  # frozen investment rows are dead
    assert "E-Trade Stocks" not in bmsg
    assert "Delivery" not in bmsg

    # Independent ledger rows, independent dedup keys.
    keys = {r.alert_key for r in session.query(AlertDispatch).all()}
    assert f"wealth:pulse:{today.isoformat()}" in keys
    assert f"balance:pulse:{today.isoformat()}" in keys

    # Second same-day run → both deduped, no third POST.
    res2 = post_pulse(today, session, apply=True)
    assert res2.status == "skipped"
    assert len(cap.payloads) == 2


def test_post_pulse_wealth_error_degrades_with_note(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wealth source down → flash still sends: frozen local investment lines
    (stale-marked) + an explicit ⚠️ note, never a silent gap."""
    import src.balance_alerts.digest as digest_mod

    today = date(2026, 8, 2)
    _add(session, "loc-inv", "Joint Tenant", "investment", "brokerage", "2000000.00",
         d=date(2026, 7, 27))
    session.commit()

    monkeypatch.setattr(digest_mod, "fetch_wealth_freshness", lambda: ("error", None))
    cap = _webhook_env(monkeypatch)

    res = post_pulse(today, session, apply=True)
    assert res.status == "sent"
    wealth = cap.by_title_prefix("📊 Wealth Snapshot")
    assert wealth is not None
    wmsg = str(wealth["message"])
    assert "Joint Tenant" in wmsg
    assert "⏳7/27" in wmsg
    assert "⚠️ wealth source: unreachable — showing last local values" in wmsg


def test_post_pulse_wealth_fetch_raise_degrades_to_local(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.balance_alerts.digest as digest_mod

    today = date(2026, 8, 2)
    _add(session, "loc-inv", "Joint Tenant", "investment", "brokerage", "2000000.00",
         d=date(2026, 7, 27))
    session.commit()

    def _boom() -> tuple[str, dict[str, object] | None]:
        raise RuntimeError("wealth fetch exploded")

    monkeypatch.setattr(digest_mod, "fetch_wealth_freshness", _boom)
    cap = _webhook_env(monkeypatch)

    res = post_pulse(today, session, apply=True)
    assert res.status == "sent"
    wealth = cap.by_title_prefix("📊 Wealth Snapshot")
    assert wealth is not None
    assert "Joint Tenant" in str(wealth["message"])
    assert "⚠️ wealth source: fetch crashed" in str(wealth["message"])


def test_post_pulse_no_business_lines_sends_only_wealth(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.balance_alerts.digest as digest_mod

    today = date(2026, 8, 2)
    monkeypatch.setattr(
        digest_mod, "fetch_wealth_freshness", lambda: ("ok", _WEALTH_PAYLOAD)
    )
    cap = _webhook_env(monkeypatch)

    res = post_pulse(today, session, apply=True)
    assert res.status == "sent"
    assert len(cap.payloads) == 1
    assert cap.by_title_prefix("📊 Wealth Snapshot") is not None


def test_post_pulse_pulse_compute_failure_degrades_not_crashes(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQ-DHL-001/REQ-BAL-010 isolation: a raise inside build_pulse must not
    propagate out of post_pulse (the live timer runs --apply --digest; an
    uncaught raise would trip OnFailure after the milestone dispatch already
    committed)."""
    import src.balance_alerts.digest as digest_mod

    def _boom(today: date, session: Session) -> list[object]:
        raise RuntimeError("pulse compute exploded")

    monkeypatch.setattr(digest_mod, "build_pulse", _boom)
    res = post_pulse(date(2026, 7, 7), session, apply=True)
    assert res.status == "failed"
    assert "digest compute error" in (res.error or "")
