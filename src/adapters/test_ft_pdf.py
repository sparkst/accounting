"""Tests for the Franklin Templeton year-end statement PDF adapter.

Mirrors the in-memory SQLite + FK-on pattern from
``test_xlsx_savings_plan.py``. Uses real sample PDFs from
``/Users/travis/Downloads/accounts/FT/`` (read-only) to exercise the
end-to-end import path; falls back with ``pytest.skip`` when those files
are not present so the test suite stays portable.
"""

from __future__ import annotations

import shutil
from collections.abc import Generator
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from src.adapters.ft_pdf import (
    ADAPTER_NAME,
    FT_ACCOUNT_NUMBER,
    FT_BROKER,
    SOURCE_TAG,
    ImportResult,
    count_csv_transactions,
    extract_portfolio_overview,
    import_statements,
    parse_statement_filename,
)
from src.models.base import Base
from src.models.brokerage import Account
from src.models.history import AccountBalanceSnapshot
from src.models.ingestion_log import IngestionLog

# Real-data anchors. Tests that depend on them skip cleanly if absent.
_FT_DIR = Path("/Users/travis/Downloads/accounts/FT")
_FT_2024 = _FT_DIR / "2024-12-31.pdf"
_FT_2026Q1 = _FT_DIR / "2026-03-31.pdf"
_FT_OLD = _FT_DIR / "2000-12-31.pdf"  # legacy format → unparseable in v1
_FT_CSV = _FT_DIR / "accounthistory.csv"


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def session() -> Generator[Session, None, None]:
    """Fresh in-memory SQLite with FK enforcement. Yields and cleans up."""
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _):  # type: ignore[no-untyped-def]
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    s = Session(bind=engine)
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture
def seeded_account(session: Session) -> Account:
    """Account row matching the FT statement contract."""
    acct = Account(
        broker=FT_BROKER,
        account_number="8291",
        account_name="Templeton Growth Fund",
        account_type="other",
        entity="personal",
    )
    session.add(acct)
    session.commit()
    return acct


# ── parse_statement_filename ─────────────────────────────────────────────────


def test_parse_statement_filename_year_end() -> None:
    assert parse_statement_filename("2024-12-31.pdf") == date(2024, 12, 31)


def test_parse_statement_filename_quarter_end() -> None:
    assert parse_statement_filename("2026-03-31.pdf") == date(2026, 3, 31)


def test_parse_statement_filename_with_path() -> None:
    """Accepts bare basename — caller passes ``Path.name``."""
    assert parse_statement_filename("2020-12-31.pdf") == date(2020, 12, 31)


def test_parse_statement_filename_rejects_portal_pdf() -> None:
    with pytest.raises(ValueError):
        parse_statement_filename("Portfolio Performance - Online Account Access.pdf")


def test_parse_statement_filename_rejects_non_pdf() -> None:
    with pytest.raises(ValueError):
        parse_statement_filename("2024-12-31.txt")


def test_parse_statement_filename_rejects_garbage_date() -> None:
    with pytest.raises(ValueError):
        parse_statement_filename("2024-13-99.pdf")


# ── extract_portfolio_overview ───────────────────────────────────────────────


_SAMPLE_TEXT = """\
                                                                Year-End Statement
                                                                January 1, 2024 to December 31, 2024

PORTFOLIO OVERVIEW                                                      $16,406.38

PORTFOLIO CHANGES
Beginning Portfolio Value as of 01/01/2024                                         $15,563.60
Portfolio Value as of 12/31/2024                                                   $16,406.38
"""


def test_extract_portfolio_overview_year_end_fixture() -> None:
    assert extract_portfolio_overview(_SAMPLE_TEXT) == Decimal("16406.38")


def test_extract_portfolio_overview_handles_thousands_separator() -> None:
    text = "PORTFOLIO OVERVIEW                                $1,234,567.89\n"
    assert extract_portfolio_overview(text) == Decimal("1234567.89")


def test_extract_portfolio_overview_raises_on_no_match() -> None:
    with pytest.raises(ValueError):
        extract_portfolio_overview("ACCOUNT VALUE: $2,647.46 AT $18.39 PER SHARE")


# ── import_statements (dry-run) ──────────────────────────────────────────────


def test_import_statements_dry_run_mixed_directory(tmp_path: Path) -> None:
    """Two PDFs: one parseable (modern), one not (old format)."""
    if not _FT_2024.exists() or not _FT_OLD.exists():
        pytest.skip("FT sample PDFs not present on this machine")
    shutil.copy(_FT_2024, tmp_path / "2024-12-31.pdf")
    shutil.copy(_FT_OLD, tmp_path / "2000-12-31.pdf")

    result = import_statements(tmp_path, dry_run=True)

    assert isinstance(result, ImportResult)
    # One file parses cleanly (modern PDF), one yields an error (legacy format).
    assert result.imported == 0  # dry-run never inserts
    assert result.errors, "expected an error for the legacy-format PDF"
    assert any("2000-12-31.pdf" in e for e in result.errors)


