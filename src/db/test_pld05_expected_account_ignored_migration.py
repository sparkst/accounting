"""Migration round-trip test for pld05_expected_account_ignored_status (REQ-FIX-PLD-005).

Verifies the CHECK constraint on `expected_account.status` accepts 'ignored'
after upgrade, and that downgrade flips any 'ignored' rows back to
'unconfirmed' (UPDATE, never a DELETE) before restoring the old constraint —
preserving the row count.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

_REVISION = "pld05_expected_account_ignored_status"
_PREV_REVISION = "na_iul_01"


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
        conn.execute(
            sa.text(
                "CREATE TABLE expected_account ("
                " id VARCHAR(36) NOT NULL PRIMARY KEY,"
                " institution VARCHAR(64) NOT NULL,"
                " account_name VARCHAR(255) NOT NULL,"
                " last_4 VARCHAR(8),"
                " status VARCHAR(16) NOT NULL DEFAULT 'unconfirmed',"
                " source VARCHAR(32) NOT NULL,"
                " notes TEXT,"
                " resolved_account_id VARCHAR(36),"
                " created_at DATETIME NOT NULL DEFAULT (datetime('now')),"
                " updated_at DATETIME NOT NULL DEFAULT (datetime('now')),"
                " CONSTRAINT uq_expected_account_natural_key UNIQUE"
                "   (institution, account_name, last_4),"
                " CONSTRAINT ck_expected_account_status CHECK"
                "   (status IN ('active', 'closed', 'unconfirmed'))"
                ")"
            )
        )
    engine.dispose()


def _insert(engine: sa.Engine, *, name: str, status: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO expected_account (id, institution, account_name,"
                " status, source, created_at, updated_at)"
                " VALUES (:id, 'Chase', :name, :status, 'plaid',"
                " datetime('now'), datetime('now'))"
            ),
            {"id": str(uuid.uuid4()), "name": name, "status": status},
        )


def test_pld05_ignored_status_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "test_pld05.db"
    _stamp_at(db_path, _PREV_REVISION, monkeypatch)

    pre_engine = sa.create_engine(f"sqlite:///{db_path}")
    _insert(pre_engine, name="Existing Unconfirmed", status="unconfirmed")
    pre_engine.dispose()

    # 1. Upgrade.
    _run_alembic("upgrade", _REVISION)

    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT account_name, status FROM expected_account")
        ).fetchall()
    assert len(rows) == 1  # pre-existing row survived
    assert rows[0] == ("Existing Unconfirmed", "unconfirmed")

    # 2. 'ignored' now accepted.
    _insert(engine, name="Ignore Me", status="ignored")
    # 3. Old values still accepted.
    _insert(engine, name="Still Active", status="active")
    # 4. Bogus value still rejected.
    with pytest.raises((IntegrityError, sa.exc.IntegrityError)):
        _insert(engine, name="Bogus", status="bogus_status")
    engine.dispose()

    total_before_downgrade = 3

    # 5. Downgrade flips 'ignored' -> 'unconfirmed' (UPDATE, no DELETE) and
    #    preserves the row count.
    _run_alembic("downgrade", "-1")

    engine2 = sa.create_engine(f"sqlite:///{db_path}")
    with engine2.connect() as conn:
        rows2 = conn.execute(
            sa.text("SELECT account_name, status FROM expected_account ORDER BY account_name")
        ).fetchall()
    assert len(rows2) == total_before_downgrade
    statuses = {name: status for name, status in rows2}
    assert statuses["Ignore Me"] == "unconfirmed"  # flipped, not deleted
    assert statuses["Still Active"] == "active"

    # 'ignored' rejected again post-downgrade.
    with pytest.raises((IntegrityError, sa.exc.IntegrityError)):
        _insert(engine2, name="Reject Ignored", status="ignored")
    engine2.dispose()

    # 6. Re-upgrade — round-trip OK.
    _run_alembic("upgrade", _REVISION)
    engine3 = sa.create_engine(f"sqlite:///{db_path}")
    _insert(engine3, name="Ignored Again", status="ignored")
    engine3.dispose()
