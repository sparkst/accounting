"""Tests for the Vanguard CSV adapter (Phase 4 — T2).

Target spec: ``proposals/brokerage-phase4/PLAN.md`` task T2. The adapter
follows the canonical pattern from ``src/adapters/xlsx_savings_plan.py``:

* ``ImportResult`` dataclass return type.
* ``dry_run=True`` default to protect the live DB during exploration.
* Per-row ``session.begin_nested()`` savepoint for error isolation.
* ``Decimal(str(value))`` numeric coercion at the boundary (via
  ``src.adapters._shared.money``).
* ``source_row_hash`` with quantized numeric components for idempotent
  re-import.
* ``IngestionLog`` row written on every ``--apply`` run.
* CLI subcommand ``import-positions --file <csv> [--apply] [--as-of YYYY-MM-DD]``.

Phase 4 scope: positions block → :class:`PositionSnapshot`. Transactions
block is parsed and counted (``ImportResult.transactions_seen``) but NOT
written — Phase 5 will handle reinvest pairs.

These tests use the real Vanguard fixture files in
``/Users/travis/Downloads/accounts/vanguard/`` read-only. If those files
are unavailable, the affected tests skip (so CI on a fresh machine still
passes the inline-fixture suite).
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from src.adapters.vanguard_csv import (
    ADAPTER_NAME,
    SOURCE_TAG,
    ImportResult,
    detect_csv_flavor,
    import_positions,
    split_blocks,
)
from src.models.base import Base
from src.models.brokerage import Account, PositionSnapshot
from src.models.enums import AccountType, Broker, Entity
from src.models.ingestion_log import IngestionLog

# ── Fixtures ─────────────────────────────────────────────────────────────────

REAL_FIXTURE_DIR = Path("/Users/travis/Downloads/accounts/vanguard")
REAL_BROKERAGE_AMY = REAL_FIXTURE_DIR / "OfxDownload.csv"
REAL_BROKERAGE_TRAVIS = REAL_FIXTURE_DIR / "OfxDownload-travis.csv"
REAL_529 = REAL_FIXTURE_DIR / "ofxdownload_05042026.csv"

# Inline CSV fixtures — copy of the real shapes, sanitised to small row counts.
INLINE_BROKERAGE = (
    "Account Number,Investment Name,Symbol,Shares,Share Price,Total Value,\n"
    "65344815,VANGUARD S&P 500 INDEX ETF,VOO,81.65,659.35,53835.93,\n"
    "65344815,VANGUARD FEDERAL MONEY MARKET INVESTOR CL,VMFXX,14873.89,1,14873.89,\n"
    "70862729,VANGUARD MEGA CAP GROWTH ETF,MGK,103.8863,84.51,8779.43,\n"
    "\n"
    "\n"
    "Account Number,Trade Date,Settlement Date,Transaction Type,"
    "Transaction Description,Investment Name,Symbol,Shares,Share Price,"
    "Principal Amount,Commissions and Fees,Net Amount,Accrued Interest,"
    "Account Type,\n"
    "65344815,2025-05-23,2025-05-27,Buy,Buy,VANGUARD MEGA CAP VALUE ETF,"
    "MGV,286.57,125.205,-35880.00,0.0,-35880.00,0.0,CASH,\n"
    "70862729,2025-05-23,2025-05-27,Buy,Buy,VANGUARD MEGA CAP GROWTH ETF,"
    "MGK,103.8863,84.51,-8779.43,0.0,-8779.43,0.0,CASH,\n"
)

INLINE_529 = (
    "Fund Account Number,Fund Name,Price,Shares,Total Value\n"
    "208182839-01,Vanguard 500 Index Portfolio,$116.24,293.8615,$34158.46\n"
    "208182839-01,Vanguard Short-Term Bond Index Portfolio,"
    "$10.79,5273.53,$56901.39\n"
    "\n"
    "Account Number,Trade Date,Process Date,Transaction Type,"
    "Transaction Description,Investment Name,Share Price,Shares,"
    "Gross Amount,Net Amount\n"
    "208182839-01,03/02/2026,03/02/2026,Qualified w/d Acct Owner,"
    "Qualified w/d Acct Owner ACH,Vanguard 500 Index Portfolio,"
    "$109.92,-12.9697,$-1425.63,$-1425.63\n"
)

# Inline brokerage with a malformed shares cell on row 2 (account 70862729).
INLINE_BROKERAGE_MALFORMED = (
    "Account Number,Investment Name,Symbol,Shares,Share Price,Total Value,\n"
    "65344815,VANGUARD S&P 500 INDEX ETF,VOO,81.65,659.35,53835.93,\n"
    "70862729,VANGUARD MEGA CAP GROWTH ETF,MGK,not-a-number,84.51,8779.43,\n"
    "65344815,VANGUARD FEDERAL MONEY MARKET INVESTOR CL,VMFXX,1,1,1,\n"
)

# Inline brokerage with one mapped + one unmapped account_number.
INLINE_BROKERAGE_UNMAPPED = (
    "Account Number,Investment Name,Symbol,Shares,Share Price,Total Value,\n"
    "65344815,VANGUARD S&P 500 INDEX ETF,VOO,81.65,659.35,53835.93,\n"
    "99999999,UNKNOWN ACCOUNT HOLDING,XXX,1,1,1,\n"
    "65344815,VANGUARD FEDERAL MONEY MARKET INVESTOR CL,VMFXX,14873.89,1,14873.89,\n"
)


@pytest.fixture
def session() -> Generator[Session, None, None]:
    """Fresh in-memory SQLite session per test, with FK enforcement on."""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _):  # type: ignore[no-untyped-def]
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    SessionCls = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    s = SessionCls()
    yield s
    s.close()
    engine.dispose()


def _seed_account(
    session: Session,
    account_number: str,
    *,
    account_type: AccountType = AccountType.TAXABLE,
) -> Account:
    """Insert a Vanguard Account row and return it."""
    a = Account(
        broker=Broker.VANGUARD.value,
        account_number=account_number,
        account_type=account_type.value,
        entity=Entity.PERSONAL.value,
    )
    session.add(a)
    session.commit()
    return a


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


# ── Pure helpers ─────────────────────────────────────────────────────────────


def test_detect_csv_flavor_brokerage() -> None:
    header = "Account Number,Investment Name,Symbol,Shares,Share Price,Total Value,"
    assert detect_csv_flavor(header) == "brokerage"


def test_detect_csv_flavor_529() -> None:
    header = "Fund Account Number,Fund Name,Price,Shares,Total Value"
    assert detect_csv_flavor(header) == "529"


def test_detect_csv_flavor_unknown_raises() -> None:
    with pytest.raises(ValueError, match="unknown vanguard csv flavor"):
        detect_csv_flavor("totally,unrelated,header")


def test_split_blocks_separates_positions_and_transactions() -> None:
    blocks = split_blocks(INLINE_BROKERAGE)
    assert len(blocks) == 2
    pos_header, pos_rows = blocks[0]
    tx_header, tx_rows = blocks[1]
    assert pos_header.startswith("Account Number,Investment Name")
    assert len(pos_rows) == 3
    assert tx_header.startswith("Account Number,Trade Date")
    assert len(tx_rows) == 2


def test_split_blocks_handles_single_block_529() -> None:
    blocks = split_blocks(INLINE_529)
    assert len(blocks) == 2
    pos_header, pos_rows = blocks[0]
    tx_header, tx_rows = blocks[1]
    assert pos_header.startswith("Fund Account Number")
    assert len(pos_rows) == 2
    assert tx_header.startswith("Account Number,Trade Date,Process Date")
    assert len(tx_rows) == 1


# ── Inline-fixture tests ─────────────────────────────────────────────────────


def test_import_positions_dry_run_inline_brokerage(
    session: Session, tmp_path: Path
) -> None:
    _seed_account(session, "65344815")
    _seed_account(session, "70862729")
    csv = _write(tmp_path, "OfxDownload.csv", INLINE_BROKERAGE)

    result = import_positions(csv, dry_run=True, session=session,
                              as_of=date(2026, 5, 1))

    assert isinstance(result, ImportResult)
    assert result.parsed == 3
    assert result.imported == 0
    assert result.transactions_seen == 2
    # Dry-run must not write IngestionLog rows.
    assert session.query(func.count(IngestionLog.id)).scalar() == 0
    assert session.query(func.count(PositionSnapshot.id)).scalar() == 0


def test_import_positions_apply_writes_position_snapshots(
    session: Session, tmp_path: Path
) -> None:
    a1 = _seed_account(session, "65344815")
    a2 = _seed_account(session, "70862729")
    csv = _write(tmp_path, "OfxDownload.csv", INLINE_BROKERAGE)

    result = import_positions(csv, dry_run=False, session=session,
                              as_of=date(2026, 5, 1))

    assert result.imported == 3
    assert result.parsed == 3
    assert not result.errors
    # Three snapshots, two for a1 and one for a2.
    rows = session.execute(select(PositionSnapshot)).scalars().all()
    assert len(rows) == 3
    by_account = {r.account_id: 0 for r in rows}
    for r in rows:
        by_account[r.account_id] += 1
    assert by_account[a1.id] == 2
    assert by_account[a2.id] == 1
    # source_file is the basename.
    assert all(r.source_file == "OfxDownload.csv" for r in rows)
    # Decimal precision preserved.
    voo = next(r for r in rows if r.symbol == "VOO")
    assert voo.quantity == Decimal("81.65")
    assert voo.price == Decimal("659.35")
    assert voo.market_value == Decimal("53835.93")
    # IngestionLog row written.
    log = session.execute(select(IngestionLog)).scalar_one()
    assert log.source == ADAPTER_NAME
    assert log.records_processed == 3


def test_import_positions_dedupes_on_second_apply(
    session: Session, tmp_path: Path
) -> None:
    _seed_account(session, "65344815")
    _seed_account(session, "70862729")
    csv = _write(tmp_path, "OfxDownload.csv", INLINE_BROKERAGE)

    first = import_positions(csv, dry_run=False, session=session,
                             as_of=date(2026, 5, 1))
    assert first.imported == 3

    second = import_positions(csv, dry_run=False, session=session,
                              as_of=date(2026, 5, 1))
    assert second.imported == 0
    assert second.dup_skipped == 3
    # Still only 3 PositionSnapshot rows total.
    assert session.query(func.count(PositionSnapshot.id)).scalar() == 3


def test_import_positions_per_row_error_isolation(
    session: Session, tmp_path: Path
) -> None:
    _seed_account(session, "65344815")
    _seed_account(session, "70862729")
    csv = _write(tmp_path, "OfxDownload.csv", INLINE_BROKERAGE_MALFORMED)

    result = import_positions(csv, dry_run=False, session=session,
                              as_of=date(2026, 5, 1))

    assert len(result.errors) == 1
    # Two good rows (VOO + VMFXX) inserted; the malformed MGK row skipped.
    assert result.imported == 2
    snaps = session.execute(select(PositionSnapshot)).scalars().all()
    symbols = sorted(s.symbol for s in snaps if s.symbol is not None)
    assert symbols == ["VMFXX", "VOO"]


def test_import_positions_unmapped_account_appends_error(
    session: Session, tmp_path: Path
) -> None:
    _seed_account(session, "65344815")
    csv = _write(tmp_path, "OfxDownload.csv", INLINE_BROKERAGE_UNMAPPED)

    result = import_positions(csv, dry_run=False, session=session,
                              as_of=date(2026, 5, 1))

    # The unmapped 99999999 row should produce an error and not insert,
    # but the two 65344815 rows should still land.
    assert result.imported == 2
    assert any("99999999" in e for e in result.errors)
    snaps = session.execute(select(PositionSnapshot)).scalars().all()
    assert {s.symbol for s in snaps} == {"VOO", "VMFXX"}


def test_import_positions_529_writes_with_null_symbol(
    session: Session, tmp_path: Path
) -> None:
    _seed_account(session, "208182839-01", account_type=AccountType.K529)
    csv = _write(tmp_path, "ofxdownload_05042026.csv", INLINE_529)

    result = import_positions(csv, dry_run=False, session=session,
                              as_of=date(2026, 5, 4))

    assert result.imported == 2
    assert result.transactions_seen == 1
    snaps = session.execute(select(PositionSnapshot)).scalars().all()
    assert len(snaps) == 2
    # 529 has no ticker — symbol must be None and description carries fund name.
    assert all(s.symbol is None for s in snaps)
    assert {s.description for s in snaps} == {
        "Vanguard 500 Index Portfolio",
        "Vanguard Short-Term Bond Index Portfolio",
    }
    # market_value parsed from "$34158.46"-style strings.
    by_desc = {s.description: s for s in snaps}
    five_hundred = by_desc["Vanguard 500 Index Portfolio"]
    assert five_hundred.market_value == Decimal("34158.46")
    assert five_hundred.price == Decimal("116.24")
    assert five_hundred.quantity == Decimal("293.8615")


def test_import_positions_dry_run_with_no_matching_account_still_parses(
    session: Session, tmp_path: Path
) -> None:
    # No accounts seeded — dry-run still parses, errors are populated for
    # every row but inserted stays 0 and no DB writes happen.
    csv = _write(tmp_path, "OfxDownload.csv", INLINE_BROKERAGE)

    result = import_positions(csv, dry_run=True, session=session,
                              as_of=date(2026, 5, 1))

    assert result.parsed == 3
    assert result.imported == 0
    # Dry-run on unmapped accounts should also surface them as errors so the
    # operator sees what's missing before --apply.
    assert any("65344815" in e for e in result.errors)
    assert any("70862729" in e for e in result.errors)


def test_as_of_defaults_to_file_mtime(
    session: Session, tmp_path: Path
) -> None:
    """When as_of is omitted the adapter uses file mtime truncated to date."""
    _seed_account(session, "65344815")
    _seed_account(session, "70862729")
    csv = _write(tmp_path, "OfxDownload.csv", INLINE_BROKERAGE)

    # Set a known mtime.
    import os
    target = date(2026, 4, 30)
    import time
    epoch = time.mktime(target.timetuple())
    os.utime(csv, (epoch, epoch))

    result = import_positions(csv, dry_run=False, session=session)
    assert result.imported == 3
    snap = session.execute(select(PositionSnapshot)).scalars().first()
    assert snap is not None
    assert snap.as_of.date() == target


# ── Real-fixture smoke tests (skip if files absent) ──────────────────────────


@pytest.mark.skipif(
    not REAL_BROKERAGE_AMY.exists(),
    reason=f"real fixture missing: {REAL_BROKERAGE_AMY}",
)
def test_real_amy_brokerage_dry_run(session: Session) -> None:
    _seed_account(session, "65344815")
    _seed_account(session, "70862729")
    result = import_positions(REAL_BROKERAGE_AMY, dry_run=True,
                              session=session, as_of=date(2026, 5, 1))
    assert result.parsed > 0
    assert result.imported == 0


@pytest.mark.skipif(
    not REAL_529.exists(),
    reason=f"real fixture missing: {REAL_529}",
)
def test_real_529_dry_run(session: Session) -> None:
    _seed_account(session, "208182839-01", account_type=AccountType.K529)
    result = import_positions(REAL_529, dry_run=True,
                              session=session, as_of=date(2026, 5, 4))
    assert result.parsed > 0
    assert result.transactions_seen > 0


# ── Constants / contract checks ──────────────────────────────────────────────


def test_source_tag_constant() -> None:
    assert SOURCE_TAG == "vanguard_csv"


def test_adapter_name_constant() -> None:
    assert ADAPTER_NAME == "vanguard_csv"
