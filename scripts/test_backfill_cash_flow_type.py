"""REQ-PERF-003 tests — backfill idempotence + per-row error isolation."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from scripts.backfill_cash_flow_type import backfill

# Import models that are referenced via FK so create_all sees their tables.
from src.models import plaid as _plaid  # noqa: F401
from src.models.base import Base
from src.models.brokerage import Account, BrokerageTransaction
from src.models.enums import (
    AccountType,
    Broker,
    BrokerageTxStatus,
    CanonicalAction,
    CashFlowType,
    Entity,
)


@pytest.fixture
def session() -> Session:
    """In-memory SQLite session with schema created."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _seed_account(s: Session, account_id: str = "acct-1") -> Account:
    acct = Account(
        id=account_id,
        broker=Broker.SCHWAB.value,
        account_number=f"X-{account_id}",
        account_name="Test",
        account_type=AccountType.TAXABLE.value,
        entity=Entity.PERSONAL.value,
    )
    s.add(acct)
    s.commit()
    return acct


def _seed_tx(
    s: Session,
    *,
    account_id: str,
    canonical_action: CanonicalAction,
    amount: Decimal | None = None,
    symbol: str | None = None,
    source_row_hash: str = "hash1",
    paired_transaction_id: str | None = None,
    tx_id: str | None = None,
) -> BrokerageTransaction:
    tx = BrokerageTransaction(
        id=tx_id,
        account_id=account_id,
        trade_date=date(2026, 1, 15),
        action=canonical_action.value,
        canonical_action=canonical_action.value,
        symbol=symbol,
        amount=amount,
        status=BrokerageTxStatus.IMPORTED.value,
        source_file="test.csv",
        source_row_hash=source_row_hash,
        raw_data={"test": True},
        paired_transaction_id=paired_transaction_id,
    )
    s.add(tx)
    s.commit()
    return tx


def test_backfill_dry_run_does_not_write(session: Session) -> None:
    """REQ-PERF-003: --dry-run reports changes but writes nothing."""
    acct = _seed_account(session)
    tx = _seed_tx(
        session,
        account_id=acct.id,
        canonical_action=CanonicalAction.CONTRIBUTION,
        amount=Decimal("1000.00"),
    )
    result = backfill(session, apply=False)
    assert result["changed_total"] == 1
    session.refresh(tx)
    assert tx.cash_flow_type == CashFlowType.NONE.value  # not written


def test_backfill_apply_writes_and_is_idempotent(session: Session) -> None:
    """REQ-PERF-003: --apply writes; rerun is a no-op."""
    acct = _seed_account(session)
    tx = _seed_tx(
        session,
        account_id=acct.id,
        canonical_action=CanonicalAction.CONTRIBUTION,
        amount=Decimal("1000.00"),
    )
    result = backfill(session, apply=True)
    assert result["changed_total"] == 1
    session.refresh(tx)
    assert tx.cash_flow_type == CashFlowType.EXTERNAL_IN.value

    # Re-run is a no-op.
    result2 = backfill(session, apply=True)
    assert result2["changed_total"] == 0
    assert result2["unchanged_total"] == 1


def test_backfill_per_row_error_isolation(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQ-PERF-003: one bad row doesn't halt the batch (CLAUDE.md pattern).

    Two seeded rows. ``classify`` is monkey-patched to raise ``ClassifyError``
    on the second row; the first row must still be written.
    """
    from src.analytics import classify as classify_mod
    from src.analytics.classify import ClassifyError

    acct = _seed_account(session)
    good = _seed_tx(
        session,
        account_id=acct.id,
        canonical_action=CanonicalAction.DISTRIBUTION,
        amount=Decimal("-500.00"),
        source_row_hash="ok",
    )
    _seed_tx(
        session,
        account_id=acct.id,
        canonical_action=CanonicalAction.CONTRIBUTION,
        amount=Decimal("1000"),
        source_row_hash="will-blow-up",
    )

    real_classify = classify_mod.classify
    call_count = {"n": 0}

    def flaky_classify(tx, scope):  # noqa: ANN001
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise ClassifyError("simulated failure on row 2")
        return real_classify(tx, scope)

    monkeypatch.setattr(
        "scripts.backfill_cash_flow_type.classify", flaky_classify
    )

    result = backfill(session, apply=True)
    session.refresh(good)
    assert good.cash_flow_type == CashFlowType.EXTERNAL_OUT.value
    assert result["changed_total"] == 1
    assert result["errors"] == 1


def test_backfill_classifies_all_action_types(session: Session) -> None:
    """REQ-PERF-003: cross-action sanity — counts match expected categorisation."""
    acct = _seed_account(session)
    _seed_tx(
        session,
        account_id=acct.id,
        canonical_action=CanonicalAction.CONTRIBUTION,
        amount=Decimal("1000"),
        source_row_hash="c1",
    )
    _seed_tx(
        session,
        account_id=acct.id,
        canonical_action=CanonicalAction.DISTRIBUTION,
        amount=Decimal("-500"),
        source_row_hash="d1",
    )
    _seed_tx(
        session,
        account_id=acct.id,
        canonical_action=CanonicalAction.BUY,
        amount=Decimal("-200"),
        symbol="VTI",
        source_row_hash="b1",
    )
    _seed_tx(
        session,
        account_id=acct.id,
        canonical_action=CanonicalAction.TRANSFER,
        amount=Decimal("750"),
        source_row_hash="t1",
    )

    result = backfill(session, apply=True)
    assert result["examined"] == 4
    by_action = result["by_action_changed"]
    assert by_action.get("contribution") == 1
    assert by_action.get("distribution") == 1
    assert by_action.get("transfer") == 1
    # BUY is NONE at portfolio scope — which equals the default. So no change.
    assert "buy" not in by_action
