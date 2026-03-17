"""Invoice API integration tests.

Uses the same shared-cache in-memory SQLite pattern as test_api.py.

REQ-ID: INV-TEST-001  GET /api/invoices returns list with AR aging.
REQ-ID: INV-TEST-002  GET /api/invoices/{id} returns invoice with line_items.
REQ-ID: INV-TEST-003  PATCH /api/invoices/{id} edits draft; 422 if not draft.
REQ-ID: INV-TEST-004  PATCH /api/invoices/{id}/status transitions per state machine.
REQ-ID: INV-TEST-005  POST /api/invoices/generate-flat creates flat-rate invoice.
REQ-ID: INV-TEST-006  POST /api/invoices/generate-calendar creates calendar invoice.
REQ-ID: INV-TEST-007  GET /api/invoices/{id}/pdf returns PDF bytes.
REQ-ID: INV-TEST-008  GET /api/customers CRUD endpoints.
REQ-ID: INV-TEST-009  POST /api/transactions/bulk-confirm confirms + creates rules.
REQ-ID: INV-TEST-010  Duplicate guards on generate-flat and generate-calendar.
REQ-ID: INV-TEST-011  Status transition audit events are created.
REQ-ID: INV-TEST-012  paid->void unlinks payment_transaction_id.
"""

from __future__ import annotations

import contextlib
import decimal
import uuid
from collections.abc import Generator
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import src.db.connection as _conn  # noqa: F401
from src.models.audit_event import AuditEvent
from src.models.base import Base
from src.models.enums import (
    BillingModel,
    ConfirmedBy,
    Direction,
    InvoiceStatus,
    Source,
    TransactionStatus,
)
from src.models.invoice import Customer, Invoice, InvoiceLineItem
from src.models.transaction import Transaction
from src.models.vendor_rule import VendorRule

# ---------------------------------------------------------------------------
# Shared-cache in-memory database
# ---------------------------------------------------------------------------

_TEST_DB_URI = "file:invoice_test?mode=memory&cache=shared&uri=true"

_test_engine = create_engine(
    "sqlite+pysqlite:///" + _TEST_DB_URI.replace("file:", ""),
    connect_args={"check_same_thread": False, "uri": True},
    poolclass=StaticPool,
)


@event.listens_for(_test_engine, "connect")
def _set_pragmas(conn: Any, _record: Any) -> None:
    cursor = conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


Base.metadata.create_all(bind=_test_engine)

_TestSession = sessionmaker(bind=_test_engine, autocommit=False, autoflush=False)

# Keep a connection alive to the shared-cache DB to prevent it from being
# garbage collected between tests (shared-cache DBs are destroyed when the
# last connection closes).
_keepalive_conn = _test_engine.connect()


@pytest.fixture(autouse=True)
def clean_db() -> Generator[None, None, None]:
    with _test_engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
        conn.execute(text("PRAGMA foreign_keys=ON"))
    yield


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    from src.api import main as _main_module

    with (
        patch.object(_main_module, "init_db", return_value=None),
        patch.object(_main_module, "seed_vendor_rules", return_value=0),
        patch.object(_main_module, "seed_customers", return_value={"customers_inserted": 0, "customers_updated": 0, "invoices_inserted": 0}),
    ):
        # Patch get_db in the invoices module to use test session
        def _test_get_db() -> Generator[Session, None, None]:
            s = _TestSession()
            try:
                yield s
            finally:
                s.close()

        from src.api.deps import get_db
        from src.api.main import app

        app.dependency_overrides[get_db] = _test_get_db

        with TestClient(app) as c:
            yield c

        app.dependency_overrides.clear()


@pytest.fixture()
def db_session() -> Generator[Session, None, None]:
    s = _TestSession()
    try:
        yield s
    finally:
        with contextlib.suppress(Exception):
            s.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_customer(
    session: Session,
    *,
    name: str = "Test Customer",
    billing_model: str = BillingModel.FLAT_RATE.value,
    default_rate: str = "33000.00",
    invoice_prefix: str = "TC",
    payment_terms: str = "Net 30",
    contract_start_date: str = "2026-01-01",
) -> Customer:
    cust = Customer(
        id=str(uuid.uuid4()),
        name=name,
        billing_model=billing_model,
        default_rate=decimal.Decimal(default_rate),
        invoice_prefix=invoice_prefix,
        payment_terms=payment_terms,
        contract_start_date=contract_start_date,
    )
    session.add(cust)
    session.commit()
    session.refresh(cust)
    return cust


