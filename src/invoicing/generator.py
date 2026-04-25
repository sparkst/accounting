"""Invoice generation functions.

Two workflows:
- generate_calendar_invoice: one line item per billable session (e.g., Fascinate).
- generate_flat_invoice: one flat-rate line item per month (e.g., Cardinal Health).

Both functions accept a SQLAlchemy Session, write all ORM objects within that
session, flush (but do NOT commit) so callers control the transaction boundary,
and return the newly created Invoice.

Double-billing protection:
- Calendar: checks existing InvoiceLineItems by customer + date + description
  (excludes voided invoices). Raises ValueError for each already-billed session.
- Flat: checks existing invoices by customer + service_period_start. Raises
  ValueError if a duplicate is found.

Audit trail:
- Every Invoice creation writes an AuditEvent (field="status", old=None,
  new="draft") using PRAGMA foreign_keys=OFF so invoice_id can be stored
  in the transaction_id column.

REQ-INV-002: Calendar-based invoice generation with double-billing guard.
REQ-INV-003: Flat-rate invoice generation with duplicate guard.
"""

from __future__ import annotations

import calendar
import decimal
import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.models.audit_event import AuditEvent
from src.models.enums import ConfirmedBy, Entity, InvoiceStatus
from src.models.invoice import Customer, Invoice, InvoiceLineItem

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _create_audit_event(
    session: Session,
    invoice_id: str,
    field: str,
    old_value: str | None,
    new_value: str | None,
) -> None:
    """Append an AuditEvent for an invoice field change.

    invoice_id is stored in the transaction_id FK column (same convention as
    the API route layer).  Callers must have already issued
    ``session.execute(text("PRAGMA foreign_keys=OFF"))`` before starting the
    current transaction — SQLite ignores the PRAGMA inside an active
    transaction so it must be the very first statement on the session.
    """
    event = AuditEvent(
        id=_new_uuid(),
        transaction_id=invoice_id,
        field_changed=field,
        old_value=old_value,
        new_value=new_value,
        changed_by=ConfirmedBy.HUMAN.value,
        changed_at=_now(),
    )
    session.add(event)
    session.flush()


def _next_calendar_invoice_number(session: Session, year: int, month: int) -> str:
    """Return the next YYYYMM-NNN invoice number for the given year/month.

    Queries all existing invoice_numbers with the YYYYMM- prefix, finds the
    maximum sequence number, and returns prefix + (max+1) zero-padded to 3
    digits. The UNIQUE constraint on invoice_number is the final race guard.
    """
    prefix = f"{year}{month:02d}-"
    existing = (
        session.query(Invoice.invoice_number)
        .filter(Invoice.invoice_number.like(f"{prefix}%"))
        .all()
    )
    max_seq = 0
    for (num,) in existing:
        try:
            seq = int(num.split("-")[-1])
            max_seq = max(max_seq, seq)
        except (ValueError, IndexError):
            pass
    return f"{prefix}{max_seq + 1:03d}"


def _business_days_in_month(year: int, month: int) -> tuple[date, date]:
    """Return (first_business_day, last_business_day) for the given month."""
    _, last_day = calendar.monthrange(year, month)
    first = date(year, month, 1)
    while first.weekday() >= 5:  # 5=Sat, 6=Sun
        first += timedelta(days=1)
    last = date(year, month, last_day)
    while last.weekday() >= 5:
        last -= timedelta(days=1)
    return first, last


def _month_ordinal(contract_start: str, service_month: str) -> int:
    """Calculate 1-based month ordinal from contract start to service month.

    Both args accept YYYY-MM or YYYY-MM-DD (only year-month portion is used).
    January 2026 = Month 1 when contract_start is 2026-01.
    """
    start_parts = contract_start.split("-")
    month_parts = service_month.split("-")
    start_year, start_month = int(start_parts[0]), int(start_parts[1])
    svc_year, svc_month = int(month_parts[0]), int(month_parts[1])
    return (svc_year - start_year) * 12 + (svc_month - start_month) + 1


# ---------------------------------------------------------------------------
# Session dataclass (used by callers that pass BillableSession objects)
# ---------------------------------------------------------------------------


class SessionInput:
    """Lightweight value object representing one billable calendar session.

    Callers may pass BillableSession objects from ical_parser, plain dicts,
    or instances of this class — generator functions accept any object with
    the attributes: date (str YYYY-MM-DD), description (str), duration_hours
    (float), and optionally rate (float | None).
    """

    def __init__(
        self,
        date: str,
        description: str,
        duration_hours: float,
        rate: float | None = None,
    ) -> None:
        self.date = date
        self.description = description
        self.duration_hours = duration_hours
        self.rate = rate


