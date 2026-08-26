"""Shared Resend sender + alert_dispatch ledger recording for the reporting
suite (WBR/TXF/SEL).

REQ-ID: REQ-WBR-001..003, REQ-TXF-001..004, REQ-SEL-001..002 — delivery-split
decision (design spec §1/§2): these three reports go out via Resend email,
never the n8n severity webhook, so they are permanently excluded from the
REQ-FIX-ALR-002 webhook replay sweep by ``delivery_channel != 'n8n_webhook'``
— re-running the CLI/timer regenerates fresher data instead of replaying a
stale rendered report (design spec §7).

Every apply-mode send is recorded in the existing ``alert_dispatch`` ledger
with ``delivery_channel="resend_email"`` and ``payload_json=NULL`` (the body
is never stored — deliberate; a failed send is retried by re-running the
report, not replayed byte-for-byte). The ledger's
``UniqueConstraint(alert_key, occurrence_date)`` is the idempotency guard: a
``Persistent=true`` catch-up run cannot double-send **as long as callers pin
``occurrence_date`` to the report's own period anchor**, not the run date
(design spec §2 — the WS5 divergence from ``src/alerts/rules.py``'s
``occurrence_date=today.isoformat()`` daily-alert convention).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from html import escape as _html_escape

import resend
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.alerts.models import AlertDispatch
from src.utils.constants import SPARKRY_CONTACT_EMAIL

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_TO_EMAIL",
    "FROM_ADDRESS",
    "SendResult",
    "already_sent",
    "build_html_body",
    "dispatch_report",
    "record_dispatch",
    "resolve_to_email",
    "send_report_email",
]

# REQ-FIX-API-004 pattern: a single FROM_ADDRESS constant, not repeated literals.
# Default recipient uses the CONTROLLED sparkry.ai domain (the .com is not ours
# — the grep-gate test bans it); production overrides via REPORT_TO_EMAIL.
FROM_ADDRESS = "Travis Sparks <travis@sparkry.ai>"
DEFAULT_TO_EMAIL = SPARKRY_CONTACT_EMAIL

_EMAIL_RE = re.compile(
    r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?"
    r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?)*"
    r"\.[a-zA-Z]{2,}$"
)


def _validate_email(email: str) -> None:
    if "\r" in email or "\n" in email:
        raise ValueError(f"Invalid email address (contains forbidden characters): {email!r}")
    if not _EMAIL_RE.match(email):
        raise ValueError(f"Invalid email address: {email!r}")


def resolve_to_email() -> str:
    """REPORT_TO_EMAIL env override, format-validated and falling back to
    the allowlisted default (mirrors REQ-FIX-ALR-003)."""
    candidate = os.environ.get("REPORT_TO_EMAIL", "").strip() or DEFAULT_TO_EMAIL
    _validate_email(candidate)
    return candidate


@dataclass(frozen=True)
class SendResult:
    status: str  # "sent" | "failed" | "dry_run" | "skipped"
    error: str | None = None
    # Resend's returned message id — a deploy receipt for the digest email leg
    # (REQ-DIG-EML-004); None for dry-run/failed/skipped and for legacy callers.
    message_id: str | None = None


def build_html_body(plain_text: str) -> str:
    """Monospace <pre> wrapper — the plain text IS the report (design spec
    §1); no design chrome, HTML is only for client rendering fidelity."""
    return (
        '<pre style="font-family: ui-monospace, SFMono-Regular, Menlo, '
        'Consolas, monospace; font-size: 13px; line-height: 1.4; '
        'white-space: pre-wrap;">' + _html_escape(plain_text) + "</pre>"
    )


def send_report_email(
    subject: str,
    plain_text: str,
    *,
    apply: bool,
    to_emails: list[str] | None = None,
) -> SendResult:
    """Send *plain_text* via Resend when apply=True; dry-run never touches
    the network.

    ``to_emails`` (REQ-DIG-EML-001): an explicit recipient list — a single
    Resend send carrying the full to-list, each address format-validated. When
    None (the default, and every existing WBR/TXF/SEL caller), the historical
    single-recipient ``resolve_to_email()`` behavior is byte-identical.
    """
    if not apply:
        return SendResult("dry_run")

    resend.api_key = os.environ.get("RESEND_API_KEY", "")
    if not resend.api_key:
        return SendResult("failed", "RESEND_API_KEY is not configured")

    try:
        if to_emails is None:
            recipients = [resolve_to_email()]
        else:
            recipients = [e for e in to_emails]
            for candidate in recipients:
                _validate_email(candidate)
    except ValueError as exc:
        return SendResult("failed", str(exc))
    if not recipients:
        return SendResult("failed", "no recipients")

    params: resend.Emails.SendParams = {
        "from": FROM_ADDRESS,
        "to": recipients,
        "subject": subject,
        "html": build_html_body(plain_text),
        "text": plain_text,
    }
    try:
        resp = resend.Emails.send(params)
        message_id = resp.get("id") if isinstance(resp, dict) else None
        # WARNING, not info: the accounting-balance-alerts unit's journal
        # filter is WARNING+ (REQ-FIX-ISSUE-67) — info never reaches it.
        logger.warning("digest email sent id=%s", message_id)
        return SendResult("sent", message_id=message_id)
    except Exception as exc:  # noqa: BLE001 — a send failure must never crash the run
        logger.exception("report email send failed (subject=%r)", subject)
        return SendResult("failed", str(exc))


def already_sent(session: Session, alert_key: str, occurrence_date: str) -> bool:
    row = (
        session.query(AlertDispatch)
        .filter_by(alert_key=alert_key, occurrence_date=occurrence_date)
        .one_or_none()
    )
    return row is not None and row.status == "sent"


def record_dispatch(
    session: Session,
    *,
    alert_key: str,
    occurrence_date: str,
    alert_type: str,
    entity: str,
    subject: str,
    result: SendResult,
) -> None:
    """Idempotent upsert into ``alert_dispatch``. ``delivery_channel`` is
    always ``resend_email``; ``payload_json`` is always NULL (design spec
    §7 channel discriminator — never the n8n_webhook replay path)."""
    # REQ-FIX-ISSUE-67: the Resend message_id is the only deploy receipt for
    # a digest send; it previously reached only a logger.info line, invisible
    # on units whose journal filter is WARNING+. error_detail is otherwise
    # unused on a "sent" result, so it doubles as the persisted receipt.
    error_detail = (
        f"message_id={result.message_id}"
        if result.status == "sent" and result.message_id
        else result.error
    )
    existing = (
        session.query(AlertDispatch)
        .filter_by(alert_key=alert_key, occurrence_date=occurrence_date)
        .one_or_none()
    )
    if existing is not None:
        existing.status = result.status
        existing.error_detail = error_detail
        existing.delivery_channel = "resend_email"
        existing.payload_json = None
        session.commit()
        return
    row = AlertDispatch(
        alert_key=alert_key,
        occurrence_date=occurrence_date,
        alert_type=alert_type,
        entity=entity,
        subject=subject,
        status=result.status,
        http_status=None,
        error_detail=error_detail,
        delivery_channel="resend_email",
        payload_json=None,
    )
    try:
        with session.begin_nested():
            session.add(row)
        session.commit()
    except IntegrityError:
        # Concurrent insert raced us to the unique key — already recorded.
        session.rollback()


def dispatch_report(
    session: Session,
    *,
    alert_key: str,
    occurrence_date: str,
    alert_type: str,
    entity: str,
    subject: str,
    body: str,
    apply: bool,
    to_emails: list[str] | None = None,
) -> SendResult:
    """Full send+ledger pattern for one report run.

    ``to_emails`` (REQ-DIG-EML-001) rides through to ``send_report_email`` for
    the multi-recipient digest email leg; None keeps the single-recipient
    ``resolve_to_email()`` behavior for existing report callers.

    Dry-run: never checks the ledger, never writes it, just returns
    ``dry_run`` (caller prints the rendered report to stdout).
    Apply: idempotency-checked against the ledger first (a
    ``Persistent=true`` catch-up on an already-sent period is a no-op,
    returned as ``skipped``); on send, records the outcome.
    """
    if not apply:
        return SendResult("dry_run")
    if already_sent(session, alert_key, occurrence_date):
        return SendResult("skipped")
    result = send_report_email(subject, body, apply=True, to_emails=to_emails)
    record_dispatch(
        session,
        alert_key=alert_key,
        occurrence_date=occurrence_date,
        alert_type=alert_type,
        entity=entity,
        subject=subject,
        result=result,
    )
    return result
