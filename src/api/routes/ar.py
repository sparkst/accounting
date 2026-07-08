"""AR reminder approve/dismiss endpoints (REQ-ARC-002).

These endpoints are the n8n Telegram callback target. They self-authenticate —
NOT via Cloudflare-Access ``_auth`` — with a shared webhook secret in the
``X-Webhook-Secret`` header PLUS the reminder's single-use ``approval_token`` in
the body. The router is therefore registered WITHOUT ``dependencies=_auth``,
mirroring ``ingest_router``. Edge scoping is a deploy-time concern (the
``books-ar-approve`` Cloudflare Access service token limited to ``/api/ar/*``).

Exactly-once: the ``pending_approval → approved`` transition is a guarded
rowcount UPDATE inside ``approve_and_send``, so a double-tap cannot double-send;
a re-used single-use token finds the row already ``sent`` and returns 409.
"""

from __future__ import annotations

import hmac
import logging
import os

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.ar.chaser import dismiss_reminder
from src.ar.send import approve_and_send
from src.db.connection import SessionLocal
from src.models.ar_reminder import AR_STATUS_PENDING_APPROVAL, ArReminder

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ar"])

_SECRET_ENV = "N8N_ALERTS_WEBHOOK_SECRET"

# Module-level singleton avoids B008 (function-call-in-default).
_WEBHOOK_SECRET_HEADER: str | None = Header(default=None, alias="X-Webhook-Secret")


class ApprovalBody(BaseModel):
    """Callback body: the reminder's single-use approval token."""

    token: str


class ReminderActionResult(BaseModel):
    reminder_id: str
    status: str
    sent: bool = False
    message_id: str | None = None


def _verify_secret(header_secret: str | None) -> None:
    expected = os.environ.get(_SECRET_ENV)
    if not expected:
        # Fail closed: without a configured secret these endpoints cannot be
        # authenticated, so refuse rather than accept anonymous callbacks.
        raise HTTPException(status_code=503, detail="approval webhook not configured")
    if not header_secret or not hmac.compare_digest(header_secret, expected):
        raise HTTPException(status_code=401, detail="invalid webhook secret")


def _load_pending(session: Session, reminder_id: str, token: str) -> ArReminder:
    reminder = session.get(ArReminder, reminder_id)
    if reminder is None:
        raise HTTPException(status_code=404, detail="reminder not found")
    if not hmac.compare_digest(token, reminder.approval_token):
        raise HTTPException(status_code=403, detail="invalid approval token")
    if reminder.status != AR_STATUS_PENDING_APPROVAL:
        # Single-use: an already-sent/dismissed reminder cannot be re-actioned.
        raise HTTPException(status_code=409, detail="reminder is not pending approval")
    return reminder


@router.post("/ar/reminders/{reminder_id}/approve", response_model=ReminderActionResult)
def approve(
    reminder_id: str,
    body: ApprovalBody,
    x_webhook_secret: str | None = _WEBHOOK_SECRET_HEADER,
) -> ReminderActionResult:
    """Approve a pending reminder and send it via Resend (REQ-ARC-002)."""
    _verify_secret(x_webhook_secret)
    session = SessionLocal()
    try:
        reminder = _load_pending(session, reminder_id, body.token)
        result = approve_and_send(session, reminder, approved_via="telegram")
        if result.status == "not_pending":
            raise HTTPException(status_code=409, detail="reminder is not pending approval")
        if not result.sent:
            raise HTTPException(status_code=502, detail="reminder send failed")
        return ReminderActionResult(
            reminder_id=reminder_id,
            status=result.status,
            sent=result.sent,
            message_id=result.message_id,
        )
    finally:
        session.close()


@router.post("/ar/reminders/{reminder_id}/dismiss", response_model=ReminderActionResult)
def dismiss(
    reminder_id: str,
    body: ApprovalBody,
    x_webhook_secret: str | None = _WEBHOOK_SECRET_HEADER,
) -> ReminderActionResult:
    """Dismiss a pending reminder without sending (REQ-ARC-002)."""
    _verify_secret(x_webhook_secret)
    session = SessionLocal()
    try:
        reminder = _load_pending(session, reminder_id, body.token)
        dismiss_reminder(
            session, reminder, changed_by="telegram", approved_via="telegram"
        )
        return ReminderActionResult(reminder_id=reminder_id, status=reminder.status)
    finally:
        session.close()
