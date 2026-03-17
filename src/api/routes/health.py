"""Health check endpoint.

GET /api/health — Returns:
  - source_freshness: last IngestionLog per source with freshness color,
    last_run_at, ingestion status, cumulative record counts, and last error.
  - classification_stats: transaction counts by status with percentages and
    pending count.
  - tax_deadlines: upcoming deadlines with days_until_due.
  - failure_log: recent ingestion failures with source and error detail.
  - total_transactions: total transaction count across all statuses.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.db.connection import SessionLocal
from src.models.enums import Source, TransactionStatus
from src.models.ingestion_log import IngestionLog
from src.models.transaction import Transaction
from src.utils.staleness import compute_source_freshness

router = APIRouter(tags=["health"])

# All known sources — shown even if no ingestion run has occurred yet.
_ALL_SOURCES: list[str] = [s.value for s in Source]

# ---------------------------------------------------------------------------
# Tax deadlines
# ---------------------------------------------------------------------------

_CURRENT_YEAR = date.today().year


def _build_tax_deadlines(today: date | None = None) -> list[dict]:
    """Return upcoming tax deadlines ordered by due date.

    Only deadlines within the next 180 days are included.  Past deadlines are
    omitted so the dashboard stays focused on actionable items.
    """
    if today is None:
        today = date.today()

    y = today.year  # current calendar year

    # Deadlines that recur annually; we generate both current and next year
    # so the list stays populated year-round.
    raw: list[tuple[str, str, str]] = [
        # (label, entity, ISO date)
        ("WA B&O (Sparkry, Monthly)", "sparkry", f"{y}-{today.month:02d}-25"),
        ("WA B&O (Sparkry, Monthly)", "sparkry", f"{y}-{(today.month % 12) + 1:02d}-25"),
        ("Q1 Estimated Tax (Federal)", "all", f"{y}-04-15"),
        ("Q2 Estimated Tax (Federal)", "all", f"{y}-06-16"),
        ("Q3 Estimated Tax (Federal)", "all", f"{y}-09-15"),
        ("Q4 Estimated Tax (Federal)", "all", f"{y + 1}-01-15"),
        ("WA B&O (BlackLine, Q1)", "blackline", f"{y}-04-30"),
        ("WA B&O (BlackLine, Q2)", "blackline", f"{y}-07-31"),
        ("WA B&O (BlackLine, Q3)", "blackline", f"{y}-10-31"),
        ("WA B&O (BlackLine, Q4)", "blackline", f"{y + 1}-01-31"),
        ("Schedule C / 1040 Due", "sparkry", f"{y}-04-15"),
        ("Form 1065 / K-1 Due (BlackLine)", "blackline", f"{y}-03-15"),
        ("Form 1065 Extension Deadline", "blackline", f"{y}-09-16"),
        ("1040 Extension Deadline", "all", f"{y}-10-15"),
    ]

    window = timedelta(days=180)
    deadlines = []
    seen: set[tuple[str, str]] = set()  # deduplicate (label, due_date)

    for label, entity, due_str in raw:
        try:
            due = date.fromisoformat(due_str)
        except ValueError:
            continue
        key = (label, due_str)
        if key in seen:
            continue
        seen.add(key)
        if today <= due <= today + window:
            days_until = (due - today).days
            deadlines.append(
                {
                    "label": label,
                    "entity": entity,
                    "due_date": due_str,
                    "days_until_due": days_until,
                }
            )

    deadlines.sort(key=lambda d: d["due_date"])
    return deadlines


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


class SourceFreshnessOut(BaseModel):
    """Freshness snapshot for a single ingestion source."""

    source: str
    last_run_at: datetime | None
    freshness_status: str  # "green" | "amber" | "red" | "never"
    ingestion_status: str | None
    records_processed: int
    records_failed: int
    last_error: str | None


class ClassificationStatsOut(BaseModel):
    """Transaction counts grouped by status, with percentages."""

    needs_review: int
    auto_classified: int
    confirmed: int
    split_parent: int
    rejected: int
    total: int
    # Percentages (0–100, rounded to 1 dp)
    auto_confirmed_pct: float
    edited_pct: float
    pending_pct: float
    rejected_pct: float
    pending_count: int


class TaxDeadlineOut(BaseModel):
    """A single upcoming tax deadline."""

    label: str
    entity: str
    due_date: str  # ISO date YYYY-MM-DD
    days_until_due: int


class FailureLogEntry(BaseModel):
    """A single recent ingestion failure."""

    source: str
    run_at: datetime
    ingestion_status: str
    error_detail: str | None
    records_processed: int
    records_failed: int


class HealthResponse(BaseModel):
    """Full health check response."""

    ok: bool
    source_freshness: list[SourceFreshnessOut]
    classification_stats: ClassificationStatsOut
    tax_deadlines: list[TaxDeadlineOut]
    failure_log: list[FailureLogEntry]
    total_transactions: int
    needs_review_count: int
    checked_at: datetime


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.get("/health", response_model=HealthResponse)
def get_health(session: Session = Depends(get_db)) -> HealthResponse:  # noqa: B008
    """Return system health: source freshness, classification stats, tax deadlines, and failure log."""
    try:
        # ── Source freshness ──────────────────────────────────────────────────
        now = datetime.now(UTC).replace(tzinfo=None)

        freshness_data = compute_source_freshness(session, _ALL_SOURCES, now=now)

        freshness_out: list[SourceFreshnessOut] = [
            SourceFreshnessOut(
                source=sf.source,
                last_run_at=sf.last_run_at,
                freshness_status=sf.freshness_status,
                ingestion_status=sf.ingestion_status,
                records_processed=sf.records_processed,
                records_failed=sf.records_failed,
                last_error=sf.last_error,
            )
            for sf in freshness_data
        ]

        # ── Classification stats ───────────────────────────────────────────────
        all_statuses: list[str] = [
            row[0] for row in session.query(Transaction.status).all()
        ]
        total = len(all_statuses)

        needs_review = sum(
            1 for s in all_statuses if s == TransactionStatus.NEEDS_REVIEW.value
        )
        auto_classified = sum(
            1 for s in all_statuses if s == TransactionStatus.AUTO_CLASSIFIED.value
        )
        confirmed = sum(
            1 for s in all_statuses if s == TransactionStatus.CONFIRMED.value
        )
        split_parent = sum(
            1 for s in all_statuses if s == TransactionStatus.SPLIT_PARENT.value
        )
        rejected = sum(
            1 for s in all_statuses if s == TransactionStatus.REJECTED.value
        )

        def _pct(count: int) -> float:
            return round(count / total * 100, 1) if total > 0 else 0.0

        stats = ClassificationStatsOut(
            needs_review=needs_review,
            auto_classified=auto_classified,
            confirmed=confirmed,
            split_parent=split_parent,
            rejected=rejected,
            total=total,
            auto_confirmed_pct=_pct(auto_classified + confirmed + split_parent),
            edited_pct=_pct(confirmed),
            pending_pct=_pct(needs_review),
            rejected_pct=_pct(rejected),
            pending_count=needs_review,
        )

        # ── Tax deadlines ─────────────────────────────────────────────────────
        raw_deadlines = _build_tax_deadlines()
        tax_deadlines = [TaxDeadlineOut(**d) for d in raw_deadlines]

        # ── Failure log — 20 most recent failures ─────────────────────────────
        recent_failures: list[IngestionLog] = (
            session.query(IngestionLog)
            .filter(
                IngestionLog.status.in_(["partial_failure", "failure"])
            )
            .order_by(IngestionLog.run_at.desc())
            .limit(20)
            .all()
        )

        failure_log: list[FailureLogEntry] = [
            FailureLogEntry(
                source=lg.source,
                run_at=lg.run_at,
                ingestion_status=lg.status,
                error_detail=lg.error_detail,
                records_processed=lg.records_processed,
                records_failed=lg.records_failed,
            )
            for lg in recent_failures
        ]

        return HealthResponse(
            ok=True,
            source_freshness=freshness_out,
            classification_stats=stats,
            tax_deadlines=tax_deadlines,
            failure_log=failure_log,
            total_transactions=total,
            needs_review_count=needs_review,
            checked_at=now,
        )
    finally:
        session.close()
