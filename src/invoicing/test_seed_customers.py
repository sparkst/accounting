"""Tests for src/invoicing/seed_customers.py.

REQ-INV-010: Customer seed is idempotent and correct.
"""

from __future__ import annotations

import decimal
import uuid

import pytest
from sqlalchemy.orm import sessionmaker

from src.db.connection import _build_engine
from src.invoicing.seed_customers import (
    _CARDINAL_HEALTH_ID,
    _FASCINATE_ID,
    seed_customers,
)
from src.models.base import Base
from src.models.enums import BillingModel, InvoiceStatus
from src.models.invoice import Customer, Invoice, InvoiceLineItem

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session():
    """In-memory SQLite session, torn down after each test."""
    engine = _build_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with Session() as session:
        yield session
    Base.metadata.drop_all(bind=engine)


# ---------------------------------------------------------------------------
# Deterministic UUID tests
# ---------------------------------------------------------------------------


def test_fascinate_uuid_is_deterministic():
    expected = str(uuid.uuid5(uuid.NAMESPACE_DNS, "fascinate"))
    assert expected == _FASCINATE_ID


def test_cardinal_health_uuid_is_deterministic():
    expected = str(uuid.uuid5(uuid.NAMESPACE_DNS, "cardinal-health"))
    assert expected == _CARDINAL_HEALTH_ID


# ---------------------------------------------------------------------------
# How To Fascinate customer
# ---------------------------------------------------------------------------


def test_fascinate_customer_seeded(db_session):
    seed_customers(db_session)
    customer = db_session.get(Customer, _FASCINATE_ID)
    assert customer is not None
    assert customer.name == "How To Fascinate"
    assert customer.contact_name == "Ben"
    assert customer.billing_model == BillingModel.HOURLY.value
    assert customer.default_rate == decimal.Decimal("100.00")
    assert customer.payment_terms == "Net 14"
    assert customer.invoice_prefix == ""
    assert customer.late_fee_pct == pytest.approx(0.10)
    assert customer.calendar_patterns == ["Ben / Travis", "Fascinate OS", "Fascinate"]
    assert customer.calendar_exclusions == ["Book with Ben"]
    assert customer.active is True


# ---------------------------------------------------------------------------
# Cardinal Health customer
# ---------------------------------------------------------------------------


def test_cardinal_health_customer_seeded(db_session):
    seed_customers(db_session)
    customer = db_session.get(Customer, _CARDINAL_HEALTH_ID)
    assert customer is not None
    assert customer.name == "Cardinal Health, Inc."
    assert customer.contact_name == "Charelle Lewis"
    assert customer.contact_email == "charelle.lewis@cardinalhealth.com"
    assert customer.billing_model == BillingModel.FLAT_RATE.value
    assert customer.default_rate == decimal.Decimal("33000.00")
    assert customer.payment_terms == "Net 90"
    assert customer.invoice_prefix == "CH"
    assert customer.po_number == "4700158965"
    assert customer.contract_start_date == "2026-01-05"
    assert customer.active is True


def test_cardinal_health_sap_config(db_session):
    seed_customers(db_session)
    customer = db_session.get(Customer, _CARDINAL_HEALTH_ID)
    assert customer.sap_config is not None
    assert customer.sap_config["ship_to_contact"]["name"] == "Adeola Ogundipe"
    assert (
        customer.sap_config["ship_to_contact"]["email"]
        == "adeola.ogundipe@cardinalhealth.com"
    )
    assert customer.sap_config["tax_id"] == "39-4105886"
    assert customer.sap_config["classification"] == "111811-L3"


# ---------------------------------------------------------------------------
# Invoice seeding
# ---------------------------------------------------------------------------


def test_ch20260131_seeded(db_session):
    seed_customers(db_session)
    invoice = (
        db_session.query(Invoice)
        .filter(Invoice.invoice_number == "CH20260131")
        .first()
    )
    assert invoice is not None
    assert invoice.customer_id == _CARDINAL_HEALTH_ID
    assert invoice.total == decimal.Decimal("33000.00")
    assert invoice.subtotal == decimal.Decimal("33000.00")
    assert invoice.service_period_start == "2026-01-05"
    assert invoice.service_period_end == "2026-01-30"
    assert invoice.submitted_date == "2026-02-05"
    assert invoice.status == InvoiceStatus.PAID.value
    assert invoice.paid_date is not None


