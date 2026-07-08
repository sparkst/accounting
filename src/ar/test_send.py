"""Tests for approve-and-send: exactly-once guard + ledger + audit (REQ-ARC-001/002)."""

from __future__ import annotations

from collections.abc import Generator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
import resend
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.alerts.models import AlertDispatch
from src.ar.send import approve_and_send
from src.models.ar_reminder import (
    AR_STATUS_DISMISSED,
    AR_STATUS_FAILED,
    AR_STATUS_PENDING_APPROVAL,
    AR_STATUS_SENT,
    ArReminder,
)
from src.models.audit_event import AuditEvent
from src.models.base import Base
from src.models.invoice import Customer, Invoice, InvoiceLineItem

_ENGINE = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
# Create only the tables these tests touch (see test_chaser.py note).
_TABLES: list[Any] = [
    Customer.__table__,
    Invoice.__table__,
    InvoiceLineItem.__table__,
    ArReminder.__table__,
    AuditEvent.__table__,
    AlertDispatch.__table__,
]
Base.metadata.create_all(_ENGINE, tables=_TABLES)
_Session = sessionmaker(bind=_ENGINE)

TODAY = date(2026, 7, 1)


@pytest.fixture(autouse=True)
def _resend_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESEND_API_KEY", "test-key")


@pytest.fixture()
def session() -> Generator[Session, None, None]:
    s = _Session()
    yield s
    for model in (AlertDispatch, AuditEvent, ArReminder, Invoice, Customer):
        s.query(model).delete()
    s.commit()
    s.close()


def _seed(
    session: Session,
    *,
    contact_email: str | None = "jane@example.com",
    invoice_status: str = "overdue",
    paid_date: str | None = None,
) -> ArReminder:
    customer = Customer(
        name="Acme", contact_email=contact_email, billing_model="hourly"
    )
    session.add(customer)
    session.flush()
    invoice = Invoice(
        invoice_number="INV-S1",
        customer_id=customer.id,
        entity="sparkry",
        status=invoice_status,
        paid_date=paid_date,
        subtotal=Decimal("750.00"),
        total=Decimal("750.00"),
    )
    session.add(invoice)
    session.flush()
    reminder = ArReminder(
        invoice_id=invoice.id,
        rung=45,
        status=AR_STATUS_PENDING_APPROVAL,
        draft_subject="Final notice — invoice INV-S1",
        draft_body="Please pay.",
    )
    session.add(reminder)
    session.commit()
    return reminder


def _mock_send(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _fake_send(params: dict[str, Any]) -> dict[str, str]:
        calls.append(params)
        return {"id": f"msg_{len(calls)}"}

    monkeypatch.setattr(resend.Emails, "send", staticmethod(_fake_send))
    return calls


def test_approve_sends_and_records(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQ-ARC-002: approval sends via Resend and records id/sent_at/status."""
    calls = _mock_send(monkeypatch)
    reminder = _seed(session)

    result = approve_and_send(session, reminder, approved_via="cli", today=TODAY)
    assert result.sent is True
    assert result.message_id == "msg_1"
    assert len(calls) == 1

    session.refresh(reminder)
    assert reminder.status == AR_STATUS_SENT
    assert reminder.resend_message_id == "msg_1"
    assert reminder.sent_at is not None
    assert reminder.approved_via == "cli"

    # resend_email ledger row, payload_json NULL.
    row = (
        session.query(AlertDispatch)
        .filter(AlertDispatch.alert_type == "ar_reminder_send")
        .one()
    )
    assert row.delivery_channel == "resend_email"
    assert row.payload_json is None
    assert row.status == "sent"


def test_double_approve_race_sends_once(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQ-ARC-002: the guarded rowcount claim makes a double-tap send once."""
    calls = _mock_send(monkeypatch)
    reminder = _seed(session)

    first = approve_and_send(session, reminder, approved_via="telegram", today=TODAY)
    second = approve_and_send(session, reminder, approved_via="telegram", today=TODAY)

    assert first.sent is True
    assert second.sent is False
    assert second.status == "not_pending"
    assert len(calls) == 1  # never double-sends


def test_audit_rows_per_transition(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQ-ARC-003: each status transition writes an entity-mode AuditEvent."""
    _mock_send(monkeypatch)
    reminder = _seed(session)
    approve_and_send(session, reminder, approved_via="cli", today=TODAY)

    audits = (
        session.query(AuditEvent)
        .filter(
            AuditEvent.entity_type == "ar_reminder",
            AuditEvent.entity_id == reminder.id,
            AuditEvent.field_changed == "status",
        )
        .all()
    )
    transitions = {(a.old_value, a.new_value) for a in audits}
    assert ("pending_approval", "approved") in transitions
    assert ("approved", "sent") in transitions


def test_send_failure_marks_failed_and_logs(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQ-ARC-002: a Resend failure moves the reminder to failed (retryable)."""
    def _boom(params: dict[str, Any]) -> dict[str, str]:
        raise RuntimeError("resend down")

    monkeypatch.setattr(resend.Emails, "send", staticmethod(_boom))
    reminder = _seed(session)

    result = approve_and_send(session, reminder, approved_via="cli", today=TODAY)
    assert result.sent is False
    assert result.status == "failed"

    session.refresh(reminder)
    assert reminder.status == AR_STATUS_FAILED
    row = (
        session.query(AlertDispatch)
        .filter(AlertDispatch.alert_type == "ar_reminder_send")
        .one()
    )
    assert row.status == "failed"


def test_missing_recipient_email_fails(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQ-ARC-002: no send without a recipient address."""
    calls = _mock_send(monkeypatch)
    reminder = _seed(session, contact_email=None)
    result = approve_and_send(session, reminder, approved_via="cli", today=TODAY)
    assert result.sent is False
    assert result.status == "failed"
    assert len(calls) == 0


def test_approve_paid_invoice_does_not_send(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1-001: an invoice paid between draft and approval is never dunned.

    The invoice can transition to PAID independently of the daily chaser
    sweep (PATCH /invoices/{id}, match_payment) at any point between a
    reminder being drafted and Travis tapping Approve. Approving must
    re-check and dismiss instead of sending.
    """
    calls = _mock_send(monkeypatch)
    reminder = _seed(session, invoice_status="paid", paid_date="2026-07-01")

    result = approve_and_send(session, reminder, approved_via="telegram", today=TODAY)

    assert result.sent is False
    assert result.status == "invoice_closed"
    assert len(calls) == 0  # never calls Resend

    session.refresh(reminder)
    assert reminder.status == AR_STATUS_DISMISSED

    audits = (
        session.query(AuditEvent)
        .filter(
            AuditEvent.entity_type == "ar_reminder",
            AuditEvent.entity_id == reminder.id,
            AuditEvent.field_changed == "status",
        )
        .all()
    )
    transitions = {(a.old_value, a.new_value) for a in audits}
    assert ("pending_approval", "approved") in transitions
    assert ("approved", "dismissed") in transitions


def test_approve_void_invoice_does_not_send(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1-001: a voided invoice is never dunned even if already approved-claimed."""
    calls = _mock_send(monkeypatch)
    reminder = _seed(session, invoice_status="void")

    result = approve_and_send(session, reminder, approved_via="cli", today=TODAY)

    assert result.sent is False
    assert result.status == "invoice_closed"
    assert len(calls) == 0
    session.refresh(reminder)
    assert reminder.status == AR_STATUS_DISMISSED
