"""Tests for GET /api/planning/runs/latest.

REQ-PLAN-011.

Auth note: when the API_KEY env var is unset (the test default), require_api_key
is a no-op — no header is required.  See src/api/auth.py for the bypass logic.
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Generator
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from src.api.deps import get_db
from src.models.base import Base
from src.planning.models import PlanningRun  # noqa: F401 — registers table in Base


def _make_engine() -> Any:
    """Per-test isolated in-memory SQLite engine."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture()
def client() -> Generator[tuple[TestClient, Any], None, None]:
    """TestClient with isolated in-memory DB wired via dependency override."""
    from src.api import main as _main_module

    engine = _make_engine()
    Sess = sessionmaker(bind=engine, expire_on_commit=False)

    def _override_get_db() -> Generator[Session, None, None]:
        s = Sess()
        try:
            yield s
        finally:
            s.close()

    with (
        patch.object(_main_module, "init_db", return_value=None),
        patch.object(_main_module, "seed_vendor_rules", return_value=0),
        patch.object(_main_module, "seed_customers", return_value={
            "customers_inserted": 0, "customers_updated": 0, "invoices_inserted": 0,
        }),
    ):
        from src.api.main import app

        app.dependency_overrides[get_db] = _override_get_db
        with TestClient(app) as tc:
            yield tc, Sess
        app.dependency_overrides.clear()


def test_latest_returns_404_when_empty(client: tuple[TestClient, Any]) -> None:
    """REQ-PLAN-011: 404 when no planning runs exist."""
    tc, _ = client
    resp = tc.get("/api/planning/runs/latest")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "no planning runs yet"


def test_latest_returns_most_recent(client: tuple[TestClient, Any]) -> None:
    """REQ-PLAN-011: returns the most recent run when multiple exist."""
    tc, Sess = client
    with Sess() as s:
        old = PlanningRun(
            run_at=dt.datetime(2026, 1, 1),
            source="cli",
            params_json={},
            live_inputs_json={},
            scenarios_json={"baseline_ret8_horizon85": {"survival": 0.80}},
            notes=None,
        )
        new = PlanningRun(
            run_at=dt.datetime(2026, 6, 1),
            source="scheduled",
            params_json={},
            live_inputs_json={},
            scenarios_json={"baseline_ret8_horizon85": {"survival": 0.86}},
            notes="month tick",
        )
        s.add_all([old, new])
        s.commit()

    resp = tc.get("/api/planning/runs/latest")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["source"] == "scheduled"
    assert payload["notes"] == "month tick"
    assert payload["scenarios"]["baseline_ret8_horizon85"]["survival"] == 0.86
    # Sanity-check other fields are present
    assert "id" in payload
    assert "run_at" in payload
    assert "params" in payload
    assert "live_inputs" in payload
