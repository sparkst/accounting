"""Tests for the alert dispatcher.

REQ-ID: REQ-ALERT-006 (dedup — running twice sends once)
REQ-ID: REQ-ALERT-008 (per-alert error isolation)
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import src.alerts.dispatcher as dispatcher_mod
from src.alerts.dispatcher import dispatch_alerts
from src.alerts.models import AlertDispatch
from src.alerts.rules import Alert
from src.alerts.webhook import WebhookResult
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


def _alert(key: str) -> Alert:
    return Alert(
        alert_key=key,
        occurrence_date="2026-05-10",
        alert_type="tax_bo",
        entity="sparkry",
        subject="subj",
        body_text="body",
        due_date="2026-05-25",
        action_url="https://secure.dor.wa.gov/home/Login",
    )


def test_apply_records_sent_and_dedupes(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        dispatcher_mod, "compute_tax_alerts", lambda today: [_alert("k1")]
    )
    monkeypatch.setattr(
        dispatcher_mod, "compute_invoice_alerts", lambda today, s: []
    )
    monkeypatch.setattr(
        dispatcher_mod, "post_alert", lambda a, apply: WebhookResult("sent", 200, None)
    )

    s1 = dispatch_alerts(session, date(2026, 5, 10), apply=True)
    assert s1.sent == 1
    # Second run on the same day must dedupe.
    s2 = dispatch_alerts(session, date(2026, 5, 10), apply=True)
    assert s2.sent == 0
    assert s2.skipped == 1
    rows = session.query(AlertDispatch).filter_by(alert_key="k1").all()
    assert len(rows) == 1


def test_failed_alert_does_not_block_others_and_retries(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        dispatcher_mod,
        "compute_tax_alerts",
        lambda today: [_alert("bad"), _alert("good")],
    )
    monkeypatch.setattr(dispatcher_mod, "compute_invoice_alerts", lambda today, s: [])

    def _post(a: Alert, apply: bool) -> WebhookResult:
        if a.alert_key == "bad":
            return WebhookResult("failed", 500, "boom")
        return WebhookResult("sent", 200, None)

    monkeypatch.setattr(dispatcher_mod, "post_alert", _post)

    summary = dispatch_alerts(session, date(2026, 5, 10), apply=True)
    assert summary.sent == 1
    assert summary.failed == 1
    # The failed one is NOT marked sent, so it retries next run.
    bad = session.query(AlertDispatch).filter_by(alert_key="bad").one()
    assert bad.status == "failed"


def test_dry_run_writes_nothing(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        dispatcher_mod, "compute_tax_alerts", lambda today: [_alert("k1")]
    )
    monkeypatch.setattr(dispatcher_mod, "compute_invoice_alerts", lambda today, s: [])
    monkeypatch.setattr(
        dispatcher_mod,
        "post_alert",
        lambda a, apply: WebhookResult("dry_run", None, None),
    )
    summary = dispatch_alerts(session, date(2026, 5, 10), apply=False)
    assert summary.dry_run == 1
    assert session.query(AlertDispatch).count() == 0


def test_exception_in_post_is_isolated(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        dispatcher_mod,
        "compute_tax_alerts",
        lambda today: [_alert("boom"), _alert("ok")],
    )
    monkeypatch.setattr(dispatcher_mod, "compute_invoice_alerts", lambda today, s: [])

    def _post(a: Alert, apply: bool) -> WebhookResult:
        if a.alert_key == "boom":
            raise RuntimeError("kaboom")
        return WebhookResult("sent", 200, None)

    monkeypatch.setattr(dispatcher_mod, "post_alert", _post)
    summary = dispatch_alerts(session, date(2026, 5, 10), apply=True)
    assert summary.sent == 1
    assert summary.failed == 1


def test_catchup_marker_at_d3_evaluates_d2_through_d(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQ-FIX-ALR-004: a run-marker at D-3 causes D-2, D-1, D to be evaluated
    on the next run (a Persistent=true boot catch-up after downtime)."""
    seen_days: list[date] = []

    def _tax(today: date) -> list[Alert]:
        seen_days.append(today)
        return [_alert(f"tax:{today.isoformat()}")]

    monkeypatch.setattr(dispatcher_mod, "compute_tax_alerts", _tax)
    monkeypatch.setattr(dispatcher_mod, "compute_invoice_alerts", lambda today, s: [])
    monkeypatch.setattr(
        dispatcher_mod, "post_alert", lambda a, apply: WebhookResult("sent", 200, None)
    )

    # Seed a run marker dated D-3.
    session.add(
        AlertDispatch(
            alert_key="ea:run",
            occurrence_date="2026-05-07",
            alert_type="run_marker",
            entity="all",
            subject="marker",
            status="sent",
        )
    )
    session.commit()

    dispatcher_mod.dispatch_alerts(session, date(2026, 5, 10), apply=True)

    assert seen_days == [date(2026, 5, 8), date(2026, 5, 9), date(2026, 5, 10)]
    # The month-end/missed-day alerts each dedupe on their own occurrence_date
    # (distinct alert_key here since our fake includes the date) — each fires once.
    rows = session.query(AlertDispatch).filter(
        AlertDispatch.alert_key.like("tax:%")
    ).all()
    assert len(rows) == 3


