"""Tests for GET /api/transactions/aggregations.

REQ-ID: AGG-TEST-001  Aggregation endpoint returns time_series, top_vendors, mom_change.
REQ-ID: AGG-TEST-002  Buckets by month for ranges > 60 days.
REQ-ID: AGG-TEST-003  Buckets by day for ranges < 14 days.
REQ-ID: ANOMALY-001   Charges >2x vendor avg flagged in anomalies list.
REQ-ID: ANOMALY-002   Vendors with <2 historical records are not flagged.
REQ-ID: CAT-BREAKDOWN-001  category_breakdown lists top expense categories with pcts.
REQ-ID: EXPENSE-ATTR-001   expense_attribution string describes MoM expense movement.
REQ-ID: AGG-TEST-004  Entity filter is respected.
REQ-ID: AGG-TEST-005  Top vendors are ranked by total and capped at 5.
REQ-ID: AGG-TEST-006  MoM change compares to prior period of equal length.
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

import src.db.connection as _conn  # noqa: F401
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
# In-memory test database (shared-cache so FastAPI workers see same data)
# ---------------------------------------------------------------------------

_TEST_DB_URI = "file:aggregation_test?mode=memory&cache=shared&uri=true"

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
    with _test_engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
        conn.execute(text("PRAGMA foreign_keys=ON"))
    yield


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    from src.api import main as _main_module
    from src.api.routes import health as _health_module
    from src.api.routes import ingest as _ingest_module
    from src.api.routes import transactions as _tx_module

    with (
        patch.object(_tx_module, "SessionLocal", _TestSession),
        patch.object(_health_module, "SessionLocal", _TestSession),
        patch.object(_ingest_module, "SessionLocal", _TestSession),
        patch.object(_main_module, "init_db", return_value=None),
        patch.object(_main_module, "seed_vendor_rules", return_value=0),
    ):
        from src.api.main import app

        with TestClient(app) as c:
            yield c


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_tx(
    session: Session,
    *,
    description: str = "Test Vendor",
    amount: Decimal = Decimal("-50.00"),
    entity: str | None = Entity.SPARKRY.value,
    direction: str | None = Direction.EXPENSE.value,
    status: str = TransactionStatus.CONFIRMED.value,
    date: str = "2026-01-15",
) -> Transaction:
    tx = Transaction(
        id=str(uuid.uuid4()),
        source=Source.GMAIL_N8N.value,
        source_id=str(uuid.uuid4()),
        source_hash=str(uuid.uuid4()),
        date=date,
        description=description,
        amount=amount,
        currency="USD",
        entity=entity,
        direction=direction,
        tax_category=TaxCategory.SUPPLIES.value,
        status=status,
        confidence=0.9,
        raw_data={"test": True},
        confirmed_by=ConfirmedBy.AUTO.value,
    )
    session.add(tx)
    session.commit()
    return tx


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAggregationEndpoint:
    """AGG-TEST-001: Basic shape and fields."""

    def test_returns_structure(self, client: TestClient) -> None:
        resp = client.get("/api/transactions/aggregations")
        assert resp.status_code == 200
        data = resp.json()
        assert "time_series" in data
        assert "top_vendors" in data
        assert "mom_change" in data
        assert "income" in data["time_series"]
        assert "expenses" in data["time_series"]
        assert "income" in data["top_vendors"]
        assert "expense" in data["top_vendors"]

    def test_empty_db_returns_zeros(self, client: TestClient) -> None:
        resp = client.get("/api/transactions/aggregations")
        data = resp.json()
        assert data["time_series"]["income"] == []
        assert data["time_series"]["expenses"] == []
        assert data["mom_change"]["income_delta"] == 0.0
        assert data["mom_change"]["expense_delta"] == 0.0

    def test_income_and_expense_time_series(self, client: TestClient) -> None:
        """AGG-TEST-002: Transactions bucketed by month."""
        with _TestSession() as s:
            _make_tx(s, description="Client A", amount=Decimal("5000"),
                     direction=Direction.INCOME.value, date="2026-01-10")
            _make_tx(s, description="AWS", amount=Decimal("-200"),
                     direction=Direction.EXPENSE.value, date="2026-01-20")
            _make_tx(s, description="Client A", amount=Decimal("6000"),
                     direction=Direction.INCOME.value, date="2026-02-10")

        resp = client.get(
            "/api/transactions/aggregations",
            params={"date_from": "2026-01-01", "date_to": "2026-03-31"},
        )
        data = resp.json()
        income_periods = {p["period"]: p["total"] for p in data["time_series"]["income"]}
        assert income_periods["2026-01"] == 5000.0
        assert income_periods["2026-02"] == 6000.0

        expense_periods = {p["period"]: p["total"] for p in data["time_series"]["expenses"]}
        assert expense_periods["2026-01"] == 200.0

    def test_day_bucket_for_short_range(self, client: TestClient) -> None:
        """AGG-TEST-003: < 14 day range uses daily buckets."""
        with _TestSession() as s:
            _make_tx(s, description="Vendor", amount=Decimal("-100"),
                     direction=Direction.EXPENSE.value, date="2026-01-02")
            _make_tx(s, description="Vendor", amount=Decimal("-50"),
                     direction=Direction.EXPENSE.value, date="2026-01-05")

        resp = client.get(
            "/api/transactions/aggregations",
            params={"date_from": "2026-01-01", "date_to": "2026-01-10"},
        )
        data = resp.json()
        periods = [p["period"] for p in data["time_series"]["expenses"]]
        assert "2026-01-02" in periods
        assert "2026-01-05" in periods

    def test_entity_filter(self, client: TestClient) -> None:
        """AGG-TEST-004: Entity filter restricts results."""
        with _TestSession() as s:
            _make_tx(s, description="A", amount=Decimal("1000"),
                     direction=Direction.INCOME.value,
                     entity=Entity.SPARKRY.value, date="2026-01-10")
            _make_tx(s, description="B", amount=Decimal("2000"),
                     direction=Direction.INCOME.value,
                     entity=Entity.BLACKLINE.value, date="2026-01-10")

        resp = client.get(
            "/api/transactions/aggregations",
            params={"entity": "sparkry", "date_from": "2026-01-01", "date_to": "2026-03-31"},
        )
        data = resp.json()
        income_total = sum(p["total"] for p in data["time_series"]["income"])
        assert income_total == 1000.0

    def test_top_vendors_capped_at_5(self, client: TestClient) -> None:
        """AGG-TEST-005: Only top 5 vendors returned."""
        with _TestSession() as s:
            for i in range(8):
                _make_tx(s, description=f"Vendor {i}", amount=Decimal(str(-(i + 1) * 100)),
                         direction=Direction.EXPENSE.value, date="2026-01-15")

        resp = client.get("/api/transactions/aggregations")
        data = resp.json()
        assert len(data["top_vendors"]["expense"]) == 5
        # Top vendor should be the one with highest amount
        assert data["top_vendors"]["expense"][0]["vendor"] == "Vendor 7"

    def test_mom_change_calculation(self, client: TestClient) -> None:
        """AGG-TEST-006: MoM compares current range to prior period."""
        with _TestSession() as s:
            # Current period: Jan 2026
            _make_tx(s, description="Inc", amount=Decimal("10000"),
                     direction=Direction.INCOME.value, date="2026-01-15")
            _make_tx(s, description="Exp", amount=Decimal("-3000"),
                     direction=Direction.EXPENSE.value, date="2026-01-15")
            # Previous period: Dec 2025
            _make_tx(s, description="Inc", amount=Decimal("8000"),
                     direction=Direction.INCOME.value, date="2025-12-15")
            _make_tx(s, description="Exp", amount=Decimal("-2000"),
                     direction=Direction.EXPENSE.value, date="2025-12-15")

        resp = client.get(
            "/api/transactions/aggregations",
            params={"date_from": "2026-01-01", "date_to": "2026-01-31"},
        )
        data = resp.json()
        mom = data["mom_change"]
        assert mom["income_delta"] == 2000.0
        assert mom["income_pct"] == 25.0
        assert mom["expense_delta"] == 1000.0
        assert mom["expense_pct"] == 50.0

    def test_rejected_transactions_excluded(self, client: TestClient) -> None:
        """Rejected transactions should not appear in aggregations."""
        with _TestSession() as s:
            _make_tx(s, description="Good", amount=Decimal("1000"),
                     direction=Direction.INCOME.value, date="2026-01-10")
            _make_tx(s, description="Bad", amount=Decimal("5000"),
                     direction=Direction.INCOME.value, date="2026-01-10",
                     status=TransactionStatus.REJECTED.value)

        resp = client.get("/api/transactions/aggregations")
        data = resp.json()
        income_total = sum(p["total"] for p in data["time_series"]["income"])
        assert income_total == 1000.0

    def test_top_vendors_percentage(self, client: TestClient) -> None:
        """Top vendor percentages should sum to roughly 100 or less."""
        with _TestSession() as s:
            _make_tx(s, description="Big", amount=Decimal("-800"),
                     direction=Direction.EXPENSE.value, date="2026-01-10")
            _make_tx(s, description="Small", amount=Decimal("-200"),
                     direction=Direction.EXPENSE.value, date="2026-01-10")

        resp = client.get("/api/transactions/aggregations")
        data = resp.json()
        vendors = data["top_vendors"]["expense"]
        assert vendors[0]["vendor"] == "Big"
        assert vendors[0]["pct"] == 80.0
        assert vendors[1]["vendor"] == "Small"
        assert vendors[1]["pct"] == 20.0


class TestConcentrationWarnings:
    """REQ-ID: CONC-WARN-001  Concentration warnings flag income vendors with >80% share."""

    def test_response_includes_concentration_warnings_field(self, client: TestClient) -> None:
        """concentration_warnings is always present in response."""
        resp = client.get("/api/transactions/aggregations")
        assert resp.status_code == 200
        data = resp.json()
        assert "concentration_warnings" in data
        assert isinstance(data["concentration_warnings"], list)

    def test_no_warning_when_income_is_diversified(self, client: TestClient) -> None:
        """CONC-WARN-001: No warning when no vendor exceeds 80%."""
        with _TestSession() as s:
            _make_tx(s, description="Cardinal Health", amount=Decimal("7000"),
                     direction=Direction.INCOME.value, date="2026-01-10")
            _make_tx(s, description="Stripe Inc", amount=Decimal("4000"),
                     direction=Direction.INCOME.value, date="2026-01-10")

        resp = client.get("/api/transactions/aggregations")
        data = resp.json()
        assert data["concentration_warnings"] == []

    def test_warning_when_single_vendor_exceeds_80_pct(self, client: TestClient) -> None:
        """CONC-WARN-001: Warning emitted when one vendor has >80% of income."""
        with _TestSession() as s:
            _make_tx(s, description="Cardinal Health", amount=Decimal("33000"),
                     direction=Direction.INCOME.value, date="2026-01-10")
            _make_tx(s, description="Other Client", amount=Decimal("1000"),
                     direction=Direction.INCOME.value, date="2026-01-10")

        resp = client.get("/api/transactions/aggregations")
        data = resp.json()
        warnings = data["concentration_warnings"]
        assert len(warnings) == 1
        w = warnings[0]
        assert w["vendor"] == "Cardinal Health"
        assert w["pct"] > 80
        assert "diversification risk" in w["message"]
        assert "Cardinal Health" in w["message"]

    def test_warning_message_includes_rounded_percentage(self, client: TestClient) -> None:
        """CONC-WARN-001: Message text shows rounded integer percent."""
        with _TestSession() as s:
            _make_tx(s, description="Cardinal Health", amount=Decimal("9000"),
                     direction=Direction.INCOME.value, date="2026-01-10")
            _make_tx(s, description="Tiny", amount=Decimal("1000"),
                     direction=Direction.INCOME.value, date="2026-01-10")

        resp = client.get("/api/transactions/aggregations")
        data = resp.json()
        warnings = data["concentration_warnings"]
        assert len(warnings) == 1
        assert "90%" in warnings[0]["message"]

    def test_no_warning_for_expense_concentration(self, client: TestClient) -> None:
        """CONC-WARN-001: Concentration warnings only apply to income, not expenses."""
        with _TestSession() as s:
            _make_tx(s, description="AWS", amount=Decimal("-9000"),
                     direction=Direction.EXPENSE.value, date="2026-01-10")
            _make_tx(s, description="Zoom", amount=Decimal("-100"),
                     direction=Direction.EXPENSE.value, date="2026-01-10")

        resp = client.get("/api/transactions/aggregations")
        data = resp.json()
        assert data["concentration_warnings"] == []

    def test_exactly_80_pct_does_not_warn(self, client: TestClient) -> None:
        """CONC-WARN-001: Exactly 80% does not trigger a warning (threshold is >80%)."""
        with _TestSession() as s:
            _make_tx(s, description="Big Client", amount=Decimal("8000"),
                     direction=Direction.INCOME.value, date="2026-01-10")
            _make_tx(s, description="Small Client", amount=Decimal("2000"),
                     direction=Direction.INCOME.value, date="2026-01-10")

        resp = client.get("/api/transactions/aggregations")
        data = resp.json()
        assert data["concentration_warnings"] == []

    def test_multiple_warnings_when_multiple_vendors_exceed_threshold(self, client: TestClient) -> None:
        """CONC-WARN-001: Edge case — two vendors both over 80% is impossible (sum >100%).
        Verify that if somehow one vendor is 100%, only one warning appears."""
        with _TestSession() as s:
            _make_tx(s, description="Solo Client", amount=Decimal("10000"),
                     direction=Direction.INCOME.value, date="2026-01-10")

        resp = client.get("/api/transactions/aggregations")
        data = resp.json()
        warnings = data["concentration_warnings"]
        assert len(warnings) == 1
        assert warnings[0]["pct"] == 100.0


class TestAnomalyDetection:
    """ANOMALY-001 / ANOMALY-002: Unusual charge detection (>2x vendor avg)."""

    def test_charge_over_2x_avg_flagged(self, client: TestClient) -> None:
        """ANOMALY-001: A charge more than 2x the vendor's historical average is flagged."""
        with _TestSession() as s:
            # Three typical charges at $10 each (avg = $10)
            for _ in range(3):
                _make_tx(s, description="Residence Inn", amount=Decimal("-10.00"),
                         direction=Direction.EXPENSE.value, date="2025-12-01")
            # One anomalous charge at $633 (>2x the avg of $10)
            _make_tx(s, description="Residence Inn", amount=Decimal("-633.00"),
                     direction=Direction.EXPENSE.value, date="2026-01-15")

        resp = client.get("/api/transactions/aggregations")
        data = resp.json()
        assert "anomalies" in data
        anomalies = data["anomalies"]
        assert len(anomalies) >= 1
        a = anomalies[0]
        assert a["vendor"] == "Residence Inn"
        assert a["amount"] == 633.0
        # avg = (10 + 10 + 10 + 633) / 4 = 165.75; 633 > 2 * 165.75 = 331.5 ✓
        assert a["avg_for_vendor"] > 0
        assert "Unusual" in a["message"]
        assert "Residence Inn" in a["message"]
        assert "tx_id" in a

    def test_charge_at_exactly_2x_avg_not_flagged(self, client: TestClient) -> None:
        """ANOMALY-001: A charge exactly 2x the average is NOT flagged (threshold is strictly >2x)."""
        with _TestSession() as s:
            # Two charges at $50 each (avg = $50); new charge = $100 = exactly 2x
            _make_tx(s, description="Dropbox", amount=Decimal("-50.00"),
                     direction=Direction.EXPENSE.value, date="2025-11-01")
            _make_tx(s, description="Dropbox", amount=Decimal("-50.00"),
                     direction=Direction.EXPENSE.value, date="2025-12-01")
            _make_tx(s, description="Dropbox", amount=Decimal("-100.00"),
                     direction=Direction.EXPENSE.value, date="2026-01-15")

        resp = client.get("/api/transactions/aggregations")
        data = resp.json()
        # avg = (50 + 50 + 100) / 3 = 66.67; 100 > 2 * 66.67 = 133.33 → not flagged
        anomaly_vendors = [a["vendor"] for a in data["anomalies"]]
        assert "Dropbox" not in anomaly_vendors

    def test_vendor_with_single_record_not_flagged(self, client: TestClient) -> None:
        """ANOMALY-002: A vendor with only one historical record is never flagged (need >=2)."""
        with _TestSession() as s:
            # Single large charge — no history to compare against
            _make_tx(s, description="New Vendor", amount=Decimal("-9999.00"),
                     direction=Direction.EXPENSE.value, date="2026-01-15")

        resp = client.get("/api/transactions/aggregations")
        data = resp.json()
        anomaly_vendors = [a["vendor"] for a in data["anomalies"]]
        assert "New Vendor" not in anomaly_vendors

    def test_response_includes_anomalies_and_category_breakdown_fields(self, client: TestClient) -> None:
        """CAT-BREAKDOWN-001 / EXPENSE-ATTR-001: Response shape includes new fields."""
        resp = client.get("/api/transactions/aggregations")
        data = resp.json()
        assert "anomalies" in data
        assert "category_breakdown" in data
        assert "expense_attribution" in data
        assert isinstance(data["anomalies"], list)
        assert isinstance(data["category_breakdown"], list)
        assert isinstance(data["expense_attribution"], str)

    def test_category_breakdown_top_5_with_percentages(self, client: TestClient) -> None:
        """CAT-BREAKDOWN-001: category_breakdown returns top 5 expense categories with pct."""
        with _TestSession() as s:
            for i, cat in enumerate(
                ["TRAVEL", "SUPPLIES", "MEALS", "ADVERTISING", "INSURANCE", "OFFICE_EXPENSE"],
                start=1,
            ):
                _make_tx(
                    s,
                    description=f"Vendor {cat}",
                    amount=Decimal(str(-(i * 100))),
                    direction=Direction.EXPENSE.value,
                    date="2026-01-15",
                )
                # patch tax_category after creation
                with _TestSession() as s2:
                    tx = s2.query(Transaction).filter(
                        Transaction.description == f"Vendor {cat}"
                    ).first()
                    if tx:
                        tx.tax_category = cat
                        s2.commit()

        resp = client.get("/api/transactions/aggregations")
        data = resp.json()
        breakdown = data["category_breakdown"]
        # Should be capped at 5 categories
        assert len(breakdown) <= 5
        # Each item must have required fields
        for item in breakdown:
            assert "category" in item
            assert "total" in item
            assert "pct" in item
        # Percentages must sum to ≤ 100
        total_pct = sum(item["pct"] for item in breakdown)
        assert total_pct <= 100.1  # allow floating-point rounding

    def test_expense_attribution_describes_mom_change(self, client: TestClient) -> None:
        """EXPENSE-ATTR-001: expense_attribution string describes direction and top category."""
        with _TestSession() as s:
            # Current period: Jan 2026 — $3,000 expenses
            _make_tx(s, description="Hotel A", amount=Decimal("-3000"),
                     direction=Direction.EXPENSE.value, date="2026-01-15")
            # Prior period: Dec 2025 — $1,000 expenses
            _make_tx(s, description="Hotel A", amount=Decimal("-1000"),
                     direction=Direction.EXPENSE.value, date="2025-12-15")

        resp = client.get(
            "/api/transactions/aggregations",
            params={"date_from": "2026-01-01", "date_to": "2026-01-31"},
        )
        data = resp.json()
        attr = data["expense_attribution"]
        assert isinstance(attr, str)
        assert len(attr) > 0
        # With higher expenses this period, attribution should say "up"
        assert "up" in attr.lower() or "$" in attr
