"""Tests for src/utils/staleness.py.

REQ-ID: HEALTH-001  Source freshness: green < 24 hr, amber 1–7 d, red > 7 d / never.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

import src.db.connection as _conn  # noqa: F401 — registers all ORM models
from src.models.base import Base
from src.models.enums import IngestionStatus, Source
from src.models.ingestion_log import IngestionLog
from src.utils.staleness import compute_source_freshness

# ---------------------------------------------------------------------------
# In-memory test database
# ---------------------------------------------------------------------------

_TEST_DB_URI = "file:staleness_test?mode=memory&cache=shared&uri=true"

_engine = create_engine(
    "sqlite+pysqlite:///" + _TEST_DB_URI.replace("file:", ""),
    connect_args={"check_same_thread": False, "uri": True},
)


@event.listens_for(_engine, "connect")
def _set_pragmas(conn: Any, _record: Any) -> None:
    cursor = conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


Base.metadata.create_all(bind=_engine)
_Session = sessionmaker(bind=_engine, autocommit=False, autoflush=False)


@pytest.fixture(autouse=True)
def clean_db():
    from sqlalchemy import text
    with _engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
        conn.execute(text("PRAGMA foreign_keys=ON"))
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 3, 16, 12, 0, 0)  # fixed reference point


def _add_log(
    session,
    *,
    source: str,
    run_at: datetime,
    status: str = IngestionStatus.SUCCESS.value,
    records_processed: int = 5,
    records_failed: int = 0,
    error_detail: str | None = None,
) -> IngestionLog:
    log = IngestionLog(
        source=source,
        run_at=run_at,
        status=status,
        records_processed=records_processed,
        records_failed=records_failed,
        error_detail=error_detail,
        retryable=False,
    )
    session.add(log)
    session.commit()
    return log


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestComputeSourceFreshness:
    def test_never_when_no_logs(self):
        with _Session() as session:
            result = compute_source_freshness(
                session, [Source.STRIPE.value], now=_NOW
            )
        assert len(result) == 1
        sf = result[0]
        assert sf.source == Source.STRIPE.value
        assert sf.freshness_status == "never"
        assert sf.last_run_at is None
        assert sf.records_processed == 0

    def test_green_when_recent(self):
        run_at = _NOW - timedelta(hours=2)
        with _Session() as session:
            _add_log(session, source=Source.GMAIL_N8N.value, run_at=run_at)
            result = compute_source_freshness(
                session, [Source.GMAIL_N8N.value], now=_NOW
            )
        assert result[0].freshness_status == "green"

    def test_boundary_green_at_24h(self):
        run_at = _NOW - timedelta(hours=24)
        with _Session() as session:
            _add_log(session, source=Source.SHOPIFY.value, run_at=run_at)
            result = compute_source_freshness(
                session, [Source.SHOPIFY.value], now=_NOW
            )
        assert result[0].freshness_status == "green"

    def test_amber_when_1_to_7_days(self):
        run_at = _NOW - timedelta(days=3)
        with _Session() as session:
            _add_log(session, source=Source.STRIPE.value, run_at=run_at)
            result = compute_source_freshness(
                session, [Source.STRIPE.value], now=_NOW
            )
        assert result[0].freshness_status == "amber"

    def test_red_when_over_7_days(self):
        run_at = _NOW - timedelta(days=10)
        with _Session() as session:
            _add_log(session, source=Source.BANK_CSV.value, run_at=run_at)
            result = compute_source_freshness(
                session, [Source.BANK_CSV.value], now=_NOW
            )
        assert result[0].freshness_status == "red"

    def test_cumulative_record_counts(self):
        with _Session() as session:
            _add_log(
                session,
                source=Source.GMAIL_N8N.value,
                run_at=_NOW - timedelta(hours=1),
                records_processed=10,
                records_failed=1,
            )
            _add_log(
                session,
                source=Source.GMAIL_N8N.value,
                run_at=_NOW - timedelta(hours=2),
                records_processed=5,
                records_failed=0,
            )
            result = compute_source_freshness(
                session, [Source.GMAIL_N8N.value], now=_NOW
            )
        sf = result[0]
        assert sf.records_processed == 15
        assert sf.records_failed == 1

    def test_last_error_from_most_recent_failure(self):
        with _Session() as session:
            _add_log(
                session,
                source=Source.SHOPIFY.value,
                run_at=_NOW - timedelta(hours=5),
                status=IngestionStatus.FAILURE.value,
                error_detail="old error",
            )
            _add_log(
                session,
                source=Source.SHOPIFY.value,
                run_at=_NOW - timedelta(hours=1),
                status=IngestionStatus.FAILURE.value,
                error_detail="recent error",
            )
            result = compute_source_freshness(
                session, [Source.SHOPIFY.value], now=_NOW
            )
        assert result[0].last_error == "recent error"

    def test_no_last_error_when_all_success(self):
        with _Session() as session:
            _add_log(
                session,
                source=Source.STRIPE.value,
                run_at=_NOW - timedelta(hours=1),
                status=IngestionStatus.SUCCESS.value,
            )
            result = compute_source_freshness(
                session, [Source.STRIPE.value], now=_NOW
            )
        assert result[0].last_error is None

    def test_multiple_sources_returned_in_order(self):
        sources = [Source.GMAIL_N8N.value, Source.STRIPE.value]
        with _Session() as session:
            _add_log(
                session,
                source=Source.GMAIL_N8N.value,
                run_at=_NOW - timedelta(hours=1),
            )
            # STRIPE has no log
            result = compute_source_freshness(session, sources, now=_NOW)
        assert [sf.source for sf in result] == sources

    def test_uses_latest_log_for_status(self):
        """Even if the most recent log is a failure, freshness is based on its age."""
        with _Session() as session:
            _add_log(
                session,
                source=Source.SHOPIFY.value,
                run_at=_NOW - timedelta(hours=1),
                status=IngestionStatus.FAILURE.value,
            )
            result = compute_source_freshness(
                session, [Source.SHOPIFY.value], now=_NOW
            )
        sf = result[0]
        assert sf.freshness_status == "green"  # recent, even though failed
        assert sf.ingestion_status == IngestionStatus.FAILURE.value
