"""Brokerage API route tests.

REQ-005a..g visibility — Option 2.
Reuses the canonical Option 1 fixture from src.reports.test_brokerage_summary.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime, timedelta
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
    """Pipeline 004: forward-fill + live re-pricing series."""

    @pytest.fixture(autouse=True)
    def _pin_today(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pin the route's clock so series boundaries are deterministic."""
        from src.api.routes import brokerage as routes_mod

        monkeypatch.setattr(
            routes_mod, "_today", lambda: date(2024, 7, 1), raising=False
        )

    def test_empty_returns_empty_list(self, empty_client: TestClient) -> None:
        r = empty_client.get("/api/brokerage/networth-history")
        assert r.status_code == 200
        assert r.json() == []

    def test_aggregates_with_forward_fill(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        from datetime import date

        from src.models.brokerage import Account
        from src.models.history import AccountBalanceSnapshot

        # Seed two accounts and two snapshot dates each.
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
        # Weekly Saturdays from 2024-01-06 (first Sat after 2024-01-01) to
        # 2024-06-29 plus today (2024-07-01) = 26 Saturdays + today = 27.
        assert len(body) >= 26
        # Earliest sampled point is 2024-01-06 (first Saturday after 2024-01-01).
        assert body[0]["as_of"] == "2024-01-06"
        # It uses the Jan 1 snapshots forward-filled → $300.
        assert body[0]["balance_total"] == 300.0
        assert body[0]["account_count"] == 2
        # Last point is today, with the latest forward-filled values → $400.
        assert body[-1]["as_of"] == "2024-07-01"
        assert body[-1]["balance_total"] == 400.0
        # Account count is forward-filled too.
        assert body[-1]["account_count"] == 2

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

        # Default — unmatched rows ignored, no real accounts → empty series.
        r = empty_client.get("/api/brokerage/networth-history")
        assert r.json() == []

        # include_unmatched=true — orphan contributes via forward-fill.
        r = empty_client.get("/api/brokerage/networth-history?include_unmatched=true")
        body = r.json()
        assert len(body) >= 1
        # All points carry the same forward-filled $999 from the single Orphan.
        assert body[0]["balance_total"] == 999.0
        assert body[-1]["balance_total"] == 999.0

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

        # Closed account is filtered out at every target date — every point
        # has balance_total=0 / account_count=0.
        r = empty_client.get("/api/brokerage/networth-history")
        body = r.json()
        assert len(body) >= 1, "Series must be non-empty when a closed account has a snapshot"
        assert all(p["balance_total"] == 0.0 for p in body)
        assert all(p["account_count"] == 0 for p in body)


# ── /api/brokerage/missing-accounts ────────────────────────────────────────


class TestMissingAccounts:
    """active expected_accounts with no/stale coverage."""

    @pytest.fixture(autouse=True)
    def _pin_today(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pin the route's clock to a fixed date so day-delta math is deterministic."""
        from src.api.routes import brokerage as routes_mod

        monkeypatch.setattr(
            routes_mod, "_today", lambda: date(2026, 1, 1), raising=False
        )

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
        from datetime import date

        from src.models.history import AccountBalanceSnapshot, ExpectedAccount

        acct = self._make_account(empty)
        # Stale: snapshot 90 days old vs default 60-day cutoff.
        empty.add(
            AccountBalanceSnapshot(
                account_id=acct.id,
                raw_account_name="A",
                as_of=date(2026, 1, 1) - timedelta(days=90),
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
        from datetime import date

        from src.models.history import AccountBalanceSnapshot, ExpectedAccount

        acct = self._make_account(empty)
        empty.add(
            AccountBalanceSnapshot(
                account_id=acct.id,
                raw_account_name="A",
                as_of=date(2026, 1, 1) - timedelta(days=10),
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
        from datetime import date

        from src.models.history import AccountBalanceSnapshot, ExpectedAccount

        acct = self._make_account(empty)
        empty.add(
            AccountBalanceSnapshot(
                account_id=acct.id,
                raw_account_name="A",
                as_of=date(2026, 1, 1) - timedelta(days=30),
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

    def test_position_snapshot_counts_as_fresh_evidence(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        """An account with a fresh PositionSnapshot should NOT be flagged as
        missing even if no AccountBalanceSnapshot exists for it (matches the
        live setup where XLSX names aren't yet matched to live accounts but
        live brokerage ingest is producing PositionSnapshot rows)."""
        from src.models.brokerage import PositionSnapshot
        from src.models.history import ExpectedAccount

        acct = self._make_account(empty)
        # PositionSnapshot 5 days old — fresh.
        empty.add(
            PositionSnapshot(
                account_id=acct.id,
                as_of=datetime(2025, 12, 27),  # PositionSnapshot.as_of is DateTime
                symbol="AAPL",
                quantity=Decimal("1"),
                price=Decimal("100"),
                source_file="seed.csv",
                source_row_hash="ps-h",
                raw_data={},
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

        # Pin _today() to give the PositionSnapshot a 5-day age (well within
        # the 60-day default cutoff).
        import datetime as _dt

        from src.api.routes import brokerage as routes_mod

        original_today = routes_mod._today
        routes_mod._today = lambda: _dt.date(2026, 1, 1)
        try:
            r = empty_client.get("/api/brokerage/missing-accounts")
            assert r.status_code == 200
            assert r.json() == []  # NOT missing — PositionSnapshot is fresh
        finally:
            routes_mod._today = original_today


# ── /api/brokerage/networth-history-benchmark (T12) ────────────────────


class TestBenchmarkComparison:
    """portfolio history vs buy-and-hold benchmark simulation."""

    @pytest.fixture(autouse=True)
    def _pin_today(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pin the route's clock so series boundaries are deterministic."""
        from src.api.routes import brokerage as routes_mod
        monkeypatch.setattr(
            routes_mod, "_today", lambda: date(2024, 1, 1), raising=False
        )

    def test_disallowed_benchmark_returns_400(self, empty_client: TestClient) -> None:
        r = empty_client.get("/api/brokerage/networth-history-benchmark?benchmark=GME")
        assert r.status_code == 400
        assert "GME" not in r.json()["detail"]  # detail lists allowed, not the rejection
        assert "SPY" in r.json()["detail"]

    def test_empty_returns_empty_series(self, empty_client: TestClient) -> None:
        r = empty_client.get("/api/brokerage/networth-history-benchmark?benchmark=SPY")
        assert r.status_code == 200
        body = r.json()
        assert body["benchmark_symbol"] == "SPY"
        assert body["series"] == []
        assert body["portfolio_pct"] is None
        assert body["benchmark_pct"] is None

    def test_buy_and_hold_simulation(
        self, empty_client: TestClient, empty: Session,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """FIX-4: end uses _per_account_value_at forward-fill; granularity=daily
        so we can check specific date values without coupling to weekly Saturday
        alignment."""
        from src.api.routes import brokerage as routes_mod
        # Pin today to 2024-12-31 so both ABS dates are in the series.
        monkeypatch.setattr(routes_mod, "_today", lambda: date(2024, 12, 31), raising=False)

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

        # Use daily so we can assert on the first and last day directly.
        r = empty_client.get(
            "/api/brokerage/networth-history-benchmark?benchmark=SPY&granularity=daily"
        )
        assert r.status_code == 200
        body = r.json()
        # 366 daily points (2024 is a leap year) from Jan 1 to Dec 31.
        assert len(body["series"]) == 366
        # First point: portfolio = 100k, benchmark anchored = 100k.
        first = body["series"][0]
        assert first["as_of"] == "2024-01-01"
        assert first["portfolio_value"] == pytest.approx(100000.0)
        assert first["benchmark_value"] == pytest.approx(100000.0)
        # Last point: portfolio forward-fills to 200k; SPY at $130 → benchmark 130k.
        last = body["series"][-1]
        assert last["as_of"] == "2024-12-31"
        assert last["portfolio_value"] == pytest.approx(200000.0)
        assert last["benchmark_value"] == pytest.approx(130000.0)
        assert body["portfolio_pct"] == pytest.approx(1.0)   # +100%
        assert body["benchmark_pct"] == pytest.approx(0.30)  # +30%

    def test_missing_benchmark_prices_returns_null_benchmark(
        self, empty_client: TestClient, empty: Session
    ) -> None:
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
        # First point (today = 2024-01-01, only 1 point) has null benchmark.
        assert body["series"][0]["benchmark_value"] is None
        assert body["benchmark_pct"] is None

    def test_single_snapshot_is_today_returns_one_point_with_zero_pct(
        self, empty_client: TestClient, empty: Session,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Single snapshot on today → 1-point series. Forward-fill: start==end→0%."""
        from src.api.routes import brokerage as routes_mod
        # Today IS the snapshot date.
        monkeypatch.setattr(routes_mod, "_today", lambda: date(2024, 1, 1), raising=False)

        from src.models.brokerage import Account
        from src.models.history import AccountBalanceSnapshot, HistoricalPrice

        a = Account(
            broker="fidelity", account_number="X1", account_type="taxable",
            entity="personal", tax_sheltered=False,
        )
        empty.add(a)
        empty.flush()
        empty.add(
            AccountBalanceSnapshot(
                account_id=a.id, raw_account_name="A",
                as_of=date(2024, 1, 1), balance=Decimal("1000"),
                source="xlsx_2024", source_row_hash="h",
            )
        )
        empty.add(
            HistoricalPrice(symbol="SPY", trade_date=date(2024, 1, 1), close=Decimal("100"))
        )
        empty.commit()

        r = empty_client.get("/api/brokerage/networth-history-benchmark?benchmark=SPY")
        body = r.json()
        assert len(body["series"]) == 1
        assert body["series"][0]["as_of"] == "2024-01-01"
        assert body["portfolio_pct"] == 0.0
        assert body["benchmark_pct"] == 0.0

    def test_negative_return_drawdown(
        self, empty_client: TestClient, empty: Session,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from src.api.routes import brokerage as routes_mod
        monkeypatch.setattr(routes_mod, "_today", lambda: date(2024, 12, 31), raising=False)

        from src.models.brokerage import Account
        from src.models.history import AccountBalanceSnapshot, HistoricalPrice

        a = Account(
            broker="fidelity", account_number="X1", account_type="taxable",
            entity="personal", tax_sheltered=False,
        )
        empty.add(a)
        empty.flush()
        # Portfolio drops 100k → 80k. SPY drops 100 → 90.
        empty.add(
            AccountBalanceSnapshot(
                account_id=a.id, raw_account_name="A",
                as_of=date(2024, 1, 1), balance=Decimal("100000"),
                source="xlsx_2024", source_row_hash="d1",
            )
        )
        empty.add(
            AccountBalanceSnapshot(
                account_id=a.id, raw_account_name="A",
                as_of=date(2024, 12, 31), balance=Decimal("80000"),
                source="xlsx_2024", source_row_hash="d2",
            )
        )
        empty.add(HistoricalPrice(symbol="SPY", trade_date=date(2024, 1, 1), close=Decimal("100")))
        empty.add(HistoricalPrice(symbol="SPY", trade_date=date(2024, 12, 31), close=Decimal("90")))
        empty.commit()

        r = empty_client.get(
            "/api/brokerage/networth-history-benchmark?benchmark=SPY&granularity=daily"
        )
        body = r.json()
        assert body["portfolio_pct"] == pytest.approx(-0.20)
        assert body["benchmark_pct"] == pytest.approx(-0.10)

    def test_benchmark_forward_fill_regression(
        self, empty_client: TestClient, empty: Session,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """FIX-4 regression: a PositionSnapshot must be re-priced via
        HistoricalPrice at intermediate target dates, confirming _per_account_value_at
        is used (not a raw ABS sum)."""
        from src.api.routes import brokerage as routes_mod
        monkeypatch.setattr(routes_mod, "_today", lambda: date(2024, 6, 1), raising=False)

        from src.models.brokerage import Account, PositionSnapshot
        from src.models.history import HistoricalPrice

        a = Account(
            broker="schwab", account_number="FF1", account_type="taxable",
            entity="personal", tax_sheltered=False,
        )
        empty.add(a)
        empty.flush()
        # 100 shares of VTI, stored mv $20k at 2024-01-01.
        empty.add(PositionSnapshot(
            account_id=a.id,
            as_of=datetime(2024, 1, 1, 12, 0, 0),
            symbol="VTI", description="VTI",
            quantity=Decimal("100"), market_value=Decimal("20000"),
            source_file="t.csv", source_row_hash="ff-rp", raw_data={},
        ))
        # Historical price on 2024-06-01: VTI = $300 → re-priced mv = $30k.
        empty.add(HistoricalPrice(
            symbol="VTI", trade_date=date(2024, 6, 1), close=Decimal("300"), source="test"
        ))
        # Benchmark prices.
        empty.add(HistoricalPrice(symbol="SPY", trade_date=date(2024, 1, 6), close=Decimal("400")))
        empty.add(HistoricalPrice(symbol="SPY", trade_date=date(2024, 6, 1), close=Decimal("440")))
        empty.commit()

        r = empty_client.get("/api/brokerage/networth-history-benchmark?benchmark=SPY")
        assert r.status_code == 200
        body = r.json()
        # First weekly point (2024-01-06): stored mv = $20k, SPY anchor = $400.
        # initial_portfolio = $20k, shares = 20000/400 = 50.
        first = body["series"][0]
        assert first["portfolio_value"] == pytest.approx(20000.0)
        assert first["benchmark_value"] == pytest.approx(20000.0)  # 50 × $400
        # Last point (2024-06-01, today): re-priced mv = $30k.
        # SPY at 440 → benchmark = 50 × $440 = $22k.
        last = body["series"][-1]
        assert last["as_of"] == "2024-06-01"
        assert last["portfolio_value"] == pytest.approx(30000.0)
        assert last["benchmark_value"] == pytest.approx(22000.0)  # 50 × $440


# ── /api/brokerage/holdings/{symbol}/history (T14) ─────────────────────


class TestHoldingHistory:
    """per-symbol value series + lot-level cost basis."""

    def test_invalid_symbol_charset_returns_422(self, empty_client: TestClient) -> None:
        # Path constraint pattern rejects symbols with disallowed chars.
        r = empty_client.get("/api/brokerage/holdings/A;DROP/history")
        assert r.status_code == 422

    def test_overlong_symbol_returns_422(self, empty_client: TestClient) -> None:
        r = empty_client.get(f"/api/brokerage/holdings/{'A' * 17}/history")
        assert r.status_code == 422

    def test_dotted_symbol_accepted(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        # BRK.B is a real ticker with a period — must pass the pattern.
        from datetime import date as _d

        from src.models.history import CostBasisLot

        empty.add(
            CostBasisLot(
                raw_account_name="X",
                symbol="BRK.B",
                open_date=_d(2020, 1, 1),
                quantity=Decimal("1"),
                cost_per_share=Decimal("250"),
                cost_total=Decimal("250"),
                source="xlsx_td_gainloss",
                source_row_hash="brkb-h",
            )
        )
        empty.commit()
        r = empty_client.get("/api/brokerage/holdings/BRK.B/history")
        assert r.status_code == 200

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


# ── /api/brokerage/accounts/{id}/tags (PUT) ────────────────────────────


def _make_acct(session: Session, *, account_number: str = "X1") -> Any:
    from src.models.brokerage import Account

    a = Account(
        broker="fidelity",
        account_number=account_number,
        account_type="taxable",
        entity="personal",
        tax_sheltered=False,
    )
    session.add(a)
    session.flush()
    return a


class TestAccountTagsEdit:
    def test_put_replaces_existing_tags_wholesale(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        from src.models.history import AccountTag

        a = _make_acct(empty)
        # Pre-existing tags that should be wiped on PUT.
        empty.add_all([
            AccountTag(account_id=a.id, tag="legacy"),
            AccountTag(account_id=a.id, tag="old"),
        ])
        empty.commit()

        r = empty_client.put(
            f"/api/brokerage/accounts/{a.id}/tags",
            json={"tags": ["retirement", "rsu"]},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["account_id"] == a.id
        assert sorted(body["tags"]) == ["retirement", "rsu"]

        # Verify DB state.
        empty.expire_all()
        rows = empty.query(AccountTag).filter(AccountTag.account_id == a.id).all()
        assert sorted(t.tag for t in rows) == ["retirement", "rsu"]

    def test_put_empty_list_clears_tags(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        from src.models.history import AccountTag

        a = _make_acct(empty)
        empty.add(AccountTag(account_id=a.id, tag="rsu"))
        empty.commit()

        r = empty_client.put(
            f"/api/brokerage/accounts/{a.id}/tags",
            json={"tags": []},
        )
        assert r.status_code == 200
        assert r.json()["tags"] == []

        empty.expire_all()
        rows = empty.query(AccountTag).filter(AccountTag.account_id == a.id).all()
        assert rows == []

    def test_put_lowercases_input(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        # Validator lowercases (whitespace stripped) — uppercase rejected if it
        # contains a disallowed character class but lower() of "RETIREMENT"
        # passes the pattern. The validator accepts "RETIREMENT" by lowercasing.
        a = _make_acct(empty)
        r = empty_client.put(
            f"/api/brokerage/accounts/{a.id}/tags",
            json={"tags": ["  Retirement  "]},
        )
        assert r.status_code == 200
        assert r.json()["tags"] == ["retirement"]

    def test_put_rejects_bad_pattern(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        a = _make_acct(empty)
        # Special chars not allowed.
        r = empty_client.put(
            f"/api/brokerage/accounts/{a.id}/tags",
            json={"tags": ["bad tag!"]},
        )
        assert r.status_code == 422

        # Empty string fails the {1,32} length.
        r = empty_client.put(
            f"/api/brokerage/accounts/{a.id}/tags",
            json={"tags": [""]},
        )
        assert r.status_code == 422

    def test_put_tag_length_boundary(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        """Pattern is {1,32} so 32 chars OK, 33 not."""
        a = _make_acct(empty)
        empty.commit()

        ok = "a" * 32
        too_long = "a" * 33

        r = empty_client.put(
            f"/api/brokerage/accounts/{a.id}/tags",
            json={"tags": [ok]},
        )
        assert r.status_code == 200
        assert r.json()["tags"] == [ok]

        r = empty_client.put(
            f"/api/brokerage/accounts/{a.id}/tags",
            json={"tags": [too_long]},
        )
        assert r.status_code == 422

    def test_put_missing_account_returns_404(
        self, empty_client: TestClient
    ) -> None:
        r = empty_client.put(
            "/api/brokerage/accounts/no-such-id/tags",
            json={"tags": ["retirement"]},
        )
        assert r.status_code == 404

    def test_round_trip_get_put_get(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        a = _make_acct(empty)
        empty.commit()

        # Initially: no tags.
        r = empty_client.get("/api/brokerage/accounts")
        rows = r.json()
        row = next(r for r in rows if r["account_id"] == a.id)
        assert row["tags"] == []

        # PUT new set.
        r = empty_client.put(
            f"/api/brokerage/accounts/{a.id}/tags",
            json={"tags": ["taxable", "personal"]},
        )
        assert r.status_code == 200

        # GET reflects the new set.
        r = empty_client.get("/api/brokerage/accounts")
        rows = r.json()
        row = next(r for r in rows if r["account_id"] == a.id)
        assert sorted(row["tags"]) == ["personal", "taxable"]


class TestAccountsTagsInResponse:
    def test_accounts_includes_tags_sorted(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        from src.models.history import AccountTag

        a = _make_acct(empty)
        empty.add_all([
            AccountTag(account_id=a.id, tag="rsu"),
            AccountTag(account_id=a.id, tag="brokerage"),
            AccountTag(account_id=a.id, tag="taxable"),
        ])
        empty.commit()

        r = empty_client.get("/api/brokerage/accounts")
        assert r.status_code == 200
        rows = r.json()
        row = next(r for r in rows if r["account_id"] == a.id)
        # Sorted lower-case.
        assert row["tags"] == ["brokerage", "rsu", "taxable"]

    def test_account_with_zero_tags_returns_empty_list(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        a = _make_acct(empty)
        empty.commit()

        r = empty_client.get("/api/brokerage/accounts")
        rows = r.json()
        row = next(r for r in rows if r["account_id"] == a.id)
        assert row["tags"] == []


class TestHistoryTagFilter:
    """Tag/account-id filters on /networth-history (and benchmark)."""

    @pytest.fixture(autouse=True)
    def _pin_today(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.api.routes import brokerage as routes_mod

        monkeypatch.setattr(
            routes_mod, "_today", lambda: date(2024, 7, 1), raising=False
        )

    def _seed_two_accounts_with_snapshots(
        self, session: Session
    ) -> tuple[str, str]:
        """Seed two accounts each with one snapshot. Returns (id_a, id_b)."""
        from src.models.brokerage import Account
        from src.models.history import AccountBalanceSnapshot

        a = Account(
            broker="fidelity", account_number="A1", account_type="taxable",
            entity="personal", tax_sheltered=False,
        )
        b = Account(
            broker="fidelity", account_number="B1", account_type="roth_ira",
            entity="personal", tax_sheltered=True,
        )
        session.add_all([a, b])
        session.flush()
        session.add_all([
            AccountBalanceSnapshot(
                account_id=a.id, raw_account_name="A1",
                as_of=date(2024, 1, 1), balance=Decimal("100"),
                source="xlsx", source_row_hash="ha",
            ),
            AccountBalanceSnapshot(
                account_id=b.id, raw_account_name="B1",
                as_of=date(2024, 1, 1), balance=Decimal("250"),
                source="xlsx", source_row_hash="hb",
            ),
        ])
        return a.id, b.id

    def _seed_three_accounts_with_snapshots(
        self, session: Session
    ) -> tuple[Any, Any, Any]:
        """Seed three accounts each with one snapshot. Returns (a, b, c) ORM objects."""
        from src.models.brokerage import Account
        from src.models.history import AccountBalanceSnapshot

        a = Account(
            broker="fidelity", account_number="A1", account_type="taxable",
            entity="personal", tax_sheltered=False,
        )
        b = Account(
            broker="vanguard", account_number="B1", account_type="taxable",
            entity="personal", tax_sheltered=False,
        )
        c = Account(
            broker="schwab", account_number="C1", account_type="taxable",
            entity="personal", tax_sheltered=False,
        )
        session.add_all([a, b, c])
        session.flush()
        for acct, hash_ in [(a, "ha"), (b, "hb"), (c, "hc")]:
            session.add(
                AccountBalanceSnapshot(
                    account_id=acct.id, raw_account_name=acct.account_number,
                    as_of=date(2024, 1, 1), balance=Decimal("100"),
                    source="xlsx", source_row_hash=hash_,
                )
            )
        return a, b, c

    def test_tags_include_single_tag(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        from src.models.history import AccountTag

        a_id, b_id = self._seed_two_accounts_with_snapshots(empty)
        empty.add(AccountTag(account_id=a_id, tag="retirement"))
        empty.commit()

        r = empty_client.get(
            "/api/brokerage/networth-history?tags_include=retirement"
        )
        assert r.status_code == 200
        body = r.json()
        # Only a's snapshot ($100) survives the filter — forward-filled at
        # every sampled point (weekly + today).
        assert len(body) >= 1
        assert all(p["balance_total"] == 100.0 for p in body)
        assert all(p["account_count"] == 1 for p in body)

    def test_tags_include_and_semantics(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        from src.models.history import AccountTag

        a_id, b_id = self._seed_two_accounts_with_snapshots(empty)
        # a has BOTH retirement and rsu; b only has retirement.
        empty.add_all([
            AccountTag(account_id=a_id, tag="retirement"),
            AccountTag(account_id=a_id, tag="rsu"),
            AccountTag(account_id=b_id, tag="retirement"),
        ])
        empty.commit()

        r = empty_client.get(
            "/api/brokerage/networth-history?tags_include=retirement,rsu"
        )
        body = r.json()
        # Only a survives — total = 100, forward-filled across the series.
        assert len(body) >= 1
        assert all(p["balance_total"] == 100.0 for p in body)

    def test_tags_exclude(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        from src.models.history import AccountTag

        a_id, b_id = self._seed_two_accounts_with_snapshots(empty)
        empty.add(AccountTag(account_id=a_id, tag="retirement"))
        empty.commit()

        r = empty_client.get(
            "/api/brokerage/networth-history?tags_exclude=retirement"
        )
        body = r.json()
        # b survives ($250), forward-filled across the series.
        assert len(body) >= 1
        assert all(p["balance_total"] == 250.0 for p in body)

    def test_account_ids_filter(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        a_id, b_id = self._seed_two_accounts_with_snapshots(empty)
        empty.commit()

        r = empty_client.get(
            f"/api/brokerage/networth-history?account_ids={a_id}"
        )
        body = r.json()
        assert len(body) >= 1
        assert all(p["balance_total"] == 100.0 for p in body)

    def test_empty_filter_result_returns_empty_series(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        # Seed accounts but no tags; filter by a non-existent tag.
        self._seed_two_accounts_with_snapshots(empty)
        empty.commit()

        r = empty_client.get(
            "/api/brokerage/networth-history?tags_include=ghost"
        )
        assert r.status_code == 200
        assert r.json() == []

    def test_combined_account_ids_and_tags_filters(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        """account_ids ∩ tags_include − tags_exclude — verify the conjunction."""
        from src.models.history import AccountTag

        a1, a2, a3 = self._seed_three_accounts_with_snapshots(empty)
        # a1 = retirement, a2 = retirement+rsu, a3 = retirement+rsu+legacy
        empty.add(AccountTag(account_id=a1.id, tag="retirement"))
        for tag in ["retirement", "rsu"]:
            empty.add(AccountTag(account_id=a2.id, tag=tag))
        for tag in ["retirement", "rsu", "legacy"]:
            empty.add(AccountTag(account_id=a3.id, tag=tag))
        empty.commit()

        # Restrict to {a1, a2, a3}, require "retirement", exclude "rsu" → only a1.
        r = empty_client.get(
            "/api/brokerage/networth-history?"
            f"account_ids={a1.id},{a2.id},{a3.id}"
            "&tags_include=retirement&tags_exclude=rsu"
        )
        assert r.status_code == 200
        body = r.json()
        # Each helper-seeded account contributes exactly one snapshot at
        # 2024-01-01 with balance Decimal("100"); a1 alone is the survivor.
        # Forward-filled across the weekly series.
        assert len(body) >= 1
        assert all(p["account_count"] == 1 for p in body)
        assert all(p["balance_total"] == 100.0 for p in body)

    def test_multi_value_account_ids_csv(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        a1, a2, _a3 = self._seed_three_accounts_with_snapshots(empty)
        empty.commit()

        r = empty_client.get(
            f"/api/brokerage/networth-history?account_ids={a1.id},{a2.id}"
        )
        assert r.status_code == 200
        body = r.json()
        # Both accounts present in every date bucket → account_count == 2.
        assert all(p["account_count"] == 2 for p in body)

    def test_multi_value_tags_exclude_csv(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        from src.models.history import AccountTag

        a1, a2, a3 = self._seed_three_accounts_with_snapshots(empty)
        empty.add(AccountTag(account_id=a1.id, tag="retirement"))
        empty.add(AccountTag(account_id=a2.id, tag="rsu"))
        empty.add(AccountTag(account_id=a3.id, tag="legacy"))
        empty.commit()

        # Exclude both rsu and legacy → only a1 survives.
        r = empty_client.get(
            "/api/brokerage/networth-history?tags_exclude=rsu,legacy"
        )
        assert r.status_code == 200
        body = r.json()
        assert all(p["account_count"] == 1 for p in body)


class TestBenchmarkTagFilter:
    """Same filter param contract on /networth-history-benchmark."""

    @pytest.fixture(autouse=True)
    def _pin_today(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pin today to the snapshot date so the series has exactly 1 weekly point."""
        from src.api.routes import brokerage as routes_mod
        monkeypatch.setattr(
            routes_mod, "_today", lambda: date(2024, 1, 6), raising=False
        )

    def test_benchmark_endpoint_honors_account_ids_filter(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        from src.models.brokerage import Account
        from src.models.history import AccountBalanceSnapshot, HistoricalPrice

        a1 = Account(
            broker="fidelity", account_number="X1", account_type="taxable",
            entity="personal", tax_sheltered=False,
        )
        a2 = Account(
            broker="vanguard", account_number="X2", account_type="taxable",
            entity="personal", tax_sheltered=False,
        )
        empty.add_all([a1, a2])
        empty.flush()
        for acct, balance in [(a1, "1000"), (a2, "9999")]:
            empty.add(
                AccountBalanceSnapshot(
                    account_id=acct.id, raw_account_name=acct.account_number,
                    as_of=date(2024, 1, 1), balance=Decimal(balance),
                    source="xlsx_2024", source_row_hash=f"h-{acct.id}",
                )
            )
        empty.add(
            HistoricalPrice(symbol="SPY", trade_date=date(2024, 1, 1), close=Decimal("100"))
        )
        empty.commit()

        # Filter to just a1 → portfolio_value should be 1000, not 10999.
        r = empty_client.get(
            f"/api/brokerage/networth-history-benchmark?benchmark=SPY&account_ids={a1.id}"
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body["series"]) == 1
        assert body["series"][0]["portfolio_value"] == 1000.0


class TestAccountTagsDedup:
    """The PUT validator dedupes case-folded duplicates."""

    def test_put_dedupes_case_folded_duplicates(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        from src.models.brokerage import Account
        from src.models.history import AccountTag

        a = Account(
            broker="fidelity", account_number="X1", account_type="taxable",
            entity="personal", tax_sheltered=False,
        )
        empty.add(a)
        empty.commit()

        r = empty_client.put(
            f"/api/brokerage/accounts/{a.id}/tags",
            json={"tags": ["retirement", "RETIREMENT", "  retirement  "]},
        )
        assert r.status_code == 200
        assert r.json()["tags"] == ["retirement"]
        rows = empty.query(AccountTag).filter_by(account_id=a.id).all()
        assert len(rows) == 1


# ── PATCH /api/brokerage/accounts/{id} (metadata) ─────────────────────


def _make_acct_full(
    session: Session,
    *,
    account_number: str = "PATCH1",
    account_name: str | None = "Original Name",
    beneficiary: str | None = "Original Bene",
    notes: str | None = "Original notes.",
) -> Any:
    """Seed an account with all three patchable fields populated."""
    from src.models.brokerage import Account

    a = Account(
        broker="fidelity",
        account_number=account_number,
        account_type="taxable",
        entity="personal",
        tax_sheltered=False,
        account_name=account_name,
        beneficiary=beneficiary,
        notes=notes,
    )
    session.add(a)
    session.commit()
    return a


class TestPatchAccount:
    """PATCH /api/brokerage/accounts/{account_id} — partial metadata updates."""

    def test_patch_account_unknown_returns_404(
        self, empty_client: TestClient
    ) -> None:
        r = empty_client.patch(
            "/api/brokerage/accounts/no-such-id",
            json={"account_name": "ignored"},
        )
        assert r.status_code == 404

    def test_patch_account_updates_only_supplied_field(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        from src.models.brokerage import Account

        a = _make_acct_full(empty)
        r = empty_client.patch(
            f"/api/brokerage/accounts/{a.id}",
            json={"account_name": "Travis Trad IRA"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["account_id"] == a.id
        assert body["account_name"] == "Travis Trad IRA"
        assert body["beneficiary"] == "Original Bene"
        assert body["notes"] == "Original notes."

        empty.expire_all()
        row = empty.query(Account).filter_by(id=a.id).one()
        assert row.account_name == "Travis Trad IRA"
        assert row.beneficiary == "Original Bene"
        assert row.notes == "Original notes."

    def test_patch_account_explicit_null_clears_field(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        from src.models.brokerage import Account

        a = _make_acct_full(empty)
        r = empty_client.patch(
            f"/api/brokerage/accounts/{a.id}",
            json={"account_name": None},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["account_name"] is None
        assert body["beneficiary"] == "Original Bene"

        empty.expire_all()
        row = empty.query(Account).filter_by(id=a.id).one()
        assert row.account_name is None
        assert row.beneficiary == "Original Bene"
        assert row.notes == "Original notes."

    def test_patch_account_empty_body_no_changes(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        from src.models.brokerage import Account

        a = _make_acct_full(empty)
        r = empty_client.patch(
            f"/api/brokerage/accounts/{a.id}",
            json={},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["account_name"] == "Original Name"
        assert body["beneficiary"] == "Original Bene"
        assert body["notes"] == "Original notes."

        empty.expire_all()
        row = empty.query(Account).filter_by(id=a.id).one()
        assert row.account_name == "Original Name"
        assert row.beneficiary == "Original Bene"
        assert row.notes == "Original notes."

    def test_patch_account_name_too_long_returns_422(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        a = _make_acct_full(empty)
        r = empty_client.patch(
            f"/api/brokerage/accounts/{a.id}",
            json={"account_name": "x" * 200},
        )
        assert r.status_code == 422

    def test_patch_account_beneficiary_updates(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        from src.models.brokerage import Account

        a = _make_acct_full(empty)
        r = empty_client.patch(
            f"/api/brokerage/accounts/{a.id}",
            json={"beneficiary": "Travis"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["beneficiary"] == "Travis"
        assert body["account_name"] == "Original Name"
        assert body["notes"] == "Original notes."

        empty.expire_all()
        row = empty.query(Account).filter_by(id=a.id).one()
        assert row.beneficiary == "Travis"
        assert row.account_name == "Original Name"

    def test_patch_account_bumps_updated_at(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        import time

        from src.models.brokerage import Account

        a = _make_acct_full(empty)
        prior_updated_at = a.updated_at
        # Sleep enough that the SQLite-stored datetime resolution distinguishes.
        time.sleep(0.01)

        r = empty_client.patch(
            f"/api/brokerage/accounts/{a.id}",
            json={"account_name": "Renamed Account"},
        )
        assert r.status_code == 200

        empty.expire_all()
        row = empty.query(Account).filter_by(id=a.id).one()
        assert row.updated_at > prior_updated_at
        # Response also reports the bumped timestamp.
        body = r.json()
        assert body["updated_at"] is not None
        # ISO-format response > prior ISO-format string lexicographically too.
        assert body["updated_at"] > prior_updated_at.isoformat()

    def test_patch_account_notes_updates(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        from src.models.brokerage import Account

        a = _make_acct_full(empty, notes=None)
        r = empty_client.patch(
            f"/api/brokerage/accounts/{a.id}",
            json={"notes": "Some long-form notes here"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["notes"] == "Some long-form notes here"
        assert body["account_name"] == "Original Name"
        assert body["beneficiary"] == "Original Bene"

        empty.expire_all()
        row = empty.query(Account).filter_by(id=a.id).one()
        assert row.notes == "Some long-form notes here"


# ── GET /api/brokerage/accounts/{id}/detail ───────────────────────────


class TestAccountDetail:
    """T3: full account detail endpoint."""

    def test_unknown_account_returns_404(
        self, empty_client: TestClient
    ) -> None:
        r = empty_client.get("/api/brokerage/accounts/no-such-id/detail")
        assert r.status_code == 404

    def test_empty_account_returns_empty_arrays_not_nulls(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        a = _make_acct(empty)
        empty.commit()

        r = empty_client.get(f"/api/brokerage/accounts/{a.id}/detail")
        assert r.status_code == 200
        body = r.json()

        # Account fields populated.
        assert body["account"]["id"] == a.id
        assert body["account"]["broker"] == "fidelity"
        # account_number is masked in the detail response (FIX-1).
        assert body["account"]["account_number_masked"] == "****"  # "X1" → short → "****"
        assert "account_number" not in body["account"]
        assert body["account"]["account_type"] == "taxable"
        assert body["account"]["entity"] == "personal"
        assert body["account"]["tax_sheltered"] is False
        assert body["account"]["is_plan_wrapper"] is False
        assert body["account"]["tags"] == []

        # Empty arrays, not nulls.
        assert body["latest_position_snapshots"] == []
        assert body["latest_balance_snapshots"] == []
        assert body["transaction_count_by_action"] == {}
        assert body["ingestion_log_recent"] == []

        # Realized G/L summary returns zero values, not nulls.
        rgl = body["realized_gl_summary"]
        assert rgl["short_term"] == 0
        assert rgl["long_term"] == 0
        assert rgl["total"] == 0
        assert rgl["lots"] == 0

    def test_mixed_positions_and_balances_sorted_desc(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        from src.models.brokerage import PositionSnapshot
        from src.models.history import AccountBalanceSnapshot

        a = _make_acct(empty)
        empty.flush()

        # Three position snapshots on different dates.
        for i, day in enumerate([1, 5, 10]):
            ps = PositionSnapshot(
                account_id=a.id,
                as_of=datetime(2025, 1, day, 12, 0, 0),
                symbol=f"SYM{i}",
                description=f"Sec {i}",
                quantity=Decimal("10"),
                price=Decimal("100"),
                market_value=Decimal("1000"),
                source_file="t.csv",
                source_row_hash=f"ps-{i}",
                raw_data={"x": True},
            )
            empty.add(ps)

        # Two balance snapshots on different dates.
        for i, day in enumerate([3, 9]):
            empty.add(
                AccountBalanceSnapshot(
                    account_id=a.id,
                    raw_account_name="X1",
                    as_of=date(2025, 1, day),
                    balance=Decimal(f"{1000 + i * 500}"),
                    source="xlsx_2025",
                    source_row_hash=f"bs-{i}",
                )
            )

        empty.commit()

        r = empty_client.get(f"/api/brokerage/accounts/{a.id}/detail")
        assert r.status_code == 200
        body = r.json()

        positions = body["latest_position_snapshots"]
        assert len(positions) == 3
        # Sorted desc by as_of.
        as_of_dates = [p["as_of"] for p in positions]
        assert as_of_dates == sorted(as_of_dates, reverse=True)

        balances = body["latest_balance_snapshots"]
        assert len(balances) == 2
        bal_dates = [b["as_of"] for b in balances]
        assert bal_dates == sorted(bal_dates, reverse=True)

    def test_position_snapshot_list_capped_at_10(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        from src.models.brokerage import PositionSnapshot

        a = _make_acct(empty)
        empty.flush()

        # Seed 15 snapshots.
        for i in range(15):
            ps = PositionSnapshot(
                account_id=a.id,
                as_of=datetime(2025, 1, 1, 12, 0, 0) + timedelta(days=i),
                symbol="SYM",
                quantity=Decimal("1"),
                market_value=Decimal("10"),
                source_file="t.csv",
                source_row_hash=f"ps-{i}",
                raw_data={"x": True},
            )
            empty.add(ps)
        empty.commit()

        r = empty_client.get(f"/api/brokerage/accounts/{a.id}/detail")
        body = r.json()
        # Capped at 10, most recent first.
        assert len(body["latest_position_snapshots"]) == 10
        as_of_dates = [p["as_of"] for p in body["latest_position_snapshots"]]
        assert as_of_dates == sorted(as_of_dates, reverse=True)
        # Latest snapshot (i=14) is first → 2025-01-15.
        assert as_of_dates[0].startswith("2025-01-15")

    def test_transaction_count_by_action(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        from src.models.brokerage import BrokerageTransaction
        from src.models.enums import BrokerageTxStatus, CanonicalAction

        a = _make_acct(empty)
        empty.flush()

        action_counts = {
            CanonicalAction.BUY: 3,
            CanonicalAction.SELL: 2,
            CanonicalAction.DIVIDEND_ORDINARY: 5,
        }
        idx = 0
        for action, n in action_counts.items():
            for _ in range(n):
                empty.add(
                    BrokerageTransaction(
                        account_id=a.id,
                        trade_date=date(2025, 1, 1),
                        action=action.value.upper(),
                        canonical_action=action.value,
                        symbol="X",
                        amount=Decimal("100"),
                        status=BrokerageTxStatus.IMPORTED.value,
                        source_file="t.csv",
                        source_row_hash=f"tx-{idx}",
                        raw_data={"x": True},
                    )
                )
                idx += 1

        empty.commit()

        r = empty_client.get(f"/api/brokerage/accounts/{a.id}/detail")
        body = r.json()
        assert body["transaction_count_by_action"] == {
            "buy": 3,
            "sell": 2,
            "dividend_ordinary": 5,
        }

    def test_realized_gl_summary_sums_correctly(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        from src.models.brokerage import RealizedGainLoss
        from src.models.enums import GainLossTerm

        a = _make_acct(empty)
        empty.flush()

        # Two short-term and one long-term lot, across two years.
        empty.add_all([
            RealizedGainLoss(
                account_id=a.id,
                symbol="A",
                closed_date=date(2024, 6, 1),
                quantity=Decimal("1"),
                proceeds=Decimal("200"),
                cost_basis=Decimal("100"),
                gain_loss=Decimal("100"),
                st_gain_loss=Decimal("100"),
                term=GainLossTerm.SHORT.value,
                source_file="t.csv",
                source_row_hash="g1",
                raw_data={},
            ),
            RealizedGainLoss(
                account_id=a.id,
                symbol="B",
                closed_date=date(2025, 1, 1),
                quantity=Decimal("1"),
                proceeds=Decimal("250"),
                cost_basis=Decimal("200"),
                gain_loss=Decimal("50"),
                st_gain_loss=Decimal("50"),
                term=GainLossTerm.SHORT.value,
                source_file="t.csv",
                source_row_hash="g2",
                raw_data={},
            ),
            RealizedGainLoss(
                account_id=a.id,
                symbol="C",
                closed_date=date(2025, 1, 1),
                quantity=Decimal("1"),
                proceeds=Decimal("500"),
                cost_basis=Decimal("100"),
                gain_loss=Decimal("400"),
                lt_gain_loss=Decimal("400"),
                term=GainLossTerm.LONG.value,
                source_file="t.csv",
                source_row_hash="g3",
                raw_data={},
            ),
        ])
        empty.commit()

        r = empty_client.get(f"/api/brokerage/accounts/{a.id}/detail")
        body = r.json()
        rgl = body["realized_gl_summary"]
        assert rgl["short_term"] == 150.0  # 100 + 50
        assert rgl["long_term"] == 400.0
        assert rgl["total"] == 550.0
        assert rgl["lots"] == 3

    def test_tags_array_populated(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        from src.models.history import AccountTag

        a = _make_acct(empty)
        empty.add_all([
            AccountTag(account_id=a.id, tag="retirement"),
            AccountTag(account_id=a.id, tag="personal"),
        ])
        empty.commit()

        r = empty_client.get(f"/api/brokerage/accounts/{a.id}/detail")
        body = r.json()
        # Sorted alphabetically.
        assert body["account"]["tags"] == ["personal", "retirement"]

    def test_ingestion_log_recent_capped_and_sorted(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        from src.models.ingestion_log import IngestionLog

        a = _make_acct(empty)  # broker = "fidelity"
        empty.flush()

        base = datetime(2025, 1, 1, 12, 0, 0)
        # 7 fidelity logs (5 should be returned), 1 schwab (excluded).
        for i in range(7):
            empty.add(
                IngestionLog(
                    source="fidelity_csv",
                    run_at=base + timedelta(days=i),
                    status="success",
                    records_processed=10,
                    records_failed=0,
                )
            )
        empty.add(
            IngestionLog(
                source="schwab_csv",
                run_at=base + timedelta(days=100),
                status="success",
                records_processed=1,
                records_failed=0,
            )
        )
        empty.commit()

        r = empty_client.get(f"/api/brokerage/accounts/{a.id}/detail")
        body = r.json()
        logs = body["ingestion_log_recent"]
        assert len(logs) == 5
        # All fidelity (no schwab).
        assert all("fidelity" in log["source"] for log in logs)
        # Sorted desc by run_at.
        run_ats = [log["run_at"] for log in logs]
        assert run_ats == sorted(run_ats, reverse=True)
        # Most recent fidelity (i=6) first → 2025-01-07.
        assert run_ats[0].startswith("2025-01-07")

    def test_balance_snapshot_list_capped_at_10(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        """FIX-9: latest_balance_snapshots must be capped at 10 (mirroring positions)."""
        from src.models.history import AccountBalanceSnapshot

        a = _make_acct(empty)
        empty.flush()

        # Seed 15 balance snapshots on distinct dates.
        for i in range(15):
            empty.add(
                AccountBalanceSnapshot(
                    account_id=a.id,
                    raw_account_name="X1",
                    as_of=date(2025, 1, 1) + timedelta(days=i),
                    balance=Decimal(f"{1000 + i * 10}"),
                    source="xlsx_2025",
                    source_row_hash=f"bs-cap-{i}",
                )
            )
        empty.commit()

        r = empty_client.get(f"/api/brokerage/accounts/{a.id}/detail")
        body = r.json()
        # Capped at 10, most recent first.
        assert len(body["latest_balance_snapshots"]) == 10
        bal_dates = [b["as_of"] for b in body["latest_balance_snapshots"]]
        assert bal_dates == sorted(bal_dates, reverse=True)
        # Latest snapshot (i=14) should be first → 2025-01-15.
        assert bal_dates[0] == "2025-01-15"

    def test_detail_account_number_is_masked(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        """FIX-1: detail endpoint must return account_number_masked, not raw account_number."""
        a = _make_acct(empty)  # account_number = "X1"
        empty.commit()

        r = empty_client.get(f"/api/brokerage/accounts/{a.id}/detail")
        body = r.json()
        # Must have the masked field, not the raw one.
        assert "account_number_masked" in body["account"]
        assert "account_number" not in body["account"]
        # account_number "X1" is short, so _mask_account_number returns "****".
        assert body["account"]["account_number_masked"] == "****"

    def test_ingestion_log_exposes_error_detail(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        """FIX-4: ingestion_log_recent must include error_detail (truncated to 200 chars)."""
        from src.models.ingestion_log import IngestionLog

        a = _make_acct(empty)  # broker = "fidelity"
        empty.flush()
        long_error = "E" * 300
        empty.add(
            IngestionLog(
                source="fidelity_csv",
                run_at=datetime(2025, 6, 1, 12, 0, 0),
                status="error",
                records_processed=0,
                records_failed=5,
                error_detail=long_error,
            )
        )
        empty.commit()

        r = empty_client.get(f"/api/brokerage/accounts/{a.id}/detail")
        body = r.json()
        logs = body["ingestion_log_recent"]
        assert len(logs) == 1
        # error_detail must be present and truncated to 200 chars.
        assert logs[0]["error_detail"] == "E" * 200

    def test_ingestion_log_error_detail_null_when_no_error(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        """FIX-4: error_detail is null for successful runs."""
        from src.models.ingestion_log import IngestionLog

        a = _make_acct(empty)  # broker = "fidelity"
        empty.flush()
        empty.add(
            IngestionLog(
                source="fidelity_csv",
                run_at=datetime(2025, 6, 1, 12, 0, 0),
                status="success",
                records_processed=10,
                records_failed=0,
                error_detail=None,
            )
        )
        empty.commit()

        r = empty_client.get(f"/api/brokerage/accounts/{a.id}/detail")
        body = r.json()
        assert body["ingestion_log_recent"][0]["error_detail"] is None


# ── Additional TestPatchAccount tests ──────────────────────────────────


class TestPatchAccountAdditional:
    """FIX-2, FIX-10, FIX-11: additional validation tests for PATCH endpoint."""

    def test_patch_notes_too_long_returns_422(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        """FIX-2: notes field must be capped at 4096 chars."""
        a = _make_acct_full(empty)
        r = empty_client.patch(
            f"/api/brokerage/accounts/{a.id}",
            json={"notes": "x" * 5000},
        )
        assert r.status_code == 422

    def test_patch_beneficiary_too_long_returns_422(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        """FIX-11: beneficiary must be capped at 64 chars."""
        a = _make_acct_full(empty)
        r = empty_client.patch(
            f"/api/brokerage/accounts/{a.id}",
            json={"beneficiary": "x" * 65},
        )
        assert r.status_code == 422

    def test_patch_empty_body_updated_at_unchanged(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        """FIX-10: empty body must not bump updated_at."""
        from src.models.brokerage import Account

        a = _make_acct_full(empty)
        prior_updated_at = a.updated_at

        r = empty_client.patch(
            f"/api/brokerage/accounts/{a.id}",
            json={},
        )
        assert r.status_code == 200
        body = r.json()

        # updated_at in response must equal the pre-existing value.
        assert body["updated_at"] == prior_updated_at.isoformat()

        # DB row must be unchanged.
        empty.expire_all()
        row = empty.query(Account).filter_by(id=a.id).one()
        assert row.updated_at == prior_updated_at


# ── Pipeline 004: networth-history forward-fill + granularity ──────────


class TestNetworthHistoryForwardFill:
    """Pipeline 004 T2 — granularity param, today always last, end-to-end
    forward-fill via the endpoint."""

    @pytest.fixture(autouse=True)
    def _pin_today(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.api.routes import brokerage as routes_mod

        monkeypatch.setattr(
            routes_mod, "_today", lambda: date(2024, 7, 1), raising=False
        )

    def _seed_one_account_one_snapshot(
        self, session: Session, *, as_of: date, balance: Decimal
    ) -> str:
        from src.models.brokerage import Account
        from src.models.history import AccountBalanceSnapshot

        a = Account(
            broker="fidelity",
            account_number="X1",
            account_type="taxable",
            entity="personal",
            tax_sheltered=False,
        )
        session.add(a)
        session.flush()
        session.add(
            AccountBalanceSnapshot(
                account_id=a.id,
                raw_account_name="X1",
                as_of=as_of,
                balance=balance,
                source="xlsx_2024",
                source_row_hash=f"ff-{as_of.isoformat()}",
            )
        )
        session.commit()
        return a.id

    def test_default_granularity_is_weekly(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        self._seed_one_account_one_snapshot(
            empty, as_of=date(2024, 1, 1), balance=Decimal("100")
        )

        r = empty_client.get("/api/brokerage/networth-history")
        body = r.json()
        # Saturdays from 2024-01-06 through 2024-06-29 = 26, plus today.
        # ≥ 26 weekly points; the exact count is 26 + 1 (today) = 27.
        assert len(body) == 27

    def test_daily_granularity_produces_one_point_per_day(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        self._seed_one_account_one_snapshot(
            empty, as_of=date(2024, 6, 25), balance=Decimal("500")
        )

        r = empty_client.get(
            "/api/brokerage/networth-history?granularity=daily"
        )
        body = r.json()
        # 2024-06-25 → 2024-07-01 inclusive = 7 days.
        assert len(body) == 7
        # Every point sees the forward-filled balance.
        assert all(p["balance_total"] == 500.0 for p in body)

    def test_monthly_granularity_uses_month_end(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        # Seed an early snapshot so we get a few full months of series.
        self._seed_one_account_one_snapshot(
            empty, as_of=date(2024, 1, 15), balance=Decimal("1000")
        )

        r = empty_client.get(
            "/api/brokerage/networth-history?granularity=monthly"
        )
        body = r.json()
        # Month-ends Jan 31, Feb 29, Mar 31, Apr 30, May 31, Jun 30 (= 6) plus
        # today (2024-07-01) appended at the end.
        assert len(body) == 7, f"Expected 6 month-ends + today = 7 points, got {len(body)}"
        as_ofs = [p["as_of"] for p in body]
        assert "2024-01-31" in as_ofs
        assert "2024-06-30" in as_ofs
        assert as_ofs[-1] == "2024-07-01"  # today always last

    def test_today_always_appended_as_last_point(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        # Snapshot is today — weekly Saturdays would land on 2024-06-29 last.
        # Today's point must still be appended.
        self._seed_one_account_one_snapshot(
            empty, as_of=date(2024, 1, 1), balance=Decimal("42")
        )

        r = empty_client.get("/api/brokerage/networth-history")
        body = r.json()
        assert body[-1]["as_of"] == "2024-07-01"

    def test_forward_fill_then_repricing_end_to_end(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        """End-to-end smoke: a PositionSnapshot at X with HistoricalPrice at
        a later target date must report the re-priced value at every weekly
        point ≥ the snapshot date."""
        from src.models.brokerage import Account, PositionSnapshot
        from src.models.history import HistoricalPrice

        a = Account(
            broker="schwab",
            account_number="RP1",
            account_type="taxable",
            entity="personal",
            tax_sheltered=False,
        )
        empty.add(a)
        empty.flush()
        empty.add(
            PositionSnapshot(
                account_id=a.id,
                as_of=datetime(2024, 1, 1, 12, 0, 0),
                symbol="VTI",
                description="VTI",
                quantity=Decimal("100"),
                market_value=Decimal("20000"),
                source_file="t.csv",
                source_row_hash="rp-end-to-end",
                raw_data={},
            )
        )
        # HistoricalPrice on the most-recent Saturday before today
        # (2024-06-29 is a Saturday). 100 × $300 = $30k.
        empty.add(
            HistoricalPrice(
                symbol="VTI",
                trade_date=date(2024, 6, 29),
                close=Decimal("300"),
                source="test",
            )
        )
        empty.commit()

        r = empty_client.get("/api/brokerage/networth-history")
        body = r.json()
        assert len(body) >= 2
        # Earliest weekly point — no price coverage yet, stored mv ($20k).
        assert body[0]["balance_total"] == 20000.0
        # Last point is today (2024-07-01), within the 7-day rollback of
        # the 2024-06-29 close → re-priced to $30k.
        assert body[-1]["as_of"] == "2024-07-01"
        assert body[-1]["balance_total"] == 30000.0

    def test_unmatched_continues_to_contribute_when_other_account_matched(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        """FIX-1 scenario A: Unmatched "Aiden 529" must still contribute after
        "Vanguard 65344815" gets a matched ABS row.  Different raw_account_name
        means no suppression — the per-account cutoff is name-based."""
        from src.models.brokerage import Account
        from src.models.history import AccountBalanceSnapshot

        # Matched live account with a different raw_account_name.
        live = Account(
            broker="vanguard",
            account_number="65344815",
            account_type="taxable",
            entity="personal",
            tax_sheltered=False,
        )
        empty.add(live)
        empty.flush()

        # Unmatched XLSX row: "Aiden 529" has no matched counterpart.
        empty.add(
            AccountBalanceSnapshot(
                account_id=None,
                raw_account_name="Aiden 529",
                as_of=date(2024, 1, 1),
                balance=Decimal("1000"),
                source="xlsx_2024",
                source_row_hash="aiden-529-h",
            )
        )
        # Matched ABS for the live account under a different name.
        empty.add(
            AccountBalanceSnapshot(
                account_id=live.id,
                raw_account_name="Vanguard 65344815",
                as_of=date(2024, 6, 1),
                balance=Decimal("5000"),
                source="xlsx_2024",
                source_row_hash="vg-live-h",
            )
        )
        empty.commit()

        r = empty_client.get(
            "/api/brokerage/networth-history?include_unmatched=true"
        )
        assert r.status_code == 200
        body = r.json()
        final = body[-1]
        assert final["as_of"] == "2024-07-01"
        # Matched $5k (live account) + unmatched $1k (Aiden 529) = $6k.
        assert final["balance_total"] == pytest.approx(6000.0), (
            "Aiden 529 ($1000 unmatched) must not be suppressed by "
            "Vanguard 65344815 ($5000 matched) — different raw_account_names"
        )

    def test_unmatched_suppressed_when_same_raw_name_matched(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        """FIX-1 scenario B: Unmatched "Travis Roth" must be suppressed once a
        matched ABS row with the same raw_account_name exists at-or-before target."""
        from src.models.brokerage import Account
        from src.models.history import AccountBalanceSnapshot

        live = Account(
            broker="vanguard",
            account_number="11111111",
            account_type="taxable",
            entity="personal",
            tax_sheltered=False,
        )
        empty.add(live)
        empty.flush()

        # Unmatched row dated 2024-01-01.
        empty.add(
            AccountBalanceSnapshot(
                account_id=None,
                raw_account_name="Travis Roth",
                as_of=date(2024, 1, 1),
                balance=Decimal("2000"),
                source="xlsx_2024",
                source_row_hash="tr-unmatched-h",
            )
        )
        # Matched row with same name starts 2024-06-01.
        empty.add(
            AccountBalanceSnapshot(
                account_id=live.id,
                raw_account_name="Travis Roth",
                as_of=date(2024, 6, 1),
                balance=Decimal("2500"),
                source="xlsx_2024",
                source_row_hash="tr-matched-h",
            )
        )
        empty.commit()

        r = empty_client.get(
            "/api/brokerage/networth-history?include_unmatched=true"
        )
        assert r.status_code == 200
        body = r.json()
        final = body[-1]
        assert final["as_of"] == "2024-07-01"
        # After 2024-06-01 only the matched $2500 should contribute.
        assert final["balance_total"] == pytest.approx(2500.0), (
            "Unmatched Travis Roth ($2000) must be suppressed once matched "
            "Travis Roth ($2500) exists — same name prevents double-count"
        )

    def test_networth_history_excludes_plan_wrapper(
        self, empty_client: TestClient, empty: Session
    ) -> None:
        """FIX-2: plan-wrapper accounts must NOT contribute to balance_total
        at any point in the series."""
        from src.models.brokerage import Account
        from src.models.history import AccountBalanceSnapshot

        wrapper = Account(
            broker="fidelity",
            account_number="WRAP01",
            account_type="401k",
            entity="personal",
            tax_sheltered=True,
            is_plan_wrapper=True,
        )
        non_wrapper = Account(
            broker="vanguard",
            account_number="REAL01",
            account_type="taxable",
            entity="personal",
            tax_sheltered=False,
        )
        empty.add_all([wrapper, non_wrapper])
        empty.flush()

        empty.add(
            AccountBalanceSnapshot(
                account_id=wrapper.id,
                raw_account_name="Fidelity 401k Plan",
                as_of=date(2024, 1, 1),
                balance=Decimal("100000"),
                source="xlsx_2024",
                source_row_hash="wrap-h",
            )
        )
        empty.add(
            AccountBalanceSnapshot(
                account_id=non_wrapper.id,
                raw_account_name="Vanguard Taxable",
                as_of=date(2024, 1, 1),
                balance=Decimal("50000"),
                source="xlsx_2024",
                source_row_hash="real-h",
            )
        )
        empty.commit()

        r = empty_client.get("/api/brokerage/networth-history")
        assert r.status_code == 200
        body = r.json()
        assert len(body) >= 1
        for pt in body:
            assert pt["balance_total"] == pytest.approx(50000.0), (
                f"Plan-wrapper $100k must be excluded; got {pt['balance_total']} "
                f"at {pt['as_of']}"
            )

    def test_single_snapshot_today_wednesday_returns_one_point(
        self, empty_client: TestClient, empty: Session,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """FIX-8: When the only snapshot is dated today (a Wednesday), the
        weekly series has no Saturday before today, so today is appended and
        the series must be exactly 1 point."""
        from src.api.routes import brokerage as routes_mod
        # 2024-07-03 is a Wednesday.
        monkeypatch.setattr(routes_mod, "_today", lambda: date(2024, 7, 3), raising=False)

        from src.models.brokerage import Account
        from src.models.history import AccountBalanceSnapshot

        a = Account(
            broker="schwab", account_number="WED01", account_type="taxable",
            entity="personal", tax_sheltered=False,
        )
        empty.add(a)
        empty.flush()
        empty.add(
            AccountBalanceSnapshot(
                account_id=a.id,
                raw_account_name="Wednesday Account",
                as_of=date(2024, 7, 3),  # today (Wednesday)
                balance=Decimal("42000"),
                source="xlsx_2024",
                source_row_hash="wed-only",
            )
        )
        empty.commit()

        r = empty_client.get("/api/brokerage/networth-history")
        assert r.status_code == 200
        body = r.json()
        assert len(body) == 1, (
            f"Expected exactly 1 point for a snapshot dated today (Wednesday); "
            f"got {len(body)}: {[p['as_of'] for p in body]}"
        )
        assert body[0]["as_of"] == "2024-07-03"
        assert body[0]["balance_total"] == pytest.approx(42000.0)


# ── /api/brokerage/policy, /bold-bets, /networth-attribution (P1-b2c) ───
#
# REQ-IPD-001..004, REQ-BBT-001..002, REQ-NWA-001. These hit the routes
# through TestClient end-to-end (not the underlying analytics functions
# directly) so a field-name typo in the route handlers' manual dict mapping
# (src/api/routes/brokerage.py ~1793-1961) would surface as a real 500/422,
# and confirm the same `require_api_key` dependency chain used by the
# sibling /api/brokerage routes applies here too.


class TestPolicyRoute:
    def test_empty_returns_200_with_expected_shape(
        self, empty_client: TestClient
    ) -> None:
        r = empty_client.get("/api/brokerage/policy")
        assert r.status_code == 200, r.text
        body = r.json()
        for key in (
            "as_of", "investable_base", "equity_base", "cash_value", "cash_pct",
            "international_value", "international_pct_of_equity",
            "international_target_pct", "combined_symbols", "combined_value",
            "combined_pct", "current_pct", "glide_pct", "headroom_pts",
            "drift_alert_threshold_pts", "concentration", "glide_series",
            "wa_tax_year", "realized_lt_gains_ytd", "excise_threshold",
            "excise_threshold_headroom", "excise_surcharge_threshold",
            "excise_surcharge_headroom", "bold_bets_over_cap",
            "bold_bets_sleeve_value", "bold_bets_cap", "warnings",
        ):
            assert key in body, f"missing key {key!r} in /brokerage/policy response"
        assert body["investable_base"] == 0
        assert body["concentration"] == []
        assert isinstance(body["glide_series"], list) and body["glide_series"]

    def test_seeded_returns_200(self, client: TestClient) -> None:
        r = client.get("/api/brokerage/policy")
        assert r.status_code == 200, r.text


class TestBoldBetsRoute:
    def test_empty_returns_200_with_expected_shape(
        self, empty_client: TestClient
    ) -> None:
        r = empty_client.get("/api/brokerage/bold-bets")
        assert r.status_code == 200, r.text
        body = r.json()
        for key in (
            "positions", "sleeve_value", "sleeve_cost_basis", "sleeve_unrealized",
            "sleeve_realized", "cap", "over_cap", "pct_of_investable",
            "investable_base",
        ):
            assert key in body, f"missing key {key!r} in /brokerage/bold-bets response"
        assert body["positions"] == []
        assert body["over_cap"] is False

    def test_seeded_returns_200(self, client: TestClient) -> None:
        r = client.get("/api/brokerage/bold-bets")
        assert r.status_code == 200, r.text


class TestNetworthAttributionRoute:
    def test_empty_returns_200_with_expected_shape(
        self, empty_client: TestClient
    ) -> None:
        r = empty_client.get(
            "/api/brokerage/networth-attribution"
            "?start=2026-01-01&end=2026-03-01"
        )
        assert r.status_code == 200, r.text
        body = r.json()
        for key in (
            "start", "end", "nw_start", "nw_end", "delta_nw", "market_effect",
            "net_flows", "coverage_change", "flow_tx_count", "new_account_count",
            "dropped_account_count", "weekly_line",
        ):
            assert key in body, (
                f"missing key {key!r} in /brokerage/networth-attribution response"
            )
        assert body["start"] == "2026-01-01"
        assert body["end"] == "2026-03-01"
        assert body["weekly_line"].startswith("NW Δ $")

    def test_seeded_returns_200(self, client: TestClient) -> None:
        r = client.get(
            "/api/brokerage/networth-attribution"
            "?start=2026-01-01&end=2026-06-01"
        )
        assert r.status_code == 200, r.text

    def test_missing_query_params_is_422(self, empty_client: TestClient) -> None:
        r = empty_client.get("/api/brokerage/networth-attribution")
        assert r.status_code == 422

    def test_end_before_start_is_422(self, empty_client: TestClient) -> None:
        r = empty_client.get(
            "/api/brokerage/networth-attribution"
            "?start=2026-03-01&end=2026-01-01"
        )
        assert r.status_code == 422


class TestNewWealthEndpointsAuthParity:
    """P1-b2c: confirm /policy, /bold-bets, /networth-attribution enforce the
    same `require_api_key` dependency chain as an existing sibling route
    (/api/brokerage/networth) — set via `_auth` on the whole router in
    src/api/main.py, so parity here is a proxy for "wired into the same
    router include" rather than a route-specific auth bypass."""

    _NEW_PATHS = (
        "/api/brokerage/policy",
        "/api/brokerage/bold-bets",
        "/api/brokerage/networth-attribution?start=2026-01-01&end=2026-03-01",
    )

    def test_no_api_key_env_all_new_routes_pass(
        self, empty_client: TestClient
    ) -> None:
        import os
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("API_KEY", None)
            for path in self._NEW_PATHS:
                r = empty_client.get(path)
                assert r.status_code == 200, f"{path}: {r.text}"

    def test_api_key_set_missing_key_gets_401_like_sibling_route(
        self, empty_client: TestClient
    ) -> None:
        with patch.dict("os.environ", {"API_KEY": "secret-test-key"}):
            # Sibling route for comparison.
            sibling = empty_client.get("/api/brokerage/networth")
            assert sibling.status_code == 401
            for path in self._NEW_PATHS:
                r = empty_client.get(path)
                assert r.status_code == 401, f"{path}: expected 401, got {r.status_code}"

    def test_api_key_set_correct_key_gets_200(
        self, empty_client: TestClient
    ) -> None:
        with patch.dict("os.environ", {"API_KEY": "secret-test-key"}):
            for path in self._NEW_PATHS:
                r = empty_client.get(
                    path, headers={"X-Api-Key": "secret-test-key"}
                )
                assert r.status_code == 200, f"{path}: {r.text}"
