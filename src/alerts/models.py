"""AlertDispatch — the dedup + audit ledger for dispatched EA alerts.

One row per (alert_key, occurrence_date) actually dispatched.  The UNIQUE
constraint is the dedup guarantee: re-running the daily job never sends the same
alert twice.  Additive table only — touches no protected table.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _now_iso() -> str:
    # naive UTC string, matches other models
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


class AlertDispatch(Base):
    __tablename__ = "alert_dispatch"
    __table_args__ = (
        UniqueConstraint(
            "alert_key", "occurrence_date", name="uq_alert_dispatch_key_date"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    alert_key: Mapped[str] = mapped_column(String, nullable=False, index=True)
    occurrence_date: Mapped[str] = mapped_column(String(10), nullable=False)
    alert_type: Mapped[str] = mapped_column(String, nullable=False)
    entity: Mapped[str] = mapped_column(String, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)  # sent|failed|dry_run
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_now_iso)
    # REQ-FIX-ALR-002: payload_json is the exact dict handed to httpx.post, stored
    # so a failed n8n_webhook row can be replayed byte-for-byte by the sweep.
    # delivery_channel discriminates 'n8n_webhook' (swept) from 'resend_email'
    # (never swept). Both NULL on rows written before this migration (legacy).
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivery_channel: Mapped[str | None] = mapped_column(String, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<AlertDispatch {self.alert_key}@{self.occurrence_date} "
            f"status={self.status}>"
        )
