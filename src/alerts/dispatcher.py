"""Alert dispatcher: compute → dedupe → POST → record.

Per-alert error isolation: one failed/raising alert never blocks the rest, and
an alert is only skipped on later runs if it was previously recorded as "sent".
DRY-RUN (apply=False) writes nothing to the ledger.

REQ-FIX-ALR-002: at the top of every --apply run, failed n8n_webhook rows from
the last 7 days are re-POSTed via the shared sweep before computing today's
alerts (own alert_types only — see src/alerts/sweep.py docstring).

REQ-FIX-ALR-004: date-keyed rules (day-3/10/17/25, last-day-of-month) only
fire when `today` matches. A run-marker ledger row lets a `Persistent=true`
catch-up after downtime evaluate every missed day instead of losing the
reminder forever.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.alerts.models import AlertDispatch
from src.alerts.rules import Alert, compute_invoice_alerts, compute_tax_alerts
from src.alerts.sweep import sweep_failed_rows
from src.alerts.webhook import (
    WebhookResult,
    build_payload,
    post_alert,
    post_raw_payload,
    resolve_from_email,
    resolve_to_email,
)

logger = logging.getLogger(__name__)

RUN_MARKER_KEY = "ea:run"
ALERT_TYPES = ("tax_bo", "invoice_sweep", "invoice_draft")
MAX_CATCHUP_DAYS = 14


@dataclass
class DispatchSummary:
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    dry_run: int = 0


def _already_sent(session: Session, alert: Alert) -> AlertDispatch | None:
    return (
        session.query(AlertDispatch)
        .filter_by(alert_key=alert.alert_key, occurrence_date=alert.occurrence_date)
        .one_or_none()
    )


def _record(
    session: Session, alert: Alert, result: WebhookResult, *, payload: Mapping[str, object]
) -> None:
    payload_json = json.dumps(payload)
    existing = _already_sent(session, alert)
    if existing is not None:
        existing.status = result.status
        existing.http_status = result.http_status
        existing.error_detail = result.error
        existing.delivery_channel = "n8n_webhook"
        existing.payload_json = payload_json
        session.commit()
        return
    row = AlertDispatch(
        alert_key=alert.alert_key,
        occurrence_date=alert.occurrence_date,
        alert_type=alert.alert_type,
        entity=alert.entity,
        subject=alert.subject,
        status=result.status,
        http_status=result.http_status,
        error_detail=result.error,
        delivery_channel="n8n_webhook",
        payload_json=payload_json,
    )
    try:
        with session.begin_nested():
            session.add(row)
        session.commit()
    except IntegrityError:
        pass  # savepoint already rolled back the failed insert; concurrent run won — safe to skip


def _catch_up_days(session: Session, today: date) -> list[date]:
    """REQ-FIX-ALR-004: every day since the last successful run, capped at 14.

    No run-marker row → evaluate today only. Otherwise evaluate the most
    recent `min(gap, 14)` days ending at `today` (at least 1 — today itself —
    even on a same-day re-run).
    """
    last_run = session.scalars(
        select(func.max(AlertDispatch.occurrence_date)).where(
            AlertDispatch.alert_key == RUN_MARKER_KEY
        )
    ).first()
    if not last_run:
        return [today]
    last_run_date = date.fromisoformat(last_run)
    gap_days = max(1, min((today - last_run_date).days, MAX_CATCHUP_DAYS))
    return [today - timedelta(days=i) for i in range(gap_days - 1, -1, -1)]


def _write_run_marker(session: Session, today: date) -> None:
    row = AlertDispatch(
        alert_key=RUN_MARKER_KEY,
        occurrence_date=today.isoformat(),
        alert_type="run_marker",
        entity="all",
        subject="EA alert dispatch run marker",
        status="sent",
    )
    try:
        with session.begin_nested():
            session.add(row)
        session.commit()
    except IntegrityError:
        session.rollback()


def _sweep_post(payload: dict[str, object]) -> WebhookResult:
    return post_raw_payload(payload, apply=True)  # type: ignore[arg-type]


def dispatch_alerts(session: Session, today: date, *, apply: bool) -> DispatchSummary:
    summary = DispatchSummary()

    if apply:
        sweep_failed_rows(
            session, today, post=_sweep_post, apply=True, alert_types=ALERT_TYPES
        )

    for day in _catch_up_days(session, today):
        try:
            alerts = compute_tax_alerts(day) + compute_invoice_alerts(day, session)
        except Exception:  # noqa: BLE001 — one bad day never halts the catch-up
            # loop or blocks the run-marker write for later (working) days.
            logger.exception("alert computation for %s raised; skipping that day", day)
            session.rollback()
            continue

        for alert in alerts:
            existing = _already_sent(session, alert)
            if existing is not None and existing.status == "sent":
                summary.skipped += 1
                continue

            try:
                result = post_alert(alert, apply=apply)
            except Exception:  # noqa: BLE001 — isolate one bad alert
                logger.exception("alert %s raised during dispatch", alert.alert_key)
                result = WebhookResult("failed", None, "dispatch error")

            if result.status == "sent":
                summary.sent += 1
            elif result.status == "dry_run":
                summary.dry_run += 1
            else:
                summary.failed += 1

            if apply:
                try:
                    payload = build_payload(alert, resolve_from_email(), resolve_to_email())
                    _record(session, alert, result, payload=payload)
                except Exception:  # noqa: BLE001
                    session.rollback()
                    logger.exception("failed to record alert %s", alert.alert_key)

    if apply:
        _write_run_marker(session, today)

    return summary
