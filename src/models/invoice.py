"""Customer, Invoice, and InvoiceLineItem ORM models.

Supports two invoicing workflows:
- Calendar-based (hourly): one InvoiceLineItem per billable session (e.g., How To Fascinate)
- Flat-rate (monthly): one InvoiceLineItem for the period (e.g., Cardinal Health)

Design decisions:
- Amounts stored as NUMERIC to preserve precision (same as Transaction).
- Dates stored as ISO 8601 strings (YYYY-MM-DD), same convention as Transaction.
- JSON columns (sap_config, calendar_patterns, etc.) stored as SQLite JSON blobs.
- payment_transaction_id is a nullable FK to transactions.id for payment reconciliation.
- sap_checklist_state persists per-invoice SAP Ariba checkbox state between sessions.
"""

import decimal
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from src.models.base import Base
from src.models.enums import BillingModel, Entity, InvoiceStatus


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Customer(Base):
    """A billing customer for Sparkry LLC invoices.

    Each customer has a billing model (hourly or flat_rate), invoice number
    format, and optional SAP Ariba or calendar configuration.
    """

    __tablename__ = "customers"

    # ── Identity ──────────────────────────────────────────────────────────────
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)

    # ── Contact ────────────────────────────────────────────────────────────────
    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Full customer / company name",
    )
    contact_name: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="Primary contact person",
    )
    contact_email: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="Primary contact email",
    )

    # ── Billing configuration ──────────────────────────────────────────────────
    billing_model: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="BillingModel enum: hourly | flat_rate | project",
    )
    default_rate: Mapped[Any | None] = mapped_column(
        Numeric(precision=12, scale=2, asdecimal=True),
        nullable=True,
        comment="Default hourly or monthly rate",
    )
    payment_terms: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="e.g. 'Net 14', 'Net 90'",
    )
    invoice_prefix: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="",
        server_default="",
        comment="Prefix for auto-generated invoice numbers (e.g. 'CH' or '' for YYYYMM-NNN)",
    )
    late_fee_pct: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0.0,
        server_default="0.0",
        comment="Late fee percentage, e.g. 0.10 = 10%",
    )
    po_number: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Standing PO number (Cardinal Health: 4700158965)",
    )

    # ── SAP Ariba configuration ────────────────────────────────────────────────
    sap_config: Mapped[Any | None] = mapped_column(
        JSON,
        nullable=True,
        comment="SAP Ariba submission details: login URL, classification, etc.",
    )

    # ── Calendar / iCal configuration ─────────────────────────────────────────
    calendar_patterns: Mapped[Any | None] = mapped_column(
        JSON,
        nullable=True,
        comment="List of regex/substring patterns to match in iCal SUMMARY fields",
    )
    calendar_exclusions: Mapped[Any | None] = mapped_column(
        JSON,
        nullable=True,
        comment="List of patterns to exclude from iCal matches",
    )

    # ── Address ────────────────────────────────────────────────────────────────
    address: Mapped[Any | None] = mapped_column(
        JSON,
        nullable=True,
        comment="Mailing address as a dict with street, city, state, zip keys",
    )

    # ── Billing history helpers ────────────────────────────────────────────────
    contract_start_date: Mapped[str | None] = mapped_column(
        String(10),
        nullable=True,
        comment="ISO date when engagement began — used for month ordinal calculation",
    )
    last_invoiced_date: Mapped[str | None] = mapped_column(
        String(10),
        nullable=True,
        comment="ISO date of the most recent sent invoice — for billing continuity checks",
    )

    # ── Miscellaneous ─────────────────────────────────────────────────────────
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="1",
        comment="Soft-delete flag; inactive customers are hidden from new invoice flows",
    )

    # ── Audit ──────────────────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=_now,
    )

    # ── Convenience properties ─────────────────────────────────────────────────

    @property
    def billing_model_enum(self) -> BillingModel:
        return BillingModel(self.billing_model)

    def __repr__(self) -> str:
        id_prefix = self.id[:8] if self.id else "unsaved"
        return f"<Customer id={id_prefix} name={self.name!r} model={self.billing_model}>"


