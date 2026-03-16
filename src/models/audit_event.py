"""AuditEvent ORM model — immutable edit history for every human action."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class AuditEvent(Base):
    """Append-only record of every change made to a Transaction.

    One row per field changed per action. Enables full undo history and
    audit trail for tax purposes.
    """

    __tablename__ = "audit_events"

    # ── Identity ──────────────────────────────────────────────────────────────
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)

    # ── What changed ──────────────────────────────────────────────────────────
    transaction_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("transactions.id"),
        nullable=False,
        index=True,
        comment="UUID of the transaction that was modified",
    )
    field_changed: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Name of the field that was changed (e.g., entity, tax_category)",
    )
    old_value: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Previous value as a string (None if field was unset)",
    )
    new_value: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="New value as a string",
    )

    # ── Who / when ────────────────────────────────────────────────────────────
    changed_by: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        comment="human | auto",
    )
    changed_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=_now,
        index=True,
    )

    def __repr__(self) -> str:
        return (
            f"<AuditEvent tx={self.transaction_id[:8]} "
            f"field={self.field_changed} "
            f"{self.old_value!r} → {self.new_value!r} by={self.changed_by}>"
        )
