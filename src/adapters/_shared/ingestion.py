"""Shared IngestionLog writer for Phase-4 adapters.

All Phase-4 adapters write an IngestionLog row at the end of every apply run.
This helper centralises that logic so the 6 adapters stay DRY.

Usage::

    from src.adapters._shared.ingestion import write_ingestion_log
    from src.models.enums import IngestionStatus

    write_ingestion_log(
        session,
        source="my_adapter",
        records_processed=result.imported + result.dup_skipped,
        records_failed=len(result.errors),
        status=IngestionStatus.SUCCESS,
        error_detail=None,
    )

Failures inside the writer are swallowed — a log-write failure must never
mask the real result. The adapter must still commit its data rows before
calling this helper.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING

from src.models.enums import IngestionStatus
from src.models.ingestion_log import IngestionLog

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def write_ingestion_log(
    session: Session,
    *,
    source: str,
    records_processed: int,
    records_failed: int,
    status: IngestionStatus,
    error_detail: str | None = None,
) -> None:
    """Write one :class:`IngestionLog` row and commit it.

    Args:
        session:           SQLAlchemy session (must be open).
        source:            Value for ``ingestion_log.source``
                           (usually the adapter's ``ADAPTER_NAME`` constant).
        records_processed: Total rows attempted (imported + dup_skipped).
        records_failed:    Count of per-record errors (``len(result.errors)``).
        status:            :class:`IngestionStatus` outcome.
        error_detail:      Optional free-text summary (errors, warnings, etc.).

    Any exception raised during the write is caught, logged, and suppressed —
    a logging failure must never mask the main import result.
    """
    try:
        log = IngestionLog(
            source=source,
            status=status.value,
            records_processed=records_processed,
            records_failed=records_failed,
            error_detail=error_detail,
        )
        session.add(log)
        session.commit()
    except Exception:  # noqa: BLE001
        logger.exception("failed to write IngestionLog for %s", source)
        with contextlib.suppress(Exception):
            session.rollback()