def _make_invoice(
    session: Session,
    customer_id: str,
    *,
    invoice_number: str = "TC20260131",
    status: str = InvoiceStatus.DRAFT.value,
    subtotal: str = "33000.00",
    service_period_start: str = "2026-01-05",
    submitted_date: str | None = None,
    due_date: str | None = None,
) -> Invoice:
    inv_id = str(uuid.uuid4())
    amt = decimal.Decimal(subtotal)
    inv = Invoice(
        id=inv_id,
        invoice_number=invoice_number,
        customer_id=customer_id,
        entity="sparkry",
        subtotal=amt,
        adjustments=decimal.Decimal("0.00"),
        tax=decimal.Decimal("0.00"),
        total=amt,
        status=status,
        service_period_start=service_period_start,
        submitted_date=submitted_date,
        due_date=due_date,
    )
    session.add(inv)

    li = InvoiceLineItem(
        id=str(uuid.uuid4()),
        invoice_id=inv_id,
        description="Test line item",
        quantity=decimal.Decimal("1.0000"),
        unit_price=amt,
        total_price=amt,
        sort_order=0,
    )
    session.add(li)

    session.commit()
    session.refresh(inv)
    return inv


def _make_transaction(
    session: Session,
    *,
    description: str = "Test Vendor",
    amount: str = "-50.00",
    direction: str = Direction.EXPENSE.value,
    status: str = TransactionStatus.NEEDS_REVIEW.value,
) -> Transaction:
    import hashlib
    tx_id = str(uuid.uuid4())
    source_id = str(uuid.uuid4())
    source_hash = hashlib.sha256(f"gmail_n8n:{source_id}".encode()).hexdigest()
    tx = Transaction(
        id=tx_id,
        source=Source.GMAIL_N8N.value,
        source_id=source_id,
        source_hash=source_hash,
        date="2026-03-01",
        description=description,
        amount=decimal.Decimal(amount),
        currency="USD",
        direction=direction,
        status=status,
        confidence=0.5,
        confirmed_by=ConfirmedBy.AUTO.value,
        raw_data={"test": True},
    )
    session.add(tx)
    session.commit()
    session.refresh(tx)
    return tx


# ---------------------------------------------------------------------------
# Invoice list / detail tests
# ---------------------------------------------------------------------------


class TestInvoiceList:
    """INV-TEST-001: GET /api/invoices"""

    def test_list_empty(self, client: TestClient) -> None:
        r = client.get("/api/invoices")
        assert r.status_code == 200
        data = r.json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_list_with_invoices(self, client: TestClient, db_session: Session) -> None:
        cust = _make_customer(db_session)
        _make_invoice(db_session, cust.id)
        r = client.get("/api/invoices")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1

    def test_list_filter_by_customer(self, client: TestClient, db_session: Session) -> None:
        c1 = _make_customer(db_session, name="Cust A", invoice_prefix="CA")
        c2 = _make_customer(db_session, name="Cust B", invoice_prefix="CB")
        _make_invoice(db_session, c1.id, invoice_number="CA001")
        _make_invoice(db_session, c2.id, invoice_number="CB001")

        r = client.get(f"/api/invoices?customer_id={c1.id}")
        assert r.status_code == 200
        assert r.json()["total"] == 1
        assert r.json()["items"][0]["customer_id"] == c1.id

    def test_list_filter_by_status(self, client: TestClient, db_session: Session) -> None:
        cust = _make_customer(db_session)
        _make_invoice(db_session, cust.id, invoice_number="A001", status=InvoiceStatus.DRAFT.value)
        _make_invoice(db_session, cust.id, invoice_number="A002", status=InvoiceStatus.SENT.value)

        r = client.get("/api/invoices?status=draft")
        assert r.status_code == 200
        assert r.json()["total"] == 1

    def test_list_ar_aging_sent_invoice(self, client: TestClient, db_session: Session) -> None:
        cust = _make_customer(db_session)
        _make_invoice(
            db_session, cust.id,
            invoice_number="AR001",
            status=InvoiceStatus.SENT.value,
            submitted_date="2026-01-01",
            due_date="2026-04-01",
        )
        r = client.get("/api/invoices")
        assert r.status_code == 200
        item = r.json()["items"][0]
        assert item["days_outstanding"] is not None
        assert item["expected_payment_date"] == "2026-04-01"


