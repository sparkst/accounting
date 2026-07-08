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
from typing import TYPE_CHECKING, Protocol

from src.models.enums import IngestionStatus
from src.models.ingestion_log import IngestionLog

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class _CloudImportResultLike(Protocol):
    """Structural type for ``write_cloud_ingestion_log``'s ``result`` arg.

    Every adapter's cloud-mode result type — whether it subclasses the shared
    ``BaseImportResult`` (most Phase-4 adapters) or defines its own dataclass
    (``xlsx_savings_plan.ImportResult``, ``brokerage_csv.CloudImportResult``)
    — carries these two fields with these semantics. Protocol (structural)
    typing here avoids forcing every adapter's result dataclass to share a
    common base class just to satisfy this one helper.
    """

    imported: int
    errors: list[str]


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


def write_cloud_ingestion_log(
    session: Session | None,
    *,
    source: str,
    result: _CloudImportResultLike,
) -> None:
    """Write ONE local IngestionLog row summarizing a cloud-mode push
    (REQ-FIX-WLT-007).

    Every cloud-mode importer run gets exactly one local IngestionLog row —
    same as the local-write path — so cloud pushes surface in delivery-health
    exactly like local imports. No-op when ``session`` is None (keeps the
    ``*_cloud`` functions importable/callable without a DB, e.g. from the
    n8n/agent callers that only pass a session when one is available).

    status derivation mirrors the local-write convention:
    * FAILURE          — errors present and nothing imported.
    * PARTIAL_FAILURE  — some imported, but errors also present.
    * SUCCESS          — no errors.
    """
    if session is None:
        return
    if result.errors and result.imported == 0:
        status = IngestionStatus.FAILURE
    elif result.errors:
        status = IngestionStatus.PARTIAL_FAILURE
    else:
        status = IngestionStatus.SUCCESS
    write_ingestion_log(
        session,
        source=source,
        records_processed=result.imported,
        records_failed=len(result.errors),
        status=status,
        error_detail="\n".join(result.errors) or None,
    )