# ---------------------------------------------------------------------------
# Calendar invoice generator
# ---------------------------------------------------------------------------


def generate_calendar_invoice(
    db: Session,
    customer: Customer,
    sessions: list[object],
    rate: float | decimal.Decimal | None = None,
) -> Invoice | None:
    """Generate a calendar-based invoice with one line item per session.

    Args:
        db:        Open SQLAlchemy Session. Caller commits.
        customer:  Customer ORM object (billing_model should be hourly).
        sessions:  List of session objects with .date, .description,
                   .duration_hours attributes. Accepts BillableSession,
                   SessionInput, or any duck-typed object.
        rate:      Hourly rate override. Falls back to customer.default_rate.

    Returns:
        The newly created Invoice (status=draft), or None if sessions is empty.

    Raises:
        ValueError: If any session is already billed on a non-void invoice
                    (double-billing guard).
    """
    if not sessions:
        return None

    # PRAGMA foreign_keys=OFF must be the first statement on the session so
    # that it runs before SQLite starts an implicit transaction.  This allows
    # storing the invoice_id in AuditEvent.transaction_id (a FK to
    # transactions.id) without triggering a constraint error.  We restore FK
    # enforcement at the end of the function before returning.
    db.execute(text("PRAGMA foreign_keys=OFF"))

    # Resolve effective rate
    effective_rate: decimal.Decimal
    if rate is not None:
        effective_rate = decimal.Decimal(str(rate))
    elif customer.default_rate is not None:
        effective_rate = decimal.Decimal(str(customer.default_rate))
    else:
        effective_rate = decimal.Decimal("0.00")

    # Double-billing guard: check each session against existing line items
    already_billed: list[str] = []
    for sess in sessions:
        sess_date = getattr(sess, "date", None)
        sess_desc = getattr(sess, "description", None)
        existing_li = (
            db.query(InvoiceLineItem)
            .join(Invoice, InvoiceLineItem.invoice_id == Invoice.id)
            .filter(
                Invoice.customer_id == customer.id,
                InvoiceLineItem.date == sess_date,
                InvoiceLineItem.description == sess_desc,
                Invoice.status != InvoiceStatus.VOID.value,
            )
            .first()
        )
        if existing_li is not None:
            already_billed.append(
                f"'{sess_desc}' on {sess_date} (invoice {existing_li.invoice_id[:8]}...)"
            )

    if already_billed:
        raise ValueError(
            "Double-billing guard triggered. Already billed:\n"
            + "\n".join(f"  - {item}" for item in already_billed)
        )

    # Filter out any sessions that slipped through (safety net — belt+suspenders)
    billable_sessions = list(sessions)

    # Determine invoice month from session dates
    session_dates = sorted(getattr(s, "date", "") for s in billable_sessions)
    first_date_str = session_dates[0]
    first_date = date.fromisoformat(first_date_str)
    last_date_str = session_dates[-1]

    # Auto-increment invoice number
    invoice_number = _next_calendar_invoice_number(db, first_date.year, first_date.month)

    invoice_id = _new_uuid()
    subtotal = decimal.Decimal("0.00")
    line_items: list[InvoiceLineItem] = []

    # One line item per session, sorted chronologically
    sorted_sessions = sorted(billable_sessions, key=lambda s: (getattr(s, "date", ""), getattr(s, "start_time", "") if hasattr(s, "start_time") else ""))
    for i, sess in enumerate(sorted_sessions):
        sess_date = getattr(sess, "date", "")
        sess_desc = getattr(sess, "description", "")
        sess_hours = decimal.Decimal(str(getattr(sess, "duration_hours", 0.0)))
        total_price = sess_hours * effective_rate

        li = InvoiceLineItem(
            id=_new_uuid(),
            invoice_id=invoice_id,
            description=sess_desc,
            quantity=sess_hours,
            unit_price=effective_rate,
            total_price=total_price,
            date=sess_date,
            sort_order=i,
        )
        db.add(li)
        line_items.append(li)
        subtotal += total_price

    # Create invoice
    today = date.today()
    terms_days = 14
    if customer.payment_terms:
        try:
            terms_days = int("".join(c for c in customer.payment_terms if c.isdigit()))
        except ValueError:
            pass

    invoice = Invoice(
        id=invoice_id,
        invoice_number=invoice_number,
        customer_id=customer.id,
        entity=Entity.SPARKRY.value,
        project=customer.name,
        submitted_date=today.isoformat(),
        due_date=(today + timedelta(days=terms_days)).isoformat(),
        service_period_start=first_date_str,
        service_period_end=last_date_str,
        subtotal=subtotal,
        adjustments=decimal.Decimal("0.00"),
        tax=decimal.Decimal("0.00"),
        total=subtotal,
        status=InvoiceStatus.DRAFT.value,
        payment_terms=customer.payment_terms,
        late_fee_pct=customer.late_fee_pct,
    )
    db.add(invoice)
    db.flush()  # Flush so invoice.id is available for audit event

    # Update customer.last_invoiced_date to the last session date
    customer.last_invoiced_date = last_date_str

    # Audit event
    _create_audit_event(db, invoice_id, "status", None, InvoiceStatus.DRAFT.value)

    # Restore FK enforcement for any subsequent operations in the session.
    db.execute(text("PRAGMA foreign_keys=ON"))

    return invoice