class TestInvoiceDetail:
    """INV-TEST-002: GET /api/invoices/{id}"""

    def test_get_invoice_with_line_items(self, client: TestClient, db_session: Session) -> None:
        cust = _make_customer(db_session)
        inv = _make_invoice(db_session, cust.id)

        r = client.get(f"/api/invoices/{inv.id}")
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == inv.id
        assert data["line_items"] is not None
        assert len(data["line_items"]) == 1

    def test_get_invoice_not_found(self, client: TestClient) -> None:
        r = client.get(f"/api/invoices/{uuid.uuid4()}")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Invoice PATCH tests
# ---------------------------------------------------------------------------


class TestInvoicePatch:
    """INV-TEST-003: PATCH /api/invoices/{id}"""

    def test_patch_draft_invoice(self, client: TestClient, db_session: Session) -> None:
        cust = _make_customer(db_session)
        inv = _make_invoice(db_session, cust.id)

        r = client.patch(f"/api/invoices/{inv.id}", json={"notes": "Updated notes"})
        assert r.status_code == 200
        assert r.json()["notes"] == "Updated notes"

    def test_patch_non_draft_returns_422(self, client: TestClient, db_session: Session) -> None:
        cust = _make_customer(db_session)
        inv = _make_invoice(db_session, cust.id, status=InvoiceStatus.SENT.value)

        r = client.patch(f"/api/invoices/{inv.id}", json={"notes": "Should fail"})
        assert r.status_code == 422

    def test_patch_line_items(self, client: TestClient, db_session: Session) -> None:
        cust = _make_customer(db_session)
        inv = _make_invoice(db_session, cust.id)

        r = client.patch(f"/api/invoices/{inv.id}", json={
            "line_items": [
                {"description": "New item A", "quantity": 2.0, "unit_price": 100.0},
                {"description": "New item B", "quantity": 1.0, "unit_price": 200.0},
            ]
        })
        assert r.status_code == 200
        data = r.json()
        assert len(data["line_items"]) == 2
        # subtotal should be 2*100 + 1*200 = 400
        assert float(data["subtotal"]) == 400.0


# ---------------------------------------------------------------------------
# Status transition tests
# ---------------------------------------------------------------------------


