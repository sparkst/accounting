"""Tests for src/alerts/sweep.py — REQ-FIX-ALR-002.

The sweep is a generic, alert_type-agnostic contract shared by every
n8n_webhook emitter across the program (EA alerts, balance alerts, and
future emitters: policy_drift_dispatch.py, the AR-chaser draft-notification
POST). It is exercised standalone here with a fake `post` callable; the two
concrete dispatchers wired in this workstream (src/alerts/dispatcher.py,
src/balance_alerts/dispatcher.py) additionally scope their own call by
`alert_types` as a practical safety net (see those modules' docstrings).
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.alerts.models import AlertDispatch
from src.alerts.sweep import sweep_failed_rows
from src.alerts.webhook import WebhookResult
from src.models.base import Base

_ENGINE = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
Base.metadata.create_all(_ENGINE)
_Session = sessionmaker(bind=_ENGINE)

TODAY = date(2026, 7, 7)
_UNSET = "__unset_sentinel__"


@pytest.fixture()
def session() -> Generator[Session, None, None]:
    s = _Session()
    yield s
    s.query(AlertDispatch).delete()
    s.commit()
    s.close()


def _row(
    session: Session,
    *,
    alert_key: str,
    occurrence_date: str,
    alert_type: str = "tax_bo",
    status: str = "failed",
    delivery_channel: str | None = "n8n_webhook",
    payload_json: str | None = _UNSET,
) -> AlertDispatch:
    if payload_json is _UNSET:
        import json

        payload_json = json.dumps({"alert_key": alert_key})
    row = AlertDispatch(
        alert_key=alert_key,
        occurrence_date=occurrence_date,
        alert_type=alert_type,
        entity="sparkry",
        subject="subj",
        status=status,
        delivery_channel=delivery_channel,
        payload_json=payload_json,
    )
    session.add(row)
    session.commit()
    return row


def test_failed_row_with_payload_is_resent_and_flips_to_sent(session: Session) -> None:
    _row(session, alert_key="k1", occurrence_date=TODAY.isoformat())
    posted: list[dict[str, object]] = []

    def post(payload: dict[str, object]) -> WebhookResult:
        posted.append(payload)
        return WebhookResult("sent", 200, None)

    summary = sweep_failed_rows(session, TODAY, post=post, apply=True)
    assert summary.resent == 1
    assert len(posted) == 1
    row = session.query(AlertDispatch).filter_by(alert_key="k1").one()
    assert row.status == "sent"
    assert row.http_status == 200


def test_null_payload_row_is_skipped(session: Session) -> None:
    _row(session, alert_key="k2", occurrence_date=TODAY.isoformat(), payload_json=None)
    calls = {"n": 0}

    def post(payload: dict[str, object]) -> WebhookResult:
        calls["n"] += 1
        return WebhookResult("sent", 200, None)

    summary = sweep_failed_rows(session, TODAY, post=post, apply=True)
    assert calls["n"] == 0
    assert summary.resent == 0
    row = session.query(AlertDispatch).filter_by(alert_key="k2").one()
    assert row.status == "failed"  # untouched


def test_older_than_7d_row_is_skipped(session: Session) -> None:
    stale_date = (TODAY - timedelta(days=8)).isoformat()
    _row(session, alert_key="k3", occurrence_date=stale_date)
    calls = {"n": 0}

    def post(payload: dict[str, object]) -> WebhookResult:
        calls["n"] += 1
        return WebhookResult("sent", 200, None)

    sweep_failed_rows(session, TODAY, post=post, apply=True)
    assert calls["n"] == 0


def test_exactly_7d_row_is_swept(session: Session) -> None:
    boundary_date = (TODAY - timedelta(days=7)).isoformat()
    _row(session, alert_key="k3b", occurrence_date=boundary_date)
    calls = {"n": 0}

    def post(payload: dict[str, object]) -> WebhookResult:
        calls["n"] += 1
        return WebhookResult("sent", 200, None)

    sweep_failed_rows(session, TODAY, post=post, apply=True)
    assert calls["n"] == 1


def test_resend_email_channel_row_is_never_swept(session: Session) -> None:
    """A resend_email-channel failed row (report/digest/close/AR-chaser
    reminder-email) with a payload must NOT be swept — regression for the
    cross-emitter coupling gap the channel discriminator closes."""
    _row(
        session,
        alert_key="k4",
        occurrence_date=TODAY.isoformat(),
        delivery_channel="resend_email",
        payload_json='{"to": "x@y.com"}',
    )
    calls = {"n": 0}

    def post(payload: dict[str, object]) -> WebhookResult:
        calls["n"] += 1
        return WebhookResult("sent", 200, None)

    sweep_failed_rows(session, TODAY, post=post, apply=True)
    assert calls["n"] == 0
    row = session.query(AlertDispatch).filter_by(alert_key="k4").one()
    assert row.status == "failed"


def test_policy_drift_webhook_failure_is_swept(session: Session) -> None:
    """Any future n8n_webhook emitter (e.g. policy_drift, wealth §11.4) is
    swept generically — the predicate does not gate on alert_type."""
    _row(session, alert_key="k5", occurrence_date=TODAY.isoformat(), alert_type="policy_drift")
    summary = sweep_failed_rows(
        session, TODAY, post=lambda p: WebhookResult("sent", 200, None), apply=True
    )
    assert summary.resent == 1


def test_ar_chaser_draft_notification_webhook_failure_is_swept(session: Session) -> None:
    """The AR-chaser's Telegram draft-notification POST (agent-features §3.3)
    is n8n_webhook + persisted payload → swept by the same generic mechanism."""
    _row(
        session,
        alert_key="k6",
        occurrence_date=TODAY.isoformat(),
        alert_type="ar_chaser_draft_notification",
    )
    summary = sweep_failed_rows(
        session, TODAY, post=lambda p: WebhookResult("sent", 200, None), apply=True
    )
    assert summary.resent == 1


def test_legacy_null_channel_row_with_payload_is_swept(session: Session) -> None:
    """Pre-migration rows have delivery_channel=NULL (every pre-migration
    emitter was webhook-only) — the explicit IS NULL arm still sweeps them
    once a payload exists."""
    _row(session, alert_key="k7", occurrence_date=TODAY.isoformat(), delivery_channel=None)
    summary = sweep_failed_rows(
        session, TODAY, post=lambda p: WebhookResult("sent", 200, None), apply=True
    )
    assert summary.resent == 1


def test_legacy_null_channel_row_without_payload_is_not_swept(session: Session) -> None:
    _row(
        session,
        alert_key="k8",
        occurrence_date=TODAY.isoformat(),
        delivery_channel=None,
        payload_json=None,
    )
    calls = {"n": 0}

    def post(payload: dict[str, object]) -> WebhookResult:
        calls["n"] += 1
        return WebhookResult("sent", 200, None)

    sweep_failed_rows(session, TODAY, post=post, apply=True)
    assert calls["n"] == 0


def test_sent_row_is_not_swept(session: Session) -> None:
    _row(session, alert_key="k9", occurrence_date=TODAY.isoformat(), status="sent")
    calls = {"n": 0}

    def post(payload: dict[str, object]) -> WebhookResult:
        calls["n"] += 1
        return WebhookResult("sent", 200, None)

    sweep_failed_rows(session, TODAY, post=post, apply=True)
    assert calls["n"] == 0


def test_alert_types_filter_scopes_sweep_when_provided(session: Session) -> None:
    _row(session, alert_key="k10", occurrence_date=TODAY.isoformat(), alert_type="tax_bo")
    _row(session, alert_key="k11", occurrence_date=TODAY.isoformat(), alert_type="balance_milestone")
    posted: list[str] = []

    def post(payload: dict[str, object]) -> WebhookResult:
        posted.append(str(payload["alert_key"]))
        return WebhookResult("sent", 200, None)

    sweep_failed_rows(session, TODAY, post=post, apply=True, alert_types=("tax_bo",))
    assert session.query(AlertDispatch).filter_by(alert_key="k10").one().status == "sent"
    assert session.query(AlertDispatch).filter_by(alert_key="k11").one().status == "failed"


def test_dry_run_writes_nothing_and_does_not_post(session: Session) -> None:
    _row(session, alert_key="k12", occurrence_date=TODAY.isoformat())
    calls = {"n": 0}

    def post(payload: dict[str, object]) -> WebhookResult:
        calls["n"] += 1
        return WebhookResult("sent", 200, None)

    summary = sweep_failed_rows(session, TODAY, post=post, apply=False)
    assert calls["n"] == 0
    assert summary.resent == 0
    row = session.query(AlertDispatch).filter_by(alert_key="k12").one()
    assert row.status == "failed"  # untouched


def test_stale_same_day_only_row_is_superseded_not_replayed(session: Session) -> None:
    """REQ-FIX-ALR-007: a failed row of a same-day-only alert type from a
    PRIOR day must never be re-POSTed verbatim (2026-08-02 incident: a
    12-hour-old balance_pulse digest was re-delivered twice). It is marked
    status='superseded' — terminal, never picked up by any later sweep."""
    stale_date = (TODAY - timedelta(days=1)).isoformat()
    _row(
        session,
        alert_key="balance:pulse:stale",
        occurrence_date=stale_date,
        alert_type="balance_pulse",
    )
    calls = {"n": 0}

    def post(payload: dict[str, object]) -> WebhookResult:
        calls["n"] += 1
        return WebhookResult("sent", 200, None)

    summary = sweep_failed_rows(
        session, TODAY, post=post, apply=True, same_day_only_types=("balance_pulse",)
    )
    assert calls["n"] == 0
    assert summary.resent == 0
    assert summary.superseded == 1
    row = session.query(AlertDispatch).filter_by(alert_key="balance:pulse:stale").one()
    assert row.status == "superseded"
    assert row.error_detail is not None and "stale" in row.error_detail


def test_same_day_only_row_from_today_is_still_resent(session: Session) -> None:
    """Same-day-only restricts the WINDOW, not the retry itself: a row that
    failed earlier today is still a legitimate transient-failure retry."""
    _row(
        session,
        alert_key="balance:pulse:today",
        occurrence_date=TODAY.isoformat(),
        alert_type="balance_pulse",
    )
    summary = sweep_failed_rows(
        session,
        TODAY,
        post=lambda p: WebhookResult("sent", 200, None),
        apply=True,
        same_day_only_types=("balance_pulse",),
    )
    assert summary.resent == 1
    assert summary.superseded == 0
    row = session.query(AlertDispatch).filter_by(alert_key="balance:pulse:today").one()
    assert row.status == "sent"


def test_stale_row_of_unlisted_type_keeps_seven_day_window(session: Session) -> None:
    """Durable date-keyed types (tax_bo, policy_drift, ...) keep the full
    7-day replay window — the same-day rule applies only to listed types."""
    stale_date = (TODAY - timedelta(days=3)).isoformat()
    _row(
        session,
        alert_key="tax:sparkry:bo:stale",
        occurrence_date=stale_date,
        alert_type="tax_bo",
    )
    summary = sweep_failed_rows(
        session,
        TODAY,
        post=lambda p: WebhookResult("sent", 200, None),
        apply=True,
        same_day_only_types=("balance_pulse",),
    )
    assert summary.resent == 1
    assert summary.superseded == 0


def test_dry_run_counts_stale_same_day_only_rows_without_writing(session: Session) -> None:
    stale_date = (TODAY - timedelta(days=1)).isoformat()
    _row(
        session,
        alert_key="balance:pulse:dry",
        occurrence_date=stale_date,
        alert_type="balance_pulse",
    )
    calls = {"n": 0}

    def post(payload: dict[str, object]) -> WebhookResult:
        calls["n"] += 1
        return WebhookResult("sent", 200, None)

    summary = sweep_failed_rows(
        session, TODAY, post=post, apply=False, same_day_only_types=("balance_pulse",)
    )
    assert calls["n"] == 0
    assert summary.superseded == 1
    assert summary.candidates == 0
    row = session.query(AlertDispatch).filter_by(alert_key="balance:pulse:dry").one()
    assert row.status == "failed"  # DRY-RUN writes nothing


def test_superseded_row_is_never_swept_again(session: Session) -> None:
    """'superseded' is terminal — a later sweep with no same_day_only_types
    (or a different caller) must not resurrect it."""
    _row(
        session,
        alert_key="balance:pulse:done",
        occurrence_date=TODAY.isoformat(),
        alert_type="balance_pulse",
        status="superseded",
    )
    calls = {"n": 0}

    def post(payload: dict[str, object]) -> WebhookResult:
        calls["n"] += 1
        return WebhookResult("sent", 200, None)

    summary = sweep_failed_rows(session, TODAY, post=post, apply=True)
    assert calls["n"] == 0
    assert summary.resent == 0


def test_per_row_isolation_one_raising_post_does_not_halt_sweep(session: Session) -> None:
    _row(session, alert_key="bad", occurrence_date=TODAY.isoformat())
    _row(session, alert_key="good", occurrence_date=TODAY.isoformat())

    def post(payload: dict[str, object]) -> WebhookResult:
        if payload.get("alert_key") == "bad":
            raise RuntimeError("boom")
        return WebhookResult("sent", 200, None)

    summary = sweep_failed_rows(session, TODAY, post=post, apply=True)
    assert summary.resent == 1
    assert summary.still_failed == 1
    good = session.query(AlertDispatch).filter_by(alert_key="good").one()
    assert good.status == "sent"
    bad = session.query(AlertDispatch).filter_by(alert_key="bad").one()
    assert bad.status == "failed"  # untouched by the raising attempt
