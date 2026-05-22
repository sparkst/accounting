"""Tests for tax document ingest helper.

REQ-ID: TAX-DOC-001  ingest_and_verify inserts and reads back correctly.
REQ-ID: TAX-DOC-002  Duplicate detection rejects same EIN on second ingest.
REQ-ID: TAX-DOC-003  Duplicate detection (null EIN) uses soft check via ingest_and_verify.
REQ-ID: TAX-DOC-004  validate_amounts accepts valid keys and rejects unknown/missing keys.
REQ-ID: TAX-DOC-005  list_documents filters by year and entity; excludes inactive.
REQ-ID: TAX-DOC-006  get_summary produces formatted report with entity sections and IRS lines.
REQ-ID: TAX-DOC-007  Soft delete sets status=inactive; row persists in DB.

All tests use in-memory SQLite and independent DB sessions per function.
Tests are written TDD-style against the expected API. They will fail until
src/tax_docs/ingest.py is implemented.
"""

from __future__ import annotations

import decimal
from collections.abc import Generator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.models.base import Base
from src.models.enums import Entity, TaxDocumentStatus, TaxFormType
from src.models.tax_document import TaxDocument
from src.tax_docs.ingest import (
    ChecklistItem,
    get_summary,
    ingest_and_verify,
    list_documents,
    validate_amounts,
)

# ---------------------------------------------------------------------------
# DB session fixture
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
# Sample document dicts (one per form type)
# ---------------------------------------------------------------------------

DOC_1099_NEC: dict = {
    "tax_year": 2025,
    "form_type": TaxFormType.FORM_1099_NEC.value,
    "entity": Entity.PERSONAL.value,
    "payer_name": "FC International Education LLC",
    "payer_ein": "85-1499443",
    "recipient_name": "Travis Sparks",
    "recipient_tin_last4": "5678",
    "amounts": {
        "box_1_nonemployee_comp": 1781.62,
        "box_4_federal_tax_withheld": 0,
    },
    "total_amount": 1781.62,
    "source_file": "data/tax-docs/2025/personal/1099-nec-fc-international-education.pdf",
    "notes": None,
}

DOC_1099_INT: dict = {
    "tax_year": 2025,
    "form_type": TaxFormType.FORM_1099_INT.value,
    "entity": Entity.PERSONAL.value,
    "payer_name": "Chase Bank",
    "payer_ein": "13-4994650",
    "recipient_name": "Travis Sparks",
    "recipient_tin_last4": "5678",
    "amounts": {
        "box_1_interest": 342.18,
        "box_3_savings_bond_interest": 0,
        "box_4_federal_tax_withheld": 0,
    },
    "total_amount": 342.18,
    "source_file": None,
    "notes": None,
}

DOC_1099_DIV: dict = {
    "tax_year": 2025,
    "form_type": TaxFormType.FORM_1099_DIV.value,
    "entity": Entity.PERSONAL.value,
    "payer_name": "Schwab Brokerage",
    "payer_ein": "94-1737782",
    "recipient_name": "Travis Sparks",
    "recipient_tin_last4": "5678",
    "amounts": {
        "box_1a_ordinary_dividends": 1205.00,
        "box_1b_qualified_dividends": 980.00,
        "box_2a_capital_gain": 0,
    },
    "total_amount": 1205.00,
    "source_file": None,
    "notes": None,
}

DOC_1099_B: dict = {
    "tax_year": 2025,
    "form_type": TaxFormType.FORM_1099_B.value,
    "entity": Entity.PERSONAL.value,
    "payer_name": "Schwab Brokerage",
    "payer_ein": "94-1737782",
    "recipient_name": "Travis Sparks",
    "recipient_tin_last4": "5678",
    "amounts": {
        "proceeds": 15000.00,
        "cost_basis": 12000.00,
        "gain_loss": 3000.00,
        "short_term_count": 5,
        "long_term_count": 12,
    },
    "total_amount": 3000.00,
    "source_file": None,
    "notes": None,
}

