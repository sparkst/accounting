"""AuditEvent ORM model — immutable edit history for every human action.

REQ-029: extended with ``entity_id`` + ``entity_type`` so Plaid lifecycle events
(connect/disconnect/map/relink) can be audited without attaching a fake
transaction. A CHECK constraint enforces exactly-one-of (transaction_id, entity_id).
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# Allowed ``entity_type`` values for the non-transaction path. Kept open
# enough to extend without a migration; the application layer is responsible
# for using a known constant.
ENTITY_TYPE_PLAID_ITEM = "plaid_item"
ENTITY_TYPE_ACCOUNT = "account"


class AuditEvent(Base):
    """Append-only record of every audit-worthy change.

    Two modes:
    - **Transaction mode** (legacy): ``transaction_id`` is set, ``entity_id``/
      ``entity_type`` are NULL. One row per field changed.
    - **Entity mode** (REQ-029): ``entity_id`` + ``entity_type`` are set,
      ``transaction_id`` is NULL. Used for Plaid lifecycle (connect, disconnect,
      map, unmap, relink) and for Account-level changes outside Transactions.

    CHECK enforces exactly-one-of so a row cannot reference both or neither.
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint(
            "(transaction_id IS NOT NULL AND entity_id IS NULL AND entity_type IS NULL) "
            "OR (transaction_id IS NULL AND entity_id IS NOT NULL AND entity_type IS NOT NULL)",
            name="ck_audit_events_exactly_one_target",
        ),
        Index("ix_audit_events_entity", "entity_type", "entity_id"),
    )

    # ── Identity ──────────────────────────────────────────────────────────────
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)

    # ── What changed: transaction-mode target ────────────────────────────────
    transaction_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("transactions.id"),
        nullable=True,
        index=True,
        comment="UUID of the transaction that was modified (transaction mode)",
    )

    # ── What changed: entity-mode target ─────────────────────────────────────
    entity_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        comment="UUID of the non-transaction entity that was modified (entity mode)",
    )
    entity_type: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="Entity type discriminator: 'plaid_item' | 'account' | ...",
    )

    field_changed: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Name of the field that was changed (e.g., entity, tax_category, plaid_link)",
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
        target = (
            f"tx={self.transaction_id[:8]}"
            if self.transaction_id
            else f"{self.entity_type}={self.entity_id[:8] if self.entity_id else '?'}"
        )
        return (
            f"<AuditEvent {target} "
            f"field={self.field_changed} "
            f"{self.old_value!r} → {self.new_value!r} by={self.changed_by}>"
        )
