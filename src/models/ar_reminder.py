"""ArReminder ORM model — draft-for-approval state for the AR chaser (REQ-ARC-*).

One row per (invoice, rung) reminder in the 14/30/45-day ladder. The row holds
the drafted email body, the single-use approval token, and the approval state
machine so *nothing sends to a customer without an explicit approval*
(REQ-ARC-001). Additive table only — touches no protected table.

State machine (guarded at the application layer, exactly-once by UNIQUE):
    drafted → pending_approval → approved → sent
    (any) → dismissed
    failed → pending_approval (retryable)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base

# Reminder-ladder rungs, in days past sent_at.
AR_RUNGS = (14, 30, 45)

# Status values for the approval state machine.
AR_STATUS_DRAFTED = "drafted"
AR_STATUS_PENDING_APPROVAL = "pending_approval"
AR_STATUS_APPROVED = "approved"
AR_STATUS_SENT = "sent"
AR_STATUS_DISMISSED = "dismissed"
AR_STATUS_FAILED = "failed"
_AR_STATUS_VALUES = (
    AR_STATUS_DRAFTED,
    AR_STATUS_PENDING_APPROVAL,
    AR_STATUS_APPROVED,
    AR_STATUS_SENT,
    AR_STATUS_DISMISSED,
    AR_STATUS_FAILED,
)


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class ArReminder(Base):
    """A single AR-reminder draft awaiting (and after) approval."""

    __tablename__ = "ar_reminder"

    _status_values = "', '".join(_AR_STATUS_VALUES)
    _rung_values = ", ".join(str(r) for r in AR_RUNGS)

    __table_args__ = (
        UniqueConstraint("invoice_id", "rung", name="uq_ar_reminder_invoice_rung"),
        CheckConstraint(
            f"rung IN ({_rung_values})",
            name="ck_ar_reminder_rung",
        ),
        CheckConstraint(
            f"status IN ('{_status_values}')",
            name="ck_ar_reminder_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    invoice_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("invoices.id"),
        nullable=False,
        index=True,
        comment="FK to the unpaid invoice this reminder chases",
    )
    rung: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Ladder rung in days past sent_at: 14 | 30 | 45",
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=AR_STATUS_DRAFTED,
        comment="drafted | pending_approval | approved | sent | dismissed | failed",
    )
    draft_subject: Mapped[str] = mapped_column(Text, nullable=False)
    draft_body: Mapped[str] = mapped_column(Text, nullable=False)
    approval_token: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        default=_new_uuid,
        comment="Single-use token verified by the Telegram callback endpoint",
    )
    approved_via: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        comment="telegram | cli — how the approval was granted",
    )
    resend_message_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="Resend message id recorded after a successful send",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_now
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_now, onupdate=_now
    )

    def __repr__(self) -> str:
        id_prefix = self.id[:8] if self.id else "unsaved"
        return (
            f"<ArReminder id={id_prefix} invoice={self.invoice_id[:8]} "
            f"rung={self.rung} status={self.status}>"
        )
