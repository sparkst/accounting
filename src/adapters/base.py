"""Abstract base class for all data-source adapters.

REQ-ID: ADAPTER-001  Every adapter exposes a uniform ``run()`` interface.
REQ-ID: ADAPTER-002  Per-record error isolation — one bad record must not
                     halt the batch.

Design spec: docs/superpowers/specs/2026-03-15-accounting-system-design.md §Per-Adapter Error Handling
"""

from __future__ import annotations

import logging
import traceback
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from src.models.enums import IngestionStatus

logger = logging.getLogger(__name__)


@dataclass
class AdapterResult:
    """Summary of a single adapter run returned by :meth:`BaseAdapter.run`.

    Attributes:
        source:            The ``Source`` enum value identifying the adapter.
        status:            Overall run outcome.
        records_processed: Number of source records successfully processed
                           (includes skipped-as-duplicate records).
        records_created:   Number of new ``Transaction`` rows inserted.
        records_skipped:   Number of records skipped because they were already
                           present in the register (dedup).
        records_failed:    Number of records that raised an exception and were
                           logged but skipped.
        errors:            List of ``(record_id, error_message)`` tuples for
                           every failed record.
        run_at:            UTC timestamp when the run started.
    """

    source: str
    status: IngestionStatus = IngestionStatus.SUCCESS
    records_processed: int = 0
    records_created: int = 0
    records_skipped: int = 0
    records_failed: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    run_at: datetime = field(default_factory=lambda: datetime.now(UTC).replace(tzinfo=None))

    def record_error(self, record_id: str, exc: Exception) -> None:
        """Log a per-record failure and update counters/status."""
        self.records_failed += 1
        self.errors.append((record_id, traceback.format_exc()))
        logger.error(
            "Adapter %s failed on record %r: %s",
            self.source,
            record_id,
            exc,
            exc_info=True,
        )
        # Degrade overall status but do not stop — partial is still useful.
        if self.status == IngestionStatus.SUCCESS:
            self.status = IngestionStatus.PARTIAL_FAILURE


class BaseAdapter(ABC):
    """Contract that every data-source adapter must implement.

    Subclasses override :meth:`run` and use ``AdapterResult`` to report
    outcomes.  The public interface is intentionally minimal; all
    adapter-specific configuration lives in ``__init__``.
    """

    @property
    @abstractmethod
    def source(self) -> str:
        """Return the ``Source`` enum value for this adapter (e.g. ``"gmail_n8n"``).

        Used to populate ``transactions.source`` and ``ingested_files.adapter``.
        """

    @abstractmethod
    def run(self, session: Session) -> AdapterResult:
        """Execute a full ingestion pass.

        The adapter reads its data source, deduplicates against the SQLite
        register, inserts new ``Transaction`` rows, and records an
        ``IngestedFile`` entry for every file processed.

        Args:
            session: An open SQLAlchemy ``Session``.  The adapter is
                     responsible for calling ``session.commit()`` after each
                     successful record so that partial progress is preserved on
                     failure.

        Returns:
            :class:`AdapterResult` summarising the run.
        """