class TestStatusTransition:
    """INV-TEST-004: PATCH /api/invoices/{id}/status"""

    def test_draft_to_sent(self, client: TestClient, db_session: Session) -> None:
        cust = _make_customer(db_session)
        inv = _make_invoice(db_session, cust.id, status=InvoiceStatus.DRAFT.value)

        r = client.patch(f"/api/invoices/{inv.id}/status", json={"status": "sent"})
        assert r.status_code == 200
        assert r.json()["status"] == "sent"

    def test_sent_to_paid(self, client: TestClient, db_session: Session) -> None:
        cust = _make_customer(db_session)
        inv = _make_invoice(db_session, cust.id, status=InvoiceStatus.SENT.value)

        r = client.patch(f"/api/invoices/{inv.id}/status", json={
            "status": "paid", "paid_date": "2026-03-15"
        })
        assert r.status_code == 200
        assert r.json()["status"] == "paid"
        assert r.json()["paid_date"] == "2026-03-15"

    def test_invalid_transition_returns_422(self, client: TestClient, db_session: Session) -> None:
        cust = _make_customer(db_session)
        inv = _make_invoice(db_session, cust.id, status=InvoiceStatus.DRAFT.value)

        r = client.patch(f"/api/invoices/{inv.id}/status", json={"status": "paid"})
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert "allowed_transitions" in detail

    def test_void_is_terminal(self, client: TestClient, db_session: Session) -> None:
        cust = _make_customer(db_session)
        inv = _make_invoice(db_session, cust.id, status=InvoiceStatus.VOID.value)

        r = client.patch(f"/api/invoices/{inv.id}/status", json={"status": "draft"})
        assert r.status_code == 422

    def test_paid_to_void_unlinks_payment(self, client: TestClient, db_session: Session) -> None:
        """INV-TEST-012: paid->void unlinks payment_transaction_id."""
        cust = _make_customer(db_session)
        inv = _make_invoice(db_session, cust.id, status=InvoiceStatus.PAID.value)

        # Manually set payment_transaction_id
        inv.payment_transaction_id = "fake-tx-id"
        inv.paid_date = "2026-03-01"
        db_session.commit()

        r = client.patch(f"/api/invoices/{inv.id}/status", json={"status": "void"})
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "void"
        assert data["payment_transaction_id"] is None

    def test_status_transition_creates_audit_event(self, client: TestClient, db_session: Session) -> None:
        """INV-TEST-011: Every transition creates AuditEvent."""
        cust = _make_customer(db_session)
        inv = _make_invoice(db_session, cust.id, status=InvoiceStatus.DRAFT.value)

        inv_id = inv.id
        client.patch(f"/api/invoices/{inv_id}/status", json={"status": "sent"})

        # Use a fresh session to verify the audit event (avoids stale connection)
        verify = _TestSession()
        try:
            events = verify.query(AuditEvent).filter(
                AuditEvent.transaction_id == inv_id,
                AuditEvent.field_changed == "status",
            ).all()
            assert len(events) == 1
            assert events[0].old_value == "draft"
            assert events[0].new_value == "sent"
        finally:
            verify.close()


# ---------------------------------------------------------------------------
# Generate flat-rate invoice
# ---------------------------------------------------------------------------


class TestGenerateFlat:
    """INV-TEST-005: POST /api/invoices/generate-flat"""

    def test_generate_flat_success(self, client: TestClient, db_session: Session) -> None:
        cust = _make_customer(
            db_session,
            name="Cardinal Health",
            billing_model=BillingModel.FLAT_RATE.value,
            default_rate="33000.00",
            invoice_prefix="CH",
            contract_start_date="2026-01-05",
        )

        r = client.post("/api/invoices/generate-flat", json={
            "customer_id": cust.id,
            "month": "2026-03",
        })
        assert r.status_code == 201
        data = r.json()
        assert data["status"] == "draft"
        assert float(data["total"]) == 33000.0
        assert data["invoice_number"].startswith("CH")
        assert data["line_items"] is not None
        assert len(data["line_items"]) == 1
        # Month 3 from 2026-01-05
        assert "Month 3" in data["line_items"][0]["description"]

    def test_generate_flat_duplicate_guard(self, client: TestClient, db_session: Session) -> None:
        """INV-TEST-010: Duplicate guard on generate-flat."""
        cust = _make_customer(
            db_session,
            invoice_prefix="DG",
            contract_start_date="2026-01-01",
        )

        # First generation succeeds
        r1 = client.post("/api/invoices/generate-flat", json={
            "customer_id": cust.id, "month": "2026-03",
        })
        assert r1.status_code == 201

        # Second generation for same month returns 422
        r2 = client.post("/api/invoices/generate-flat", json={
            "customer_id": cust.id, "month": "2026-03",
        })
        assert r2.status_code == 422

    def test_generate_flat_customer_not_found(self, client: TestClient) -> None:
        r = client.post("/api/invoices/generate-flat", json={
            "customer_id": str(uuid.uuid4()), "month": "2026-03",
        })
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Generate calendar-based invoice
# ---------------------------------------------------------------------------


