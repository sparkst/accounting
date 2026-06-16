"""Tests for the daily account-pulse digest (REQ-BAL-008)."""

from __future__ import annotations

import logging
from collections.abc import Generator
from datetime import date, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.alerts.models import AlertDispatch
from src.balance_alerts import webhook as wh
from src.balance_alerts.digest import build_pulse, post_pulse, render_pulse
from src.models.base import Base
from src.models.brokerage import Account
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
    text = render_pulse(build_pulse(date(2026, 6, 14), session))
    assert "BlackLine" in text
    assert "$800.00" in text
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
