"""Tests for ORM models and enums.

REQ-ID: DB-001 through DB-006 (data model fields match design spec)
"""

import decimal
from collections.abc import Generator
from datetime import datetime

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session, sessionmaker

from src.models.audit_event import AuditEvent
from src.models.base import Base
from src.models.enums import (
    ConfirmedBy,
    Direction,
    Entity,
    FileStatus,
    IngestionStatus,
    Source,
    TaxCategory,
    TransactionStatus,
    VendorRuleSource,
)
from src.models.ingested_file import IngestedFile
from src.models.ingestion_log import IngestionLog
from src.models.transaction import Transaction
from src.models.vendor_rule import VendorRule

_TEST_ENGINE = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
Base.metadata.create_all(_TEST_ENGINE)
_SessionCls = sessionmaker(bind=_TEST_ENGINE)


@pytest.fixture(scope="module")
def session() -> Generator[Session, None, None]:
    """In-memory SQLite session for model tests."""
    s = _SessionCls()
    yield s
    s.close()


# ── Enum completeness ─────────────────────────────────────────────────────────

class TestEntityEnum:
    def test_all_values(self) -> None:
        assert {e.value for e in Entity} == {"sparkry", "blackline", "personal"}

    def test_str_subclass(self) -> None:
        # StrEnum compares equal to its own value — verify identity via .value
        assert Entity.SPARKRY.value == "sparkry"


class TestDirectionEnum:
    def test_all_values(self) -> None:
        assert {d.value for d in Direction} == {
            "income", "expense", "transfer", "reimbursable"
        }


class TestTaxCategoryEnum:
    BUSINESS = {
        "ADVERTISING", "CAR_AND_TRUCK", "CONTRACT_LABOR", "INSURANCE",
        "LEGAL_AND_PROFESSIONAL", "OFFICE_EXPENSE", "SUPPLIES",
        "TAXES_AND_LICENSES", "TRAVEL", "MEALS", "COGS",
        "CONSULTING_INCOME", "SUBSCRIPTION_INCOME", "SALES_INCOME",
        "REIMBURSABLE",
    }
    PERSONAL = {
        "CHARITABLE_CASH", "CHARITABLE_STOCK", "MEDICAL", "STATE_LOCAL_TAX",
        "MORTGAGE_INTEREST", "INVESTMENT_INCOME", "PERSONAL_NON_DEDUCTIBLE",
    }
    EQUITY_OTHER = {
        "CAPITAL_CONTRIBUTION", "OTHER_EXPENSE",
    }

    def test_business_categories_present(self) -> None:
        values = {c.value for c in TaxCategory}
        assert self.BUSINESS.issubset(values)

    def test_personal_categories_present(self) -> None:
        values = {c.value for c in TaxCategory}
        assert self.PERSONAL.issubset(values)

    def test_equity_other_categories_present(self) -> None:
        values = {c.value for c in TaxCategory}
        assert self.EQUITY_OTHER.issubset(values)

    def test_total_count(self) -> None:
        assert len(TaxCategory) == len(self.BUSINESS) + len(self.PERSONAL) + len(self.EQUITY_OTHER)


class TestTransactionStatusEnum:
    def test_all_values(self) -> None:
        assert {s.value for s in TransactionStatus} == {
            "auto_classified", "needs_review", "confirmed",
            "split_parent", "rejected",
        }


class TestSourceEnum:
    def test_all_adapters_present(self) -> None:
        assert {s.value for s in Source} == {
            "gmail_n8n", "stripe", "shopify",
            "brokerage_csv", "bank_csv",
            "photo_receipt", "deduction_email",
            "woocommerce_csv",
        }


class TestVendorRuleSourceEnum:
    def test_values(self) -> None:
        assert {v.value for v in VendorRuleSource} == {"human", "learned"}


class TestIngestionStatusEnum:
    def test_values(self) -> None:
        assert {s.value for s in IngestionStatus} == {
            "success", "partial_failure", "failure"
        }


class TestFileStatusEnum:
    def test_values(self) -> None:
        assert {s.value for s in FileStatus} == {"success", "failed", "skipped"}


class TestConfirmedByEnum:
    def test_values(self) -> None:
        assert {c.value for c in ConfirmedBy} == {"auto", "human"}


# ── Transaction model ─────────────────────────────────────────────────────────