class TestGenerateCalendar:
    """INV-TEST-006: POST /api/invoices/generate-calendar"""

    def test_generate_calendar_success(self, client: TestClient, db_session: Session) -> None:
        cust = _make_customer(
            db_session,
            name="Fascinate",
            billing_model=BillingModel.HOURLY.value,
            default_rate="100.00",
            invoice_prefix="",
        )

        r = client.post("/api/invoices/generate-calendar", json={
            "customer_id": cust.id,
            "sessions": [
                {"date": "2026-03-05", "description": "Coaching session 1", "hours": 1.0, "rate": 100.0},
                {"date": "2026-03-12", "description": "Coaching session 2", "hours": 1.5, "rate": 100.0},
            ],
        })
        assert r.status_code == 201
        data = r.json()
        assert data["status"] == "draft"
        assert float(data["total"]) == 250.0  # 1*100 + 1.5*100
        assert data["invoice_number"].startswith("202603-")
        assert len(data["line_items"]) == 2

    def test_generate_calendar_double_billing_guard(self, client: TestClient, db_session: Session) -> None:
        """INV-TEST-010: Double-billing guard on generate-calendar."""
        cust = _make_customer(
            db_session,
            name="Fascinate",
            billing_model=BillingModel.HOURLY.value,
            invoice_prefix="",
        )

        sessions = [
            {"date": "2026-03-05", "description": "Session A", "hours": 1.0, "rate": 100.0},
        ]
        r1 = client.post("/api/invoices/generate-calendar", json={
            "customer_id": cust.id, "sessions": sessions,
        })
        assert r1.status_code == 201

        # Same session again should fail
        r2 = client.post("/api/invoices/generate-calendar", json={
            "customer_id": cust.id, "sessions": sessions,
        })
        assert r2.status_code == 422

    def test_generate_calendar_auto_increment(self, client: TestClient, db_session: Session) -> None:
        cust = _make_customer(
            db_session,
            billing_model=BillingModel.HOURLY.value,
            invoice_prefix="",
        )

        r1 = client.post("/api/invoices/generate-calendar", json={
            "customer_id": cust.id,
            "sessions": [{"date": "2026-03-05", "description": "S1", "hours": 1.0, "rate": 100.0}],
        })
        assert r1.json()["invoice_number"] == "202603-001"

        r2 = client.post("/api/invoices/generate-calendar", json={
            "customer_id": cust.id,
            "sessions": [{"date": "2026-03-12", "description": "S2", "hours": 1.0, "rate": 100.0}],
        })
        assert r2.json()["invoice_number"] == "202603-002"


# ---------------------------------------------------------------------------
# PDF / HTML
# ---------------------------------------------------------------------------


class TestPDFHTML:
    """INV-TEST-007: GET /api/invoices/{id}/pdf and /html"""

    def test_get_pdf(self, client: TestClient, db_session: Session) -> None:
        cust = _make_customer(db_session)
        inv = _make_invoice(
            db_session, cust.id,
            submitted_date="2026-03-01",
            due_date="2026-04-01",
        )

        r = client.get(f"/api/invoices/{inv.id}/pdf")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/pdf"
        assert r.content[:5] == b"%PDF-"

    def test_get_html(self, client: TestClient, db_session: Session) -> None:
        cust = _make_customer(db_session)
        inv = _make_invoice(db_session, cust.id)

        r = client.get(f"/api/invoices/{inv.id}/html")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

    def test_pdf_not_found(self, client: TestClient) -> None:
        r = client.get(f"/api/invoices/{uuid.uuid4()}/pdf")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Customer CRUD
# ---------------------------------------------------------------------------


class TestCustomerCRUD:
    """INV-TEST-008: Customer endpoints"""

    def test_list_customers_empty(self, client: TestClient) -> None:
        r = client.get("/api/customers")
        assert r.status_code == 200
        assert r.json() == []

    def test_create_customer(self, client: TestClient) -> None:
        r = client.post("/api/customers", json={
            "name": "New Customer",
            "billing_model": "hourly",
            "default_rate": 150.0,
            "payment_terms": "Net 14",
        })
        assert r.status_code == 201
        data = r.json()
        assert data["name"] == "New Customer"
        assert float(data["default_rate"]) == 150.0

    def test_patch_customer(self, client: TestClient, db_session: Session) -> None:
        cust = _make_customer(db_session, name="Old Name")

        r = client.patch(f"/api/customers/{cust.id}", json={"name": "New Name"})
        assert r.status_code == 200
        assert r.json()["name"] == "New Name"

    def test_patch_customer_not_found(self, client: TestClient) -> None:
        r = client.patch(f"/api/customers/{uuid.uuid4()}", json={"name": "X"})
        assert r.status_code == 404

    def test_list_after_create(self, client: TestClient) -> None:
        client.post("/api/customers", json={
            "name": "Customer One",
            "billing_model": "flat_rate",
        })
        r = client.get("/api/customers")
        assert r.status_code == 200
        assert len(r.json()) == 1


