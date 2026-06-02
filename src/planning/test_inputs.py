"""Tests for live-input loaders against the planning fixture DB.

Covers: REQ-PLAN-005, REQ-PLAN-007, REQ-PLAN-013, REQ-PLAN-014, REQ-PLAN-019.
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Generator
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.planning.inputs import load_live

FIXTURE_DB = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "planning"
    / "accounting.fixture.db"
)


@pytest.fixture
def session() -> Generator[Session, None, None]:
    assert FIXTURE_DB.exists(), (
        f"fixture missing — run: python tests/fixtures/planning/build_fixture_db.py\n"
        f"Expected at: {FIXTURE_DB}"
    )
    engine = create_engine(f"sqlite:///{FIXTURE_DB}")
    with Session(engine) as s:
        yield s


def test_load_live_pool_taxable_sums_taxable_accounts(session: Session) -> None:
    """REQ-PLAN-005: pool_taxable = sum of latest balances for TAXABLE accounts."""
    live = load_live(session, today=dt.date(2026, 6, 1))
    # Fixture has 1 taxable broker ($6,300,000) + 1 checking ($50,000)
    assert live.pool_taxable == pytest.approx(6_350_000.0, abs=0.01)


def test_load_live_pool_retirement_sums_retirement_accounts(session: Session) -> None:
    """REQ-PLAN-005: pool_retirement = sum of latest balances for retirement accounts."""
    live = load_live(session, today=dt.date(2026, 6, 1))
    # Fixture has 1 trad_ira ($1,500,000)
    assert live.pool_retirement == pytest.approx(1_500_000.0, abs=0.01)


def test_load_live_ttm_spend(session: Session) -> None:
    """TTM personal expense across 12 months × $20k = $240k."""
    live = load_live(session, today=dt.date(2026, 6, 1))
    assert live.ttm_spend == pytest.approx(240_000.0, abs=1.0)


def test_load_live_ttm_biz_income(session: Session) -> None:
    """TTM sparkry-entity income across 12 months × $26,666.67 ≈ $320k."""
    live = load_live(session, today=dt.date(2026, 6, 1))
    assert live.ttm_biz_income == pytest.approx(320_000.0, abs=1.0)


def test_load_live_ttm_personal_income(session: Session) -> None:
    """REQ-PLAN-019: TTM personal-entity income credits (Amy wage proxy)."""
    live = load_live(session, today=dt.date(2026, 6, 1))
    assert live.ttm_personal_income == pytest.approx(80_000.0, abs=1.0)


def test_load_live_staleness_warning_when_old(session: Session) -> None:
    """REQ-PLAN-013: if latest snapshot >7d old, staleness_warning is populated."""
    # Pretend "today" is 30 days after the most recent fixture snapshot (2026-06-01).
    future_today = dt.date(2026, 7, 1)
    live = load_live(session, today=future_today)
    assert live.staleness_warning is not None
    assert "day" in live.staleness_warning


def test_load_live_no_snapshots_raises(tmp_path: Path) -> None:
    """REQ-PLAN-014: missing wealth data → hard fail with actionable message."""
    from sqlalchemy import text

    empty_db = tmp_path / "empty.db"
    engine = create_engine(f"sqlite:///{empty_db}")
    # SQLite does not enforce FKs by default, so we can create just the tables
    # that load_live queries without having to create all referenced tables
    # (e.g. plaid_item). Use raw DDL to avoid SQLAlchemy's FK resolution pass.
    with engine.connect() as conn:
        conn.execute(text("PRAGMA foreign_keys = OFF"))
        conn.execute(text(
            "CREATE TABLE account ("
            "  id TEXT PRIMARY KEY,"
            "  broker TEXT NOT NULL,"
            "  account_number TEXT NOT NULL,"
            "  account_name TEXT,"
            "  account_type TEXT NOT NULL,"
            "  entity TEXT NOT NULL DEFAULT 'personal',"
            "  tax_sheltered INTEGER NOT NULL DEFAULT 0,"
            "  is_plan_wrapper INTEGER NOT NULL DEFAULT 0,"
            "  plaid_item_id TEXT,"
            "  plaid_account_id TEXT,"
            "  payment_method TEXT,"
            "  beneficiary TEXT,"
            "  notes TEXT,"
            "  parent_account_id TEXT,"
            "  created_at TEXT NOT NULL DEFAULT (datetime('now')),"
            "  updated_at TEXT NOT NULL DEFAULT (datetime('now'))"
            ")"
        ))
        conn.execute(text(
            "CREATE TABLE account_balance_snapshot ("
            "  id TEXT PRIMARY KEY,"
            "  account_id TEXT,"
            "  raw_account_name TEXT NOT NULL,"
            "  as_of TEXT NOT NULL,"
            "  balance NUMERIC NOT NULL,"
            "  source TEXT NOT NULL,"
            "  source_row_hash TEXT NOT NULL,"
            "  created_at TEXT NOT NULL DEFAULT (datetime('now'))"
            ")"
        ))
        conn.execute(text(
            "CREATE TABLE transactions ("
            "  id TEXT PRIMARY KEY,"
            "  source TEXT NOT NULL,"
            "  source_hash TEXT NOT NULL UNIQUE,"
            "  date TEXT NOT NULL,"
            "  description TEXT NOT NULL,"
            "  amount NUMERIC,"
            "  entity TEXT,"
            "  direction TEXT,"
            "  status TEXT NOT NULL DEFAULT 'needs_review',"
            "  raw_data TEXT NOT NULL DEFAULT '{}'"
            ")"
        ))
        conn.commit()
    with Session(engine) as s, pytest.raises(RuntimeError, match="No AccountBalanceSnapshot"):
        load_live(s, today=dt.date(2026, 6, 1))
