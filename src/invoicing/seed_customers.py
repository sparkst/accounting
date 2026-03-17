"""Seed Customer rows and historical invoices for Sparkry LLC.

Two customers are seeded with deterministic UUIDs (uuid5 with NAMESPACE_DNS):
- "fascinate"       → How To Fascinate (hourly, $100/hr)
- "cardinal-health" → Cardinal Health, Inc. (flat_rate, $33,000/mo)

Two existing Cardinal Health invoices are seeded:
- CH20260131: $33,000, Jan 5–30 2026, submitted Feb 5 2026, status=paid
- CH20260228: $33,000, Feb 2–27 2026, submitted Mar 2 2026, status=sent

Idempotency rules:
- Customer rows are looked up by deterministic UUID on every call.
- Contact/config fields are updated on re-seed.
- default_rate is NOT updated if any invoice already exists for that customer.
- Invoices are looked up by invoice_number; existing rows are not re-inserted.
"""

from __future__ import annotations

import decimal
import logging
import uuid

from sqlalchemy.orm import Session

from src.models.enums import BillingModel, Entity, InvoiceStatus
from src.models.invoice import Customer, Invoice, InvoiceLineItem

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Deterministic UUIDs
# ---------------------------------------------------------------------------

_FASCINATE_ID = str(uuid.uuid5(uuid.NAMESPACE_DNS, "fascinate"))
_CARDINAL_HEALTH_ID = str(uuid.uuid5(uuid.NAMESPACE_DNS, "cardinal-health"))

# ---------------------------------------------------------------------------
# Customer definitions
# ---------------------------------------------------------------------------


def _upsert_fascinate(session: Session) -> Customer:
    """Create or update the How To Fascinate customer row."""
    customer = session.get(Customer, _FASCINATE_ID)

    # Determine whether any invoices already exist so we know if rate is locked.
    has_invoices = (
        session.query(Invoice)
        .filter(Invoice.customer_id == _FASCINATE_ID)
        .first()
        is not None
    )

    if customer is None:
        customer = Customer(id=_FASCINATE_ID)
        session.add(customer)
        logger.info("Seeding new customer: How To Fascinate (id=%s)", _FASCINATE_ID)
    else:
        logger.debug("Updating existing customer: How To Fascinate (id=%s)", _FASCINATE_ID)

    # Always update contact / config fields.
    customer.name = "How To Fascinate"
    customer.contact_name = "Ben"
    customer.billing_model = BillingModel.HOURLY.value
    customer.payment_terms = "Net 14"
    customer.invoice_prefix = ""
    customer.late_fee_pct = 0.10
    customer.calendar_patterns = ["Ben / Travis", "Fascinate OS", "Fascinate"]
    customer.calendar_exclusions = ["Book with Ben"]
    customer.active = True

    # Only set default_rate when no invoices exist yet.
    if not has_invoices:
        customer.default_rate = decimal.Decimal("100.00")
    else:
        logger.debug(
            "Skipping default_rate update for How To Fascinate — invoices already exist."
        )

    return customer


def _upsert_cardinal_health(session: Session) -> Customer:
    """Create or update the Cardinal Health customer row."""
    customer = session.get(Customer, _CARDINAL_HEALTH_ID)

    has_invoices = (
        session.query(Invoice)
        .filter(Invoice.customer_id == _CARDINAL_HEALTH_ID)
        .first()
        is not None
    )

    if customer is None:
        customer = Customer(id=_CARDINAL_HEALTH_ID)
        session.add(customer)
        logger.info("Seeding new customer: Cardinal Health (id=%s)", _CARDINAL_HEALTH_ID)
    else:
        logger.debug(
            "Updating existing customer: Cardinal Health (id=%s)", _CARDINAL_HEALTH_ID
        )

    customer.name = "Cardinal Health, Inc."
    customer.contact_name = "Charelle Lewis"
    customer.contact_email = "charelle.lewis@cardinalhealth.com"
    customer.billing_model = BillingModel.FLAT_RATE.value
    customer.payment_terms = "Net 90"
    customer.invoice_prefix = "CH"
    customer.late_fee_pct = 0.0
    customer.po_number = "4700158965"
    customer.contract_start_date = "2026-01-05"
    customer.active = True
    customer.sap_config = {
        "ship_to_contact": {
            "name": "Adeola Ogundipe",
            "email": "adeola.ogundipe@cardinalhealth.com",
        },
        "tax_id": "39-4105886",
        "classification": "111811-L3",
    }

    if not has_invoices:
        customer.default_rate = decimal.Decimal("33000.00")
    else:
        logger.debug(
            "Skipping default_rate update for Cardinal Health — invoices already exist."
        )

    return customer