def test_import_statements_dry_run_skips_bogus_filename(tmp_path: Path) -> None:
    """A PDF whose basename isn't YYYY-MM-DD is reported as an error and the
    walk continues to the next file."""
    if not _FT_2024.exists():
        pytest.skip("FT sample PDFs not present")
    shutil.copy(_FT_2024, tmp_path / "Portfolio Performance.pdf")
    shutil.copy(_FT_2024, tmp_path / "2024-12-31.pdf")

    result = import_statements(tmp_path, dry_run=True)
    assert any("Portfolio Performance.pdf" in e for e in result.errors)


# ── import_statements (apply) ────────────────────────────────────────────────


def test_import_statements_apply_writes_one_row_per_pdf(
    tmp_path: Path, session: Session, seeded_account: Account
) -> None:
    if not _FT_2024.exists() or not _FT_2026Q1.exists():
        pytest.skip("FT sample PDFs not present")
    shutil.copy(_FT_2024, tmp_path / "2024-12-31.pdf")
    shutil.copy(_FT_2026Q1, tmp_path / "2026-03-31.pdf")

    result = import_statements(tmp_path, dry_run=False, session=session)

    assert result.imported == 2, f"unexpected: {result}"
    assert result.errors == []
    rows = (
        session.query(AccountBalanceSnapshot)
        .order_by(AccountBalanceSnapshot.as_of)
        .all()
    )
    assert len(rows) == 2
    assert {r.as_of for r in rows} == {date(2024, 12, 31), date(2026, 3, 31)}
    for r in rows:
        assert r.account_id == seeded_account.id
        assert r.source == SOURCE_TAG
        assert r.raw_account_name.startswith("Franklin Templeton")
        assert r.balance > Decimal("0")

    # The apply path emits an IngestionLog row.
    log = (
        session.query(IngestionLog)
        .filter(IngestionLog.source == ADAPTER_NAME)
        .one()
    )
    assert log.records_processed >= 2


def test_import_statements_apply_is_idempotent(
    tmp_path: Path, session: Session, seeded_account: Account
) -> None:
    if not _FT_2024.exists():
        pytest.skip("FT sample PDF not present")
    shutil.copy(_FT_2024, tmp_path / "2024-12-31.pdf")

    first = import_statements(tmp_path, dry_run=False, session=session)
    assert first.imported == 1

    second = import_statements(tmp_path, dry_run=False, session=session)
    assert second.imported == 0
    assert second.dup_skipped == 1
    assert session.query(AccountBalanceSnapshot).count() == 1


def test_import_statements_unmapped_account_errors(
    tmp_path: Path, session: Session
) -> None:
    """No Account row seeded → every parseable PDF errors, nothing inserted."""
    if not _FT_2024.exists():
        pytest.skip("FT sample PDF not present")
    shutil.copy(_FT_2024, tmp_path / "2024-12-31.pdf")

    result = import_statements(tmp_path, dry_run=False, session=session)
    assert result.imported == 0
    assert result.errors, "expected an unmapped-account error"
    assert any("8291" in e or FT_BROKER in e for e in result.errors)
    assert session.query(AccountBalanceSnapshot).count() == 0


def test_import_statements_unparseable_pdf_continues_batch(
    tmp_path: Path, session: Session, seeded_account: Account
) -> None:
    """An unparseable PDF appends an error but does not halt later files."""
    if not _FT_2024.exists() or not _FT_OLD.exists():
        pytest.skip("FT sample PDFs not present")
    shutil.copy(_FT_OLD, tmp_path / "2000-12-31.pdf")
    shutil.copy(_FT_2024, tmp_path / "2024-12-31.pdf")

    result = import_statements(tmp_path, dry_run=False, session=session)
    assert result.imported == 1
    assert result.errors, "expected an error from the legacy PDF"
    assert session.query(AccountBalanceSnapshot).count() == 1


def test_import_statements_empty_directory_is_safe(
    tmp_path: Path, session: Session
) -> None:
    result = import_statements(tmp_path, dry_run=True)
    assert result.imported == 0
    assert result.errors == []


def test_import_statements_apply_requires_session(tmp_path: Path) -> None:
    """``dry_run=False`` without a session must raise rather than silently no-op."""
    result = import_statements(tmp_path, dry_run=False, session=None)
    assert result.errors, "expected a 'session required' error"


# ── count_csv_transactions ───────────────────────────────────────────────────


def test_count_csv_transactions_real_file() -> None:
    if not _FT_CSV.exists():
        pytest.skip("FT accounthistory.csv not present")
    n = count_csv_transactions(_FT_CSV)
    # The real file at time-of-writing has 7 data rows; assert > 0 to keep
    # the test resilient to operator-side appends.
    assert n > 0


def test_count_csv_transactions_handles_empty(tmp_path: Path) -> None:
    p = tmp_path / "empty.csv"
    p.write_text("Account,Fund,Process Date\n")  # header only
    assert count_csv_transactions(p) == 0


