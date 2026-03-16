"""VendorRule ORM model — account memory for deterministic classification."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base
from src.models.enums import (
    Direction,
    Entity,
    TaxCategory,
    TaxSubcategory,
    VendorRuleSource,
)


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class VendorRule(Base):
    """A classification rule scoped to a (vendor_pattern, entity) pair.

    A single vendor may have multiple rules — one per entity — because the
    same payee can be used by both Sparkry and BlackLine. When multiple rules
    match, the classification engine ranks by ``examples`` descending then
    ``confidence`` descending.
    """

    __tablename__ = "vendor_rules"

    # ── Identity ──────────────────────────────────────────────────────────────
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)

    # ── Matching ───────────────────────────────────────────────────────────────
    vendor_pattern: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Regex or exact string matched against sender/description",
    )
    entity: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        index=True,
        comment="Entity enum value this rule applies to",
    )

    # ── Classification defaults ────────────────────────────────────────────────
    tax_category: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="TaxCategory enum value",
    )
    tax_subcategory: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="TaxSubcategory enum value (optional refinement)",
    )
    direction: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="Direction enum value: income | expense | reimbursable",
    )
    deductible_pct: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=1.0,
        server_default="1.0",
        comment="Default deductible percentage applied to matched transactions",
    )

    # ── Rule metadata ──────────────────────────────────────────────────────────
    confidence: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=1.0,
        server_default="1.0",
        comment="Rule confidence; >= 0.8 triggers auto-classification",
    )
    source: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        default=VendorRuleSource.HUMAN.value,
        comment="VendorRuleSource: human | learned",
    )
    examples: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
        comment="Number of transactions this rule has matched — used for ranking",
    )
    last_matched: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        comment="Timestamp of the most recent match",
    )

    # ── Audit ──────────────────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=_now,
    )

    # ── Convenience properties ─────────────────────────────────────────────────

    @property
    def entity_enum(self) -> Entity:
        return Entity(self.entity)

    @property
    def tax_category_enum(self) -> TaxCategory:
        return TaxCategory(self.tax_category)

    @property
    def tax_subcategory_enum(self) -> TaxSubcategory | None:
        return TaxSubcategory(self.tax_subcategory) if self.tax_subcategory else None

    @property
    def direction_enum(self) -> Direction:
        return Direction(self.direction)

    @property
    def source_enum(self) -> VendorRuleSource:
        return VendorRuleSource(self.source)

    def __repr__(self) -> str:
        return (
            f"<VendorRule pattern={self.vendor_pattern!r} "
            f"entity={self.entity} category={self.tax_category} "
            f"confidence={self.confidence}>"
        )
