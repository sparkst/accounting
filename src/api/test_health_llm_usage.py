"""Tests for LLM usage section of GET /api/health.

REQ-ID: LLM-HEALTH-001  /api/health includes llm_usage with zero counts when table is empty.
REQ-ID: LLM-HEALTH-002  /api/health aggregates calls, tokens, and cost for the current month.
REQ-ID: LLM-HEALTH-003  /api/health excludes LLM usage rows from previous months.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

import src.db.connection as _conn  # noqa: F401 — registers all models on Base.metadata
from src.models.base import Base
from src.models.llm_usage import LLMUsageLog, estimate_cost

# ---------------------------------------------------------------------------
# Shared-cache in-memory database (same pattern as test_api.py)
# ---------------------------------------------------------------------------

_TEST_DB_URI = "file:health_llm_test?mode=memory&cache=shared&uri=true"

_test_engine = create_engine(
    "sqlite+pysqlite:///" + _TEST_DB_URI.replace("file:", ""),
    connect_args={"check_same_thread": False, "uri": True},
)


@event.listens_for(_test_engine, "connect")
def _set_pragmas(conn: Any, _record: Any) -> None:
    cursor = conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


Base.metadata.create_all(bind=_test_engine)

_TestSession = sessionmaker(bind=_test_engine, autocommit=False, autoflush=False)


@pytest.fixture(autouse=True)
def clean_db() -> Generator[None, None, None]:
    with _test_engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
        conn.execute(text("PRAGMA foreign_keys=ON"))
    yield


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    from src.api import main as _main_module
    from src.api.routes import health as _health_module
    from src.api.routes import ingest as _ingest_module
    from src.api.routes import transactions as _tx_module

    with (
        patch.object(_tx_module, "SessionLocal", _TestSession),
        patch.object(_health_module, "SessionLocal", _TestSession),
        patch.object(_ingest_module, "SessionLocal", _TestSession),
        patch.object(_main_module, "init_db", return_value=None),
        patch.object(_main_module, "seed_vendor_rules", return_value=0),
    ):
        from src.api.main import app

        with TestClient(app) as c:
            yield c


@pytest.fixture()
def db_session() -> Generator[Session, None, None]:
    s = _TestSession()
    try:
        yield s
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_usage(
    session: Session,
    *,
    model: str = "claude-3-5-haiku-20241022",
    input_tokens: int = 300,
    output_tokens: int = 100,
    duration_ms: int = 500,
    timestamp: datetime | None = None,
) -> LLMUsageLog:
    entry = LLMUsageLog(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_estimate=estimate_cost(input_tokens, output_tokens),
        duration_ms=duration_ms,
    )
    if timestamp is not None:
        entry.timestamp = timestamp
    session.add(entry)
    session.commit()
    return entry


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_health_llm_usage_zero_when_empty(client: TestClient) -> None:
    """REQ-ID: LLM-HEALTH-001 — llm_usage present with zeros when no calls recorded."""
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "llm_usage" in data
    lu = data["llm_usage"]
    assert lu["calls_this_month"] == 0
    assert lu["total_tokens"] == 0
    assert lu["estimated_cost_usd"] == 0.0


def test_health_llm_usage_aggregates_this_month(
    client: TestClient, db_session: Session
) -> None:
    """REQ-ID: LLM-HEALTH-002 — aggregates counts correctly for current month."""
    now = datetime.now(UTC).replace(tzinfo=None)
    _insert_usage(db_session, input_tokens=500, output_tokens=200, timestamp=now)
    _insert_usage(db_session, input_tokens=300, output_tokens=100, timestamp=now)

    resp = client.get("/api/health")
    assert resp.status_code == 200
    lu = resp.json()["llm_usage"]

    assert lu["calls_this_month"] == 2
    assert lu["total_input_tokens"] == 800
    assert lu["total_output_tokens"] == 300
    assert lu["total_tokens"] == 1100
    expected_cost = estimate_cost(500, 200) + estimate_cost(300, 100)
    assert abs(lu["estimated_cost_usd"] - expected_cost) < 1e-5


def test_health_llm_usage_excludes_prior_months(
    client: TestClient, db_session: Session
) -> None:
    """REQ-ID: LLM-HEALTH-003 — rows from last month are not counted."""
    now = datetime.now(UTC).replace(tzinfo=None)
    this_month = now

    # A row from last month
    year = now.year if now.month > 1 else now.year - 1
    month = now.month - 1 if now.month > 1 else 12
    last_month = now.replace(year=year, month=month, day=1)

    _insert_usage(db_session, input_tokens=1000, output_tokens=500, timestamp=last_month)
    _insert_usage(db_session, input_tokens=200, output_tokens=80, timestamp=this_month)

    resp = client.get("/api/health")
    lu = resp.json()["llm_usage"]

    # Only the current-month row should be counted
    assert lu["calls_this_month"] == 1
    assert lu["total_input_tokens"] == 200
    assert lu["total_output_tokens"] == 80
