"""TaxDocument ORM model — received tax documents (1099s, K-1s, 1098s, property tax)."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from src.models.base import Base
from src.models.enums import Entity, TaxDocumentStatus, TaxFormType


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class TaxDocument(Base):
    """Received tax document (1099, K-1, 1098, property tax statement).

    Stores form metadata and per-box amounts as JSON.
    UUIDs stored as TEXT (SQLite has no native UUID type).
    """

    __tablename__ = "tax_documents"

    # ── CHECK constraints on enum columns ─────────────────────────────────────
    _entity_values = "', '".join(e.value for e in Entity)
    _form_type_values = "', '".join(f.value for f in TaxFormType)
    _status_values = "', '".join(s.value for s in TaxDocumentStatus)

    __table_args__ = (
        CheckConstraint(
            f"entity IN ('{_entity_values}')",
            name="ck_tax_document_entity",
        ),
        CheckConstraint(
            f"form_type IN ('{_form_type_values}')",
            name="ck_tax_document_form_type",
        ),
        CheckConstraint(
            f"status IN ('{_status_values}')",
            name="ck_tax_document_status",
        ),
        UniqueConstraint(
            "tax_year", "form_type", "entity", "payer_ein",
            name="uq_tax_doc_natural_key",
        ),
    )

    # ── Identity ──────────────────────────────────────────────────────────────
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)

    # ── Document classification ────────────────────────────────────────────────
    tax_year: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        index=True,
        comment="Tax year, e.g. 2025",
    )
    form_type: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="TaxFormType enum value",
    )
    entity: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        index=True,
        comment="Entity enum value: sparkry | blackline | personal",
    )

    # ── Payer / recipient ──────────────────────────────────────────────────────
    payer_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="e.g. 'FC International Education LLC'",
    )
    payer_ein: Mapped[str | None] = mapped_column(
        String(10),
        nullable=True,
        comment="e.g. '85-1499443'",
    )
    recipient_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="e.g. 'Travis Sparks'",
    )
    recipient_tin_last4: Mapped[str | None] = mapped_column(
        String(4),
        nullable=True,
        comment="Last 4 of SSN/EIN for verification",
    )

    # ── Amounts ────────────────────────────────────────────────────────────────
    amounts: Mapped[Any] = mapped_column(
        JSON,
        nullable=False,
        comment="Form-specific box values keyed by box name",
    )
    total_amount: Mapped[Any] = mapped_column(
        Numeric(precision=12, scale=2, asdecimal=True),
        nullable=False,
        comment="Primary/headline amount (form-type-specific primary box)",
    )

    # ── Source file ────────────────────────────────────────────────────────────
    source_file: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Relative path from project root, e.g. data/tax-docs/2025/personal/...",
    )

    # ── Status & notes ─────────────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=TaxDocumentStatus.ACTIVE.value,
        server_default=TaxDocumentStatus.ACTIVE.value,
        comment="TaxDocumentStatus enum value",
    )
    notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Free-form notes",
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
    def form_type_enum(self) -> TaxFormType:
        return TaxFormType(self.form_type)

    @property
    def entity_enum(self) -> Entity:
        return Entity(self.entity)

    @property
    def status_enum(self) -> TaxDocumentStatus:
        return TaxDocumentStatus(self.status)

    def __repr__(self) -> str:
        id_prefix = self.id[:8] if self.id else "unsaved"
        return (
            f"<TaxDocument id={id_prefix} year={self.tax_year} "
            f"form={self.form_type} entity={self.entity} payer={self.payer_name!r}>"
        )
