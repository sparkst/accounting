"""Tests for the GSK cash-balance pension PDF adapter (Phase 4 T5).

Single-page PDF with one ``Closing Balance as of <Date> $<balance>`` line.
Mirrors ``test_xlsx_savings_plan.py`` for in-memory SQLite + FK enforcement.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from src.adapters.gsk_pdf import (
    GSK_ACCOUNT_NUMBER,
    GSK_RAW_ACCOUNT_NAME,
    SOURCE_TAG,
    extract_closing_balance,
    import_pdf,
)
from src.models.base import Base
from src.models.brokerage import Account
from src.models.enums import AccountType, Broker, Entity
from src.models.history import AccountBalanceSnapshot
from src.models.ingestion_log import IngestionLog

# ── Fixtures ─────────────────────────────────────────────────────────────────


SAMPLE_TEXT = """\
Cash Balance Account Activity
Start Date (no earlier than Jun 1, 2017)

06-01-2017

End Date (no later than May 7, 2026)

05-07-2026



  Redisplay



GSK Cash Balance Pension Plan
You're 100% vested in the plan.

Jan 1, 2025 to Dec 31, 2025 Interest Rate: 4.25%
Jan 1, 2026 to Dec 31, 2026 Interest Rate: 4.50%



 Opening Balance as of Jun 1, 2017                   $24,207.82


 Interest Credits                                     $7,197.73


 Closing Balance as of May 7, 2026                 $31,405.55

Tools and Calculators
"""


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


def _seed_gsk_account(session: Session) -> Account:
    """Insert the canonical GSK_PENSION Account row used by the adapter."""
    acct = Account(
        broker=Broker.GSK_PENSION.value,
        account_number=GSK_ACCOUNT_NUMBER,
        account_name="GSK Cash Balance Pension Plan",
        account_type=AccountType.OTHER.value,
        entity=Entity.PERSONAL.value,
        tax_sheltered=True,
    )
    session.add(acct)
    session.commit()
    return acct


# ── extract_closing_balance ─────────────────────────────────────────────────


def test_extract_closing_balance_parses_date_and_amount() -> None:
    as_of, balance = extract_closing_balance(SAMPLE_TEXT)
    assert as_of == date(2026, 5, 7)
    assert balance == Decimal("31405.55")


def test_extract_closing_balance_raises_when_marker_missing() -> None:
    with pytest.raises(ValueError, match="Closing Balance"):
        extract_closing_balance("nothing useful in here")


# ── import_pdf ──────────────────────────────────────────────────────────────


def test_import_pdf_dry_run_reports_parsed_one(tmp_path: Path) -> None:
    pdf = tmp_path / "gsk.pdf"
    pdf.write_bytes(b"placeholder")  # contents irrelevant — pdftotext is patched

    with patch("src.adapters.gsk_pdf.pdftotext_layout", return_value=SAMPLE_TEXT):
        result = import_pdf(pdf, dry_run=True)

    assert result.parsed == 1
    assert result.would_insert == 1
    assert result.imported == 0
    assert result.errors == []


def test_import_pdf_apply_inserts_then_dedups(
    session: Session, tmp_path: Path
) -> None:
    _seed_gsk_account(session)
    pdf = tmp_path / "gsk.pdf"
    pdf.write_bytes(b"placeholder")

    with patch("src.adapters.gsk_pdf.pdftotext_layout", return_value=SAMPLE_TEXT):
        first = import_pdf(pdf, dry_run=False, session=session)

    assert first.imported == 1
    assert first.errors == []
    snaps = session.query(AccountBalanceSnapshot).all()
    assert len(snaps) == 1
    snap = snaps[0]
    assert snap.raw_account_name == GSK_RAW_ACCOUNT_NAME
    assert snap.as_of == date(2026, 5, 7)
    assert snap.balance == Decimal("31405.55")
    assert snap.source == SOURCE_TAG
    assert snap.account_id is not None

    # Second apply — dedup hits the source_row_hash lookup (or the natural-
    # key UNIQUE if that path is taken). Either way: no new row.
    with patch("src.adapters.gsk_pdf.pdftotext_layout", return_value=SAMPLE_TEXT):
        second = import_pdf(pdf, dry_run=False, session=session)

    assert second.imported == 0
    assert second.dup_skipped == 1
    assert session.query(AccountBalanceSnapshot).count() == 1

    # IngestionLog rows written for both apply runs.
    logs = session.query(IngestionLog).all()
    assert len(logs) == 2
    assert all(log.source == "gsk_pdf" for log in logs)


def test_import_pdf_unmapped_account_appends_error(
    session: Session, tmp_path: Path
) -> None:
    # Note: deliberately do NOT seed a GSK_PENSION Account row.
    pdf = tmp_path / "gsk.pdf"
    pdf.write_bytes(b"placeholder")

    with patch("src.adapters.gsk_pdf.pdftotext_layout", return_value=SAMPLE_TEXT):
        result = import_pdf(pdf, dry_run=False, session=session)

    assert result.imported == 0
    assert len(result.errors) == 1
    assert "GSK_PENSION" in result.errors[0]
    assert session.query(AccountBalanceSnapshot).count() == 0


# ── FIX-R: --as-of override ─────────────────────────────────────────────────


def test_import_pdf_as_of_override(session: Session, tmp_path: Path) -> None:
    """When as_of is supplied, the override date wins over the PDF's date."""
    _seed_gsk_account(session)
    pdf = tmp_path / "gsk.pdf"
    pdf.write_bytes(b"placeholder")

    override = date(2025, 12, 31)  # different from the SAMPLE_TEXT date (2026-05-07)

    with patch("src.adapters.gsk_pdf.pdftotext_layout", return_value=SAMPLE_TEXT):
        result = import_pdf(pdf, dry_run=False, session=session, as_of=override)

    assert result.imported == 1
    assert result.errors == []
    snap = session.query(AccountBalanceSnapshot).one()
    assert snap.as_of == override
