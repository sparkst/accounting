"""Invoice and Customer endpoints.

GET   /api/invoices                     — List with optional filters + AR aging.
GET   /api/invoices/{id}                — Full invoice with line_items array.
PATCH /api/invoices/{id}                — Edit draft invoice fields and line items.
PATCH /api/invoices/{id}/status         — Transition status per state machine.
POST  /api/invoices/generate-flat       — Generate flat-rate invoice.
POST  /api/invoices/generate-calendar   — Generate calendar-based invoice.
POST  /api/invoices/ical-upload         — Parse .ics file for sessions.
GET   /api/invoices/{id}/pdf            — Download PDF.
GET   /api/invoices/{id}/html           — HTML preview.
GET   /api/customers                    — List all customers.
POST  /api/customers                    — Create customer.
PATCH /api/customers/{id}               — Update customer.
POST  /api/transactions/bulk-confirm    — Bulk confirm transactions.
"""

from __future__ import annotations

import calendar
import contextlib
import decimal
import logging
import os
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import stripe as _stripe
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy.orm import Session

from src.api.deps import get_db
from src.invoicing.email_sender import _validate_email, send_invoice_email
from src.invoicing.generator import generate_calendar_invoice as _gen_calendar
from src.invoicing.generator import generate_flat_invoice as _gen_flat
from src.invoicing.payment_link import create_payment_link
from src.invoicing.pdf_renderer import render_html as _render_html
from src.invoicing.pdf_renderer import render_pdf
from src.models.enums import (
    INVOICE_STATUS_TRANSITIONS,
    BillingModel,
    ConfirmedBy,
    InvoiceStatus,
    VendorRuleSource,
)
from src.models.invoice import Customer, Invoice, InvoiceLineItem
from src.models.transaction import Transaction
from src.models.vendor_rule import VendorRule

logger = logging.getLogger(__name__)

