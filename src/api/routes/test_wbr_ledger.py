"""Tests for GET /api/ingest/wbr/ledger-summary — Sparks Personal WBR ledger feed.

REQ-ID: REQ-WBR-LED-001  Endpoint returns the WBR ledger JSON contract
                         (week_end, transactions[{date,name,category,amount}],
                         inflow_total, outflow_total, entity, truncated).
REQ-ID: REQ-WBR-LED-002  Auth mirrors the ingest routes: 401 without a key;
                         accepts either API_KEY or INGEST_API_KEY via X-Api-Key.
REQ-ID: REQ-WBR-LED-003  Window is the 7 calendar days ending week_end inclusive.
REQ-ID: REQ-WBR-LED-004  week_end defaults to the most recent Sunday.
REQ-ID: REQ-WBR-LED-005  status="rejected" transactions are excluded.
REQ-ID: REQ-WBR-LED-006  Entity filter defaults to personal; explicit values
                         honored; unknown entity -> 422.
REQ-ID: REQ-WBR-LED-007  Rows sorted by absolute amount descending, capped at
                         40, truncated=true when capped; totals still cover the
                         full (uncapped) window.
REQ-ID: REQ-WBR-LED-008  inflow_total / outflow_total are positive 2dp numbers
                         computed with Decimal (no float drift).
REQ-ID: REQ-WBR-LED-009  Malformed week_end -> 422.
REQ-ID: REQ-WBR-LED-010  Income-direction rows stored negative are surfaced
                         positive (mirrors TransactionOut.fix_income_sign).
REQ-ID: REQ-WBR-LED-011  NULL-amount rows and split children (parent_id set)
                         are excluded so totals never double-count.
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
from sqlalchemy.orm import sessionmaker

from src.api.routes.wbr_ledger import most_recent_sunday
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

_AK = "a" * 32
_IK = "i" * 32

# ---------------------------------------------------------------------------
# Shared-cache in-memory test database (same pattern as test_reimbursable.py)
# ---------------------------------------------------------------------------

_TEST_DB_URI = "file:accounting_wbr_ledger_test?mode=memory&cache=shared&uri=true"

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
    """Truncate all tables before each test for isolation."""
    with _test_engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
        conn.execute(text("PRAGMA foreign_keys=ON"))
    yield


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    """TestClient with the WBR route's sessions redirected to the test DB."""
    monkeypatch.setenv("API_KEY", _AK)
    monkeypatch.setenv("INGEST_API_KEY", _IK)

    from src.api import main as _main_module
    from src.api.routes import wbr_ledger as _wbr_module

    with (
        patch.object(_wbr_module, "SessionLocal", _TestSession),
        patch.object(_main_module, "SessionLocal", _TestSession),
        patch.object(_main_module, "init_db", return_value=None),
        patch.object(_main_module, "seed_vendor_rules", return_value=0),
        patch.object(
            _main_module,
            "seed_customers",
            return_value={
                "customers_inserted": 0,
                "customers_updated": 0,
                "invoices_inserted": 0,
            },
        ),
    ):
        from src.api.main import app

        with TestClient(app) as c:
            yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

URL = "/api/ingest/wbr/ledger-summary"
WEEK_END = "2026-07-26"  # a Sunday
AUTH = {"X-Api-Key": _IK}


def _make_tx(
    *,
    date: str = "2026-07-24",
    description: str = "Test Vendor",
    amount: Decimal | None = Decimal("-50.00"),
    entity: str | None = Entity.PERSONAL.value,
    tax_category: str | None = TaxCategory.PERSONAL_NON_DEDUCTIBLE.value,
    direction: str | None = Direction.EXPENSE.value,
    status: str = TransactionStatus.CONFIRMED.value,
    parent_id: str | None = None,
) -> Transaction:
    tx = Transaction(
        id=str(uuid.uuid4()),
        source=Source.BANK_CSV.value,
        source_id=str(uuid.uuid4()),
        source_hash=str(uuid.uuid4()),
        date=date,
        description=description,
        amount=amount,
        currency="USD",
        entity=entity,
        direction=direction,
        tax_category=tax_category,
        status=status,
        confidence=1.0,
        raw_data={"test": True},
        confirmed_by=ConfirmedBy.AUTO.value,
        parent_id=parent_id,
    )
    with _TestSession() as s:
        s.add(tx)
        s.commit()
        s.refresh(tx)
        s.expunge(tx)
    return tx