class TestTransactionModel:
    def _make(self) -> Transaction:
        return Transaction(
            source=Source.STRIPE.value,
            source_id="ch_abc123",
            source_hash="a" * 64,
            date="2025-01-15",
            description="Anthropic API",
            amount=decimal.Decimal("-16.90"),
            entity=Entity.SPARKRY.value,
            direction=Direction.EXPENSE.value,
            tax_category=TaxCategory.SUPPLIES.value,
            status=TransactionStatus.AUTO_CLASSIFIED.value,
            confidence=0.95,
            raw_data={"charge_id": "ch_abc123", "amount": 1690},
        )

    def test_persist_and_retrieve(self, session: Session) -> None:
        tx = self._make()
        session.add(tx)
        session.commit()
        fetched = session.get(Transaction, tx.id)
        assert fetched is not None
        assert fetched.description == "Anthropic API"
        assert fetched.entity == "sparkry"

    def test_amount_is_decimal(self, session: Session) -> None:
        tx = self._make()
        tx.source_hash = "b" * 64
        session.add(tx)
        session.commit()
        fetched = session.get(Transaction, tx.id)
        assert fetched is not None
        assert isinstance(fetched.amount, decimal.Decimal)
        assert fetched.amount == decimal.Decimal("-16.90")

    def test_defaults(self, session: Session) -> None:
        tx = Transaction(
            source=Source.BANK_CSV.value,
            source_hash="c" * 64,
            date="2025-02-01",
            description="Unknown Vendor",
            amount=decimal.Decimal("-50.00"),
            raw_data={},
        )
        session.add(tx)
        session.commit()
        fetched = session.get(Transaction, tx.id)
        assert fetched is not None
        assert fetched.currency == "USD"
        assert fetched.status == TransactionStatus.NEEDS_REVIEW.value
        assert fetched.confirmed_by == ConfirmedBy.AUTO.value
        assert fetched.deductible_pct == 1.0
        assert fetched.confidence == 0.0

    def test_enum_properties(self, session: Session) -> None:
        tx = self._make()
        tx.source_hash = "d" * 64
        session.add(tx)
        session.commit()
        fetched = session.get(Transaction, tx.id)
        assert fetched is not None
        assert fetched.entity_enum == Entity.SPARKRY
        assert fetched.direction_enum == Direction.EXPENSE
        assert fetched.tax_category_enum == TaxCategory.SUPPLIES
        assert fetched.status_enum == TransactionStatus.AUTO_CLASSIFIED
        assert fetched.source_enum == Source.STRIPE

    def test_split_child_references_parent(self, session: Session) -> None:
        parent = Transaction(
            source=Source.STRIPE.value,
            source_hash="e" * 64,
            date="2025-03-01",
            description="Hotel Stay",
            amount=decimal.Decimal("-300.00"),
            status=TransactionStatus.SPLIT_PARENT.value,
            raw_data={},
        )
        session.add(parent)
        session.flush()

        child = Transaction(
            source=Source.STRIPE.value,
            source_hash="f" * 64,
            date="2025-03-01",
            description="Hotel Room",
            amount=decimal.Decimal("-250.00"),
            parent_id=parent.id,
            raw_data={},
        )
        session.add(child)
        session.commit()
        fetched_child = session.get(Transaction, child.id)
        assert fetched_child is not None
        assert fetched_child.parent_id == parent.id

    def test_repr(self) -> None:
        tx = self._make()
        r = repr(tx)
        assert "Transaction" in r
        assert "sparkry" in r


# ── VendorRule model ──────────────────────────────────────────────────────────

class TestVendorRuleModel:
    def _make(self, suffix: str = "") -> VendorRule:
        return VendorRule(
            vendor_pattern=f"Anthropic{suffix}",
            entity=Entity.SPARKRY.value,
            tax_category=TaxCategory.SUPPLIES.value,
            direction=Direction.EXPENSE.value,
        )

    def test_persist_and_retrieve(self, session: Session) -> None:
        rule = self._make()
        session.add(rule)
        session.commit()
        fetched = session.get(VendorRule, rule.id)
        assert fetched is not None
        assert fetched.vendor_pattern == "Anthropic"
        assert fetched.confidence == 1.0
        assert fetched.examples == 1
        assert fetched.source == VendorRuleSource.HUMAN.value

    def test_enum_properties(self, session: Session) -> None:
        rule = self._make(" Inc")
        session.add(rule)
        session.commit()
        fetched = session.get(VendorRule, rule.id)
        assert fetched is not None
        assert fetched.entity_enum == Entity.SPARKRY
        assert fetched.tax_category_enum == TaxCategory.SUPPLIES
        assert fetched.direction_enum == Direction.EXPENSE

    def test_repr(self) -> None:
        rule = self._make()
        assert "VendorRule" in repr(rule)


# ── IngestedFile model ────────────────────────────────────────────────────────

