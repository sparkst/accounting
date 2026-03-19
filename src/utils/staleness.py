"""Source freshness utilities.

REQ-ID: HEALTH-001  Source freshness: green < 24 hr, amber 1–7 d, red > 7 d / never.

Queries IngestionLog to compute per-source freshness status, last run time,
and cumulative record counts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from src.models.ingestion_log import IngestionLog  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GREEN_THRESHOLD = timedelta(hours=24)
_AMBER_THRESHOLD = timedelta(days=7)


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------


@dataclass
class SourceFreshness:
    """Freshness snapshot for a single ingestion source."""

    source: str
    last_run_at: datetime | None
    freshness_status: str  # "green" | "amber" | "red" | "never"
    ingestion_status: str | None  # most recent IngestionStatus value
    records_processed: int  # total across all runs
    records_failed: int  # total across all runs
    last_error: str | None  # error_detail from the most recent failed run


# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------


def compute_source_freshness(
    session: Session,
    sources: list[str],
    *,
    now: datetime | None = None,
) -> list[SourceFreshness]:
    """Return freshness snapshots for all known sources.

    Args:
        session:  SQLAlchemy session (caller owns lifecycle).
        sources:  List of source identifier strings to include (e.g. all
                  ``Source`` enum values).  Sources with no log entries get
                  a ``"never"`` status.
        now:      Override for current UTC time (useful in tests).

    Returns:
        One ``SourceFreshness`` per entry in ``sources``, in the same order.
    """
    if now is None:
        now = datetime.now(UTC).replace(tzinfo=None)

    all_logs: list[IngestionLog] = session.query(IngestionLog).all()

    # Group logs by source
    logs_by_source: dict[str, list[IngestionLog]] = {}
    for log in all_logs:
        logs_by_source.setdefault(log.source, []).append(log)

    result: list[SourceFreshness] = []
    for source in sources:
        source_logs = logs_by_source.get(source, [])

        if not source_logs:
            result.append(
                SourceFreshness(
                    source=source,
                    last_run_at=None,
                    freshness_status="never",
                    ingestion_status=None,
                    records_processed=0,
                    records_failed=0,
                    last_error=None,
                )
            )
            continue

        # Latest run (for freshness / status)
        latest = max(source_logs, key=lambda lg: lg.run_at)

        # Cumulative totals
        total_processed = sum(lg.records_processed for lg in source_logs)
        total_failed = sum(lg.records_failed for lg in source_logs)

        # Freshness colour
        age = now - latest.run_at
        if age <= _GREEN_THRESHOLD:
            freshness_status = "green"
        elif age <= _AMBER_THRESHOLD:
            freshness_status = "amber"
        else:
            freshness_status = "red"

        # Most recent error detail (from most recent failure run, if any)
        failed_logs = [
            lg for lg in source_logs if lg.status in ("partial_failure", "failure")
        ]
        last_error: str | None = None
        if failed_logs:
            most_recent_failure = max(failed_logs, key=lambda lg: lg.run_at)
            last_error = most_recent_failure.error_detail

        result.append(
            SourceFreshness(
                source=source,
                last_run_at=latest.run_at,
                freshness_status=freshness_status,
                ingestion_status=latest.status,
                records_processed=total_processed,
                records_failed=total_failed,
                last_error=last_error,
            )
        )

    return result
