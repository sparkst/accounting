"""Tests for ``scripts/cleanup-stale-position-snapshots.py`` (TASK-13).

Covers:
  * ``symbol = 'TOTAL'`` (case-insensitive) deletion.
  * ``symbol LIKE 'Generated %'`` deletion.
  * Duplicate ``(account_id, COALESCE(symbol, description), as_of)`` collapse —
    keep ``MIN(id)``, delete the rest.
  * DRY-RUN (default) makes no changes.
  * ``--apply`` actually deletes; legitimate distinct rows survive.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Generator
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from src.models.base import Base
from src.models.brokerage import Account, PositionSnapshot
from src.models.enums import AccountType, Entity
from src.models.ingestion_log import IngestionLog

# Load scripts/cleanup-stale-position-snapshots.py as a module (hyphenated filename).
_THIS_DIR = Path(__file__).parent
_SPEC = importlib.util.spec_from_file_location(
    "_cleanup_stale_position_snapshots",
    _THIS_DIR / "cleanup-stale-position-snapshots.py",
)
assert _SPEC is not None and _SPEC.loader is not None
cleanup_cli = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cleanup_cli)


# ─────────────────────────────────────────────────────────────────────────────
# Session fixture (in-memory SQLite + FK enforcement)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="function")
def session() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):  # type: ignore[no-untyped-def]
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    SessionCls = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    s = SessionCls()
    yield s
    s.close()
    engine.dispose()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — seed accounts + snapshots
# ─────────────────────────────────────────────────────────────────────────────


def _make_account(session: Session, broker: str = "etrade", number: str = "6354") -> Account:
    a = Account(
        broker=broker,
        account_number=number,
        account_type=AccountType.TAXABLE.value,
        entity=Entity.PERSONAL.value,
        tax_sheltered=False,
    )
    session.add(a)
    session.flush()
    return a


def _make_snap(
    session: Session,
    account: Account,
    *,
    symbol: str | None,
    description: str | None = None,
    as_of: datetime | None = None,
    source_file: str = "test.csv",
    source_row_hash: str | None = None,
    quantity: str = "1.00",
) -> PositionSnapshot:
    """Factory for PositionSnapshot rows. ``source_row_hash`` defaults to a
    unique value derived from ``id(snap)``-equivalent inputs so different
    rows in the same test don't collide on the dedup UNIQUE constraint."""
    if as_of is None:
        as_of = datetime(2026, 5, 4, 12, 0, 0)
    if source_row_hash is None:
        # Manufacture a unique-ish hash; only its uniqueness matters here.
        source_row_hash = f"hash-{symbol or description}-{quantity}-{source_file}"
    snap = PositionSnapshot(
        account_id=account.id,
        as_of=as_of,
        symbol=symbol,
        description=description,
        quantity=None,
        price=None,
        market_value=None,
        source_file=source_file,
        source_row_hash=source_row_hash,
        raw_data={},
    )
    session.add(snap)
    session.flush()
    return snap


# ─────────────────────────────────────────────────────────────────────────────
# Cleanup behaviour
# ─────────────────────────────────────────────────────────────────────────────


def test_dry_run_makes_no_changes(session: Session) -> None:
    """Default (apply=False) reports candidates but leaves the DB unchanged."""
    account = _make_account(session)
    _make_snap(session, account, symbol="TOTAL", source_row_hash="h1")
    _make_snap(session, account, symbol="Generated at May 4 2026", source_row_hash="h2")
    _make_snap(session, account, symbol="AAPL", source_row_hash="h3")
    session.commit()

    before = session.execute(select(PositionSnapshot)).scalars().all()
    assert len(before) == 3

    total, deleted, skipped = cleanup_cli.cleanup(session, apply=False)
    assert total == 3
    assert deleted == 0
    assert skipped == 2  # TOTAL + Generated

    after = session.execute(select(PositionSnapshot)).scalars().all()
    assert len(after) == 3  # unchanged


