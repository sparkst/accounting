"""Tests for the estimated_tax section of GET /api/tax-summary.

REQ-ID: EST-TAX-001  estimated_tax key present for sparkry and blackline entities.
REQ-ID: EST-TAX-002  estimated_tax is None for personal entity.
REQ-ID: EST-TAX-003  SE tax = net_profit × 92.35% × 15.3% (projected annual).
REQ-ID: EST-TAX-004  Income tax = (projected_annual_net - 50% SE) × 22%.
REQ-ID: EST-TAX-005  quarterly_payment = total_annual / 4.
REQ-ID: EST-TAX-006  Q1–Q4 due dates are correct ISO strings.
REQ-ID: EST-TAX-007  total_paid sums transactions with tax_subcategory containing "estimated".
REQ-ID: EST-TAX-008  Quarter state is "overdue" for past due dates with unpaid balance.
REQ-ID: EST-TAX-009  Quarter state is "paid" when paid >= projected_amount.
REQ-ID: EST-TAX-010  months_elapsed = 12 for a prior year; projected = YTD for past year.
REQ-ID: EST-TAX-011  Empty transaction set returns zero estimates.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import date
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
from src.models.enums import Direction, Entity, Source, TaxCategory, TransactionStatus
from src.models.transaction import Transaction

# ---------------------------------------------------------------------------
# Shared in-memory test database
# ---------------------------------------------------------------------------

_TEST_DB_URI = "file:est_tax_test?mode=memory&cache=shared&uri=true"

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


# ---------------------------------------------------------------------------
# App client fixture — uses FastAPI dependency override for get_db
# ---------------------------------------------------------------------------


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    """TestClient with get_db overridden to use the in-memory test DB."""
    from src.api import main as _main_module

    def _override_get_db() -> Generator[Session, None, None]:
        session = _TestSession()
        try:
            yield session
        finally:
            session.close()

    with (
        patch.object(_main_module, "init_db", return_value=None),
        patch.object(_main_module, "seed_vendor_rules", return_value=0),
    ):
        from src.api.main import app

        app.dependency_overrides[get_db] = _override_get_db
        try:
            with TestClient(app) as c:
                yield c
        finally:
            app.dependency_overrides.pop(get_db, None)


@pytest.fixture()
def db_session() -> Generator[Session, None, None]:
    session = _TestSession()
    try:
        yield session
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_income_tx(
    session: Session,
    *,
    amount: float = 33_000.0,
    entity: str = Entity.SPARKRY.value,
    date_str: str = "2026-01-15",
    tax_subcategory: str | None = None,
) -> Transaction:
    uid = str(uuid.uuid4())
    tx = Transaction(
        id=uid,
        source=Source.BANK_CSV.value,
        source_id=uid,
        source_hash=uid,
        date=date_str,
        description="Cardinal Health Consulting",
        amount=Decimal(str(amount)),
        currency="USD",
        entity=entity,
        direction=Direction.INCOME.value,
        tax_category=TaxCategory.CONSULTING_INCOME.value,
        tax_subcategory=tax_subcategory,
        status=TransactionStatus.CONFIRMED.value,
        confidence=0.99,
        raw_data={},
    )
    session.add(tx)
    session.commit()
    return tx


def _make_expense_tx(
    session: Session,
    *,
    amount: float = -1_000.0,
    entity: str = Entity.SPARKRY.value,
    date_str: str = "2026-01-20",
    tax_category: str = TaxCategory.OFFICE_EXPENSE.value,
    tax_subcategory: str | None = None,
) -> Transaction:
    uid = str(uuid.uuid4())
    tx = Transaction(
        id=uid,
        source=Source.BANK_CSV.value,
        source_id=uid,
        source_hash=uid,
        date=date_str,
        description="Expense",
        amount=Decimal(str(amount)),
        currency="USD",
        entity=entity,
        direction=Direction.EXPENSE.value,
        tax_category=tax_category,
        tax_subcategory=tax_subcategory,
        status=TransactionStatus.CONFIRMED.value,
        confidence=0.99,
        raw_data={},
    )
    session.add(tx)
    session.commit()
    return tx


def _tax_summary(
    client: TestClient, entity: str = "sparkry", year: int = 2026
) -> dict[str, Any]:
    r = client.get(f"/api/tax-summary?entity={entity}&year={year}")
    assert r.status_code == 200, r.text
    return r.json()


def _get_est(
    client: TestClient, entity: str = "sparkry", year: int = 2026
) -> dict[str, Any]:
    return _tax_summary(client, entity, year)["estimated_tax"]


# ---------------------------------------------------------------------------
# EST-TAX-001/002: Presence of estimated_tax field
# ---------------------------------------------------------------------------


class TestEstimatedTaxPresence:
    def test_present_for_sparkry(self, client: TestClient, db_session: Session) -> None:
        """EST-TAX-001: estimated_tax key is in response for sparkry."""
        _make_income_tx(db_session, entity="sparkry", date_str="2026-01-15")
        data = _tax_summary(client, "sparkry", 2026)
        assert "estimated_tax" in data
        assert data["estimated_tax"] is not None

    def test_present_for_blackline(self, client: TestClient, db_session: Session) -> None:
        """EST-TAX-001: estimated_tax key is in response for blackline."""
        _make_income_tx(db_session, entity="blackline", date_str="2026-01-15")
        data = _tax_summary(client, "blackline", 2026)
        assert "estimated_tax" in data
        assert data["estimated_tax"] is not None

    def test_absent_for_personal(self, client: TestClient) -> None:
        """EST-TAX-002: estimated_tax is None for personal entity."""
        data = _tax_summary(client, "personal", 2026)
        assert data.get("estimated_tax") is None


# ---------------------------------------------------------------------------
# EST-TAX-003–005: Tax math — tested directly against _compute_estimated_tax
# ---------------------------------------------------------------------------


class TestEstimatedTaxMath:
    """Verify the SE tax and income tax formulas with a known net profit.

    Given: net_profit = $99,000 over 3 months in 2026 (today = Mar 18, 2026).

    projected_annual_net = 99,000 × 12/3 = $396,000
    SE tax base          = 396,000 × 0.9235 = 365,706
    SE tax annual        = 365,706 × 0.153  = 55,953.018
    SE deduction         = 55,953.018 × 0.50 = 27,976.509
    Income tax base      = 396,000 - 27,976.509 = 368,023.491
    Income tax annual    = 368,023.491 × 0.22 = 80,965.168
    total_annual         = 55,953.018 + 80,965.168 = 136,918.186
    quarterly            = 136,918.186 / 4 = 34,229.547
    """

    def _run(self, net: Decimal, year: int, today: date, txs: list[Transaction] | None = None) -> dict[str, Any]:
        """Call _compute_estimated_tax with a frozen today date."""
        import src.api.routes.tax_export as _te

        txs = txs or []
        with patch("src.api.routes.tax_export.date_type") as mock_date:
            mock_date.today.return_value = today
            return _te._compute_estimated_tax(txs, net, year)

    def test_se_tax_formula(self) -> None:
        """EST-TAX-003: SE tax = projected_net × 92.35% × 15.3%."""
        result = self._run(
            net=Decimal("99000"),
            year=2026,
            today=date(2026, 3, 18),
        )
        assert abs(result["projected_annual_net"] - 396_000.0) < 0.1
        expected_se = round(396_000 * 0.9235 * 0.153, 2)
        assert abs(result["se_tax_annual"] - expected_se) < 0.10

    def test_income_tax_formula(self) -> None:
        """EST-TAX-004: Income tax = (projected - 50% SE) × 22%."""
        result = self._run(
            net=Decimal("99000"),
            year=2026,
            today=date(2026, 3, 18),
        )
        expected_se = 396_000 * 0.9235 * 0.153
        expected_it = round((396_000 - expected_se * 0.5) * 0.22, 2)
        assert abs(result["income_tax_annual"] - expected_it) < 0.10

    def test_quarterly_payment_equals_annual_div_4(self) -> None:
        """EST-TAX-005: quarterly_payment = total_annual / 4."""
        result = self._run(
            net=Decimal("99000"),
            year=2026,
            today=date(2026, 3, 18),
        )
        assert abs(result["quarterly_payment"] - result["total_annual"] / 4) < 0.02

    def test_known_33k_monthly(self) -> None:
        """Full scenario: $33k/month for 3 months, no expenses."""
        # net = 33,000 × 3 = 99,000 YTD (months_elapsed = 3)
        result = self._run(
            net=Decimal("99000"),
            year=2026,
            today=date(2026, 3, 18),
        )
        assert result["months_elapsed"] == 3
        assert abs(result["projected_annual_net"] - 396_000.0) < 0.1

        expected_se = 396_000 * 0.9235 * 0.153
        expected_it = (396_000 - expected_se * 0.5) * 0.22
        assert abs(result["total_annual"] - (expected_se + expected_it)) < 0.20

    def test_zero_net_gives_zero_tax(self) -> None:
        """EST-TAX-011: Zero net profit → zero tax estimates."""
        result = self._run(net=Decimal("0"), year=2026, today=date(2026, 3, 18))
        assert result["se_tax_annual"] == 0.0
        assert result["income_tax_annual"] == 0.0
        assert result["total_annual"] == 0.0
        assert result["quarterly_payment"] == 0.0


# ---------------------------------------------------------------------------
# EST-TAX-006: Due dates
# ---------------------------------------------------------------------------


class TestDueDates:
    def test_q1_through_q4_due_dates(self, client: TestClient, db_session: Session) -> None:
        """EST-TAX-006: Q1–Q4 due dates are correct for the requested year."""
        _make_income_tx(db_session, date_str="2026-01-15")
        est = _get_est(client, year=2026)
        quarters = {q["quarter"]: q["due_date"] for q in est["quarters"]}
        assert quarters["Q1"] == "2026-04-15"
        assert quarters["Q2"] == "2026-06-15"
        assert quarters["Q3"] == "2026-09-15"
        assert quarters["Q4"] == "2027-01-15"

    def test_due_dates_for_different_year(self) -> None:
        """EST-TAX-006: Due dates shift correctly for a different tax year."""
        import src.api.routes.tax_export as _te

        with patch("src.api.routes.tax_export.date_type") as mock_date:
            mock_date.today.return_value = date(2025, 3, 1)
            result = _te._compute_estimated_tax([], Decimal("0"), 2025)
        quarters = {q["quarter"]: q["due_date"] for q in result["quarters"]}
        assert quarters["Q4"] == "2026-01-15"


# ---------------------------------------------------------------------------
# EST-TAX-007: total_paid from estimated-tagged transactions
# ---------------------------------------------------------------------------


class TestTotalPaid:
    def test_sums_estimated_subcategory_payments(
        self, client: TestClient, db_session: Session
    ) -> None:
        """EST-TAX-007: Estimated-tagged expense transactions are summed as paid."""
        _make_income_tx(db_session, amount=33_000.0, date_str="2026-01-15")
        _make_expense_tx(
            db_session,
            amount=-5_000.0,
            date_str="2026-04-15",
            tax_category=TaxCategory.TAXES_AND_LICENSES.value,
            tax_subcategory="estimated_federal",
        )
        _make_expense_tx(
            db_session,
            amount=-3_000.0,
            date_str="2026-04-15",
            tax_category=TaxCategory.TAXES_AND_LICENSES.value,
            tax_subcategory="estimated_se",
        )
        est = _get_est(client, year=2026)
        assert abs(est["total_paid"] - 8_000.0) < 0.01

    def test_non_estimated_subcategory_not_counted(
        self, client: TestClient, db_session: Session
    ) -> None:
        """EST-TAX-007: Only 'estimated' subcategory transactions are counted."""
        _make_income_tx(db_session, amount=33_000.0, date_str="2026-01-15")
        _make_expense_tx(
            db_session,
            amount=-500.0,
            date_str="2026-03-01",
            tax_category=TaxCategory.TAXES_AND_LICENSES.value,
            tax_subcategory="state_tax",
        )
        est = _get_est(client, year=2026)
        assert est["total_paid"] == 0.0

    def test_estimated_tagged_directly(self) -> None:
        """EST-TAX-007: Direct test of _compute_estimated_tax with fake transactions."""
        import src.api.routes.tax_export as _te

        # Minimal fake transaction with only the fields _compute_estimated_tax reads
        class _FakeTx:
            tax_subcategory = "estimated_federal"
            amount = Decimal("7500.00")

        class _FakeTxNo:
            tax_subcategory = "office_supplies"
            amount = Decimal("100.00")

        with patch("src.api.routes.tax_export.date_type") as mock_date:
            mock_date.today.return_value = date(2026, 3, 18)
            result = _te._compute_estimated_tax(
                [_FakeTx(), _FakeTxNo()],  # type: ignore[list-item]
                Decimal("99000"),
                2026,
            )
        assert abs(result["total_paid"] - 7_500.0) < 0.01


# ---------------------------------------------------------------------------
# EST-TAX-008 & EST-TAX-009: Quarter state
# ---------------------------------------------------------------------------


class TestQuarterState:
    def test_overdue_state_for_past_unpaid_quarter(self) -> None:
        """EST-TAX-008: Overdue when due date has passed and nothing paid."""
        import src.api.routes.tax_export as _te

        with patch("src.api.routes.tax_export.date_type") as mock_date:
            mock_date.today.return_value = date(2026, 5, 1)
            result = _te._compute_estimated_tax([], Decimal("99000"), 2026)

        q1 = next(q for q in result["quarters"] if q["quarter"] == "Q1")
        assert q1["state"] == "overdue"

    def test_upcoming_state_for_future_quarter(self) -> None:
        """EST-TAX-008: Upcoming when due date is in the future."""
        import src.api.routes.tax_export as _te

        with patch("src.api.routes.tax_export.date_type") as mock_date:
            mock_date.today.return_value = date(2026, 5, 1)  # before Jun 15
            result = _te._compute_estimated_tax([], Decimal("99000"), 2026)

        q2 = next(q for q in result["quarters"] if q["quarter"] == "Q2")
        assert q2["state"] == "upcoming"

    def test_paid_state_when_sufficient_payment_made(self) -> None:
        """EST-TAX-009: 'paid' state when paid amount covers quarterly projection."""
        import src.api.routes.tax_export as _te

        class _FakeTx:
            tax_subcategory = "estimated_federal"
            amount = Decimal("999999.00")  # wildly overpaid

        with patch("src.api.routes.tax_export.date_type") as mock_date:
            mock_date.today.return_value = date(2026, 5, 1)
            result = _te._compute_estimated_tax([_FakeTx()], Decimal("99000"), 2026)  # type: ignore[list-item]

        for q in result["quarters"]:
            assert q["state"] == "paid", f"{q['quarter']} expected paid, got {q['state']}"
            assert q["remaining"] == 0.0


# ---------------------------------------------------------------------------
# EST-TAX-010: Prior year projection uses months_elapsed=12
# ---------------------------------------------------------------------------


class TestPriorYearProjection:
    def test_prior_year_uses_12_months(self) -> None:
        """EST-TAX-010: months_elapsed=12 and projected==ytd for a prior year."""
        import src.api.routes.tax_export as _te

        with patch("src.api.routes.tax_export.date_type") as mock_date:
            mock_date.today.return_value = date(2026, 3, 18)  # today is 2026
            result = _te._compute_estimated_tax([], Decimal("396000"), 2025)

        assert result["months_elapsed"] == 12
        # For 12 months, projected_annual_net == ytd_net_profit
        assert abs(result["projected_annual_net"] - result["ytd_net_profit"]) < 0.01

    def test_prior_year_via_api(self, client: TestClient, db_session: Session) -> None:
        """EST-TAX-010: API response for prior year shows months_elapsed=12."""
        # 2025 is in the past (today = 2026-03-18 per memory context)
        _make_income_tx(db_session, entity="sparkry", date_str="2025-06-15", amount=33_000.0)
        est = _get_est(client, entity="sparkry", year=2025)
        assert est["months_elapsed"] == 12
        assert abs(est["projected_annual_net"] - est["ytd_net_profit"]) < 0.01


# ---------------------------------------------------------------------------
# EST-TAX-011: Empty transactions → zero estimates (via API)
# ---------------------------------------------------------------------------


class TestEmptyTransactions:
    def test_zero_estimates_with_zero_net_profit(self) -> None:
        """EST-TAX-011: All tax amounts are 0.0 when net profit is zero."""
        import src.api.routes.tax_export as _te

        with patch("src.api.routes.tax_export.date_type") as mock_date:
            mock_date.today.return_value = date(2026, 3, 18)
            result = _te._compute_estimated_tax([], Decimal("0"), 2026)

        assert result["projected_annual_net"] == 0.0
        assert result["se_tax_annual"] == 0.0
        assert result["income_tax_annual"] == 0.0
        assert result["total_annual"] == 0.0
        assert result["quarterly_payment"] == 0.0
        assert result["total_paid"] == 0.0
        for q in result["quarters"]:
            assert q["projected_amount"] == 0.0
            assert q["paid"] == 0.0
            assert q["remaining"] == 0.0

    def test_no_income_transactions_via_api(self, client: TestClient) -> None:
        """EST-TAX-011: With no income, estimated_tax is present but projections are near zero.

        Note: Sparkry always has a home_office_deduction (currently $720), so net
        profit with zero income is negative. The important thing is that total_paid
        is 0.0 and the structure is intact.
        """
        est = _get_est(client, year=2026)
        assert "quarters" in est
        assert len(est["quarters"]) == 4
        assert est["total_paid"] == 0.0
