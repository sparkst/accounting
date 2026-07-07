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


def test_compute_exception_on_one_catchup_day_does_not_block_others_or_marker(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for the compute-phase try/except added around the
    catch-up loop (dispatch_alerts): a raise while computing one day's
    alerts must not stop later catch-up days from being evaluated, and the
    run-marker must still be written at the end of the run."""
    seen_days: list[date] = []

    def _tax(today: date) -> list[Alert]:
        if today == date(2026, 5, 9):
            raise RuntimeError("boom")
        seen_days.append(today)
        return [_alert(f"tax:{today.isoformat()}")]

    monkeypatch.setattr(dispatcher_mod, "compute_tax_alerts", _tax)
    monkeypatch.setattr(dispatcher_mod, "compute_invoice_alerts", lambda today, s: [])
    monkeypatch.setattr(
        dispatcher_mod, "post_alert", lambda a, apply: WebhookResult("sent", 200, None)
    )

    # Run marker at D-2 so the catch-up window is [D-1 (raises), D (should
    # still run)].
    session.add(
        AlertDispatch(
            alert_key="ea:run",
            occurrence_date="2026-05-08",
            alert_type="run_marker",
            entity="all",
            subject="marker",
            status="sent",
        )
    )
    session.commit()

    dispatcher_mod.dispatch_alerts(session, date(2026, 5, 10), apply=True)

    # The raising day (05-09) never reached the alert loop; 05-10 still did.
    assert seen_days == [date(2026, 5, 10)]
    rows = session.query(AlertDispatch).filter(AlertDispatch.alert_key.like("tax:%")).all()
    assert len(rows) == 1
    assert rows[0].occurrence_date == "2026-05-10"
    # The run-marker write after the loop must not be skipped by the earlier
    # per-day exception.
    marker = (
        session.query(AlertDispatch)
        .filter_by(alert_key="ea:run", occurrence_date="2026-05-10")
        .one()
    )
    assert marker.status == "sent"


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


# ── REQ-FIX-ALR-002: sweep wiring + payload/channel persistence ────────────


def test_apply_sweeps_prior_failed_row_before_computing_today(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQ-FIX-ALR-002: a failed tax_bo row from a prior day is re-POSTed and
    flips to sent at the top of the next --apply run (mirrors
    src/balance_alerts/test_dispatcher.py::test_apply_sweeps_prior_failed_row_before_computing_today)."""
    session.add(
        AlertDispatch(
            alert_key="tax_bo:2026-04",
            occurrence_date="2026-05-09",
            alert_type="tax_bo",
            entity="sparkry",
            subject="WA B&O due",
            status="failed",
            delivery_channel="n8n_webhook",
            payload_json='{"alert_key": "tax_bo:2026-04"}',
        )
    )
    session.commit()
    monkeypatch.setattr(dispatcher_mod, "compute_tax_alerts", lambda today: [])
    monkeypatch.setattr(dispatcher_mod, "compute_invoice_alerts", lambda today, s: [])
    posted_payloads: list[dict[str, object]] = []

    def _fake_post_raw_payload(payload, *, apply, timeout=10.0):  # type: ignore[no-untyped-def]
        posted_payloads.append(payload)
        return WebhookResult("sent", 200, None)

    monkeypatch.setattr(dispatcher_mod, "post_raw_payload", _fake_post_raw_payload)
    dispatcher_mod.dispatch_alerts(session, date(2026, 5, 10), apply=True)

    assert posted_payloads == [{"alert_key": "tax_bo:2026-04"}]
    row = session.query(AlertDispatch).filter_by(alert_key="tax_bo:2026-04").one()
    assert row.status == "sent"


def test_sweep_does_not_cross_route_a_balance_milestone_row(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQ-FIX-ALR-002: the EA dispatcher scopes its sweep to
    `ALERT_TYPES = ("tax_bo", "invoice_sweep", "invoice_draft")` — a failed
    `balance_milestone` row (the balance-alerts dispatcher's own alert_type)
    must NOT be swept/re-POSTed here, or it would be replayed through the
    wrong n8n webhook target."""
    session.add(
        AlertDispatch(
            alert_key="balance:acc:checking:1000",
            occurrence_date="2026-05-09",
            alert_type="balance_milestone",
            entity="sparkry",
            subject="Sparkry checking below $1,000.00",
            status="failed",
            delivery_channel="n8n_webhook",
            payload_json='{"alert_key": "balance:acc:checking:1000"}',
        )
    )
    session.commit()
    monkeypatch.setattr(dispatcher_mod, "compute_tax_alerts", lambda today: [])
    monkeypatch.setattr(dispatcher_mod, "compute_invoice_alerts", lambda today, s: [])
    posted_payloads: list[dict[str, object]] = []

    def _fake_post_raw_payload(payload, *, apply, timeout=10.0):  # type: ignore[no-untyped-def]
        posted_payloads.append(payload)
        return WebhookResult("sent", 200, None)

    monkeypatch.setattr(dispatcher_mod, "post_raw_payload", _fake_post_raw_payload)
    dispatcher_mod.dispatch_alerts(session, date(2026, 5, 10), apply=True)

    assert posted_payloads == []
    row = session.query(AlertDispatch).filter_by(
        alert_key="balance:acc:checking:1000"
    ).one()
    assert row.status == "failed"  # untouched — not this dispatcher's alert_type


def test_apply_records_payload_json_and_delivery_channel(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQ-FIX-ALR-002: every written/updated row persists the exact payload +
    channel (mirrors the balance-alerts dispatcher test of the same name)."""
    import json

    monkeypatch.setattr(
        dispatcher_mod, "compute_tax_alerts", lambda today: [_alert("k1")]
    )
    monkeypatch.setattr(dispatcher_mod, "compute_invoice_alerts", lambda today, s: [])
    monkeypatch.setattr(
        dispatcher_mod, "post_alert", lambda a, apply: WebhookResult("sent", 200, None)
    )
    dispatch_alerts(session, date(2026, 5, 10), apply=True)

    row = session.query(AlertDispatch).filter_by(alert_key="k1").one()
    assert row.delivery_channel == "n8n_webhook"
    assert row.payload_json is not None
    payload = json.loads(row.payload_json)
    assert payload["alert_key"] == "k1"

    # Re-run with a failed result on the same key/day exercises the update
    # (not insert) branch of `_record` and must still persist both fields.
    monkeypatch.setattr(
        dispatcher_mod,
        "post_alert",
        lambda a, apply: WebhookResult("failed", 500, "non-2xx"),
    )
    session.query(AlertDispatch).filter_by(alert_key="k1").update({"status": "failed"})
    session.commit()
    dispatch_alerts(session, date(2026, 5, 10), apply=True)
    row = session.query(AlertDispatch).filter_by(alert_key="k1").one()
    assert row.delivery_channel == "n8n_webhook"
    assert row.payload_json is not None
    payload = json.loads(row.payload_json)
    assert payload["alert_key"] == "k1"
