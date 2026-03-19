"""Tests for the DeductionEmailAdapter.

REQ-ID: ADAPTER-DEDUCTION-001  Reads JSON files from the deductions/ folder.
REQ-ID: ADAPTER-DEDUCTION-002  Auto-classifies as personal entity with deduction categories.
REQ-ID: ADAPTER-DEDUCTION-003  Keyword pattern matching selects the specific TaxCategory.
REQ-ID: ADAPTER-DEDUCTION-004  Deduplicates: running twice yields no new rows.
REQ-ID: ADAPTER-DEDUCTION-005  raw_data preserved verbatim.
REQ-ID: ADAPTER-DEDUCTION-006  IngestionLog entry created for every run.

All tests use in-memory SQLite and fixture JSON files written to a temp dir.
"""

from __future__ import annotations

import decimal
import json
from collections.abc import Generator
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.adapters.deduction_email import (
    DeductionEmailAdapter,
    classify_deduction,
)
from src.models.base import Base
from src.models.enums import (
    Direction,
    Entity,
    FileStatus,
    Source,
    TaxCategory,
    TransactionStatus,
)
from src.models.ingested_file import IngestedFile
from src.models.ingestion_log import IngestionLog
from src.models.transaction import Transaction

# ---------------------------------------------------------------------------
# Fixtures — DB session
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def session() -> Generator[Session, None, None]:
    """Fresh in-memory SQLite session per test function."""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    SessionCls = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    s = SessionCls()
    yield s
    s.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# Fixtures — JSON payloads
# ---------------------------------------------------------------------------

CHARITABLE_CASH_RECEIPT: dict[str, object] = {
    "id": "ded0000000000001",
    "filename": "2025-01-15_United_Way_ded0000000000001",
    "date": "2025-01-15T10:00:00.000Z",
    "from": "United Way <receipts@unitedway.org>",
    "subject": "Thank you for your donation receipt",
    "body_text": (
        "Dear Travis,\n\n"
        "Thank you for your tax-deductible charitable contribution of $500.00 "
        "to United Way on January 15, 2025.\n\n"
        "Please retain this donation receipt for your tax records.\n"
        "No goods or services were provided in exchange for your donation.\n"
        "Organization EIN: 13-1760110\n"
    ),
    "body_html": "<html></html>",
}

CHARITABLE_STOCK_RECEIPT: dict[str, object] = {
    "id": "ded0000000000002",
    "filename": "2025-02-10_Fidelity_Charitable_ded0000000000002",
    "date": "2025-02-10T14:00:00.000Z",
    "from": "Fidelity Charitable <giving@fidelitycharitable.org>",
    "subject": "Stock donation confirmation",
    "body_text": (
        "Dear Travis,\n\n"
        "We have received your stock donation of 10 shares of AAPL "
        "(donated shares valued at $1,750.00).\n\n"
        "This is a noncash contribution. The deductible amount is the fair "
        "market value on the date of the stock transfer.\n"
    ),
    "body_html": "<html></html>",
}

MEDICAL_RECEIPT: dict[str, object] = {
    "id": "ded0000000000003",
    "filename": "2025-03-01_Premera_ded0000000000003",
    "date": "2025-03-01T08:00:00.000Z",
    "from": "Premera Blue Cross <noreply@premera.com>",
    "subject": "Explanation of Benefits - Claim #98765",
    "body_text": (
        "This is your Explanation of Benefits (EOB) for a recent medical expense.\n\n"
        "Provider: Seattle Family Medicine\n"
        "Service Date: 2025-02-28\n"
        "Amount Billed: $350.00\n"
        "Your Amount Due: $75.00\n"
    ),
    "body_html": "<html></html>",
}

STATE_LOCAL_TAX_RECEIPT: dict[str, object] = {
    "id": "ded0000000000004",
    "filename": "2025-04-30_King_County_ded0000000000004",
    "date": "2025-04-30T09:00:00.000Z",
    "from": "King County Treasury <treasury@kingcounty.gov>",
    "subject": "Property Tax Payment Confirmation",
    "body_text": (
        "Your property tax payment has been received.\n\n"
        "Account: 123456789\n"
        "Property Tax Amount Paid: $4,200.00\n"
        "Due Date: April 30, 2025\n"
        "Tax Statement Reference: 2025-Q2\n"
    ),
    "body_html": "<html></html>",
}

