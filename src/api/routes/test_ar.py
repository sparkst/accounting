"""Tests for the AR approve/dismiss endpoints (REQ-ARC-002).

Auth is self-contained: X-Webhook-Secret header + single-use approval token in
the body (no Cloudflare-Access API key). Exactly-once is enforced by the guarded
rowcount claim in approve_and_send, so a re-used token cannot double-send.
"""

from __future__ import annotations

from collections.abc import Generator
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pytest
import resend
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from src.alerts.models import AlertDispatch
from src.models.ar_reminder import (
    AR_STATUS_PENDING_APPROVAL,
    AR_STATUS_SENT,
    ArReminder,
)
from src.models.audit_event import AuditEvent
from src.models.base import Base
from src.models.invoice import Customer, Invoice, InvoiceLineItem
from src.models.transaction import Transaction

_TEST_DB_URI = "file:ar_endpoint_test?mode=memory&cache=shared&uri=true"
_test_engine = create_engine(
    "sqlite+pysqlite:///" + _TEST_DB_URI,
    connect_args={"check_same_thread": False, "uri": True},
)


@event.listens_for(_test_engine, "connect")
def _set_pragmas(conn: Any, _record: Any) -> None:
    cursor = conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


# Create only the tables these tests touch (the full metadata carries unrelated
# FK targets that may be unimported at collection time).
_TABLES: list[Any] = [
    Transaction.__table__,  # FK target of invoices + audit_events
    Customer.__table__,
    Invoice.__table__,
    InvoiceLineItem.__table__,
    ArReminder.__table__,
    AuditEvent.__table__,
    AlertDispatch.__table__,
]
Base.metadata.create_all(bind=_test_engine, tables=_TABLES)
_TestSession = sessionmaker(bind=_test_engine, autocommit=False, autoflush=False)
_CLEAN_ORDER = [AlertDispatch, AuditEvent, ArReminder, InvoiceLineItem, Invoice, Customer]

