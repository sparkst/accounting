"""Tests for the planning CLI."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.models.base import Base
from src.planning.cli import main as cli_main
from src.planning.models import PlanningRun

FIXTURE_DB = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "planning" / "accounting.fixture.db"


@pytest.fixture
def db_with_planning_table(tmp_path: Path) -> Path:
    """Copy the fixture DB and add the planning_runs table for write tests."""
    import shutil

    target = tmp_path / "test.db"
    shutil.copy(FIXTURE_DB, target)
    engine = create_engine(f"sqlite:///{target}")
    Base.metadata.create_all(engine)  # ensures planning_runs table exists
    return target


def test_simulate_writes_exactly_one_row(db_with_planning_table: Path) -> None:
    """REQ-PLAN-009: one simulate invocation → exactly one PlanningRun row."""
    with patch("src.planning.cli._open_session") as open_session:
        engine = create_engine(f"sqlite:///{db_with_planning_table}")
        Sess = sessionmaker(bind=engine)
        open_session.return_value = Sess()

        # --as-of pins the TTM window to the fixture DB's era (12x$20k spanning
        # 2025-07-06..2026-06-01); without it the expected 240k decays as months
        # age out of a real-today window.
        rc = cli_main(["simulate", "--n-sims", "300", "--as-of", "2026-06-15"])
        assert rc == 0

    # Verify exactly one row
    engine = create_engine(f"sqlite:///{db_with_planning_table}")
    with Session(engine) as s:
        rows = s.query(PlanningRun).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.source == "cli"
        # All 15 scenarios should be in scenarios_json
        assert len(row.scenarios_json) == 15
        # live_inputs_json should have the fixture-derived values
        assert row.live_inputs_json["pool_taxable"] > 0
        assert row.live_inputs_json["ttm_spend"] == pytest.approx(240_000.0, abs=1.0)


def test_simulate_dry_run_writes_nothing(db_with_planning_table: Path) -> None:
    """REQ-PLAN-010: --dry-run does not persist."""
    with patch("src.planning.cli._open_session") as open_session:
        engine = create_engine(f"sqlite:///{db_with_planning_table}")
        Sess = sessionmaker(bind=engine)
        open_session.return_value = Sess()

        rc = cli_main(["simulate", "--dry-run", "--n-sims", "300"])
        assert rc == 0

    engine = create_engine(f"sqlite:///{db_with_planning_table}")
    with Session(engine) as s:
        assert s.query(PlanningRun).count() == 0


def test_simulate_override_unknown_key_exits_nonzero(
    db_with_planning_table: Path,
) -> None:
    """REQ-PLAN-010: unknown override key → non-zero exit with helpful message."""
    with patch("src.planning.cli._open_session") as open_session:
        engine = create_engine(f"sqlite:///{db_with_planning_table}")
        Sess = sessionmaker(bind=engine)
        open_session.return_value = Sess()

        rc = cli_main(
            ["simulate", "--override", "bogus_field=1", "--n-sims", "100"]
        )
        assert rc != 0


def test_simulate_with_note_persists_it(db_with_planning_table: Path) -> None:
    """REQ-PLAN-010: --note tags the persisted run."""
    with patch("src.planning.cli._open_session") as open_session:
        engine = create_engine(f"sqlite:///{db_with_planning_table}")
        Sess = sessionmaker(bind=engine)
        open_session.return_value = Sess()

        rc = cli_main(["simulate", "--note", "after-Schwab-rebalance", "--n-sims", "300"])
        assert rc == 0

    engine = create_engine(f"sqlite:///{db_with_planning_table}")
    with Session(engine) as s:
        row = s.query(PlanningRun).one()
        assert row.notes == "after-Schwab-rebalance"