DOC_1099_K: dict = {
    "tax_year": 2025,
    "form_type": TaxFormType.FORM_1099_K.value,
    "entity": Entity.SPARKRY.value,
    "payer_name": "Stripe Inc",
    "payer_ein": "26-0484878",
    "recipient_name": "Sparkry LLC",
    "recipient_tin_last4": "1234",
    "amounts": {
        "box_1a_gross_amount": 48000.00,
        "box_1b_card_not_present": 48000.00,
    },
    "total_amount": 48000.00,
    "source_file": None,
    "notes": None,
}

DOC_K1: dict = {
    "tax_year": 2025,
    "form_type": TaxFormType.FORM_K1.value,
    "entity": Entity.SPARKRY.value,
    "payer_name": "BlackLine MTB LLC",
    "payer_ein": "92-3456789",
    "recipient_name": "Sparkry LLC",
    "recipient_tin_last4": "1234",
    "amounts": {
        "box_1_ordinary_income": 12000.00,
        "box_14_se_earnings": 12000.00,
        "box_16_foreign_transactions": 0,
    },
    "total_amount": 12000.00,
    "source_file": None,
    "notes": None,
}

DOC_1098: dict = {
    "tax_year": 2025,
    "form_type": TaxFormType.FORM_1098.value,
    "entity": Entity.PERSONAL.value,
    "payer_name": "US Bank",
    "payer_ein": "41-0369205",
    "recipient_name": "Travis Sparks",
    "recipient_tin_last4": "5678",
    "amounts": {
        "box_1_mortgage_interest": 8412.00,
        "box_2_outstanding_principal": 320000.00,
        "box_5_property_tax": 6200.00,
    },
    "total_amount": 8412.00,
    "source_file": None,
    "notes": None,
}

DOC_PROPERTY_TAX: dict = {
    "tax_year": 2025,
    "form_type": TaxFormType.PROPERTY_TAX.value,
    "entity": Entity.PERSONAL.value,
    "payer_name": "King County Assessor",
    "payer_ein": None,
    "recipient_name": "Travis Sparks",
    "recipient_tin_last4": None,
    "amounts": {
        "assessed_value": 850000.00,
        "tax_amount": 6200.00,
        "year": 2025,
    },
    "total_amount": 6200.00,
    "source_file": None,
    "notes": None,
}