def test_ch20260228_seeded(db_session):
    seed_customers(db_session)
    invoice = (
        db_session.query(Invoice)
        .filter(Invoice.invoice_number == "CH20260228")
        .first()
    )
    assert invoice is not None
    assert invoice.customer_id == _CARDINAL_HEALTH_ID
    assert invoice.total == decimal.Decimal("33000.00")
    assert invoice.service_period_start == "2026-02-02"
    assert invoice.service_period_end == "2026-02-27"
    assert invoice.submitted_date == "2026-03-02"
    assert invoice.status == InvoiceStatus.SENT.value
    assert invoice.paid_date is None


def test_ch_invoices_have_line_items(db_session):
    seed_customers(db_session)
    for invoice_number, expected_description in [
        ("CH20260131", "AI Product Engineering Coaching Month 1"),
        ("CH20260228", "AI Product Engineering Coaching Month 2"),
    ]:
        invoice = (
            db_session.query(Invoice)
            .filter(Invoice.invoice_number == invoice_number)
            .first()
        )
        assert invoice is not None
        line_items = (
            db_session.query(InvoiceLineItem)
            .filter(InvoiceLineItem.invoice_id == invoice.id)
            .all()
        )
        assert len(line_items) == 1, f"{invoice_number} should have exactly one line item"
        item = line_items[0]
        assert item.description == expected_description
        assert item.quantity == decimal.Decimal("1.0000")
        assert item.unit_price == decimal.Decimal("33000.00")
        assert item.total_price == decimal.Decimal("33000.00")


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_idempotent_double_seed_no_duplicate_customers(db_session):
    seed_customers(db_session)
    seed_customers(db_session)
    customers = db_session.query(Customer).all()
    ids = [c.id for c in customers]
    assert ids.count(_FASCINATE_ID) == 1
    assert ids.count(_CARDINAL_HEALTH_ID) == 1


def test_idempotent_double_seed_no_duplicate_invoices(db_session):
    seed_customers(db_session)
    seed_customers(db_session)
    for invoice_number in ("CH20260131", "CH20260228"):
        count = (
            db_session.query(Invoice)
            .filter(Invoice.invoice_number == invoice_number)
            .count()
        )
        assert count == 1, f"Expected exactly 1 row for {invoice_number}, got {count}"


def test_idempotent_second_call_returns_zero_inserted(db_session):
    seed_customers(db_session)
    second = seed_customers(db_session)
    assert second["customers_inserted"] == 0
    assert second["invoices_inserted"] == 0


def test_idempotent_second_call_updates_customers(db_session):
    seed_customers(db_session)
    counts = seed_customers(db_session)
    assert counts["customers_updated"] == 2


# ---------------------------------------------------------------------------
# Re-seed preserves default_rate when invoices exist
# ---------------------------------------------------------------------------


def test_reseed_does_not_change_rate_when_invoices_exist(db_session):
    """default_rate must not be overwritten once invoices exist for the customer."""
    seed_customers(db_session)

    # Manually change the rate.
    customer = db_session.get(Customer, _CARDINAL_HEALTH_ID)
    customer.default_rate = decimal.Decimal("40000.00")
    db_session.commit()

    # Re-seed — should NOT revert the rate since invoices exist.
    seed_customers(db_session)
    db_session.expire_all()
    customer = db_session.get(Customer, _CARDINAL_HEALTH_ID)
    assert customer.default_rate == decimal.Decimal("40000.00")


def test_reseed_does_change_rate_when_no_invoices_exist(db_session):
    """default_rate IS updated when the customer has no invoices yet (Fascinate case)."""
    # Seed only the customer rows, skipping invoice creation by calling _upsert directly.
    from src.invoicing.seed_customers import _upsert_fascinate

    _upsert_fascinate(db_session)
    db_session.commit()

    # Manually change the rate.
    customer = db_session.get(Customer, _FASCINATE_ID)
    customer.default_rate = decimal.Decimal("50.00")
    db_session.commit()

    # Full seed — no invoices for Fascinate, so rate should be restored.
    seed_customers(db_session)
    db_session.expire_all()
    customer = db_session.get(Customer, _FASCINATE_ID)
    assert customer.default_rate == decimal.Decimal("100.00")


# ---------------------------------------------------------------------------
# Return value structure
# ---------------------------------------------------------------------------


def test_first_seed_returns_two_customers_inserted(db_session):
    counts = seed_customers(db_session)
    assert counts["customers_inserted"] == 2
    assert counts["invoices_inserted"] == 2
