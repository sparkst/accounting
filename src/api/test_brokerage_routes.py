"""Brokerage API route tests.

REQ-005a..g visibility — Option 2.
Reuses the canonical Option 1 fixture from src.reports.test_brokerage_summary.
"""

from __future__ import annotations

from collections.abc import Generator
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# Import all brokerage models so Base.metadata is fully registered for the
# co-located create_all call. Other test modules in the suite may have already
# imported their models — the per-test isolated engine below sidesteps any
# cross-test schema pollution.
from src.models import brokerage as _brokerage_models  # noqa: F401
from src.models import history as _history_models  # noqa: F401
from src.models.base import Base
from src.reports.test_brokerage_summary import TODAY, _seed_fixture


def _make_engine() -> Any:
    """Per-test isolated in-memory SQLite engine. StaticPool keeps the same
    connection across the engine, so the schema persists for the test's
    lifetime without sharing state with other tests."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture()
def engine() -> Generator[Any, None, None]:
    e = _make_engine()
    yield e
    e.dispose()


@pytest.fixture()
def seeded(
    engine: Any, monkeypatch: pytest.MonkeyPatch
) -> Generator[Session, None, None]:
    """Pin _today, seed canonical fixture, yield session."""
    import src.reports.brokerage_summary as report_mod
    monkeypatch.setattr(report_mod, "_today", lambda: TODAY, raising=False)

    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    s = SessionLocal()
    _seed_fixture(s)
    yield s
    s.close()


@pytest.fixture()
def empty(
    engine: Any, monkeypatch: pytest.MonkeyPatch
) -> Generator[Session, None, None]:
    import src.reports.brokerage_summary as report_mod
    monkeypatch.setattr(report_mod, "_today", lambda: TODAY, raising=False)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    s = SessionLocal()
    yield s
    s.close()


def _build_client(engine: Any) -> Generator[TestClient, None, None]:
    """Common TestClient builder used by both seeded and empty fixtures."""
    from src.api import main as _main_module

    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    def _test_get_db() -> Generator[Session, None, None]:
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    with (
        patch.object(_main_module, "init_db", return_value=None),
        patch.object(_main_module, "seed_vendor_rules", return_value=0),
        patch.object(_main_module, "seed_customers", return_value={
            "customers_inserted": 0, "customers_updated": 0, "invoices_inserted": 0,
        }),
    ):
        from src.api.main import app
        from src.api.routes.brokerage import get_db

        app.dependency_overrides[get_db] = _test_get_db
        with TestClient(app) as c:
            yield c
        app.dependency_overrides.clear()


@pytest.fixture()
def client(seeded: Session, engine: Any) -> Generator[TestClient, None, None]:
    yield from _build_client(engine)


@pytest.fixture()
def empty_client(empty: Session, engine: Any) -> Generator[TestClient, None, None]:
    yield from _build_client(engine)


# ── /api/brokerage/networth ────────────────────────────────────────────


class TestNetworth:
    def test_seeded(self, client: TestClient) -> None:
        r = client.get("/api/brokerage/networth")
        assert r.status_code == 200
        body = r.json()
        # Total computed from canonical fixture; matches test_compute_net_worth_total.
        assert body["total"] == pytest.approx(206016.16, rel=1e-9)
        assert "fidelity" in body["by_broker"]
        assert body["zero_snapshot_account_count"] == 1
        assert body["plan_wrapper_excluded_count"] == 1
        assert body["as_of_max"] == TODAY.isoformat()

    def test_empty(self, empty_client: TestClient) -> None:
        r = empty_client.get("/api/brokerage/networth")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 0
        assert body["by_broker"] == {}
        assert body["by_entity"] == {}
        assert body["as_of_min"] is None


# ── /api/brokerage/accounts ────────────────────────────────────────────


class TestAccounts:
    def test_seeded_returns_all(self, client: TestClient) -> None:
        r = client.get("/api/brokerage/accounts")
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) == 10
        masks = {row["account_number_masked"] for row in rows}
        assert "****7759" in masks  # A1 Z23257759

    def test_plan_wrapper_flagged(self, client: TestClient) -> None:
        r = client.get("/api/brokerage/accounts")
        rows = r.json()
        wrapper = next(row for row in rows if row["account_number_masked"] == "****9766")
        assert wrapper["is_plan_wrapper"] is True

    def test_empty(self, empty_client: TestClient) -> None:
        r = empty_client.get("/api/brokerage/accounts")
        assert r.status_code == 200
        assert r.json() == []


# ── /api/brokerage/top-holdings ────────────────────────────────────────


class TestTopHoldings:
    def test_default(self, client: TestClient) -> None:
        r = client.get("/api/brokerage/top-holdings")
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) <= 10
        # No TOTAL row leaks through.
        assert not any(row["symbol"] == "TOTAL" for row in rows)

    def test_cash_sleeve_folded_when_unbounded(self, client: TestClient) -> None:
        # Need n large enough that the small Cash row makes the cut.
        r = client.get("/api/brokerage/top-holdings?n=50")
        rows = r.json()
        cash = [row for row in rows if row.get("is_cash_sleeve")]
        assert len(cash) == 1, f"expected exactly 1 cash row, got {len(cash)}"

    def test_n_query_param(self, client: TestClient) -> None:
        r = client.get("/api/brokerage/top-holdings?n=3")
        assert r.status_code == 200
        assert len(r.json()) == 3

    def test_n_invalid_returns_422(self, client: TestClient) -> None:
        r = client.get("/api/brokerage/top-holdings?n=-1")
        assert r.status_code == 422

    def test_empty(self, empty_client: TestClient) -> None:
        r = empty_client.get("/api/brokerage/top-holdings")
        assert r.status_code == 200
        assert r.json() == []


# ── /api/brokerage/recent-transactions ─────────────────────────────────


class TestRecentTransactions:
    def test_default_window(self, client: TestClient) -> None:
        r = client.get("/api/brokerage/recent-transactions")
        assert r.status_code == 200
        rows = r.json()
        # T1 (AAPL BUY today-1) appears, T5 (today-30) does not.
        symbols = [row["symbol"] for row in rows]
        assert "AAPL" in symbols
        assert "ZZZ" not in symbols

    def test_wider_window(self, client: TestClient) -> None:
        r = client.get("/api/brokerage/recent-transactions?days=60")
        assert r.status_code == 200
        symbols = [row["symbol"] for row in r.json()]
        assert "ZZZ" in symbols

    def test_rejected_excluded(self, client: TestClient) -> None:
        r = client.get("/api/brokerage/recent-transactions")
        rows = r.json()
        # T6 (REJECTED MSFT dividend on today-1) must not appear.
        rej = [
            row for row in rows
            if row["symbol"] == "MSFT" and row["canonical_action"] == "dividend_ordinary"
        ]
        assert rej == []

    def test_reinvest_partner_suppressed(self, client: TestClient) -> None:
        r = client.get("/api/brokerage/recent-transactions")
        rows = r.json()
        assert not any(row["canonical_action"] == "reinvest" for row in rows)


# ── /api/brokerage/realized-gl ─────────────────────────────────────────


class TestRealizedGL:
    def test_buckets(self, client: TestClient) -> None:
        r = client.get("/api/brokerage/realized-gl")
        assert r.status_code == 200
        body = r.json()
        # JSON dict keys are strings — year keys come back as "2024"/"2025".
        years = {int(k): v for k, v in body["by_year"].items()}
        assert 2024 in years
        assert years[2024]["lots"] >= 3

    def test_wash_sales(self, client: TestClient) -> None:
        r = client.get("/api/brokerage/realized-gl")
        body = r.json()
        assert body["wash_sales"]["lots"] == 1
        assert body["wash_sales"]["total_disallowed_loss"] == 50.0

    def test_empty(self, empty_client: TestClient) -> None:
        r = empty_client.get("/api/brokerage/realized-gl")
        assert r.status_code == 200
        body = r.json()
        assert body["by_year"] == {}
        assert body["wash_sales"]["lots"] == 0


# ── /api/brokerage/data-integrity ──────────────────────────────────────


class TestDataIntegrity:
    def test_counts(self, client: TestClient) -> None:
        r = client.get("/api/brokerage/data-integrity")
        assert r.status_code == 200
        body = r.json()
        assert body["accounts"] == 10
        assert body["suspect_symbols"] == 2  # TOTAL + Generated-at
        assert body["duplicate_position_groups"] == 1  # A8 MGK

    def test_stale_days_param(self, client: TestClient) -> None:
        r = client.get("/api/brokerage/data-integrity?stale_days=5")
        assert r.status_code == 200
        # A7 (today-10) still stale at 5d threshold.
        assert r.json()["stale_snapshot_accounts"] == 1


# ── auth ────────────────────────────────────────────────────────────────


class TestAuth:
    """When API_KEY env var is set, all /api/brokerage/* endpoints require X-API-Key."""

    def test_no_api_key_when_required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Set API_KEY env var to enforce auth.
        monkeypatch.setenv("API_KEY", "test-key-12345")
        # Reload auth module so it picks up the new env var.
        import importlib

        import src.api.auth
        importlib.reload(src.api.auth)
        # Re-import app so its dependency uses the reloaded auth.
        import src.api.main
        importlib.reload(src.api.main)

        from src.api.main import app

        with TestClient(app) as c:
            # No X-API-Key header.
            r = c.get("/api/brokerage/networth")
            assert r.status_code in (401, 403)

        # Reset for other tests.
        monkeypatch.delenv("API_KEY", raising=False)
        importlib.reload(src.api.auth)
        importlib.reload(src.api.main)


# ── /api/brokerage/networth-history ─────────────────────────────────────


class TestNetWorthHistory:
    """Phase 3 T6: aggregated balance series from account_balance_snapshot."""

    def test_empty_returns_empty_list(self, empty_client: TestClient) -> None:
        r = empty_client.get("/api/brokerage/networth-history")
        assert r.status_code == 200
        assert r.json() == []

    def test_aggregates_by_date(self, empty_client: TestClient, empty: Session) -> None:
        from datetime import date

        from src.models.brokerage import Account
        from src.models.history import AccountBalanceSnapshot

        # Seed two accounts and three snapshot dates.
        a1 = Account(
            broker="fidelity",
            account_number="X1",
            account_type="taxable",
            entity="personal",
            tax_sheltered=False,
        )
        a2 = Account(
            broker="vanguard",
            account_number="X2",
            account_type="roth_ira",
            entity="personal",
            tax_sheltered=True,
        )
        empty.add_all([a1, a2])
        empty.flush()

        rows = [
            (a1.id, "A1", date(2024, 1, 1), Decimal("100")),
            (a2.id, "A2", date(2024, 1, 1), Decimal("200")),
            (a1.id, "A1", date(2024, 6, 1), Decimal("150")),
            (a2.id, "A2", date(2024, 6, 1), Decimal("250")),
        ]
        for idx, (acct_id, raw_name, as_of, bal) in enumerate(rows):
            empty.add(
                AccountBalanceSnapshot(
                    account_id=acct_id,
                    raw_account_name=raw_name,
                    as_of=as_of,
                    balance=bal,
                    source="xlsx_2024",
                    source_row_hash=f"hash-{idx}",
                )
            )
        empty.commit()

        r = empty_client.get("/api/brokerage/networth-history")
        assert r.status_code == 200
        body = r.json()
        assert len(body) == 2
        assert body[0]["as_of"] == "2024-01-01"
        assert body[0]["balance_total"] == 300.0
        assert body[0]["account_count"] == 2
        assert body[1]["as_of"] == "2024-06-01"
        assert body[1]["balance_total"] == 400.0

    def test_excludes_unmatched_by_default(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        from datetime import date

        from src.models.history import AccountBalanceSnapshot

        empty.add(
            AccountBalanceSnapshot(
                account_id=None,
                raw_account_name="Orphan",
                as_of=date(2024, 1, 1),
                balance=Decimal("999"),
                source="xlsx_2024",
                source_row_hash="orphan-hash",
            )
        )
        empty.commit()

        r = empty_client.get("/api/brokerage/networth-history")
        assert r.json() == []

        r = empty_client.get("/api/brokerage/networth-history?include_unmatched=true")
        body = r.json()
        assert len(body) == 1
        assert body[0]["balance_total"] == 999.0

    def test_excludes_closed_expected_accounts(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        from datetime import date

        from src.models.brokerage import Account
        from src.models.history import AccountBalanceSnapshot, ExpectedAccount

        a = Account(
            broker="fidelity",
            account_number="X1",
            account_type="taxable",
            entity="personal",
            tax_sheltered=False,
        )
        empty.add(a)
        empty.flush()
        empty.add(
            AccountBalanceSnapshot(
                account_id=a.id,
                raw_account_name="A",
                as_of=date(2024, 1, 1),
                balance=Decimal("500"),
                source="xlsx_2024",
                source_row_hash="hashclosed",
            )
        )
        empty.add(
            ExpectedAccount(
                institution="fidelity",
                account_name="A",
                source="manual",
                status="closed",
                resolved_account_id=a.id,
            )
        )
        empty.commit()

        r = empty_client.get("/api/brokerage/networth-history")
        assert r.json() == []  # closed account is filtered out


# ── /api/brokerage/missing-accounts ────────────────────────────────────────


class TestMissingAccounts:
    """Phase 3 T18: active expected_accounts with no/stale coverage."""

    def _make_account(self, session: Session) -> Any:
        from src.models.brokerage import Account

        a = Account(
            broker="vanguard",
            account_number="12345678",
            account_type="taxable",
            entity="personal",
            tax_sheltered=False,
        )
        session.add(a)
        session.flush()
        return a

    def test_empty_db_returns_empty_list(self, empty_client: TestClient) -> None:
        r = empty_client.get("/api/brokerage/missing-accounts")
        assert r.status_code == 200
        assert r.json() == []

    def test_active_with_no_resolved_account_appears(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        from src.models.history import ExpectedAccount

        empty.add(
            ExpectedAccount(
                institution="Vanguard",
                account_name="Travis Roth",
                last_4="9844",
                source="credit_karma",
                status="active",
                resolved_account_id=None,
            )
        )
        empty.commit()

        r = empty_client.get("/api/brokerage/missing-accounts")
        assert r.status_code == 200
        body = r.json()
        assert len(body) == 1
        row = body[0]
        assert row["institution"] == "Vanguard"
        assert row["account_name"] == "Travis Roth"
        assert row["resolved_account_id"] is None
        assert row["last_seen_days_ago"] is None

    def test_active_with_stale_snapshot_appears(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        from datetime import date, timedelta

        from src.models.history import AccountBalanceSnapshot, ExpectedAccount

        acct = self._make_account(empty)
        # Stale: snapshot 90 days old vs default 60-day cutoff.
        empty.add(
            AccountBalanceSnapshot(
                account_id=acct.id,
                raw_account_name="A",
                as_of=date.today() - timedelta(days=90),
                balance=Decimal("100"),
                source="xlsx_2024",
                source_row_hash="stale-h",
            )
        )
        empty.add(
            ExpectedAccount(
                institution="Vanguard",
                account_name="Travis Roth",
                source="credit_karma",
                status="active",
                resolved_account_id=acct.id,
            )
        )
        empty.commit()

        r = empty_client.get("/api/brokerage/missing-accounts")
        assert r.status_code == 200
        body = r.json()
        assert len(body) == 1
        assert body[0]["resolved_account_id"] == acct.id
        assert body[0]["last_seen_days_ago"] == 90

    def test_active_with_fresh_snapshot_excluded(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        from datetime import date, timedelta

        from src.models.history import AccountBalanceSnapshot, ExpectedAccount

        acct = self._make_account(empty)
        empty.add(
            AccountBalanceSnapshot(
                account_id=acct.id,
                raw_account_name="A",
                as_of=date.today() - timedelta(days=10),
                balance=Decimal("100"),
                source="xlsx_2024",
                source_row_hash="fresh-h",
            )
        )
        empty.add(
            ExpectedAccount(
                institution="Vanguard",
                account_name="Travis Roth",
                source="credit_karma",
                status="active",
                resolved_account_id=acct.id,
            )
        )
        empty.commit()

        r = empty_client.get("/api/brokerage/missing-accounts")
        assert r.status_code == 200
        assert r.json() == []

    def test_closed_and_unconfirmed_excluded(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        from src.models.history import ExpectedAccount

        empty.add_all(
            [
                ExpectedAccount(
                    institution="Vanguard",
                    account_name="Closed One",
                    source="manual",
                    status="closed",
                ),
                ExpectedAccount(
                    institution="Vanguard",
                    account_name="Unconfirmed One",
                    source="xlsx",
                    status="unconfirmed",
                ),
            ]
        )
        empty.commit()

        r = empty_client.get("/api/brokerage/missing-accounts")
        assert r.status_code == 200
        assert r.json() == []

    def test_stale_days_param_tunable(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        """A snapshot 30 days old is fresh at 60-day cutoff, stale at 14-day."""
        from datetime import date, timedelta

        from src.models.history import AccountBalanceSnapshot, ExpectedAccount

        acct = self._make_account(empty)
        empty.add(
            AccountBalanceSnapshot(
                account_id=acct.id,
                raw_account_name="A",
                as_of=date.today() - timedelta(days=30),
                balance=Decimal("100"),
                source="xlsx_2024",
                source_row_hash="med-h",
            )
        )
        empty.add(
            ExpectedAccount(
                institution="Vanguard",
                account_name="A",
                source="credit_karma",
                status="active",
                resolved_account_id=acct.id,
            )
        )
        empty.commit()

        # Default cutoff (60 days) — fresh.
        r = empty_client.get("/api/brokerage/missing-accounts")
        assert r.json() == []

        # Tighter cutoff (14 days) — stale.
        r = empty_client.get("/api/brokerage/missing-accounts?stale_days=14")
        body = r.json()
        assert len(body) == 1
        assert body[0]["last_seen_days_ago"] == 30


# ── /api/brokerage/networth-history-benchmark (T12) ────────────────────


class TestBenchmarkComparison:
    """Phase 3 T12: portfolio history vs buy-and-hold benchmark simulation."""

    def test_empty_returns_empty_series(self, empty_client: TestClient) -> None:
        r = empty_client.get("/api/brokerage/networth-history-benchmark?benchmark=SPY")
        assert r.status_code == 200
        body = r.json()
        assert body["benchmark_symbol"] == "SPY"
        assert body["series"] == []
        assert body["portfolio_pct"] is None
        assert body["benchmark_pct"] is None

    def test_buy_and_hold_simulation(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        from datetime import date

        from src.models.brokerage import Account
        from src.models.history import AccountBalanceSnapshot, HistoricalPrice

        a = Account(
            broker="fidelity",
            account_number="X1",
            account_type="taxable",
            entity="personal",
            tax_sheltered=False,
        )
        empty.add(a)
        empty.flush()
        # Portfolio doubles from 100k to 200k.
        empty.add(
            AccountBalanceSnapshot(
                account_id=a.id,
                raw_account_name="A",
                as_of=date(2024, 1, 1),
                balance=Decimal("100000"),
                source="xlsx_2024",
                source_row_hash="h1",
            )
        )
        empty.add(
            AccountBalanceSnapshot(
                account_id=a.id,
                raw_account_name="A",
                as_of=date(2024, 12, 31),
                balance=Decimal("200000"),
                source="xlsx_2024",
                source_row_hash="h2",
            )
        )
        # SPY at $100 → $130 (30% return).
        empty.add(HistoricalPrice(symbol="SPY", trade_date=date(2024, 1, 1), close=Decimal("100")))
        empty.add(HistoricalPrice(symbol="SPY", trade_date=date(2024, 12, 31), close=Decimal("130")))
        empty.commit()

        r = empty_client.get("/api/brokerage/networth-history-benchmark?benchmark=SPY")
        assert r.status_code == 200
        body = r.json()
        assert len(body["series"]) == 2
        # First point: portfolio = 100k, benchmark = 100k (anchor).
        assert body["series"][0]["portfolio_value"] == 100000.0
        assert body["series"][0]["benchmark_value"] == 100000.0
        # Second point: portfolio = 200k, benchmark = 100k * 130/100 = 130k.
        assert body["series"][1]["portfolio_value"] == 200000.0
        assert body["series"][1]["benchmark_value"] == 130000.0
        assert body["portfolio_pct"] == pytest.approx(1.0)  # +100%
        assert body["benchmark_pct"] == pytest.approx(0.30)  # +30%

    def test_missing_benchmark_prices_returns_null_benchmark(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        from datetime import date

        from src.models.brokerage import Account
        from src.models.history import AccountBalanceSnapshot

        a = Account(
            broker="fidelity",
            account_number="X1",
            account_type="taxable",
            entity="personal",
            tax_sheltered=False,
        )
        empty.add(a)
        empty.flush()
        empty.add(
            AccountBalanceSnapshot(
                account_id=a.id,
                raw_account_name="A",
                as_of=date(2024, 1, 1),
                balance=Decimal("100"),
                source="xlsx_2024",
                source_row_hash="h",
            )
        )
        empty.commit()

        # No HistoricalPrice rows for SPY at all.
        r = empty_client.get("/api/brokerage/networth-history-benchmark?benchmark=SPY")
        assert r.status_code == 200
        body = r.json()
        assert body["series"][0]["benchmark_value"] is None
        assert body["benchmark_pct"] is None


# ── /api/brokerage/holdings/{symbol}/history (T14) ─────────────────────


class TestHoldingHistory:
    """Phase 3 T14: per-symbol value series + lot-level cost basis."""

    def test_unknown_symbol_returns_zeros(self, empty_client: TestClient) -> None:
        r = empty_client.get("/api/brokerage/holdings/NOPE/history")
        assert r.status_code == 200
        body = r.json()
        assert body["symbol"] == "NOPE"
        assert body["current_value"] == 0
        assert body["value_series"] == []
        assert body["lots"] == []

    def test_aggregates_position_snapshots_across_accounts(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        from datetime import datetime

        from src.models.brokerage import Account, PositionSnapshot

        a1 = Account(
            broker="fidelity",
            account_number="X1",
            account_type="taxable",
            entity="personal",
            tax_sheltered=False,
        )
        a2 = Account(
            broker="vanguard",
            account_number="X2",
            account_type="roth_ira",
            entity="personal",
            tax_sheltered=True,
        )
        empty.add_all([a1, a2])
        empty.flush()
        # Two accounts hold AMZN on the same date.
        empty.add(
            PositionSnapshot(
                account_id=a1.id,
                as_of=datetime(2024, 6, 1),
                symbol="AMZN",
                description="Amazon.com Inc",
                quantity=Decimal("10"),
                price=Decimal("180"),
                market_value=Decimal("1800"),
                cost_basis=Decimal("1500"),
                source_file="t1",
                source_row_hash="h1",
                raw_data={},
            )
        )
        empty.add(
            PositionSnapshot(
                account_id=a2.id,
                as_of=datetime(2024, 6, 1),
                symbol="AMZN",
                quantity=Decimal("5"),
                price=Decimal("180"),
                market_value=Decimal("900"),
                cost_basis=Decimal("700"),
                source_file="t2",
                source_row_hash="h2",
                raw_data={},
            )
        )
        empty.commit()

        r = empty_client.get("/api/brokerage/holdings/AMZN/history")
        assert r.status_code == 200
        body = r.json()
        assert body["symbol"] == "AMZN"
        assert body["security_name"] == "Amazon.com Inc"
        assert body["current_value"] == 2700  # 1800 + 900
        assert body["current_quantity"] == 15  # 10 + 5
        assert body["cost_basis"] == 2200  # 1500 + 700
        assert body["unrealized_gain"] == 500  # 2700 - 2200
        assert body["unrealized_pct"] == pytest.approx(500 / 2200, rel=1e-6)
        assert len(body["value_series"]) == 1
        assert body["value_series"][0]["market_value"] == 2700

    def test_returns_lots_for_symbol(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        from datetime import date

        from src.models.history import CostBasisLot

        empty.add(
            CostBasisLot(
                raw_account_name="TD Ameritrade",
                symbol="AMZN",
                open_date=date(2009, 4, 7),
                quantity=Decimal("8.4108"),
                cost_per_share=Decimal("54.9139"),
                cost_total=Decimal("461.87"),
                source="xlsx_td_gainloss",
                source_row_hash="lot-h1",
            )
        )
        empty.commit()

        r = empty_client.get("/api/brokerage/holdings/AMZN/history")
        body = r.json()
        assert len(body["lots"]) == 1
        assert body["lots"][0]["open_date"] == "2009-04-07"
        assert body["lots"][0]["raw_account_name"] == "TD Ameritrade"
        assert body["lots"][0]["cost_total"] == 461.87

    def test_case_insensitive_symbol_lookup(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        from datetime import date

        from src.models.history import CostBasisLot

        empty.add(
            CostBasisLot(
                raw_account_name="TD",
                symbol="amzn",
                open_date=date(2010, 1, 1),
                quantity=Decimal("1"),
                cost_per_share=Decimal("100"),
                cost_total=Decimal("100"),
                source="xlsx_td_gainloss",
                source_row_hash="lc-h",
            )
        )
        empty.commit()

        r = empty_client.get("/api/brokerage/holdings/AMZN/history")
        body = r.json()
        assert len(body["lots"]) == 1
