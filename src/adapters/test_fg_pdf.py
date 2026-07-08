"""Tests for the F&G annuity PDF adapter (Phase 4 — T3).

Two PDF flavors are supported:
  - "annual": F&G mailed annual statement (`Contract #:` + dated rows).
  - "portal": online policy-detail screen-grab (`Policy number` + undated row).

Both must auto-route via :func:`detect_template`. Successful extraction yields
one ``AccountBalanceSnapshot``; unmapped contracts yield an error and no row.
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

from src.adapters.fg_pdf import (
    ADAPTER_NAME,
    BROKER,
    SOURCE_TAG,
    ImportResult,
    detect_template,
    extract_annual_statement,
    extract_portal_screen,
    import_pdf,
)
from src.models.base import Base
from src.models.brokerage import Account
from src.models.history import AccountBalanceSnapshot
from src.models.ingestion_log import IngestionLog
from src.models.plaid import (
    PlaidItem,  # noqa: F401 — Account FKs plaid_item; register for create_all
)

# Skip live-PDF tests when pdftotext isn't installed (e.g. some CI images).
HAS_PDFTOTEXT = shutil.which("pdftotext") is not None
LIVE_ANNUAL = Path("/Users/travis/Downloads/accounts/FG/Annual Statement.pdf")
LIVE_PORTAL = Path("/Users/travis/Downloads/accounts/FG/MZ152585.pdf")


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
    """Pre-create the F&G account that the live PDFs map to."""
    acct = Account(
        broker=BROKER,
        account_number="MZ152585",
        account_type="other",
        entity="personal",
    )
    session.add(acct)
    session.commit()
    return acct


# ── Inline text fixtures (captured from real pdftotext -layout output) ───────


# Trimmed to the lines our regexes care about. Real PDFs have hundreds of
# trailing lines we don't read.
ANNUAL_TEXT_FIXTURE = """\
                      MZ152585 - MZ152585

TRAVIS SPARKS
24517 SE 43RD PLACE
SAMMAMISH, WA 98029
                                                                        ANNUAL STATEMENT OF POLICY VALUES
                                                                                        Flexible Premium Fixed Deferred Annuity

 Owner: TRAVIS SPARKS                                            Agent Name:    JAMES CARLSON

 Contract #:        MZ152585                                     Issue Date:            05/01/2025
 Product Name: FG Accumulator Plus 10                            Statement Date:        05/04/2026

                                              What is my Total Account Value?
        Total Account Value as of 05/01/2025                                                         $             609,760.84
                 Initial premium                                                                     $             609,760.84
                 Plus interest                                                                       $              50,457.71

        = Total Account Value as of 05/01/2026                                                       $             660,218.55
                                         What is my value if I surrender my contract?
        Total Account Value as of 05/01/2026                                                         $             660,218.55
        Less Early Withdrawal Penalties                                                              $              53,477.70
        Surrender value as of 05/01/2026                                                             $             606,740.85
"""


PORTAL_TEXT_FIXTURE = """\
My Policy Details
Annuity Policy Details
Policy number          MZ152585                                                  Owner(s)                                   TRAVIS SPARKS
Product                FG AccumulatorPlus 10                                     Annuitant(s)                               TRAVIS SPARKS
Policy issue date      05/01/2025                                                Vested account value                       $660,218.55
Policy issue state     Washington                                                Surrender value                            $606,740.85
Policy status          Active                                                    Tax code                                   IRA

Values displayed are current as of 05/06/2026. Please refer to your policy for an explanation of these values along with applicable terms and
conditions. For further assistance, please contact us.


  Policy values

