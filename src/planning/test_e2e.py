"""End-to-end smoke: fixture DB → CLI simulate (persist) → API → JSON.

REQ-PLAN-015.

Full pipeline: copy fixture DB, run CLI simulate to persist a planning run,
spin up FastAPI TestClient, fetch the run via API, verify JSON matches.
"""
from __future__ import annotations

import shutil
from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.api.deps import get_db
from src.models.base import Base
from src.planning.cli import main as cli_main
from src.planning.models import PlanningRun  # noqa: F401 — registers table in Base

FIXTURE_DB = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "planning" / "accounting.fixture.db"


def test_e2e_simulate_then_api(tmp_path: Path) -> None:
    """Full pipeline: persist a run via CLI, fetch it back via API.

    REQ-PLAN-015: End-to-end smoke test exercising the complete path.
    """
    # Step 1: Copy fixture DB and ensure planning_runs table exists
    db_path = tmp_path / "e2e.db"
    shutil.copy(FIXTURE_DB, db_path)
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    Sess = sessionmaker(bind=engine)

    # Step 2: CLI persists a run
    with patch("src.planning.cli._open_session") as open_sess:
        open_sess.return_value = Sess()
        rc = cli_main(["simulate", "--n-sims", "300", "--note", "e2e"])
        assert rc == 0

    # Step 3: Verify the row was written to the DB
    with Session(engine) as s:
        rows = s.query(PlanningRun).all()
        assert len(rows) == 1
        assert rows[0].notes == "e2e"

    # Step 4: API reads it back via TestClient with patched lifespan
    def override_get_db() -> Generator[Session, None, None]:
        s = Sess()
        try:
            yield s
        finally:
            s.close()

    from src.api import main as _main_module

    with (
        patch.object(_main_module, "init_db", return_value=None),
        patch.object(_main_module, "seed_vendor_rules", return_value=0),
        patch.object(_main_module, "seed_customers", return_value={
            "customers_inserted": 0, "customers_updated": 0, "invoices_inserted": 0,
        }),
    ):
        from src.api.main import app

        app.dependency_overrides[get_db] = override_get_db
        try:
            tc = TestClient(app)
            resp = tc.get("/api/planning/runs/latest")
            assert resp.status_code == 200, resp.text
            payload = resp.json()

            # Verify all expected fields are present and correct
            assert payload["source"] == "cli"
            assert payload["notes"] == "e2e"
            assert len(payload["scenarios"]) == 15

            # Spot-check scenario structure (baseline_ret8_horizon85 should exist)
            baseline = payload["scenarios"]["baseline_ret8_horizon85"]
            assert 0.0 <= baseline["survival"] <= 1.0
            assert "percentiles" in baseline
            assert isinstance(baseline["percentiles"], dict)

            # Spot-check a percentile value (keyed by age as string, values are [min, median, max])
            percentile_keys = list(baseline["percentiles"].keys())
            assert len(percentile_keys) > 0
            sample_age = percentile_keys[0]
            sample_value = baseline["percentiles"][sample_age]
            assert isinstance(sample_value, list)
            assert len(sample_value) == 3  # [min, median, max]
            assert all(isinstance(v, (int, float)) for v in sample_value)

            # Verify live_inputs are present and make sense
            assert "live_inputs" in payload
            assert payload["live_inputs"]["pool_taxable"] > 0
            assert payload["live_inputs"]["ttm_spend"] > 0

            # Verify other top-level fields
            assert "id" in payload
            assert "run_at" in payload
            assert "params" in payload
        finally:
            app.dependency_overrides.clear()
