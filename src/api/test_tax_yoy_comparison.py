"""Tests for year-over-year tax summary comparison.

REQ-ID: YOY-001  compare_year param returns a 'comparison' object with prior_year_items and deltas.
REQ-ID: YOY-002  Deltas correctly reflect current - prior amounts for each category.
REQ-ID: YOY-003  net_profit_delta and net_profit_delta_pct are correct.
REQ-ID: YOY-004  Categories only in current year appear with prior=0.
REQ-ID: YOY-005  Categories only in prior year appear with current=0.
REQ-ID: YOY-006  compare_year == year returns 422.
REQ-ID: YOY-007  compare_year out of valid range returns 422.
REQ-ID: YOY-008  Without compare_year, comparison field is null.
REQ-ID: YOY-009  B&O monthly and quarterly deltas are correct.
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

import src.db.connection as _conn  # noqa: F401 — registers models on Base.metadata
from src.api.deps import get_db
from src.models.base import Base
from src.models.enums import ConfirmedBy, Direction, Entity, Source, TaxCategory, TransactionStatus
from src.models.transaction import Transaction

# ---------------------------------------------------------------------------
# In-memory test database
# ---------------------------------------------------------------------------

_TEST_DB_URI = "file:yoy_test_db?mode=memory&cache=shared&uri=true"

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
    """Truncate all tables before each test."""
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


# ---------------------------------------------------------------------------
# App client
# ---------------------------------------------------------------------------


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    """TestClient with all sessions redirected to the in-memory test DB."""
    from src.api import main as _main_module

    with (
        patch.object(_main_module, "init_db", return_value=None),
        patch.object(_main_module, "seed_vendor_rules", return_value=0),
        patch.object(
            _main_module,
            "seed_customers",
            return_value={"customers_inserted": 0, "customers_updated": 0, "invoices_inserted": 0},
        ),
    ):
        from src.api.main import app

        app.dependency_overrides[get_db] = _override_get_db
        with TestClient(app) as c:
            yield c
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture()
def db_session() -> Generator[Session, None, None]:
    """Direct DB session for test setup."""
    session = _TestSession()
    try:
        yield session
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tx(
    session: Session,
    *,
    entity: str = Entity.SPARKRY.value,
    date: str = "2025-06-15",
    amount: float = -100.0,
    status: str = TransactionStatus.CONFIRMED.value,
    direction: str = Direction.EXPENSE.value,
    tax_category: str = TaxCategory.OFFICE_EXPENSE.value,
    deductible_pct: float = 1.0,
) -> Transaction:
    uid = str(uuid.uuid4())
    tx = Transaction(
        id=uid,
        source=Source.GMAIL_N8N.value,
        source_id=uid,
        source_hash=uid,
        entity=entity,
        date=date,
        description=f"tx-{uid[:8]}",
        amount=Decimal(str(amount)),
        currency="USD",
        status=status,
        direction=direction,
        tax_category=tax_category,
        deductible_pct=Decimal(str(deductible_pct)),
        confidence=0.95,
        confirmed_by=ConfirmedBy.HUMAN.value,
        raw_data={},
    )
    session.add(tx)
    session.commit()
    return tx


def _tax_summary(
    client: TestClient,
    entity: str,
    year: int,
    compare_year: int | None = None,
) -> Any:
    params = f"entity={entity}&year={year}"
    if compare_year is not None:
        params += f"&compare_year={compare_year}"
    return client.get(f"/api/tax-summary?{params}")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_no_compare_year_returns_null_comparison(
    client: TestClient, db_session: Session
) -> None:
    """REQ-ID: YOY-008  Without compare_year, comparison field is null."""
    _make_tx(db_session, date="2025-03-01", amount=-500.0)
    r = _tax_summary(client, "sparkry", 2025)
    assert r.status_code == 200
    data = r.json()
    assert "comparison" in data
    assert data["comparison"] is None


def test_compare_year_same_as_year_returns_422(client: TestClient) -> None:
    """REQ-ID: YOY-006  compare_year == year returns 422."""
    r = _tax_summary(client, "sparkry", 2025, compare_year=2025)
    assert r.status_code == 422
    detail = r.json()["detail"].lower()
    assert "compare_year" in detail or "differ" in detail


def test_compare_year_out_of_range_returns_422(client: TestClient) -> None:
    """REQ-ID: YOY-007  compare_year out of valid range returns 422."""
    r = _tax_summary(client, "sparkry", 2025, compare_year=2019)
    assert r.status_code == 422


def test_comparison_structure(client: TestClient, db_session: Session) -> None:
    """REQ-ID: YOY-001  comparison object has expected keys."""
    _make_tx(db_session, date="2025-03-01", amount=-200.0, tax_category=TaxCategory.OFFICE_EXPENSE.value)
    _make_tx(db_session, date="2024-03-01", amount=-100.0, tax_category=TaxCategory.OFFICE_EXPENSE.value)

    r = _tax_summary(client, "sparkry", 2025, compare_year=2024)
    assert r.status_code == 200
    cmp = r.json()["comparison"]
    assert cmp is not None
    assert cmp["prior_year"] == 2024
    assert "prior_year_items" in cmp
    assert "deltas" in cmp
    assert "net_profit_delta" in cmp
    assert "net_profit_delta_pct" in cmp
    assert "prior_gross_income" in cmp
    assert "prior_total_expenses" in cmp
    assert "prior_net_profit" in cmp
    assert "bno_monthly_deltas" in cmp
    assert "bno_quarterly_deltas" in cmp


def test_delta_values_correct(client: TestClient, db_session: Session) -> None:
    """REQ-ID: YOY-002  Deltas correctly reflect current - prior amounts."""
    # Current year: $300 office expense
    _make_tx(db_session, date="2025-04-01", amount=-300.0, tax_category=TaxCategory.OFFICE_EXPENSE.value)
    # Prior year: $100 office expense
    _make_tx(db_session, date="2024-04-01", amount=-100.0, tax_category=TaxCategory.OFFICE_EXPENSE.value)

    r = _tax_summary(client, "sparkry", 2025, compare_year=2024)
    assert r.status_code == 200
    cmp = r.json()["comparison"]

    office_delta = next(d for d in cmp["deltas"] if d["tax_category"] == "OFFICE_EXPENSE")
    assert office_delta["current"] == pytest.approx(300.0, abs=0.01)
    assert office_delta["prior"] == pytest.approx(100.0, abs=0.01)
    assert office_delta["delta"] == pytest.approx(200.0, abs=0.01)
    # (200/100)*100 = 200%
    assert office_delta["delta_pct"] == pytest.approx(200.0, abs=0.1)


def test_net_profit_delta_correct(client: TestClient, db_session: Session) -> None:
    """REQ-ID: YOY-003  net_profit_delta and net_profit_delta_pct are correct."""
    # Current: $5000 income, $1000 expense
    _make_tx(
        db_session,
        date="2025-01-10",
        amount=5000.0,
        direction=Direction.INCOME.value,
        tax_category=TaxCategory.CONSULTING_INCOME.value,
    )
    _make_tx(db_session, date="2025-02-01", amount=-1000.0, tax_category=TaxCategory.OFFICE_EXPENSE.value)

    # Prior: $3000 income, $500 expense
    _make_tx(
        db_session,
        date="2024-01-10",
        amount=3000.0,
        direction=Direction.INCOME.value,
        tax_category=TaxCategory.CONSULTING_INCOME.value,
    )
    _make_tx(db_session, date="2024-02-01", amount=-500.0, tax_category=TaxCategory.OFFICE_EXPENSE.value)

    r = _tax_summary(client, "sparkry", 2025, compare_year=2024)
    assert r.status_code == 200
    data = r.json()
    cmp = data["comparison"]

    # Verify delta matches current_net - prior_net
    current_net = data["net_profit"]
    prior_net = cmp["prior_net_profit"]
    expected_delta = round(current_net - prior_net, 2)
    assert cmp["net_profit_delta"] == pytest.approx(expected_delta, abs=0.01)

    if prior_net != 0:
        expected_pct = round((expected_delta / abs(prior_net)) * 100.0, 1)
        assert cmp["net_profit_delta_pct"] == pytest.approx(expected_pct, abs=0.1)


def test_category_only_in_current_year(client: TestClient, db_session: Session) -> None:
    """REQ-ID: YOY-004  Category only in current year has prior=0 and delta_pct=None."""
    # Current: travel expense
    _make_tx(db_session, date="2025-05-01", amount=-400.0, tax_category=TaxCategory.TRAVEL.value)
    # Prior: office expense only (no travel)
    _make_tx(db_session, date="2024-05-01", amount=-200.0, tax_category=TaxCategory.OFFICE_EXPENSE.value)

    r = _tax_summary(client, "sparkry", 2025, compare_year=2024)
    assert r.status_code == 200
    cmp = r.json()["comparison"]

    travel_delta = next((d for d in cmp["deltas"] if d["tax_category"] == "TRAVEL"), None)
    assert travel_delta is not None
    assert travel_delta["prior"] == pytest.approx(0.0, abs=0.01)
    assert travel_delta["current"] == pytest.approx(400.0, abs=0.01)
    assert travel_delta["delta"] == pytest.approx(400.0, abs=0.01)
    assert travel_delta["delta_pct"] is None  # prior is 0, can't compute pct


def test_category_only_in_prior_year(client: TestClient, db_session: Session) -> None:
    """REQ-ID: YOY-005  Category only in prior year has current=0."""
    # Current: office expense only
    _make_tx(db_session, date="2025-03-01", amount=-150.0, tax_category=TaxCategory.OFFICE_EXPENSE.value)
    # Prior: also has meals
    _make_tx(db_session, date="2024-03-01", amount=-75.0, tax_category=TaxCategory.MEALS.value)

    r = _tax_summary(client, "sparkry", 2025, compare_year=2024)
    assert r.status_code == 200
    cmp = r.json()["comparison"]

    meals_delta = next((d for d in cmp["deltas"] if d["tax_category"] == "MEALS"), None)
    assert meals_delta is not None
    assert meals_delta["current"] == pytest.approx(0.0, abs=0.01)
    assert meals_delta["prior"] == pytest.approx(75.0, abs=0.01)
    assert meals_delta["delta"] == pytest.approx(-75.0, abs=0.01)
    assert meals_delta["delta_pct"] == pytest.approx(-100.0, abs=0.1)


def test_bno_monthly_deltas(client: TestClient, db_session: Session) -> None:
    """REQ-ID: YOY-009  B&O monthly deltas are correct."""
    # Current year: $2000 consulting in January
    _make_tx(
        db_session,
        date="2025-01-15",
        amount=2000.0,
        direction=Direction.INCOME.value,
        tax_category=TaxCategory.CONSULTING_INCOME.value,
    )
    # Prior year: $1000 consulting in January
    _make_tx(
        db_session,
        date="2024-01-15",
        amount=1000.0,
        direction=Direction.INCOME.value,
        tax_category=TaxCategory.CONSULTING_INCOME.value,
    )

    r = _tax_summary(client, "sparkry", 2025, compare_year=2024)
    assert r.status_code == 200
    cmp = r.json()["comparison"]

    assert len(cmp["bno_monthly_deltas"]) == 12
    jan = next(d for d in cmp["bno_monthly_deltas"] if d["month"].endswith("-01"))
    assert jan["current"] == pytest.approx(2000.0, abs=0.01)
    assert jan["prior"] == pytest.approx(1000.0, abs=0.01)
    assert jan["delta"] == pytest.approx(1000.0, abs=0.01)

    # February should be zero for both years
    feb = next(d for d in cmp["bno_monthly_deltas"] if d["month"].endswith("-02"))
    assert feb["current"] == pytest.approx(0.0, abs=0.01)
    assert feb["prior"] == pytest.approx(0.0, abs=0.01)
    assert feb["delta"] == pytest.approx(0.0, abs=0.01)


def test_bno_quarterly_deltas(client: TestClient, db_session: Session) -> None:
    """REQ-ID: YOY-009  B&O quarterly deltas are correct."""
    # Current: $6000 in Q1 (Jan + Feb + Mar, $2000 each)
    for month in ["01", "02", "03"]:
        _make_tx(
            db_session,
            date=f"2025-{month}-10",
            amount=2000.0,
            direction=Direction.INCOME.value,
            tax_category=TaxCategory.CONSULTING_INCOME.value,
        )
    # Prior: $3000 in Q1 ($1000 each)
    for month in ["01", "02", "03"]:
        _make_tx(
            db_session,
            date=f"2024-{month}-10",
            amount=1000.0,
            direction=Direction.INCOME.value,
            tax_category=TaxCategory.CONSULTING_INCOME.value,
        )

    r = _tax_summary(client, "sparkry", 2025, compare_year=2024)
    assert r.status_code == 200
    cmp = r.json()["comparison"]

    assert len(cmp["bno_quarterly_deltas"]) == 4
    q1 = next(d for d in cmp["bno_quarterly_deltas"] if d["quarter"] == "Q1")
    assert q1["current"] == pytest.approx(6000.0, abs=0.01)
    assert q1["prior"] == pytest.approx(3000.0, abs=0.01)
    assert q1["delta"] == pytest.approx(3000.0, abs=0.01)

    # Q2-Q4 should be zero
    q2 = next(d for d in cmp["bno_quarterly_deltas"] if d["quarter"] == "Q2")
    assert q2["delta"] == pytest.approx(0.0, abs=0.01)


def test_prior_year_items_match_prior_data(client: TestClient, db_session: Session) -> None:
    """prior_year_items reflects the prior year's aggregated line items."""
    _make_tx(db_session, date="2025-06-01", amount=-500.0, tax_category=TaxCategory.SUPPLIES.value)
    _make_tx(db_session, date="2024-06-01", amount=-250.0, tax_category=TaxCategory.SUPPLIES.value)

    r = _tax_summary(client, "sparkry", 2025, compare_year=2024)
    assert r.status_code == 200
    cmp = r.json()["comparison"]

    prior_supplies = next(
        (i for i in cmp["prior_year_items"] if i["tax_category"] == "SUPPLIES"), None
    )
    assert prior_supplies is not None
    assert prior_supplies["total"] == pytest.approx(250.0, abs=0.01)


