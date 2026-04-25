"""Tests for src/invoicing/generator.py.

REQ-INV-002: Calendar-based invoice generation with double-billing guard.
REQ-INV-003: Flat-rate invoice generation with duplicate guard.

Uses an in-memory SQLite database so tests are isolated and fast.
"""

from __future__ import annotations

import decimal
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.db.connection import _configure_sqlite
from src.invoicing.generator import (
    SessionInput,
    _business_days_in_month,
    _month_ordinal,
    _next_calendar_invoice_number,
    generate_calendar_invoice,
    generate_flat_invoice,
)
from src.models.base import Base
from src.models.enums import BillingModel, InvoiceStatus
from src.models.invoice import Customer, Invoice, InvoiceLineItem

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine():
    """In-memory SQLite engine with all tables created."""
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    from sqlalchemy import event as sa_event
    sa_event.listen(eng, "connect", _configure_sqlite)
    Base.metadata.create_all(bind=eng)
    yield eng
    Base.metadata.drop_all(bind=eng)
    eng.dispose()


@pytest.fixture()
def db(engine):
    """Session scoped to a single test; rolled back on teardown."""
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = factory()
    yield session
    session.rollback()
    session.close()


@pytest.fixture()
def hourly_customer(db: Session) -> Customer:
    customer = Customer(
        name="Fascinate OS",
        billing_model=BillingModel.HOURLY.value,
        default_rate=decimal.Decimal("100.00"),
        payment_terms="Net 14",
        invoice_prefix="",
        late_fee_pct=0.0,
        contract_start_date="2026-01-01",
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


@pytest.fixture()
def flat_customer(db: Session) -> Customer:
    customer = Customer(
        name="Cardinal Health",
        billing_model=BillingModel.FLAT_RATE.value,
        default_rate=decimal.Decimal("33000.00"),
        payment_terms="Net 90",
        invoice_prefix="CH",
        late_fee_pct=0.0,
        po_number="4700158965",
        contract_start_date="2026-01-01",
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


def _sessions(*dates_descs: tuple[str, str, float]) -> list[SessionInput]:
    """Helper: build SessionInput list from (date, desc, hours) tuples."""
    return [SessionInput(date=d, description=desc, duration_hours=h) for d, desc, h in dates_descs]


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------


class TestBusinessDaysInMonth:
    def test_march_2026(self):
        first, last = _business_days_in_month(2026, 3)
        assert first == date(2026, 3, 2)   # Mar 1 is Sunday
        assert last == date(2026, 3, 31)   # Mar 31 is Tuesday

    def test_january_2026(self):
        first, last = _business_days_in_month(2026, 1)
        assert first == date(2026, 1, 1)   # Jan 1 is Thursday
        assert last == date(2026, 1, 30)   # Jan 31 is Saturday

    def test_result_are_weekdays(self):
        for month in range(1, 13):
            first, last = _business_days_in_month(2026, month)
            assert first.weekday() < 5, f"{first} is a weekend"
            assert last.weekday() < 5, f"{last} is a weekend"


class TestMonthOrdinal:
    def test_first_month(self):
        assert _month_ordinal("2026-01", "2026-01") == 1

    def test_second_month(self):
        assert _month_ordinal("2026-01", "2026-02") == 2

    def test_cross_year(self):
        assert _month_ordinal("2025-12", "2026-01") == 2

    def test_with_full_date_string(self):
        assert _month_ordinal("2026-01-15", "2026-03") == 3


class TestNextCalendarInvoiceNumber:
    def test_first_invoice_in_month(self, db: Session):
        num = _next_calendar_invoice_number(db, 2026, 3)
        assert num == "202603-001"

    def test_second_invoice_in_month(self, db: Session, hourly_customer: Customer):
        # Insert a dummy invoice with the first number
        inv = Invoice(
            invoice_number="202603-001",
            customer_id=hourly_customer.id,
            entity="sparkry",
            subtotal=decimal.Decimal("0"),
            adjustments=decimal.Decimal("0"),
            tax=decimal.Decimal("0"),
            total=decimal.Decimal("0"),
            status=InvoiceStatus.DRAFT.value,
        )
        db.add(inv)
        db.commit()

        num = _next_calendar_invoice_number(db, 2026, 3)
        assert num == "202603-002"

    def test_different_month_not_counted(self, db: Session, hourly_customer: Customer):
        inv = Invoice(
            invoice_number="202602-001",
            customer_id=hourly_customer.id,
            entity="sparkry",
            subtotal=decimal.Decimal("0"),
            adjustments=decimal.Decimal("0"),
            tax=decimal.Decimal("0"),
            total=decimal.Decimal("0"),
            status=InvoiceStatus.DRAFT.value,
        )
        db.add(inv)
        db.commit()

        # March should still be 001
        num = _next_calendar_invoice_number(db, 2026, 3)
        assert num == "202603-001"


# ---------------------------------------------------------------------------
# generate_calendar_invoice tests
# ---------------------------------------------------------------------------


class TestGenerateCalendarInvoice:
    def test_returns_none_for_empty_sessions(self, db: Session, hourly_customer: Customer):
        result = generate_calendar_invoice(db, hourly_customer, [])
        assert result is None

    def test_creates_draft_invoice(self, db: Session, hourly_customer: Customer):
        sessions = _sessions(("2026-03-05", "Fascinate OS sync", 1.0))
        invoice = generate_calendar_invoice(db, hourly_customer, sessions)
        db.commit()
        assert invoice is not None
        assert invoice.status == InvoiceStatus.DRAFT.value

    def test_invoice_number_format(self, db: Session, hourly_customer: Customer):
        sessions = _sessions(("2026-03-05", "Fascinate OS sync", 1.0))
        invoice = generate_calendar_invoice(db, hourly_customer, sessions)
        db.commit()
        assert invoice is not None
        assert invoice.invoice_number == "202603-001"

    def test_invoice_number_increments(self, db: Session, hourly_customer: Customer):
        sessions1 = _sessions(("2026-03-05", "Session A", 1.0))
        inv1 = generate_calendar_invoice(db, hourly_customer, sessions1)
        db.commit()

        sessions2 = _sessions(("2026-03-10", "Session B", 2.0))
        inv2 = generate_calendar_invoice(db, hourly_customer, sessions2)
        db.commit()

        assert inv1 is not None and inv2 is not None
        assert inv1.invoice_number == "202603-001"
        assert inv2.invoice_number == "202603-002"

    def test_no_header_line_item(self, db: Session, hourly_customer: Customer):
        """Section headers are rendered by the PDF/HTML layer, not stored as line items."""
        sessions = _sessions(
            ("2026-03-05", "Sync A", 1.0),
            ("2026-03-12", "Sync B", 2.0),
        )
        invoice = generate_calendar_invoice(db, hourly_customer, sessions)
        db.commit()
        assert invoice is not None

        line_items = (
            db.query(InvoiceLineItem)
            .filter(InvoiceLineItem.invoice_id == invoice.id)
            .order_by(InvoiceLineItem.sort_order)
            .all()
        )
        assert len(line_items) == 2
        assert line_items[0].description == "Sync A"
        assert line_items[0].sort_order == 0

    def test_one_line_item_per_session(self, db: Session, hourly_customer: Customer):
        sessions = _sessions(
            ("2026-03-05", "Sync A", 1.0),
            ("2026-03-12", "Sync B", 2.0),
            ("2026-03-19", "Sync C", 1.5),
        )
        invoice = generate_calendar_invoice(db, hourly_customer, sessions)
        db.commit()
        assert invoice is not None

        line_items = (
            db.query(InvoiceLineItem)
            .filter(InvoiceLineItem.invoice_id == invoice.id)
            .order_by(InvoiceLineItem.sort_order)
            .all()
        )
        assert len(line_items) == 3

    def test_subtotal_correctly_summed(self, db: Session, hourly_customer: Customer):
        # 1.0 hr @ $100 + 2.0 hrs @ $100 = $300
        sessions = _sessions(
            ("2026-03-05", "Sync A", 1.0),
            ("2026-03-12", "Sync B", 2.0),
        )
        invoice = generate_calendar_invoice(db, hourly_customer, sessions)
        db.commit()
        assert invoice is not None
        assert invoice.subtotal == decimal.Decimal("300.00")
        assert invoice.total == decimal.Decimal("300.00")

    def test_rate_override(self, db: Session, hourly_customer: Customer):
        sessions = _sessions(("2026-03-05", "Sync", 2.0))
        invoice = generate_calendar_invoice(db, hourly_customer, sessions, rate=150.0)
        db.commit()
        assert invoice is not None
        assert invoice.subtotal == decimal.Decimal("300.00")

    def test_service_period_covers_session_range(self, db: Session, hourly_customer: Customer):
        sessions = _sessions(
            ("2026-03-05", "Sync A", 1.0),
            ("2026-03-19", "Sync B", 1.0),
        )
        invoice = generate_calendar_invoice(db, hourly_customer, sessions)
        db.commit()
        assert invoice is not None
        assert invoice.service_period_start == "2026-03-05"
        assert invoice.service_period_end == "2026-03-19"

    def test_customer_last_invoiced_date_updated(self, db: Session, hourly_customer: Customer):
        sessions = _sessions(
            ("2026-03-05", "Sync A", 1.0),
            ("2026-03-19", "Sync B", 1.0),
        )
        generate_calendar_invoice(db, hourly_customer, sessions)
        db.commit()
        db.refresh(hourly_customer)
        assert hourly_customer.last_invoiced_date == "2026-03-19"

    def test_audit_event_created(self, db: Session, hourly_customer: Customer):
        from src.models.audit_event import AuditEvent

        sessions = _sessions(("2026-03-05", "Sync", 1.0))
        invoice = generate_calendar_invoice(db, hourly_customer, sessions)
        db.commit()
        assert invoice is not None

        events = (
            db.query(AuditEvent)
            .filter(AuditEvent.transaction_id == invoice.id)
            .all()
        )
        assert len(events) >= 1
        status_event = next((e for e in events if e.field_changed == "status"), None)
        assert status_event is not None
        assert status_event.old_value is None
        assert status_event.new_value == InvoiceStatus.DRAFT.value

    def test_double_billing_guard_raises(self, db: Session, hourly_customer: Customer):
        sessions = _sessions(("2026-03-05", "Sync A", 1.0))
        generate_calendar_invoice(db, hourly_customer, sessions)
        db.commit()

        # Try to bill the same session again
        with pytest.raises(ValueError, match="Double-billing guard"):
            generate_calendar_invoice(db, hourly_customer, sessions)

    def test_double_billing_guard_skips_voided_invoices(self, db: Session, hourly_customer: Customer):
        sessions = _sessions(("2026-03-05", "Sync A", 1.0))
        first_invoice = generate_calendar_invoice(db, hourly_customer, sessions)
        db.commit()
        assert first_invoice is not None

        # Void the first invoice
        first_invoice.status = InvoiceStatus.VOID.value
        db.commit()

        # Now the same session should be billable again
        second_invoice = generate_calendar_invoice(db, hourly_customer, sessions)
        db.commit()
        assert second_invoice is not None
        assert second_invoice.invoice_number == "202603-002"

    def test_double_billing_guard_partial_session_raises(self, db: Session, hourly_customer: Customer):
        """If any session in the batch is already billed, the whole call raises."""
        sessions_first = _sessions(("2026-03-05", "Sync A", 1.0))
        generate_calendar_invoice(db, hourly_customer, sessions_first)
        db.commit()

        sessions_mixed = _sessions(
            ("2026-03-05", "Sync A", 1.0),  # already billed
            ("2026-03-12", "Sync B", 2.0),  # new
        )
        with pytest.raises(ValueError, match="Double-billing guard"):
            generate_calendar_invoice(db, hourly_customer, sessions_mixed)

    def test_line_items_sorted_chronologically(self, db: Session, hourly_customer: Customer):
        sessions = _sessions(
            ("2026-03-19", "Sync C", 1.0),
            ("2026-03-05", "Sync A", 1.0),
            ("2026-03-12", "Sync B", 1.0),
        )
        invoice = generate_calendar_invoice(db, hourly_customer, sessions)
        db.commit()
        assert invoice is not None

        items = (
            db.query(InvoiceLineItem)
            .filter(InvoiceLineItem.invoice_id == invoice.id)
            .order_by(InvoiceLineItem.sort_order)
            .all()
        )
        # Skip header (sort_order=0), rest should be chronological
        session_items = [i for i in items if i.sort_order > 0]
        dates = [i.date for i in session_items]
        assert dates == sorted(dates)

    def test_entity_is_sparkry(self, db: Session, hourly_customer: Customer):
        sessions = _sessions(("2026-03-05", "Sync", 1.0))
        invoice = generate_calendar_invoice(db, hourly_customer, sessions)
        db.commit()
        assert invoice is not None
        assert invoice.entity == "sparkry"

    def test_payment_terms_from_customer(self, db: Session, hourly_customer: Customer):
        sessions = _sessions(("2026-03-05", "Sync", 1.0))
        invoice = generate_calendar_invoice(db, hourly_customer, sessions)
        db.commit()
        assert invoice is not None
        assert invoice.payment_terms == "Net 14"


# ---------------------------------------------------------------------------
# generate_flat_invoice tests
# ---------------------------------------------------------------------------


class TestGenerateFlatInvoice:
    def test_creates_draft_invoice(self, db: Session, flat_customer: Customer):
        invoice = generate_flat_invoice(db, flat_customer, 2026, 3)
        db.commit()
        assert invoice.status == InvoiceStatus.DRAFT.value

    def test_invoice_number_format(self, db: Session, flat_customer: Customer):
        # CH + YYYYMMDD (last business day of March 2026)
        invoice = generate_flat_invoice(db, flat_customer, 2026, 3)
        db.commit()
        # Last business day of March 2026: March 31 (Tuesday)
        assert invoice.invoice_number == "CH20260331"

    def test_invoice_number_without_prefix(self, db: Session, flat_customer: Customer):
        flat_customer.invoice_prefix = ""
        db.commit()
        invoice = generate_flat_invoice(db, flat_customer, 2026, 3)
        db.commit()
        assert invoice.invoice_number == "20260331"

    def test_service_period_is_business_days(self, db: Session, flat_customer: Customer):
        invoice = generate_flat_invoice(db, flat_customer, 2026, 3)
        db.commit()
        # March 2026: first biz = Mar 2 (Mon), last biz = Mar 31 (Tue)
        assert invoice.service_period_start == "2026-03-02"
        assert invoice.service_period_end == "2026-03-31"

    def test_amount_from_customer_default_rate(self, db: Session, flat_customer: Customer):
        invoice = generate_flat_invoice(db, flat_customer, 2026, 3)
        db.commit()
        assert invoice.subtotal == decimal.Decimal("33000.00")
        assert invoice.total == decimal.Decimal("33000.00")

    def test_month_ordinal_in_description(self, db: Session, flat_customer: Customer):
        # contract_start_date = 2026-01, billing March 2026 = Month 3
        invoice = generate_flat_invoice(db, flat_customer, 2026, 3)
        db.commit()

        line_items = (
            db.query(InvoiceLineItem)
            .filter(InvoiceLineItem.invoice_id == invoice.id)
            .all()
        )
        assert len(line_items) == 1
        assert "Month 3" in line_items[0].description

    def test_month_ordinal_month_one(self, db: Session, flat_customer: Customer):
        invoice = generate_flat_invoice(db, flat_customer, 2026, 1)
        db.commit()
        line_items = (
            db.query(InvoiceLineItem)
            .filter(InvoiceLineItem.invoice_id == invoice.id)
            .all()
        )
        assert "Month 1" in line_items[0].description

    def test_line_item_description_prefix(self, db: Session, flat_customer: Customer):
        invoice = generate_flat_invoice(db, flat_customer, 2026, 3)
        db.commit()
        line_items = (
            db.query(InvoiceLineItem)
            .filter(InvoiceLineItem.invoice_id == invoice.id)
            .all()
        )
        assert "AI Product Engineering Coaching" in line_items[0].description

    def test_po_number_from_customer(self, db: Session, flat_customer: Customer):
        invoice = generate_flat_invoice(db, flat_customer, 2026, 3)
        db.commit()
        assert invoice.po_number == "4700158965"

    def test_payment_terms_from_customer(self, db: Session, flat_customer: Customer):
        invoice = generate_flat_invoice(db, flat_customer, 2026, 3)
        db.commit()
        assert invoice.payment_terms == "Net 90"

    def test_entity_is_sparkry(self, db: Session, flat_customer: Customer):
        invoice = generate_flat_invoice(db, flat_customer, 2026, 3)
        db.commit()
        assert invoice.entity == "sparkry"

    def test_audit_event_created(self, db: Session, flat_customer: Customer):
        from src.models.audit_event import AuditEvent

        invoice = generate_flat_invoice(db, flat_customer, 2026, 3)
        db.commit()

        events = (
            db.query(AuditEvent)
            .filter(AuditEvent.transaction_id == invoice.id)
            .all()
        )
        assert len(events) >= 1
        status_event = next((e for e in events if e.field_changed == "status"), None)
        assert status_event is not None
        assert status_event.old_value is None
        assert status_event.new_value == InvoiceStatus.DRAFT.value

    def test_duplicate_guard_same_period_raises(self, db: Session, flat_customer: Customer):
        generate_flat_invoice(db, flat_customer, 2026, 3)
        db.commit()

        with pytest.raises(ValueError, match="Invoice already exists"):
            generate_flat_invoice(db, flat_customer, 2026, 3)

    def test_duplicate_guard_different_month_ok(self, db: Session, flat_customer: Customer):
        inv1 = generate_flat_invoice(db, flat_customer, 2026, 2)
        db.commit()
        inv2 = generate_flat_invoice(db, flat_customer, 2026, 3)
        db.commit()
        assert inv1.invoice_number != inv2.invoice_number

    def test_duplicate_guard_different_customer_ok(self, db: Session, flat_customer: Customer):
        other = Customer(
            name="Other Corp",
            billing_model=BillingModel.FLAT_RATE.value,
            default_rate=decimal.Decimal("5000.00"),
            invoice_prefix="OC",
            late_fee_pct=0.0,
            contract_start_date="2026-01-01",
        )
        db.add(other)
        db.commit()
        db.refresh(other)

        inv1 = generate_flat_invoice(db, flat_customer, 2026, 3)
        db.commit()
        inv2 = generate_flat_invoice(db, other, 2026, 3)
        db.commit()
        assert inv1.id != inv2.id

    def test_voided_invoice_keeps_number(self, db: Session, flat_customer: Customer):
        """Voided invoices retain their number (not deleted)."""
        invoice = generate_flat_invoice(db, flat_customer, 2026, 3)
        db.commit()
        original_number = invoice.invoice_number

        invoice.status = InvoiceStatus.VOID.value
        db.commit()
        db.refresh(invoice)

        assert invoice.invoice_number == original_number
        assert invoice.status == InvoiceStatus.VOID.value

    def test_voided_invoice_blocks_duplicate(self, db: Session, flat_customer: Customer):
        """Even a voided invoice blocks regeneration for same period (no re-issue without a new number)."""
        invoice = generate_flat_invoice(db, flat_customer, 2026, 3)
        db.commit()
        invoice.status = InvoiceStatus.VOID.value
        db.commit()

        # The duplicate guard checks service_period_start regardless of status
        with pytest.raises(ValueError, match="Invoice already exists"):
            generate_flat_invoice(db, flat_customer, 2026, 3)

    def test_customer_last_invoiced_date_updated(self, db: Session, flat_customer: Customer):
        generate_flat_invoice(db, flat_customer, 2026, 3)
        db.commit()
        db.refresh(flat_customer)
        # Last business day of March 2026 is March 31
        assert flat_customer.last_invoiced_date == "2026-03-31"

    def test_no_contract_start_ordinal_defaults_to_one(self, db: Session, flat_customer: Customer):
        flat_customer.contract_start_date = None
        db.commit()

        invoice = generate_flat_invoice(db, flat_customer, 2026, 3)
        db.commit()

        line_items = (
            db.query(InvoiceLineItem)
            .filter(InvoiceLineItem.invoice_id == invoice.id)
            .all()
        )
        assert "Month 1" in line_items[0].description
