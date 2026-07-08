"""Approve-and-send for AR reminders (REQ-ARC-001/002).

Nothing sends to a customer without an explicit approval. The
``pending_approval → approved`` transition is a single guarded
``UPDATE ... WHERE status='pending_approval'`` with a rowcount check, so a
Telegram double-tap (or a re-used single-use token) cannot double-send: the
second attempt claims zero rows and returns a no-op.

On a successful send the reminder records ``resend_message_id``/``sent_at`` and
moves to ``sent``; a Resend failure moves it to ``failed`` (retryable back to
``pending_approval``). Every transition writes an entity-mode AuditEvent, and
the send is logged to ``alert_dispatch`` on the ``resend_email`` channel
(``payload_json=NULL``) so it is excluded from the webhook replay sweep.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import resend
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.alerts.models import AlertDispatch
from src.ar.chaser import _load_context, record_status_audit
from src.invoicing.email_sender import FROM_ADDRESS, _validate_email
from src.models.ar_reminder import (
    AR_STATUS_APPROVED,
    AR_STATUS_FAILED,
    AR_STATUS_PENDING_APPROVAL,
    AR_STATUS_SENT,
    ArReminder,
)
from src.models.invoice import Invoice

logger = logging.getLogger(__name__)

ALERT_TYPE_SEND = "ar_reminder_send"
DELIVERY_CHANNEL = "resend_email"


@dataclass(frozen=True)
class ApproveResult:
    """Outcome of an approve-and-send attempt."""

    sent: bool
    status: str  # sent | failed | not_pending
    message_id: str | None = None
    error: str | None = None


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _send_alert_key(invoice_id: str, rung: int) -> str:
    # Distinct from the notify key (``ar:<inv>:<rung>``) so the send-channel row
    # never collides with the notify-channel row on UNIQUE(alert_key, day).
    return f"ar-send:{invoice_id}:{rung}"


def _record_send_dispatch(
    session: Session,
    *,
    reminder: ArReminder,
    invoice: Invoice,
    today: date,
    status: str,
    http_status: int | None,
    error: str | None,
) -> None:
    row = AlertDispatch(
        alert_key=_send_alert_key(invoice.id, reminder.rung),
        occurrence_date=today.isoformat(),
        alert_type=ALERT_TYPE_SEND,
        entity=invoice.entity,
        subject=reminder.draft_subject,
        status=status,
        http_status=http_status,
        error_detail=error,
        delivery_channel=DELIVERY_CHANNEL,
        payload_json=None,
    )
    try:
        with session.begin_nested():
            session.add(row)
        session.commit()
    except IntegrityError:
        session.rollback()


def _build_html(body: str) -> str:
    """Minimal inline-CSS wrapper around the plain-text draft body."""
    from src.invoicing.email_sender import _FONT_STACK

    safe = body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (
        f'<div style="font-family: {_FONT_STACK}; font-size: 14px; '
        f'color: #1d1d1f; white-space: pre-wrap; line-height: 1.5;">{safe}</div>'
    )


def _send_via_resend(
    *, to_email: str, subject: str, body: str
) -> str:
    """Send the reminder email via Resend; return the message id."""
    resend.api_key = os.environ.get("RESEND_API_KEY", "")
    if not resend.api_key:
        raise ValueError("RESEND_API_KEY is not configured")
    _validate_email(to_email)
    params: resend.Emails.SendParams = {
        "from": FROM_ADDRESS,
        "to": [to_email],
        "subject": subject,
        "html": _build_html(body),
        "text": body,
    }
    result = resend.Emails.send(params)
    return result["id"]


def _recipient_email(customer: Any) -> str | None:
    return getattr(customer, "contact_email", None)


def approve_and_send(
    session: Session,
    reminder: ArReminder,
    *,
    approved_via: str,
    today: date | None = None,
) -> ApproveResult:
    """Claim the reminder, send it via Resend, and record the transition.

    ``approved_via`` is ``telegram`` (API callback) or ``cli`` (local operator).
    Returns ``not_pending`` without sending if the guarded claim finds the row
    already out of ``pending_approval`` (double-tap / re-used token).
    """
    today = today or date.today()

    # Guarded exactly-once claim: pending_approval → approved.
    rowcount = (
        session.query(ArReminder)
        .filter_by(id=reminder.id, status=AR_STATUS_PENDING_APPROVAL)
        .update(
            {
                "status": AR_STATUS_APPROVED,
                "approved_via": approved_via,
                "updated_at": _now(),
            }
        )
    )
    session.commit()
    if rowcount != 1:
        return ApproveResult(sent=False, status="not_pending")

    session.refresh(reminder)
    record_status_audit(
        session,
        reminder,
        AR_STATUS_PENDING_APPROVAL,
        AR_STATUS_APPROVED,
        approved_via,
    )
    session.commit()

    invoice = session.get(Invoice, reminder.invoice_id)
    if invoice is None:  # pragma: no cover - FK integrity guarantees this
        return ApproveResult(sent=False, status="failed", error="invoice missing")
    customer, _line_items = _load_context(session, invoice)
    to_email = _recipient_email(customer)

    try:
        if not to_email:
            raise ValueError("customer has no contact_email")
        message_id = _send_via_resend(
            to_email=to_email,
            subject=reminder.draft_subject,
            body=reminder.draft_body,
        )
    except Exception as exc:  # noqa: BLE001 — surface as a failed transition
        logger.warning("AR reminder %s send failed: %s", reminder.id, type(exc).__name__)
        reminder.status = AR_STATUS_FAILED
        record_status_audit(
            session, reminder, AR_STATUS_APPROVED, AR_STATUS_FAILED, approved_via
        )
        session.commit()
        _record_send_dispatch(
            session,
            reminder=reminder,
            invoice=invoice,
            today=today,
            status="failed",
            http_status=None,
            error="send failed",
        )
        return ApproveResult(sent=False, status="failed", error="send failed")

    reminder.resend_message_id = message_id
    reminder.sent_at = _now()
    reminder.status = AR_STATUS_SENT
    record_status_audit(
        session, reminder, AR_STATUS_APPROVED, AR_STATUS_SENT, approved_via
    )
    session.commit()
    _record_send_dispatch(
        session,
        reminder=reminder,
        invoice=invoice,
        today=today,
        status="sent",
        http_status=None,
        error=None,
    )
    return ApproveResult(sent=True, status="sent", message_id=message_id)
