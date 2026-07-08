"""Tests for the AR chaser CLI (REQ-ARC-001/002)."""

from __future__ import annotations

from collections.abc import Generator
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import resend
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import scripts.ar_chaser as cli
from src.models.ar_reminder import (
    AR_STATUS_PENDING_APPROVAL,
    AR_STATUS_SENT,
    ArReminder,
)
from src.models.base import Base
from src.models.invoice import Customer, Invoice

_ENGINE = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
# Create only the tables these tests touch (unrelated FK targets in the full
# metadata may be unimported at collection time).
from src.alerts.models import AlertDispatch  # noqa: E402
from src.models.audit_event import AuditEvent  # noqa: E402
from src.models.invoice import InvoiceLineItem  # noqa: E402

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


@pytest.fixture(autouse=True)
def _patch_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "SessionLocal", _Session)
    monkeypatch.setattr(cli, "init_db", lambda: None)
    monkeypatch.setenv("RESEND_API_KEY", "test-key")


@pytest.fixture(autouse=True)
def _clean() -> Generator[None, None, None]:
    yield
    s = _Session()
    for model in (ArReminder, Invoice, Customer):
        s.query(model).delete()
    s.commit()
    s.close()


def _seed_invoice(*, days_since_sent: int) -> str:
    s = _Session()
    try:
        customer = Customer(
            name="Acme", contact_email="jane@example.com", billing_model="hourly"
        )
        s.add(customer)
        s.flush()
        sent_at = datetime(2026, 6, 1) - timedelta(days=days_since_sent)
        invoice = Invoice(
            invoice_number=f"INV-{days_since_sent}-{customer.id[:6]}",
            customer_id=customer.id,
            entity="sparkry",
            status="sent",
            subtotal=Decimal("500.00"),
            total=Decimal("500.00"),
            sent_at=sent_at,
        )
        s.add(invoice)
        s.commit()
        return invoice.id
    finally:
        s.close()


def _seed_reminder(status: str = AR_STATUS_PENDING_APPROVAL) -> str:
    s = _Session()
    try:
        customer = Customer(
            name="Acme", contact_email="jane@example.com", billing_model="hourly"
        )
        s.add(customer)
        s.flush()
        invoice = Invoice(
            invoice_number=f"INV-R-{customer.id[:6]}",
            customer_id=customer.id,
            entity="sparkry",
            status="overdue",
            subtotal=Decimal("500.00"),
            total=Decimal("500.00"),
        )
        s.add(invoice)
        s.flush()
        reminder = ArReminder(
            invoice_id=invoice.id,
            rung=30,
            status=status,
            draft_subject="Second reminder — invoice",
            draft_body="Please pay.",
        )
        s.add(reminder)
        s.commit()
        return reminder.id
    finally:
        s.close()


def test_run_dry_run_writes_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    """REQ-ARC-001: `run` without --apply drafts nothing to the DB."""
    _seed_invoice(days_since_sent=20)
    rc = cli.main(["run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    s = _Session()
    try:
        assert s.query(ArReminder).count() == 0
    finally:
        s.close()


def test_cli_approve_sends(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQ-ARC-002: CLI `approve` sends via Resend with approved_via=cli."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        resend.Emails,
        "send",
        staticmethod(lambda params: calls.append(params) or {"id": "msg_1"}),
    )
    reminder_id = _seed_reminder()
    rc = cli.main(["approve", reminder_id])
    assert rc == 0
    assert len(calls) == 1
    s = _Session()
    try:
        reminder = s.get(ArReminder, reminder_id)
        assert reminder is not None
        assert reminder.status == AR_STATUS_SENT
        assert reminder.approved_via == "cli"
    finally:
        s.close()


def test_cli_dismiss(capsys: pytest.CaptureFixture[str]) -> None:
    """REQ-ARC-002: CLI `dismiss` closes a pending reminder without sending."""
    reminder_id = _seed_reminder()
    rc = cli.main(["dismiss", reminder_id])
    assert rc == 0
    s = _Session()
    try:
        reminder = s.get(ArReminder, reminder_id)
        assert reminder is not None
        assert reminder.status == "dismissed"
    finally:
        s.close()


def test_cli_list(capsys: pytest.CaptureFixture[str]) -> None:
    """REQ-ARC-002: CLI `list` shows open reminders."""
    reminder_id = _seed_reminder()
    rc = cli.main(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert reminder_id in out


def test_cli_approve_unknown_id(capsys: pytest.CaptureFixture[str]) -> None:
    """REQ-ARC-002: approving a missing reminder is a clean non-zero exit."""
    rc = cli.main(["approve", "nope"])
    assert rc == 1
