"""TaxYearLock ORM model — prevents edits to transactions in filed tax years."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class TaxYearLock(Base):
    """A lock record that freezes a (entity, year) pair against edits.

    Once a tax year has been filed, a lock is created so that no further
    PATCH / split / bulk-confirm operations can mutate transactions dated
    within that year for that entity.  Locks can be removed by an admin
    (DELETE /api/tax-year-locks/{id}) if an amendment is required.

    Constraint: UNIQUE(entity, year) — only one lock per entity-year pair.
    """

    __tablename__ = "tax_year_locks"
    __table_args__ = (
        UniqueConstraint("entity", "year", name="uq_tax_year_lock_entity_year"),
    )

    # ── Identity ──────────────────────────────────────────────────────────────
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)

    # ── Lock scope ─────────────────────────────────────────────────────────────
    entity: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        index=True,
        comment="Entity enum value: sparkry | blackline | personal",
    )
    year: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="4-digit calendar year that is locked (e.g. 2024)",
    )

    # ── Audit ──────────────────────────────────────────────────────────────────
    locked_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=_now,
        comment="When the lock was created",
    )
    locked_by: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="human",
        comment="Who created the lock (free-form label, e.g. 'human' or a user identifier)",
    )

    def __repr__(self) -> str:
        return f"<TaxYearLock entity={self.entity} year={self.year} locked_by={self.locked_by!r}>"