Total account value                                               $660,218.55     Penalty free amount                                                 $66,021.85
Non-vested bonus account value                                           $0.00
Vested account value                                              $660,218.55
"""


# ── Pure-extraction tests ────────────────────────────────────────────────────


def test_extract_annual_statement_returns_contract_date_balance() -> None:
    from datetime import date
    from decimal import Decimal

    contract, as_of, balance = extract_annual_statement(ANNUAL_TEXT_FIXTURE)
    assert contract == "MZ152585"
    # Two "Total Account Value as of …" rows appear; the LAST one is the
    # current end-of-period value. 05/01/2026 is what we expect.
    assert as_of == date(2026, 5, 1)
    assert balance == Decimal("660218.55")


def test_extract_portal_screen_returns_contract_date_balance() -> None:
    from datetime import date
    from decimal import Decimal

    fallback = date(2099, 1, 1)  # must NOT be used because text has its own date
    contract, as_of, balance = extract_portal_screen(PORTAL_TEXT_FIXTURE, fallback)
    assert contract == "MZ152585"
    assert as_of == date(2026, 5, 6)
    assert balance == Decimal("660218.55")


def test_extract_portal_screen_uses_fallback_when_date_missing() -> None:
    from datetime import date
    from decimal import Decimal

    text_no_date = (
        "Policy number          MZ152585\n"
        "Total account value                                               $123.45\n"
    )
    fallback = date(2026, 5, 5)
    _, as_of, balance = extract_portal_screen(text_no_date, fallback)
    assert as_of == fallback
    assert balance == Decimal("123.45")


def test_extract_annual_statement_raises_on_missing_match() -> None:
    with pytest.raises(ValueError):
        extract_annual_statement("nothing useful here\n")


def test_extract_portal_screen_raises_on_missing_match() -> None:
    from datetime import date

    with pytest.raises(ValueError):
        extract_portal_screen("nothing useful here\n", date(2026, 1, 1))


def test_detect_template_routes_annual_and_portal() -> None:
    assert detect_template(ANNUAL_TEXT_FIXTURE) == "annual"
    assert detect_template(PORTAL_TEXT_FIXTURE) == "portal"


def test_detect_template_raises_on_unknown() -> None:
    with pytest.raises(ValueError):
        detect_template("totally unrelated PDF text\n")


# ── import_pdf integration tests (use the live PDFs) ────────────────────────


@pytest.mark.skipif(
    not HAS_PDFTOTEXT or not LIVE_ANNUAL.exists(),
    reason="needs pdftotext and live FG annual PDF",
)
def test_import_pdf_dry_run_annual_reports_parsed_one() -> None:
    result = import_pdf(LIVE_ANNUAL, dry_run=True)
    assert isinstance(result, ImportResult)
    assert result.imported == 0
    assert result.unmatched == 1  # one parsed candidate
    assert result.errors == []


@pytest.mark.skipif(
    not HAS_PDFTOTEXT or not LIVE_PORTAL.exists(),
    reason="needs pdftotext and live FG portal PDF",
)
def test_import_pdf_dry_run_portal_reports_parsed_one() -> None:
    result = import_pdf(LIVE_PORTAL, dry_run=True)
    assert result.imported == 0
    assert result.unmatched == 1
    assert result.errors == []


@pytest.mark.skipif(
    not HAS_PDFTOTEXT or not LIVE_ANNUAL.exists(),
    reason="needs pdftotext and live FG annual PDF",
)
def test_import_pdf_apply_writes_one_snapshot_and_dedups(
    session: Session,
    seeded_account: Account,
) -> None:
    from decimal import Decimal

    first = import_pdf(LIVE_ANNUAL, dry_run=False, session=session)
    assert first.imported == 1
    assert first.errors == []

    rows = session.query(AccountBalanceSnapshot).all()
    assert len(rows) == 1
    snap = rows[0]
    assert snap.account_id == seeded_account.id
    assert snap.raw_account_name == "F&G Annuity MZ152585"
    assert snap.balance == Decimal("660218.55")
    assert snap.source == SOURCE_TAG

    # Second apply must dedup, not duplicate.
    second = import_pdf(LIVE_ANNUAL, dry_run=False, session=session)
    assert second.imported == 0
    assert second.dup_skipped == 1
    assert session.query(AccountBalanceSnapshot).count() == 1


@pytest.mark.skipif(
    not HAS_PDFTOTEXT or not LIVE_ANNUAL.exists(),
    reason="needs pdftotext and live FG annual PDF",
)
def test_import_pdf_apply_writes_ingestion_log(
    session: Session, seeded_account: Account
) -> None:
    import_pdf(LIVE_ANNUAL, dry_run=False, session=session)
    logs = session.query(IngestionLog).all()
    assert len(logs) == 1
    assert logs[0].source == "fg_pdf"


@pytest.mark.skipif(
    not HAS_PDFTOTEXT or not LIVE_ANNUAL.exists(),
    reason="needs pdftotext and live FG annual PDF",
)
def test_import_pdf_unmapped_contract_appends_error(session: Session) -> None:
    """No Account row seeded → error appended, no snapshot written."""
    result = import_pdf(LIVE_ANNUAL, dry_run=False, session=session)
    assert result.imported == 0
    assert any("MZ152585" in e for e in result.errors)
    assert session.query(AccountBalanceSnapshot).count() == 0


def test_import_pdf_missing_file_appends_error(session: Session) -> None:
    """Pointing at a non-existent path errors gracefully (does not raise)."""
    bogus = Path("/tmp/this-file-definitely-does-not-exist-xyz.pdf")
    result = import_pdf(bogus, dry_run=True)
    assert result.imported == 0
    assert result.errors, "expected at least one error"


def test_import_pdf_non_pdf_file_appends_error(
    session: Session, tmp_path: Path
) -> None:
    """Garbage input → error appended (not raised)."""
    junk = tmp_path / "junk.pdf"
    junk.write_text("this is not a pdf at all")
    result = import_pdf(junk, dry_run=True)
    assert result.imported == 0
    assert result.errors, "expected at least one error"


def test_import_pdf_as_of_override_used_when_extraction_lacks_date(
    session: Session, tmp_path: Path
) -> None:
    """When --as-of is supplied, portal-flavor extraction must respect it."""
    # We exercise this through the pure helper because crafting a real PDF
    # without pdftotext support is heavy. The helper is the contract that
    # import_pdf delegates to for portal flavors.
    from datetime import date
    from decimal import Decimal

    text_no_date = (
        "Policy number          MZ999999\n"
        "Total account value                                               $42.00\n"
    )
    contract, as_of, balance = extract_portal_screen(text_no_date, date(2024, 1, 2))
    assert contract == "MZ999999"
    assert as_of == date(2024, 1, 2)
    assert balance == Decimal("42.00")


# ── Inline-mock apply tests (FIX-I, FIX-J) ──────────────────────────────────


def test_import_pdf_portal_apply_writes_snapshot(
    session: Session, tmp_path: Path
) -> None:
    """Portal flavor apply path: patches pdftotext, seeds Account, asserts snapshot."""
    # Seed the Account row the adapter will look up.
    acct = Account(
        broker=BROKER,
        account_number="MZ152585",
        account_type="other",
        entity="personal",
    )
    session.add(acct)
    session.commit()

    pdf = tmp_path / "portal.pdf"
    pdf.write_bytes(b"placeholder")

    with patch("src.adapters.fg_pdf.pdftotext_layout", return_value=PORTAL_TEXT_FIXTURE):
        result = import_pdf(pdf, dry_run=False, session=session)

    assert result.imported == 1
    assert result.errors == []
    snaps = session.query(AccountBalanceSnapshot).all()
    assert len(snaps) == 1
    snap = snaps[0]
    assert snap.account_id == acct.id
    # The portal fixture has "Values displayed are current as of 05/06/2026"
    assert snap.as_of == date(2026, 5, 6)
    assert snap.balance == Decimal("660218.55")
    assert snap.source == SOURCE_TAG

    # IngestionLog written.
    logs = session.query(IngestionLog).all()
    assert len(logs) == 1
    assert logs[0].source == ADAPTER_NAME


def test_import_pdf_portal_idempotent(session: Session, tmp_path: Path) -> None:
    """Second apply of the same portal PDF reports dup_skipped, not re-insert."""
    acct = Account(
        broker=BROKER,
        account_number="MZ152585",
        account_type="other",
        entity="personal",
    )
    session.add(acct)
    session.commit()

    pdf = tmp_path / "portal.pdf"
    pdf.write_bytes(b"placeholder")

    with patch("src.adapters.fg_pdf.pdftotext_layout", return_value=PORTAL_TEXT_FIXTURE):
        first = import_pdf(pdf, dry_run=False, session=session)
        second = import_pdf(pdf, dry_run=False, session=session)

    assert first.imported == 1
    assert second.imported == 0
    assert second.dup_skipped == 1
    assert session.query(AccountBalanceSnapshot).count() == 1


def test_import_pdf_unmapped_contract_inline(session: Session, tmp_path: Path) -> None:
    """FIX-J: unmapped contract → error appended, no row written (no real PDF)."""
    # Synthetic annual fixture text with a contract that has no Account row.
    synthetic_annual = """\
 Contract #:        MZ999999                                     Issue Date:            05/01/2025
        Total Account Value as of 05/01/2026                                                       $             100,000.00