def test_apply_deletes_total_rows_case_insensitive(session: Session) -> None:
    """``symbol = 'TOTAL'`` rows are removed; matching is case-insensitive."""
    account = _make_account(session)
    _make_snap(session, account, symbol="TOTAL", source_row_hash="h1")
    _make_snap(session, account, symbol="total", source_row_hash="h2")
    _make_snap(session, account, symbol="Total", source_row_hash="h3")
    keep = _make_snap(session, account, symbol="AAPL", source_row_hash="h4")
    session.commit()

    total, deleted, skipped = cleanup_cli.cleanup(session, apply=True)
    assert deleted == 3

    remaining = session.execute(select(PositionSnapshot)).scalars().all()
    assert len(remaining) == 1
    assert remaining[0].id == keep.id


def test_apply_deletes_generated_metadata_rows(session: Session) -> None:
    """``symbol LIKE 'Generated %'`` rows are removed."""
    account = _make_account(session)
    _make_snap(
        session, account,
        symbol="Generated at May 4 2026 02:47 PM ET",
        source_row_hash="h1",
    )
    _make_snap(session, account, symbol="Generated_xxx", source_row_hash="h2")
    keep = _make_snap(session, account, symbol="MSFT", source_row_hash="h3")
    session.commit()

    total, deleted, skipped = cleanup_cli.cleanup(session, apply=True)
    assert deleted == 1  # Only the 'Generated at ...' row; 'Generated_xxx' has no space

    remaining = session.execute(select(PositionSnapshot)).scalars().all()
    # Only the MSFT row survives. "Generated_xxx" (no space) does NOT match
    # the LIKE 'Generated %' pattern, so it's preserved by design.
    symbols = sorted(r.symbol or "" for r in remaining)
    assert "MSFT" in symbols
    assert "Generated at May 4 2026 02:47 PM ET" not in symbols
    assert keep.id in {r.id for r in remaining}


def test_apply_collapses_duplicate_groups_keep_min_id(session: Session) -> None:
    """Duplicate (account_id, symbol|description, as_of) → keep MIN(id)."""
    account = _make_account(session)
    as_of = datetime(2026, 5, 4, 12, 0, 0)
    a = _make_snap(
        session, account, symbol="MGK", as_of=as_of, source_row_hash="h-a"
    )
    b = _make_snap(
        session, account, symbol="MGK", as_of=as_of, source_row_hash="h-b"
    )
    c = _make_snap(
        session, account, symbol="MGK", as_of=as_of, source_row_hash="h-c"
    )
    # Different as_of → not a duplicate of the group above.
    other = _make_snap(
        session, account,
        symbol="MGK",
        as_of=datetime(2026, 4, 1),
        source_row_hash="h-d",
    )
    session.commit()

    total, deleted, skipped = cleanup_cli.cleanup(session, apply=True)
    assert deleted == 2  # 2 extras deleted from the 3-row group

    remaining_ids = {
        r.id for r in session.execute(select(PositionSnapshot)).scalars().all()
    }
    # Group (account, MGK, 2026-05-04): keep MIN(id).
    expected_keep = min((a.id, b.id, c.id))
    assert expected_keep in remaining_ids
    # The other two from that group are gone.
    assert remaining_ids & {a.id, b.id, c.id} == {expected_keep}
    # The 2026-04-01 row survives — distinct as_of.
    assert other.id in remaining_ids


def test_apply_treats_description_as_key_when_symbol_is_null(
    session: Session,
) -> None:
    """For 529 plan rows where ``symbol`` is NULL, the dedup key falls back to
    ``description`` so two identical (account, description, as_of) rows collapse.
    """
    account = _make_account(session, broker="vanguard", number="208182839-01")
    as_of = datetime(2026, 5, 4, 12, 0, 0)
    a = _make_snap(
        session, account,
        symbol=None,
        description="Vanguard 500 Index Portfolio",
        as_of=as_of,
        source_row_hash="h-1",
    )
    b = _make_snap(
        session, account,
        symbol=None,
        description="Vanguard 500 Index Portfolio",
        as_of=as_of,
        source_row_hash="h-2",
    )
    session.commit()

    total, deleted, skipped = cleanup_cli.cleanup(session, apply=True)
    assert deleted == 1

    remaining = session.execute(select(PositionSnapshot)).scalars().all()
    assert len(remaining) == 1
    assert remaining[0].id == min(a.id, b.id)


