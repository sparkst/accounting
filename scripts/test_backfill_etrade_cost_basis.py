"""REQ-FIX-WLT-003 (spec §3.3) — E*TRADE cost_basis backfill tests.

Covers: dry-run writes nothing; --apply backfills cost_basis in place and
writes exactly one AuditEvent per changed row; re-run is idempotent; only
E*TRADE accounts are touched; rows lacking avg_cost/quantity are skipped;
raw_data is never mutated.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from scripts.backfill_etrade_cost_basis import backfill

# Import models referenced via FK so create_all sees their tables.
from src.models import plaid as _plaid  # noqa: F401
from src.models.audit_event import AuditEvent
from src.models.base import Base
from src.models.brokerage import Account, PositionSnapshot
from src.models.enums import AccountType, Broker, Entity


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _seed_account(
    s: Session, *, broker: str, account_id: str, number: str
) -> Account:
    acct = Account(
        id=account_id,
        broker=broker,
        account_number=number,
        account_name="Test",
        account_type=AccountType.TAXABLE.value,
        entity=Entity.PERSONAL.value,
    )
    s.add(acct)
    s.commit()
    return acct


def _seed_snapshot(
    s: Session,
    *,
    account_id: str,
    symbol: str,
    quantity: Decimal | None,
    avg_cost_basis: Decimal | None,
    cost_basis: Decimal | None,
    row_hash: str,
) -> PositionSnapshot:
    snap = PositionSnapshot(
        account_id=account_id,
        as_of=datetime(2026, 5, 1),
        symbol=symbol,
        quantity=quantity,
        price=Decimal("100.00"),
        market_value=Decimal("100.00"),
        cost_basis=cost_basis,
        avg_cost_basis=avg_cost_basis,
        source_file="PortfolioDownload.csv",
        source_row_hash=row_hash,
        raw_data={"row": ["AAPL"], "as_of_source": "mtime"},
    )
    s.add(snap)
    s.commit()
    return snap


def test_dry_run_writes_nothing(session: Session) -> None:
    """DRY-RUN reports would-change but leaves cost_basis NULL and no AuditEvents."""
    _seed_account(session, broker=Broker.ETRADE.value, account_id="e1", number="6354")
    snap = _seed_snapshot(
        session,
        account_id="e1",
        symbol="AAPL",
        quantity=Decimal("100.0000"),
        avg_cost_basis=Decimal("140.0000"),
        cost_basis=None,
        row_hash="h1",
    )

    result = backfill(session, apply=False)
    assert result["examined"] == 1
    assert result["changed"] == 1

    session.refresh(snap)
    assert snap.cost_basis is None
    assert session.scalars(select(AuditEvent)).all() == []


def test_apply_backfills_and_writes_one_audit_event_per_row(
    session: Session,
) -> None:
    """--apply sets cost_basis = avg×qty (cents) and writes one AuditEvent each."""
    _seed_account(session, broker=Broker.ETRADE.value, account_id="e1", number="6354")
    snap_a = _seed_snapshot(
        session,
        account_id="e1",
        symbol="AAPL",
        quantity=Decimal("100.0000"),
        avg_cost_basis=Decimal("140.0000"),
        cost_basis=None,
        row_hash="h1",
    )
    snap_m = _seed_snapshot(
        session,
        account_id="e1",
        symbol="MSFT",
        quantity=Decimal("2630.5850"),
        avg_cost_basis=Decimal("333.9237"),
        cost_basis=None,
        row_hash="h2",
    )

    result = backfill(session, apply=True)
    assert result == {"examined": 2, "changed": 2, "errors": 0}

    session.refresh(snap_a)
    session.refresh(snap_m)
    assert snap_a.cost_basis == Decimal("14000.00")
    expected_m = (Decimal("2630.5850") * Decimal("333.9237")).quantize(Decimal("0.01"))
    assert snap_m.cost_basis == expected_m

    # raw_data untouched.
    assert snap_a.raw_data == {"row": ["AAPL"], "as_of_source": "mtime"}

    events = session.scalars(select(AuditEvent)).all()
    assert len(events) == 2
    for ev in events:
        assert ev.entity_type == "position_snapshot"
        assert ev.transaction_id is None
        assert ev.field_changed == "cost_basis"
        assert ev.changed_by == "script:etrade_cost_basis_backfill"
        assert ev.old_value is None
    by_entity = {ev.entity_id: ev for ev in events}
    assert by_entity[snap_a.id].new_value == "14000.00"
    assert by_entity[snap_m.id].new_value == str(expected_m)


def test_reapply_is_idempotent(session: Session) -> None:
    """A second --apply run writes nothing (rows already have cost_basis)."""
    _seed_account(session, broker=Broker.ETRADE.value, account_id="e1", number="6354")
    _seed_snapshot(
        session,
        account_id="e1",
        symbol="AAPL",
        quantity=Decimal("100.0000"),
        avg_cost_basis=Decimal("140.0000"),
        cost_basis=None,
        row_hash="h1",
    )

    first = backfill(session, apply=True)
    assert first["changed"] == 1

    second = backfill(session, apply=True)
    assert second == {"examined": 0, "changed": 0, "errors": 0}
    # Still exactly one AuditEvent — the no-op run added none.
    assert len(session.scalars(select(AuditEvent)).all()) == 1


def test_only_etrade_accounts_touched(session: Session) -> None:
    """A Schwab snapshot with null cost_basis must be left untouched."""
    _seed_account(session, broker=Broker.ETRADE.value, account_id="e1", number="6354")
    _seed_account(
        session, broker=Broker.SCHWAB.value, account_id="s1", number="SCH-1"
    )
    etrade = _seed_snapshot(
        session,
        account_id="e1",
        symbol="AAPL",
        quantity=Decimal("10.0000"),
        avg_cost_basis=Decimal("150.0000"),
        cost_basis=None,
        row_hash="h1",
    )
    schwab = _seed_snapshot(
        session,
        account_id="s1",
        symbol="VTI",
        quantity=Decimal("10.0000"),
        avg_cost_basis=Decimal("200.0000"),
        cost_basis=None,
        row_hash="h2",
    )

    result = backfill(session, apply=True)
    assert result["changed"] == 1

    session.refresh(etrade)
    session.refresh(schwab)
    assert etrade.cost_basis == Decimal("1500.00")
    assert schwab.cost_basis is None


def test_rows_missing_inputs_are_skipped(session: Session) -> None:
    """Rows missing avg_cost or quantity are excluded by the predicate."""
    _seed_account(session, broker=Broker.ETRADE.value, account_id="e1", number="6354")
    # avg_cost present but quantity NULL → skipped.
    _seed_snapshot(
        session,
        account_id="e1",
        symbol="AAPL",
        quantity=None,
        avg_cost_basis=Decimal("140.0000"),
        cost_basis=None,
        row_hash="h1",
    )
    # quantity present but avg_cost NULL → skipped.
    _seed_snapshot(
        session,
        account_id="e1",
        symbol="MSFT",
        quantity=Decimal("5.0000"),
        avg_cost_basis=None,
        cost_basis=None,
        row_hash="h2",
    )

    result = backfill(session, apply=True)
    assert result == {"examined": 0, "changed": 0, "errors": 0}
    assert session.scalars(select(AuditEvent)).all() == []