"""
    pdf = tmp_path / "unknown.pdf"
    pdf.write_bytes(b"placeholder")

    with patch("src.adapters.fg_pdf.pdftotext_layout", return_value=synthetic_annual):
        result = import_pdf(pdf, dry_run=False, session=session)

    assert result.imported == 0
    assert any("MZ999999" in e for e in result.errors), result.errors
    assert session.query(AccountBalanceSnapshot).count() == 0


def test_import_pdf_annual_apply_writes_snapshot(
    session: Session, tmp_path: Path
) -> None:
    """FIX-11: annual flavor apply path — patches pdftotext, seeds Account, asserts snapshot."""
    # Seed the Account row the adapter will look up.
    acct = Account(
        broker=BROKER,
        account_number="MZ152585",
        account_type="other",
        entity="personal",
    )
    session.add(acct)
    session.commit()

    pdf = tmp_path / "annual.pdf"
    pdf.write_bytes(b"placeholder")

    with patch("src.adapters.fg_pdf.pdftotext_layout", return_value=ANNUAL_TEXT_FIXTURE):
        result = import_pdf(pdf, dry_run=False, session=session)

    assert result.imported == 1
    assert result.errors == []
    snaps = session.query(AccountBalanceSnapshot).all()
    assert len(snaps) == 1
    snap = snaps[0]
    assert snap.account_id == acct.id
    # Annual fixture has "Total Account Value as of 05/01/2026 $660,218.55" (last match).
    assert snap.as_of == date(2026, 5, 1)
    assert snap.balance == Decimal("660218.55")
    assert snap.source == SOURCE_TAG

    # IngestionLog written.
    logs = session.query(IngestionLog).all()
    assert len(logs) == 1
    assert logs[0].source == ADAPTER_NAME


def test_import_pdf_annual_as_of_override_wins(
    session: Session, tmp_path: Path
) -> None:
    """REQ-FIX-WLT-009: --as-of overrides the annual statement's embedded date."""
    acct = Account(
        broker=BROKER,
        account_number="MZ152585",
        account_type="other",
        entity="personal",
    )
    session.add(acct)
    session.commit()

    pdf = tmp_path / "annual.pdf"
    pdf.write_bytes(b"placeholder")

    override = date(2026, 1, 15)
    with patch("src.adapters.fg_pdf.pdftotext_layout", return_value=ANNUAL_TEXT_FIXTURE):
        result = import_pdf(pdf, dry_run=False, session=session, as_of=override)

    assert result.imported == 1
    snap = session.query(AccountBalanceSnapshot).one()
    # Override wins over the document's 05/01/2026.
    assert snap.as_of == override
    assert snap.balance == Decimal("660218.55")
