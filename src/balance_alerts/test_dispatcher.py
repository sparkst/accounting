"""Tests for the balance-alert dispatcher (REQ-BAL-006 dedup, REQ-BAL-010 isolation)."""

from __future__ import annotations

from collections.abc import Generator
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import src.balance_alerts.dispatcher as disp
from src.alerts.models import AlertDispatch
from src.alerts.webhook import WebhookResult
from src.balance_alerts.dispatcher import dispatch_balance_alerts
from src.balance_alerts.rules import BalanceAlert
from src.models.base import Base

_ENGINE = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
Base.metadata.create_all(_ENGINE)
_Session = sessionmaker(bind=_ENGINE)


@pytest.fixture()
def session() -> Generator[Session, None, None]:
    s = _Session()
    yield s
    s.query(AlertDispatch).delete()
    s.commit()
    s.close()


def _alert(key: str = "balance:acc:checking:1000", sev: str = "sev3") -> BalanceAlert:
    return BalanceAlert(
        alert_key=key,
        occurrence_date="2026-06-14",
        alert_type="balance_milestone",
        severity=sev,
        entity="sparkry",
        account_id="acc",
        account_name="Sparkry checking",
        kind="checking",
        level="1000",
        baseline="1500.00",
        new_balance="900.00",
        title="Sparkry checking below $1,000.00",
        message="fell",
    )


def test_apply_sends_records_and_dedupes(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(disp, "compute_balance_alerts", lambda today, s: [_alert()])
    monkeypatch.setattr(
        disp, "post_balance_alert", lambda a, apply: WebhookResult("sent", 200, None)
    )
    s1 = dispatch_balance_alerts(date(2026, 6, 14), session, apply=True)
    assert (s1.sent, s1.skipped, s1.failed) == (1, 0, 0)
    assert session.query(AlertDispatch).count() == 1

    # Second run same day → skipped (REQ-BAL-006).
    s2 = dispatch_balance_alerts(date(2026, 6, 14), session, apply=True)
    assert s2.skipped == 1
    assert s2.sent == 0
    assert session.query(AlertDispatch).count() == 1


def test_dry_run_writes_nothing(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(disp, "compute_balance_alerts", lambda today, s: [_alert()])
    monkeypatch.setattr(
        disp, "post_balance_alert", lambda a, apply: WebhookResult("dry_run", None, None)
    )
    summary = dispatch_balance_alerts(date(2026, 6, 14), session, apply=False)
    assert summary.dry_run == 1
    assert session.query(AlertDispatch).count() == 0


def test_per_alert_error_isolation(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = _alert(key="balance:a:checking:1000")
    bad = _alert(key="balance:b:checking:1000")
    monkeypatch.setattr(disp, "compute_balance_alerts", lambda today, s: [bad, good])

    def _post(a: BalanceAlert, apply: bool) -> WebhookResult:
        if a.alert_key == "balance:b:checking:1000":
            raise RuntimeError("boom")
        return WebhookResult("sent", 200, None)

    monkeypatch.setattr(disp, "post_balance_alert", _post)
    summary = dispatch_balance_alerts(date(2026, 6, 14), session, apply=True)
    # One raised, the other still sent (REQ-BAL-010).
    assert summary.sent == 1
    assert summary.failed == 1


def test_failed_post_not_marked_sent_so_retries(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(disp, "compute_balance_alerts", lambda today, s: [_alert()])
    monkeypatch.setattr(
        disp,
        "post_balance_alert",
        lambda a, apply: WebhookResult("failed", 500, "non-2xx"),
    )
    s1 = dispatch_balance_alerts(date(2026, 6, 14), session, apply=True)
    assert s1.failed == 1
    # Recorded as "failed" (not "sent") → next run retries rather than skips.
    s2 = dispatch_balance_alerts(date(2026, 6, 14), session, apply=True)
    assert s2.failed == 1
    assert s2.skipped == 0


def test_apply_records_payload_json_and_delivery_channel(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQ-FIX-ALR-002: every written row persists the exact payload + channel."""
    monkeypatch.setattr(disp, "compute_balance_alerts", lambda today, s: [_alert()])
    monkeypatch.setattr(
        disp, "post_balance_alert", lambda a, apply: WebhookResult("sent", 200, None)
    )
    dispatch_balance_alerts(date(2026, 6, 14), session, apply=True)
    row = session.query(AlertDispatch).filter_by(
        alert_key="balance:acc:checking:1000"
    ).one()
    assert row.delivery_channel == "n8n_webhook"
    assert row.payload_json is not None
    import json

    payload = json.loads(row.payload_json)
    assert payload["alert_key"] == "balance:acc:checking:1000"


def test_apply_sweeps_prior_failed_row_before_computing_today(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQ-FIX-ALR-002: a failed row from a prior day is resent at the top of
    the next --apply run."""
    session.add(
        AlertDispatch(
            alert_key="balance:acc:checking:5000",
            occurrence_date="2026-06-13",
            alert_type="balance_milestone",
            entity="sparkry",
            subject="Sparkry checking below $5,000.00",
            status="failed",
            delivery_channel="n8n_webhook",
            payload_json='{"alert_key": "balance:acc:checking:5000"}',
        )
    )
    session.commit()
    monkeypatch.setattr(disp, "compute_balance_alerts", lambda today, s: [])
    posted_keys: list[str] = []

    def _fake_post_payload(payload, *, key, apply, timeout=10.0):  # type: ignore[no-untyped-def]
        posted_keys.append(key)
        from src.alerts.webhook import WebhookResult as WR

        return WR("sent", 200, None)

    monkeypatch.setattr(disp, "post_payload", _fake_post_payload)
    dispatch_balance_alerts(date(2026, 6, 14), session, apply=True)

    assert "balance:acc:checking:5000" in posted_keys
    row = session.query(AlertDispatch).filter_by(
        alert_key="balance:acc:checking:5000"
    ).one()
    assert row.status == "sent"


def test_same_key_different_day_both_send(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQ-BAL-006: a re-crossing on a LATER day is a distinct (key, occurrence)
    and sends again — the ledger keys on the day, not just the level."""
    monkeypatch.setattr(
        disp, "post_balance_alert", lambda a, apply: WebhookResult("sent", 200, None)
    )

    def _on(occ: str) -> None:
        alert = _alert()
        object.__setattr__(alert, "occurrence_date", occ)
        monkeypatch.setattr(disp, "compute_balance_alerts", lambda today, s: [alert])
        dispatch_balance_alerts(date.fromisoformat(occ), session, apply=True)

    _on("2026-06-14")  # day 1 crossing
    _on("2026-06-16")  # recovered, re-crossed two days later
    rows = session.query(AlertDispatch).filter_by(
        alert_key="balance:acc:checking:1000"
    ).all()
    assert {r.occurrence_date for r in rows} == {"2026-06-14", "2026-06-16"}
    assert all(r.status == "sent" for r in rows)
