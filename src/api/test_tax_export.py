"""Tests for GET /api/tax-summary — home office deduction and 1099 tracking.

REQ-T-103: Tax summary for Sparkry includes home_office_deduction=180 (IRS
           simplified method: 36 sqft × $5/sqft, Form 8829 Line 30).
           Other entities report home_office_deduction=0.

REQ-1099: Tax summary includes income_1099_breakdown array grouping income
          by payer_1099 where not null, and emits a warning when total income
          exceeds the sum of 1099-tagged income.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

import src.db.connection as _conn  # noqa: F401 — registers all ORM models
from src.api.deps import get_db
from src.models.base import Base
from src.models.enums import (
    ConfirmedBy,
    Direction,
    Entity,
    Source,
    TaxCategory,
    TransactionStatus,
)
from src.models.transaction import Transaction

# ---------------------------------------------------------------------------
# In-memory test database (shared-cache so thread-pool workers see same DB)
# ---------------------------------------------------------------------------

_TEST_DB_URI = "file:tax_export_test?mode=memory&cache=shared&uri=true"

_test_engine = create_engine(
    "sqlite+pysqlite:///" + _TEST_DB_URI.replace("file:", ""),
    connect_args={"check_same_thread": False, "uri": True},
)


@event.listens_for(_test_engine, "connect")
def _set_pragmas(conn: Any, _record: Any) -> None:
    cursor = conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


Base.metadata.create_all(bind=_test_engine)

_TestSession = sessionmaker(bind=_test_engine, autocommit=False, autoflush=False)


@pytest.fixture(autouse=True)
def clean_db() -> Generator[None, None, None]:
    """Wipe all tables before each test."""
    with _test_engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
        conn.execute(text("PRAGMA foreign_keys=ON"))
    yield


def _override_get_db() -> Generator[Session, None, None]:
    """Dependency override: yield a session against the test DB."""
    session = _TestSession()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    """TestClient with all sessions redirected to the in-memory test DB."""
    from src.api import main as _main_module

    with (
        patch.object(_main_module, "init_db", return_value=None),
        patch.object(_main_module, "seed_vendor_rules", return_value=0),
        patch.object(_main_module, "seed_customers", return_value={
            "customers_inserted": 0,
            "customers_updated": 0,
            "invoices_inserted": 0,
        }),
    ):
        from src.api.main import app

        app.dependency_overrides[get_db] = _override_get_db
        with TestClient(app) as c:
            yield c
        app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# Helper: persist a confirmed transaction
# ---------------------------------------------------------------------------


def _make_tx(
    session: Session,
    *,
    entity: str = Entity.SPARKRY.value,
    tax_category: str = TaxCategory.CONSULTING_INCOME.value,
    amount: Decimal = Decimal("10000.00"),
    direction: str = Direction.INCOME.value,
    date: str = "2025-06-15",
) -> Transaction:
    tx = Transaction(
        id=str(uuid.uuid4()),
        source=Source.GMAIL_N8N.value,
        source_id=str(uuid.uuid4()),
        source_hash=str(uuid.uuid4()),
        date=date,
        description="Test transaction",
        amount=amount,
        currency="USD",
        entity=entity,
        direction=direction,
        tax_category=tax_category,
        status=TransactionStatus.CONFIRMED.value,
        confidence=0.95,
        raw_data={"test": True},
        confirmed_by=ConfirmedBy.HUMAN.value,
    )
    session.add(tx)
    session.commit()
    return tx


# ---------------------------------------------------------------------------
# Tests: home_office_deduction field in /api/tax-summary
# ---------------------------------------------------------------------------


class TestHomeOfficeDeduction:
    """REQ-T-103: Sparkry tax summary includes home_office_deduction=180."""

    def test_sparkry_home_office_deduction_is_180(self, client: TestClient) -> None:
        """Sparkry entity returns home_office_deduction=180 (36 sqft × $5)."""
        with _TestSession() as s:
            _make_tx(s, entity=Entity.SPARKRY.value)

        resp = client.get("/api/tax-summary", params={"entity": "sparkry", "year": 2025})
        assert resp.status_code == 200
        data = resp.json()
        assert "home_office_deduction" in data, "home_office_deduction field missing from response"
        assert data["home_office_deduction"] == 180

    def test_blackline_home_office_deduction_is_zero(self, client: TestClient) -> None:
        """BlackLine entity returns home_office_deduction=0 (not a home office filer)."""
        with _TestSession() as s:
            _make_tx(s, entity=Entity.BLACKLINE.value)

        resp = client.get("/api/tax-summary", params={"entity": "blackline", "year": 2025})
        assert resp.status_code == 200
        data = resp.json()
        assert data["home_office_deduction"] == 0

    def test_personal_home_office_deduction_is_zero(self, client: TestClient) -> None:
        """Personal entity returns home_office_deduction=0."""
        with _TestSession() as s:
            _make_tx(
                s,
                entity=Entity.PERSONAL.value,
                tax_category=TaxCategory.CHARITABLE_CASH.value,
                direction=Direction.EXPENSE.value,
                amount=Decimal("-500.00"),
            )

        resp = client.get("/api/tax-summary", params={"entity": "personal", "year": 2025})
        assert resp.status_code == 200
        data = resp.json()
        assert data["home_office_deduction"] == 0

    def test_home_office_included_in_total_expenses(self, client: TestClient) -> None:
        """For Sparkry, total_expenses includes the $180 home office deduction."""
        with _TestSession() as s:
            # Add a $100 supply expense
            _make_tx(
                s,
                entity=Entity.SPARKRY.value,
                tax_category=TaxCategory.SUPPLIES.value,
                direction=Direction.EXPENSE.value,
                amount=Decimal("-100.00"),
            )

        resp = client.get("/api/tax-summary", params={"entity": "sparkry", "year": 2025})
        assert resp.status_code == 200
        data = resp.json()
        # total_expenses should be $100 (supplies) + $180 (home office) = $280
        assert data["total_expenses"] == pytest.approx(280.0)

    def test_empty_sparkry_still_returns_home_office_180(self, client: TestClient) -> None:
        """home_office_deduction=180 even when there are no transactions."""
        resp = client.get("/api/tax-summary", params={"entity": "sparkry", "year": 2025})
        assert resp.status_code == 200
        data = resp.json()
        assert data["home_office_deduction"] == 180


# ---------------------------------------------------------------------------
# Helper: persist a transaction with 1099 payer fields set
# ---------------------------------------------------------------------------


def _make_tx_1099(
    session: Session,
    *,
    entity: str = Entity.SPARKRY.value,
    tax_category: str = TaxCategory.CONSULTING_INCOME.value,
    amount: Decimal = Decimal("10000.00"),
    direction: str = Direction.INCOME.value,
    date: str = "2025-06-15",
    payer_1099: str | None = None,
    payer_1099_type: str | None = None,
) -> Transaction:
    tx = Transaction(
        id=str(uuid.uuid4()),
        source=Source.GMAIL_N8N.value,
        source_id=str(uuid.uuid4()),
        source_hash=str(uuid.uuid4()),
        date=date,
        description="Test transaction",
        amount=amount,
        currency="USD",
        entity=entity,
        direction=direction,
        tax_category=tax_category,
        status=TransactionStatus.CONFIRMED.value,
        confidence=0.95,
        raw_data={"test": True},
        confirmed_by=ConfirmedBy.HUMAN.value,
        payer_1099=payer_1099,
        payer_1099_type=payer_1099_type,
    )
    session.add(tx)
    session.commit()
    return tx


# ---------------------------------------------------------------------------
# Tests: income_1099_breakdown in /api/tax-summary (REQ-1099)
# ---------------------------------------------------------------------------


class TestIncome1099Breakdown:
    """REQ-1099: income_1099_breakdown groups income by payer_1099."""

    def test_breakdown_empty_when_no_1099_payers(self, client: TestClient) -> None:
        """income_1099_breakdown is an empty list when no transactions have payer_1099 set."""
        with _TestSession() as s:
            _make_tx(s, entity=Entity.SPARKRY.value, amount=Decimal("5000.00"))

        resp = client.get("/api/tax-summary", params={"entity": "sparkry", "year": 2025})
        assert resp.status_code == 200
        data = resp.json()
        assert "income_1099_breakdown" in data
        assert data["income_1099_breakdown"] == []

    def test_single_payer_grouped_correctly(self, client: TestClient) -> None:
        """Two income transactions from the same 1099 payer sum into one entry."""
        with _TestSession() as s:
            _make_tx_1099(
                s,
                amount=Decimal("5000.00"),
                payer_1099="Cardinal Health Inc",
                payer_1099_type="NEC",
                date="2025-01-15",
            )
            _make_tx_1099(
                s,
                amount=Decimal("3000.00"),
                payer_1099="Cardinal Health Inc",
                payer_1099_type="NEC",
                date="2025-02-15",
            )

        resp = client.get("/api/tax-summary", params={"entity": "sparkry", "year": 2025})
        assert resp.status_code == 200
        data = resp.json()
        breakdown = data["income_1099_breakdown"]
        assert len(breakdown) == 1
        entry = breakdown[0]
        assert entry["payer"] == "Cardinal Health Inc"
        assert entry["type"] == "NEC"
        assert entry["total"] == pytest.approx(8000.0)

    def test_multiple_payers_sorted_descending(self, client: TestClient) -> None:
        """Multiple payers appear sorted largest-total first."""
        with _TestSession() as s:
            _make_tx_1099(
                s,
                amount=Decimal("2000.00"),
                payer_1099="Small Client LLC",
                payer_1099_type="NEC",
                date="2025-03-01",
            )
            _make_tx_1099(
                s,
                amount=Decimal("10000.00"),
                payer_1099="Big Corp Inc",
                payer_1099_type="NEC",
                date="2025-04-01",
            )

        resp = client.get("/api/tax-summary", params={"entity": "sparkry", "year": 2025})
        assert resp.status_code == 200
        data = resp.json()
        breakdown = data["income_1099_breakdown"]
        assert len(breakdown) == 2
        assert breakdown[0]["payer"] == "Big Corp Inc"
        assert breakdown[0]["total"] == pytest.approx(10000.0)
        assert breakdown[1]["payer"] == "Small Client LLC"
        assert breakdown[1]["total"] == pytest.approx(2000.0)

    def test_non_income_transactions_excluded_from_breakdown(self, client: TestClient) -> None:
        """Expense transactions with payer_1099 set are not included in the breakdown."""
        with _TestSession() as s:
            # Income with 1099 tag
            _make_tx_1099(
                s,
                amount=Decimal("5000.00"),
                payer_1099="Real Payer LLC",
                payer_1099_type="NEC",
                date="2025-05-01",
            )
            # Expense with (incorrectly) payer_1099 set — should not appear
            _make_tx_1099(
                s,
                tax_category=TaxCategory.SUPPLIES.value,
                direction=Direction.EXPENSE.value,
                amount=Decimal("-500.00"),
                payer_1099="Vendor With Field",
                payer_1099_type="MISC",
                date="2025-05-10",
            )

        resp = client.get("/api/tax-summary", params={"entity": "sparkry", "year": 2025})
        assert resp.status_code == 200
        data = resp.json()
        breakdown = data["income_1099_breakdown"]
        assert len(breakdown) == 1
        assert breakdown[0]["payer"] == "Real Payer LLC"

    def test_1099_type_none_allowed(self, client: TestClient) -> None:
        """Transactions with payer_1099 but no payer_1099_type are accepted (type=null)."""
        with _TestSession() as s:
            _make_tx_1099(
                s,
                amount=Decimal("4000.00"),
                payer_1099="Mystery Payer",
                payer_1099_type=None,
                date="2025-06-01",
            )

        resp = client.get("/api/tax-summary", params={"entity": "sparkry", "year": 2025})
        assert resp.status_code == 200
        data = resp.json()
        breakdown = data["income_1099_breakdown"]
        assert len(breakdown) == 1
        assert breakdown[0]["payer"] == "Mystery Payer"
        assert breakdown[0]["type"] is None
        assert breakdown[0]["total"] == pytest.approx(4000.0)


# ---------------------------------------------------------------------------
# Tests: undocumented income warning (REQ-1099)
# ---------------------------------------------------------------------------


class TestUndocumentedIncomeWarning:
    """REQ-1099: Warning emitted when gross income > sum of 1099-tagged income."""

    def test_warning_when_income_has_no_1099_tags(self, client: TestClient) -> None:
        """A warning is included when income exists but none is tagged to a 1099 payer."""
        with _TestSession() as s:
            _make_tx(s, amount=Decimal("10000.00"))  # no payer_1099 set

        resp = client.get("/api/tax-summary", params={"entity": "sparkry", "year": 2025})
        assert resp.status_code == 200
        data = resp.json()
        warnings = data["warnings"]
        # Should have at least one warning mentioning 1099 documentation
        doc_warnings = [w for w in warnings if "1099" in w.get("warning", "")]
        assert len(doc_warnings) == 1, f"Expected exactly one 1099 warning, got: {warnings}"
        w = doc_warnings[0]
        assert w["undocumented_amount"] == pytest.approx(10000.0)
        assert w["tagged_amount"] == pytest.approx(0.0)

    def test_warning_when_partial_income_tagged(self, client: TestClient) -> None:
        """Warning includes correct undocumented amount when only some income is tagged."""
        with _TestSession() as s:
            _make_tx_1099(
                s,
                amount=Decimal("6000.00"),
                payer_1099="Client A",
                payer_1099_type="NEC",
                date="2025-01-10",
            )
            _make_tx(  # untagged income
                s,
                amount=Decimal("4000.00"),
                date="2025-02-10",
            )

        resp = client.get("/api/tax-summary", params={"entity": "sparkry", "year": 2025})
        assert resp.status_code == 200
        data = resp.json()
        warnings = data["warnings"]
        doc_warnings = [w for w in warnings if "1099" in w.get("warning", "")]
        assert len(doc_warnings) == 1
        w = doc_warnings[0]
        assert w["undocumented_amount"] == pytest.approx(4000.0)
        assert w["tagged_amount"] == pytest.approx(6000.0)

    def test_no_warning_when_all_income_tagged(self, client: TestClient) -> None:
        """No 1099 warning when 100% of income is tagged to a payer."""
        with _TestSession() as s:
            _make_tx_1099(
                s,
                amount=Decimal("10000.00"),
                payer_1099="Cardinal Health Inc",
                payer_1099_type="NEC",
                date="2025-03-10",
            )

        resp = client.get("/api/tax-summary", params={"entity": "sparkry", "year": 2025})
        assert resp.status_code == 200
        data = resp.json()
        warnings = data["warnings"]
        doc_warnings = [w for w in warnings if "1099" in w.get("warning", "")]
        assert doc_warnings == [], f"Unexpected 1099 warning when all income is tagged: {warnings}"

    def test_no_warning_when_no_income_at_all(self, client: TestClient) -> None:
        """No 1099 warning when gross income is zero (nothing to document)."""
        with _TestSession() as s:
            _make_tx(
                s,
                tax_category=TaxCategory.SUPPLIES.value,
                direction=Direction.EXPENSE.value,
                amount=Decimal("-200.00"),
            )

        resp = client.get("/api/tax-summary", params={"entity": "sparkry", "year": 2025})
        assert resp.status_code == 200
        data = resp.json()
        warnings = data["warnings"]
        doc_warnings = [w for w in warnings if "1099" in w.get("warning", "")]
        assert doc_warnings == []
