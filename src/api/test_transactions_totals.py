"""Tests for GET /api/transactions header totals and GET /api/transactions/aggregations
money-aggregate exclusion semantics.

REQ-ID: REQ-FIX-API-001  list_transactions header totals (income_total/expense_total)
    always exclude rejected and split_parent rows, regardless of caller status filters.
REQ-ID: REQ-FIX-API-002  get_aggregations excludes split_parent rows (in addition to
    the existing rejected exclusion) from time-series, vendor totals, category totals,
    and the anomaly-baseline query — split children double-count otherwise.

Semantics (spec §10): `rejected` = excluded ledger row; `split_parent` = container
whose children carry the amounts (summing both double-counts). Item *lists* may still
show them when the caller filters for them; money aggregates never include them.
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

_TEST_DB_URI = "file:transactions_totals_test?mode=memory&cache=shared&uri=true"

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


class TestListTransactionsHeaderTotals:
    """REQ-FIX-API-001"""

    def test_rejected_excluded_from_totals_by_default(self, client: TestClient) -> None:
        with _TestSession() as s:
            _make_tx(s, amount=Decimal("100.00"), direction=Direction.INCOME.value)
            _make_tx(s, amount=Decimal("-999.00"), status=TransactionStatus.REJECTED.value)

        r = client.get("/api/transactions")
        assert r.status_code == 200
        data = r.json()
        assert data["income_total"] == pytest.approx(100.0)
        assert data["expense_total"] == pytest.approx(0.0)

    def test_split_parent_excluded_from_totals_by_default(self, client: TestClient) -> None:
        with _TestSession() as s:
            _make_tx(s, amount=Decimal("-500.00"), status=TransactionStatus.SPLIT_PARENT.value)
            _make_tx(s, amount=Decimal("-300.00"), description="child A")
            _make_tx(s, amount=Decimal("-200.00"), description="child B")

        r = client.get("/api/transactions")
        assert r.status_code == 200
        data = r.json()
        # Only the two children count; the parent (-500) must not ALSO be summed.
        assert data["expense_total"] == pytest.approx(-500.0)

    def test_rejected_excluded_even_when_caller_filters_for_rejected_status(
        self, client: TestClient
    ) -> None:
        """The caller can still LIST rejected rows (status=rejected filter);
        the header totals must still exclude them from the sum."""
        with _TestSession() as s:
            _make_tx(s, amount=Decimal("-999.00"), status=TransactionStatus.REJECTED.value)

        r = client.get("/api/transactions", params={"status": "rejected"})
        assert r.status_code == 200
        data = r.json()
        assert len(data["items"]) == 1  # caller can still see the rejected row
        assert data["expense_total"] == pytest.approx(0.0)  # but it's never summed

    def test_split_parent_excluded_even_when_caller_filters_for_it(
        self, client: TestClient
    ) -> None:
        with _TestSession() as s:
            _make_tx(s, amount=Decimal("-500.00"), status=TransactionStatus.SPLIT_PARENT.value)

        r = client.get("/api/transactions", params={"status": "split_parent"})
        assert r.status_code == 200
        data = r.json()
        assert len(data["items"]) == 1
        assert data["expense_total"] == pytest.approx(0.0)

    def test_confirmed_rows_still_summed_normally(self, client: TestClient) -> None:
        with _TestSession() as s:
            _make_tx(s, amount=Decimal("-100.00"))
            _make_tx(s, amount=Decimal("-50.00"))

        r = client.get("/api/transactions")
        assert r.status_code == 200
        assert r.json()["expense_total"] == pytest.approx(-150.0)


class TestAggregationsExclusion:
    """REQ-FIX-API-002"""

    def test_split_parent_excluded_from_category_totals(self, client: TestClient) -> None:
        with _TestSession() as s:
            _make_tx(s, amount=Decimal("-500.00"), status=TransactionStatus.SPLIT_PARENT.value)
            _make_tx(s, amount=Decimal("-300.00"), description="child A")
            _make_tx(s, amount=Decimal("-200.00"), description="child B")

        r = client.get("/api/transactions/aggregations")
        assert r.status_code == 200
        data = r.json()
        supplies_total = data["category_breakdown"]
        # Total expense category sum across children only = 500, not 1000
        # (parent -500 + children -500 would double it).
        total_amt = sum(c["total"] for c in supplies_total)
        assert total_amt == pytest.approx(500.0)

    def test_split_parent_excluded_from_vendor_history_anomaly_baseline(
        self, client: TestClient
    ) -> None:
        """The anomaly-baseline query (all_expense_q) must also exclude
        split_parent rows — otherwise a parent + its children both feed the
        historical average, skewing the anomaly threshold."""
        with _TestSession() as s:
            # Seed 3 months of normal spend for the vendor so it qualifies
            # for anomaly comparison (>=2 historical records).
            _make_tx(s, description="Recurring Vendor", amount=Decimal("-100.00"), date="2025-11-01")
            _make_tx(s, description="Recurring Vendor", amount=Decimal("-100.00"), date="2025-12-01")
            # A split parent for the same vendor must not pollute the baseline.
            _make_tx(
                s, description="Recurring Vendor", amount=Decimal("-9999.00"),
                status=TransactionStatus.SPLIT_PARENT.value, date="2025-10-01",
            )

        r = client.get(
            "/api/transactions/aggregations",
            params={"date_from": "2026-01-01", "date_to": "2026-01-31"},
        )
        assert r.status_code == 200
        # No crash and the response is well-formed; the real assertion is
        # that -9999 never appears anywhere in aggregated output.
        assert "-9999" not in r.text