class Invoice(Base):
    """A Sparkry LLC invoice sent to a customer.

    Status state machine (enforced at the API layer):
        draft → sent | void
        sent  → paid | void | overdue
        paid  → void
        overdue → paid | void
        void  → (terminal)
    """

    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("invoice_number", name="uq_invoices_invoice_number"),
    )

    # ── Identity ──────────────────────────────────────────────────────────────
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)

    # ── Invoice identity ───────────────────────────────────────────────────────
    invoice_number: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        index=True,
        comment="Human-readable invoice number (YYYYMM-NNN or CH+YYYYMMDD)",
    )
    customer_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("customers.id"),
        nullable=False,
        index=True,
    )
    entity: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=Entity.SPARKRY.value,
        comment="Always 'sparkry' for now — all invoices are Sparkry LLC",
    )
    project: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="Project name shown on invoice (e.g., 'Fascinate OS')",
    )

    # ── Dates ─────────────────────────────────────────────────────────────────
    submitted_date: Mapped[str | None] = mapped_column(
        String(10),
        nullable=True,
        comment="ISO date when invoice was created / sent to customer",
    )
    due_date: Mapped[str | None] = mapped_column(
        String(10),
        nullable=True,
        comment="ISO date payment is due",
    )
    service_period_start: Mapped[str | None] = mapped_column(
        String(10),
        nullable=True,
        comment="ISO date — start of billing period",
    )
    service_period_end: Mapped[str | None] = mapped_column(
        String(10),
        nullable=True,
        comment="ISO date — end of billing period",
    )
    paid_date: Mapped[str | None] = mapped_column(
        String(10),
        nullable=True,
        comment="ISO date payment was received (nullable until paid)",
    )

    # ── Amounts ────────────────────────────────────────────────────────────────
    subtotal: Mapped[Any] = mapped_column(
        Numeric(precision=12, scale=2, asdecimal=True),
        nullable=False,
        comment="Sum of all InvoiceLineItem.total_price values",
    )
    adjustments: Mapped[Any] = mapped_column(
        Numeric(precision=12, scale=2, asdecimal=True),
        nullable=False,
        default=decimal.Decimal("0.00"),
        comment="Discounts or credits (negative = discount)",
    )
    tax: Mapped[Any] = mapped_column(
        Numeric(precision=12, scale=2, asdecimal=True),
        nullable=False,
        default=decimal.Decimal("0.00"),
        comment="Tax amount (typically $0 for services in WA B&O context)",
    )
    total: Mapped[Any] = mapped_column(
        Numeric(precision=12, scale=2, asdecimal=True),
        nullable=False,
        comment="Final amount due = subtotal + adjustments + tax",
    )

    # ── Status ────────────────────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=InvoiceStatus.DRAFT.value,
        index=True,
        comment="InvoiceStatus enum: draft | sent | paid | overdue | void",
    )

    # ── Payment details ────────────────────────────────────────────────────────
    payment_terms: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="e.g. 'Net 14', 'Net 90'",
    )
    payment_method: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="e.g. 'ACH', 'Check', 'Wire'",
    )
    late_fee_pct: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0.0,
        server_default="0.0",
        comment="Late fee percentage applied if paid after due_date",
    )
    po_number: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Customer purchase order number for this invoice",
    )

    # ── Payment reconciliation ─────────────────────────────────────────────────
    payment_transaction_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("transactions.id"),
        nullable=True,
        index=True,
        comment="FK to the income transaction when payment is received",
    )

    # ── SAP / workflow data ────────────────────────────────────────────────────
    sap_instructions: Mapped[Any | None] = mapped_column(
        JSON,
        nullable=True,
        comment="SAP Ariba submission steps rendered for the dashboard panel",
    )
    sap_checklist_state: Mapped[Any | None] = mapped_column(
        JSON,
        nullable=True,
        comment="Per-step checkbox state for SAP Ariba submission (persisted between sessions)",
    )

    # ── Payment link / send tracking ──────────────────────────────────────────
    payment_link_url: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        comment="Stripe payment link URL for online payment",
    )
    payment_link_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="Stripe payment link ID (for deactivation on void)",
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        comment="When the invoice email was last sent",
    )
    sent_to: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="Email address the invoice was sent to",
    )

    # ── PDF / document ─────────────────────────────────────────────────────────
    pdf_path: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        comment="Filesystem path to generated PDF file",
    )

    # ── Notes ─────────────────────────────────────────────────────────────────
    notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="e.g. 'Introductory Rate: $100/hr'",
    )

    # ── Audit ──────────────────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=_now,
        onupdate=_now,
    )

    # ── Convenience properties ─────────────────────────────────────────────────

    @property
    def status_enum(self) -> InvoiceStatus:
        return InvoiceStatus(self.status)

    def __repr__(self) -> str:
        id_prefix = self.id[:8] if self.id else "unsaved"
        return (
            f"<Invoice id={id_prefix} number={self.invoice_number!r} "
            f"status={self.status} total={self.total}>"
        )


class InvoiceLineItem(Base):
    """A single line on an invoice — one billable session or one flat-rate charge."""

    __tablename__ = "invoice_line_items"

    # ── Identity ──────────────────────────────────────────────────────────────
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)

    # ── Parent invoice ─────────────────────────────────────────────────────────
    invoice_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("invoices.id"),
        nullable=False,
        index=True,
    )

    # ── Line item content ──────────────────────────────────────────────────────
    description: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="e.g. 'Jan 5 — Fascinate OS sync' or 'AI Engineering Coaching Month 3'",
    )
    quantity: Mapped[Any] = mapped_column(
        Numeric(precision=10, scale=4, asdecimal=True),
        nullable=False,
        comment="Units (hours, months, etc.)",
    )
    unit_price: Mapped[Any] = mapped_column(
        Numeric(precision=12, scale=2, asdecimal=True),
        nullable=False,
        comment="Rate per unit",
    )
    total_price: Mapped[Any] = mapped_column(
        Numeric(precision=12, scale=2, asdecimal=True),
        nullable=False,
        comment="quantity * unit_price — stored explicitly for audit stability",
    )

    # ── Calendar session metadata ──────────────────────────────────────────────
    date: Mapped[str | None] = mapped_column(
        String(10),
        nullable=True,
        comment="ISO date of the calendar session (null for flat-rate line items)",
    )
    sort_order: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="Display order on the invoice",
    )

    def __repr__(self) -> str:
        id_prefix = self.id[:8] if self.id else "unsaved"
        return (
            f"<InvoiceLineItem id={id_prefix} "
            f"description={self.description!r} total={self.total_price}>"
        )