def _get(client: TestClient, **params: str) -> Any:
    r = client.get(URL, headers=AUTH, params={"week_end": WEEK_END, **params})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


class TestContract:
    def test_returns_wbr_ledger_contract(self, client: TestClient) -> None:
        """REQ-ID: REQ-WBR-LED-001 — full JSON shape with signed amounts."""
        _make_tx(
            date="2026-07-22",
            description="Whole Foods",
            amount=Decimal("-612.00"),
            tax_category=TaxCategory.PERSONAL_NON_DEDUCTIBLE.value,
        )
        _make_tx(
            date="2026-07-20",
            description="Cardinal Health Payroll",
            amount=Decimal("11530.00"),
            direction=Direction.INCOME.value,
            tax_category=None,
        )
        body = _get(client)
        assert body["week_end"] == WEEK_END
        assert body["entity"] == "personal"
        assert body["truncated"] is False
        assert body["inflow_total"] == 11530.00
        assert body["outflow_total"] == 612.00
        assert body["transactions"] == [
            {
                "date": "2026-07-20",
                "name": "Cardinal Health Payroll",
                "category": "income",
                "amount": 11530.00,
            },
            {
                "date": "2026-07-22",
                "name": "Whole Foods",
                "category": "PERSONAL_NON_DEDUCTIBLE",
                "amount": -612.00,
            },
        ]

    def test_category_falls_back_to_direction_then_placeholder(
        self, client: TestClient
    ) -> None:
        """REQ-ID: REQ-WBR-LED-001 — category = tax_category, else direction."""
        _make_tx(
            description="No Category",
            amount=Decimal("-10.00"),
            tax_category=None,
            direction=Direction.EXPENSE.value,
        )
        _make_tx(
            description="Nothing At All",
            amount=Decimal("-20.00"),
            tax_category=None,
            direction=None,
        )
        rows = {t["name"]: t["category"] for t in _get(client)["transactions"]}
        assert rows["No Category"] == "expense"
        assert rows["Nothing At All"] == "uncategorized"

    def test_empty_week_returns_zero_totals(self, client: TestClient) -> None:
        """REQ-ID: REQ-WBR-LED-001 — empty window is a valid, zeroed payload."""
        body = _get(client)
        assert body["transactions"] == []
        assert body["inflow_total"] == 0.0
        assert body["outflow_total"] == 0.0


# ---------------------------------------------------------------------------
# Auth (mirror of the ingest-route pattern)
# ---------------------------------------------------------------------------


class TestAuth:
    def test_requires_auth(self, client: TestClient) -> None:
        """REQ-ID: REQ-WBR-LED-002 — no key -> 401."""
        r = client.get(URL, params={"week_end": WEEK_END})
        assert r.status_code == 401

    def test_rejects_wrong_key(self, client: TestClient) -> None:
        """REQ-ID: REQ-WBR-LED-002 — wrong key -> 401."""
        r = client.get(URL, headers={"X-Api-Key": "nope"}, params={"week_end": WEEK_END})
        assert r.status_code == 401

    def test_accepts_ingest_key(self, client: TestClient) -> None:
        """REQ-ID: REQ-WBR-LED-002 — n8n's INGEST_API_KEY is accepted."""
        r = client.get(URL, headers={"X-Api-Key": _IK}, params={"week_end": WEEK_END})
        assert r.status_code == 200

    def test_accepts_api_key(self, client: TestClient) -> None:
        """REQ-ID: REQ-WBR-LED-002 — the browser API_KEY is accepted too."""
        r = client.get(URL, headers={"X-Api-Key": _AK}, params={"week_end": WEEK_END})
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------


