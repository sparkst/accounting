"""Health check endpoint.

GET /api/health — Returns:
  - source_freshness: last IngestionLog per source with timestamp and status.
  - classification_stats: transaction count broken down by status.
  - total_transactions: total transaction count across all statuses.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.db.connection import SessionLocal
from src.models.ingestion_log import IngestionLog
from src.models.transaction import Transaction

router = APIRouter(tags=["health"])


# ---------------------------------------------------------------------------
# Dependency
# ---------------------------------------------------------------------------


def get_db() -> Session:
    """Yield a database session, closing it when the request is done."""
    session = SessionLocal()
    try:
        return session
    except Exception:
        session.close()
        raise


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class SourceFreshness(BaseModel):
    """Freshness snapshot for a single ingestion source."""

    source: str
    last_run_at: datetime | None
    status: str | None
    records_processed: int
    records_failed: int


class ClassificationStats(BaseModel):
    """Transaction counts grouped by status."""

    needs_review: int
    auto_classified: int
    confirmed: int
    split_parent: int
    rejected: int


class HealthResponse(BaseModel):
    """Full health check response."""

    ok: bool
    source_freshness: list[SourceFreshness]
    classification_stats: ClassificationStats
    total_transactions: int
    checked_at: datetime


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.get("/health", response_model=HealthResponse)
def get_health(session: Session = Depends(get_db)) -> HealthResponse:  # noqa: B008
    """Return system health: source freshness and classification statistics."""
    try:
        # ── Source freshness ──────────────────────────────────────────────────
        # One row per distinct source — most recent run for each.
        all_logs: list[IngestionLog] = session.query(IngestionLog).all()

        latest_by_source: dict[str, IngestionLog] = {}
        for log in all_logs:
            existing = latest_by_source.get(log.source)
            if existing is None or log.run_at > existing.run_at:
                latest_by_source[log.source] = log

        freshness: list[SourceFreshness] = [
            SourceFreshness(
                source=log.source,
                last_run_at=log.run_at,
                status=log.status,
                records_processed=log.records_processed,
                records_failed=log.records_failed,
            )
            for log in latest_by_source.values()
        ]

        # ── Classification stats ───────────────────────────────────────────────
        all_statuses: list[str] = [
            row[0] for row in session.query(Transaction.status).all()
        ]

        stats = ClassificationStats(
            needs_review=sum(1 for s in all_statuses if s == "needs_review"),
            auto_classified=sum(1 for s in all_statuses if s == "auto_classified"),
            confirmed=sum(1 for s in all_statuses if s == "confirmed"),
            split_parent=sum(1 for s in all_statuses if s == "split_parent"),
            rejected=sum(1 for s in all_statuses if s == "rejected"),
        )

        total = len(all_statuses)

        return HealthResponse(
            ok=True,
            source_freshness=freshness,
            classification_stats=stats,
            total_transactions=total,
            checked_at=datetime.now(UTC).replace(tzinfo=None),
        )
    finally:
        session.close()
