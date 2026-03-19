"""Tests for tax_tips generation in GET /api/tax-summary.

Tests that _generate_tax_tips emits the correct tips based on transaction state.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import date, timedelta
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
# In-memory test database
# ---------------------------------------------------------------------------

_TEST_DB_URI = "file:tax_tips_test?mode=memory&cache=shared&uri=true"

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
    session = _TestSession()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
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
# Helper factories
# ---------------------------------------------------------------------------


def _make_tx(
    session: Session,
    *,
    entity: str = Entity.SPARKRY.value,
    tax_category: str = TaxCategory.CONSULTING_INCOME.value,
    amount: Decimal = Decimal("5000.00"),
    direction: str = Direction.INCOME.value,
    date: str = "2025-06-15",
    status: str = TransactionStatus.CONFIRMED.value,
    source: str = Source.GMAIL_N8N.value,
    direction_str: str | None = None,
    reimbursement_link: str | None = None,
    payer_1099: str | None = None,
    tax_subcategory: str | None = None,
) -> Transaction:
    if direction_str is not None:
        direction = direction_str
    tx = Transaction(
        id=str(uuid.uuid4()),
        source=source,
        source_id=str(uuid.uuid4()),
        source_hash=str(uuid.uuid4()),
        date=date,
        description="Test transaction",
        amount=amount,
        currency="USD",
        entity=entity,
        direction=direction,
        tax_category=tax_category,
        status=status,
        confidence=0.95,
        raw_data={"test": True},
        confirmed_by=ConfirmedBy.HUMAN.value,
        reimbursement_link=reimbursement_link,
        payer_1099=payer_1099,
        tax_subcategory=tax_subcategory,
    )
    session.add(tx)
    session.commit()
    return tx


def _get_tips(client: TestClient, entity: str = "sparkry", year: int = 2025) -> list[dict]:
    resp = client.get("/api/tax-summary", params={"entity": entity, "year": year})
    assert resp.status_code == 200
    return resp.json().get("tax_tips", [])


# ---------------------------------------------------------------------------
# Test: tax_tips field always present
# ---------------------------------------------------------------------------


class TestTaxTipsFieldPresent:
    def test_tax_tips_present_in_response(self, client: TestClient) -> None:
        """tax_tips array is always present in the tax-summary response."""
        resp = client.get("/api/tax-summary", params={"entity": "sparkry", "year": 2025})
        assert resp.status_code == 200
        data = resp.json()
        assert "tax_tips" in data
        assert isinstance(data["tax_tips"], list)

    def test_tip_shape(self, client: TestClient) -> None:
        """Each tip has required fields: id, type, title, detail, dismissible."""
        tips = _get_tips(client, entity="sparkry", year=2025)
        # Sparkry with no transactions will have home_office and vehicle tips
        for tip in tips:
            assert "id" in tip
            assert "type" in tip
            assert "title" in tip
            assert "detail" in tip
            assert "dismissible" in tip
            assert tip["dismissible"] is True


# ---------------------------------------------------------------------------
# Test: home office tip
# ---------------------------------------------------------------------------


class TestHomeOfficeTip:
    def test_home_office_tip_emitted_for_sparkry(self, client: TestClient) -> None:
        """home_office tip is emitted for Sparkry (no HOME_OFFICE transactions needed)."""
        tips = _get_tips(client, entity="sparkry")
        tip_types = [t["type"] for t in tips]
        assert "home_office" in tip_types

    def test_home_office_tip_not_emitted_for_blackline(self, client: TestClient) -> None:
        """home_office tip is NOT emitted for BlackLine (no home office deduction)."""
        tips = _get_tips(client, entity="blackline")
        tip_types = [t["type"] for t in tips]
        assert "home_office" not in tip_types

    def test_home_office_tip_not_emitted_for_personal(self, client: TestClient) -> None:
        """home_office tip is NOT emitted for Personal entity."""
        tips = _get_tips(client, entity="personal")
        tip_types = [t["type"] for t in tips]
        assert "home_office" not in tip_types

    def test_home_office_tip_mentions_deduction_amount(self, client: TestClient) -> None:
        """home_office tip title mentions the $180 deduction amount."""
        tips = _get_tips(client, entity="sparkry")
        home_tip = next(t for t in tips if t["type"] == "home_office")
        assert "180" in home_tip["title"]


# ---------------------------------------------------------------------------
# Test: estimated tax tip
# ---------------------------------------------------------------------------


class TestEstimatedTaxTip:
    def test_estimated_tax_tip_emitted_when_high_income_no_payments(
        self, client: TestClient
    ) -> None:
        """estimated_tax tip appears when income > $10K and no estimated payments exist."""
        with _TestSession() as s:
            _make_tx(s, entity=Entity.SPARKRY.value, amount=Decimal("15000.00"))

        tips = _get_tips(client, entity="sparkry")
        tip_types = [t["type"] for t in tips]
        assert "estimated_tax" in tip_types

    def test_estimated_tax_tip_not_emitted_when_income_low(
        self, client: TestClient
    ) -> None:
        """estimated_tax tip is NOT emitted when income <= $10K."""
        with _TestSession() as s:
            _make_tx(s, entity=Entity.SPARKRY.value, amount=Decimal("5000.00"))

        tips = _get_tips(client, entity="sparkry")
        tip_types = [t["type"] for t in tips]
        assert "estimated_tax" not in tip_types

    def test_estimated_tax_tip_not_emitted_when_payments_exist(
        self, client: TestClient
    ) -> None:
        """estimated_tax tip is NOT emitted when estimated tax payments are recorded."""
        with _TestSession() as s:
            _make_tx(s, entity=Entity.SPARKRY.value, amount=Decimal("20000.00"))
            # Add an estimated tax payment
            _make_tx(
                s,
                entity=Entity.SPARKRY.value,
                tax_category=TaxCategory.TAXES_AND_LICENSES.value,
                direction=Direction.EXPENSE.value,
                amount=Decimal("-2000.00"),
                tax_subcategory="estimated",
            )

        tips = _get_tips(client, entity="sparkry")
        tip_types = [t["type"] for t in tips]
        assert "estimated_tax" not in tip_types

    def test_estimated_tax_tip_not_emitted_for_personal(
        self, client: TestClient
    ) -> None:
        """estimated_tax tip is NOT emitted for Personal entity."""
        tips = _get_tips(client, entity="personal")
        tip_types = [t["type"] for t in tips]
        assert "estimated_tax" not in tip_types


# ---------------------------------------------------------------------------
# Test: reimbursable tip
# ---------------------------------------------------------------------------


class TestReimbursableTip:
    def test_reimbursable_tip_emitted_for_old_unlinked_expenses(
        self, client: TestClient
    ) -> None:
        """reimbursable tip appears when there are unlinked reimbursable expenses > 30 days."""
        today = date.today()
        year = today.year
        old_date = (today - timedelta(days=45)).isoformat()
        with _TestSession() as s:
            _make_tx(
                s,
                entity=Entity.SPARKRY.value,
                tax_category=TaxCategory.REIMBURSABLE.value,
                direction_str=Direction.REIMBURSABLE.value,
                amount=Decimal("-500.00"),
                date=old_date,
            )

        tips = _get_tips(client, entity="sparkry", year=year)
        tip_types = [t["type"] for t in tips]
        assert "reimbursable" in tip_types

    def test_reimbursable_tip_not_emitted_for_recent_expenses(
        self, client: TestClient
    ) -> None:
        """reimbursable tip is NOT emitted for reimbursable expenses within 30 days."""
        today = date.today()
        year = today.year
        recent_date = (today - timedelta(days=10)).isoformat()
        with _TestSession() as s:
            _make_tx(
                s,
                entity=Entity.SPARKRY.value,
                tax_category=TaxCategory.REIMBURSABLE.value,
                direction_str=Direction.REIMBURSABLE.value,
                amount=Decimal("-500.00"),
                date=recent_date,
            )

        tips = _get_tips(client, entity="sparkry", year=year)
        tip_types = [t["type"] for t in tips]
        assert "reimbursable" not in tip_types

    def test_reimbursable_tip_not_emitted_when_linked(
        self, client: TestClient
    ) -> None:
        """reimbursable tip is NOT emitted when expense is linked to a reimbursement."""
        today = date.today()
        year = today.year
        old_date = (today - timedelta(days=45)).isoformat()
        with _TestSession() as s:
            reimb_id = str(uuid.uuid4())
            # Create the reimbursement transaction first
            reimbursement = Transaction(
                id=reimb_id,
                source=Source.GMAIL_N8N.value,
                source_id=str(uuid.uuid4()),
                source_hash=str(uuid.uuid4()),
                date=old_date,
                description="Reimbursement received",
                amount=Decimal("500.00"),
                currency="USD",
                entity=Entity.SPARKRY.value,
                direction=Direction.INCOME.value,
                tax_category=TaxCategory.REIMBURSABLE.value,
                status=TransactionStatus.CONFIRMED.value,
                confidence=0.95,
                raw_data={},
                confirmed_by=ConfirmedBy.HUMAN.value,
            )
            s.add(reimbursement)
            s.commit()

            _make_tx(
                s,
                entity=Entity.SPARKRY.value,
                tax_category=TaxCategory.REIMBURSABLE.value,
                direction_str=Direction.REIMBURSABLE.value,
                amount=Decimal("-500.00"),
                date=old_date,
                reimbursement_link=reimb_id,
            )

        tips = _get_tips(client, entity="sparkry", year=year)
        tip_types = [t["type"] for t in tips]
        assert "reimbursable" not in tip_types


# ---------------------------------------------------------------------------
# Test: vehicle tip
# ---------------------------------------------------------------------------


class TestVehicleTip:
    def test_vehicle_tip_emitted_when_no_car_transactions(
        self, client: TestClient
    ) -> None:
        """vehicle tip appears for business entities with no CAR_AND_TRUCK transactions."""
        tips = _get_tips(client, entity="sparkry")
        tip_types = [t["type"] for t in tips]
        assert "vehicle" in tip_types

    def test_vehicle_tip_not_emitted_when_car_transactions_exist(
        self, client: TestClient
    ) -> None:
        """vehicle tip is NOT emitted when CAR_AND_TRUCK transactions exist."""
        with _TestSession() as s:
            _make_tx(
                s,
                entity=Entity.SPARKRY.value,
                tax_category=TaxCategory.CAR_AND_TRUCK.value,
                direction=Direction.EXPENSE.value,
                amount=Decimal("-200.00"),
            )

        tips = _get_tips(client, entity="sparkry")
        tip_types = [t["type"] for t in tips]
        assert "vehicle" not in tip_types

    def test_vehicle_tip_not_emitted_for_personal(
        self, client: TestClient
    ) -> None:
        """vehicle tip is NOT emitted for Personal entity."""
        tips = _get_tips(client, entity="personal")
        tip_types = [t["type"] for t in tips]
        assert "vehicle" not in tip_types
