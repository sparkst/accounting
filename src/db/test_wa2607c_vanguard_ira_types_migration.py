"""Migration round-trip test for wa2607c_vanguard_ira_types (REQ-FIX-DAT-001).

Seeds the four mis-typed Vanguard accounts, runs the audited data migration,
asserts corrected account_type/tax_sheltered + one AuditEvent per changed field,
verifies the downgrade restores prior values (append-only audit), and proves the
migration fails loudly on 0-match and 2-match name ambiguity.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa

_REVISION = "wa2607c_vanguard_ira_types"
_PREV_REVISION = "wa2607b_account_alias"


def _run_alembic(*args: str) -> None:
    from alembic.config import main as alembic_main

    alembic_main(list(args))


def _base_schema(conn: sa.engine.Connection, revision: str) -> None:
    conn.execute(
        sa.text(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL,"
            " CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
        )
    )
    conn.execute(sa.text(f"INSERT INTO alembic_version VALUES ('{revision}')"))
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
            "CREATE TABLE audit_events ("
            " id VARCHAR(36) NOT NULL PRIMARY KEY,"
            " transaction_id VARCHAR(36),"
            " entity_id VARCHAR(36),"
            " entity_type VARCHAR(32),"
            " field_changed VARCHAR(64) NOT NULL,"
            " old_value TEXT, new_value TEXT,"
            " changed_by VARCHAR(64) NOT NULL,"
            " changed_at DATETIME NOT NULL,"
            " cf_scheduled_time BIGINT)"
        )
    )


def _insert_account(
    conn: sa.engine.Connection, acct_id: str, name: str, *, number: str = "0000"
) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO account"
            " (id, broker, account_number, account_name, account_type, entity,"
            "  tax_sheltered, created_at, updated_at)"
            " VALUES (:id, 'vanguard', :num, :name, 'taxable', 'personal', 0,"
            " '2026-07-07T00:00:00', '2026-07-07T00:00:00')"
        ),
        {"id": acct_id, "num": number, "name": name},
    )


_NAMES = ["Amy IRA", "Amy Roth IRA", "Travis Vanguard IRA", "Travis Roth IRA"]
_EXPECTED = {
    "Amy IRA": "trad_ira",
    "Amy Roth IRA": "roth_ira",
    "Travis Vanguard IRA": "trad_ira",
    "Travis Roth IRA": "roth_ira",
}


def test_wa2607c_corrects_and_downgrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "test_wa2607c.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        _base_schema(conn, _PREV_REVISION)
        for i, name in enumerate(_NAMES):
            _insert_account(conn, f"acct-{i}", name, number=f"100{i}")
    engine.dispose()

    _run_alembic("upgrade", _REVISION)
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        for name, want_type in _EXPECTED.items():
            row = conn.execute(
                sa.text(
                    "SELECT account_type, tax_sheltered FROM account"
                    " WHERE account_name = :n"
                ),
                {"n": name},
            ).fetchone()
            assert row[0] == want_type
            assert int(row[1]) == 1
        # One audit event per changed field: 4 accounts × 2 fields = 8.
        n_events = conn.execute(
            sa.text(
                "SELECT COUNT(*) FROM audit_events WHERE changed_by = 'migration:wa2607c'"
                " AND entity_type = 'account'"
            )
        ).scalar()
        assert n_events == 8
    engine.dispose()

    # Downgrade restores taxable / tax_sheltered=0 and appends reversal events.
    _run_alembic("downgrade", "-1")
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        for name in _NAMES:
            row = conn.execute(
                sa.text(
                    "SELECT account_type, tax_sheltered FROM account"
                    " WHERE account_name = :n"
                ),
                {"n": name},
            ).fetchone()
            assert row[0] == "taxable"
            assert int(row[1]) == 0
        # Upgrade events preserved; reversal events appended.
        up = conn.execute(
            sa.text(
                "SELECT COUNT(*) FROM audit_events WHERE changed_by = 'migration:wa2607c'"
            )
        ).scalar()
        down = conn.execute(
            sa.text(
                "SELECT COUNT(*) FROM audit_events"
                " WHERE changed_by = 'migration:wa2607c:downgrade'"
            )
        ).scalar()
        assert up == 8
        assert down == 8
    engine.dispose()


def test_wa2607c_fails_on_zero_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "test_wa2607c_zero.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        _base_schema(conn, _PREV_REVISION)
        # Only three of the four names present → "Travis Roth IRA" matches 0 rows.
        for i, name in enumerate(_NAMES[:3]):
            _insert_account(conn, f"acct-{i}", name, number=f"200{i}")
    engine.dispose()

    with pytest.raises(RuntimeError, match="found 0"):
        _run_alembic("upgrade", _REVISION)


def test_wa2607c_fails_on_two_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "test_wa2607c_two.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        _base_schema(conn, _PREV_REVISION)
        for i, name in enumerate(_NAMES):
            _insert_account(conn, f"acct-{i}", name, number=f"300{i}")
        # Duplicate "Amy IRA" vanguard row → matches 2 rows.
        _insert_account(conn, "acct-dup", "Amy IRA", number="9999")
    engine.dispose()

    with pytest.raises(RuntimeError, match="found 2"):
        _run_alembic("upgrade", _REVISION)
