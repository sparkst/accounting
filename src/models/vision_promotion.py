"""VisionPromotion ORM model — the per-institution promotion ledger (REQ-VIS-003).

Tracks how many consecutive equal-or-better shadow cycles an institution's
vision extractor has produced. At 3, the CLI declares the institution eligible
for promotion; the flip to ``promoted=true`` is a manual, qdecide-gated command
(never an automatic code path). Additive table only.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class VisionPromotion(Base):
    """One row per institution tracking shadow-cycle cleanliness + promotion."""

    __tablename__ = "vision_promotion"

    institution: Mapped[str] = mapped_column(
        String(32),
        primary_key=True,
        comment="Institution key: fg | gsk | nw_mutual | ft | na_iul",
    )
    consecutive_clean: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="Consecutive equal-or-better shadow cycles; resets to 0 on any dirty run",
    )
    last_cycle_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_report_path: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        comment="Path to the most recent shadow diff report",
    )
    promoted: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
        comment="True once vision is primary for this institution (legacy stays fallback)",
    )
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decision_ref: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="qdecide decision reference recorded at promotion",
    )

    def __repr__(self) -> str:
        return (
            f"<VisionPromotion {self.institution} clean={self.consecutive_clean} "
            f"promoted={self.promoted}>"
        )
