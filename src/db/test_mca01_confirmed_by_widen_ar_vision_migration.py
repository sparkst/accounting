"""Migration round-trip test for mca01_confirmed_by_widen_ar_vision.

REQ-MCA-002 / REQ-ARC-* / REQ-VIS-003. Exercises the actual
batch_alter_table + create_table upgrade/downgrade (the ORM tests build schema
via Base.metadata.create_all and bypass this file). Mirrors
test_alr01_alert_dispatch_payload_migration.py: hand-stamp the schema at the
prior revision, run only the mca01 step, and assert:

- upgrade creates ar_reminder + vision_promotion and lets confirmed_by hold a
  long ``auto:rule:<uuid>`` value,
- downgrade drops the two tables AND normalizes ``auto:rule:%`` → ``auto``
  (no silent truncation), keeping every transaction row.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa

_REVISION = "mca01_confirmed_by_widen_ar_vision"
_PREV_REVISION = "vr_isregex01_vendor_rule_is_regex"


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
        # Minimal invoices table (ar_reminder FK target).
        conn.execute(
            sa.text(
                "CREATE TABLE invoices ("
                " id VARCHAR(36) NOT NULL PRIMARY KEY,"
                " invoice_number VARCHAR(64) NOT NULL"
                ")"
            )
        )
        # transactions as it exists BEFORE mca01: confirmed_by is String(8).
        conn.execute(
            sa.text(
                "CREATE TABLE transactions ("
                " id VARCHAR(36) NOT NULL PRIMARY KEY,"
                " description TEXT NOT NULL,"
                " confirmed_by VARCHAR(8) NOT NULL"
                ")"
            )
        )
    engine.dispose()


def _insert_tx(engine: sa.Engine, *, confirmed_by: str) -> str:
    tid = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO transactions (id, description, confirmed_by)"
                " VALUES (:id, 'ACME', :cb)"
            ),
            {"id": tid, "cb": confirmed_by},
        )
    return tid


def test_mca01_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "test_mca01.db"
    _stamp_at(db_path, _PREV_REVISION, monkeypatch)

    pre_engine = sa.create_engine(f"sqlite:///{db_path}")
    human_id = _insert_tx(pre_engine, confirmed_by="human")
    pre_engine.dispose()

    # 1. Upgrade: new tables exist; long confirmed_by writes succeed.
    _run_alembic("upgrade", _REVISION)
    long_cb = f"auto:rule:{uuid.uuid4()}"
    engine = sa.create_engine(f"sqlite:///{db_path}")
    auto_id = _insert_tx(engine, confirmed_by=long_cb)
    with engine.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                sa.text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
        assert "ar_reminder" in tables
        assert "vision_promotion" in tables
        stored = conn.execute(
            sa.text("SELECT confirmed_by FROM transactions WHERE id = :id"),
            {"id": auto_id},
        ).scalar_one()
        assert stored == long_cb
    engine.dispose()

    # 2. Downgrade: tables dropped, long value normalized, rows survive.
    _run_alembic("downgrade", "-1")
    engine2 = sa.create_engine(f"sqlite:///{db_path}")
    with engine2.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                sa.text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
        assert "ar_reminder" not in tables
        assert "vision_promotion" not in tables
        rows = dict(
            conn.execute(
                sa.text("SELECT id, confirmed_by FROM transactions")
            ).fetchall()
        )
        # Both rows survive; the out-of-domain value reverted to plain 'auto'.
        assert rows[human_id] == "human"
        assert rows[auto_id] == "auto"
    engine2.dispose()
