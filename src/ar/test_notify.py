"""Tests for the AR draft-notification emitter + ledger row (REQ-ARC-002)."""

from __future__ import annotations

import json
from collections.abc import Generator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import src.ar.notify as notify_mod
from src.alerts.models import AlertDispatch
from src.alerts.webhook import WebhookResult
from src.ar.notify import build_notification_payload, notify_draft
from src.models.ar_reminder import ArReminder
from src.models.base import Base
from src.models.invoice import Customer, Invoice

_ENGINE = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
# Create only the tables these tests touch (see test_chaser.py note).
_TABLES: list[Any] = [
    Customer.__table__,
    Invoice.__table__,
    ArReminder.__table__,
    AlertDispatch.__table__,
]
Base.metadata.create_all(_ENGINE, tables=_TABLES)
_Session = sessionmaker(bind=_ENGINE)

TODAY = date(2026, 7, 1)


@pytest.fixture()
def session() -> Generator[Session, None, None]:
    s = _Session()
    yield s
    for model in (AlertDispatch, ArReminder, Invoice, Customer):
        s.query(model).delete()
    s.commit()
    s.close()


def _seed(session: Session) -> tuple[Invoice, ArReminder]:
    customer = Customer(name="Acme", contact_email="a@b.com", billing_model="hourly")
    session.add(customer)
    session.flush()
    invoice = Invoice(
        invoice_number="INV-N1",
        customer_id=customer.id,
        entity="sparkry",
        status="sent",
        subtotal=Decimal("500.00"),
        total=Decimal("500.00"),
    )
    session.add(invoice)
    session.flush()
    reminder = ArReminder(
        invoice_id=invoice.id,
        rung=30,
        status="pending_approval",
        draft_subject="Second reminder — invoice INV-N1 is past due",
        draft_body="body",
    )
    session.add(reminder)
    session.commit()
    return invoice, reminder


def test_payload_carries_callback_and_token(session: Session) -> None:
    """REQ-ARC-002: the notification payload carries approve/dismiss URLs + token."""
    invoice, reminder = _seed(session)
    payload = build_notification_payload(reminder, invoice)
    assert payload["type"] == "info"
    assert payload["alert_key"] == f"ar:{invoice.id}:30"
    callback: dict[str, Any] = payload["callback"]
    assert callback["token"] == reminder.approval_token
    assert reminder.id in callback["approve_url"]
    assert callback["approve_url"].endswith("/approve")
    assert callback["dismiss_url"].endswith("/dismiss")


def test_notify_records_dispatch_row_with_payload_and_channel(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQ-ARC-002: a successful notify writes an n8n_webhook ledger row."""
    invoice, reminder = _seed(session)
    monkeypatch.setattr(
        notify_mod, "post_payload", lambda p, **k: WebhookResult("sent", 200, None)
    )
    result = notify_draft(session, reminder, invoice=invoice, today=TODAY, apply=True)
    assert result.status == "sent"

    row = session.query(AlertDispatch).one()
    assert row.alert_type == "ar_reminder_notify"
    assert row.alert_key == f"ar:{invoice.id}:30"
    assert row.occurrence_date == TODAY.isoformat()
    assert row.delivery_channel == "n8n_webhook"
    assert row.status == "sent"
    assert row.entity == "sparkry"
    # payload_json round-trips and contains the callback.
    assert row.payload_json is not None
    stored = json.loads(row.payload_json)
    assert stored["callback"]["token"] == reminder.approval_token


def test_notify_records_failed_row_when_webhook_fails(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQ-ARC-002: a failed POST still writes a ledger row (sweep-eligible)."""
    invoice, reminder = _seed(session)
    monkeypatch.setattr(
        notify_mod,
        "post_payload",
        lambda p, **k: WebhookResult("failed", None, "network error"),
    )
    notify_draft(session, reminder, invoice=invoice, today=TODAY, apply=True)
    row = session.query(AlertDispatch).one()
    assert row.status == "failed"
    assert row.delivery_channel == "n8n_webhook"
    assert row.payload_json is not None


def test_dry_run_notify_writes_no_row(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQ-ARC-002: DRY-RUN notify posts nothing and writes no ledger row."""
    invoice, reminder = _seed(session)
    monkeypatch.setattr(
        notify_mod, "post_payload", lambda p, **k: WebhookResult("dry_run", None, None)
    )
    notify_draft(session, reminder, invoice=invoice, today=TODAY, apply=False)
    assert session.query(AlertDispatch).count() == 0