SECRET = "top-secret-webhook"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("N8N_ALERTS_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("RESEND_API_KEY", "test-key")


@pytest.fixture(autouse=True)
def clean_db() -> Generator[None, None, None]:
    s = _TestSession()
    for model in _CLEAN_ORDER:
        s.query(model).delete()
    s.commit()
    s.close()
    yield


@pytest.fixture()
def resend_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _fake_send(params: dict[str, Any]) -> dict[str, str]:
        calls.append(params)
        return {"id": f"msg_{len(calls)}"}

    monkeypatch.setattr(resend.Emails, "send", staticmethod(_fake_send))
    return calls


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    from src.api import main as _main_module
    from src.api.routes import ar as _ar_module

    with (
        patch.object(_ar_module, "SessionLocal", _TestSession),
        patch.object(_main_module, "init_db", return_value=None),
        patch.object(_main_module, "seed_vendor_rules", return_value=0),
        patch.object(_main_module, "seed_customers", return_value={
            "customers_inserted": 0,
            "customers_updated": 0,
            "invoices_inserted": 0,
        }),
    ):
        from src.api.main import app

        with TestClient(app) as c:
            yield c


def _seed_reminder(status: str = AR_STATUS_PENDING_APPROVAL) -> ArReminder:
    s = _TestSession()
    try:
        customer = Customer(
            name="Acme", contact_email="jane@example.com", billing_model="hourly"
        )
        s.add(customer)
        s.flush()
        invoice = Invoice(
            invoice_number="INV-A1",
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
            draft_subject="Second reminder — invoice INV-A1",
            draft_body="Please pay.",
        )
        s.add(reminder)
        s.commit()
        s.refresh(reminder)
        s.expunge(reminder)
        return reminder
    finally:
        s.close()


def test_approve_happy_path_sends(
    client: TestClient, resend_calls: list[dict[str, Any]]
) -> None:
    """REQ-ARC-002: valid secret + token approves and sends the reminder."""
    reminder = _seed_reminder()
    resp = client.post(
        f"/api/ar/reminders/{reminder.id}/approve",
        headers={"X-Webhook-Secret": SECRET},
        json={"token": reminder.approval_token},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["sent"] is True
    assert body["status"] == "sent"
    assert len(resend_calls) == 1

    s = _TestSession()
    try:
        refreshed = s.get(ArReminder, reminder.id)
        assert refreshed is not None
        assert refreshed.status == AR_STATUS_SENT
        assert refreshed.approved_via == "telegram"
    finally:
        s.close()


def test_approve_rejects_bad_secret(
    client: TestClient, resend_calls: list[dict[str, Any]]
) -> None:
    """REQ-ARC-002: a wrong webhook secret is 401 and never sends."""
    reminder = _seed_reminder()
    resp = client.post(
        f"/api/ar/reminders/{reminder.id}/approve",
        headers={"X-Webhook-Secret": "wrong"},
        json={"token": reminder.approval_token},
    )
    assert resp.status_code == 401
    assert len(resend_calls) == 0


def test_approve_rejects_bad_token(
    client: TestClient, resend_calls: list[dict[str, Any]]
) -> None:
    """REQ-ARC-002: a wrong approval token is 403 and never sends."""
    reminder = _seed_reminder()
    resp = client.post(
        f"/api/ar/reminders/{reminder.id}/approve",
        headers={"X-Webhook-Secret": SECRET},
        json={"token": "not-the-token"},
    )
    assert resp.status_code == 403
    assert len(resend_calls) == 0


def test_single_use_token_second_approve_409(
    client: TestClient, resend_calls: list[dict[str, Any]]
) -> None:
    """REQ-ARC-002: the token is single-use — a second approve is 409, no re-send."""
    reminder = _seed_reminder()
    ok = client.post(
        f"/api/ar/reminders/{reminder.id}/approve",
        headers={"X-Webhook-Secret": SECRET},
        json={"token": reminder.approval_token},
    )
    assert ok.status_code == 200

    again = client.post(
        f"/api/ar/reminders/{reminder.id}/approve",
        headers={"X-Webhook-Secret": SECRET},
        json={"token": reminder.approval_token},
    )
    assert again.status_code == 409
    assert len(resend_calls) == 1


def test_approve_falls_back_to_legacy_n8n_secret(
    client: TestClient, resend_calls: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """P3-203: with AR_APPROVE_SECRET unset, the legacy shared webhook secret
    still authenticates (backward compatibility)."""
    monkeypatch.delenv("AR_APPROVE_SECRET", raising=False)
    reminder = _seed_reminder()
    resp = client.post(
        f"/api/ar/reminders/{reminder.id}/approve",
        headers={"X-Webhook-Secret": SECRET},
        json={"token": reminder.approval_token},
    )
    assert resp.status_code == 200
    assert len(resend_calls) == 1


def test_approve_accepts_dedicated_ar_approve_secret(
    client: TestClient, resend_calls: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """P3-203: a dedicated AR_APPROVE_SECRET authenticates independently of
    the legacy N8N_ALERTS_WEBHOOK_SECRET."""
    monkeypatch.setenv("AR_APPROVE_SECRET", "dedicated-ar-secret")
    reminder = _seed_reminder()
    resp = client.post(
        f"/api/ar/reminders/{reminder.id}/approve",
        headers={"X-Webhook-Secret": "dedicated-ar-secret"},
        json={"token": reminder.approval_token},
    )
    assert resp.status_code == 200
    assert len(resend_calls) == 1


def test_approve_dedicated_secret_set_rejects_legacy_secret(
    client: TestClient, resend_calls: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """P3-203: once AR_APPROVE_SECRET is set, it takes precedence — the old
    legacy secret alone no longer authenticates."""
    monkeypatch.setenv("AR_APPROVE_SECRET", "dedicated-ar-secret")
    reminder = _seed_reminder()
    resp = client.post(
        f"/api/ar/reminders/{reminder.id}/approve",
        headers={"X-Webhook-Secret": SECRET},  # legacy N8N_ALERTS_WEBHOOK_SECRET value
        json={"token": reminder.approval_token},
    )
    assert resp.status_code == 401
    assert len(resend_calls) == 0


def test_approve_unknown_reminder_404(client: TestClient) -> None:
    """REQ-ARC-002: an unknown reminder id is 404."""
    resp = client.post(
        "/api/ar/reminders/does-not-exist/approve",
        headers={"X-Webhook-Secret": SECRET},
        json={"token": "x"},
    )
    assert resp.status_code == 404


def _seed_reminder_for_closed_invoice(invoice_status: str) -> ArReminder:
    """A pending-approval reminder whose invoice has since closed (paid/void)
    — reproduces the race where the invoice closes between drafting and the
    human tapping Approve."""
    s = _TestSession()
    try:
        customer = Customer(
            name="Acme", contact_email="jane@example.com", billing_model="hourly"
        )
        s.add(customer)
        s.flush()
        invoice = Invoice(
            invoice_number="INV-CLOSED",
            customer_id=customer.id,
            entity="sparkry",
            status=invoice_status,
            subtotal=Decimal("500.00"),
            total=Decimal("500.00"),
            paid_date="2026-07-01" if invoice_status == "paid" else None,
        )
        s.add(invoice)
        s.flush()
        reminder = ArReminder(
            invoice_id=invoice.id,
            rung=30,
            status=AR_STATUS_PENDING_APPROVAL,
            draft_subject="Second reminder — invoice INV-CLOSED",
            draft_body="Please pay.",
        )
        s.add(reminder)
        s.commit()
        s.refresh(reminder)
        s.expunge(reminder)
        return reminder
    finally:
        s.close()


def test_approve_paid_invoice_returns_409_and_does_not_send(
    client: TestClient, resend_calls: list[dict[str, Any]]
) -> None:
    """P2-202: an invoice that closed (paid) between draft and approval is
    409, and the send function is never invoked."""
    reminder = _seed_reminder_for_closed_invoice("paid")
    resp = client.post(
        f"/api/ar/reminders/{reminder.id}/approve",
        headers={"X-Webhook-Secret": SECRET},
        json={"token": reminder.approval_token},
    )
    assert resp.status_code == 409
    assert len(resend_calls) == 0


def test_approve_void_invoice_returns_409_and_does_not_send(
    client: TestClient, resend_calls: list[dict[str, Any]]
) -> None:
    """P2-202: a void invoice is also 409, and never sends."""
    reminder = _seed_reminder_for_closed_invoice("void")
    resp = client.post(
        f"/api/ar/reminders/{reminder.id}/approve",
        headers={"X-Webhook-Secret": SECRET},
        json={"token": reminder.approval_token},
    )
    assert resp.status_code == 409
    assert len(resend_calls) == 0


def test_dismiss_happy_path(
    client: TestClient, resend_calls: list[dict[str, Any]]
) -> None:
    """REQ-ARC-002: dismiss transitions to dismissed without sending."""
    reminder = _seed_reminder()
    resp = client.post(
        f"/api/ar/reminders/{reminder.id}/dismiss",
        headers={"X-Webhook-Secret": SECRET},
        json={"token": reminder.approval_token},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "dismissed"
    assert len(resend_calls) == 0

    s = _TestSession()
    try:
        refreshed = s.get(ArReminder, reminder.id)
        assert refreshed is not None
        assert refreshed.status == "dismissed"
    finally:
        s.close()