def test_count_csv_transactions_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        count_csv_transactions(tmp_path / "does-not-exist.csv")


# ── FIX-K: Inline-mock apply tests (no real PDFs required) ──────────────────

# Reuse the inline fixture text from the extraction tests.
_MOCK_TEXT_2024 = _SAMPLE_TEXT  # date 2024-12-31, balance $16,406.38
_MOCK_TEXT_2026 = """\
                                                                Year-End Statement
                                                                January 1, 2026 to March 31, 2026

PORTFOLIO OVERVIEW                                                      $17,500.00
"""


def _seed_ft_account(session: Session) -> Account:
    acct = Account(
        broker=FT_BROKER,
        account_number=FT_ACCOUNT_NUMBER,
        account_name="Templeton Growth Fund",
        account_type="other",
        entity="personal",
    )
    session.add(acct)
    session.commit()
    return acct


def test_import_statements_inline_mock_writes_one_row_per_file(
    tmp_path: Path, session: Session
) -> None:
    """FIX-K Test 1: apply writes one row per PDF; balance is pinned."""
    _seed_ft_account(session)

    # Two statement PDFs with names that parse to valid dates.
    (tmp_path / "2024-12-31.pdf").write_bytes(b"placeholder")
    (tmp_path / "2026-03-31.pdf").write_bytes(b"placeholder")

    # Route each file to a different mock text.
    def _mock_pdftotext(path: Path) -> str:
        if path.name == "2024-12-31.pdf":
            return _MOCK_TEXT_2024
        return _MOCK_TEXT_2026

    with patch("src.adapters.ft_pdf.pdftotext_layout", side_effect=_mock_pdftotext):
        result = import_statements(tmp_path, dry_run=False, session=session)

    assert result.imported == 2, f"unexpected: {result}"
    assert result.errors == []
    rows = (
        session.query(AccountBalanceSnapshot)
        .order_by(AccountBalanceSnapshot.as_of)
        .all()
    )
    assert len(rows) == 2
    assert rows[0].as_of == date(2024, 12, 31)
    assert rows[0].balance == Decimal("16406.38")  # pinned value from fixture
    assert rows[1].as_of == date(2026, 3, 31)
    assert rows[1].balance == Decimal("17500.00")

    # IngestionLog row written.
    log = session.query(IngestionLog).filter(IngestionLog.source == ADAPTER_NAME).one()
    assert log.records_processed >= 2


def test_import_statements_inline_mock_idempotent(
    tmp_path: Path, session: Session
) -> None:
    """FIX-K Test 2: second apply reports dup_skipped == row count."""
    _seed_ft_account(session)
    (tmp_path / "2024-12-31.pdf").write_bytes(b"placeholder")

    with patch("src.adapters.ft_pdf.pdftotext_layout", return_value=_MOCK_TEXT_2024):
        first = import_statements(tmp_path, dry_run=False, session=session)
        second = import_statements(tmp_path, dry_run=False, session=session)

    assert first.imported == 1
    assert second.imported == 0
    assert second.dup_skipped == 1
    assert session.query(AccountBalanceSnapshot).count() == 1


def test_import_statements_inline_mock_per_file_error_isolation(
    tmp_path: Path, session: Session
) -> None:
    """FIX-K Test 3: one PDF raises → that file errors, others still imported."""
    _seed_ft_account(session)
    (tmp_path / "2024-12-31.pdf").write_bytes(b"placeholder")
    (tmp_path / "2026-03-31.pdf").write_bytes(b"placeholder")

    def _mock_pdftotext(path: Path) -> str:
        if path.name == "2024-12-31.pdf":
            raise RuntimeError("simulated pdftotext failure")
        return _MOCK_TEXT_2026

    with patch("src.adapters.ft_pdf.pdftotext_layout", side_effect=_mock_pdftotext):
        result = import_statements(tmp_path, dry_run=False, session=session)

    # The failed file produces an error; the good file still imports.
    assert result.imported == 1
    assert len(result.errors) == 1
    assert "2024-12-31.pdf" in result.errors[0]
    assert session.query(AccountBalanceSnapshot).count() == 1


def test_import_statements_inline_mock_unmapped_account(
    tmp_path: Path, session: Session
) -> None:
    """FIX-K Test 4: unmapped account → all rows error, no rows written."""
    # Note: deliberately do NOT seed an FT Account row.
    (tmp_path / "2024-12-31.pdf").write_bytes(b"placeholder")

    with patch("src.adapters.ft_pdf.pdftotext_layout", return_value=_MOCK_TEXT_2024):
        result = import_statements(tmp_path, dry_run=False, session=session)

    assert result.imported == 0
    assert result.errors, "expected unmapped account errors"
    assert any(FT_ACCOUNT_NUMBER in e or FT_BROKER in e for e in result.errors)
    assert session.query(AccountBalanceSnapshot).count() == 0