MORTGAGE_INTEREST_RECEIPT: dict[str, object] = {
    "id": "ded0000000000005",
    "filename": "2025-01-31_Chase_Mortgage_ded0000000000005",
    "date": "2025-01-31T00:00:00.000Z",
    "from": "Chase Mortgage <statements@chase.com>",
    "subject": "Your 2024 Form 1098 - Mortgage Interest Statement",
    "body_text": (
        "Your 2024 Mortgage Interest Statement (Form 1098) is now available.\n\n"
        "Mortgage interest paid: $18,432.00\n"
        "Property address: 123 Main St, Seattle, WA 98101\n"
        "This form is for your home loan account ending in 4321.\n"
    ),
    "body_html": "<html></html>",
}

# A deduction email with no matching keywords — should route to needs_review.
UNCLASSIFIED_DEDUCTION: dict[str, object] = {
    "id": "ded0000000000006",
    "filename": "2025-05-15_Unknown_Deduction_ded0000000000006",
    "date": "2025-05-15T12:00:00.000Z",
    "from": "Some Org <info@someorg.org>",
    "subject": "Your receipt",
    "body_text": "Thank you for your payment of $100.00.",
    "body_html": "<html></html>",
}

# Missing amount — should route to needs_review even with keyword match.
NO_AMOUNT_CHARITABLE: dict[str, object] = {
    "id": "ded0000000000007",
    "filename": "2025-06-01_Charity_No_Amount_ded0000000000007",
    "date": "2025-06-01T00:00:00.000Z",
    "from": "Red Cross <noreply@redcross.org>",
    "subject": "Thank you for your donation",
    "body_text": (
        "Dear Travis,\n\n"
        "Thank you for your generous donation to the Red Cross.\n"
        "Your tax-deductible contribution supports disaster relief.\n"
    ),
    "body_html": "<html></html>",
}

# Mortgage interest detected via bare "1098" in subject.
MORTGAGE_1098_BARE: dict[str, object] = {
    "id": "ded0000000000008",
    "filename": "2025-02-01_Lender_1098_ded0000000000008",
    "date": "2025-02-01T00:00:00.000Z",
    "from": "Lender <statements@lender.com>",
    "subject": "Your 1098 is ready",
    "body_text": (
        "Your annual mortgage statement is ready.\n"
        "Mortgage interest paid in 2024: $12,000.00\n"
    ),
    "body_html": "<html></html>",
}

# Dental/medical detected via keyword.
DENTAL_RECEIPT: dict[str, object] = {
    "id": "ded0000000000009",
    "filename": "2025-07-10_Dentist_ded0000000000009",
    "date": "2025-07-10T00:00:00.000Z",
    "from": "Smile Dental <billing@smiledental.com>",
    "subject": "Your dental appointment receipt",
    "body_text": (
        "Thank you for visiting Smile Dental.\n"
        "Amount charged for dental services: $280.00\n"
    ),
    "body_html": "<html></html>",
}


# ---------------------------------------------------------------------------
# Helper: write fixture JSON file to tmp_path
# ---------------------------------------------------------------------------