def test_apply_preserves_clean_rows(session: Session) -> None:
    """A DB with no garbage and no dupes is unchanged by cleanup."""
    account = _make_account(session)
    _make_snap(session, account, symbol="AAPL", source_row_hash="h1")
    _make_snap(session, account, symbol="MSFT", source_row_hash="h2")
    _make_snap(session, account, symbol="TSLA", source_row_hash="h3")
    session.commit()

    before = session.execute(select(PositionSnapshot)).scalars().all()
    assert len(before) == 3

    total, deleted, skipped = cleanup_cli.cleanup(session, apply=True)
    assert deleted == 0

    after = session.execute(select(PositionSnapshot)).scalars().all()
    assert len(after) == 3
    assert {r.symbol for r in after} == {"AAPL", "MSFT", "TSLA"}


def test_apply_writes_ingestion_log(session: Session) -> None:
    """P1-C: after apply=True, an IngestionLog row is written."""
    account = _make_account(session)
    _make_snap(session, account, symbol="TOTAL", source_row_hash="t1")
    _make_snap(session, account, symbol="AAPL", source_row_hash="t2")
    session.commit()

    total, deleted, skipped = cleanup_cli.cleanup(session, apply=True)
    assert deleted == 1

    logs = session.execute(
        select(IngestionLog).where(
            IngestionLog.source == "cleanup-position-snapshots"
        )
    ).scalars().all()
    assert len(logs) == 1
    log = logs[0]
    assert log.status == "success"
    assert log.records_processed == deleted
    assert log.records_failed == 0
    assert log.error_detail is not None
    import json
    detail = json.loads(log.error_detail)
    assert detail["by_category"]["TOTAL_DELETED"] == deleted


def test_main_dry_run_exits_zero(session: Session, capsys: pytest.CaptureFixture[str]) -> None:
    """R2 P2-006: cleanup main() dry-run returns 0 and prints summary."""
    account = _make_account(session)
    _make_snap(session, account, symbol="TOTAL", source_row_hash="main1")
    session.commit()

    # Patch SessionLocal so main() uses our in-memory session.
    import unittest.mock as mock
    with mock.patch.object(cleanup_cli, "SessionLocal", return_value=session):
        rc = cleanup_cli.main([])
    assert rc == 0
    out, _ = capsys.readouterr()
    assert "DRY-RUN" in out


def test_dry_run_no_double_count_overlapping_rows(session: Session) -> None:
    """P1-D: a row that qualifies for multiple selectors is counted only once."""
    account = _make_account(session)
    as_of = datetime(2026, 5, 4, 12, 0, 0)
    # This row matches BOTH 'TOTAL' (case-insensitive) and could theoretically
    # appear in the dup set if there were duplicates. Add two TOTAL rows so they
    # appear in BOTH total_rows AND dup_extras.
    _make_snap(session, account, symbol="TOTAL", as_of=as_of, source_row_hash="o1")
    _make_snap(session, account, symbol="TOTAL", as_of=as_of, source_row_hash="o2")
    session.commit()

    total, deleted, skipped = cleanup_cli.cleanup(session, apply=False)
    # 2 TOTAL rows + 1 dup extra = 3 naive sum, but both TOTAL rows are in the
    # combined set, and the dup extra is one of those same TOTAL rows.
    # Combined unique IDs = 2 (both TOTAL rows). Dry-run returns len(combined_ids).
    assert skipped == 2
