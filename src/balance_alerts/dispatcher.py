"""Balance-alert dispatcher: compute → dedupe → POST → record (REQ-BAL-006/010).

Reuses the `alert_dispatch` ledger. Its UNIQUE(alert_key, occurrence_date) gives
the per-(account, level, UTC-day) dedup for free. Per-alert error isolation: one
raising/failed POST never blocks the rest. DRY-RUN (apply=False) writes nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.alerts.models import AlertDispatch
from src.alerts.webhook import WebhookResult
from src.balance_alerts.rules import BalanceAlert, compute_balance_alerts
from src.balance_alerts.webhook import post_balance_alert

logger = logging.getLogger(__name__)


@dataclass
class DispatchSummary:
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    dry_run: int = 0


def _already_sent(session: Session, alert: BalanceAlert) -> AlertDispatch | None:
    return (
        session.query(AlertDispatch)
        .filter_by(alert_key=alert.alert_key, occurrence_date=alert.occurrence_date)
        .one_or_none()
    )


def _record(
    session: Session,
    alert: BalanceAlert,
    result: WebhookResult,
    existing: AlertDispatch | None = None,
) -> None:
    if existing is None:
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
        subject=alert.title,
        status=result.status,
        http_status=result.http_status,
        error_detail=result.error,
    )
    try:
        with session.begin_nested():
            session.add(row)
        session.commit()
    except IntegrityError:
        # Concurrent insert raced us to the unique key — treat as already recorded.
        session.rollback()


def dispatch_balance_alerts(
    today: date, session: Session, *, apply: bool
) -> DispatchSummary:
    """Compute today's balance alerts, skip ones already sent, POST the rest."""
    summary = DispatchSummary()
    alerts = compute_balance_alerts(today, session)
    for alert in alerts:
        try:
            prior = _already_sent(session, alert)
            if prior is not None and prior.status == "sent":
                summary.skipped += 1
                continue
            result = post_balance_alert(alert, apply=apply)
            if apply:
                _record(session, alert, result, existing=prior)
            if result.status == "sent":
                summary.sent += 1
            elif result.status == "dry_run":
                summary.dry_run += 1
            else:
                summary.failed += 1
        except Exception:  # noqa: BLE001 — per-alert isolation (REQ-BAL-010)
            logger.exception("balance alert %s failed; continuing", alert.alert_key)
            summary.failed += 1
            session.rollback()
    return summary
