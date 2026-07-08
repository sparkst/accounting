"""AR chaser ladder, drafts, transitions and aging buckets (REQ-ARC-001/003).

Daily entry point ``run`` (DRY-RUN default). For every unpaid SENT/OVERDUE
invoice it computes ``days = today - sent_at`` and drafts *only the highest
unsent rung* this run — an invoice discovered at day 46 gets one 45-day draft,
not three. Exactly-once per (invoice, rung) is guaranteed by the
``UNIQUE(invoice_id, rung)`` constraint; the insert is wrapped in
``begin_nested()`` and a racing/duplicate insert is skipped, never crashes.

A paid or void invoice dismisses all of its still-open drafts. Every state
transition writes an entity-mode ``AuditEvent`` (``entity_type="ar_reminder"``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.ar.templates import build_draft
from src.models.ar_reminder import (
    AR_RUNGS,
    AR_STATUS_APPROVED,
    AR_STATUS_DISMISSED,
    AR_STATUS_DRAFTED,
    AR_STATUS_FAILED,
    AR_STATUS_PENDING_APPROVAL,
    AR_STATUS_SENT,
    ArReminder,
)
from src.models.audit_event import AuditEvent
from src.models.enums import InvoiceStatus
from src.models.invoice import Customer, Invoice, InvoiceLineItem

logger = logging.getLogger(__name__)

AR_ENTITY_TYPE = "ar_reminder"

# Statuses that still represent an *open* draft (eligible for dismissal when the
# invoice is paid/void). Sent and dismissed are terminal.
_OPEN_STATUSES = (
    AR_STATUS_DRAFTED,
    AR_STATUS_PENDING_APPROVAL,
    AR_STATUS_APPROVED,
    AR_STATUS_FAILED,
)

_ACTIVE_INVOICE_STATUSES = (InvoiceStatus.SENT.value, InvoiceStatus.OVERDUE.value)
_CLOSED_INVOICE_STATUSES = (InvoiceStatus.PAID.value, InvoiceStatus.VOID.value)


@dataclass
class RunSummary:
    """Result of a single ``run`` pass."""

    drafted: int = 0
    dismissed: int = 0
    notified: int = 0
    notify_failed: int = 0
    drafted_ids: list[str] = field(default_factory=list)


def record_status_audit(
    session: Session,
    reminder: ArReminder,
    old: str | None,
    new: str,
    changed_by: str,
) -> None:
    """Append an entity-mode AuditEvent for a reminder ``status`` transition."""
    session.add(
        AuditEvent(
            entity_id=reminder.id,
            entity_type=AR_ENTITY_TYPE,
            field_changed="status",
            old_value=old,
            new_value=new,
            changed_by=changed_by,
        )
    )


def dismiss_reminder(
    session: Session,
    reminder: ArReminder,
    *,
    changed_by: str,
    approved_via: str | None = None,
    apply: bool = True,
) -> bool:
    """Transition a reminder to ``dismissed`` (+ audit). No-op if already closed.

    Returns True when a transition was applied. Shared by the paid-invoice
    sweep, the CLI ``dismiss`` command, and the API dismiss endpoint.
    """
    if reminder.status in (AR_STATUS_SENT, AR_STATUS_DISMISSED):
        return False
    if not apply:
        return True
    old = reminder.status
    reminder.status = AR_STATUS_DISMISSED
    if approved_via is not None:
        reminder.approved_via = approved_via
    record_status_audit(session, reminder, old, AR_STATUS_DISMISSED, changed_by)
    session.commit()
    return True


def _load_context(
    session: Session, invoice: Invoice
) -> tuple[Customer | None, list[InvoiceLineItem]]:
    customer = session.get(Customer, invoice.customer_id)
    line_items = (
        session.query(InvoiceLineItem)
        .filter(InvoiceLineItem.invoice_id == invoice.id)
        .order_by(InvoiceLineItem.sort_order)
        .all()
    )
    return customer, line_items


def _dismiss_closed_invoice_drafts(
    session: Session, *, apply: bool
) -> int:
    """Dismiss open drafts whose invoice is now paid or void."""
    dismissed = 0
    open_reminders = (
        session.query(ArReminder)
        .filter(ArReminder.status.in_(_OPEN_STATUSES))
        .all()
    )
    for reminder in open_reminders:
        invoice = session.get(Invoice, reminder.invoice_id)
        if invoice is None:
            continue
        closed = (
            invoice.status in _CLOSED_INVOICE_STATUSES
            or invoice.paid_date is not None
        )
        if not closed:
            continue
        if dismiss_reminder(session, reminder, changed_by="auto", apply=apply):
            dismissed += 1
    return dismissed


def _highest_unsent_rung(
    days: int, existing_rungs: set[int]
) -> int | None:
    """Highest ladder rung that is due (``days >= r``) and not yet drafted."""
    candidates = [r for r in AR_RUNGS if days >= r and r not in existing_rungs]
    return max(candidates) if candidates else None


def run(session: Session, *, today: date, apply: bool) -> RunSummary:
    """Draft the due reminder ladder for unpaid invoices (DRY-RUN default).

    REQ-ARC-001: one draft per (invoice, rung); only the highest unsent rung is
    drafted per run. DRY-RUN computes counts and writes nothing (no DB rows, no
    webhook). ``--apply`` inserts the ``drafted`` row, transitions it to
    ``pending_approval``, and posts the draft notification.
    """
    # Imported lazily to keep chaser importable without webhook/env wiring.
    from src.ar.notify import notify_draft

    summary = RunSummary()

    summary.dismissed = _dismiss_closed_invoice_drafts(session, apply=apply)

    invoices = (
        session.query(Invoice)
        .filter(
            Invoice.status.in_(_ACTIVE_INVOICE_STATUSES),
            Invoice.paid_date.is_(None),
        )
        .all()
    )

    for invoice in invoices:
        if invoice.sent_at is None:
            continue
        days = (today - invoice.sent_at.date()).days
        existing_rungs = {
            row.rung
            for row in session.query(ArReminder)
            .filter(ArReminder.invoice_id == invoice.id)
            .all()
        }
        rung = _highest_unsent_rung(days, existing_rungs)
        if rung is None:
            continue

        customer, line_items = _load_context(session, invoice)
        subject, body = build_draft(invoice, customer, line_items, rung)

        if not apply:
            summary.drafted += 1
            continue

        reminder = ArReminder(
            invoice_id=invoice.id,
            rung=rung,
            status=AR_STATUS_DRAFTED,
            draft_subject=subject,
            draft_body=body,
        )
        try:
            with session.begin_nested():
                session.add(reminder)
        except IntegrityError:
            # UNIQUE(invoice_id, rung) — another run already drafted this rung.
            session.rollback()
            continue
        session.commit()

        # drafted → pending_approval (+ audit), then notify.
        reminder.status = AR_STATUS_PENDING_APPROVAL
        record_status_audit(
            session,
            reminder,
            AR_STATUS_DRAFTED,
            AR_STATUS_PENDING_APPROVAL,
            "auto",
        )
        session.commit()

        summary.drafted += 1
        summary.drafted_ids.append(reminder.id)

        result = notify_draft(
            session,
            reminder,
            invoice=invoice,
            today=today,
            apply=apply,
        )
        if result.status == "sent":
            summary.notified += 1
        elif result.status != "dry_run":
            summary.notify_failed += 1

    return summary


def _bucket_for_days(days: int) -> str:
    if days < 14:
        return "current"
    if days < 30:
        return "14"
    if days < 45:
        return "30"
    return "45+"


def aging_buckets(session: Session, *, as_of: date) -> dict[str, Decimal]:
    """AR aging over unpaid SENT/OVERDUE invoices, bucketed by days past sent_at.

    REQ-ARC-003: exports current / 14 / 30 / 45+ dollar totals (abs of invoice
    total) for the WBR scorecard. Invoices without a ``sent_at`` are skipped.
    """
    buckets: dict[str, Decimal] = {
        "current": Decimal("0.00"),
        "14": Decimal("0.00"),
        "30": Decimal("0.00"),
        "45+": Decimal("0.00"),
    }
    invoices = (
        session.query(Invoice)
        .filter(
            Invoice.status.in_(_ACTIVE_INVOICE_STATUSES),
            Invoice.paid_date.is_(None),
        )
        .all()
    )
    for invoice in invoices:
        if invoice.sent_at is None:
            continue
        days = (as_of - invoice.sent_at.date()).days
        amount = abs(Decimal(str(invoice.total)))
        buckets[_bucket_for_days(days)] += amount
    return buckets
