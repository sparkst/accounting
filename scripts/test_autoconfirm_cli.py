"""Tests for the autoconfirm CLI: sweep / digest / undo (REQ-MCA-002/003)."""

from __future__ import annotations

import itertools
from collections.abc import Generator
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import scripts.autoconfirm as cli
import src.db.connection  # noqa: F401  (register every ORM table on Base)
from src.alerts.models import AlertDispatch
from src.classification.engine import ClassificationResult
from src.close.autoconfirm import auto_confirm_if_eligible
from src.models.audit_event import AuditEvent
from src.models.base import Base
from src.models.enums import Direction, Entity, TaxCategory, TransactionStatus
from src.models.tax_year_lock import TaxYearLock
from src.models.transaction import Transaction
from src.models.vendor_rule import VendorRule

_ENGINE = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
Base.metadata.create_all(_ENGINE)
_Session = sessionmaker(bind=_ENGINE)
_counter = itertools.count()


@pytest.fixture()
def session() -> Generator[Session, None, None]:
    s = _Session()
    yield s
    s.rollback()
    for model in (AuditEvent, AlertDispatch, Transaction, VendorRule, TaxYearLock):
        s.query(model).delete()
    s.commit()
    s.close()


def _rule(session: Session, confidence: float = 0.95) -> VendorRule:
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


def _tx(session: Session, **overrides: object) -> Transaction:
    defaults: dict[str, object] = {
        "source": "plaid",
        "source_hash": f"h-{next(_counter)}",
        "date": "2026-06-10",
        "description": "ACME Corp",
        "amount": Decimal("-25.00"),
        "entity": Entity.SPARKRY.value,
        "tax_category": TaxCategory.OFFICE_EXPENSE.value,
        "direction": Direction.EXPENSE.value,
        "status": TransactionStatus.AUTO_CLASSIFIED.value,
        "confirmed_by": "auto",
        "raw_data": {},
    }
    defaults.update(overrides)
    tx = Transaction(**defaults)
    session.add(tx)
    session.flush()
    return tx


# ── sweep ─────────────────────────────────────────────────────────────────


def test_sweep_dry_run_writes_nothing(session: Session) -> None:
    """REQ-MCA-002: DRY-RUN sweep previews candidates but writes nothing."""
    _rule(session, confidence=0.95)
    tx = _tx(session)
    session.commit()  # backlog is already committed in production
    tx_id = tx.id
    result = cli.sweep(session, apply=False)
    assert result.confirmed == 1
    assert result.candidates[0].tx_id == tx_id
    # Nothing persisted: status unchanged, no audit rows.
    session.expire_all()
    assert session.get(Transaction, tx_id).status == TransactionStatus.AUTO_CLASSIFIED.value
    assert session.query(AuditEvent).count() == 0


def test_sweep_apply_confirms(session: Session) -> None:
    """REQ-MCA-002: --apply sweep confirms eligible rows and writes audit rows."""
    rule = _rule(session, confidence=0.95)
    tx = _tx(session)
    session.commit()
    tx_id, rule_id = tx.id, rule.id
    result = cli.sweep(session, apply=True)
    assert result.confirmed == 1
    session.expire_all()
    persisted = session.get(Transaction, tx_id)
    assert persisted.status == TransactionStatus.CONFIRMED.value
    assert persisted.confirmed_by == f"auto:rule:{rule_id}"
    assert session.query(AuditEvent).filter_by(transaction_id=tx_id).count() == 2


def test_sweep_skips_low_confidence_rule(session: Session) -> None:
    """REQ-MCA-002: a sub-0.90 rule leaves the backlog row untouched."""
    _rule(session, confidence=0.80)
    tx = _tx(session)
    session.commit()
    tx_id = tx.id
    result = cli.sweep(session, apply=True)
    assert result.confirmed == 0
    session.expire_all()
    assert session.get(Transaction, tx_id).status == TransactionStatus.AUTO_CLASSIFIED.value