# All form types in a list for parametrized tests
ALL_FORM_DOCS = [
    DOC_1099_NEC,
    DOC_1099_INT,
    DOC_1099_DIV,
    DOC_1099_B,
    DOC_1099_K,
    DOC_K1,
    DOC_1098,
    DOC_PROPERTY_TAX,
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestIngest1099Nec:
    """REQ-ID: TAX-DOC-001"""

    def test_ingest_1099_nec(self, session: Session) -> None:
        """Ingest a 1099-NEC and verify all fields are stored correctly."""
        result = ingest_and_verify(DOC_1099_NEC, session)

        assert result.success is True
        assert result.document_id is not None

        doc = session.get(TaxDocument, result.document_id)
        assert doc is not None
        assert doc.tax_year == 2025
        assert doc.form_type == TaxFormType.FORM_1099_NEC.value
        assert doc.entity == Entity.PERSONAL.value
        assert doc.payer_name == "FC International Education LLC"
        assert doc.payer_ein == "85-1499443"
        assert doc.recipient_name == "Travis Sparks"
        assert doc.recipient_tin_last4 == "5678"
        assert decimal.Decimal(str(doc.total_amount)) == decimal.Decimal("1781.62")
        assert doc.amounts["box_1_nonemployee_comp"] == 1781.62
        assert doc.amounts["box_4_federal_tax_withheld"] == 0
        assert doc.status == TaxDocumentStatus.ACTIVE.value
        assert doc.source_file is not None
        assert doc.created_at is not None
        assert doc.updated_at is not None


class TestIngestAndVerifyChecklist:
    """REQ-ID: TAX-DOC-001"""

    def test_ingest_and_verify_checklist(self, session: Session) -> None:
        """ingest_and_verify returns a passing checklist with correct items."""
        result = ingest_and_verify(DOC_1099_NEC, session)

        assert result.success is True
        assert len(result.checklist) > 0

        # All checklist items must have passed
        for item in result.checklist:
            assert isinstance(item, ChecklistItem)
            assert item.passed is True, f"Checklist item failed: {item.label}"
            assert item.label
            assert item.expected is not None
            assert item.actual is not None

        # Required checklist item labels
        labels = {item.label for item in result.checklist}
        assert "form_type" in labels
        assert "total_amount" in labels
        assert "entity" in labels
        assert "tax_year" in labels


class TestIngestAllFormTypes:
    """REQ-ID: TAX-DOC-001 — parametrized across all form types."""

    @pytest.mark.parametrize("doc", ALL_FORM_DOCS, ids=[d["form_type"] for d in ALL_FORM_DOCS])
    def test_ingest_all_form_types(self, session: Session, doc: dict) -> None:
        """Each form type ingests successfully and stores correct total_amount."""
        result = ingest_and_verify(doc, session)

        assert result.success is True
        stored = session.get(TaxDocument, result.document_id)
        assert stored is not None
        assert decimal.Decimal(str(stored.total_amount)) == decimal.Decimal(
            str(doc["total_amount"])
        )
        assert stored.form_type == doc["form_type"]


class TestDuplicateDetectionWithEin:
    """REQ-ID: TAX-DOC-002"""

    def test_duplicate_detection_with_ein(self, session: Session) -> None:
        """Inserting the same 1099-NEC (same year/form/entity/EIN) is rejected."""
        first = ingest_and_verify(DOC_1099_NEC, session)
        assert first.success is True

        # Attempt to insert the identical document again
        second = ingest_and_verify(DOC_1099_NEC, session)
        assert second.success is False


class TestDuplicateDetectionNullEin:
    """REQ-ID: TAX-DOC-003"""

    def test_duplicate_detection_null_ein(self, session: Session) -> None:
        """PROPERTY_TAX (no EIN) uses soft check on payer_name+year+entity."""
        first = ingest_and_verify(DOC_PROPERTY_TAX, session)
        assert first.success is True

        # Same payer/year/entity, no EIN — must be caught by soft check
        second = ingest_and_verify(DOC_PROPERTY_TAX, session)
        assert second.success is False


class TestAmountsValidationValid:
    """REQ-ID: TAX-DOC-004"""

    @pytest.mark.parametrize("doc", ALL_FORM_DOCS, ids=[d["form_type"] for d in ALL_FORM_DOCS])
    def test_amounts_validation_valid(self, doc: dict) -> None:
        """validate_amounts accepts correct keys for each form type."""
        # Must not raise
        validate_amounts(doc["form_type"], doc["amounts"])


class TestAmountsValidationInvalidKeys:
    """REQ-ID: TAX-DOC-004"""

    def test_amounts_validation_invalid_keys(self) -> None:
        """validate_amounts rejects unknown keys for a form type."""
        bad_amounts = {
            "box_1_nonemployee_comp": 1000.00,
            "completely_bogus_key": 99.00,  # not a valid key for 1099-NEC
        }
        with pytest.raises((ValueError, KeyError)):
            validate_amounts(TaxFormType.FORM_1099_NEC.value, bad_amounts)


class TestAmountsValidationMissingPrimary:
    """REQ-ID: TAX-DOC-004"""

    def test_amounts_validation_missing_primary(self) -> None:
        """validate_amounts rejects amounts dict missing the required primary key."""
        # 1099-NEC requires box_1_nonemployee_comp
        amounts_without_primary = {
            "box_4_federal_tax_withheld": 0,
        }
        with pytest.raises((ValueError, KeyError)):
            validate_amounts(TaxFormType.FORM_1099_NEC.value, amounts_without_primary)


class TestListDocumentsFilters:
    """REQ-ID: TAX-DOC-005"""

    def test_list_documents_filters(self, session: Session) -> None:
        """list_documents filters by year and entity correctly."""
        # Insert 2025 personal doc
        ingest_and_verify(DOC_1099_NEC, session)
        ingest_and_verify(DOC_1099_INT, session)

        # Insert a 2025 sparkry doc
        ingest_and_verify(DOC_1099_K, session)

        # Also insert a 2024 doc by modifying year
        doc_2024 = {**DOC_1098, "tax_year": 2024}
        ingest_and_verify(doc_2024, session)

        # All 2025 docs
        all_2025 = list_documents(session, year=2025)
        assert len(all_2025) == 3

        # 2025 personal only
        personal_2025 = list_documents(session, year=2025, entity=Entity.PERSONAL.value)
        assert len(personal_2025) == 2
        assert all(d.entity == Entity.PERSONAL.value for d in personal_2025)

        # 2025 sparkry only
        sparkry_2025 = list_documents(session, year=2025, entity=Entity.SPARKRY.value)
        assert len(sparkry_2025) == 1
        assert sparkry_2025[0].form_type == TaxFormType.FORM_1099_K.value

        # 2024 docs
        docs_2024 = list_documents(session, year=2024)
        assert len(docs_2024) == 1


class TestListDocumentsExcludesInactive:
    """REQ-ID: TAX-DOC-007"""

    def test_list_documents_excludes_inactive(self, session: Session) -> None:
        """list_documents returns only active documents."""
        active_result = ingest_and_verify(DOC_1099_NEC, session)
        inactive_result = ingest_and_verify(DOC_1099_INT, session)

        # Mark the second document inactive
        inactive_doc = session.get(TaxDocument, inactive_result.document_id)
        assert inactive_doc is not None
        inactive_doc.status = TaxDocumentStatus.INACTIVE.value
        session.commit()

        docs = list_documents(session, year=2025)
        assert len(docs) == 1
        assert docs[0].id == active_result.document_id


class TestSummaryReportFormat:
    """REQ-ID: TAX-DOC-006"""

    def test_summary_report_format(self, session: Session) -> None:
        """get_summary generates report with entity sections, form types, amounts, IRS lines."""
        # Insert a mix of docs for 2025
        ingest_and_verify(DOC_1099_NEC, session)
        ingest_and_verify(DOC_1099_INT, session)
        ingest_and_verify(DOC_1099_K, session)
        ingest_and_verify(DOC_PROPERTY_TAX, session)

        report = get_summary(session, year=2025)

        # Report should be a string with key sections
        assert isinstance(report, str)

        # Entity headers
        assert "Personal" in report or "personal" in report.lower()
        assert "Sparkry" in report or "sparkry" in report.lower()

        # Form types present in output
        assert "1099-NEC" in report
        assert "1099-INT" in report
        assert "1099-K" in report

        # Payer names
        assert "FC International Education LLC" in report
        assert "Chase Bank" in report

        # Amounts
        assert "1,781.62" in report

        # IRS line references
        assert "Schedule" in report or "IRS" in report or "Line" in report

        # PROPERTY_TAX / SALT reference
        assert "PROPERTY_TAX" in report or "Prop Tax" in report or "King County" in report


class TestSoftDelete:
    """REQ-ID: TAX-DOC-007"""

    def test_soft_delete(self, session: Session) -> None:
        """Setting status=inactive excludes document from list but keeps DB row."""
        result = ingest_and_verify(DOC_1099_NEC, session)
        assert result.success is True

        doc = session.get(TaxDocument, result.document_id)
        assert doc is not None

        # Soft delete
        doc.status = TaxDocumentStatus.INACTIVE.value
        session.commit()

        # Row must still exist in DB
        still_there = session.get(TaxDocument, result.document_id)
        assert still_there is not None
        assert still_there.status == TaxDocumentStatus.INACTIVE.value

        # But list_documents must exclude it
        docs = list_documents(session, year=2025)
        ids = [d.id for d in docs]
        assert result.document_id not in ids
