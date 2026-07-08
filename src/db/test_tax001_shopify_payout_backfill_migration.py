"""Migration round-trip test for tax001_shopify_payout_backfill (REQ-FIX-TAX-001).

Exercises the actual data-migration upgrade/downgrade against a stamped SQLite DB
(the adapter/ORM unit tests never touch this file). Seeds four rows:

- an ``income`` payout (flipped; 2 upgrade audit events)
- a ``rejected`` ``income`` payout (flipped, but ``status`` stays ``rejected``)
- a non-payout Shopify order (untouched)
- an already-``transfer`` payout (untouched — idempotency)

then asserts upgrade -> downgrade (compensating events, originals preserved) ->
re-upgrade.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

_REVISION = "tax001_shopify_payout_backfill"
_PREV_REVISION = "inv002_payment_link_amount"
_UPGRADE_ACTOR = "migration:tax001_shopify_payout_backfill"
_DOWNGRADE_ACTOR = "migration:tax001_shopify_payout_backfill:downgrade"
_STALE_TS = "2020-01-01 00:00:00.000000"


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
        # Minimal transactions/audit_events tables — only the columns this
        # migration reads or writes.
        conn.execute(
            sa.text(
                "CREATE TABLE transactions ("
                " id VARCHAR(36) NOT NULL PRIMARY KEY,"
                " source VARCHAR(32) NOT NULL,"
                " source_id VARCHAR(255),"
                " source_hash VARCHAR(64) NOT NULL UNIQUE,"
                " date VARCHAR(10) NOT NULL,"
                " description TEXT NOT NULL,"
                " amount NUMERIC,"
                " direction VARCHAR(16),"
                " tax_category VARCHAR(32),"
                " status VARCHAR(24) NOT NULL,"
                " confirmed_by VARCHAR(8) NOT NULL,"
                " raw_data JSON NOT NULL,"
                " created_at DATETIME NOT NULL,"
                " updated_at DATETIME NOT NULL"
                ")"
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
                " old_value TEXT,"
                " new_value TEXT,"
                " changed_by VARCHAR(64) NOT NULL,"
                " changed_at DATETIME NOT NULL,"
                " CONSTRAINT ck_audit_events_exactly_one_target CHECK ("
                "  (transaction_id IS NOT NULL AND entity_id IS NULL AND entity_type IS NULL)"
                "  OR (transaction_id IS NULL AND entity_id IS NOT NULL AND entity_type IS NOT NULL))"
                ")"
            )
        )
    engine.dispose()


def _seed_tx(
    engine: sa.Engine,
    *,
    tx_id: str,
    source_id: str | None,
    direction: str,
    tax_category: str | None,
    status: str,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO transactions (id, source, source_id, source_hash, date,"
                " description, amount, direction, tax_category, status, confirmed_by,"
                " raw_data, created_at, updated_at)"
                " VALUES (:id, 'shopify', :sid, :hash, '2026-05-01', 'seed', 100.00,"
                " :dir, :cat, :status, 'auto', '{}', :ts, :ts)"
            ),
            {
                "id": tx_id,
                "sid": source_id,
                "hash": str(uuid.uuid4()),
                "dir": direction,
                "cat": tax_category,
                "status": status,
                "ts": _STALE_TS,
            },
        )


def _fetch_tx(engine: sa.Engine, tx_id: str) -> sa.Row[Any]:
    with engine.connect() as conn:
        return conn.execute(
            sa.text(
                "SELECT direction, tax_category, status, updated_at"
                " FROM transactions WHERE id = :id"
            ),
            {"id": tx_id},
        ).one()


def _audit_count(engine: sa.Engine, changed_by: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                sa.text(
                    "SELECT COUNT(*) FROM audit_events WHERE changed_by = :by"
                ),
                {"by": changed_by},
            ).scalar_one()
        )


def test_tax001_shopify_payout_backfill_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "test_tax001.db"
    _stamp_at(db_path, _PREV_REVISION, monkeypatch)

    income_id = "11111111-1111-1111-1111-111111111111"
    rejected_id = "22222222-2222-2222-2222-222222222222"
    order_id = "33333333-3333-3333-3333-333333333333"
    already_transfer_id = "44444444-4444-4444-4444-444444444444"

    engine = sa.create_engine(f"sqlite:///{db_path}")
    _seed_tx(
        engine,
        tx_id=income_id,
        source_id="payout_9001",
        direction="income",
        tax_category="SALES_INCOME",
        status="auto_classified",
    )
    _seed_tx(
        engine,
        tx_id=rejected_id,
        source_id="payout_9002",
        direction="income",
        tax_category="SALES_INCOME",
        status="rejected",
    )
    _seed_tx(
        engine,
        tx_id=order_id,
        source_id="order_5000",
        direction="income",
        tax_category="SALES_INCOME",
        status="confirmed",
    )
    _seed_tx(
        engine,
        tx_id=already_transfer_id,
        source_id="payout_9003",
        direction="transfer",
        tax_category=None,
        status="auto_classified",
    )
    engine.dispose()

    # 1. Upgrade.
    _run_alembic("upgrade", _REVISION)
    engine = sa.create_engine(f"sqlite:///{db_path}")

    # income payout: flipped, updated_at bumped, status preserved.
    dir_, cat, status, updated_at = _fetch_tx(engine, income_id)
    assert (dir_, cat, status) == ("transfer", None, "auto_classified")
    assert updated_at != _STALE_TS

    # rejected payout: flipped BUT status stays rejected.
    assert _fetch_tx(engine, rejected_id)[:3] == ("transfer", None, "rejected")

    # non-payout order: untouched.
    assert _fetch_tx(engine, order_id)[:3] == ("income", "SALES_INCOME", "confirmed")

    # already-transfer payout: untouched (idempotency — no new audit rows for it).
    assert _fetch_tx(engine, already_transfer_id)[:3] == ("transfer", None, "auto_classified")

    # 2 flipped rows * 2 events each = 4 upgrade audit events.
    assert _audit_count(engine, _UPGRADE_ACTOR) == 4
    engine.dispose()

    # 2. Downgrade — compensating events; upgrade events preserved.
    _run_alembic("downgrade", "-1")
    engine = sa.create_engine(f"sqlite:///{db_path}")

    assert _fetch_tx(engine, income_id)[:3] == ("income", "SALES_INCOME", "auto_classified")
    # status still preserved through the reversal.
    assert _fetch_tx(engine, rejected_id)[:3] == ("income", "SALES_INCOME", "rejected")
    # untouched rows stay untouched.
    assert _fetch_tx(engine, order_id)[:3] == ("income", "SALES_INCOME", "confirmed")
    assert _fetch_tx(engine, already_transfer_id)[:3] == ("transfer", None, "auto_classified")

    # Append-only: original upgrade events survive, compensating events added.
    assert _audit_count(engine, _UPGRADE_ACTOR) == 4
    assert _audit_count(engine, _DOWNGRADE_ACTOR) == 4
    engine.dispose()

    # 3. Re-upgrade — round-trip flips the two payouts back to transfer.
    _run_alembic("upgrade", _REVISION)
    engine = sa.create_engine(f"sqlite:///{db_path}")
    assert _fetch_tx(engine, income_id)[:2] == ("transfer", None)
    assert _fetch_tx(engine, rejected_id)[:2] == ("transfer", None)
    engine.dispose()
