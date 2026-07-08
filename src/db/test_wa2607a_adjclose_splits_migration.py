"""Migration round-trip test for wa2607a_adjclose_splits (REQ-FIX-WLT-001/002).

Exercises the real batch_alter_table + create_table upgrade/downgrade: a legacy
historical_price row survives the upgrade with NULL adj_close, the new
adj_close column and stock_split table accept writes, and the downgrade drops
both without losing the price row.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa

_REVISION = "wa2607a_adjclose_splits"
_PREV_REVISION = "tax001_shopify_payout_backfill"


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
        # historical_price as it exists BEFORE wa2607a (no adj_close).
        conn.execute(
            sa.text(
                "CREATE TABLE historical_price ("
                " symbol VARCHAR(32) NOT NULL,"
                " trade_date DATE NOT NULL,"
                " close NUMERIC(18, 8) NOT NULL,"
                " open NUMERIC(18, 8), high NUMERIC(18, 8), low NUMERIC(18, 8),"
                " volume INTEGER,"
                " source VARCHAR(16) NOT NULL DEFAULT 'yfinance',"
                " ingested_at DATETIME NOT NULL,"
                " PRIMARY KEY (symbol, trade_date))"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO historical_price"
                " (symbol, trade_date, close, ingested_at)"
                " VALUES ('AAPL', '2026-07-01', '210.50000000', '2026-07-01T00:00:00')"
            )
        )
    engine.dispose()


def test_wa2607a_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "test_wa2607a.db"
    _stamp_at(db_path, _PREV_REVISION, monkeypatch)

    # 1. Upgrade: legacy row survives with NULL adj_close; stock_split exists.
    _run_alembic("upgrade", _REVISION)
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT symbol, close, adj_close FROM historical_price")
        ).fetchone()
        assert row[0] == "AAPL"
        assert row[2] is None  # adj_close NULL on legacy row
        cols = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(stock_split)"))}
    assert {"symbol", "ex_date", "ratio", "source", "ingested_at"} <= cols

    # 2. New column + table accept writes.
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "UPDATE historical_price SET adj_close = '208.00000000'"
                " WHERE symbol = 'AAPL'"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO stock_split (symbol, ex_date, ratio, source, ingested_at)"
                " VALUES ('AAPL', '2020-08-31', '4.000000', 'yfinance',"
                " '2026-07-07T00:00:00')"
            )
        )
    engine.dispose()

    # 3. Downgrade drops adj_close + stock_split, keeps the price row.
    _run_alembic("downgrade", "-1")
    engine2 = sa.create_engine(f"sqlite:///{db_path}")
    with engine2.connect() as conn:
        hp_cols = {
            r[1] for r in conn.execute(sa.text("PRAGMA table_info(historical_price)"))
        }
        count = conn.execute(
            sa.text("SELECT COUNT(*) FROM historical_price")
        ).scalar()
        has_split = conn.execute(
            sa.text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='stock_split'"
            )
        ).fetchone()
    assert "adj_close" not in hp_cols
    assert count == 1
    assert has_split is None
    engine2.dispose()

    # 4. Re-upgrade round-trips cleanly.
    _run_alembic("upgrade", _REVISION)
    engine3 = sa.create_engine(f"sqlite:///{db_path}")
    with engine3.connect() as conn:
        hp_cols3 = {
            r[1] for r in conn.execute(sa.text("PRAGMA table_info(historical_price)"))
        }
    assert "adj_close" in hp_cols3
    engine3.dispose()