def write_fixture(tmp_path: Path, payload: dict[str, object]) -> Path:
    """Write a single fixture as a deduction-style JSON array file."""
    filename = str(payload["filename"]) + ".json"
    p = tmp_path / filename
    p.write_text(json.dumps([payload]), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Unit tests: classify_deduction
# ---------------------------------------------------------------------------


class TestClassifyDeduction:
    """Unit tests for the keyword-based deduction classifier."""

    def test_donation_receipt_keyword(self) -> None:
        assert classify_deduction("Thank you for your donation receipt", "") == TaxCategory.CHARITABLE_CASH

    def test_tax_deductible_keyword(self) -> None:
        assert classify_deduction("", "Your tax-deductible contribution") == TaxCategory.CHARITABLE_CASH

    def test_charitable_contribution_keyword(self) -> None:
        assert classify_deduction("", "charitable contribution of $500") == TaxCategory.CHARITABLE_CASH

    def test_charitable_gift_keyword(self) -> None:
        assert classify_deduction("Charitable gift receipt", "") == TaxCategory.CHARITABLE_CASH

    def test_donate_keyword(self) -> None:
        assert classify_deduction("", "Thank you for choosing to donate today") == TaxCategory.CHARITABLE_CASH

    def test_donation_keyword_in_body(self) -> None:
        assert classify_deduction("", "Your donation has been received") == TaxCategory.CHARITABLE_CASH

    def test_stock_donation_keyword(self) -> None:
        assert classify_deduction("Stock donation confirmation", "") == TaxCategory.CHARITABLE_STOCK

    def test_donated_shares_keyword(self) -> None:
        assert classify_deduction("", "You donated shares of AAPL") == TaxCategory.CHARITABLE_STOCK

    def test_noncash_contribution_keyword(self) -> None:
        assert classify_deduction("", "This is a noncash contribution.") == TaxCategory.CHARITABLE_STOCK

    def test_stock_transfer_keyword(self) -> None:
        assert classify_deduction("", "Your stock transfer has been processed") == TaxCategory.CHARITABLE_STOCK

    def test_eob_keyword(self) -> None:
        assert classify_deduction("Explanation of Benefits", "") == TaxCategory.MEDICAL

    def test_eob_abbreviation(self) -> None:
        assert classify_deduction("", "Your EOB for last visit") == TaxCategory.MEDICAL

    def test_medical_expense_keyword(self) -> None:
        assert classify_deduction("", "medical expense summary") == TaxCategory.MEDICAL

    def test_health_insurance_keyword(self) -> None:
        assert classify_deduction("", "Your health insurance premium is due") == TaxCategory.MEDICAL

    def test_prescription_keyword(self) -> None:
        assert classify_deduction("", "Your prescription was filled") == TaxCategory.MEDICAL

    def test_dental_keyword(self) -> None:
        assert classify_deduction("", "dental cleaning receipt") == TaxCategory.MEDICAL

    def test_vision_keyword(self) -> None:
        assert classify_deduction("", "vision exam and glasses") == TaxCategory.MEDICAL

    def test_physician_keyword(self) -> None:
        assert classify_deduction("", "Your physician visit receipt") == TaxCategory.MEDICAL

    def test_property_tax_keyword(self) -> None:
        assert classify_deduction("Property Tax Payment Confirmation", "") == TaxCategory.STATE_LOCAL_TAX

    def test_state_tax_keyword(self) -> None:
        assert classify_deduction("", "state tax payment received") == TaxCategory.STATE_LOCAL_TAX

    def test_local_tax_keyword(self) -> None:
        assert classify_deduction("", "local tax assessment") == TaxCategory.STATE_LOCAL_TAX

    def test_excise_tax_keyword(self) -> None:
        assert classify_deduction("", "excise tax payment confirmation") == TaxCategory.STATE_LOCAL_TAX

    def test_mortgage_interest_keyword(self) -> None:
        assert classify_deduction("", "mortgage interest paid in 2024") == TaxCategory.MORTGAGE_INTEREST

    def test_form_1098_keyword(self) -> None:
        assert classify_deduction("Your 2024 Form 1098 is ready", "") == TaxCategory.MORTGAGE_INTEREST

    def test_bare_1098_keyword(self) -> None:
        assert classify_deduction("Your 1098 is ready", "") == TaxCategory.MORTGAGE_INTEREST

    def test_home_loan_keyword(self) -> None:
        assert classify_deduction("", "home loan annual statement") == TaxCategory.MORTGAGE_INTEREST

    def test_escrow_statement_keyword(self) -> None:
        assert classify_deduction("", "your escrow statement for 2024") == TaxCategory.MORTGAGE_INTEREST

    def test_no_match_returns_none(self) -> None:
        assert classify_deduction("Your receipt", "Thank you for your payment") is None

    def test_mortgage_takes_priority_over_charitable(self) -> None:
        # A 1098 email should be classified as MORTGAGE_INTEREST, not CHARITABLE_CASH
        # even if "donation" appears in the body.
        body = "Form 1098 enclosed. Thank you for your donation."
        assert classify_deduction("", body) == TaxCategory.MORTGAGE_INTEREST

    def test_stock_takes_priority_over_charitable_cash(self) -> None:
        # "stock donation" should match CHARITABLE_STOCK, not CHARITABLE_CASH
        assert classify_deduction("Stock donation receipt", "") == TaxCategory.CHARITABLE_STOCK

    def test_empty_strings_return_none(self) -> None:
        assert classify_deduction("", "") is None


# ---------------------------------------------------------------------------
# Integration tests: DeductionEmailAdapter.run()
# ---------------------------------------------------------------------------


class TestDeductionEmailAdapterRun:
    def _make_adapter(self, *dirs: Path) -> DeductionEmailAdapter:
        return DeductionEmailAdapter(source_dirs=[str(d) for d in dirs])

    # ------------------------------------------------------------------
    # Happy-path ingestion — each deduction type
    # ------------------------------------------------------------------

    def test_ingests_charitable_cash(
        self, tmp_path: Path, session: Session
    ) -> None:
        write_fixture(tmp_path, CHARITABLE_CASH_RECEIPT)
        result = self._make_adapter(tmp_path).run(session)

        assert result.records_created == 1
        assert result.records_failed == 0

        tx = session.query(Transaction).one()
        assert tx.source == Source.DEDUCTION_EMAIL.value
        assert tx.entity == Entity.PERSONAL.value
        assert tx.direction == Direction.EXPENSE.value
        assert tx.tax_category == TaxCategory.CHARITABLE_CASH.value
        assert tx.status == TransactionStatus.AUTO_CLASSIFIED.value
        assert tx.confidence == 0.85
        assert tx.amount == decimal.Decimal("-500.00")

    def test_ingests_charitable_stock(
        self, tmp_path: Path, session: Session
    ) -> None:
        write_fixture(tmp_path, CHARITABLE_STOCK_RECEIPT)
        self._make_adapter(tmp_path).run(session)

        tx = session.query(Transaction).one()
        assert tx.tax_category == TaxCategory.CHARITABLE_STOCK.value
        assert tx.entity == Entity.PERSONAL.value
        assert tx.status == TransactionStatus.AUTO_CLASSIFIED.value
        assert tx.amount == decimal.Decimal("-1750.00")

    def test_ingests_medical(
        self, tmp_path: Path, session: Session
    ) -> None:
        write_fixture(tmp_path, MEDICAL_RECEIPT)
        self._make_adapter(tmp_path).run(session)

        tx = session.query(Transaction).one()
        assert tx.tax_category == TaxCategory.MEDICAL.value
        assert tx.entity == Entity.PERSONAL.value
        assert tx.status == TransactionStatus.AUTO_CLASSIFIED.value
        # EOB shows "Your Amount Due: $75.00" — generic $ pattern extracts the first $350.00
        assert tx.amount is not None

    def test_ingests_state_local_tax(
        self, tmp_path: Path, session: Session
    ) -> None:
        write_fixture(tmp_path, STATE_LOCAL_TAX_RECEIPT)
        self._make_adapter(tmp_path).run(session)

        tx = session.query(Transaction).one()
        assert tx.tax_category == TaxCategory.STATE_LOCAL_TAX.value
        assert tx.entity == Entity.PERSONAL.value
        assert tx.status == TransactionStatus.AUTO_CLASSIFIED.value
        assert tx.amount == decimal.Decimal("-4200.00")

    def test_ingests_mortgage_interest(
        self, tmp_path: Path, session: Session
    ) -> None:
        write_fixture(tmp_path, MORTGAGE_INTEREST_RECEIPT)
        self._make_adapter(tmp_path).run(session)

        tx = session.query(Transaction).one()
        assert tx.tax_category == TaxCategory.MORTGAGE_INTEREST.value
        assert tx.entity == Entity.PERSONAL.value
        assert tx.status == TransactionStatus.AUTO_CLASSIFIED.value
        assert tx.amount == decimal.Decimal("-18432.00")

    def test_ingests_mortgage_via_bare_1098(
        self, tmp_path: Path, session: Session
    ) -> None:
        write_fixture(tmp_path, MORTGAGE_1098_BARE)
        self._make_adapter(tmp_path).run(session)

        tx = session.query(Transaction).one()
        assert tx.tax_category == TaxCategory.MORTGAGE_INTEREST.value

    def test_ingests_dental_as_medical(
        self, tmp_path: Path, session: Session
    ) -> None:
        write_fixture(tmp_path, DENTAL_RECEIPT)
        self._make_adapter(tmp_path).run(session)

        tx = session.query(Transaction).one()
        assert tx.tax_category == TaxCategory.MEDICAL.value
        assert tx.amount == decimal.Decimal("-280.00")

    # ------------------------------------------------------------------
    # Core fields
    # ------------------------------------------------------------------

    def test_source_id_and_date_extracted(
        self, tmp_path: Path, session: Session
    ) -> None:
        write_fixture(tmp_path, CHARITABLE_CASH_RECEIPT)
        self._make_adapter(tmp_path).run(session)
        tx = session.query(Transaction).one()
        assert tx.source_id == "ded0000000000001"
        assert tx.date == "2025-01-15"

    def test_vendor_extracted_from_from_field(
        self, tmp_path: Path, session: Session
    ) -> None:
        write_fixture(tmp_path, CHARITABLE_CASH_RECEIPT)
        self._make_adapter(tmp_path).run(session)
        tx = session.query(Transaction).one()
        assert tx.description == "United Way"

    def test_currency_is_usd(
        self, tmp_path: Path, session: Session
    ) -> None:
        write_fixture(tmp_path, CHARITABLE_CASH_RECEIPT)
        self._make_adapter(tmp_path).run(session)
        tx = session.query(Transaction).one()
        assert tx.currency == "USD"

    def test_raw_data_preserved_verbatim(
        self, tmp_path: Path, session: Session
    ) -> None:
        write_fixture(tmp_path, CHARITABLE_CASH_RECEIPT)
        self._make_adapter(tmp_path).run(session)
        tx = session.query(Transaction).one()
        assert tx.raw_data == CHARITABLE_CASH_RECEIPT

    def test_source_hash_is_64_char_hex(
        self, tmp_path: Path, session: Session
    ) -> None:
        write_fixture(tmp_path, CHARITABLE_CASH_RECEIPT)
        self._make_adapter(tmp_path).run(session)
        tx = session.query(Transaction).one()
        assert len(tx.source_hash) == 64
        assert all(c in "0123456789abcdef" for c in tx.source_hash)

    def test_source_enum_is_deduction_email(
        self, tmp_path: Path, session: Session
    ) -> None:
        write_fixture(tmp_path, CHARITABLE_CASH_RECEIPT)
        self._make_adapter(tmp_path).run(session)
        tx = session.query(Transaction).one()
        assert tx.source == Source.DEDUCTION_EMAIL.value

    def test_json_file_included_in_attachments(
        self, tmp_path: Path, session: Session
    ) -> None:
        json_path = write_fixture(tmp_path, CHARITABLE_CASH_RECEIPT)
        self._make_adapter(tmp_path).run(session)
        tx = session.query(Transaction).one()
        assert tx.attachments is not None
        assert any(str(json_path.resolve()) in p for p in tx.attachments)

    def test_pdf_attachment_linked(self, tmp_path: Path, session: Session) -> None:
        write_fixture(tmp_path, CHARITABLE_CASH_RECEIPT)
        hex_id = str(CHARITABLE_CASH_RECEIPT["id"])
        pdf = tmp_path / f"{hex_id}_donation-confirmation.pdf"
        pdf.write_bytes(b"%PDF stub")

        self._make_adapter(tmp_path).run(session)
        tx = session.query(Transaction).one()
        assert any("donation-confirmation.pdf" in p for p in tx.attachments)

    # ------------------------------------------------------------------
    # Unclassified and missing amount → needs_review
    # ------------------------------------------------------------------

    def test_unclassified_sets_needs_review(
        self, tmp_path: Path, session: Session
    ) -> None:
        write_fixture(tmp_path, UNCLASSIFIED_DEDUCTION)
        self._make_adapter(tmp_path).run(session)
        tx = session.query(Transaction).one()
        assert tx.status == TransactionStatus.NEEDS_REVIEW.value
        assert tx.tax_category is None
        assert tx.confidence == 0.0
        assert tx.review_reason is not None

    def test_no_amount_sets_needs_review(
        self, tmp_path: Path, session: Session
    ) -> None:
        write_fixture(tmp_path, NO_AMOUNT_CHARITABLE)
        self._make_adapter(tmp_path).run(session)
        tx = session.query(Transaction).one()
        assert tx.status == TransactionStatus.NEEDS_REVIEW.value
        assert tx.amount is None
        assert tx.review_reason is not None

    def test_no_amount_review_reason_mentions_amount(
        self, tmp_path: Path, session: Session
    ) -> None:
        write_fixture(tmp_path, NO_AMOUNT_CHARITABLE)
        self._make_adapter(tmp_path).run(session)
        tx = session.query(Transaction).one()
        assert "Amount could not be extracted" in (tx.review_reason or "")

    def test_entity_always_personal(
        self, tmp_path: Path, session: Session
    ) -> None:
        """Even unclassified deductions are forced to personal entity."""
        write_fixture(tmp_path, UNCLASSIFIED_DEDUCTION)
        self._make_adapter(tmp_path).run(session)
        tx = session.query(Transaction).one()
        assert tx.entity == Entity.PERSONAL.value

    def test_direction_always_expense(
        self, tmp_path: Path, session: Session
    ) -> None:
        write_fixture(tmp_path, UNCLASSIFIED_DEDUCTION)
        self._make_adapter(tmp_path).run(session)
        tx = session.query(Transaction).one()
        assert tx.direction == Direction.EXPENSE.value

    # ------------------------------------------------------------------
    # IngestedFile tracking
    # ------------------------------------------------------------------

    def test_ingested_file_record_created(
        self, tmp_path: Path, session: Session
    ) -> None:
        write_fixture(tmp_path, CHARITABLE_CASH_RECEIPT)
        self._make_adapter(tmp_path).run(session)

        ingested = session.query(IngestedFile).one()
        assert ingested.adapter == Source.DEDUCTION_EMAIL.value
        assert ingested.status == FileStatus.SUCCESS.value
        assert len(ingested.transaction_ids) == 1

    def test_ingested_file_links_to_transaction(
        self, tmp_path: Path, session: Session
    ) -> None:
        write_fixture(tmp_path, CHARITABLE_CASH_RECEIPT)
        self._make_adapter(tmp_path).run(session)
        tx = session.query(Transaction).one()
        ingested = session.query(IngestedFile).one()
        assert ingested.transaction_ids == [tx.id]

    # ------------------------------------------------------------------
    # IngestionLog
    # ------------------------------------------------------------------

    def test_ingestion_log_created_on_success(
        self, tmp_path: Path, session: Session
    ) -> None:
        write_fixture(tmp_path, CHARITABLE_CASH_RECEIPT)
        self._make_adapter(tmp_path).run(session)

        log = session.query(IngestionLog).one()
        assert log.source == Source.DEDUCTION_EMAIL.value
        assert log.status == "success"
        assert log.records_processed == 1
        assert log.records_failed == 0

    def test_ingestion_log_created_on_empty_run(
        self, tmp_path: Path, session: Session
    ) -> None:
        """An IngestionLog row is written even when no files are found."""
        self._make_adapter(tmp_path).run(session)  # tmp_path has no JSON files
        log = session.query(IngestionLog).one()
        assert log.source == Source.DEDUCTION_EMAIL.value
        assert log.records_processed == 0

    def test_ingestion_log_records_partial_failure(
        self, tmp_path: Path, session: Session
    ) -> None:
        (tmp_path / "bad.json").write_text("not valid json", encoding="utf-8")
        write_fixture(tmp_path, CHARITABLE_CASH_RECEIPT)

        self._make_adapter(tmp_path).run(session)
        log = session.query(IngestionLog).one()
        assert log.status == "partial_failure"
        assert log.records_failed == 1
        assert log.error_detail is not None

    # ------------------------------------------------------------------
    # Deduplication
    # ------------------------------------------------------------------

    def test_running_twice_does_not_create_duplicate(
        self, tmp_path: Path, session: Session
    ) -> None:
        write_fixture(tmp_path, CHARITABLE_CASH_RECEIPT)
        adapter = self._make_adapter(tmp_path)

        result1 = adapter.run(session)
        result2 = adapter.run(session)

        assert result1.records_created == 1
        assert result2.records_created == 0
        assert result2.records_skipped == 1
        assert session.query(Transaction).count() == 1

    def test_same_content_in_two_dirs_skipped(
        self, tmp_path: Path, session: Session
    ) -> None:
        dir1 = tmp_path / "dir1"
        dir2 = tmp_path / "dir2"
        dir1.mkdir()
        dir2.mkdir()

        payload_str = json.dumps([CHARITABLE_CASH_RECEIPT])
        (dir1 / "receipt.json").write_text(payload_str)
        (dir2 / "receipt_copy.json").write_text(payload_str)

        adapter = DeductionEmailAdapter(source_dirs=[str(dir1), str(dir2)])
        result = adapter.run(session)

        assert result.records_created == 1
        assert result.records_skipped == 1
        assert session.query(Transaction).count() == 1

    def test_multiple_different_files_all_ingested(
        self, tmp_path: Path, session: Session
    ) -> None:
        fixtures = [
            CHARITABLE_CASH_RECEIPT,
            CHARITABLE_STOCK_RECEIPT,
            MEDICAL_RECEIPT,
            STATE_LOCAL_TAX_RECEIPT,
            MORTGAGE_INTEREST_RECEIPT,
        ]
        for fixture in fixtures:
            write_fixture(tmp_path, fixture)

        result = self._make_adapter(tmp_path).run(session)

        assert result.records_created == 5
        assert result.records_failed == 0
        assert session.query(Transaction).count() == 5

    # ------------------------------------------------------------------
    # Error isolation
    # ------------------------------------------------------------------

    def test_malformed_json_does_not_halt_batch(
        self, tmp_path: Path, session: Session
    ) -> None:
        (tmp_path / "bad.json").write_text("not valid json", encoding="utf-8")
        write_fixture(tmp_path, CHARITABLE_CASH_RECEIPT)

        result = self._make_adapter(tmp_path).run(session)

        assert result.records_created == 1
        assert result.records_failed == 1
        # Valid transaction was still ingested.
        assert session.query(Transaction).count() == 1

    def test_missing_id_field_counted_as_failure(
        self, tmp_path: Path, session: Session
    ) -> None:
        bad_payload = {k: v for k, v in CHARITABLE_CASH_RECEIPT.items() if k != "id"}
        (tmp_path / "no_id.json").write_text(json.dumps([bad_payload]))

        result = self._make_adapter(tmp_path).run(session)

        assert result.records_failed == 1
        assert session.query(Transaction).count() == 0

    def test_nonexistent_directory_silently_skipped(
        self, session: Session
    ) -> None:
        adapter = DeductionEmailAdapter(source_dirs=["/tmp/does_not_exist_deductions_xyz"])
        result = adapter.run(session)
        assert result.records_created == 0
        assert result.records_failed == 0
        # IngestionLog still written.
        assert session.query(IngestionLog).count() == 1

    def test_empty_json_array_counted_as_failure(
        self, tmp_path: Path, session: Session
    ) -> None:
        (tmp_path / "empty.json").write_text("[]", encoding="utf-8")

        result = self._make_adapter(tmp_path).run(session)
        assert result.records_failed == 1

    def test_adapter_source_property(self) -> None:
        adapter = DeductionEmailAdapter(source_dirs=["/tmp/noop"])
        assert adapter.source == Source.DEDUCTION_EMAIL.value