def test_comparison_with_empty_prior_year(client: TestClient, db_session: Session) -> None:
    """When prior year has no transactions, deltas equal current amounts."""
    _make_tx(db_session, date="2025-07-01", amount=-600.0, tax_category=TaxCategory.TRAVEL.value)
    # No 2024 transactions

    r = _tax_summary(client, "sparkry", 2025, compare_year=2024)
    assert r.status_code == 200
    cmp = r.json()["comparison"]

    assert cmp["prior_year_items"] == []
    travel_delta = next((d for d in cmp["deltas"] if d["tax_category"] == "TRAVEL"), None)
    assert travel_delta is not None
    assert travel_delta["prior"] == pytest.approx(0.0, abs=0.01)
    assert travel_delta["current"] == pytest.approx(600.0, abs=0.01)
    assert travel_delta["delta"] == pytest.approx(600.0, abs=0.01)


def test_delta_pct_none_when_prior_is_zero(client: TestClient, db_session: Session) -> None:
    """delta_pct is None when prior value is 0 (avoids division by zero)."""
    _make_tx(db_session, date="2025-03-01", amount=-100.0, tax_category=TaxCategory.ADVERTISING.value)

    r = _tax_summary(client, "sparkry", 2025, compare_year=2024)
    assert r.status_code == 200
    cmp = r.json()["comparison"]

    ad_delta = next((d for d in cmp["deltas"] if d["tax_category"] == "ADVERTISING"), None)
    assert ad_delta is not None
    assert ad_delta["delta_pct"] is None
