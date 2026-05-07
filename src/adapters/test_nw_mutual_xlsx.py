"""Tests for the Northwestern Mutual whole-life XLSX importer (Phase 4 T4).

Builds an in-memory fixture workbook that mirrors the real
``nw-mutual/allAccounts.xlsx`` (4 policy rows; the 4th has
``Net Accumulated Value = "N/A"`` and must be skipped with a warning).

Uses an in-memory SQLite engine with FK enforcement, mirroring
``test_xlsx_savings_plan.py``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from src.adapters.nw_mutual_xlsx import (
    SOURCE_TAG,
    ImportResult,
    import_balances,
    parse_workbook,
)
from src.models.base import Base
from src.models.brokerage import Account
from src.models.history import AccountBalanceSnapshot
from src.models.ingestion_log import IngestionLog

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def session() -> Session:
    """Fresh in-memory SQLite with FK enforcement."""
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _):  # type: ignore[no-untyped-def]
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return Session(bind=engine)


def _build_fixture_workbook(path: Path) -> None:
    """Mirror the real allAccounts.xlsx — 4 policy rows, last has N/A balance."""
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Life Insurance"

    headers = [
        "Insured",
        "Account Number",
        "Net Death Benefit",
        "Annualized Premium",
        "Last Annual Dividend",
        "Loans",
        "Net Accumulated Value",
    ]
    for col_idx, h in enumerate(headers, start=1):
        ws.cell(row=1, column=col_idx, value=h)

    rows = [
        ("Aiden C Sparks", "17397277", "$32,216.00", "$200.16", "$80.37", "$0.00", "$3,250.34"),
        ("Travis D Sparks", "18305148", "$28,547.00", "$327.84", "$157.65", "$0.00", "$5,621.63"),
        ("Travis D Sparks", "17399215", "$28,327.00", "$349.80", "$163.75", "$0.00", "$7,280.48"),
        ("Travis D Sparks", "17399232", "$275,000.00", "$601.92", "$117.86", "$0.00", "N/A"),
    ]
    for r_idx, row in enumerate(rows, start=2):
        for c_idx, val in enumerate(row, start=1):
            ws.cell(row=r_idx, column=c_idx, value=val)

    wb.save(path)


@pytest.fixture
def fixture_xlsx(tmp_path: Path) -> Path:
    p = tmp_path / "nw_mutual_fixture.xlsx"
    _build_fixture_workbook(p)
    return p


def _seed_accounts(session: Session, policies: list[str]) -> dict[str, str]:
    """Seed Account rows for each policy. Returns {policy_number: account_id}."""
    ids: dict[str, str] = {}
    for policy in policies:
        acct = Account(
            broker="nw_mutual",
            account_number=policy,
            account_type="other",
            entity="personal",
            account_name=f"NW Mutual {policy}",
        )
        session.add(acct)
        session.flush()
        ids[policy] = acct.id
    session.commit()
    return ids


ALL_POLICIES = ["17397277", "18305148", "17399215", "17399232"]


# ── parse_workbook ───────────────────────────────────────────────────────────


def test_parse_workbook_returns_4_dicts(fixture_xlsx: Path) -> None:
    rows = parse_workbook(fixture_xlsx)
    assert len(rows) == 4
    assert {r["policy_number"] for r in rows} == set(ALL_POLICIES)
    # Required keys per row.
    expected_keys = {
        "policy_number",
        "insured",
        "net_death_benefit",
        "annualized_premium",
        "last_annual_dividend",
        "loans",
        "net_accum_value",
    }
    for r in rows:
        assert expected_keys.issubset(r.keys())


def test_parse_workbook_na_row_yields_none(fixture_xlsx: Path) -> None:
    rows = parse_workbook(fixture_xlsx)
    by_policy = {r["policy_number"]: r for r in rows}
    assert by_policy["17399232"]["net_accum_value"] is None
    # Other rows have parsed Decimals.
    assert by_policy["17397277"]["net_accum_value"] == Decimal("3250.34")
    assert by_policy["18305148"]["net_accum_value"] == Decimal("5621.63")
    assert by_policy["17399215"]["net_accum_value"] == Decimal("7280.48")


def test_parse_workbook_other_columns(fixture_xlsx: Path) -> None:
    rows = parse_workbook(fixture_xlsx)
    aiden = next(r for r in rows if r["policy_number"] == "17397277")
    assert aiden["insured"] == "Aiden C Sparks"
    assert aiden["net_death_benefit"] == Decimal("32216.00")
    assert aiden["annualized_premium"] == Decimal("200.16")
    assert aiden["last_annual_dividend"] == Decimal("80.37")
    assert aiden["loans"] == Decimal("0.00")


# ── import_balances dry-run ──────────────────────────────────────────────────


def test_dry_run_reports_3_inserts_and_skip_warning(fixture_xlsx: Path) -> None:
    result = import_balances(fixture_xlsx, dry_run=True, as_of=date(2026, 5, 7))
    assert isinstance(result, ImportResult)
    # 4 parsed, 3 would-be-inserted, 1 skipped with warning in errors.
    assert result.parsed == 4
    assert result.would_insert == 3
    # The N/A row appears as a warning in errors.
    assert any("17399232" in e and "N/A" in e for e in result.errors), result.errors


# ── import_balances apply ───────────────────────────────────────────────────


def test_apply_writes_three_snapshots(session: Session, fixture_xlsx: Path) -> None:
    _seed_accounts(session, ALL_POLICIES)
    result = import_balances(
        fixture_xlsx, dry_run=False, session=session, as_of=date(2026, 5, 7)
    )
    assert result.imported == 3
    snaps = session.query(AccountBalanceSnapshot).all()
    assert len(snaps) == 3
    # Each surviving row has the right tag/source/balance.
    by_raw = {s.raw_account_name: s for s in snaps}
    assert "NW Mutual Aiden C Sparks 17397277" in by_raw
    assert by_raw["NW Mutual Aiden C Sparks 17397277"].balance == Decimal("3250.34")
    assert by_raw["NW Mutual Aiden C Sparks 17397277"].source == SOURCE_TAG
    # Account-id linkage by (broker='nw_mutual', account_number).
    seeded = {a.account_number: a.id for a in session.query(Account).all()}
    assert by_raw["NW Mutual Aiden C Sparks 17397277"].account_id == seeded["17397277"]
    # IngestionLog written.
    logs = session.query(IngestionLog).all()
    assert len(logs) == 1


def test_apply_is_idempotent(session: Session, fixture_xlsx: Path) -> None:
    _seed_accounts(session, ALL_POLICIES)
    first = import_balances(
        fixture_xlsx, dry_run=False, session=session, as_of=date(2026, 5, 7)
    )
    assert first.imported == 3
    second = import_balances(
        fixture_xlsx, dry_run=False, session=session, as_of=date(2026, 5, 7)
    )
    assert second.imported == 0
    assert second.dup_skipped == 3
    assert session.query(AccountBalanceSnapshot).count() == 3


def test_unmapped_policy_appends_error(session: Session, fixture_xlsx: Path) -> None:
    # Seed only 2 of the 3 valid policies; one will be unmapped.
    _seed_accounts(session, ["17397277", "18305148"])
    result = import_balances(
        fixture_xlsx, dry_run=False, session=session, as_of=date(2026, 5, 7)
    )
    # 2 imported, 1 unmapped error, 1 N/A skip warning → 2 errors total.
    assert result.imported == 2
    assert any("17399215" in e for e in result.errors), result.errors
    snaps = session.query(AccountBalanceSnapshot).all()
    assert len(snaps) == 2


def test_default_as_of_uses_file_mtime(session: Session, fixture_xlsx: Path) -> None:
    """When as_of is omitted, the file mtime date is used."""
    _seed_accounts(session, ALL_POLICIES)
    result = import_balances(fixture_xlsx, dry_run=False, session=session)
    assert result.imported == 3
    expected = date.fromtimestamp(fixture_xlsx.stat().st_mtime)
    snaps = session.query(AccountBalanceSnapshot).all()
    for s in snaps:
        assert s.as_of == expected
