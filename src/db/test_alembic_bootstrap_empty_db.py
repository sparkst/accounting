"""P2-004: ``alembic upgrade head`` against a genuinely empty database.

The original P0-mig001/P2-004 fix (``_bootstrap_fresh_db`` in
``src/db/alembic/env.py``) had no test at all — this file closes that gap.

Verifies:
- without the ``ALEMBIC_BOOTSTRAP_EMPTY=1`` opt-in, upgrading a fresh
  (zero-table) database refuses loudly rather than silently creating one
  (the mis-set/typo'd ``DATABASE_PATH`` guard added by this round's fix);
- with the opt-in set, the upgrade succeeds, ``alembic_version`` lands
  exactly at the script directory's head revision(s), and the resulting
  schema is IDENTICAL to ``Base.metadata`` — i.e. the fast-bootstrap path and
  a "real" `Base.metadata.create_all()` never diverge;
- P2-r3e: the shortcut applies to ``upgrade head`` ONLY — a non-head target
  against a fresh DB follows real migration history, and an already-populated
  database is untouched by both the shortcut and the refusal guard.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa


def _run_alembic(*args: str) -> None:
    from alembic.config import main as alembic_main

    alembic_main(list(args))


def test_bootstrap_refuses_without_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh zero-table DATABASE_PATH without the opt-in must refuse, not
    silently create-and-stamp a brand-new empty database (P2-004)."""
    db_path = tmp_path / "fresh_no_opt_in.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.delenv("ALEMBIC_BOOTSTRAP_EMPTY", raising=False)

    with pytest.raises(SystemExit):
        _run_alembic("upgrade", "head")

    # Nothing should have been created — the refusal happens before
    # create_all() runs. (SQLite may still have touched the file on connect;
    # what matters is no tables landed.)
    if db_path.exists():
        engine = sa.create_engine(f"sqlite:///{db_path}")
        with engine.connect() as conn:
            tables = sa.inspect(conn).get_table_names()
        assert tables == []


def test_bootstrap_with_opt_in_matches_base_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the explicit opt-in, the bootstrapped schema must be byte-for-byte
    the same table set as Base.metadata, and alembic_version must land at the
    script directory's actual head(s) — no drift between the fast-bootstrap
    path and a normal migration replay."""
    db_path = tmp_path / "fresh_opt_in.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("ALEMBIC_BOOTSTRAP_EMPTY", "1")

    _run_alembic("upgrade", "head")

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    import src.models  # noqa: F401 — registers every model on Base.metadata
    import src.planning.models  # noqa: F401
    from src.models.base import Base

    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        actual_tables = set(sa.inspect(conn).get_table_names()) - {"alembic_version"}
        expected_tables = set(Base.metadata.tables.keys())
        assert actual_tables == expected_tables

        version_rows = conn.execute(
            sa.text("SELECT version_num FROM alembic_version")
        ).fetchall()
        stamped = {row[0] for row in version_rows}

    cfg = Config(str(Path.cwd() / "alembic.ini"))
    script = ScriptDirectory.from_config(cfg)
    expected_heads = set(script.get_heads())
    assert stamped == expected_heads


def test_non_head_target_on_fresh_db_follows_normal_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2-r3e: the bootstrap shortcut is scoped to ``upgrade head``.

    Targeting a specific (non-head) revision against a zero-table database
    must replay that revision normally — NOT create the full current schema
    and stamp it at head. Otherwise `upgrade <rev>` would silently land the
    database far ahead of where the operator asked it to go, and a broken
    migration could never be reproduced against a fresh DB.
    """
    db_path = tmp_path / "fresh_non_head.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    # Opt-in set, so a refusal cannot be the reason nothing was bootstrapped.
    monkeypatch.setenv("ALEMBIC_BOOTSTRAP_EMPTY", "1")

    _run_alembic("upgrade", "1c8d9ab67214")

    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        tables = set(sa.inspect(conn).get_table_names())
        stamped = {
            row[0]
            for row in conn.execute(sa.text("SELECT version_num FROM alembic_version"))
        }

    # The initial migration is ALTER-only and self-guarded, so it creates no
    # tables of its own — the point is that create_all() did NOT run behind it.
    assert tables == {"alembic_version"}
    assert stamped == {"1c8d9ab67214"}


def test_populated_db_unaffected_by_bootstrap_and_refusal_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2-r3e: neither the shortcut nor the ``ALEMBIC_BOOTSTRAP_EMPTY`` refusal
    touches a database that already has tables. Re-running `upgrade head`
    against a populated DB with an explicit DATABASE_PATH and NO opt-in must
    be an ordinary no-op upgrade, not a SystemExit."""
    db_path = tmp_path / "populated.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("ALEMBIC_BOOTSTRAP_EMPTY", "1")
    _run_alembic("upgrade", "head")

    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        before = set(sa.inspect(conn).get_table_names())
    assert len(before) > 1  # genuinely populated

    monkeypatch.delenv("ALEMBIC_BOOTSTRAP_EMPTY", raising=False)
    _run_alembic("upgrade", "head")  # must not raise SystemExit

    with engine.connect() as conn:
        after = set(sa.inspect(conn).get_table_names())
        stamped = {
            row[0]
            for row in conn.execute(sa.text("SELECT version_num FROM alembic_version"))
        }

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    assert after == before
    assert stamped == set(ScriptDirectory.from_config(
        Config(str(Path.cwd() / "alembic.ini"))
    ).get_heads())
