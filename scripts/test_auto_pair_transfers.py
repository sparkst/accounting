"""Tests for REQ-PERF-004 — auto-pair candidate generator."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import src.models.plaid as _plaid  # noqa: F401  # registers PlaidItem for FK resolution
from scripts.auto_pair_transfers import (
    find_candidates,
    is_rejected,
    reject_pair,
)
from src.models.audit_event import AuditEvent
from src.models.base import Base
from src.models.brokerage import Account, BrokerageTransaction
from src.models.enums import (
    AccountType,
    Broker,
    BrokerageTxStatus,
    CanonicalAction,
    Entity,
)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _acct(s: Session, acct_id: str) -> Account:
    acct = Account(
        id=acct_id,
        broker=Broker.SCHWAB.value,
        account_number=f"NUM-{acct_id}",
        account_name=acct_id,
        account_type=AccountType.TAXABLE.value,
        entity=Entity.PERSONAL.value,
    )
    s.add(acct)
    s.commit()
    return acct


def _tx(
    s: Session,
    *,
    tx_id: str,
    account_id: str,
    action: CanonicalAction,
    amount: Decimal,
    trade_date: date,
    paired_id: str | None = None,
    hash_suffix: str = "",
) -> BrokerageTransaction:
    tx = BrokerageTransaction(
        id=tx_id,
        account_id=account_id,
        trade_date=trade_date,
        action=action.value,
        canonical_action=action.value,
        amount=amount,
        status=BrokerageTxStatus.IMPORTED.value,
        source_file="test.csv",
        source_row_hash=f"{tx_id}{hash_suffix}",
        raw_data={},
        paired_transaction_id=paired_id,
    )
    s.add(tx)
    s.commit()
    return tx


class TestFindCandidates:
    def test_finds_clear_pair(self, session: Session) -> None:
        """REQ-PERF-004: finds obvious transfer pair on same day, opposite amounts."""
        _acct(session, "A1")
        _acct(session, "A2")
        _tx(session, tx_id="t1", account_id="A1", action=CanonicalAction.TRANSFER,
            amount=Decimal("1000.00"), trade_date=date(2025, 6, 2))
        _tx(session, tx_id="t2", account_id="A2", action=CanonicalAction.TRANSFER,
            amount=Decimal("-1000.00"), trade_date=date(2025, 6, 2))

        pairs = find_candidates(session)
        assert len(pairs) == 1
        assert {pairs[0].tx_a_id, pairs[0].tx_b_id} == {"t1", "t2"}
        assert pairs[0].confidence == pytest.approx(1.0)

    def test_skips_already_paired(self, session: Session) -> None:
        """REQ-PERF-004: already-paired rows excluded."""
        _acct(session, "A1")
        _acct(session, "A2")
        _tx(session, tx_id="t1", account_id="A1", action=CanonicalAction.TRANSFER,
            amount=Decimal("500.00"), trade_date=date(2025, 6, 2), paired_id="t2")
        _tx(session, tx_id="t2", account_id="A2", action=CanonicalAction.TRANSFER,
            amount=Decimal("-500.00"), trade_date=date(2025, 6, 2), paired_id="t1")

        assert find_candidates(session) == []

    def test_skips_rejected_pairs(self, session: Session) -> None:
        """REQ-PERF-004: rejected pairs do not re-surface."""
        _acct(session, "A1")
        _acct(session, "A2")
        _tx(session, tx_id="t1", account_id="A1", action=CanonicalAction.TRANSFER,
            amount=Decimal("750.00"), trade_date=date(2025, 6, 2))
        _tx(session, tx_id="t2", account_id="A2", action=CanonicalAction.TRANSFER,
            amount=Decimal("-750.00"), trade_date=date(2025, 6, 2))

        reject_pair(session, "t1", "t2")
        assert find_candidates(session) == []

    def test_lower_confidence_multiple_candidates(self, session: Session) -> None:
        """REQ-PERF-004: confidence drops when a tx has multiple valid partners."""
        _acct(session, "A1")
        _acct(session, "A2")
        _acct(session, "A3")
        _tx(session, tx_id="t1", account_id="A1", action=CanonicalAction.TRANSFER,
            amount=Decimal("1000.00"), trade_date=date(2025, 6, 2))
        _tx(session, tx_id="t2", account_id="A2", action=CanonicalAction.TRANSFER,
            amount=Decimal("-1000.00"), trade_date=date(2025, 6, 2))
        _tx(session, tx_id="t3", account_id="A3", action=CanonicalAction.TRANSFER,
            amount=Decimal("-1000.00"), trade_date=date(2025, 6, 2))

        pairs = find_candidates(session)
        assert len(pairs) == 2
        for pair in pairs:
            assert pair.confidence == pytest.approx(0.5)

    def test_respects_business_day_window(self, session: Session) -> None:
        """REQ-PERF-004: 5-business-day window enforced (Mon→Mon = 5 bdays IN; Mon→Tue+1w = 6 OUT)."""
        _acct(session, "A1")
        _acct(session, "A2")
        _acct(session, "A3")

        # Mon 2025-06-02 → Mon 2025-06-09: 5 business days. Within window.
        _tx(session, tx_id="within_a", account_id="A1", action=CanonicalAction.TRANSFER,
            amount=Decimal("1000.00"), trade_date=date(2025, 6, 2))
        _tx(session, tx_id="within_b", account_id="A2", action=CanonicalAction.TRANSFER,
            amount=Decimal("-1000.00"), trade_date=date(2025, 6, 9))

        # Mon 2025-06-02 → Tue 2025-06-10: 6 business days. Outside window.
        _tx(session, tx_id="outside_a", account_id="A1", action=CanonicalAction.TRANSFER,
            amount=Decimal("2000.00"), trade_date=date(2025, 6, 2), hash_suffix="out")
        _tx(session, tx_id="outside_b", account_id="A3", action=CanonicalAction.TRANSFER,
            amount=Decimal("-2000.00"), trade_date=date(2025, 6, 10))

        pairs = find_candidates(session)
        pair_ids = [{p.tx_a_id, p.tx_b_id} for p in pairs]
        assert {"within_a", "within_b"} in pair_ids
        assert {"outside_a", "outside_b"} not in pair_ids


class TestIsRejected:
    def test_both_orderings(self, session: Session) -> None:
        """REQ-PERF-004: is_rejected returns True for both (a,b) and (b,a) orderings."""
        _acct(session, "A1")
        _acct(session, "A2")
        _tx(session, tx_id="tx1", account_id="A1", action=CanonicalAction.TRANSFER,
            amount=Decimal("100"), trade_date=date(2025, 1, 1))
        _tx(session, tx_id="tx2", account_id="A2", action=CanonicalAction.TRANSFER,
            amount=Decimal("-100"), trade_date=date(2025, 1, 1))

        assert not is_rejected(session, "tx1", "tx2")
        reject_pair(session, "tx1", "tx2")
        assert is_rejected(session, "tx1", "tx2")
        assert is_rejected(session, "tx2", "tx1")

    def test_idempotent(self, session: Session) -> None:
        """REQ-PERF-004: reject_pair twice → only one audit row."""
        _acct(session, "A1")
        _acct(session, "A2")
        _tx(session, tx_id="tx1", account_id="A1", action=CanonicalAction.TRANSFER,
            amount=Decimal("100"), trade_date=date(2025, 1, 1))
        _tx(session, tx_id="tx2", account_id="A2", action=CanonicalAction.TRANSFER,
            amount=Decimal("-100"), trade_date=date(2025, 1, 1))

        reject_pair(session, "tx1", "tx2")
        reject_pair(session, "tx1", "tx2")

        count = (
            session.query(AuditEvent)
            .filter(AuditEvent.field_changed == "transfer_pair_rejected")
            .count()
        )
        assert count == 1
