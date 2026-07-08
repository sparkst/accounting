"""Draft-notification emitter for the AR chaser (REQ-ARC-002).

On draft creation the chaser POSTs a Telegram-bound notification through the
n8n severity-webhook client (``src/balance_alerts/webhook.py``). Because this is
an ``n8n_webhook``-channel emitter it records an ``alert_dispatch`` row with the
full ``payload_json`` and ``delivery_channel="n8n_webhook"`` — making a transient
failure eligible for the REQ-FIX-ALR-002 replay sweep. The draft itself always
lives in ``ar_reminder`` regardless of whether this POST succeeds.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.alerts.models import AlertDispatch
from src.alerts.webhook import WebhookResult
from src.balance_alerts.webhook import post_payload
from src.models.ar_reminder import ArReminder
from src.models.invoice import Invoice

logger = logging.getLogger(__name__)

ALERT_TYPE_NOTIFY = "ar_reminder_notify"
DELIVERY_CHANNEL = "n8n_webhook"

# Public base for the approve/dismiss callback URLs handed to n8n. Overridable
# for the box; defaults to the production edge.
_BOOKS_BASE_ENV = "BOOKS_PUBLIC_URL"
_DEFAULT_BASE = "https://books.sparkry.ai"


def _books_base() -> str:
    return os.environ.get(_BOOKS_BASE_ENV, _DEFAULT_BASE).rstrip("/")


def _alert_key(invoice_id: str, rung: int) -> str:
    return f"ar:{invoice_id}:{rung}"


def build_notification_payload(
    reminder: ArReminder, invoice: Invoice
) -> dict[str, Any]:
    """The n8n draft-notification contract with an inline-keyboard callback."""
    base = _books_base()
    return {
        "type": "info",
        "title": "AR reminder draft",
        "message": reminder.draft_subject,
        "alert_key": _alert_key(invoice.id, reminder.rung),
        "callback": {
            "approve_url": f"{base}/api/ar/reminders/{reminder.id}/approve",
            "dismiss_url": f"{base}/api/ar/reminders/{reminder.id}/dismiss",
            "token": reminder.approval_token,
        },
    }


def _record_dispatch(
    session: Session,
    *,
    reminder: ArReminder,
    invoice: Invoice,
    today: date,
    payload: dict[str, Any],
    result: WebhookResult,
) -> None:
    row = AlertDispatch(
        alert_key=_alert_key(invoice.id, reminder.rung),
        occurrence_date=today.isoformat(),
        alert_type=ALERT_TYPE_NOTIFY,
        entity=invoice.entity,
        subject=reminder.draft_subject,
        status=result.status,
        http_status=result.http_status,
        error_detail=result.error,
        delivery_channel=DELIVERY_CHANNEL,
        payload_json=json.dumps(payload),
    )
    try:
        with session.begin_nested():
            session.add(row)
        session.commit()
    except IntegrityError:
        # Concurrent/duplicate notify for the same (alert_key, day) — the ledger
        # already has the row; nothing else to do.
        session.rollback()


def notify_draft(
    session: Session,
    reminder: ArReminder,
    *,
    invoice: Invoice,
    today: date,
    apply: bool,
) -> WebhookResult:
    """POST the draft notification and record its ledger row (apply mode).

    DRY-RUN (``apply=False``) posts nothing and writes no ledger row.
    """
    payload = build_notification_payload(reminder, invoice)
    result = post_payload(
        payload,
        key=_alert_key(invoice.id, reminder.rung),
        apply=apply,
    )
    if apply:
        _record_dispatch(
            session,
            reminder=reminder,
            invoice=invoice,
            today=today,
            payload=payload,
            result=result,
        )
    return result
