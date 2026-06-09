"""Alert dispatcher: compute → dedupe → POST → record.

Per-alert error isolation: one failed/raising alert never blocks the rest, and
an alert is only skipped on later runs if it was previously recorded as "sent".
DRY-RUN (apply=False) writes nothing to the ledger.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.alerts.models import AlertDispatch
from src.alerts.rules import Alert, compute_invoice_alerts, compute_tax_alerts
from src.alerts.webhook import WebhookResult, post_alert

logger = logging.getLogger(__name__)


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


def _record(session: Session, alert: Alert, result: WebhookResult) -> None:
    existing = _already_sent(session, alert)
    if existing is not None:
        existing.status = result.status
        existing.http_status = result.http_status
        existing.error_detail = result.error
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
    )
    try:
        with session.begin_nested():
            session.add(row)
        session.commit()
    except IntegrityError:
        pass  # savepoint already rolled back the failed insert; concurrent run won — safe to skip


def dispatch_alerts(session: Session, today: date, *, apply: bool) -> DispatchSummary:
    alerts = compute_tax_alerts(today) + compute_invoice_alerts(today, session)
    summary = DispatchSummary()

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
                _record(session, alert, result)
            except Exception:  # noqa: BLE001
                session.rollback()
                logger.exception("failed to record alert %s", alert.alert_key)

    return summary
