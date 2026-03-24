"""Transaction ORM model — core register entry for every financial event."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Numeric, String, Text  # Float kept for confidence/deductible_pct
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from src.models.base import Base
from src.models.enums import (
    ConfirmedBy,
    Direction,
    Entity,
    Source,
    TaxCategory,
    TaxSubcategory,
    TransactionStatus,
)


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Transaction(Base):
    """Single financial transaction in the accounting register.

    Amounts: positive = income, negative = expense.
    Stored as NUMERIC (text-backed in SQLite) to avoid floating-point error.
    UUIDs stored as TEXT (SQLite has no native UUID type).
    """

    __tablename__ = "transactions"

    # ── CHECK constraints on enum columns ─────────────────────────────────────
    _entity_values = "', '".join(e.value for e in Entity)
    _status_values = "', '".join(s.value for s in TransactionStatus)
    _direction_values = "', '".join(d.value for d in Direction)
    _tax_category_values = "', '".join(c.value for c in TaxCategory)

    __table_args__ = (
        CheckConstraint(
            f"entity IN ('{_entity_values}') OR entity IS NULL",
            name="ck_transaction_entity",
        ),
        CheckConstraint(
            f"status IN ('{_status_values}')",
            name="ck_transaction_status",
        ),
        CheckConstraint(
            f"direction IN ('{_direction_values}') OR direction IS NULL",
            name="ck_transaction_direction",
        ),
        CheckConstraint(
            f"tax_category IN ('{_tax_category_values}') OR tax_category IS NULL",
            name="ck_transaction_tax_category",
        ),
    )

    # ── Identity ──────────────────────────────────────────────────────────────
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)

    # ── Source provenance ──────────────────────────────────────────────────────
    source: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Originating adapter (Source enum value)",
    )
    source_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="Original ID from the source system",
    )
    source_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        index=True,
        comment="SHA256(source, source_id) — primary dedup key",
    )

    # ── Core fields ────────────────────────────────────────────────────────────
    date: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        index=True,
        comment="ISO date YYYY-MM-DD",
    )
    description: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Vendor / payee / payer as received from source",
    )
    amount: Mapped[Any] = mapped_column(
        Numeric(precision=12, scale=2, asdecimal=True),
        nullable=True,
        comment="Positive = income, negative = expense; NULL means amount unknown",
    )
    currency: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
        default="USD",
        server_default="USD",
    )

    # ── Foreign currency tracking ────────────────────────────────────────────
    currency_code: Mapped[str | None] = mapped_column(
        String(3),
        nullable=True,
        default=None,
        comment="ISO 4217 code (GBP, EUR, etc.) when original amount is non-USD. None means USD.",
    )
    amount_foreign: Mapped[Any] = mapped_column(
        Numeric(precision=18, scale=6, asdecimal=True),
        nullable=True,
        default=None,
        comment="Original amount in foreign currency (always positive, sign on amount field)",
    )
    exchange_rate: Mapped[Any] = mapped_column(
        Numeric(precision=18, scale=8, asdecimal=True),
        nullable=True,
        default=None,
        comment="Exchange rate used: foreign_amount * rate = USD amount",
    )
    exchange_rate_source: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        default=None,
        comment="frankfurter_api | credit_card_statement | manual",
    )

    # ── Classification ──────────────────────────────────────────────────────────
    entity: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        index=True,
        comment="Entity enum value: sparkry | blackline | personal",
    )
    direction: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        comment="Direction enum value",
    )
    tax_category: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        index=True,
        comment="TaxCategory enum value",
    )
    tax_subcategory: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="TaxSubcategory enum value",
    )
    deductible_pct: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=1.0,
        server_default="1.0",
        comment="1.0 default, 0.5 for meals, 0.0 for personal non-deductible",
    )

    # ── Status & confidence ────────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default=TransactionStatus.NEEDS_REVIEW.value,
        index=True,
        comment="TransactionStatus enum value",
    )
    confidence: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0.0,
        server_default="0.0",
        comment="Classification confidence 0.0–1.0",
    )
    review_reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Human-readable explanation of why this needs review",
    )

    # ── Split / reimbursement links ────────────────────────────────────────────
    parent_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("transactions.id"),
        nullable=True,
        comment="Parent transaction UUID for split line items",
    )
    reimbursement_link: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("transactions.id"),
        nullable=True,
        comment="Links an expense to its reimbursement payment",
    )

    # ── 1099 tracking ─────────────────────────────────────────────────────────
    payer_1099: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        default=None,
        comment="Name of 1099 payer (e.g. 'Cardinal Health Inc') for income documentation",
    )
    payer_1099_type: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        default=None,
        comment="1099 form type: NEC, MISC, K, etc.",
    )

    # ── Payment method ─────────────────────────────────────────────────────────
    payment_method: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="e.g. 'VISA ****5482' — last-4 for reconciliation with statements",
    )

    # ── Rich data ──────────────────────────────────────────────────────────────
    attachments: Mapped[Any] = mapped_column(
        JSON,
        nullable=True,
        comment="List of file paths to PDFs, images, JSON source files",
    )
    raw_data: Mapped[Any] = mapped_column(
        JSON,
        nullable=False,
        comment="Original source record verbatim — never modified after insert",
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
    confirmed_by: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        default=ConfirmedBy.AUTO.value,
        comment="auto | human",
    )
    notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Free-form human notes",
    )

    # ── Convenience properties ─────────────────────────────────────────────────

    @property
    def entity_enum(self) -> Entity | None:
        return Entity(self.entity) if self.entity else None

    @property
    def direction_enum(self) -> Direction | None:
        return Direction(self.direction) if self.direction else None

    @property
    def tax_category_enum(self) -> TaxCategory | None:
        return TaxCategory(self.tax_category) if self.tax_category else None

    @property
    def tax_subcategory_enum(self) -> TaxSubcategory | None:
        return TaxSubcategory(self.tax_subcategory) if self.tax_subcategory else None

    @property
    def status_enum(self) -> TransactionStatus:
        return TransactionStatus(self.status)

    @property
    def source_enum(self) -> Source:
        return Source(self.source)

    def __repr__(self) -> str:
        id_prefix = self.id[:8] if self.id else "unsaved"
        return (
            f"<Transaction id={id_prefix} date={self.date} "
            f"amount={self.amount} entity={self.entity} status={self.status}>"
        )
