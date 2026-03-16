"""IngestionLog ORM model — one row per adapter run, success or failure."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base
from src.models.enums import IngestionStatus


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class IngestionLog(Base):
    """Audit log for every adapter execution.

    Failures include the full stack trace in ``error_detail`` so they can be
    diagnosed and retried. Retryable failures get exponential back-off (3
    attempts before the dashboard surface an alert).
    """

    __tablename__ = "ingestion_log"

    # ── Identity ──────────────────────────────────────────────────────────────
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)

    # ── Run context ────────────────────────────────────────────────────────────
    source: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        index=True,
        comment="Adapter identifier (Source enum value)",
    )
    run_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=_now,
        index=True,
    )

    # ── Outcome ────────────────────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="IngestionStatus: success | partial_failure | failure",
    )
    records_processed: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    records_failed: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    error_detail: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Full stack trace or human-readable error for failures",
    )

    # ── Retry tracking ─────────────────────────────────────────────────────────
    retryable: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
        comment="True when the failure is transient and safe to retry",
    )
    retried_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        comment="Timestamp of the most recent retry attempt",
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        comment="Timestamp when the failure was resolved (null = still open)",
    )

    # ── Convenience ───────────────────────────────────────────────────────────

    @property
    def status_enum(self) -> IngestionStatus:
        return IngestionStatus(self.status)

    def __repr__(self) -> str:
        return (
            f"<IngestionLog source={self.source} "
            f"status={self.status} "
            f"processed={self.records_processed} failed={self.records_failed}>"
        )
