"""Migration round-trip test for alr01_alert_dispatch_payload (REQ-FIX-ALR-002).

Exercises the actual batch_alter_table upgrade/downgrade (the ORM tests build
schema via Base.metadata.create_all and bypass this file entirely): legacy rows
survive the upgrade with NULL payload_json/delivery_channel, new columns accept
writes, and the downgrade drops the columns without losing rows.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa

_REVISION = "alr01_alert_dispatch_payload"
_PREV_REVISION = "pld05_expected_account_ignored_status"


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
        # alert_dispatch as it exists BEFORE alr01 (no payload_json/delivery_channel).
        conn.execute(
            sa.text(
                "CREATE TABLE alert_dispatch ("
                " id VARCHAR(36) NOT NULL PRIMARY KEY,"
                " alert_key VARCHAR NOT NULL,"
                " occurrence_date VARCHAR(10) NOT NULL,"
                " alert_type VARCHAR NOT NULL,"
                " entity VARCHAR NOT NULL,"
                " subject TEXT NOT NULL,"
                " status VARCHAR NOT NULL,"
                " http_status INTEGER,"
                " error_detail TEXT,"
                " created_at VARCHAR NOT NULL,"
                " CONSTRAINT uq_alert_dispatch_key_date UNIQUE (alert_key, occurrence_date)"
                ")"
            )
        )
    engine.dispose()


def _insert_legacy(engine: sa.Engine, *, key: str, status: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO alert_dispatch (id, alert_key, occurrence_date,"
                " alert_type, entity, subject, status, created_at)"
                " VALUES (:id, :key, '2026-07-01', 'balance_milestone', 'sparkry',"
                " 'legacy subject', :status, '2026-07-01T14:00:00')"
            ),
            {"id": str(uuid.uuid4()), "key": key, "status": status},
        )


def test_alr01_payload_columns_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "test_alr01.db"
    _stamp_at(db_path, _PREV_REVISION, monkeypatch)

    pre_engine = sa.create_engine(f"sqlite:///{db_path}")
    _insert_legacy(pre_engine, key="legacy:failed", status="failed")
    _insert_legacy(pre_engine, key="legacy:sent", status="sent")
    pre_engine.dispose()

    # 1. Upgrade: legacy rows survive with NULL/NULL new columns.
    _run_alembic("upgrade", _REVISION)
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT alert_key, status, payload_json, delivery_channel"
                " FROM alert_dispatch ORDER BY alert_key"
            )
        ).fetchall()
    assert [(r[0], r[1], r[2], r[3]) for r in rows] == [
        ("legacy:failed", "failed", None, None),
        ("legacy:sent", "sent", None, None),
    ]

    # 2. New columns accept writes.
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO alert_dispatch (id, alert_key, occurrence_date,"
                " alert_type, entity, subject, status, created_at,"
                " payload_json, delivery_channel)"
                " VALUES (:id, 'new:webhook', '2026-07-07', 'balance_milestone',"
                " 'sparkry', 'new subject', 'failed', '2026-07-07T14:00:00',"
                " '{\"severity\": \"sev2\"}', 'n8n_webhook')"
            ),
            {"id": str(uuid.uuid4())},
        )
    engine.dispose()

    # 3. Downgrade drops the columns, keeps every row.
    _run_alembic("downgrade", "-1")
    engine2 = sa.create_engine(f"sqlite:///{db_path}")
    with engine2.connect() as conn:
        cols = {
            row[1]
            for row in conn.execute(sa.text("PRAGMA table_info(alert_dispatch)"))
        }
        count = conn.execute(sa.text("SELECT COUNT(*) FROM alert_dispatch")).scalar()
    assert "payload_json" not in cols
    assert "delivery_channel" not in cols
    assert count == 3  # no rows lost
    engine2.dispose()

    # 4. Re-upgrade — round-trip OK, columns back.
    _run_alembic("upgrade", _REVISION)
    engine3 = sa.create_engine(f"sqlite:///{db_path}")
    with engine3.connect() as conn:
        cols3 = {
            row[1]
            for row in conn.execute(sa.text("PRAGMA table_info(alert_dispatch)"))
        }
    assert {"payload_json", "delivery_channel"} <= cols3
    engine3.dispose()
