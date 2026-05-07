"""Migration round-trip test for the Phase-4 enum extension (REQ-005h).

Verifies the Phase-4 Alembic migration that extends the ``Broker`` and
``AccountType`` enum CHECK constraints on the ``account`` table.

Approach: stamp the DB at the prior head (``0dfd3fb13224``), upgrade to the
Phase-4 revision, assert the new enum values are accepted, downgrade back, and
re-upgrade to confirm round-trip.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

# Phase-4 migration revision identifiers.
_PHASE4_REVISION = "p4ext1enum0xt"
_PREV_REVISION = "0dfd3fb13224"


def _run_alembic(*args: str) -> None:
    from alembic.config import main as alembic_main

    alembic_main(list(args))


def _stamp_at(db_path: Path, revision: str) -> None:
    """Create a fresh DB stamped at ``revision`` without running prior migrations.

    Mirrors the technique in ``test_brokerage_migration.py``: many historical
    migrations reference tables that exist in production but not in a brand-new
    DB (e.g., ``audit_events``), so we manually create the alembic_version row
    and seed only the tables Phase 4 needs to round-trip.
    """
    os.environ["DATABASE_PATH"] = str(db_path)
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL,"
                " CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
            )
        )
        conn.execute(sa.text(f"INSERT INTO alembic_version VALUES ('{revision}')"))
        # Phase 4 mutates the ``account`` table CHECK constraints, so we need it
        # pre-existing with the OLD enum values so batch_alter_table has
        # something to recreate.
        conn.execute(
            sa.text(
                "CREATE TABLE account ("
                " id VARCHAR(36) NOT NULL PRIMARY KEY,"
                " broker VARCHAR(16) NOT NULL,"
                " account_number VARCHAR(64) NOT NULL,"
                " account_name VARCHAR(128),"
                " account_type VARCHAR(24) NOT NULL,"
                " entity VARCHAR(16) NOT NULL DEFAULT 'personal',"
                " tax_sheltered BOOLEAN NOT NULL DEFAULT 0,"
                " parent_account_id VARCHAR(36),"
                " is_plan_wrapper BOOLEAN NOT NULL DEFAULT 0,"
                " beneficiary VARCHAR(64),"
                " notes TEXT,"
                " created_at DATETIME NOT NULL,"
                " updated_at DATETIME NOT NULL,"
                " CONSTRAINT uq_account_broker_number UNIQUE (broker, account_number),"
                " CONSTRAINT ck_account_broker CHECK"
                "   (broker IN ('etrade', 'schwab', 'vanguard', 'fidelity')),"
                " CONSTRAINT ck_account_type CHECK"
                "   (account_type IN ('taxable', 'joint', 'roth_ira', 'trad_ira',"
                "    '401k', '403b', 'hsa', '529', 'tod', 'brokeragelink', 'rsu')),"
                " CONSTRAINT ck_account_entity CHECK"
                "   (entity IN ('sparkry_ai_llc', 'blackline_mtb_llc', 'personal'))"
                ")"
            )
        )
    engine.dispose()


def _insert_account(
    engine: sa.Engine,
    *,
    broker: str,
    account_type: str = "taxable",
    account_number: str | None = None,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO account (id, broker, account_number, account_type,"
                " entity, tax_sheltered, is_plan_wrapper, created_at, updated_at)"
                " VALUES (:id, :broker, :num, :atype, 'personal', 0, 0,"
                " datetime('now'), datetime('now'))"
            ),
            {
                "id": str(uuid.uuid4()),
                "broker": broker,
                "num": account_number or f"acct-{uuid.uuid4().hex[:8]}",
                "atype": account_type,
            },
        )


def test_phase4_extend_enums_round_trip(tmp_path: Path) -> None:
    """Upgrade → assert new enum values accepted → downgrade → upgrade."""
    db_path = tmp_path / "test_p4.db"

    try:
        # 0. Bring fresh DB up to the prior head.
        _stamp_at(db_path, _PREV_REVISION)

        # 1. Upgrade to Phase-4.
        _run_alembic("upgrade", _PHASE4_REVISION)

        engine = sa.create_engine(f"sqlite:///{db_path}")

        # 2. New broker values accepted.
        for new_broker in (
            "franklin_templeton",
            "nw_mutual",
            "fg_annuity",
            "gsk_pension",
        ):
            _insert_account(engine, broker=new_broker)

        # 3. New account_type 'other' accepted.
        _insert_account(engine, broker="vanguard", account_type="other")

        # 4. Existing broker values still work.
        _insert_account(engine, broker="schwab")

        # 5. Bogus broker still rejected.
        with pytest.raises((IntegrityError, sa.exc.IntegrityError)):
            _insert_account(engine, broker="robinhood")

        # 6. Bogus account_type still rejected.
        with pytest.raises((IntegrityError, sa.exc.IntegrityError)):
            _insert_account(engine, broker="vanguard", account_type="meme_stock")

        # Clean out rows with new enum values so downgrade's safety guard passes.
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "DELETE FROM account WHERE broker IN"
                    " ('franklin_templeton', 'nw_mutual', 'fg_annuity', 'gsk_pension')"
                    " OR account_type = 'other'"
                )
            )
        engine.dispose()

        # 7a. Downgrade refuses if new-enum rows remain — assert by re-adding one.
        engine_guard = sa.create_engine(f"sqlite:///{db_path}")
        _insert_account(engine_guard, broker="fg_annuity")
        engine_guard.dispose()
        with pytest.raises(RuntimeError, match="refusing to downgrade"):
            _run_alembic("downgrade", "-1")
        engine_clear = sa.create_engine(f"sqlite:///{db_path}")
        with engine_clear.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM account WHERE broker = 'fg_annuity'")
            )
        engine_clear.dispose()

        # 7. Downgrade -1 (back to prior head).
        _run_alembic("downgrade", "-1")

        engine2 = sa.create_engine(f"sqlite:///{db_path}")
        # Old broker values still accepted; new broker values rejected.
        _insert_account(engine2, broker="vanguard")
        with pytest.raises((IntegrityError, sa.exc.IntegrityError)):
            _insert_account(engine2, broker="franklin_templeton")
        with pytest.raises((IntegrityError, sa.exc.IntegrityError)):
            _insert_account(engine2, broker="vanguard", account_type="other")
        engine2.dispose()

        # 8. Re-upgrade — round-trip OK.
        _run_alembic("upgrade", _PHASE4_REVISION)

        engine3 = sa.create_engine(f"sqlite:///{db_path}")
        _insert_account(engine3, broker="fg_annuity")
        engine3.dispose()

    finally:
        os.environ.pop("DATABASE_PATH", None)
