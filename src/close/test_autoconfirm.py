"""Tests for auto-confirm eligibility + no-self-reinforcement (REQ-MCA-002/003).

Each §2.1 conjunct is falsified individually, and the §2.3 invariant (a 100-cycle
auto-confirm loop leaves the VendorRule row byte-identical) is asserted.
"""

from __future__ import annotations

from collections.abc import Generator
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import src.db.connection  # noqa: F401  (register every ORM table on Base)
from src.classification.engine import ClassificationResult
from src.close.autoconfirm import auto_confirm_if_eligible
from src.models.audit_event import AuditEvent
from src.models.base import Base
from src.models.enums import Direction, Entity, TaxCategory, TransactionStatus
from src.models.transaction import Transaction
from src.models.vendor_rule import VendorRule

_ENGINE = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
Base.metadata.create_all(_ENGINE)
_Session = sessionmaker(bind=_ENGINE)


@pytest.fixture()
def session() -> Generator[Session, None, None]:
    s = _Session()
    yield s
    s.rollback()
    s.query(AuditEvent).delete()
    s.query(Transaction).delete()
    s.query(VendorRule).delete()
    s.commit()
    s.close()


def _make_rule(session: Session, confidence: float = 0.95) -> VendorRule:
    rule = VendorRule(
        vendor_pattern="acme",
        is_regex=False,
        entity=Entity.SPARKRY.value,
        tax_category=TaxCategory.OFFICE_EXPENSE.value,
        direction=Direction.EXPENSE.value,
        confidence=confidence,
    )
    session.add(rule)
    session.flush()
    return rule


def _make_tx(session: Session, *, flush: bool = True, **overrides: object) -> Transaction:
    defaults: dict[str, object] = {
        "source": "plaid",
        "source_hash": "hash-" + str(id(overrides)),
        "date": "2026-06-15",
        "description": "ACME Corp",
        "amount": Decimal("-42.00"),
        "entity": Entity.SPARKRY.value,
        "tax_category": TaxCategory.OFFICE_EXPENSE.value,
        "direction": Direction.EXPENSE.value,
        "status": TransactionStatus.AUTO_CLASSIFIED.value,
        "confidence": 0.95,
        "confirmed_by": "auto",
        "raw_data": {},
    }
    defaults.update(overrides)
    tx = Transaction(**defaults)
    session.add(tx)
    if flush:
        session.flush()
    return tx


def _result(rule: VendorRule, **overrides: object) -> ClassificationResult:
    kwargs: dict[str, object] = {
        "entity": Entity.SPARKRY,
        "tax_category": TaxCategory.OFFICE_EXPENSE,
        "direction": Direction.EXPENSE,
        "confidence": rule.confidence,
        "tier_used": 1,
        "reasoning": "matched acme",
        "status": TransactionStatus.AUTO_CLASSIFIED,
        "rule_id": rule.id,
    }
    kwargs.update(overrides)
    return ClassificationResult(**kwargs)  # type: ignore[arg-type]


def test_eligible_transaction_is_confirmed(session: Session) -> None:
    """REQ-MCA-002: a Tier-1 match on a >=0.90 rule auto-confirms with audit rows."""
    rule = _make_rule(session, confidence=0.95)
    tx = _make_tx(session)

    assert auto_confirm_if_eligible(session, tx, _result(rule)) is True
    assert tx.status == TransactionStatus.CONFIRMED.value
    assert tx.confirmed_by == f"auto:rule:{rule.id}"

    events = session.query(AuditEvent).filter_by(transaction_id=tx.id).all()
    fields = {e.field_changed for e in events}
    assert fields == {"status", "confirmed_by"}
    for e in events:
        assert e.changed_by == f"auto:rule:{rule.id}"
        assert e.entity_id is None and e.entity_type is None


def test_tier2_never_auto_confirms(session: Session) -> None:
    """REQ-MCA-002: a Tier-2 result never auto-confirms regardless of confidence."""
    rule = _make_rule(session, confidence=0.99)
    tx = _make_tx(session)
    assert auto_confirm_if_eligible(session, tx, _result(rule, tier_used=2)) is False
    assert tx.status == TransactionStatus.AUTO_CLASSIFIED.value
    assert session.query(AuditEvent).count() == 0


def test_rule_confidence_below_threshold_is_ineligible(session: Session) -> None:
    """REQ-MCA-002: a 0.89 rule confidence falls just below the 0.90 bar."""
    rule = _make_rule(session, confidence=0.89)
    tx = _make_tx(session)
    assert auto_confirm_if_eligible(session, tx, _result(rule)) is False
    assert tx.status == TransactionStatus.AUTO_CLASSIFIED.value


def test_vetoed_needs_review_result_is_ineligible(session: Session) -> None:
    """REQ-MCA-002: a sign-veto (result.status=NEEDS_REVIEW) disqualifies."""
    rule = _make_rule(session, confidence=0.99)
    tx = _make_tx(session)
    result = _result(rule, status=TransactionStatus.NEEDS_REVIEW)
    assert auto_confirm_if_eligible(session, tx, result) is False


def test_null_amount_is_ineligible(session: Session) -> None:
    """REQ-MCA-002: a null amount can never auto-confirm."""
    rule = _make_rule(session, confidence=0.99)
    tx = _make_tx(session, amount=None, status=TransactionStatus.AUTO_CLASSIFIED.value)
    assert auto_confirm_if_eligible(session, tx, _result(rule)) is False


def test_split_child_parent_id_is_ineligible(session: Session) -> None:
    """REQ-MCA-002: a split child (parent_id set) never auto-confirms."""
    rule = _make_rule(session, confidence=0.99)
    parent = _make_tx(session, source_hash="parent-hash")
    child = _make_tx(session, source_hash="child-hash", parent_id=parent.id)
    assert auto_confirm_if_eligible(session, child, _result(rule)) is False


def test_missing_rule_id_is_ineligible(session: Session) -> None:
    """REQ-MCA-002: no rule_id on the result means no Tier-1 rule to trust."""
    rule = _make_rule(session, confidence=0.99)
    tx = _make_tx(session)
    assert auto_confirm_if_eligible(session, tx, _result(rule, rule_id=None)) is False


def test_status_not_auto_classified_is_ineligible(session: Session) -> None:
    """REQ-MCA-002: a tx already routed to needs_review is not auto-confirmed."""
    rule = _make_rule(session, confidence=0.99)
    tx = _make_tx(session, status=TransactionStatus.NEEDS_REVIEW.value)
    assert auto_confirm_if_eligible(session, tx, _result(rule)) is False


def test_no_self_reinforcement_100_cycles(session: Session) -> None:
    """REQ-MCA-003 §2.3: auto-confirm never mutates the VendorRule row."""
    rule = _make_rule(session, confidence=0.95)
    before = {
        c.name: getattr(rule, c.name) for c in VendorRule.__table__.columns
    }
    for i in range(100):
        tx = _make_tx(session, source_hash=f"loop-{i}")
        assert auto_confirm_if_eligible(session, tx, _result(rule)) is True
    session.flush()
    session.refresh(rule)
    after = {c.name: getattr(rule, c.name) for c in VendorRule.__table__.columns}
    assert after == before
