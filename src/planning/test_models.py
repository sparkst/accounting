"""Tests for PlanningRun model — schema + JSON round-trip."""
from __future__ import annotations

import datetime as dt
from collections.abc import Generator
from typing import Any

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from src.db.connection import _configure_sqlite
from src.planning.models import VALID_SOURCES, PlanningRun


@pytest.fixture
def in_memory_engine() -> Generator[Any, None, None]:
    """SQLite in-memory engine with planning_runs table."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    event.listen(engine, "connect", _configure_sqlite)
    # Only create PlanningRun table — suppress FK resolution errors for other models
    PlanningRun.__table__.create(engine, checkfirst=True)  # type: ignore[attr-defined]
    yield engine
    engine.dispose()


@pytest.fixture
def session(in_memory_engine: Any) -> Generator[Session, None, None]:
    factory = sessionmaker(bind=in_memory_engine, autocommit=False, autoflush=False)
    with factory() as s:
        yield s


def test_planning_run_round_trip(session: Session) -> None:
    """Write a PlanningRun, read it back — JSON columns preserve nesting."""
    row = PlanningRun(
        run_at=dt.datetime(2026, 6, 1, 6, 0, 0),
        source="cli",
        params_json={"pool_taxable": 6_300_000.0, "ret_mean": 0.08, "nested": {"k": 1}},
        live_inputs_json={"pool_taxable": 6_350_000.0, "ttm_spend": 240_000.0},
        scenarios_json={
            "baseline_ret8_horizon85": {
                "survival": 0.86,
                "owed": 0.0,
                "percentiles": {"85": [0.0, 28_000_000.0, 136_000_000.0]},
            }
        },
        notes="initial test",
    )
    session.add(row)
    session.commit()

    fetched = session.query(PlanningRun).one()
    assert fetched.params_json["pool_taxable"] == 6_300_000.0
    assert fetched.params_json["nested"]["k"] == 1
    assert fetched.scenarios_json["baseline_ret8_horizon85"]["survival"] == 0.86
    assert fetched.source == "cli"
    assert fetched.notes == "initial test"


def test_planning_run_source_constrained() -> None:
    """Source must be one of 'cli' | 'scheduled' | 'api' — enforced at app level."""
    # Application-level validation only; SQLite doesn't enforce. Just confirm
    # the constant set exists for the CLI to validate against.
    assert frozenset({"cli", "scheduled", "api"}) == VALID_SOURCES