class TestWindow:
    def test_seven_day_inclusive_window(self, client: TestClient) -> None:
        """REQ-ID: REQ-WBR-LED-003 — [week_end-6, week_end] inclusive."""
        _make_tx(date="2026-07-26", description="on-end", amount=Decimal("-1.00"))
        _make_tx(date="2026-07-20", description="on-start", amount=Decimal("-2.00"))
        _make_tx(date="2026-07-19", description="before", amount=Decimal("-3.00"))
        _make_tx(date="2026-07-27", description="after", amount=Decimal("-4.00"))
        names = {t["name"] for t in _get(client)["transactions"]}
        assert names == {"on-end", "on-start"}

    def test_default_week_end_is_most_recent_sunday(self, client: TestClient) -> None:
        """REQ-ID: REQ-WBR-LED-004 — omitted week_end defaults to last Sunday."""
        r = client.get(URL, headers=AUTH)
        assert r.status_code == 200, r.text
        expected = most_recent_sunday(date.today()).isoformat()
        assert r.json()["week_end"] == expected

    @pytest.mark.parametrize(
        ("today", "sunday"),
        [
            (date(2026, 7, 26), date(2026, 7, 26)),  # Sunday -> itself
            (date(2026, 7, 27), date(2026, 7, 26)),  # Monday
            (date(2026, 7, 30), date(2026, 7, 26)),  # Thursday
            (date(2026, 8, 1), date(2026, 7, 26)),  # Saturday
        ],
    )
    def test_most_recent_sunday(self, today: date, sunday: date) -> None:
        """REQ-ID: REQ-WBR-LED-004 — helper handles every weekday."""
        assert most_recent_sunday(today) == sunday

    def test_malformed_week_end_rejected(self, client: TestClient) -> None:
        """REQ-ID: REQ-WBR-LED-009 — non-ISO week_end -> 422."""
        for bad in ("07/26/2026", "2026-13-01", "sunday", "2026-07-26T00:00:00"):
            r = client.get(URL, headers=AUTH, params={"week_end": bad})
            assert r.status_code == 422, f"{bad!r}: {r.status_code}"


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


class TestFilters:
    def test_rejected_excluded(self, client: TestClient) -> None:
        """REQ-ID: REQ-WBR-LED-005 — status=rejected never appears."""
        _make_tx(description="keep", amount=Decimal("-10.00"))
        _make_tx(
            description="drop",
            amount=Decimal("-99.00"),
            status=TransactionStatus.REJECTED.value,
        )
        body = _get(client)
        assert [t["name"] for t in body["transactions"]] == ["keep"]
        assert body["outflow_total"] == 10.00

    def test_entity_defaults_to_personal(self, client: TestClient) -> None:
        """REQ-ID: REQ-WBR-LED-006 — sparkry/blackline/NULL-entity rows excluded."""
        _make_tx(description="personal", amount=Decimal("-10.00"))
        _make_tx(
            description="business",
            amount=Decimal("-20.00"),
            entity=Entity.SPARKRY.value,
        )
        _make_tx(description="unclassified", amount=Decimal("-30.00"), entity=None)
        names = [t["name"] for t in _get(client)["transactions"]]
        assert names == ["personal"]

    def test_explicit_entity_param(self, client: TestClient) -> None:
        """REQ-ID: REQ-WBR-LED-006 — ?entity=sparkry honored."""
        _make_tx(description="personal", amount=Decimal("-10.00"))
        _make_tx(
            description="business",
            amount=Decimal("-20.00"),
            entity=Entity.SPARKRY.value,
        )
        body = _get(client, entity="sparkry")
        assert [t["name"] for t in body["transactions"]] == ["business"]
        assert body["entity"] == "sparkry"

    def test_unknown_entity_rejected(self, client: TestClient) -> None:
        """REQ-ID: REQ-WBR-LED-006 — bogus entity -> 422."""
        r = client.get(
            URL, headers=AUTH, params={"week_end": WEEK_END, "entity": "acme"}
        )
        assert r.status_code == 422

    def test_null_amount_and_split_children_excluded(self, client: TestClient) -> None:
        """REQ-ID: REQ-WBR-LED-011 — no NULL amounts, no split double-count."""
        parent = _make_tx(description="split-parent", amount=Decimal("-100.00"))
        _make_tx(
            description="split-child",
            amount=Decimal("-60.00"),
            parent_id=parent.id,
        )
        _make_tx(description="amountless", amount=None)
        body = _get(client)
        assert [t["name"] for t in body["transactions"]] == ["split-parent"]
        assert body["outflow_total"] == 100.00


