"""REQ-PERF-010..015 — performance endpoints + pair confirm/reject + candidates."""

from __future__ import annotations

import uuid
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

# Register all models so create_all builds the full schema.
import src.models.plaid as _plaid  # noqa: F401
from src.models import brokerage as _brokerage  # noqa: F401
from src.models import history as _history  # noqa: F401
from src.models.audit_event import (
    ENTITY_TYPE_BROKERAGE_TRANSACTION,
    AuditEvent,
)
from src.models.base import Base
from src.models.brokerage import Account, BrokerageTransaction, PositionSnapshot
from src.models.enums import (
    AccountType,
    Broker,
    BrokerageTxStatus,
    CanonicalAction,
    CashFlowType,
    Entity,
)
from src.models.history import HistoricalPrice


def _make_engine() -> Any:
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


def _make_session(engine: Any) -> Session:
    return sessionmaker(bind=engine, expire_on_commit=False)()


@pytest.fixture()
def session(engine: Any) -> Generator[Session, None, None]:
    s = _make_session(engine)
    yield s
    s.close()


@pytest.fixture()
def client(engine: Any) -> Generator[TestClient, None, None]:
    """Build a TestClient with get_db overridden to use our test engine."""
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
        from src.api.routes.brokerage import get_db

        app.dependency_overrides[get_db] = _test_get_db
        with TestClient(app) as c:
            yield c
        app.dependency_overrides.clear()


# ── Fixtures ───────────────────────────────────────────────────────────


def _acct(s: Session, acct_id: str = "acct-1", num: str = "NUM-1") -> Account:
    a = Account(
        id=acct_id,
        broker=Broker.SCHWAB.value,
        account_number=num,
        account_name=acct_id,
        account_type=AccountType.TAXABLE.value,
        entity=Entity.PERSONAL.value,
    )
    s.add(a)
    s.commit()
    return a


def _tx(
    s: Session,
    *,
    acct_id: str,
    action: CanonicalAction,
    amount: Decimal,
    trade_date: date,
    symbol: str | None = None,
    paired_id: str | None = None,
    tx_id: str | None = None,
    cash_flow_type: CashFlowType = CashFlowType.NONE,
) -> BrokerageTransaction:
    the_id = tx_id or str(uuid.uuid4())
    tx = BrokerageTransaction(
        id=the_id,
        account_id=acct_id,
        trade_date=trade_date,
        action=action.value,
        canonical_action=action.value,
        symbol=symbol,
        amount=amount,
        status=BrokerageTxStatus.IMPORTED.value,
        source_file="test.csv",
        source_row_hash=the_id,
        raw_data={},
        paired_transaction_id=paired_id,
        cash_flow_type=cash_flow_type.value,
    )
    s.add(tx)
    s.commit()
    return tx


def _snap(
    s: Session,
    *,
    acct_id: str,
    symbol: str | None,
    as_of: datetime,
    qty: Decimal,
    market_value: Decimal,
) -> PositionSnapshot:
    snap = PositionSnapshot(
        id=str(uuid.uuid4()),
        account_id=acct_id,
        symbol=symbol,
        as_of=as_of,
        quantity=qty,
        market_value=market_value,
        source_file="test.csv",
        source_row_hash=str(uuid.uuid4()),
        raw_data={},
    )
    s.add(snap)
    s.commit()
    return snap


def _price(s: Session, *, symbol: str, trade_date: date, close: Decimal) -> None:
    s.add(
        HistoricalPrice(
            symbol=symbol, trade_date=trade_date, close=close, source="test"
        )
    )
    s.commit()


# ── Tests ──────────────────────────────────────────────────────────────


TODAY = date(2026, 5, 15)