# ── digest ────────────────────────────────────────────────────────────────


def test_digest_dry_run_no_ledger(session: Session) -> None:
    """REQ-MCA-003: DRY-RUN digest sends nothing and writes no ledger row."""
    rule = _rule(session, confidence=0.95)
    tx = _tx(session)
    result = ClassificationResult(
        entity=Entity.SPARKRY,
        tax_category=TaxCategory.OFFICE_EXPENSE,
        direction=Direction.EXPENSE,
        confidence=0.95,
        tier_used=1,
        reasoning="acme",
        status=TransactionStatus.AUTO_CLASSIFIED,
        rule_id=rule.id,
    )
    auto_confirm_if_eligible(session, tx, result)
    session.commit()

    vendors = cli.collect_digest(session)
    assert vendors and vendors[0].vendor == "ACME Corp"
    assert cli.send_digest(session, apply=False) is None
    assert session.query(AlertDispatch).count() == 0


def test_digest_apply_records_ledger(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """REQ-MCA-003: --apply digest records an autoconfirm_digest ledger row."""
    monkeypatch.setenv("ALERT_TO_EMAIL", "travis@sparkry.ai")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)  # send fails → status=failed, still ledgered
    row = cli.send_digest(session, apply=True)
    assert row is not None
    assert row.alert_type == "autoconfirm_digest"
    assert row.delivery_channel == "resend_email"
    assert row.payload_json is None
    assert session.query(AlertDispatch).count() == 1


# ── undo ──────────────────────────────────────────────────────────────────


def test_undo_round_trip_with_audit(session: Session) -> None:
    """REQ-MCA-003 §2.5: undo reverts to needs_review with three human audit rows."""
    tx = _tx(session, status=TransactionStatus.CONFIRMED.value, confirmed_by="auto:rule:r1")
    res = cli.undo(session, tx.id, apply=True)
    assert res.old_status == TransactionStatus.CONFIRMED.value
    session.expire_all()
    persisted = session.get(Transaction, tx.id)
    assert persisted.status == TransactionStatus.NEEDS_REVIEW.value
    assert persisted.confirmed_by == "auto"
    assert persisted.review_reason == "auto-confirm undone by operator"
    events = session.query(AuditEvent).filter_by(transaction_id=tx.id).all()
    assert {e.field_changed for e in events} == {"status", "review_reason", "confirmed_by"}
    assert all(e.changed_by == "human" for e in events)


def test_undo_dry_run_no_change(session: Session) -> None:
    """REQ-MCA-003 §2.5: DRY-RUN undo previews without mutating the row."""
    tx = _tx(session, status=TransactionStatus.CONFIRMED.value, confirmed_by="auto:rule:r1")
    cli.undo(session, tx.id, apply=False)
    session.expire_all()
    assert session.get(Transaction, tx.id).status == TransactionStatus.CONFIRMED.value
    assert session.query(AuditEvent).count() == 0


def test_undo_refuses_non_auto_confirm(session: Session) -> None:
    """REQ-MCA-003 §2.5: undo refuses a human-confirmed row (never touches it)."""
    tx = _tx(session, status=TransactionStatus.CONFIRMED.value, confirmed_by="human")
    with pytest.raises(cli.UndoError):
        cli.undo(session, tx.id, apply=True)


def test_undo_blocked_by_tax_year_lock(session: Session) -> None:
    """REQ-MCA-003 §2.5: a locked tax year blocks the undo."""
    from fastapi import HTTPException

    session.add(
        TaxYearLock(entity=Entity.SPARKRY.value, year=2026, locked_by="human")
    )
    tx = _tx(session, status=TransactionStatus.CONFIRMED.value, confirmed_by="auto:rule:r1")
    session.flush()
    with pytest.raises(HTTPException):
        cli.undo(session, tx.id, apply=True)