router = APIRouter(tags=["invoices"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _business_days_in_month(year: int, month: int) -> tuple[date, date]:
    """Return (first_business_day, last_business_day) for the given month."""
    _, last_day = calendar.monthrange(year, month)
    first = date(year, month, 1)
    # Advance to first weekday
    while first.weekday() >= 5:  # 5=Sat, 6=Sun
        first += timedelta(days=1)
    last = date(year, month, last_day)
    # Retreat to last weekday
    while last.weekday() >= 5:
        last -= timedelta(days=1)
    return first, last


def _month_ordinal(contract_start: str, service_month: str) -> int:
    """Calculate month ordinal from contract start to service month.

    Both args in YYYY-MM format or YYYY-MM-DD (only year-month used).
    """
    start_parts = contract_start.split("-")
    month_parts = service_month.split("-")
    start_year, start_month = int(start_parts[0]), int(start_parts[1])
    svc_year, svc_month = int(month_parts[0]), int(month_parts[1])
    return (svc_year - start_year) * 12 + (svc_month - start_month) + 1


def _create_invoice_audit_event(
    session: Session,
    invoice_id: str,
    field: str,
    old_value: str | None,
    new_value: str | None,
) -> None:
    """Create an AuditEvent for an invoice status/field change.

    Reuses the AuditEvent model. Since transaction_id has a FK to
    transactions and invoice IDs are not in that table, we use a
    separate connection with FK checks disabled (PRAGMA foreign_keys
    cannot be toggled mid-transaction in SQLite).
    """
    from sqlalchemy import Engine, text

    bind = session.get_bind()
    assert isinstance(bind, Engine)
    with bind.connect() as conn:
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        conn.execute(
            text(
                "INSERT INTO audit_events (id, transaction_id, field_changed, "
                "old_value, new_value, changed_by, changed_at) "
                "VALUES (:id, :tid, :field, :old, :new, :by, :at)"
            ),
            {
                "id": _new_uuid(),
                "tid": invoice_id,
                "field": field,
                "old": old_value,
                "new": new_value,
                "by": ConfirmedBy.HUMAN.value,
                "at": _now().isoformat(),
            },
        )
        conn.execute(text("PRAGMA foreign_keys=ON"))
        conn.commit()


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class LineItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    invoice_id: str
    description: str
    quantity: Any
    unit_price: Any
    total_price: Any
    date: str | None = None
    sort_order: int = 0

    @field_validator("quantity", "unit_price", "total_price", mode="before")
    @classmethod
    def coerce_decimal(cls, v: Any) -> str | None:
        if v is None:
            return None
        return str(v)


class InvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    invoice_number: str
    customer_id: str
    entity: str
    project: str | None = None
    submitted_date: str | None = None
    due_date: str | None = None
    service_period_start: str | None = None
    service_period_end: str | None = None
    paid_date: str | None = None
    subtotal: Any
    adjustments: Any
    tax: Any
    total: Any
    status: str
    payment_terms: str | None = None
    payment_method: str | None = None
    late_fee_pct: float = 0.0
    po_number: str | None = None
    payment_transaction_id: str | None = None
    payment_link_url: str | None = None
    payment_link_id: str | None = None
    sent_at: datetime | None = None
    sent_to: str | None = None
    sap_instructions: Any | None = None
    sap_checklist_state: Any | None = None
    pdf_path: str | None = None
    notes: str | None = None
    created_at: datetime
    updated_at: datetime

    # AR aging fields (computed, not on model)
    days_outstanding: int | None = None
    expected_payment_date: str | None = None

    # Nested line items (populated on detail endpoint)
    line_items: list[LineItemOut] | None = None

    @field_validator("subtotal", "adjustments", "tax", "total", mode="before")
    @classmethod
    def coerce_decimal(cls, v: Any) -> str | None:
        if v is None:
            return None
        return str(v)


class InvoiceListResponse(BaseModel):
    items: list[InvoiceOut]
    total: int


class LineItemPatch(BaseModel):
    id: str | None = None
    description: str | None = None
    quantity: float | None = None
    unit_price: float | None = None
    date: str | None = None
    sort_order: int | None = None


class InvoicePatch(BaseModel):
    project: str | None = None
    submitted_date: str | None = None
    due_date: str | None = None
    service_period_start: str | None = None
    service_period_end: str | None = None
    notes: str | None = None
    payment_terms: str | None = None
    payment_method: str | None = None
    po_number: str | None = None
    late_fee_pct: float | None = None
    line_items: list[LineItemPatch] | None = None
    sap_checklist_state: dict[str, bool] | None = None


class StatusTransition(BaseModel):
    status: str
    paid_date: str | None = None
    payment_transaction_id: str | None = None


class GenerateFlatRequest(BaseModel):
    customer_id: str
    month: str  # YYYY-MM


class CalendarSession(BaseModel):
    date: str
    description: str
    hours: float
    rate: float


class GenerateCalendarRequest(BaseModel):
    customer_id: str
    sessions: list[CalendarSession]


class CustomerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    contact_name: str | None = None
    contact_email: str | None = None
    billing_model: str
    default_rate: Any | None = None
    payment_terms: str | None = None
    invoice_prefix: str = ""
    late_fee_pct: float = 0.0
    po_number: str | None = None
    sap_config: Any | None = None
    calendar_patterns: Any | None = None
    calendar_exclusions: Any | None = None
    address: Any | None = None
    contract_start_date: str | None = None
    last_invoiced_date: str | None = None
    notes: str | None = None
    active: bool = True
    created_at: datetime

    @field_validator("default_rate", mode="before")
    @classmethod
    def coerce_decimal(cls, v: Any) -> str | None:
        if v is None:
            return None
        return str(v)


class CustomerCreate(BaseModel):
    name: str
    contact_name: str | None = None
    contact_email: str | None = None
    billing_model: str = BillingModel.HOURLY.value
    default_rate: float | None = None
    payment_terms: str | None = None
    invoice_prefix: str = ""
    late_fee_pct: float = 0.0
    po_number: str | None = None
    contract_start_date: str | None = None
    notes: str | None = None

    @field_validator("billing_model")
    @classmethod
    def validate_billing_model(cls, v: str) -> str:
        BillingModel(v)
        return v


class CustomerPatch(BaseModel):
    name: str | None = None
    contact_name: str | None = None
    contact_email: str | None = None
    billing_model: str | None = None
    default_rate: float | None = None
    payment_terms: str | None = None
    invoice_prefix: str | None = None
    late_fee_pct: float | None = None
    po_number: str | None = None
    contract_start_date: str | None = None
    notes: str | None = None
    active: bool | None = None

    @field_validator("billing_model")
    @classmethod
    def validate_billing_model(cls, v: str | None) -> str | None:
        if v is not None:
            BillingModel(v)
        return v


class BulkConfirmRequest(BaseModel):
    ids: list[str]
    entity: str | None = None
    tax_category: str | None = None


class BulkConfirmResponse(BaseModel):
    confirmed: int
    rules_created: int


class PaymentSuggestion(BaseModel):
    transaction_id: str
    date: str
    description: str
    amount: str
    days_from_due: int


class MatchPaymentRequest(BaseModel):
    transaction_id: str


class MatchPaymentResponse(BaseModel):
    invoice: InvoiceOut
    warning: str | None = None


class ARAgingItem(BaseModel):
    id: str
    invoice_number: str
    customer_name: str
    total: str
    submitted_date: str | None
    due_date: str | None
    days_outstanding: int
    expected_payment_date: str | None
    is_overdue: bool


class SendInvoiceRequest(BaseModel):
    to_email: str | None = None


class SendInvoiceResponse(BaseModel):
    invoice: InvoiceOut
    message: str


# ---------------------------------------------------------------------------
# Invoice CRUD routes
# ---------------------------------------------------------------------------


def _enrich_with_aging(inv: Invoice) -> dict[str, Any]:
    """Add AR aging fields to invoice data."""
    data = {c.name: getattr(inv, c.name) for c in inv.__table__.columns}
    data["days_outstanding"] = None
    data["expected_payment_date"] = None

    effective_status = inv.status
    # Auto-calculate overdue
    if inv.status == InvoiceStatus.SENT.value and inv.due_date:
        today = date.today()
        try:
            due = date.fromisoformat(inv.due_date)
            if today > due:
                effective_status = InvoiceStatus.OVERDUE.value
        except ValueError:
            pass

    if effective_status in (InvoiceStatus.SENT.value, InvoiceStatus.OVERDUE.value):
        if inv.submitted_date:
            try:
                submitted = date.fromisoformat(inv.submitted_date)
                today = date.today()
                data["days_outstanding"] = (today - submitted).days
            except ValueError:
                pass
        if inv.due_date:
            data["expected_payment_date"] = inv.due_date

    data["status"] = effective_status
    return data


@router.get("/invoices", response_model=InvoiceListResponse)
def list_invoices(
    customer_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    session: Session = Depends(get_db),  # noqa: B008
) -> InvoiceListResponse:
    """List invoices with optional filters and AR aging data."""
    query = session.query(Invoice)

    if customer_id is not None:
        query = query.filter(Invoice.customer_id == customer_id)
    if status is not None:
        query = query.filter(Invoice.status == status)
    if date_from is not None:
        query = query.filter(Invoice.submitted_date >= date_from)
    if date_to is not None:
        query = query.filter(Invoice.submitted_date <= date_to)

    query = query.order_by(Invoice.created_at.desc())
    invoices = query.all()
    total = len(invoices)

    items = []
    for inv in invoices:
        data = _enrich_with_aging(inv)
        items.append(InvoiceOut.model_validate(data))

    return InvoiceListResponse(items=items, total=total)


@router.get("/invoices/outstanding", response_model=list[ARAgingItem])
def get_outstanding_invoices(
    session: Session = Depends(get_db),  # noqa: B008
) -> list[ARAgingItem]:
    """Return all unpaid invoices (sent + overdue) for AR aging report.

    Each item includes days_outstanding (since submitted_date), expected_payment_date,
    and is_overdue flag. Sorted by days_outstanding descending (oldest first).
    """
    invoices = (
        session.query(Invoice)
        .filter(Invoice.status.in_([InvoiceStatus.SENT.value, InvoiceStatus.OVERDUE.value]))
        .all()
    )

    today = date.today()
    items: list[ARAgingItem] = []

    for inv in invoices:
        customer: Customer | None = session.get(Customer, inv.customer_id)
        customer_name = customer.name if customer else "Unknown"

        days_outstanding = 0
        if inv.submitted_date:
            try:
                submitted = date.fromisoformat(inv.submitted_date)
                days_outstanding = (today - submitted).days
            except ValueError:
                pass

        is_overdue = False
        if inv.due_date:
            try:
                due = date.fromisoformat(inv.due_date)
                is_overdue = today > due
            except ValueError:
                pass
        # Also flag invoices stored with overdue status
        if inv.status == InvoiceStatus.OVERDUE.value:
            is_overdue = True

        items.append(ARAgingItem(
            id=inv.id,
            invoice_number=inv.invoice_number,
            customer_name=customer_name,
            total=str(inv.total),
            submitted_date=inv.submitted_date,
            due_date=inv.due_date,
            days_outstanding=days_outstanding,
            expected_payment_date=inv.due_date,
            is_overdue=is_overdue,
        ))

    items.sort(key=lambda x: x.days_outstanding, reverse=True)
    return items


@router.get("/invoices/{invoice_id}", response_model=InvoiceOut)
def get_invoice(
    invoice_id: str,
    session: Session = Depends(get_db),  # noqa: B008
) -> InvoiceOut:
    """Return a single invoice with line_items array."""
    inv: Invoice | None = session.get(Invoice, invoice_id)
    if inv is None:
        raise HTTPException(status_code=404, detail="Invoice not found")

    data = _enrich_with_aging(inv)

    line_items = (
        session.query(InvoiceLineItem)
        .filter(InvoiceLineItem.invoice_id == invoice_id)
        .order_by(InvoiceLineItem.sort_order)
        .all()
    )
    data["line_items"] = [LineItemOut.model_validate(li) for li in line_items]

    return InvoiceOut.model_validate(data)


@router.patch("/invoices/{invoice_id}", response_model=InvoiceOut)
def patch_invoice(
    invoice_id: str,
    body: InvoicePatch,
    session: Session = Depends(get_db),  # noqa: B008
) -> InvoiceOut:
    """Edit a draft invoice's fields and line items.

    Returns 422 if the invoice is not in draft status.
    """
    inv: Invoice | None = session.get(Invoice, invoice_id)
    if inv is None:
        raise HTTPException(status_code=404, detail="Invoice not found")

    patch_data = body.model_dump(exclude_none=True)

    # sap_checklist_state can be updated regardless of invoice status
    if body.sap_checklist_state is not None:
        inv.sap_checklist_state = body.sap_checklist_state
        inv.updated_at = _now()
        session.commit()
        session.refresh(inv)

        # If only sap_checklist_state was sent, return early
        other_fields = {k: v for k, v in patch_data.items() if k != "sap_checklist_state"}
        if not other_fields:
            line_items = (
                session.query(InvoiceLineItem)
                .filter(InvoiceLineItem.invoice_id == invoice_id)
                .order_by(InvoiceLineItem.sort_order)
                .all()
            )
            data = _enrich_with_aging(inv)
            data["line_items"] = [LineItemOut.model_validate(li) for li in line_items]
            return InvoiceOut.model_validate(data)

    if inv.status != InvoiceStatus.DRAFT.value:
        raise HTTPException(
            status_code=422,
            detail=f"Cannot edit invoice in '{inv.status}' status. Only draft invoices can be edited.",
        )

    # Update scalar fields
    scalar_fields = [
        "project", "submitted_date", "due_date", "service_period_start",
        "service_period_end", "notes", "payment_terms", "payment_method",
        "po_number", "late_fee_pct",
    ]
    for field in scalar_fields:
        if field in patch_data:
            setattr(inv, field, patch_data[field])

    # Update line items if provided
    if body.line_items is not None:
        # Delete existing line items
        session.query(InvoiceLineItem).filter(
            InvoiceLineItem.invoice_id == invoice_id
        ).delete()

        subtotal = decimal.Decimal("0.00")
        for i, li in enumerate(body.line_items):
            qty = decimal.Decimal(str(li.quantity or 1))
            price = decimal.Decimal(str(li.unit_price or 0))
            total_price = qty * price
            subtotal += total_price

            item = InvoiceLineItem(
                id=_new_uuid(),
                invoice_id=invoice_id,
                description=li.description or "",
                quantity=qty,
                unit_price=price,
                total_price=total_price,
                date=li.date,
                sort_order=li.sort_order if li.sort_order is not None else i,
            )
            session.add(item)

        inv.subtotal = subtotal
        inv.total = subtotal + inv.adjustments + inv.tax

    inv.updated_at = _now()
    session.commit()
    session.refresh(inv)

    # Return with line items
    line_items = (
        session.query(InvoiceLineItem)
        .filter(InvoiceLineItem.invoice_id == invoice_id)
        .order_by(InvoiceLineItem.sort_order)
        .all()
    )
    data = _enrich_with_aging(inv)
    data["line_items"] = [LineItemOut.model_validate(li) for li in line_items]
    return InvoiceOut.model_validate(data)


@router.patch("/invoices/{invoice_id}/status", response_model=InvoiceOut)
def transition_invoice_status(
    invoice_id: str,
    body: StatusTransition,
    session: Session = Depends(get_db),  # noqa: B008
) -> InvoiceOut:
    """Transition invoice status per the state machine.

    Invalid transitions return 422 with allowed transitions list.
    Every transition creates an AuditEvent.
    paid->void unlinks payment_transaction_id.
    """
    inv: Invoice | None = session.get(Invoice, invoice_id)
    if inv is None:
        raise HTTPException(status_code=404, detail="Invoice not found")

    current = inv.status
    new_status = body.status

    allowed = INVOICE_STATUS_TRANSITIONS.get(current, set())
    if new_status not in allowed:
        raise HTTPException(
            status_code=422,
            detail={
                "message": f"Cannot transition from '{current}' to '{new_status}'.",
                "allowed_transitions": sorted(allowed),
            },
        )

    old_status = inv.status
    inv.status = new_status

    # Handle paid transition
    if new_status == InvoiceStatus.PAID.value:
        if body.paid_date:
            inv.paid_date = body.paid_date
        else:
            inv.paid_date = date.today().isoformat()
        if body.payment_transaction_id:
            inv.payment_transaction_id = body.payment_transaction_id

    # Handle paid->void: unlink payment
    if old_status == InvoiceStatus.PAID.value and new_status == InvoiceStatus.VOID.value:
        old_payment_id = inv.payment_transaction_id
        inv.payment_transaction_id = None
        inv.paid_date = None
        if old_payment_id:
            _create_invoice_audit_event(
                session, invoice_id,
                "payment_transaction_id", old_payment_id, None,
            )

    # Deactivate Stripe payment link when voiding
    if new_status == InvoiceStatus.VOID.value and inv.payment_link_id:
        try:
            _stripe_deactivate_link(inv.payment_link_id)
        except Exception:
            logger.warning("Failed to deactivate payment link %s", inv.payment_link_id)

    inv.updated_at = _now()

    # Audit event for status change
    _create_invoice_audit_event(
        session, invoice_id, "status", old_status, new_status,
    )

    session.commit()
    session.refresh(inv)

    data = _enrich_with_aging(inv)
    return InvoiceOut.model_validate(data)


# ---------------------------------------------------------------------------
# Invoice send
# ---------------------------------------------------------------------------


def _stripe_deactivate_link(payment_link_id: str) -> None:
    _stripe.api_key = os.environ.get("STRIPE_RESTRICTED_KEY", "")
    _stripe.PaymentLink.modify(payment_link_id, active=False)


@router.post("/invoices/{invoice_id}/send", response_model=SendInvoiceResponse)
def send_invoice(
    invoice_id: str,
    body: SendInvoiceRequest,
    session: Session = Depends(get_db),  # noqa: B008
) -> SendInvoiceResponse:
    """Send an invoice via email with a Stripe payment link.

    Creates a Stripe payment link (or reuses existing), generates PDF,
    sends email via Resend. Transitions status to 'sent'.
    """
    inv: Invoice | None = session.get(Invoice, invoice_id)
    if inv is None:
        raise HTTPException(status_code=404, detail="Invoice not found")

    customer: Customer | None = session.get(Customer, inv.customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")

    if inv.status in (InvoiceStatus.PAID.value, InvoiceStatus.VOID.value):
        raise HTTPException(
            status_code=422,
            detail=f"Cannot send invoice in '{inv.status}' status.",
        )

    if inv.total is None or inv.total <= 0:
        raise HTTPException(
            status_code=422,
            detail="Invoice total must be positive.",
        )

    to_email = body.to_email or customer.contact_email
    if not to_email:
        raise HTTPException(
            status_code=422,
            detail="No recipient email. Provide to_email or set customer contact_email.",
        )
    try:
        _validate_email(to_email)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    line_items = (
        session.query(InvoiceLineItem)
        .filter(InvoiceLineItem.invoice_id == invoice_id)
        .order_by(InvoiceLineItem.sort_order)
        .all()
    )

    pdf_bytes = render_pdf(inv, line_items, customer)

    freshly_created = not inv.payment_link_id
    try:
        link_result = create_payment_link(inv)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to create payment link: {exc}",
        ) from exc

    inv.payment_link_url = link_result.url
    inv.payment_link_id = link_result.link_id
    inv.updated_at = _now()
    session.commit()
    session.refresh(inv)

    try:
        send_invoice_email(
            invoice=inv,
            line_items=line_items,
            customer=customer,
            pdf_bytes=pdf_bytes,
            payment_link_url=link_result.url,
            to_email=to_email,
        )
    except Exception as exc:
        if freshly_created:
            try:
                _stripe_deactivate_link(link_result.link_id)
            except Exception:
                logger.warning("Failed to deactivate payment link after email failure")
        raise HTTPException(
            status_code=502,
            detail=f"Payment link created but email failed: {exc}",
        ) from exc

    old_sent_to = inv.sent_to
    inv.sent_at = _now()
    inv.sent_to = to_email
    inv.updated_at = _now()

    old_status = inv.status
    if inv.status == InvoiceStatus.DRAFT.value:
        inv.status = InvoiceStatus.SENT.value
        _create_invoice_audit_event(
            session, invoice_id, "status", old_status, InvoiceStatus.SENT.value,
        )

    _create_invoice_audit_event(
        session, invoice_id, "sent_to", old_sent_to, to_email,
    )

    session.commit()
    session.refresh(inv)

    data = _enrich_with_aging(inv)
    return SendInvoiceResponse(
        invoice=InvoiceOut.model_validate(data),
        message=f"Invoice sent to {to_email}",
    )


# ---------------------------------------------------------------------------
# Invoice generation
# ---------------------------------------------------------------------------


@router.post("/invoices/generate-flat", response_model=InvoiceOut, status_code=201)
def generate_flat_invoice(
    body: GenerateFlatRequest,
    session: Session = Depends(get_db),  # noqa: B008
) -> InvoiceOut:
    """Generate a flat-rate invoice for a given customer and month."""
    customer: Customer | None = session.get(Customer, body.customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")

    # Parse month
    try:
        parts = body.month.split("-")
        year, month_num = int(parts[0]), int(parts[1])
    except (ValueError, IndexError) as exc:
        raise HTTPException(
            status_code=422, detail=f"Invalid month format: {body.month!r}. Expected YYYY-MM."
        ) from exc

    try:
        invoice = _gen_flat(session, customer, year, month_num)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    session.commit()
    session.refresh(invoice)

    line_items = (
        session.query(InvoiceLineItem)
        .filter(InvoiceLineItem.invoice_id == invoice.id)
        .order_by(InvoiceLineItem.sort_order)
        .all()
    )

    data = _enrich_with_aging(invoice)
    data["line_items"] = [LineItemOut.model_validate(li) for li in line_items]
    return InvoiceOut.model_validate(data)


@router.post("/invoices/generate-calendar", response_model=InvoiceOut, status_code=201)
def generate_calendar_invoice(
    body: GenerateCalendarRequest,
    session: Session = Depends(get_db),  # noqa: B008
) -> InvoiceOut:
    """Generate a calendar-based invoice with one line item per session."""
    customer: Customer | None = session.get(Customer, body.customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")

    if not body.sessions:
        raise HTTPException(status_code=422, detail="At least one session is required.")

    # Adapt CalendarSession objects to duck-typed interface expected by generator
    class _SessionAdapter:
        def __init__(self, cs: CalendarSession) -> None:
            self.date = cs.date
            self.description = cs.description
            self.duration_hours = cs.hours
            self.rate = cs.rate

    adapted = [_SessionAdapter(s) for s in body.sessions]

    # Use the rate from the first session (all sessions in one request share a rate)
    rate = body.sessions[0].rate if body.sessions else None

    try:
        invoice = _gen_calendar(session, customer, adapted, rate=rate)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if invoice is None:
        raise HTTPException(status_code=422, detail="At least one session is required.")

    session.commit()
    session.refresh(invoice)

    line_items = (
        session.query(InvoiceLineItem)
        .filter(InvoiceLineItem.invoice_id == invoice.id)
        .order_by(InvoiceLineItem.sort_order)
        .all()
    )

    data = _enrich_with_aging(invoice)
    data["line_items"] = [LineItemOut.model_validate(li) for li in line_items]
    return InvoiceOut.model_validate(data)


# ---------------------------------------------------------------------------
# iCal upload
# ---------------------------------------------------------------------------


@router.post("/invoices/ical-upload")
async def ical_upload(
    file: UploadFile = File(...),  # noqa: B008
    customer_id: str | None = Query(default=None),
    start_date: str | None = Query(default=None, description="YYYY-MM-DD"),
    end_date: str | None = Query(default=None, description="YYYY-MM-DD"),
    session: Session = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    """Parse an uploaded .ics file and return parsed sessions + unmatched events."""
    if file.size is not None and file.size > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File exceeds 10MB limit.")

    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File exceeds 10MB limit.")

    # Try to use ical_parser if available
    try:
        from src.invoicing.ical_parser import parse_ical
    except ImportError as exc:
        raise HTTPException(
            status_code=501,
            detail="iCal parser not yet implemented. Upload is accepted but parsing is unavailable.",
        ) from exc

    # Look up customer if provided
    customer = None
    if customer_id:
        customer = session.get(Customer, customer_id)
        if customer is None:
            raise HTTPException(status_code=404, detail="Customer not found")

    # Parse dates
    sd = date.fromisoformat(start_date) if start_date else date.today().replace(day=1)
    ed = date.fromisoformat(end_date) if end_date else date.today()

    result = parse_ical(content, customer, sd, ed)

    # Convert dataclass result to dict, renaming duration_hours → hours
    return {
        "matched_sessions": [
            {
                "date": s.date,
                "start_time": s.start_time,
                "description": s.description,
                "hours": s.duration_hours,
                "event_uid": s.event_uid,
            }
            for s in result.matched_sessions
        ],
        "unmatched_events": result.unmatched_events,
        "warnings": result.warnings if hasattr(result, "warnings") else [],
    }


# ---------------------------------------------------------------------------
# PDF / HTML
# ---------------------------------------------------------------------------


@router.get("/invoices/{invoice_id}/pdf")
def get_invoice_pdf(
    invoice_id: str,
    session: Session = Depends(get_db),  # noqa: B008
) -> Response:
    """Download the invoice as a PDF."""
    inv: Invoice | None = session.get(Invoice, invoice_id)
    if inv is None:
        raise HTTPException(status_code=404, detail="Invoice not found")

    customer: Customer | None = session.get(Customer, inv.customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")

    line_items = (
        session.query(InvoiceLineItem)
        .filter(InvoiceLineItem.invoice_id == invoice_id)
        .order_by(InvoiceLineItem.sort_order)
        .all()
    )

    pdf_bytes = render_pdf(inv, line_items, customer)

    filename = f"invoice-{inv.invoice_number}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/invoices/{invoice_id}/html", response_class=HTMLResponse)
def get_invoice_html(
    invoice_id: str,
    session: Session = Depends(get_db),  # noqa: B008
) -> HTMLResponse:
    """HTML preview of the invoice."""
    inv: Invoice | None = session.get(Invoice, invoice_id)
    if inv is None:
        raise HTTPException(status_code=404, detail="Invoice not found")

    customer: Customer | None = session.get(Customer, inv.customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")

    line_items = (
        session.query(InvoiceLineItem)
        .filter(InvoiceLineItem.invoice_id == invoice_id)
        .order_by(InvoiceLineItem.sort_order)
        .all()
    )

    html = _render_html(inv, line_items, customer)
    return HTMLResponse(content=html)


# ---------------------------------------------------------------------------
# Payment matching
# ---------------------------------------------------------------------------


@router.get("/invoices/{invoice_id}/payment-suggestions", response_model=list[PaymentSuggestion])
def get_payment_suggestions(
    invoice_id: str,
    session: Session = Depends(get_db),  # noqa: B008
) -> list[PaymentSuggestion]:
    """Return up to 5 income transactions that are candidates to match this invoice.

    Matching criteria:
    - direction = 'income'
    - entity matches the invoice entity
    - date >= invoice submitted_date
    - amount within $0.01 of invoice total
    Sorted by proximity to invoice due_date (closest first).
    """
    inv: Invoice | None = session.get(Invoice, invoice_id)
    if inv is None:
        raise HTTPException(status_code=404, detail="Invoice not found")

    if inv.total is None:
        return []

    invoice_total = decimal.Decimal(str(inv.total))
    tolerance = decimal.Decimal("0.01")

    query = (
        session.query(Transaction)
        .filter(Transaction.direction == "income")
    )

    if inv.entity:
        query = query.filter(Transaction.entity == inv.entity)

    if inv.submitted_date:
        query = query.filter(Transaction.date >= inv.submitted_date)

    candidates = query.all()

    # Filter by amount within tolerance
    matches = []
    for tx in candidates:
        if tx.amount is None:
            continue
        tx_amount = decimal.Decimal(str(tx.amount))
        if abs(tx_amount - invoice_total) <= tolerance:
            matches.append(tx)

    # Sort by proximity to due_date
    due_date_obj: date | None = None
    if inv.due_date:
        with contextlib.suppress(ValueError):
            due_date_obj = date.fromisoformat(inv.due_date)

    def _days_from_due(tx: Transaction) -> int:
        if due_date_obj is None:
            return 0
        try:
            tx_date = date.fromisoformat(tx.date)
            return abs((tx_date - due_date_obj).days)
        except ValueError:
            return 999999

    matches.sort(key=_days_from_due)

    return [
        PaymentSuggestion(
            transaction_id=tx.id,
            date=tx.date,
            description=tx.description,
            amount=str(tx.amount),
            days_from_due=_days_from_due(tx),
        )
        for tx in matches[:5]
    ]


@router.post("/invoices/{invoice_id}/match-payment", response_model=MatchPaymentResponse)
def match_payment(
    invoice_id: str,
    body: MatchPaymentRequest,
    session: Session = Depends(get_db),  # noqa: B008
) -> MatchPaymentResponse:
    """Link an income transaction to an invoice as payment.

    - Sets payment_transaction_id and paid_date on the invoice.
    - If transaction amount == invoice total (within $0.01): transitions to 'paid'.
    - If transaction amount < invoice total: invoice stays 'sent', returns partial warning.
    - If transaction amount > invoice total: marks paid, returns overpayment warning.
    - Creates AuditEvent for the match.
    """
    inv: Invoice | None = session.get(Invoice, invoice_id)
    if inv is None:
        raise HTTPException(status_code=404, detail="Invoice not found")

    tx: Transaction | None = session.get(Transaction, body.transaction_id)
    if tx is None:
        raise HTTPException(status_code=404, detail="Transaction not found")

    invoice_total = decimal.Decimal(str(inv.total))
    tx_amount = decimal.Decimal(str(tx.amount)) if tx.amount is not None else decimal.Decimal("0.00")
    tolerance = decimal.Decimal("0.01")

    warning: str | None = None

    if tx_amount < invoice_total - tolerance:
        # Partial payment — do not mark paid
        warning = f"Partial payment: ${tx_amount:,.2f} of ${invoice_total:,.2f}"
        inv.payment_transaction_id = tx.id
        inv.updated_at = _now()
        _create_invoice_audit_event(
            session, invoice_id,
            "payment_transaction_id", inv.payment_transaction_id, tx.id,
        )
    else:
        # Full payment or overpayment
        if tx_amount > invoice_total + tolerance:
            warning = f"Overpayment: received ${tx_amount:,.2f} for ${invoice_total:,.2f} invoice"

        old_status = inv.status
        inv.status = InvoiceStatus.PAID.value
        inv.paid_date = date.today().isoformat()
        inv.payment_transaction_id = tx.id
        inv.updated_at = _now()

        _create_invoice_audit_event(
            session, invoice_id,
            "payment_transaction_id", None, tx.id,
        )
        _create_invoice_audit_event(
            session, invoice_id,
            "status", old_status, InvoiceStatus.PAID.value,
        )

    session.commit()
    session.refresh(inv)

    data = _enrich_with_aging(inv)
    return MatchPaymentResponse(
        invoice=InvoiceOut.model_validate(data),
        warning=warning,
    )


# ---------------------------------------------------------------------------
# Customer CRUD
# ---------------------------------------------------------------------------


@router.get("/customers", response_model=list[CustomerOut])
def list_customers(
    session: Session = Depends(get_db),  # noqa: B008
) -> list[CustomerOut]:
    """List all customers."""
    customers = session.query(Customer).order_by(Customer.name).all()
    return [CustomerOut.model_validate(c) for c in customers]


@router.post("/customers", response_model=CustomerOut, status_code=201)
def create_customer(
    body: CustomerCreate,
    session: Session = Depends(get_db),  # noqa: B008
) -> CustomerOut:
    """Create a new customer."""
    customer = Customer(
        id=_new_uuid(),
        name=body.name,
        contact_name=body.contact_name,
        contact_email=body.contact_email,
        billing_model=body.billing_model,
        default_rate=decimal.Decimal(str(body.default_rate)) if body.default_rate else None,
        payment_terms=body.payment_terms,
        invoice_prefix=body.invoice_prefix,
        late_fee_pct=body.late_fee_pct,
        po_number=body.po_number,
        contract_start_date=body.contract_start_date,
        notes=body.notes,
    )
    session.add(customer)
    session.commit()
    session.refresh(customer)
    return CustomerOut.model_validate(customer)


@router.patch("/customers/{customer_id}", response_model=CustomerOut)
def patch_customer(
    customer_id: str,
    body: CustomerPatch,
    session: Session = Depends(get_db),  # noqa: B008
) -> CustomerOut:
    """Update an existing customer."""
    customer: Customer | None = session.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")

    patch_data = body.model_dump(exclude_none=True)
    for field, value in patch_data.items():
        if field == "default_rate":
            value = decimal.Decimal(str(value))
        setattr(customer, field, value)

    session.commit()
    session.refresh(customer)
    return CustomerOut.model_validate(customer)


# ---------------------------------------------------------------------------
# Bulk confirm (for review queue)
# ---------------------------------------------------------------------------


@router.post("/transactions/bulk-confirm", response_model=BulkConfirmResponse)
def bulk_confirm_transactions(
    body: BulkConfirmRequest,
    session: Session = Depends(get_db),  # noqa: B008
) -> BulkConfirmResponse:
    """Bulk confirm transactions with entity and tax_category.

    Creates a VendorRule for each unique vendor (learning loop).
    """
    confirmed = 0
    rules_created = 0
    now = _now()

    for tx_id in body.ids:
        tx: Transaction | None = session.get(Transaction, tx_id)
        if tx is None:
            continue

        if body.entity:
            tx.entity = body.entity
        if body.tax_category:
            tx.tax_category = body.tax_category
        tx.status = "confirmed"
        tx.confirmed_by = ConfirmedBy.HUMAN.value
        tx.updated_at = now
        confirmed += 1

        # Learning loop: create vendor rule for unique vendors
        vendor_pattern = tx.description
        rule_entity = body.entity or tx.entity
        rule_category = body.tax_category or tx.tax_category
        if vendor_pattern and tx.direction and rule_entity:
            existing_rule = (
                session.query(VendorRule)
                .filter(
                    VendorRule.vendor_pattern == vendor_pattern,
                    VendorRule.entity == rule_entity,
                )
                .first()
            )
            if existing_rule is None:
                rule = VendorRule(
                    vendor_pattern=vendor_pattern,
                    entity=rule_entity,
                    tax_category=rule_category,
                    direction=tx.direction,
                    confidence=0.80,
                    source=VendorRuleSource.LEARNED.value,
                    examples=1,
                    last_matched=now,
                )
                session.add(rule)
                rules_created += 1
            else:
                existing_rule.examples += 1
                existing_rule.last_matched = now

    session.commit()
    return BulkConfirmResponse(confirmed=confirmed, rules_created=rules_created)