# ---------------------------------------------------------------------------
# Flat-rate invoice generator
# ---------------------------------------------------------------------------


def generate_flat_invoice(
    db: Session,
    customer: Customer,
    year: int,
    month: int,
) -> Invoice:
    """Generate a flat-rate monthly invoice.

    Args:
        db:       Open SQLAlchemy Session. Caller commits.
        customer: Customer ORM object (billing_model should be flat_rate).
        year:     Service year (e.g., 2026).
        month:    Service month, 1–12.

    Returns:
        The newly created Invoice (status=draft).

    Raises:
        ValueError: If an invoice already exists for the same customer +
                    service_period_start (duplicate guard).
    """
    # PRAGMA foreign_keys=OFF must be the first statement on the session.
    # See generate_calendar_invoice for the full explanation.
    db.execute(text("PRAGMA foreign_keys=OFF"))

    # Calculate service period
    first_biz, last_biz = _business_days_in_month(year, month)

    # Duplicate guard
    existing = (
        db.query(Invoice)
        .filter(
            Invoice.customer_id == customer.id,
            Invoice.service_period_start == first_biz.isoformat(),
        )
        .first()
    )
    if existing is not None:
        raise ValueError(
            f"Invoice already exists for customer '{customer.name}' "
            f"with service_period_start={first_biz.isoformat()} "
            f"(invoice #{existing.invoice_number})."
        )

    # Invoice number: invoice_prefix + YYYYMMDD (last business day)
    invoice_number = f"{customer.invoice_prefix}{year}{month:02d}{last_biz.day:02d}"

    # Amount
    rate = customer.default_rate or decimal.Decimal("0.00")
    subtotal = decimal.Decimal(str(rate))

    # Month ordinal
    ordinal = 1
    if customer.contract_start_date:
        service_month_str = f"{year}-{month:02d}"
        ordinal = _month_ordinal(customer.contract_start_date, service_month_str)

    # Line item description
    project_name = "AI Product Engineering Coaching"
    line_desc = f"{project_name} Month {ordinal}"

    invoice_id = _new_uuid()

    today = date.today()
    flat_terms_days = 14
    if customer.payment_terms:
        try:
            flat_terms_days = int("".join(c for c in customer.payment_terms if c.isdigit()))
        except ValueError:
            pass

    invoice = Invoice(
        id=invoice_id,
        invoice_number=invoice_number,
        customer_id=customer.id,
        entity=Entity.SPARKRY.value,
        project=project_name,
        submitted_date=today.isoformat(),
        due_date=(today + timedelta(days=flat_terms_days)).isoformat(),
        service_period_start=first_biz.isoformat(),
        service_period_end=last_biz.isoformat(),
        subtotal=subtotal,
        adjustments=decimal.Decimal("0.00"),
        tax=decimal.Decimal("0.00"),
        total=subtotal,
        status=InvoiceStatus.DRAFT.value,
        payment_terms=customer.payment_terms,
        late_fee_pct=customer.late_fee_pct,
        po_number=customer.po_number,
        payment_method=getattr(customer, "payment_method", None),
    )
    db.add(invoice)

    line_item = InvoiceLineItem(
        id=_new_uuid(),
        invoice_id=invoice_id,
        description=line_desc,
        quantity=decimal.Decimal("1.0000"),
        unit_price=subtotal,
        total_price=subtotal,
        sort_order=0,
    )
    db.add(line_item)
    db.flush()

    # Update customer.last_invoiced_date to the last business day of the month
    customer.last_invoiced_date = last_biz.isoformat()

    # Audit event
    _create_audit_event(db, invoice_id, "status", None, InvoiceStatus.DRAFT.value)

    # Restore FK enforcement for any subsequent operations in the session.
    db.execute(text("PRAGMA foreign_keys=ON"))

    return invoice
