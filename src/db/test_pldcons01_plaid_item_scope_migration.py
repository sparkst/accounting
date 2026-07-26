"""Migration round-trip test for pldcons01_plaid_item_scope (REQ-PC-B1).

Verifies:
- upgrade adds ``plaid_item.scope`` with server default 'register' (existing
  rows are backfilled to 'register');
- the CHECK constraint accepts 'register'/'wealth' and rejects anything else
  (constraint values are the enum VALUES);
- downgrade with only register rows drops the column cleanly (real downgrade);
- downgrade REFUSES while a scope='wealth' row exists (silent drop would
  revert the Item to register scope and feed wealth data into the register);
- re-upgrade round-trips.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

_REVISION = "pldcons01_plaid_item_scope"
_PREV_REVISION = "mca01_confirmed_by_widen_ar_vision"


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
        # Minimal plaid_item shape as of the previous revision (pre-scope).
        conn.execute(
            sa.text(
                "CREATE TABLE plaid_item ("
                " id VARCHAR(36) NOT NULL PRIMARY KEY,"
                " item_id VARCHAR(128) NOT NULL,"
                " institution_id VARCHAR(64) NOT NULL,"
                " institution_name VARCHAR(128) NOT NULL,"
                " access_token_encrypted TEXT NOT NULL,"
                " cursor VARCHAR(255),"
                " last_sync_at DATETIME,"
                " last_sync_status VARCHAR(24),"
                " last_error VARCHAR(64),"
                " consent_expiration_at DATETIME,"
                " state_nonce VARCHAR(64),"
                " state_nonce_expires_at DATETIME,"
                " status VARCHAR(16) NOT NULL DEFAULT 'active',"
                " created_at DATETIME NOT NULL DEFAULT (datetime('now')),"
                " updated_at DATETIME NOT NULL DEFAULT (datetime('now')),"
                " CONSTRAINT uq_plaid_item_item_id UNIQUE (item_id),"
                " CONSTRAINT ck_plaid_item_status CHECK"
                "   (status IN ('active', 'disconnected', 'pending_oauth', 'abandoned')),"
                " CONSTRAINT ck_plaid_item_last_sync_status CHECK"
                "   (last_sync_status IS NULL OR last_sync_status IN"
                "    ('ok', 'error', 'pending', 'institution_down'))"
                ")"
            )
        )
    engine.dispose()


def _insert(engine: sa.Engine, *, item_id: str, scope: str | None = None) -> None:
    with engine.begin() as conn:
        if scope is None:
            conn.execute(
                sa.text(
                    "INSERT INTO plaid_item (id, item_id, institution_id,"
                    " institution_name, access_token_encrypted, created_at, updated_at)"
                    " VALUES (:id, :item_id, 'ins_3', 'Chase', 'REVOKED',"
                    " datetime('now'), datetime('now'))"
                ),
                {"id": str(uuid.uuid4()), "item_id": item_id},
            )
        else:
            conn.execute(
                sa.text(
                    "INSERT INTO plaid_item (id, item_id, institution_id,"
                    " institution_name, access_token_encrypted, scope,"
                    " created_at, updated_at)"
                    " VALUES (:id, :item_id, 'ins_3', 'Chase', 'REVOKED', :scope,"
                    " datetime('now'), datetime('now'))"
                ),
                {"id": str(uuid.uuid4()), "item_id": item_id, "scope": scope},
            )


def test_pldcons01_scope_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "test_pldcons01.db"
    _stamp_at(db_path, _PREV_REVISION, monkeypatch)

    pre_engine = sa.create_engine(f"sqlite:///{db_path}")
    _insert(pre_engine, item_id="existing_item")
    pre_engine.dispose()

    # 1. Upgrade — existing rows backfilled to 'register'.
    _run_alembic("upgrade", _REVISION)

    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        rows = [
            tuple(r)
            for r in conn.execute(
                sa.text("SELECT item_id, scope FROM plaid_item")
            ).fetchall()
        ]
    assert rows == [("existing_item", "register")]

    # 2. Both enum values accepted.
    _insert(engine, item_id="wealth_item", scope="wealth")
    _insert(engine, item_id="register_item", scope="register")
    # 3. Bogus value rejected (CHECK carries the enum VALUES).
    with pytest.raises(IntegrityError):
        _insert(engine, item_id="bogus_item", scope="both")
    engine.dispose()

    # 4. Downgrade REFUSES while a wealth row exists.
    with pytest.raises(RuntimeError, match="scope='wealth'"):
        _run_alembic("downgrade", "-1")

    # 5. Remove the wealth row → downgrade drops the column (real downgrade).
    engine2 = sa.create_engine(f"sqlite:///{db_path}")
    with engine2.begin() as conn:
        conn.execute(sa.text("DELETE FROM plaid_item WHERE item_id = 'wealth_item'"))
    engine2.dispose()
    _run_alembic("downgrade", "-1")

    engine3 = sa.create_engine(f"sqlite:///{db_path}")
    cols = {
        row[1]
        for row in engine3.connect()
        .execute(sa.text("PRAGMA table_info(plaid_item)"))
        .fetchall()
    }
    assert "scope" not in cols
    with engine3.connect() as conn:
        count = conn.execute(sa.text("SELECT COUNT(*) FROM plaid_item")).scalar_one()
    assert count == 2  # no rows lost
    engine3.dispose()

    # 6. Re-upgrade — round-trip OK, backfill applies again.
    _run_alembic("upgrade", _REVISION)
    engine4 = sa.create_engine(f"sqlite:///{db_path}")
    with engine4.connect() as conn:
        scopes = [
            tuple(r)
            for r in conn.execute(
                sa.text("SELECT DISTINCT scope FROM plaid_item")
            ).fetchall()
        ]
    assert scopes == [("register",)]
    engine4.dispose()