class TestIngestedFileModel:
    def test_persist_and_retrieve(self, session: Session) -> None:
        f = IngestedFile(
            file_path="/data/drop/bank_jan.csv",
            file_hash="g" * 64,
            adapter="bank_csv",
            transaction_ids=["uuid-1", "uuid-2"],
        )
        session.add(f)
        session.commit()
        fetched = session.get(IngestedFile, f.id)
        assert fetched is not None
        assert fetched.adapter == "bank_csv"
        assert fetched.status == FileStatus.SUCCESS.value
        assert fetched.transaction_ids == ["uuid-1", "uuid-2"]

    def test_repr(self, session: Session) -> None:
        f = IngestedFile(
            file_path="/tmp/x.csv",
            file_hash="h" * 64,
            adapter="bank_csv",
            transaction_ids=[],
        )
        assert "IngestedFile" in repr(f)


# ── AuditEvent model ──────────────────────────────────────────────────────────

class TestAuditEventModel:
    def test_persist_and_retrieve(self, session: Session) -> None:
        # Need a transaction to reference.
        tx = Transaction(
            source=Source.STRIPE.value,
            source_hash="i" * 64,
            date="2025-04-01",
            description="Test TX for audit",
            amount=decimal.Decimal("-10.00"),
            raw_data={},
        )
        session.add(tx)
        session.flush()

        event = AuditEvent(
            transaction_id=tx.id,
            field_changed="entity",
            old_value=None,
            new_value="sparkry",
            changed_by="human",
        )
        session.add(event)
        session.commit()

        fetched = session.get(AuditEvent, event.id)
        assert fetched is not None
        assert fetched.field_changed == "entity"
        assert fetched.new_value == "sparkry"
        assert fetched.old_value is None
        assert isinstance(fetched.changed_at, datetime)

    def test_repr(self) -> None:
        ev = AuditEvent(
            transaction_id="x" * 36,
            field_changed="status",
            old_value="needs_review",
            new_value="confirmed",
            changed_by="human",
        )
        assert "AuditEvent" in repr(ev)


# ── IngestionLog model ────────────────────────────────────────────────────────

class TestIngestionLogModel:
    def test_persist_and_retrieve(self, session: Session) -> None:
        log = IngestionLog(
            source=Source.STRIPE.value,
            status=IngestionStatus.SUCCESS.value,
            records_processed=42,
            records_failed=0,
        )
        session.add(log)
        session.commit()
        fetched = session.get(IngestionLog, log.id)
        assert fetched is not None
        assert fetched.records_processed == 42
        assert fetched.retryable is False
        assert fetched.retried_at is None
        assert fetched.resolved_at is None

    def test_failure_fields(self, session: Session) -> None:
        log = IngestionLog(
            source=Source.SHOPIFY.value,
            status=IngestionStatus.FAILURE.value,
            records_processed=0,
            records_failed=1,
            error_detail="ConnectionError: timed out",
            retryable=True,
        )
        session.add(log)
        session.commit()
        fetched = session.get(IngestionLog, log.id)
        assert fetched is not None
        assert fetched.retryable is True
        assert "ConnectionError" in (fetched.error_detail or "")

    def test_status_enum_property(self, session: Session) -> None:
        log = IngestionLog(
            source=Source.GMAIL_N8N.value,
            status=IngestionStatus.PARTIAL_FAILURE.value,
            records_processed=10,
            records_failed=2,
        )
        session.add(log)
        session.commit()
        fetched = session.get(IngestionLog, log.id)
        assert fetched is not None
        assert fetched.status_enum == IngestionStatus.PARTIAL_FAILURE


# ── Schema introspection ──────────────────────────────────────────────────────

class TestSchemaIntrospection:
    """Verify all 5 expected tables are created by the Base metadata."""

    def test_all_tables_present(self, session: Session) -> None:
        insp = inspect(_TEST_ENGINE)
        tables = set(insp.get_table_names())
        expected = {
            "transactions",
            "vendor_rules",
            "ingested_files",
            "audit_events",
            "ingestion_log",
        }
        assert expected.issubset(tables), f"Missing tables: {expected - tables}"

    def test_transactions_columns(self, session: Session) -> None:
        insp = inspect(_TEST_ENGINE)
        cols = {c["name"] for c in insp.get_columns("transactions")}
        required = {
            "id", "source", "source_id", "source_hash", "date", "description",
            "amount", "currency", "entity", "direction", "tax_category",
            "tax_subcategory", "deductible_pct", "status", "confidence",
            "review_reason", "parent_id", "reimbursement_link",
            "attachments", "raw_data", "created_at", "updated_at",
            "confirmed_by", "notes",
        }
        assert required.issubset(cols), f"Missing columns: {required - cols}"

    def test_vendor_rules_columns(self, session: Session) -> None:
        insp = inspect(_TEST_ENGINE)
        cols = {c["name"] for c in insp.get_columns("vendor_rules")}
        required = {
            "id", "vendor_pattern", "entity", "tax_category", "tax_subcategory",
            "direction", "deductible_pct", "confidence", "source", "examples",
            "last_matched", "created_at",
        }
        assert required.issubset(cols), f"Missing columns: {required - cols}"
