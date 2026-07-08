"""Tests for the autoconfirm CLI: sweep / digest / undo (REQ-MCA-002/003)."""

from __future__ import annotations

import itertools
from collections.abc import Generator
from datetime import datetime
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


def test_sweep_apply_never_touches_vendor_rules(session: Session) -> None:
    """P1-a1c: --apply sweep must never mutate vendor_rules, incl. last_matched.

    src/close/autoconfirm.py's module docstring asserts auto-confirm "never
    touches vendor_rules ... no path mutates vendor_rules from auto-confirm".
    lookup_vendor_rule's default touch_last_matched=True previously dirtied
    that column even for a plain read, and the outer sweep commit persisted
    it. The sweep must call the read-only variant.
    """
    rule = _rule(session, confidence=0.95)
    _tx(session)
    session.commit()
    rule_id = rule.id
    assert rule.last_matched is None

    result = cli.sweep(session, apply=True)
    assert result.confirmed == 1

    session.expire_all()
    persisted_rule = session.get(VendorRule, rule_id)
    assert persisted_rule.last_matched is None


def test_sweep_skips_row_whose_stored_classification_diverges_from_rule(
    session: Session,
) -> None:
    """P2-b2e: confirmed_by=auto:rule:<id> must be faithful to what it confirms.

    If the transaction's stored (already-classified) entity/tax_category/
    direction no longer match the current best Tier-1 rule for its
    description — e.g. it was originally Tier-2/3 classified, or the rule
    was edited since — the sweep must skip it rather than confirm the OLD
    classification while attributing it to a rule whose classification
    doesn't match.
    """
    rule = _rule(session, confidence=0.95)  # office_expense / expense
    tx = _tx(session, tax_category=TaxCategory.SUPPLIES.value)  # diverges from rule
    session.commit()
    tx_id = tx.id

    result = cli.sweep(session, apply=True)

    assert result.confirmed == 0
    session.expire_all()
    persisted = session.get(Transaction, tx_id)
    assert persisted.status == TransactionStatus.AUTO_CLASSIFIED.value
    assert persisted.confirmed_by == "auto"
    assert rule.last_matched is None


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


def test_digest_apply_ledger_row_durable_across_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0-001 regression: the ledger row must be committed, not just flushed.

    A same-session query can't distinguish a flush from a commit — both are
    visible within the same in-flight transaction. Close the writer session
    and reopen a fresh one bound to the same engine to prove the row
    actually reached disk (durability), not just the session identity map.
    """
    monkeypatch.setenv("ALERT_TO_EMAIL", "travis@sparkry.ai")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    writer = _Session()
    try:
        row = cli.send_digest(writer, apply=True)
        assert row is not None
    finally:
        writer.close()

    reader = _Session()
    try:
        assert reader.query(AlertDispatch).count() == 1
    finally:
        for model in (AuditEvent, AlertDispatch, Transaction, VendorRule, TaxYearLock):
            reader.query(model).delete()
        reader.commit()
        reader.close()


def test_digest_same_day_rerun_sends_exactly_once(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1-201 regression: a same-day re-run (systemd retry, operator re-run)
    must NOT send a second real email — the pre-send ledger check
    short-circuits before resend.Emails.send, not merely before the ledger
    insert."""
    import resend

    monkeypatch.setenv("ALERT_TO_EMAIL", "travis@sparkry.ai")
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    calls = {"n": 0}

    def _fake_send(params: dict) -> dict:  # type: ignore[type-arg]
        calls["n"] += 1
        return {"id": "fake"}

    monkeypatch.setattr(resend.Emails, "send", staticmethod(_fake_send))

    now = datetime(2026, 7, 6, 14, 10)
    first = cli.send_digest(session, apply=True, now=now)
    assert first is not None and first.status == "sent"
    second = cli.send_digest(session, apply=True, now=now)
    assert second is not None and second.id == first.id  # same ledger row
    assert calls["n"] == 1  # exactly one real send
    assert session.query(AlertDispatch).count() == 1

    # A prior FAILED day retries (and flips in place) rather than skipping.
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    other_day = datetime(2026, 7, 13, 14, 10)
    failed = cli.send_digest(session, apply=True, now=other_day)
    assert failed is not None and failed.status == "failed"
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    retried = cli.send_digest(session, apply=True, now=other_day)
    assert retried is not None and retried.id == failed.id
    assert retried.status == "sent"
    assert calls["n"] == 2


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