def _record_day_return_empty(seen_days: list[date], today: date) -> list[Alert]:
    seen_days.append(today)
    return []


def test_catchup_20_day_gap_capped_at_14(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen_days: list[date] = []
    monkeypatch.setattr(
        dispatcher_mod,
        "compute_tax_alerts",
        lambda today: _record_day_return_empty(seen_days, today),
    )
    monkeypatch.setattr(dispatcher_mod, "compute_invoice_alerts", lambda today, s: [])

    session.add(
        AlertDispatch(
            alert_key="ea:run",
            occurrence_date="2026-04-20",
            alert_type="run_marker",
            entity="all",
            subject="marker",
            status="sent",
        )
    )
    session.commit()

    dispatcher_mod.dispatch_alerts(session, date(2026, 5, 10), apply=True)

    assert len(seen_days) == 14
    assert seen_days[0] == date(2026, 4, 27)
    assert seen_days[-1] == date(2026, 5, 10)


def test_no_marker_evaluates_today_only(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen_days: list[date] = []
    monkeypatch.setattr(
        dispatcher_mod,
        "compute_tax_alerts",
        lambda today: _record_day_return_empty(seen_days, today),
    )
    monkeypatch.setattr(dispatcher_mod, "compute_invoice_alerts", lambda today, s: [])

    dispatcher_mod.dispatch_alerts(session, date(2026, 5, 10), apply=True)

    assert seen_days == [date(2026, 5, 10)]


def test_apply_writes_run_marker(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispatcher_mod, "compute_tax_alerts", lambda today: [])
    monkeypatch.setattr(dispatcher_mod, "compute_invoice_alerts", lambda today, s: [])

    dispatcher_mod.dispatch_alerts(session, date(2026, 5, 10), apply=True)

    marker = (
        session.query(AlertDispatch)
        .filter_by(alert_key="ea:run", occurrence_date="2026-05-10")
        .one()
    )
    assert marker.status == "sent"


def test_dry_run_writes_no_marker(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispatcher_mod, "compute_tax_alerts", lambda today: [])
    monkeypatch.setattr(dispatcher_mod, "compute_invoice_alerts", lambda today, s: [])

    dispatcher_mod.dispatch_alerts(session, date(2026, 5, 10), apply=False)

    assert session.query(AlertDispatch).filter_by(alert_key="ea:run").count() == 0


def test_dry_run_skips_already_sent(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # First run with apply=True sends the alert.
    monkeypatch.setattr(
        dispatcher_mod, "compute_tax_alerts", lambda today: [_alert("k1")]
    )
    monkeypatch.setattr(dispatcher_mod, "compute_invoice_alerts", lambda today, s: [])
    monkeypatch.setattr(
        dispatcher_mod, "post_alert", lambda a, apply: WebhookResult("sent", 200, None)
    )
    dispatch_alerts(session, date(2026, 5, 10), apply=True)

    # DRY-RUN on the same day: already-sent alert is skipped, not counted as dry_run.
    summary = dispatch_alerts(session, date(2026, 5, 10), apply=False)
    assert summary.skipped == 1
    assert summary.dry_run == 0