# ---------------------------------------------------------------------------
# Sorting / cap / totals
# ---------------------------------------------------------------------------


class TestSortCapTotals:
    def test_sorted_by_abs_amount_desc(self, client: TestClient) -> None:
        """REQ-ID: REQ-WBR-LED-007 — |amount| descending, sign-agnostic."""
        _make_tx(description="small", amount=Decimal("-5.00"))
        _make_tx(
            description="big-in",
            amount=Decimal("900.00"),
            direction=Direction.INCOME.value,
        )
        _make_tx(description="mid", amount=Decimal("-450.00"))
        names = [t["name"] for t in _get(client)["transactions"]]
        assert names == ["big-in", "mid", "small"]

    def test_caps_at_40_rows_and_flags_truncated(self, client: TestClient) -> None:
        """REQ-ID: REQ-WBR-LED-007 — 41 rows -> 40 returned + truncated=true;
        totals still include the dropped row."""
        for i in range(41):
            _make_tx(
                description=f"tx-{i:02d}",
                amount=Decimal(f"-{100 + i}.00"),
            )
        body = _get(client)
        assert len(body["transactions"]) == 40
        assert body["truncated"] is True
        # Smallest |amount| row (tx-00, -100.00) is the one dropped …
        assert "tx-00" not in {t["name"] for t in body["transactions"]}
        # … but totals cover the full window: sum(100..140) = 4920
        assert body["outflow_total"] == 4920.00

    def test_forty_rows_not_truncated(self, client: TestClient) -> None:
        """REQ-ID: REQ-WBR-LED-007 — exactly 40 rows -> truncated=false."""
        for i in range(40):
            _make_tx(description=f"tx-{i:02d}", amount=Decimal(f"-{100 + i}.00"))
        body = _get(client)
        assert len(body["transactions"]) == 40
        assert body["truncated"] is False

    def test_totals_are_decimal_exact_2dp(self, client: TestClient) -> None:
        """REQ-ID: REQ-WBR-LED-008 — cent-level sums stay exact (no float drift)."""
        _make_tx(description="a", amount=Decimal("-10.10"))
        _make_tx(description="b", amount=Decimal("-20.20"))
        _make_tx(description="c", amount=Decimal("-0.03"))
        _make_tx(
            description="in-1",
            amount=Decimal("0.10"),
            direction=Direction.INCOME.value,
        )
        _make_tx(
            description="in-2",
            amount=Decimal("0.20"),
            direction=Direction.INCOME.value,
        )
        body = _get(client)
        assert body["outflow_total"] == 30.33
        assert body["inflow_total"] == 0.30


# ---------------------------------------------------------------------------
# Sign convention
# ---------------------------------------------------------------------------


class TestSignConvention:
    def test_income_stored_negative_surfaces_positive(self, client: TestClient) -> None:
        """REQ-ID: REQ-WBR-LED-010 — mirrors TransactionOut.fix_income_sign."""
        _make_tx(
            description="gmail-income",
            amount=Decimal("-250.00"),
            direction=Direction.INCOME.value,
            tax_category=None,
        )
        body = _get(client)
        (row,) = body["transactions"]
        assert row["amount"] == 250.00
        assert body["inflow_total"] == 250.00
        assert body["outflow_total"] == 0.0
