"""Tests for GET /api/tax-summary — home office deduction and 1099 tracking.

REQ-T-103: Tax summary for Sparkry includes home_office_deduction=180 (IRS
           simplified method: 36 sqft × $5/sqft, Form 8829 Line 30).
           Other entities report home_office_deduction=0.

REQ-1099: Tax summary includes income_1099_breakdown array grouping income
          by payer_1099 where not null, and emits a warning when total income
          exceeds the sum of 1099-tagged income.
"""

from __future__ import annotations

import csv
import io
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
    "sqlite+pysqlite:///" + _TEST_DB_URI,
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


# ---------------------------------------------------------------------------
# Tests: GET /api/export/bno?format=dor hard-fails on unmapped WA locality
# (REQ-FIX-TAX-007)
# ---------------------------------------------------------------------------


class TestDorUploadUnmappedLocalityHardFail:
    """REQ-FIX-TAX-007: an unmapped WA locality is a 422, not a 500 or a
    silently-emitted '____' sentinel line."""

    def _make_locality_tx(self, session: Session, *, city: str) -> Transaction:
        tx = Transaction(
            id=str(uuid.uuid4()),
            source=Source.SHOPIFY.value,
            source_id=str(uuid.uuid4()),
            source_hash=str(uuid.uuid4()),
            date="2026-01-15",
            description=f"Shopify Order — {city}",
            amount=Decimal("100.00"),
            currency="USD",
            entity=Entity.BLACKLINE.value,
            direction=Direction.INCOME.value,
            tax_category=TaxCategory.SALES_INCOME.value,
            status=TransactionStatus.CONFIRMED.value,
            confidence=0.95,
            raw_data={
                "total_price": "100.00",
                "total_tax": "9.30",
                "shipping_address": {"province_code": "WA", "city": city},
                "tax_lines": [
                    {"title": "Washington State Tax"},
                    {"title": f"{city} City Tax"},
                ],
            },
            confirmed_by=ConfirmedBy.HUMAN.value,
        )
        session.add(tx)
        session.commit()
        return tx

    def test_dor_upload_returns_422(self, client: TestClient) -> None:
        with _TestSession() as s:
            self._make_locality_tx(s, city="Nowhereville")

        resp = client.get(
            "/api/export/bno",
            params={"entity": "blackline", "year": 2026, "format": "dor", "quarter": 1},
        )
        assert resp.status_code == 422
        assert "____" in resp.json()["detail"]

    def test_mapped_locality_still_succeeds(self, client: TestClient) -> None:
        with _TestSession() as s:
            self._make_locality_tx(s, city="Sammamish")

        resp = client.get(
            "/api/export/bno",
            params={"entity": "blackline", "year": 2026, "format": "dor", "quarter": 1},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Tests: GET /api/tax-summary/monthly reports SALES_INCOME on the pre-tax
# basis (REQ-FIX-TAX-002)
# ---------------------------------------------------------------------------


class TestMonthlyBreakdownExcludesCollectedSalesTax:
    """REQ-FIX-TAX-002: collected WA sales tax must be excluded from gross
    receipts 'everywhere, not just the DOR upload' — including the monthly
    breakdown that drives the B&O wizard's period-scoped readiness warning."""

    def _make_sales_tx(self, session: Session) -> Transaction:
        tx = Transaction(
            id=str(uuid.uuid4()),
            source=Source.SHOPIFY.value,
            source_id=str(uuid.uuid4()),
            source_hash=str(uuid.uuid4()),
            date="2026-01-15",
            description="Shopify Order — WA retail",
            amount=Decimal("109.30"),
            currency="USD",
            entity=Entity.BLACKLINE.value,
            direction=Direction.INCOME.value,
            tax_category=TaxCategory.SALES_INCOME.value,
            status=TransactionStatus.CONFIRMED.value,
            confidence=0.95,
            raw_data={
                "total_price": "109.30",
                "total_tax": "9.30",
                "shipping_address": {"province_code": "WA"},
                "tax_lines": [{"title": "Washington State Tax"}],
            },
            confirmed_by=ConfirmedBy.HUMAN.value,
        )
        session.add(tx)
        session.commit()
        return tx

    def test_monthly_sales_income_total_is_pretax(self, client: TestClient) -> None:
        with _TestSession() as s:
            self._make_sales_tx(s)

        resp = client.get(
            "/api/tax-summary/monthly",
            params={"entity": "blackline", "year": 2026},
        )
        assert resp.status_code == 200
        body = resp.json()

        jan = next(m for m in body["months"] if m["month"] == "2026-01")
        sales = next(
            c for c in jan["categories"] if c["tax_category"] == "SALES_INCOME"
        )
        # $109.30 total_price - $9.30 total_tax = $100.00 pre-tax — NOT the
        # tax-inclusive $109.30 stored amount.
        assert sales["total"] == 100.00

    def test_monthly_total_matches_tax_summary_pretax_figure(
        self, client: TestClient
    ) -> None:
        """The monthly breakdown and /api/tax-summary must agree on the same
        pre-tax SALES_INCOME figure — both derive from pretax_abs_amount."""
        with _TestSession() as s:
            self._make_sales_tx(s)

        monthly_resp = client.get(
            "/api/tax-summary/monthly",
            params={"entity": "blackline", "year": 2026},
        )
        summary_resp = client.get(
            "/api/tax-summary",
            params={"entity": "blackline", "year": 2026},
        )
        assert monthly_resp.status_code == 200
        assert summary_resp.status_code == 200

        jan = next(
            m for m in monthly_resp.json()["months"] if m["month"] == "2026-01"
        )
        monthly_sales = next(
            c for c in jan["categories"] if c["tax_category"] == "SALES_INCOME"
        )
        summary_sales = next(
            li
            for li in summary_resp.json()["line_items"]
            if li["tax_category"] == "SALES_INCOME"
        )
        assert monthly_sales["total"] == summary_sales["total"] == 100.00

    def _make_oos_sales_tx(self, session: Session) -> Transaction:
        tx = Transaction(
            id=str(uuid.uuid4()),
            source=Source.SHOPIFY.value,
            source_id=str(uuid.uuid4()),
            source_hash=str(uuid.uuid4()),
            date="2026-01-20",
            description="Shopify Order — shipped to Portland OR",
            amount=Decimal("250.00"),
            currency="USD",
            entity=Entity.BLACKLINE.value,
            direction=Direction.INCOME.value,
            tax_category=TaxCategory.SALES_INCOME.value,
            status=TransactionStatus.CONFIRMED.value,
            confidence=0.95,
            raw_data={
                "total_price": "250.00",
                "total_tax": "0.00",
                "shipping_address": {"province_code": "OR"},
                "tax_lines": [],
            },
            confirmed_by=ConfirmedBy.HUMAN.value,
        )
        session.add(tx)
        session.commit()
        return tx

    def test_monthly_cells_sum_to_annual_with_confirmed_oos_order(
        self, client: TestClient
    ) -> None:
        """P2-201 regression: the monthly drill-down uses the INCOME-TAX basis
        (pre-tax, INCLUDING out-of-state sales) — the sum of monthly
        SALES_INCOME cells must equal the annual line_items cell beside them
        on the Financials page. OOS deduction is a B&O concept and belongs to
        compute_retail_detail, never to this endpoint."""
        with _TestSession() as s:
            self._make_sales_tx(s)
            self._make_oos_sales_tx(s)

        monthly_resp = client.get(
            "/api/tax-summary/monthly",
            params={"entity": "blackline", "year": 2026},
        )
        summary_resp = client.get(
            "/api/tax-summary",
            params={"entity": "blackline", "year": 2026},
        )
        assert monthly_resp.status_code == 200
        assert summary_resp.status_code == 200

        monthly_total = sum(
            c["total"]
            for m in monthly_resp.json()["months"]
            for c in m["categories"]
            if c["tax_category"] == "SALES_INCOME"
        )
        summary_sales = next(
            li
            for li in summary_resp.json()["line_items"]
            if li["tax_category"] == "SALES_INCOME"
        )
        # $100.00 pre-tax WA + $250.00 OOS = $350.00 on BOTH surfaces.
        assert monthly_total == summary_sales["total"] == 350.00


# ---------------------------------------------------------------------------
# Tests: /tax-summary bno_monthly and /tax-summary/monthly reconcile with the
# B&O CSV on confirmed out-of-state SALES_INCOME (REQ-FIX-TAX-002 / REQ-020 /
# REQ-016 — P2-b1c)
# ---------------------------------------------------------------------------


class TestBnoSurfacesExcludeConfirmedOutOfStateSales:
    """The round-1 fix made the B&O CSV/DOR upload exclude confirmed
    out-of-state retail sales from the Retailing basis. This must also hold
    for /api/tax-summary's bno_monthly/bno_quarterly (the dashboard's
    'B&O subtotals', REQ-016) and /api/tax-summary/monthly (the B&O wizard's
    readiness surface) — otherwise the dashboard shows a different B&O gross
    receipts figure than the downloaded CSV/DOR upload for the same period.
    The income-tax line_items/gross_income, by contrast, must stay on the
    gross-incl-out-of-state basis (correct for Schedule C / 1065 gross
    receipts)."""

    def _make_wa_and_oos_sales(self, session: Session) -> None:
        wa_tx = Transaction(
            id=str(uuid.uuid4()),
            source=Source.SHOPIFY.value,
            source_id=str(uuid.uuid4()),
            source_hash=str(uuid.uuid4()),
            date="2026-01-10",
            description="Shopify Order — WA retail",
            amount=Decimal("100.00"),
            currency="USD",
            entity=Entity.BLACKLINE.value,
            direction=Direction.INCOME.value,
            tax_category=TaxCategory.SALES_INCOME.value,
            status=TransactionStatus.CONFIRMED.value,
            confidence=0.95,
            raw_data={
                "total_price": "100.00",
                "total_tax": "9.30",
                "shipping_address": {"province_code": "WA"},
                "tax_lines": [{"title": "Washington State Tax"}],
            },
            confirmed_by=ConfirmedBy.HUMAN.value,
        )
        oos_tx = Transaction(
            id=str(uuid.uuid4()),
            source=Source.SHOPIFY.value,
            source_id=str(uuid.uuid4()),
            source_hash=str(uuid.uuid4()),
            date="2026-01-15",
            description="Shopify Order — OR (confirmed out-of-state)",
            amount=Decimal("250.00"),
            currency="USD",
            entity=Entity.BLACKLINE.value,
            direction=Direction.INCOME.value,
            tax_category=TaxCategory.SALES_INCOME.value,
            status=TransactionStatus.CONFIRMED.value,
            confidence=0.95,
            raw_data={
                "total_price": "250.00",
                "total_tax": "0.00",
                "shipping_address": {"province_code": "OR"},
                "tax_lines": [],
            },
            confirmed_by=ConfirmedBy.HUMAN.value,
        )
        session.add_all([wa_tx, oos_tx])
        session.commit()

    def test_tax_summary_bno_monthly_excludes_confirmed_oos(
        self, client: TestClient
    ) -> None:
        with _TestSession() as s:
            self._make_wa_and_oos_sales(s)

        resp = client.get(
            "/api/tax-summary", params={"entity": "blackline", "year": 2026}
        )
        assert resp.status_code == 200
        body = resp.json()

        jan = next(m for m in body["bno_monthly"] if m["month"] == "2026-01")
        # WA-only pretax ($100 - $9.30 tax = $90.70) — the $250 OOS order is
        # excluded, matching the B&O CSV/DOR wa_taxable basis.
        assert jan["income"] == 90.70

        # The income-tax line_items stay on the gross-incl-OOS basis:
        # $90.70 (WA) + $250.00 (OOS) = $340.70.
        sales_line_item = next(
            li for li in body["line_items"] if li["tax_category"] == "SALES_INCOME"
        )
        assert sales_line_item["total"] == 340.70

    def test_tax_summary_monthly_uses_income_basis_including_oos(
        self, client: TestClient
    ) -> None:
        """P2-201: /tax-summary/monthly serves the income-tax Financials
        drill-down and must INCLUDE out-of-state sales (pre-tax) so monthly
        cells reconcile with annual line_items. The B&O wizard gets its
        OOS-deducted Retailing basis from compute_retail_detail instead."""
        with _TestSession() as s:
            self._make_wa_and_oos_sales(s)

        resp = client.get(
            "/api/tax-summary/monthly",
            params={"entity": "blackline", "year": 2026},
        )
        assert resp.status_code == 200
        body = resp.json()

        jan = next(m for m in body["months"] if m["month"] == "2026-01")
        sales = next(
            c for c in jan["categories"] if c["tax_category"] == "SALES_INCOME"
        )
        # $90.70 pre-tax WA + $250.00 OOS = $340.70 (income basis, not the
        # B&O wa_taxable $90.70).
        assert sales["total"] == 340.70

    def test_bno_csv_retailing_total_matches_tax_summary_bno_monthly(
        self, client: TestClient
    ) -> None:
        """The downloaded B&O CSV and the dashboard's bno_monthly must
        report the same Retailing gross receipts for the same period."""
        with _TestSession() as s:
            self._make_wa_and_oos_sales(s)

        summary_resp = client.get(
            "/api/tax-summary", params={"entity": "blackline", "year": 2026}
        )
        csv_resp = client.get(
            "/api/export/bno",
            params={"entity": "blackline", "year": 2026, "quarter": 1},
        )
        assert summary_resp.status_code == 200
        assert csv_resp.status_code == 200

        jan = next(
            m
            for m in summary_resp.json()["bno_monthly"]
            if m["month"] == "2026-01"
        )

        rows = list(csv.reader(io.StringIO(csv_resp.text)))
        csv_retailing_total = Decimal("0")
        for r in rows[1:]:
            if len(r) > 2 and r[2] == "Retailing" and "TOTAL" not in r[0]:
                csv_retailing_total += Decimal(r[3])

        assert Decimal(str(jan["income"])) == csv_retailing_total == Decimal("90.70")
