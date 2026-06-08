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
