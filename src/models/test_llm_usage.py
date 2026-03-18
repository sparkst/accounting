"""Tests for LLMUsageLog model and cost estimation helper.

REQ-ID: LLM-USAGE-001  LLMUsageLog persists to database and round-trips correctly.
REQ-ID: LLM-USAGE-002  estimate_cost computes correct USD amounts per published pricing.
REQ-ID: LLM-USAGE-003  estimate_cost handles zero tokens without error.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from src.db.connection import _configure_sqlite
from src.models.base import Base
from src.models.llm_usage import LLMUsageLog, estimate_cost

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine() -> Generator[Any, None, None]:
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    event.listen(eng, "connect", _configure_sqlite)
    Base.metadata.create_all(bind=eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Any) -> Generator[Session, None, None]:
    Session_ = sessionmaker(bind=engine)
    s = Session_()
    try:
        yield s
    finally:
        s.close()


# ---------------------------------------------------------------------------
# estimate_cost
# ---------------------------------------------------------------------------


def test_estimate_cost_zero_tokens() -> None:
    """REQ-ID: LLM-USAGE-003"""
    assert estimate_cost(0, 0) == 0.0


def test_estimate_cost_input_only() -> None:
    """REQ-ID: LLM-USAGE-002 — 1M input tokens = $3.00"""
    cost = estimate_cost(1_000_000, 0)
    assert abs(cost - 3.0) < 1e-9


def test_estimate_cost_output_only() -> None:
    """REQ-ID: LLM-USAGE-002 — 1M output tokens = $15.00"""
    cost = estimate_cost(0, 1_000_000)
    assert abs(cost - 15.0) < 1e-9


def test_estimate_cost_mixed() -> None:
    """REQ-ID: LLM-USAGE-002 — 500k input + 100k output."""
    # 500_000 * 3 / 1_000_000 + 100_000 * 15 / 1_000_000
    # = 1.50 + 1.50 = 3.00
    expected = 500_000 * 3 / 1_000_000 + 100_000 * 15 / 1_000_000
    assert abs(estimate_cost(500_000, 100_000) - expected) < 1e-9


def test_estimate_cost_small_call() -> None:
    """Typical single Haiku call — ~300 in / ~100 out."""
    cost = estimate_cost(300, 100)
    expected = 300 * 3e-6 + 100 * 15e-6
    assert abs(cost - expected) < 1e-9


# ---------------------------------------------------------------------------
# LLMUsageLog persistence
# ---------------------------------------------------------------------------


def test_llm_usage_log_persists(session: Session) -> None:
    """REQ-ID: LLM-USAGE-001 — row round-trips through SQLite."""
    entry = LLMUsageLog(
        model="claude-3-5-haiku-20241022",
        input_tokens=500,
        output_tokens=120,
        cost_estimate=estimate_cost(500, 120),
        duration_ms=843,
    )
    session.add(entry)
    session.commit()

    fetched = session.query(LLMUsageLog).one()
    assert fetched.model == "claude-3-5-haiku-20241022"
    assert fetched.input_tokens == 500
    assert fetched.output_tokens == 120
    assert fetched.duration_ms == 843
    assert fetched.cost_estimate > 0
    assert fetched.id is not None
    assert isinstance(fetched.timestamp, datetime)


def test_llm_usage_log_defaults(session: Session) -> None:
    """Columns with defaults work when not provided explicitly."""
    entry = LLMUsageLog(model="claude-3-5-haiku-20241022")
    session.add(entry)
    session.commit()

    fetched = session.query(LLMUsageLog).one()
    assert fetched.input_tokens == 0
    assert fetched.output_tokens == 0
    assert fetched.cost_estimate == 0.0
    assert fetched.duration_ms == 0


def test_llm_usage_log_repr(session: Session) -> None:
    entry = LLMUsageLog(
        model="claude-3-5-haiku-20241022",
        input_tokens=100,
        output_tokens=50,
        cost_estimate=estimate_cost(100, 50),
    )
    r = repr(entry)
    assert "claude-3-5-haiku" in r
    assert "in=100" in r
    assert "out=50" in r