def test_holding_endpoint_returns_series_and_summary(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.api.routes.brokerage as broker_routes

    monkeypatch.setattr(broker_routes, "_today", lambda: TODAY, raising=False)

    _acct(session)
    # Deposit 60 days ago, buy 50 shares of AAPL @ 100
    deposit_date = TODAY - timedelta(days=60)
    _tx(
        session,
        acct_id="acct-1",
        action=CanonicalAction.CONTRIBUTION,
        amount=Decimal("5000.00"),
        trade_date=deposit_date,
        cash_flow_type=CashFlowType.EXTERNAL_IN,
    )
    _tx(
        session,
        acct_id="acct-1",
        action=CanonicalAction.BUY,
        amount=Decimal("-5000.00"),
        trade_date=deposit_date,
        symbol="AAPL",
    )
    _snap(
        session,
        acct_id="acct-1",
        symbol="AAPL",
        as_of=datetime.combine(deposit_date, datetime.min.time()),
        qty=Decimal("50"),
        market_value=Decimal("5000"),
    )
    _snap(
        session,
        acct_id="acct-1",
        symbol="AAPL",
        as_of=datetime.combine(TODAY, datetime.min.time()),
        qty=Decimal("50"),
        market_value=Decimal("5500"),
    )
    _price(session, symbol="AAPL", trade_date=deposit_date, close=Decimal("100"))
    _price(session, symbol="AAPL", trade_date=TODAY, close=Decimal("110"))
    # SPY benchmark
    _price(session, symbol="SPY", trade_date=deposit_date, close=Decimal("500"))
    _price(session, symbol="SPY", trade_date=TODAY, close=Decimal("525"))

    r = client.get("/api/brokerage/performance/holding/AAPL")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["symbol"] == "AAPL"
    assert body["view"] == "outside_money"
    assert isinstance(body["series"], list)
    assert len(body["series"]) > 0
    summary = body["summary"]
    for k in (
        "twr",
        "twr_annualized",
        "xirr",
        "benchmark_twr",
        "current_value",
        "total_principal",
        "total_growth",
    ):
        assert k in summary


def test_holding_endpoint_404_unknown_symbol(client: TestClient) -> None:
    r = client.get("/api/brokerage/performance/holding/NOPE")
    assert r.status_code == 404


def test_account_endpoint_404_unknown_account(client: TestClient) -> None:
    r = client.get("/api/brokerage/performance/account/does-not-exist")
    assert r.status_code == 404


def test_portfolio_endpoint_includes_tracked_fields(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.api.routes.brokerage as broker_routes

    monkeypatch.setattr(broker_routes, "_today", lambda: TODAY, raising=False)
    _acct(session)
    # one classified tx so tracked_value > 0
    _tx(
        session,
        acct_id="acct-1",
        action=CanonicalAction.CONTRIBUTION,
        amount=Decimal("1000"),
        trade_date=TODAY - timedelta(days=10),
        cash_flow_type=CashFlowType.EXTERNAL_IN,
    )
    _snap(
        session,
        acct_id="acct-1",
        symbol=None,
        as_of=datetime.combine(TODAY, datetime.min.time()),
        qty=Decimal("0"),
        market_value=Decimal("1000"),
    )

    r = client.get("/api/brokerage/performance/portfolio")
    assert r.status_code == 200, r.text
    body = r.json()
    summary = body["summary"]
    assert summary["tracked_value"] is not None
    assert summary["total_value"] is not None
    assert summary["tracked_pct"] is not None


def test_periods_endpoint_omits_periods_outside_window(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.api.routes.brokerage as broker_routes

    monkeypatch.setattr(broker_routes, "_today", lambda: TODAY, raising=False)

    _acct(session)
    # First tx 60 days ago — 1M and YTD windows should be inside, 5Y/10Y outside.
    first_date = TODAY - timedelta(days=60)
    _tx(
        session,
        acct_id="acct-1",
        action=CanonicalAction.CONTRIBUTION,
        amount=Decimal("1000"),
        trade_date=first_date,
        cash_flow_type=CashFlowType.EXTERNAL_IN,
    )
    _snap(
        session,
        acct_id="acct-1",
        symbol=None,
        as_of=datetime.combine(first_date, datetime.min.time()),
        qty=Decimal("0"),
        market_value=Decimal("1000"),
    )
    _snap(
        session,
        acct_id="acct-1",
        symbol=None,
        as_of=datetime.combine(TODAY, datetime.min.time()),
        qty=Decimal("0"),
        market_value=Decimal("1100"),
    )

    r = client.get("/api/brokerage/performance/periods?scope=portfolio")
    assert r.status_code == 200, r.text
    labels = {row["period"] for row in r.json()["rows"]}
    # 5Y / 10Y predate the 60-day window → omitted
    assert "5Y" not in labels
    assert "10Y" not in labels
    # 1M and ITD should be present
    assert "1M" in labels
    assert "ITD" in labels


def test_pair_confirm_flips_both_legs_to_internal(
    client: TestClient, session: Session
) -> None:
    _acct(session, acct_id="acct-1", num="N1")
    _acct(session, acct_id="acct-2", num="N2")
    d = date(2026, 4, 1)
    tx_a = _tx(
        session,
        acct_id="acct-1",
        action=CanonicalAction.TRANSFER,
        amount=Decimal("-1000.00"),
        trade_date=d,
        tx_id="tx-a",
        cash_flow_type=CashFlowType.EXTERNAL_OUT,
    )
    tx_b = _tx(
        session,
        acct_id="acct-2",
        action=CanonicalAction.TRANSFER,
        amount=Decimal("1000.00"),
        trade_date=d,
        tx_id="tx-b",
        cash_flow_type=CashFlowType.EXTERNAL_IN,
    )
    assert tx_a.paired_transaction_id is None
    assert tx_b.paired_transaction_id is None

    r = client.post(
        f"/api/brokerage/transactions/{tx_a.id}/pair",
        json={"paired_transaction_id": tx_b.id, "action": "confirm"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tx_a"]["paired_transaction_id"] == tx_b.id
    assert body["tx_b"]["paired_transaction_id"] == tx_a.id
    assert body["tx_a"]["cash_flow_type"] == CashFlowType.INTERNAL.value
    assert body["tx_b"]["cash_flow_type"] == CashFlowType.INTERNAL.value

    # Verify in DB (in a fresh session)
    session.expire_all()
    refetched_a = session.get(BrokerageTransaction, "tx-a")
    refetched_b = session.get(BrokerageTransaction, "tx-b")
    assert refetched_a is not None and refetched_b is not None
    assert refetched_a.paired_transaction_id == "tx-b"
    assert refetched_b.paired_transaction_id == "tx-a"
    assert refetched_a.cash_flow_type == CashFlowType.INTERNAL.value


def test_pair_reject_writes_audit_event(
    client: TestClient, session: Session
) -> None:
    _acct(session, acct_id="acct-1", num="N1")
    _acct(session, acct_id="acct-2", num="N2")
    d = date(2026, 4, 1)
    tx_a = _tx(
        session,
        acct_id="acct-1",
        action=CanonicalAction.TRANSFER,
        amount=Decimal("-500.00"),
        trade_date=d,
        tx_id="rej-a",
    )
    tx_b = _tx(
        session,
        acct_id="acct-2",
        action=CanonicalAction.TRANSFER,
        amount=Decimal("500.00"),
        trade_date=d,
        tx_id="rej-b",
    )

    r = client.post(
        f"/api/brokerage/transactions/{tx_a.id}/pair",
        json={"paired_transaction_id": tx_b.id, "action": "reject"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"rejected": True}

    # AuditEvent row exists
    session.expire_all()
    events = (
        session.query(AuditEvent)
        .filter(
            AuditEvent.field_changed == "transfer_pair_rejected",
            AuditEvent.entity_type == ENTITY_TYPE_BROKERAGE_TRANSACTION,
        )
        .all()
    )
    assert len(events) >= 1
    # And is_rejected sees it
    from scripts.auto_pair_transfers import find_candidates, is_rejected

    assert is_rejected(session, tx_a.id, tx_b.id)
    # find_candidates excludes it
    cands = find_candidates(session)
    keys = {frozenset({c.tx_a_id, c.tx_b_id}) for c in cands}
    assert frozenset({tx_a.id, tx_b.id}) not in keys


def test_unpaired_transfers_returns_candidates(
    client: TestClient, session: Session
) -> None:
    _acct(session, acct_id="acct-1", num="N1")
    _acct(session, acct_id="acct-2", num="N2")
    d = date(2026, 4, 1)
    tx_a = _tx(
        session,
        acct_id="acct-1",
        action=CanonicalAction.TRANSFER,
        amount=Decimal("-1234.56"),
        trade_date=d,
        tx_id="u-a",
    )
    tx_b = _tx(
        session,
        acct_id="acct-2",
        action=CanonicalAction.TRANSFER,
        amount=Decimal("1234.56"),
        trade_date=d,
        tx_id="u-b",
    )

    r = client.get("/api/brokerage/performance/unpaired-transfers")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["candidates"]) == 1
    row = body["candidates"][0]
    assert row["confidence"] == 1.0
    ids = {row["tx_a"]["id"], row["tx_b"]["id"]}
    assert ids == {tx_a.id, tx_b.id}
    assert "amount match" in row["reason"]


def test_pair_confirm_idempotent(client: TestClient, session: Session) -> None:
    _acct(session, acct_id="acct-1", num="N1")
    _acct(session, acct_id="acct-2", num="N2")
    d = date(2026, 4, 1)
    tx_a = _tx(
        session,
        acct_id="acct-1",
        action=CanonicalAction.TRANSFER,
        amount=Decimal("-1000.00"),
        trade_date=d,
        tx_id="idem-a",
    )
    tx_b = _tx(
        session,
        acct_id="acct-2",
        action=CanonicalAction.TRANSFER,
        amount=Decimal("1000.00"),
        trade_date=d,
        tx_id="idem-b",
    )

    # First confirm
    r1 = client.post(
        f"/api/brokerage/transactions/{tx_a.id}/pair",
        json={"paired_transaction_id": tx_b.id, "action": "confirm"},
    )
    assert r1.status_code == 200, r1.text

    # Second confirm — should be no-op (no new audit rows, same response shape)
    session.expire_all()
    audit_count_before = (
        session.query(AuditEvent)
        .filter(AuditEvent.field_changed == "paired_transaction_id")
        .count()
    )
    r2 = client.post(
        f"/api/brokerage/transactions/{tx_a.id}/pair",
        json={"paired_transaction_id": tx_b.id, "action": "confirm"},
    )
    assert r2.status_code == 200, r2.text
    session.expire_all()
    audit_count_after = (
        session.query(AuditEvent)
        .filter(AuditEvent.field_changed == "paired_transaction_id")
        .count()
    )
    assert audit_count_after == audit_count_before
