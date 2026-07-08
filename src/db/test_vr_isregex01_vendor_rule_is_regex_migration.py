"""Migration round-trip test for vr_isregex01_vendor_rule_is_regex (REQ-FIX-ING-005).

Mirrors src/db/test_alr01_alert_dispatch_payload_migration.py: exercises the
actual batch_alter_table upgrade/downgrade (the ORM tests build schema via
Base.metadata.create_all and bypass this file entirely) — legacy vendor_rules
rows survive the upgrade with is_regex=0 (server default), new rows can write
is_regex=1, and the downgrade drops the column without losing rows.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa

_REVISION = "vr_isregex01_vendor_rule_is_regex"
_PREV_REVISION = "wa2607c_vanguard_ira_types"


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
        # vendor_rules as it exists BEFORE vr_isregex01 (no is_regex column).
        conn.execute(
            sa.text(
                "CREATE TABLE vendor_rules ("
                " id VARCHAR(36) NOT NULL PRIMARY KEY,"
                " vendor_pattern TEXT NOT NULL,"
                " entity VARCHAR(16) NOT NULL,"
                " tax_category VARCHAR(32) NOT NULL,"
                " tax_subcategory VARCHAR(32),"
                " direction VARCHAR(16) NOT NULL,"
                " deductible_pct FLOAT NOT NULL DEFAULT 1.0,"
                " confidence FLOAT NOT NULL DEFAULT 1.0,"
                " source VARCHAR(8) NOT NULL,"
                " examples INTEGER NOT NULL DEFAULT 1,"
                " last_matched DATETIME,"
                " created_at DATETIME NOT NULL"
                ")"
            )
        )
    engine.dispose()


def _insert_legacy(engine: sa.Engine, *, pattern: str, entity: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO vendor_rules (id, vendor_pattern, entity, tax_category,"
                " direction, source, created_at)"
                " VALUES (:id, :pattern, :entity, 'SUPPLIES', 'expense', 'human',"
                " '2026-01-01 00:00:00')"
            ),
            {"id": str(uuid.uuid4()), "pattern": pattern, "entity": entity},
        )


def test_vr_isregex01_column_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "test_vr_isregex01.db"
    _stamp_at(db_path, _PREV_REVISION, monkeypatch)

    pre_engine = sa.create_engine(f"sqlite:///{db_path}")
    _insert_legacy(pre_engine, pattern="anthropic", entity="sparkry")
    _insert_legacy(pre_engine, pattern=r"amazon.*aws|aws\.amazon", entity="sparkry")
    pre_engine.dispose()

    # 1. Upgrade: legacy rows survive with is_regex=0 (server default) —
    # every existing row flips from raw-regex to literal matching.
    _run_alembic("upgrade", _REVISION)
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT vendor_pattern, is_regex FROM vendor_rules ORDER BY vendor_pattern"
            )
        ).fetchall()
    assert [(r[0], r[1]) for r in rows] == [
        (r"amazon.*aws|aws\.amazon", 0),
        ("anthropic", 0),
    ]

    # 2. New rows can write is_regex=1.
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO vendor_rules (id, vendor_pattern, is_regex, entity,"
                " tax_category, direction, source, created_at)"
                " VALUES (:id, 'new.*pattern', 1, 'sparkry', 'SUPPLIES', 'expense',"
                " 'human', '2026-07-07 00:00:00')"
            ),
            {"id": str(uuid.uuid4())},
        )
    engine.dispose()

    # 3. Downgrade drops the column, keeps every row.
    _run_alembic("downgrade", "-1")
    engine2 = sa.create_engine(f"sqlite:///{db_path}")
    with engine2.connect() as conn:
        cols = {row[1] for row in conn.execute(sa.text("PRAGMA table_info(vendor_rules)"))}
        count = conn.execute(sa.text("SELECT COUNT(*) FROM vendor_rules")).scalar()
    assert "is_regex" not in cols
    assert count == 3  # no rows lost
    engine2.dispose()

    # 4. Re-upgrade — round-trip OK, column back with server default applied
    # to the pre-existing rows (SQLite recreates the table via batch mode).
    _run_alembic("upgrade", _REVISION)
    engine3 = sa.create_engine(f"sqlite:///{db_path}")
    with engine3.connect() as conn:
        cols3 = {row[1] for row in conn.execute(sa.text("PRAGMA table_info(vendor_rules)"))}
        count3 = conn.execute(sa.text("SELECT COUNT(*) FROM vendor_rules")).scalar()
    assert "is_regex" in cols3
    assert count3 == 3
    engine3.dispose()
