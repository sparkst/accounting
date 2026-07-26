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
REQ-ID: REQ-WBR-LED-012  direction=transfer rows stay visible in the ledger
                         list (category "Transfer") but are EXCLUDED from
                         inflow_total/outflow_total.
REQ-ID: REQ-WBR-LED-013  Ingest-key entity scope: INGEST_API_KEY may only
                         query entity=personal (403 otherwise); full API_KEY
                         may query any entity. week_end is bounded to no more
                         than 120 days old and never in the future (422).

Golden-date table for ``most_recent_sunday`` (round-2 fix directive P1-a1b —
the most recent Sunday STRICTLY BEFORE the reference date, in
America/Los_Angeles; n8n's compute-week-end.js semantics are authoritative).
The identical table is mirrored in sparkry-crm-wbr's dates.test.ts and
n8n-render's compute-week-end.js tests.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Generator
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from src.api.routes import wbr_ledger as wbr_ledger_module
from src.api.routes.wbr_ledger import WbrLedgerSummary, most_recent_sunday
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


@pytest.fixture(autouse=True)
def _freeze_today(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze "today" (LA calendar date) to `WEEK_END` for every test in this
    module, decoupling the suite from the real wall-clock date now that the
    endpoint rejects any `week_end` in the future (round-2 fix directives
    P1-a1b/P1-a1c/REQ-WBR-LED-013). `WEEK_END` == the frozen "today" sits
    exactly on the "equal to today is allowed" boundary, so every existing
    test that passes `WEEK_END` (or omits `week_end` and expects the most
    recent Sunday before it) keeps working regardless of when the suite
    actually runs. Individual tests that need a different "today" (the
    golden-date table, the 120-day boundary tests, the new future-date
    tests) call `monkeypatch.setattr(..., "_today_la", ...)` again inside
    the test body, which overrides this default."""
    monkeypatch.setattr(wbr_ledger_module, "_today_la", lambda: date(2026, 7, 26))


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
        """REQ-ID: REQ-WBR-LED-002 — n8n's INGEST_API_KEY is accepted for the
        default entity=personal."""
        r = client.get(URL, headers={"X-Api-Key": _IK}, params={"week_end": WEEK_END})
        assert r.status_code == 200

    def test_accepts_api_key(self, client: TestClient) -> None:
        """REQ-ID: REQ-WBR-LED-002 — the browser API_KEY is accepted too."""
        r = client.get(URL, headers={"X-Api-Key": _AK}, params={"week_end": WEEK_END})
        assert r.status_code == 200

    def test_ingest_key_rejected_for_non_personal_entity(
        self, client: TestClient
    ) -> None:
        """REQ-ID: REQ-WBR-LED-013 — INGEST_API_KEY + entity=sparkry -> 403."""
        r = client.get(
            URL,
            headers={"X-Api-Key": _IK},
            params={"week_end": WEEK_END, "entity": "sparkry"},
        )
        assert r.status_code == 403

    def test_ingest_key_rejected_for_blackline_entity(
        self, client: TestClient
    ) -> None:
        """REQ-ID: REQ-WBR-LED-013 — INGEST_API_KEY + entity=blackline -> 403."""
        r = client.get(
            URL,
            headers={"X-Api-Key": _IK},
            params={"week_end": WEEK_END, "entity": "blackline"},
        )
        assert r.status_code == 403

    def test_ingest_key_explicit_personal_entity_allowed(
        self, client: TestClient
    ) -> None:
        """REQ-ID: REQ-WBR-LED-013 — INGEST_API_KEY + explicit entity=personal
        (not just the default) is still allowed."""
        r = client.get(
            URL,
            headers={"X-Api-Key": _IK},
            params={"week_end": WEEK_END, "entity": "personal"},
        )
        assert r.status_code == 200

    def test_api_key_allowed_for_any_entity(self, client: TestClient) -> None:
        """REQ-ID: REQ-WBR-LED-013 — the full API_KEY may query any entity."""
        for entity in ("sparkry", "blackline", "personal"):
            r = client.get(
                URL,
                headers={"X-Api-Key": _AK},
                params={"week_end": WEEK_END, "entity": entity},
            )
            assert r.status_code == 200, (entity, r.text)


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
        """REQ-ID: REQ-WBR-LED-004 — omitted week_end defaults to the most
        recent Sunday STRICTLY BEFORE today, in America/Los_Angeles. "Today"
        is frozen by the `_freeze_today` autouse fixture rather than the real
        wall clock, so this test is deterministic regardless of when the
        suite runs."""
        r = client.get(URL, headers=AUTH)
        assert r.status_code == 200, r.text
        expected = most_recent_sunday(wbr_ledger_module._today_la()).isoformat()
        assert r.json()["week_end"] == expected

    @pytest.mark.parametrize(
        ("reference", "sunday"),
        [
            # Sunday -> the PRIOR Sunday (week not closed yet), NOT itself —
            # round-2 fix directive P1-a1b; n8n's compute-week-end.js agrees.
            (date(2026, 7, 26), date(2026, 7, 19)),  # Sunday
            (date(2026, 7, 27), date(2026, 7, 26)),  # Monday
            (date(2026, 7, 29), date(2026, 7, 26)),  # Wednesday (mid-week)
            (date(2026, 8, 1), date(2026, 7, 26)),  # Saturday
            (date(2026, 3, 8), date(2026, 3, 1)),  # Sunday, US DST spring-forward day
            (date(2026, 11, 1), date(2026, 10, 25)),  # Sunday, US DST fall-back day
        ],
    )
    def test_most_recent_sunday(self, reference: date, sunday: date) -> None:
        """REQ-ID: REQ-WBR-LED-004 — golden-date table shared across all three
        repos (round-2 fix directive P1-a1b): helper handles every weekday
        plus DST boundary Sundays with strictly-before semantics."""
        assert most_recent_sunday(reference) == sunday

    def test_utc_evening_still_resolves_to_la_calendar_sunday(self) -> None:
        """Golden case: 2026-07-27T05:30:00Z is 2026-07-26 22:30 PT — already
        Monday in UTC but still Sunday evening in America/Los_Angeles. The
        LA calendar date must be used, so `most_recent_sunday` still treats
        it as a Sunday reference (week not closed) and returns the PRIOR
        Sunday, not the coming one."""
        utc_instant = datetime(2026, 7, 27, 5, 30, tzinfo=ZoneInfo("UTC"))
        la_date = utc_instant.astimezone(ZoneInfo("America/Los_Angeles")).date()
        assert la_date == date(2026, 7, 26)
        assert most_recent_sunday(la_date) == date(2026, 7, 19)

    def test_default_uses_la_calendar_date_not_utc(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """REQ-ID: REQ-WBR-LED-004 — the endpoint's default resolution is
        pinned to the LA calendar date, exercised end-to-end by monkeypatching
        `_today_la` to the UTC/LA boundary-evening golden case above."""
        monkeypatch.setattr(wbr_ledger_module, "_today_la", lambda: date(2026, 7, 26))
        r = client.get(URL, headers=AUTH)
        assert r.status_code == 200, r.text
        assert r.json()["week_end"] == "2026-07-19"

    def test_malformed_week_end_rejected(self, client: TestClient) -> None:
        """REQ-ID: REQ-WBR-LED-009 — non-ISO week_end -> 422."""
        for bad in ("07/26/2026", "2026-13-01", "sunday", "2026-07-26T00:00:00"):
            r = client.get(URL, headers=AUTH, params={"week_end": bad})
            assert r.status_code == 422, f"{bad!r}: {r.status_code}"

    def test_week_end_older_than_120_days_rejected(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """REQ-ID: REQ-WBR-LED-013 — week_end more than 120 days old -> 422,
        regardless of which credential is used."""
        monkeypatch.setattr(wbr_ledger_module, "_today_la", lambda: date(2026, 7, 26))
        too_old = (date(2026, 7, 26) - timedelta(days=121)).isoformat()
        r = client.get(URL, headers=AUTH, params={"week_end": too_old})
        assert r.status_code == 422
        r = client.get(URL, headers={"X-Api-Key": _AK}, params={"week_end": too_old})
        assert r.status_code == 422

    def test_week_end_exactly_120_days_old_allowed(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """REQ-ID: REQ-WBR-LED-013 — exactly 120 days old is still allowed."""
        monkeypatch.setattr(wbr_ledger_module, "_today_la", lambda: date(2026, 7, 26))
        exactly_120 = (date(2026, 7, 26) - timedelta(days=120)).isoformat()
        r = client.get(URL, headers=AUTH, params={"week_end": exactly_120})
        assert r.status_code == 200, r.text

    def test_week_end_in_the_future_rejected(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """REQ-ID: REQ-WBR-LED-013 — week_end after today (LA calendar date)
        -> 422, regardless of which credential is used (round-2 fix
        directives P1-a1b/P1-a1c: "never in the future")."""
        monkeypatch.setattr(wbr_ledger_module, "_today_la", lambda: date(2026, 7, 26))
        tomorrow = (date(2026, 7, 26) + timedelta(days=1)).isoformat()
        r = client.get(URL, headers=AUTH, params={"week_end": tomorrow})
        assert r.status_code == 422
        r = client.get(URL, headers={"X-Api-Key": _AK}, params={"week_end": tomorrow})
        assert r.status_code == 422

    def test_week_end_equal_to_today_allowed(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """REQ-ID: REQ-WBR-LED-013 — week_end == today (LA calendar date) is
        the boundary and must still be allowed; only strictly-future dates
        are rejected."""
        monkeypatch.setattr(wbr_ledger_module, "_today_la", lambda: date(2026, 7, 26))
        r = client.get(URL, headers=AUTH, params={"week_end": "2026-07-26"})
        assert r.status_code == 200, r.text



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
        """REQ-ID: REQ-WBR-LED-006 — ?entity=sparkry honored (under the full
        API_KEY; the ingest key is scoped to entity=personal only, see
        REQ-WBR-LED-013 / TestAuth.test_ingest_key_rejected_for_non_personal_entity)."""
        _make_tx(description="personal", amount=Decimal("-10.00"))
        _make_tx(
            description="business",
            amount=Decimal("-20.00"),
            entity=Entity.SPARKRY.value,
        )
        r = client.get(
            URL,
            headers={"X-Api-Key": _AK},
            params={"week_end": WEEK_END, "entity": "sparkry"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
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


# ---------------------------------------------------------------------------
# Transfers (round-2 fix directive P1-tfr3)
# ---------------------------------------------------------------------------


class TestTransfers:
    def test_transfer_row_visible_with_transfer_category(
        self, client: TestClient
    ) -> None:
        """REQ-ID: REQ-WBR-LED-012 — a transfer row still appears in
        `transactions`, labeled category "Transfer"."""
        _make_tx(
            description="Internal Move to Savings",
            amount=Decimal("-500.00"),
            direction=Direction.TRANSFER.value,
            tax_category=None,
        )
        body = _get(client)
        (row,) = body["transactions"]
        assert row["name"] == "Internal Move to Savings"
        assert row["category"] == "Transfer"
        assert row["amount"] == -500.00

    def test_transfer_row_excluded_from_inflow_outflow_totals(
        self, client: TestClient
    ) -> None:
        """REQ-ID: REQ-WBR-LED-012 — a positive AND a negative transfer row
        contribute $0 to inflow_total/outflow_total, even though a real
        expense/income in the same window is still counted normally."""
        _make_tx(
            description="Transfer Out",
            amount=Decimal("-1000.00"),
            direction=Direction.TRANSFER.value,
            tax_category=None,
        )
        _make_tx(
            description="Transfer In",
            amount=Decimal("1000.00"),
            direction=Direction.TRANSFER.value,
            tax_category=None,
        )
        _make_tx(
            description="Real Expense",
            amount=Decimal("-25.00"),
            direction=Direction.EXPENSE.value,
        )
        _make_tx(
            description="Real Income",
            amount=Decimal("500.00"),
            direction=Direction.INCOME.value,
            tax_category=None,
        )
        body = _get(client)
        assert body["outflow_total"] == 25.00
        assert body["inflow_total"] == 500.00
        names = {t["name"] for t in body["transactions"]}
        assert names == {"Transfer Out", "Transfer In", "Real Expense", "Real Income"}


# ---------------------------------------------------------------------------
# Shared cross-repo golden fixture (round-2 fix directive P2-g7h)
# ---------------------------------------------------------------------------

_GOLDEN_PATH = (
    Path(__file__).resolve().parents[3]
    / "tests"
    / "fixtures"
    / "wbr-golden"
    / "wbr-ledger-golden.json"
)


class TestGoldenFixture:
    """REQ-ID: REQ-WBR-LED-001/012 — the WbrLedgerSummary response model
    accepts the identical golden fixture shared with sparkry-crm-wbr and
    n8n-render, so the three-repo contract is machine-checked against one
    file instead of three independently hand-written fixtures that can
    silently drift."""

    def test_golden_fixture_validates_against_the_response_model(self) -> None:
        raw = json.loads(_GOLDEN_PATH.read_text())
        # Strip the doc-only _comment key before validating against the
        # strict (extra-forbidding by default in pydantic v2? no — pydantic
        # allows extra unless configured) response model.
        payload = {k: v for k, v in raw.items() if k != "_comment"}
        parsed = WbrLedgerSummary.model_validate(payload)
        assert parsed.week_end == "2026-07-26"
        assert parsed.entity == "personal"
        assert parsed.truncated is True
        assert len(parsed.transactions) == 8

    def test_golden_fixture_transfer_rows_are_present_but_excluded_from_totals(
        self,
    ) -> None:
        """The fixture's transfer pair (+/- 800) AND its card-payment-named
        transfer row (P0-001/P2-h8d) must NOT be counted in
        inflow_total/outflow_total (round-2 fix directive P1-tfr3) — this
        pins the fixture's own internal consistency with that rule so a
        future edit to the fixture can't silently drift from it."""
        raw = json.loads(_GOLDEN_PATH.read_text())
        transfer_rows = [t for t in raw["transactions"] if t["category"] == "Transfer"]
        assert len(transfer_rows) == 3
        non_transfer_income = sum(
            t["amount"]
            for t in raw["transactions"]
            if t["category"] != "Transfer" and t["amount"] > 0
        )
        non_transfer_expense = sum(
            -t["amount"]
            for t in raw["transactions"]
            if t["category"] != "Transfer" and t["amount"] < 0
        )
        assert non_transfer_income == raw["inflow_total"]
        assert round(non_transfer_expense, 2) == raw["outflow_total"]

    def test_golden_fixture_card_payment_named_row_is_a_transfer(self) -> None:
        """P0-001/P2-h8d: the fixture's "AUTOPAY PAYMENT - THANK YOU" row is
        `category: "Transfer"` (as the endpoint emits once REQ-WBR-LED-015
        lands) so the three-repo contract exercises the exact payload shape
        that used to be double-excluded by the CRM (P0-001/P0-a1c)."""
        raw = json.loads(_GOLDEN_PATH.read_text())
        card_payment_row = next(
            t for t in raw["transactions"] if "AUTOPAY" in t["name"]
        )
        assert card_payment_row["category"] == "Transfer"
        assert card_payment_row["amount"] > 0