# ---------------------------------------------------------------------------
# Bulk confirm
# ---------------------------------------------------------------------------


class TestBulkConfirm:
    """INV-TEST-009: POST /api/transactions/bulk-confirm"""

    def test_bulk_confirm(self, client: TestClient, db_session: Session) -> None:
        tx1 = _make_transaction(db_session, description="Vendor A")
        tx2 = _make_transaction(db_session, description="Vendor B")

        r = client.post("/api/transactions/bulk-confirm", json={
            "ids": [tx1.id, tx2.id],
            "entity": "sparkry",
            "tax_category": "OFFICE_EXPENSE",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["confirmed"] == 2
        assert data["rules_created"] == 2

        # Verify transactions are confirmed
        db_session.expire_all()
        tx1_updated = db_session.get(Transaction, tx1.id)
        assert tx1_updated is not None
        assert tx1_updated.status == "confirmed"

    def test_bulk_confirm_creates_vendor_rules(self, client: TestClient, db_session: Session) -> None:
        tx = _make_transaction(db_session, description="UniqueVendor123")

        client.post("/api/transactions/bulk-confirm", json={
            "ids": [tx.id],
            "entity": "sparkry",
            "tax_category": "SUPPLIES",
        })

        rule = db_session.query(VendorRule).filter(
            VendorRule.vendor_pattern == "UniqueVendor123"
        ).first()
        assert rule is not None
        assert rule.entity == "sparkry"
        assert rule.tax_category == "SUPPLIES"

    def test_bulk_confirm_skips_missing_ids(self, client: TestClient, db_session: Session) -> None:
        tx = _make_transaction(db_session)

        r = client.post("/api/transactions/bulk-confirm", json={
            "ids": [tx.id, str(uuid.uuid4())],
            "entity": "sparkry",
            "tax_category": "OFFICE_EXPENSE",
        })
        assert r.status_code == 200
        assert r.json()["confirmed"] == 1

    def test_bulk_confirm_no_duplicate_rules(self, client: TestClient, db_session: Session) -> None:
        """Same vendor confirmed twice should increment examples, not create duplicate."""
        tx1 = _make_transaction(db_session, description="SameVendor")
        tx2 = _make_transaction(db_session, description="SameVendor")

        client.post("/api/transactions/bulk-confirm", json={
            "ids": [tx1.id],
            "entity": "sparkry",
            "tax_category": "OFFICE_EXPENSE",
        })
        client.post("/api/transactions/bulk-confirm", json={
            "ids": [tx2.id],
            "entity": "sparkry",
            "tax_category": "OFFICE_EXPENSE",
        })

        rules = db_session.query(VendorRule).filter(
            VendorRule.vendor_pattern == "SameVendor",
            VendorRule.entity == "sparkry",
        ).all()
        assert len(rules) == 1
        assert rules[0].examples == 2


# ---------------------------------------------------------------------------
# iCal upload
# ---------------------------------------------------------------------------


class TestICalUpload:
    """Test iCal upload endpoint error handling."""

    def test_ical_too_large(self, client: TestClient) -> None:
        # Create a file > 10MB
        large_content = b"X" * (10 * 1024 * 1024 + 1)
        r = client.post(
            "/api/invoices/ical-upload",
            files={"file": ("big.ics", large_content, "text/calendar")},
        )
        assert r.status_code == 413

    def test_ical_valid_file(self, client: TestClient, db_session: Session) -> None:
        """Valid .ics file with customer returns parsed result."""
        cust = _make_customer(db_session, name="Test Cal Customer")
        ical_content = b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR"
        r = client.post(
            f"/api/invoices/ical-upload?customer_id={cust.id}&start_date=2026-03-01&end_date=2026-03-31",
            files={"file": ("test.ics", ical_content, "text/calendar")},
        )
        assert r.status_code == 200
        data = r.json()
        assert "matched_sessions" in data
        assert "unmatched_events" in data