# ---------------------------------------------------------------------------
# Invoice seeding helpers
# ---------------------------------------------------------------------------


def _seed_invoice(
    session: Session,
    *,
    invoice_number: str,
    customer_id: str,
    subtotal: decimal.Decimal,
    service_period_start: str,
    service_period_end: str,
    submitted_date: str,
    status: InvoiceStatus,
    paid_date: str | None,
    line_description: str,
    project: str = "AI Product Engineering Coaching",
) -> bool:
    """Insert an invoice + one line item if the invoice_number does not yet exist.

    Returns True if a new row was inserted, False if it already existed.
    """
    existing = (
        session.query(Invoice)
        .filter(Invoice.invoice_number == invoice_number)
        .first()
    )
    if existing is not None:
        logger.debug("Invoice %s already exists — skipping.", invoice_number)
        return False

    invoice_id = str(uuid.uuid4())

    invoice = Invoice(
        id=invoice_id,
        invoice_number=invoice_number,
        customer_id=customer_id,
        entity=Entity.SPARKRY.value,
        project=project,
        submitted_date=submitted_date,
        service_period_start=service_period_start,
        service_period_end=service_period_end,
        subtotal=subtotal,
        adjustments=decimal.Decimal("0.00"),
        tax=decimal.Decimal("0.00"),
        total=subtotal,
        status=status.value,
        paid_date=paid_date,
        payment_terms="Net 90",
        payment_method="ACH",
        po_number="4700158965",
        late_fee_pct=0.0,
    )
    session.add(invoice)

    line_item = InvoiceLineItem(
        id=str(uuid.uuid4()),
        invoice_id=invoice_id,
        description=line_description,
        quantity=decimal.Decimal("1.0000"),
        unit_price=subtotal,
        total_price=subtotal,
        sort_order=0,
    )
    session.add(line_item)

    logger.info("Seeded invoice %s (status=%s)", invoice_number, status.value)
    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def seed_customers(session: Session) -> dict[str, int]:
    """Seed customers and historical invoices.

    Idempotent — safe to call on every startup.

    Returns:
        Dict with keys "customers_inserted", "customers_updated", "invoices_inserted".
    """
    customers_inserted = 0
    customers_updated = 0
    invoices_inserted = 0

    # ── How To Fascinate ──────────────────────────────────────────────────────
    fascinate_is_new = session.get(Customer, _FASCINATE_ID) is None
    _upsert_fascinate(session)
    if fascinate_is_new:
        customers_inserted += 1
    else:
        customers_updated += 1

    # ── Cardinal Health ───────────────────────────────────────────────────────
    cardinal_is_new = session.get(Customer, _CARDINAL_HEALTH_ID) is None
    _upsert_cardinal_health(session)
    if cardinal_is_new:
        customers_inserted += 1
    else:
        customers_updated += 1

    # Flush so FK constraints are satisfied before inserting invoices.
    session.flush()

    # ── Existing Cardinal Health invoices ─────────────────────────────────────
    _33k = decimal.Decimal("33000.00")

    if _seed_invoice(
        session,
        invoice_number="CH20260131",
        customer_id=_CARDINAL_HEALTH_ID,
        subtotal=_33k,
        service_period_start="2026-01-05",
        service_period_end="2026-01-30",
        submitted_date="2026-02-05",
        status=InvoiceStatus.PAID,
        paid_date="2026-05-06",  # 90 days after submission
        line_description="AI Product Engineering Coaching Month 1",
    ):
        invoices_inserted += 1

    if _seed_invoice(
        session,
        invoice_number="CH20260228",
        customer_id=_CARDINAL_HEALTH_ID,
        subtotal=_33k,
        service_period_start="2026-02-02",
        service_period_end="2026-02-27",
        submitted_date="2026-03-02",
        status=InvoiceStatus.SENT,
        paid_date=None,
        line_description="AI Product Engineering Coaching Month 2",
    ):
        invoices_inserted += 1

    session.commit()

    logger.info(
        "seed_customers complete: %d inserted, %d updated, %d invoices inserted.",
        customers_inserted,
        customers_updated,
        invoices_inserted,
    )

    return {
        "customers_inserted": customers_inserted,
        "customers_updated": customers_updated,
        "invoices_inserted": invoices_inserted,
    }
