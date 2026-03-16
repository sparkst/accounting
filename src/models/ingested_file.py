"""IngestedFile ORM model — tracks which source files have been processed."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from src.models.base import Base
from src.models.enums import FileStatus


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class IngestedFile(Base):
    """Records every source file (CSV, JSON webhook payload, etc.) that has
    been ingested so adapters can skip already-processed files on re-run.

    Replaces the old ``processed_files.json`` sidecar approach.
    """

    __tablename__ = "ingested_files"

    # ── Identity ──────────────────────────────────────────────────────────────
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)

    # ── File identification ────────────────────────────────────────────────────
    file_path: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Absolute path to the source file on disk",
    )
    file_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        index=True,
        comment="SHA256 of file contents — used for exact dedup",
    )

    # ── Processing metadata ────────────────────────────────────────────────────
    adapter: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        index=True,
        comment="Name of the adapter that processed this file",
    )
    processed_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=_now,
    )
    status: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        default=FileStatus.SUCCESS.value,
        comment="FileStatus: success | failed | skipped",
    )

    # ── Output ────────────────────────────────────────────────────────────────
    transaction_ids: Mapped[Any] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        comment="List of transaction UUIDs created from this file",
    )

    # ── Convenience ───────────────────────────────────────────────────────────

    @property
    def status_enum(self) -> FileStatus:
        return FileStatus(self.status)

    def __repr__(self) -> str:
        return (
            f"<IngestedFile adapter={self.adapter} "
            f"file={self.file_path!r} status={self.status}>"
        )
