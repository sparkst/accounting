"""Migration round-trip test for wa2607b_account_alias (REQ-FIX-WLT-004).

The account_alias table is created on upgrade and dropped on downgrade; an
account row referenced by an alias survives the round trip.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa

_REVISION = "wa2607b_account_alias"
_PREV_REVISION = "wa2607a_adjclose_splits"


def _run_alembic(*args: str) -> None:
    from alembic.config import main as alembic_main

    alembic_main(list(args))


def _stamp_at(db_path: Path, revision: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL,"
                " CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
            )
        )
        conn.execute(sa.text(f"INSERT INTO alembic_version VALUES ('{revision}')"))
        # Minimal account table so the FK target exists.
        conn.execute(
            sa.text(
                "CREATE TABLE account ("
                " id VARCHAR(36) NOT NULL PRIMARY KEY,"
                " broker VARCHAR(16) NOT NULL,"
                " account_number VARCHAR(64) NOT NULL,"
                " account_name VARCHAR(128),"
                " account_type VARCHAR(24) NOT NULL,"
                " entity VARCHAR(16) NOT NULL,"
                " tax_sheltered BOOLEAN NOT NULL DEFAULT 0,"
                " is_plan_wrapper BOOLEAN NOT NULL DEFAULT 0,"
                " created_at DATETIME NOT NULL,"
                " updated_at DATETIME NOT NULL)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO account"
                " (id, broker, account_number, account_name, account_type, entity,"
                "  created_at, updated_at)"
                " VALUES ('acct-1', 'vanguard', '1234', 'Amy IRA', 'trad_ira',"
                " 'personal', '2026-07-07T00:00:00', '2026-07-07T00:00:00')"
            )
        )
    engine.dispose()


def test_wa2607b_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "test_wa2607b.db"
    _stamp_at(db_path, _PREV_REVISION, monkeypatch)

    _run_alembic("upgrade", _REVISION)
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        cols = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(account_alias)"))}
        assert {"raw_account_name", "account_id", "created_at"} <= cols
        conn.execute(
            sa.text(
                "INSERT INTO account_alias (raw_account_name, account_id, created_at)"
                " VALUES ('amy ira', 'acct-1', '2026-07-07T00:00:00')"
            )
        )
    engine.dispose()

    _run_alembic("downgrade", "-1")
    engine2 = sa.create_engine(f"sqlite:///{db_path}")
    with engine2.connect() as conn:
        has_alias = conn.execute(
            sa.text(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name='account_alias'"
            )
        ).fetchone()
        acct_count = conn.execute(sa.text("SELECT COUNT(*) FROM account")).scalar()
    assert has_alias is None
    assert acct_count == 1  # account row preserved
    engine2.dispose()

    _run_alembic("upgrade", _REVISION)
    engine3 = sa.create_engine(f"sqlite:///{db_path}")
    with engine3.connect() as conn:
        has_alias3 = conn.execute(
            sa.text(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name='account_alias'"
            )
        ).fetchone()
    assert has_alias3 is not None
    engine3.dispose()
