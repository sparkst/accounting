"""Tests for src/adapters/plaid_transactions.py — REQ-PT-008..010.

Uses an in-memory SQLite so make_transaction can call the classifier
(which queries VendorRule rows). Plaid sign convention: positive = money out.
"""

from __future__ import annotations

import unittest.mock as mock
from collections.abc import Generator
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from src.adapters.plaid_client import RetryablePlaidError, TerminalPlaidError
from src.adapters.plaid_transactions import (
    KNOWN_MIRROR_ACCOUNT_IDS,
    UnrecognizedPlaidAccountError,
    build_tx_fields,
    card_payment_signal_for_raw,
    card_payment_signal_for_txn,
    fetch_all_pages,
    make_transaction,
    process_added,
    process_modified,
    process_removed,
    supersede_csv_rows,
    sync_all_active,
    sync_one_item,
)
from src.classification.engine import ClassificationResult
from src.models.audit_event import AuditEvent
from src.models.base import Base
from src.models.brokerage import Account
from src.models.enums import (
    Direction,
    Entity,
    IngestionStatus,
    TaxCategory,
    TransactionStatus,
)
from src.models.ingestion_log import IngestionLog
from src.models.plaid import PlaidItem
from src.models.transaction import Transaction
from src.utils.plaid_crypto import InvalidCiphertextError

#: One real KNOWN_MIRROR_ACCOUNT_IDS entry, for tests exercising the
#: safe/silent-skip path (REQ-WBR-LED-014 case A). Sorted so the choice is
#: deterministic regardless of frozenset iteration order.
_A_KNOWN_MIRROR_ID = sorted(KNOWN_MIRROR_ACCOUNT_IDS)[0]


def _plaid_txn(**kw):
    base = dict(
        transaction_id="txn_1", account_id="acc_1", amount=12.34, date="2026-05-01",
        name="STARBUCKS #123", merchant_name="Starbucks", pending=False,
        pending_transaction_id=None, iso_currency_code="USD",
    )
    base.update(kw)
    return SimpleNamespace(**base, to_dict=lambda: {**base})


# ── db fixture (in-memory SQLite, FK enforcement) ─────────────────────────────


@pytest.fixture
def db() -> Generator[Session, None, None]:
    """Fresh in-memory SQLite with full schema and FK enforcement."""
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _):  # type: ignore[no-untyped-def]
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    # Register all models needed for classifier + transaction table.
    from src.models import audit_event  # noqa: F401

    Base.metadata.create_all(engine)
    sess = Session(bind=engine)
    yield sess
    sess.close()


# ── Task 3 tests: build_tx_fields ─────────────────────────────────────────────


def test_outflow_is_negative_expense():
    f = build_tx_fields(_plaid_txn(amount=12.34))
    assert f["amount"] == Decimal("-12.34")


def test_inflow_is_positive_income():
    f = build_tx_fields(_plaid_txn(amount=-500.00))
    assert f["amount"] == Decimal("500.00")


def test_description_prefers_merchant_name():
    assert build_tx_fields(_plaid_txn())["description"] == "Starbucks"
    assert build_tx_fields(_plaid_txn(merchant_name=None))["description"] == "STARBUCKS #123"


def test_source_and_hash_stable():
    from src.utils.dedup import compute_source_hash
    f = build_tx_fields(_plaid_txn(transaction_id="txn_xyz"))
    assert f["source"] == "plaid"
    assert f["source_id"] == "txn_xyz"
    assert f["source_hash"] == compute_source_hash("plaid", "txn_xyz")
    assert f["raw_data"]["transaction_id"] == "txn_xyz"


def test_raw_data_is_complete_plaid_dict():
    """REQ-PT-009: raw_data preserves the COMPLETE Plaid txn object verbatim."""
    txn = _plaid_txn(transaction_id="txn_full", amount=42.5, name="ACME",
                     merchant_name="Acme Co", pending=True)
    f = build_tx_fields(txn)
    assert f["raw_data"] == txn.to_dict()
    # spot-check several distinct fields are all present, not just the id
    for key in ("transaction_id", "account_id", "amount", "date", "name",
                "merchant_name", "pending"):
        assert key in f["raw_data"]


# ── Internal-transfer detection (REQ-PT general / spec §10) ──────────────────


def test_transfer_category_routes_to_needs_review(db):
    """A Plaid TRANSFER-category txn must NOT be auto-set to direction=transfer
    (that silently drops real inbound income from P&L/B&O). Instead it is routed
    to needs_review with the transfer-confirm reason, and the classifier's
    direction is preserved as a suggestion."""
    item, acct = _mapped(db)
    pfc = SimpleNamespace(primary="TRANSFER_IN", detailed="TRANSFER_IN_DEPOSIT")
    txn = _plaid_txn(transaction_id="xfer1", amount=-4800.0,
                     name="Online Transfer from SAV", personal_finance_category=pfc)
    # classifier returns a high-confidence (0.95) result; without the override
    # this would auto-classify. It must instead be held for human review.
    with mock.patch("src.adapters.plaid_transactions.classify",
                    return_value=_cls()):
        tx = make_transaction(txn, session=db, entity="personal",
                              payment_method="Chase ****1234")
    assert tx.status == TransactionStatus.NEEDS_REVIEW.value
    assert tx.review_reason == "plaid: transfer-category — confirm transfer vs income"
    # classifier's direction is left as a suggestion, NOT overridden to transfer.
    assert tx.direction == Direction.EXPENSE.value


def test_transfer_code_routes_to_needs_review(db):
    item, acct = _mapped(db)
    txn = _plaid_txn(transaction_id="xfer2", transaction_code="transfer")
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        tx = make_transaction(txn, session=db, entity="personal",
                              payment_method="Chase ****1234")
    assert tx.status == TransactionStatus.NEEDS_REVIEW.value
    assert tx.review_reason == "plaid: transfer-category — confirm transfer vs income"
    assert tx.direction == Direction.EXPENSE.value


def test_transfer_in_client_deposit_not_silently_dropped(db):
    """A TRANSFER_IN inbound deposit (indistinguishable from a real client ACH)
    must NOT be auto-classified as a non-P&L transfer; it is surfaced for human
    review so genuine income is never dropped from B&O gross receipts."""
    item, acct = _mapped(db)
    pfc = SimpleNamespace(primary="TRANSFER_IN", detailed="TRANSFER_IN_ACCOUNT_TRANSFER")
    txn = _plaid_txn(transaction_id="client_ach", amount=-12000.0,
                     name="ACH CREDIT ACME CORP", personal_finance_category=pfc)
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        tx = make_transaction(txn, session=db, entity="sparkry",
                              payment_method="Chase ****1234")
    assert tx.status == TransactionStatus.NEEDS_REVIEW.value
    assert tx.direction != Direction.TRANSFER.value


def test_non_transfer_keeps_classifier_direction(db):
    item, acct = _mapped(db)
    pfc = SimpleNamespace(primary="FOOD_AND_DRINK")
    txn = _plaid_txn(transaction_id="meal1", personal_finance_category=pfc)
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        tx = make_transaction(txn, session=db, entity="personal",
                              payment_method="Chase ****1234")
    assert tx.direction == Direction.EXPENSE.value
    # A non-transfer, high-confidence row auto-classifies as usual.
    assert tx.status == TransactionStatus.AUTO_CLASSIFIED.value


# ── Task 4 tests: make_transaction ────────────────────────────────────────────


def _cls(confidence=0.95):
    return ClassificationResult(
        entity=Entity.PERSONAL, tax_category=TaxCategory.MEALS, direction=Direction.EXPENSE,
        confidence=confidence, tier_used=1, reasoning="rule",
        status=TransactionStatus.AUTO_CLASSIFIED, deductible_pct=0.5,
    )


def test_make_transaction_entity_from_account_overrides_classifier(db):
    txn = _plaid_txn()
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        tx = make_transaction(txn, session=db, entity="sparkry", payment_method="Chase ****1234")
    assert tx.entity == "sparkry"
    assert tx.payment_method == "Chase ****1234"
    assert tx.tax_category == TaxCategory.MEALS.value
    assert tx.status == TransactionStatus.AUTO_CLASSIFIED.value


def test_make_transaction_low_confidence_needs_review(db):
    with mock.patch("src.adapters.plaid_transactions.classify",
                    return_value=_cls(confidence=0.4)):
        tx = make_transaction(_plaid_txn(), session=db, entity="sparkry",
                              payment_method="Chase ****1234")
    assert tx.status == TransactionStatus.NEEDS_REVIEW.value


def test_make_transaction_unmapped_account_null_entity(db):
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        tx = make_transaction(_plaid_txn(), session=db, entity=None, payment_method=None)
    assert tx.entity is None
    assert tx.payment_method is None
    assert tx.status == TransactionStatus.NEEDS_REVIEW.value
    assert tx.review_reason == "plaid: account not mapped to an entity"


def test_make_transaction_unmapped_and_transfer_combines_review_reasons(db):
    """P3-002: when an account is BOTH unmapped (entity None) AND the txn is a
    Plaid transfer-category, review_reason must surface BOTH signals — an
    if/elif chain would silently drop the transfer flag so an operator querying
    needs_review for transfer patterns would miss these rows."""
    pfc = SimpleNamespace(primary="TRANSFER_IN", detailed="TRANSFER_IN_DEPOSIT")
    txn = _plaid_txn(transaction_id="dual1", personal_finance_category=pfc)
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        tx = make_transaction(txn, session=db, entity=None, payment_method=None)
    assert tx.status == TransactionStatus.NEEDS_REVIEW.value
    assert "account not mapped to an entity" in tx.review_reason
    assert "transfer-category" in tx.review_reason
    assert tx.review_reason == (
        "plaid: account not mapped to an entity; "
        "transfer-category — confirm transfer vs income"
    )


def test_make_transaction_mapped_transfer_low_confidence_combines_all_reasons(db):
    """P3-001-CQ: when an account IS mapped but the txn is BOTH a Plaid
    transfer-category AND the classifier's confidence is below threshold, the
    transfer note must NOT clobber the classifier's low-confidence detail. The
    operator needs all the signals — the transfer flag AND the tier+score detail.
    """
    pfc = SimpleNamespace(primary="TRANSFER_IN", detailed="TRANSFER_IN_DEPOSIT")
    txn = _plaid_txn(transaction_id="triple1", personal_finance_category=pfc)
    low_conf = _cls(confidence=0.42)
    low_conf.review_reason = "Low confidence (0.42) from Tier 3 LLM: ambiguous merchant"
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=low_conf):
        tx = make_transaction(txn, session=db, entity="personal",
                              payment_method="Chase ****1234")
    assert tx.status == TransactionStatus.NEEDS_REVIEW.value
    # All three signals present (entity IS mapped, so no unmapped note).
    assert "transfer-category" in tx.review_reason
    assert "Low confidence (0.42) from Tier 3 LLM" in tx.review_reason
    assert "account not mapped" not in tx.review_reason
    assert tx.review_reason == (
        "plaid: transfer-category — confirm transfer vs income; "
        "Low confidence (0.42) from Tier 3 LLM: ambiguous merchant"
    )


# ── Task 5 tests: process_added upsert + idempotency (REQ-PT-002) ─────────────


def _mapped(db, plaid_account_id="acc_1", entity="sparkry", pm="Chase ****1234"):
    item = PlaidItem(item_id="it_1", institution_id="ins_56", institution_name="Chase",
                     access_token_encrypted="REVOKED", status="active")
    db.add(item)
    db.flush()
    acct = Account(broker="chase", account_number="****1234", account_name="Op",
                   account_type="checking", entity=entity, payment_method=pm,
                   plaid_item_id=item.id, plaid_account_id=plaid_account_id)
    db.add(acct)
    db.commit()
    return item, acct


def test_added_inserts_one_row_idempotent(db):
    item, acct = _mapped(db)
    txns = [_plaid_txn(transaction_id="t1", account_id="acc_1")]
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        process_added(db, item, txns, account_index={"acc_1": acct})
        process_added(db, item, txns, account_index={"acc_1": acct})  # re-run
    rows = db.query(Transaction).filter_by(source="plaid", source_id="t1").all()
    assert len(rows) == 1
    assert rows[0].entity == "sparkry"


# ── Task 6 tests: pending→posted reconcile (REQ-PT-005) ──────────────────────


def test_pending_then_posted_updates_in_place(db):
    item, acct = _mapped(db)
    pending = _plaid_txn(transaction_id="p1", amount=20.00, pending=True)
    posted = _plaid_txn(transaction_id="post1", amount=22.50, pending=False,
                        pending_transaction_id="p1")
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        process_added(db, item, [pending], account_index={"acc_1": acct})
        process_added(db, item, [posted], account_index={"acc_1": acct})
    rows = db.query(Transaction).filter_by(source="plaid").all()
    assert len(rows) == 1
    assert rows[0].source_id == "post1"
    assert rows[0].amount == Decimal("-22.50")


def test_pending_posted_as_transfer_demotes_to_needs_review(db):
    """P3-001: a pending row that auto-classified (non-transfer PFC) but posts as
    a Plaid TRANSFER-category settlement must be demoted to needs_review so a
    real internal transfer doesn't slip through as auto_classified income."""
    item, acct = _mapped(db)
    pending = _plaid_txn(transaction_id="pp1", amount=20.00, pending=True)
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        process_added(db, item, [pending], account_index={"acc_1": acct})
    prior = db.query(Transaction).filter_by(source_id="pp1").one()
    # Simulate the pending having auto-classified (non-transfer at pending time).
    prior.status = TransactionStatus.AUTO_CLASSIFIED.value
    db.commit()
    # dict-shaped PFC so raw_data (which stores to_dict) stays JSON-serializable;
    # _is_plaid_transfer_category handles dict and object shapes alike.
    pfc = {"primary": "TRANSFER_IN", "detailed": "TRANSFER_IN_ACCOUNT_TRANSFER"}
    posted = _plaid_txn(transaction_id="ppost1", amount=20.00, pending=False,
                        pending_transaction_id="pp1", personal_finance_category=pfc)
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        process_added(db, item, [posted], account_index={"acc_1": acct})
    row = db.query(Transaction).filter_by(source="plaid").one()
    assert row.source_id == "ppost1"
    assert row.status == TransactionStatus.NEEDS_REVIEW.value
    assert row.review_reason == "plaid: transfer-category — confirm transfer vs income"
    # The status flip is audited.
    events = db.query(AuditEvent).filter_by(transaction_id=row.id,
                                            field_changed="status").all()
    assert any(e.new_value == "needs_review" for e in events)


def test_pending_posted_non_transfer_keeps_status(db):
    """P3-001 negative: a pending→posted reconcile where the posted txn is NOT a
    transfer-category must NOT touch the prior row's status (no spurious demote)."""
    item, acct = _mapped(db)
    pending = _plaid_txn(transaction_id="pn1", amount=20.00, pending=True)
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        process_added(db, item, [pending], account_index={"acc_1": acct})
    prior = db.query(Transaction).filter_by(source_id="pn1").one()
    prior.status = TransactionStatus.AUTO_CLASSIFIED.value
    db.commit()
    posted = _plaid_txn(transaction_id="pnpost1", amount=22.50, pending=False,
                        pending_transaction_id="pn1")
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        process_added(db, item, [posted], account_index={"acc_1": acct})
    row = db.query(Transaction).filter_by(source="plaid").one()
    assert row.status == TransactionStatus.AUTO_CLASSIFIED.value


def test_pending_posted_card_payment_signal_reclassifies_direction(db):
    """P1-b2d: a card payment routinely arrives PENDING first with only a
    generic "PAYMENT" descriptor (no transaction_code/PFC yet), so it is
    classified normally by the 3-tier engine on insert. Once the POSTED
    payload carries the card-payment signal, the row must be reclassified
    direction=transfer — not left with its original income/expense
    classification and tax_category forever."""
    item, acct = _mapped(db)
    acct.account_type = "credit_card"
    db.commit()
    pending = _plaid_txn(transaction_id="cardpend1", amount=-50.0, pending=True,
                        name="PAYMENT")
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        process_added(db, item, [pending], account_index={"acc_1": acct})
    prior = db.query(Transaction).filter_by(source_id="cardpend1").one()
    # Classified normally at pending time — no card-payment signal yet.
    assert prior.direction == Direction.EXPENSE.value
    assert prior.tax_category == TaxCategory.MEALS.value

    posted = _plaid_txn(transaction_id="cardpost1", amount=-50.0, pending=False,
                        pending_transaction_id="cardpend1", name="PAYMENT",
                        transaction_code="payment")
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        process_added(db, item, [posted], account_index={"acc_1": acct})

    row = db.query(Transaction).filter_by(source="plaid").one()
    assert row.source_id == "cardpost1"
    assert row.direction == Direction.TRANSFER.value
    assert row.tax_category is None
    assert row.deductible_pct == 0.0
    direction_events = db.query(AuditEvent).filter_by(
        transaction_id=row.id, field_changed="direction"
    ).all()
    assert any(e.new_value == "transfer" for e in direction_events)


# ── Task 7 tests: process_modified (REQ-PT-003, REQ-PT-013) ──────────────────


def test_modified_updates_amount_but_preserves_confirmed_classification(db):
    item, acct = _mapped(db)
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        process_added(db, item, [_plaid_txn(transaction_id="m1", amount=10.0)],
                      account_index={"acc_1": acct})
    row = db.query(Transaction).filter_by(source_id="m1").one()
    row.status = "confirmed"
    row.tax_category = "OFFICE_EXPENSE"
    row.entity = "blackline"
    row.direction = "income"
    row.tax_subcategory = "home_office"
    db.commit()
    process_modified(db, [_plaid_txn(transaction_id="m1", amount=11.5)])
    db.refresh(row)
    assert row.amount == Decimal("-11.50")
    assert row.tax_category == "OFFICE_EXPENSE"
    assert row.entity == "blackline"
    assert row.status == "confirmed"
    # REQ-PT-013: human-set direction + tax_subcategory survive a modified refresh.
    assert row.direction == "income"
    assert row.tax_subcategory == "home_office"


def test_modified_amount_change_writes_audit_event(db):
    """REQ-PT-005 audit: an amount delta on modified leaves a field-level trail."""
    item, acct = _mapped(db)
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        process_added(db, item, [_plaid_txn(transaction_id="ma1", amount=10.0)],
                      account_index={"acc_1": acct})
    row = db.query(Transaction).filter_by(source_id="ma1").one()
    process_modified(db, [_plaid_txn(transaction_id="ma1", amount=15.0)])
    events = db.query(AuditEvent).filter_by(transaction_id=row.id,
                                            field_changed="amount").all()
    assert len(events) == 1
    assert Decimal(events[0].old_value) == Decimal("-10")
    assert Decimal(events[0].new_value) == Decimal("-15")
    assert events[0].entity_id is None and events[0].entity_type is None


def test_process_modified_new_card_payment_signal_reclassifies(db):
    """P1-b2d: Plaid can enrich a row's metadata (transaction_code/PFC) on a
    plain `modified` payload for an already-posted row — process_modified
    must re-check the card-payment signal and reclassify, not just refresh
    amount/date/description/raw_data."""
    item, acct = _mapped(db)
    acct.account_type = "credit_card"
    db.commit()
    original = _plaid_txn(transaction_id="modcard1", amount=-75.0, name="PAYMENT")
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        process_added(db, item, [original], account_index={"acc_1": acct})
    row = db.query(Transaction).filter_by(source_id="modcard1").one()
    assert row.direction == Direction.EXPENSE.value

    enriched = _plaid_txn(transaction_id="modcard1", amount=-75.0, name="PAYMENT",
                          transaction_code="payment")
    updated = process_modified(db, [enriched], account_index={"acc_1": acct})
    db.refresh(row)
    assert updated == 1
    assert row.direction == Direction.TRANSFER.value
    assert row.tax_category is None
    assert row.deductible_pct == 0.0


def test_process_modified_without_account_index_leaves_direction_alone(db):
    """process_modified's account_index param is optional (backward
    compatible) — without it, the card-payment reclassification simply
    cannot resolve an account_type and no-ops, rather than erroring."""
    item, acct = _mapped(db)
    acct.account_type = "credit_card"
    db.commit()
    original = _plaid_txn(transaction_id="modcard2", amount=-20.0, name="PAYMENT")
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        process_added(db, item, [original], account_index={"acc_1": acct})
    enriched = _plaid_txn(transaction_id="modcard2", amount=-20.0, name="PAYMENT",
                          transaction_code="payment")
    updated = process_modified(db, [enriched])  # no account_index
    row = db.query(Transaction).filter_by(source_id="modcard2").one()
    assert updated == 1
    assert row.direction == Direction.EXPENSE.value  # unscoped -> no signal -> untouched


def test_modified_on_rejected_row_refreshes_fields_status_untouched(db):
    """REQ-FIX-ING-007 decision table: `modified` on a rejected (non-split) row
    still refreshes volatile fields (audited), but status must NOT change —
    a human rejection is not a classification the sync should overwrite."""
    item, acct = _mapped(db)
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        process_added(db, item, [_plaid_txn(transaction_id="mr1", amount=10.0)],
                      account_index={"acc_1": acct})
    row = db.query(Transaction).filter_by(source_id="mr1").one()
    row.status = TransactionStatus.REJECTED.value
    row.review_reason = "human rejected this charge"
    db.commit()
    updated = process_modified(db, [_plaid_txn(transaction_id="mr1", amount=13.25)])
    assert updated == 1
    db.refresh(row)
    assert row.amount == Decimal("-13.25")
    assert row.status == TransactionStatus.REJECTED.value
    assert row.review_reason == "human rejected this charge"
    events = db.query(AuditEvent).filter_by(transaction_id=row.id,
                                            field_changed="amount").all()
    assert len(events) == 1


def test_process_modified_unknown_id_is_noop(db):
    """P2-002-TEST: process_modified on a source_id not in DB returns 0, no-op.
    Mirrors test_removed_unknown_id_is_noop for the analogous removed path."""
    result = process_modified(db, [_plaid_txn(transaction_id="ghost_mod")])
    assert result == 0
    assert db.query(Transaction).count() == 0


def test_modified_same_amount_no_audit_event(db):
    """P3-001-TEST: a modified event whose amount equals the stored row produces
    NO AuditEvent for 'amount'. Guards _apply_update's `old != new` guard."""
    item, acct = _mapped(db)
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        process_added(db, item, [_plaid_txn(transaction_id="sa1", amount=10.0)],
                      account_index={"acc_1": acct})
    db.query(Transaction).filter_by(source_id="sa1").one()
    before = db.query(AuditEvent).filter_by(field_changed="amount").count()
    # Re-send the same amount — _apply_update must not audit.
    process_modified(db, [_plaid_txn(transaction_id="sa1", amount=10.0)])
    after = db.query(AuditEvent).filter_by(field_changed="amount").count()
    assert after == before  # no new audit event


# ── Task 8 tests: process_removed (REQ-PT-004) ───────────────────────────────


def test_removed_marks_rejected_not_deleted(db):
    item, acct = _mapped(db)
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        process_added(db, item, [_plaid_txn(transaction_id="r1")],
                      account_index={"acc_1": acct})
    process_removed(db, [{"transaction_id": "r1"}])
    row = db.query(Transaction).filter_by(source_id="r1").one()  # still present
    assert row.status == "rejected"
    assert row.review_reason == "plaid_removed"


def test_removed_unknown_id_is_noop(db):
    assert process_removed(db, [{"transaction_id": "ghost"}]) == 0


def test_removed_writes_status_audit_event(db):
    """FIX 1 / REQ-PT-011: rejecting a removed txn must leave an audit trail."""
    item, acct = _mapped(db)
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        process_added(db, item, [_plaid_txn(transaction_id="r1")],
                      account_index={"acc_1": acct})
    row = db.query(Transaction).filter_by(source_id="r1").one()
    process_removed(db, [{"transaction_id": "r1"}])
    events = db.query(AuditEvent).filter_by(transaction_id=row.id,
                                            field_changed="status").all()
    assert len(events) == 1
    assert events[0].new_value == "rejected"
    assert events[0].entity_id is None and events[0].entity_type is None


def test_removed_twice_on_already_rejected_is_noop(db):
    """REQ-FIX-ING-007 decision table: `removed` on an already-rejected row is a
    no-op guard (skip re-audit) — Plaid can redeliver a removed entry across
    syncs. The second call must not write a duplicate status AuditEvent nor
    count the row as newly-processed."""
    item, acct = _mapped(db)
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        process_added(db, item, [_plaid_txn(transaction_id="rr2", amount=9.0)],
                      account_index={"acc_1": acct})
    first = process_removed(db, [{"transaction_id": "rr2"}])
    assert first == 1
    row = db.query(Transaction).filter_by(source_id="rr2").one()
    assert row.status == "rejected"
    events_before = db.query(AuditEvent).filter_by(transaction_id=row.id,
                                                    field_changed="status").count()
    second = process_removed(db, [{"transaction_id": "rr2"}])
    assert second == 0  # no-op: not counted as newly-processed
    db.refresh(row)
    assert row.status == "rejected"
    assert row.review_reason == "plaid_removed"
    events_after = db.query(AuditEvent).filter_by(transaction_id=row.id,
                                                   field_changed="status").count()
    assert events_after == events_before  # no duplicate no-op audit event


def test_pending_id_in_removed_is_noop_after_promotion(db):
    """REQ-PT-005: after pending→posted promotion the original pending id no
    longer exists, so it arriving in `removed` is a no-op (count 0) and the
    promoted row is untouched."""
    item, acct = _mapped(db)
    pending = _plaid_txn(transaction_id="p1", amount=20.00, pending=True)
    posted = _plaid_txn(transaction_id="post1", amount=22.50, pending=False,
                        pending_transaction_id="p1")
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        process_added(db, item, [pending], account_index={"acc_1": acct})
        process_added(db, item, [posted], account_index={"acc_1": acct})
    assert process_removed(db, [{"transaction_id": "p1"}]) == 0
    row = db.query(Transaction).filter_by(source="plaid").one()
    assert row.source_id == "post1"
    assert row.status != "rejected"


def test_removed_then_readded_is_reactivated(db):
    """REQ-PT-004: a removed (rejected) txn that Plaid re-adds must re-enter the
    register (reactivated to needs_review), not stay rejected forever."""
    item, acct = _mapped(db)
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        process_added(db, item, [_plaid_txn(transaction_id="rr1", amount=9.0)],
                      account_index={"acc_1": acct})
    process_removed(db, [{"transaction_id": "rr1"}])
    row = db.query(Transaction).filter_by(source_id="rr1").one()
    assert row.status == "rejected"
    # Plaid re-delivers the same id as added.
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        process_added(db, item, [_plaid_txn(transaction_id="rr1", amount=9.0)],
                      account_index={"acc_1": acct})
    db.refresh(row)
    assert row.status == TransactionStatus.NEEDS_REVIEW.value
    assert row.review_reason == "plaid_readded"
    # status flip is audited
    events = db.query(AuditEvent).filter_by(transaction_id=row.id,
                                            field_changed="status").all()
    assert any(e.new_value == "needs_review" for e in events)


def test_readded_row_gets_card_payment_reclassification(db):
    """P1-b2d: a re-added (previously removed) row's payload may now carry
    the card-payment signal — the reactivation branch must reclassify it too,
    not just the insert and pending→posted paths."""
    item, acct = _mapped(db)
    acct.account_type = "credit_card"
    db.commit()
    original = _plaid_txn(transaction_id="readd_card1", amount=-40.0, name="PAYMENT")
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        process_added(db, item, [original], account_index={"acc_1": acct})
    process_removed(db, [{"transaction_id": "readd_card1"}])
    row = db.query(Transaction).filter_by(source_id="readd_card1").one()
    assert row.status == "rejected"

    readded = _plaid_txn(transaction_id="readd_card1", amount=-40.0, name="PAYMENT",
                         transaction_code="payment")
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        counts = process_added(db, item, [readded], account_index={"acc_1": acct})
    db.refresh(row)
    assert counts.reactivated == 1
    assert row.status == TransactionStatus.NEEDS_REVIEW.value
    assert row.direction == Direction.TRANSFER.value
    assert row.tax_category is None
    assert row.deductible_pct == 0.0


def test_added_replay_against_human_rejected_row_is_noop(db):
    """REQ-FIX-ING-007 decision table: `added` replayed against a row a human
    rejected (any review_reason other than 'plaid_removed') must NOT be
    reactivated — a human veto sticks. Only the plaid_removed reactivation
    path (test_removed_then_readded_is_reactivated) resurrects a row."""
    item, acct = _mapped(db)
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        process_added(db, item, [_plaid_txn(transaction_id="hr1", amount=9.0)],
                      account_index={"acc_1": acct})
    row = db.query(Transaction).filter_by(source_id="hr1").one()
    row.status = TransactionStatus.REJECTED.value
    row.review_reason = "human rejected this charge"
    db.commit()
    events_before = db.query(AuditEvent).filter_by(transaction_id=row.id).count()
    # Plaid re-delivers the same id in `added` (e.g. a resync).
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        counts = process_added(db, item, [_plaid_txn(transaction_id="hr1", amount=9.0)],
                               account_index={"acc_1": acct})
    db.refresh(row)
    assert row.status == TransactionStatus.REJECTED.value
    assert row.review_reason == "human rejected this charge"
    assert counts.inserted == 0 and counts.reactivated == 0
    events_after = db.query(AuditEvent).filter_by(transaction_id=row.id).count()
    assert events_after == events_before  # no audit trail from a skipped no-op


def test_process_added_reports_reactivated_count(db):
    """REQ-PT-004: process_added returns reactivated separately from inserted so
    a reinstated (plaid_readded) row is visible in operator metrics."""
    item, acct = _mapped(db)
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        c1 = process_added(db, item, [_plaid_txn(transaction_id="rc1", amount=9.0)],
                           account_index={"acc_1": acct})
    assert c1.inserted == 1 and c1.reactivated == 0
    process_removed(db, [{"transaction_id": "rc1"}])
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        c2 = process_added(db, item, [_plaid_txn(transaction_id="rc1", amount=9.0)],
                           account_index={"acc_1": acct})
    assert c2.inserted == 0 and c2.reactivated == 1


def test_sync_one_item_accumulates_reactivated(db):
    """sync_one_item rolls reactivations into result.reactivated / batch totals."""
    item, acct = _mapped(db)
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        process_added(db, item, [_plaid_txn(transaction_id="sr1", amount=5.0)],
                      account_index={"acc_1": acct})
    process_removed(db, [{"transaction_id": "sr1"}])
    db.commit()
    client = mock.Mock()
    client.transactions_sync.side_effect = [
        _sync_resp(added=[_plaid_txn(transaction_id="sr1", account_id="acc_1", amount=5.0)],
                   has_more=False, next_cursor="c")
    ]
    with mock.patch("src.adapters.plaid_transactions.decrypt_token", return_value="tok"), \
         mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        result = sync_one_item(db, item, client=client)
    db.commit()
    assert result.reactivated == 1
    assert result.added == 0
    # P2-001: records_processed must include reactivated rows (else a sync that
    # only reactivates logs records_processed=0 and is invisible to ops).
    log = db.query(IngestionLog).filter_by(source="plaid_tx:Chase").one()
    assert log.records_processed == 1


def test_removed_skips_split_parent(db):
    """A split_parent row must NOT be rejected by process_removed (would orphan
    its split children)."""
    item, acct = _mapped(db)
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        process_added(db, item, [_plaid_txn(transaction_id="sp1")],
                      account_index={"acc_1": acct})
    row = db.query(Transaction).filter_by(source_id="sp1").one()
    row.status = TransactionStatus.SPLIT_PARENT.value
    db.commit()
    assert process_removed(db, [{"transaction_id": "sp1"}]) == 0
    db.refresh(row)
    assert row.status == TransactionStatus.SPLIT_PARENT.value


def test_modified_skips_split_parent(db):
    """A Plaid 'modified' on a split_parent row must NOT change the parent's
    amount (would break the split-sum invariant); skip + warn like removed."""
    item, acct = _mapped(db)
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        process_added(db, item, [_plaid_txn(transaction_id="msp1", amount=10.0)],
                      account_index={"acc_1": acct})
    row = db.query(Transaction).filter_by(source_id="msp1").one()
    row.status = TransactionStatus.SPLIT_PARENT.value
    db.commit()
    original_amount = row.amount
    # Plaid sends a modified event with a different amount.
    assert process_modified(db, [_plaid_txn(transaction_id="msp1", amount=99.0)]) == 0
    db.refresh(row)
    assert row.amount == original_amount
    assert row.status == TransactionStatus.SPLIT_PARENT.value


def test_added_readded_skips_split_parent(db):
    """P2-005: process_added's reactivation path must skip a split_parent row.
    Re-applying a Plaid re-delivered amount to a split parent would overwrite the
    parent total and break the split-sum invariant for its children."""
    item, acct = _mapped(db)
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        process_added(db, item, [_plaid_txn(transaction_id="spr1", amount=10.0)],
                      account_index={"acc_1": acct})
    row = db.query(Transaction).filter_by(source_id="spr1").one()
    row.status = TransactionStatus.SPLIT_PARENT.value
    original_amount = row.amount
    db.commit()
    # Plaid re-delivers the same id as added with a different amount.
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        counts = process_added(db, item,
                               [_plaid_txn(transaction_id="spr1", amount=99.0)],
                               account_index={"acc_1": acct})
    db.refresh(row)
    assert counts.inserted == 0 and counts.reactivated == 0
    assert row.status == TransactionStatus.SPLIT_PARENT.value
    assert row.amount == original_amount


def test_pending_posted_reconcile_skips_split_parent(db):
    """P1-001-FIN / P2-001-TEST: the pending→posted reconcile path must NOT
    overwrite a split_parent's amount. A human can split a still-pending charge
    (status=split_parent); when Plaid posts the settled version with a different
    amount, _apply_update would clobber the parent total and break the split-sum
    invariant for the children. The centralized _apply_update guard refuses the
    mutation, so the reconcile path leaves the parent untouched: amount unchanged,
    status still split_parent, and source_id NOT promoted to the posted id."""
    item, acct = _mapped(db)
    pending = _plaid_txn(transaction_id="psp1", amount=500.00, pending=True)
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        process_added(db, item, [pending], account_index={"acc_1": acct})
    prior = db.query(Transaction).filter_by(source_id="psp1").one()
    # Human splits the still-pending charge into legs (parent becomes split_parent).
    prior.status = TransactionStatus.SPLIT_PARENT.value
    original_amount = prior.amount
    db.commit()
    # Plaid posts the settled version with a different amount, keyed off the
    # pending id via pending_transaction_id.
    posted = _plaid_txn(transaction_id="psppost1", amount=512.50, pending=False,
                        pending_transaction_id="psp1")
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        counts = process_added(db, item, [posted], account_index={"acc_1": acct})
    db.refresh(prior)
    # Parent untouched: amount, status, and source_id all unchanged.
    assert prior.amount == original_amount
    assert prior.status == TransactionStatus.SPLIT_PARENT.value
    assert prior.source_id == "psp1"
    # The posted txn was neither inserted as a duplicate nor reconciled onto the
    # parent (a split parent must not be silently re-amounted).
    assert counts.inserted == 0 and counts.reactivated == 0
    assert db.query(Transaction).filter_by(source="plaid").count() == 1


# ── Task 9 tests: fetch_all_pages (REQ-PT-001, REQ-PT-006) ───────────────────


def _sync_resp(added=(), modified=(), removed=(), next_cursor="c1", has_more=False):
    return SimpleNamespace(added=list(added), modified=list(modified),
                           removed=list(removed), next_cursor=next_cursor, has_more=has_more)


def test_fetch_all_pages_concatenates_until_has_more_false(db):
    item, acct = _mapped(db)
    client = mock.Mock()
    client.transactions_sync.side_effect = [
        _sync_resp(added=[_plaid_txn(transaction_id="a")], next_cursor="c1", has_more=True),
        _sync_resp(added=[_plaid_txn(transaction_id="b")], next_cursor="c2", has_more=False),
    ]
    added, modified, removed, cursor = fetch_all_pages(client, "tok", cursor=None)
    assert [t.transaction_id for t in added] == ["a", "b"]
    assert cursor == "c2"
    assert client.transactions_sync.call_count == 2


# ── Task 10 tests: supersede_csv_rows (REQ-PT-011) ───────────────────────────


def test_supersede_rejects_overlapping_csv_rows_only(db):
    item, acct = _mapped(db, pm="Chase ****1234")
    db.add(Transaction(source="bank_csv", source_id="c1", source_hash="h1", date="2026-03-15",
                       description="x", amount=Decimal("-5"), currency="USD", entity="sparkry",
                       status="confirmed", confidence=0.0, payment_method="Chase ****1234",
                       raw_data={}))
    db.add(Transaction(source="bank_csv", source_id="c2", source_hash="h2", date="2025-01-01",
                       description="old", amount=Decimal("-5"), currency="USD", entity="sparkry",
                       status="confirmed", confidence=0.0, payment_method="Chase ****1234",
                       raw_data={}))
    db.add(Transaction(source="bank_csv", source_id="c3", source_hash="h3", date="2026-03-15",
                       description="other", amount=Decimal("-5"), currency="USD", entity="sparkry",
                       status="confirmed", confidence=0.0, payment_method="Amex ****9999",
                       raw_data={}))
    db.commit()
    n = supersede_csv_rows(db, payment_method="Chase ****1234",
                           covered_min="2026-01-01", covered_max="2026-05-31")
    assert n == 1
    assert db.query(Transaction).filter_by(source_id="c1").one().status == "rejected"
    assert db.query(Transaction).filter_by(source_id="c2").one().status == "confirmed"
    assert db.query(Transaction).filter_by(source_id="c3").one().status == "confirmed"


def test_supersede_noop_when_label_blank(db):
    assert supersede_csv_rows(db, payment_method=None,
                              covered_min="2026-01-01", covered_max="2026-05-31") == 0


def test_supersede_writes_status_audit_event(db):
    """FIX 1 / REQ-PT-011: superseding a CSV row must leave an audit trail."""
    item, acct = _mapped(db, pm="Chase ****1234")
    db.add(Transaction(source="bank_csv", source_id="c1", source_hash="h1",
                       date="2026-03-15", description="x", amount=Decimal("-5"),
                       currency="USD", entity="sparkry", status="confirmed",
                       confidence=0.0, payment_method="Chase ****1234", raw_data={}))
    db.commit()
    n = supersede_csv_rows(db, payment_method="Chase ****1234",
                           covered_min="2026-01-01", covered_max="2026-05-31")
    assert n == 1
    row = db.query(Transaction).filter_by(source_id="c1").one()
    events = db.query(AuditEvent).filter_by(transaction_id=row.id,
                                            field_changed="status").all()
    assert len(events) == 1
    assert events[0].old_value == "confirmed"
    assert events[0].new_value == "rejected"
    assert events[0].entity_id is None and events[0].entity_type is None


def test_supersede_targets_only_bank_csv_not_other_sources(db):
    """REQ-PT-011 narrowing: a Stripe/Gmail row sharing the payment_method label
    inside the window is NOT collateral; only bank_csv rows are superseded.
    A split_parent bank_csv row is also spared (orphan guard)."""
    item, acct = _mapped(db, pm="Chase ****1234")
    common = dict(date="2026-03-15", amount=Decimal("-5"), currency="USD",
                  entity="sparkry", confidence=0.0,
                  payment_method="Chase ****1234", raw_data={})
    db.add(Transaction(source="bank_csv", source_id="bc", source_hash="hbc",
                       description="csv", status="confirmed", **common))
    db.add(Transaction(source="stripe", source_id="st", source_hash="hst",
                       description="stripe payout", status="confirmed", **common))
    db.add(Transaction(source="gmail_n8n", source_id="gm", source_hash="hgm",
                       description="receipt", status="needs_review", **common))
    db.add(Transaction(source="bank_csv", source_id="sp", source_hash="hsp",
                       description="parent", status="split_parent", **common))
    db.commit()
    n = supersede_csv_rows(db, payment_method="Chase ****1234",
                           covered_min="2026-01-01", covered_max="2026-05-31")
    assert n == 1
    assert db.query(Transaction).filter_by(source_id="bc").one().status == "rejected"
    assert db.query(Transaction).filter_by(source_id="st").one().status == "confirmed"
    assert db.query(Transaction).filter_by(source_id="gm").one().status == "needs_review"
    assert db.query(Transaction).filter_by(source_id="sp").one().status == "split_parent"


# ── Task 11 tests: sync_one_item orchestration (REQ-PT-001,006,007,011,016) ──


def test_sync_one_item_full_flow_first_sync_sets_cursor(db):
    item, acct = _mapped(db)
    client = mock.Mock()
    client.transactions_sync.side_effect = [
        _sync_resp(added=[_plaid_txn(transaction_id="t1", account_id="acc_1")],
                   next_cursor="cur1", has_more=False)
    ]
    with mock.patch("src.adapters.plaid_transactions.decrypt_token", return_value="tok"), \
         mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        result = sync_one_item(db, item, client=client)
    db.commit()
    assert result.status == "ok"
    assert result.added == 1
    assert db.query(Transaction).filter_by(source="plaid", source_id="t1").count() == 1
    db.refresh(item)
    assert item.cursor == "cur1"
    # P2-003: the SUCCESS (no-failure) path must write a SUCCESS IngestionLog with
    # the processed count and zero failures — guards against a silent regression
    # that flips status to FAILURE or undercounts records_processed on a clean run.
    log = db.query(IngestionLog).filter_by(source="plaid_tx:Chase").one()
    assert log.status == IngestionStatus.SUCCESS.value
    assert log.records_processed == 1
    assert log.records_failed == 0


def test_sync_one_item_per_row_isolation(db):
    item, acct = _mapped(db)
    client = mock.Mock()
    client.transactions_sync.side_effect = [
        _sync_resp(added=[_plaid_txn(transaction_id="bad", account_id="acc_1"),
                          _plaid_txn(transaction_id="ok", account_id="acc_1")],
                   has_more=False, next_cursor="c")
    ]
    # classify raises only for the 'bad' txn (matched by description/source_id via the tx).
    def _cls_side(tx, session):
        if tx.source_id == "bad":
            raise ValueError("boom")
        return _cls()
    with mock.patch("src.adapters.plaid_transactions.decrypt_token", return_value="tok"), \
         mock.patch("src.adapters.plaid_transactions.classify", side_effect=_cls_side):
        result = sync_one_item(db, item, client=client)
    db.commit()
    assert result.failed == 1
    assert db.query(Transaction).filter_by(source_id="ok").count() == 1
    assert db.query(Transaction).filter_by(source_id="bad").count() == 0
    # REQ-PT-007: IngestionLog reflects the partial failure.
    log = db.query(IngestionLog).filter_by(source="plaid_tx:Chase").one()
    assert log.status == IngestionStatus.PARTIAL_FAILURE.value
    assert log.records_failed == 1
    # 1 ok row processed (the 'bad' row failed) — records_processed must reflect it.
    assert log.records_processed == 1
    # REQ-PT-006: a per-row failure must NOT advance the cursor (so the failed
    # row is re-delivered next run).
    db.refresh(item)
    assert item.cursor is None
    assert result.status == "error"


def test_partial_failure_holds_cursor_then_clean_rerun_ingests(db):
    """REQ-PT-006: after a partial-failure sync the cursor is held; a clean
    re-run from the same cursor then ingests the previously-failed row."""
    item, acct = _mapped(db)

    def _client():
        c = mock.Mock()
        c.transactions_sync.side_effect = [
            _sync_resp(added=[_plaid_txn(transaction_id="late", account_id="acc_1")],
                       has_more=False, next_cursor="cur1")
        ]
        return c

    calls = {"n": 0}

    def _cls_first_fails(tx, session):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("transient")
        return _cls()

    with mock.patch("src.adapters.plaid_transactions.decrypt_token", return_value="tok"), \
         mock.patch("src.adapters.plaid_transactions.classify", side_effect=_cls_first_fails):
        sync_one_item(db, item, client=_client())
        db.commit()
        # Cursor held at None (failure), row not ingested.
        db.refresh(item)
        assert item.cursor is None
        assert db.query(Transaction).filter_by(source_id="late").count() == 0
        # Clean re-run from the same cursor re-delivers and ingests it.
        sync_one_item(db, item, client=_client())
        db.commit()
    db.refresh(item)
    assert item.cursor == "cur1"
    assert db.query(Transaction).filter_by(source_id="late").count() == 1


# ── Error-path tests (REQ-PT-006, REQ-PT-016) ───────────────────────────────


def test_sync_one_item_retryable_error_holds_cursor(db):
    """RetryablePlaidError (INSTITUTION_DOWN): status institution_down, cursor
    unchanged, last_error set, IngestionLog failure + retryable."""
    item, acct = _mapped(db)
    item.cursor = "prev_cursor"
    db.commit()
    client = mock.Mock()
    with mock.patch("src.adapters.plaid_transactions.decrypt_token", return_value="tok"), \
         mock.patch("src.adapters.plaid_transactions.fetch_all_pages",
                    side_effect=RetryablePlaidError("INSTITUTION_DOWN")):
        result = sync_one_item(db, item, client=client)
    db.commit()
    assert result.status == "institution_down"
    assert result.error_code == "INSTITUTION_DOWN"
    db.refresh(item)
    assert item.cursor == "prev_cursor"
    assert item.last_sync_status == "institution_down"
    assert item.last_error == "INSTITUTION_DOWN"
    log = db.query(IngestionLog).filter_by(source="plaid_tx:Chase").one()
    assert log.status == IngestionStatus.FAILURE.value
    assert log.retryable is True
    assert log.error_detail == "INSTITUTION_DOWN"


def test_sync_one_item_invalid_ciphertext_is_terminal_holds_cursor(db):
    """InvalidCiphertextError → TerminalPlaidError(INVALID_ACCESS_TOKEN): status
    error, cursor unchanged, last_error set."""
    item, acct = _mapped(db)
    item.cursor = "prev_cursor"
    db.commit()
    client = mock.Mock()
    with mock.patch("src.adapters.plaid_transactions.decrypt_token",
                    side_effect=InvalidCiphertextError("bad")):
        result = sync_one_item(db, item, client=client)
    db.commit()
    assert result.status == "error"
    assert result.error_code == "INVALID_ACCESS_TOKEN"
    db.refresh(item)
    assert item.cursor == "prev_cursor"
    assert item.last_sync_status == "error"
    assert item.last_error == "INVALID_ACCESS_TOKEN"
    log = db.query(IngestionLog).filter_by(source="plaid_tx:Chase").one()
    assert log.status == IngestionStatus.FAILURE.value
    # P3-003: retryable=False is the IngestionLog column default. This assertion
    # (and the matching ones in the terminal/unexpected tests below) is partially
    # tautological today — it cannot fail unless the implementation is changed.
    # It is retained deliberately to guard against a FUTURE mutation that
    # incorrectly sets retryable=True in a terminal/unexpected handler. The
    # meaningful positive coverage lives in the two RetryablePlaidError tests
    # (INSTITUTION_DOWN and RATE_LIMIT_EXCEEDED) which assert retryable is True.
    assert log.retryable is False


def test_sync_one_item_terminal_error_does_not_advance_cursor(db):
    """REQ-PT-016: TerminalPlaidError(ITEM_LOGIN_REQUIRED) holds cursor, sets
    last_sync_status='error' + last_error code."""
    item, acct = _mapped(db)
    item.cursor = "prev_cursor"
    db.commit()
    client = mock.Mock()
    with mock.patch("src.adapters.plaid_transactions.decrypt_token", return_value="tok"), \
         mock.patch("src.adapters.plaid_transactions.fetch_all_pages",
                    side_effect=TerminalPlaidError("ITEM_LOGIN_REQUIRED")):
        result = sync_one_item(db, item, client=client)
    db.commit()
    assert result.error_code == "ITEM_LOGIN_REQUIRED"
    db.refresh(item)
    assert item.cursor == "prev_cursor"
    assert item.last_sync_status == "error"
    assert item.last_error == "ITEM_LOGIN_REQUIRED"
    # REQ-PT-016: terminal error writes a FAILURE log, non-retryable. Guards
    # against a silent log-write regression.
    log = db.query(IngestionLog).filter_by(source="plaid_tx:Chase").one()
    assert log.status == IngestionStatus.FAILURE.value
    assert log.retryable is False
    assert log.error_detail == "ITEM_LOGIN_REQUIRED"


def test_sync_one_item_unexpected_error_holds_cursor(db):
    """A generic Exception → status error, error_code UNEXPECTED, cursor held."""
    item, acct = _mapped(db)
    item.cursor = "prev_cursor"
    db.commit()
    client = mock.Mock()
    with mock.patch("src.adapters.plaid_transactions.decrypt_token", return_value="tok"), \
         mock.patch("src.adapters.plaid_transactions.fetch_all_pages",
                    side_effect=RuntimeError("boom")):
        result = sync_one_item(db, item, client=client)
    db.commit()
    assert result.status == "error"
    assert result.error_code == "UNEXPECTED"
    db.refresh(item)
    assert item.cursor == "prev_cursor"
    # REQ-PT-007: unexpected error writes a FAILURE log, non-retryable, with the
    # 'unexpected:' error_detail prefix. Guards against silent log-write loss.
    log = db.query(IngestionLog).filter_by(source="plaid_tx:Chase").one()
    assert log.status == IngestionStatus.FAILURE.value
    assert log.retryable is False
    assert log.error_detail.startswith("unexpected:")


def test_sync_one_item_rate_limit_sets_status_error(db):
    """P2-006: a RetryablePlaidError whose code is NOT an institution-down code
    (e.g. RATE_LIMIT_EXCEEDED) must set status/last_sync_status='error' (NOT
    'institution_down'), hold the cursor, and write a retryable FAILURE log.
    Exercises the ternary else-branch — a test fixed to INSTITUTION_DOWN can't."""
    item, acct = _mapped(db)
    item.cursor = "prev_cursor"
    db.commit()
    client = mock.Mock()
    with mock.patch("src.adapters.plaid_transactions.decrypt_token", return_value="tok"), \
         mock.patch("src.adapters.plaid_transactions.fetch_all_pages",
                    side_effect=RetryablePlaidError("RATE_LIMIT_EXCEEDED")):
        result = sync_one_item(db, item, client=client)
    db.commit()
    assert result.status == "error"
    assert result.error_code == "RATE_LIMIT_EXCEEDED"
    db.refresh(item)
    assert item.cursor == "prev_cursor"
    assert item.last_sync_status == "error"
    log = db.query(IngestionLog).filter_by(source="plaid_tx:Chase").one()
    assert log.status == IngestionStatus.FAILURE.value
    assert log.retryable is True
    assert log.error_detail == "RATE_LIMIT_EXCEEDED"


def test_supersede_failure_holds_cursor(db):
    """P2-007: if supersede_csv_rows raises during a first sync, the failure is
    classified (result.failed += 1) and the cursor is held (NOT advanced) so a
    clean re-run can complete the supersede. Guards REQ-PT-006/011."""
    item, acct = _mapped(db, pm="Chase ****1234")
    client = mock.Mock()
    client.transactions_sync.side_effect = [
        _sync_resp(added=[_plaid_txn(transaction_id="s1", account_id="acc_1")],
                   has_more=False, next_cursor="cur1")
    ]
    with mock.patch("src.adapters.plaid_transactions.decrypt_token", return_value="tok"), \
         mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()), \
         mock.patch("src.adapters.plaid_transactions.supersede_csv_rows",
                    side_effect=RuntimeError("supersede boom")):
        result = sync_one_item(db, item, client=client)
    db.commit()
    assert result.failed == 1
    assert result.status == "error"
    # The added row itself still ingested (failure was only in supersede).
    assert db.query(Transaction).filter_by(source="plaid", source_id="s1").count() == 1
    # Cursor held so the supersede is retried on a clean re-run.
    db.refresh(item)
    assert item.cursor is None


def test_sync_one_item_modified_row_failure_holds_cursor(db):
    """P2-008: a per-row failure inside a `modified` event must be isolated
    (result.failed += 1) and hold the cursor, mirroring the added-row isolation.
    Other events in the batch still process."""
    item, acct = _mapped(db)
    # Seed a row that the modified event will target.
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        process_added(db, item, [_plaid_txn(transaction_id="mod1", amount=10.0)],
                      account_index={"acc_1": acct})
    db.commit()
    client = mock.Mock()
    client.transactions_sync.side_effect = [
        _sync_resp(modified=[_plaid_txn(transaction_id="mod1", amount=11.0)],
                   removed=[{"transaction_id": "rem_ghost"}],
                   has_more=False, next_cursor="cur1")
    ]
    with mock.patch("src.adapters.plaid_transactions.decrypt_token", return_value="tok"), \
         mock.patch("src.adapters.plaid_transactions.process_modified",
                    side_effect=RuntimeError("modify boom")):
        result = sync_one_item(db, item, client=client)
    db.commit()
    assert result.failed == 1
    # The removed event (a no-op ghost) still ran — batch wasn't aborted.
    assert result.removed == 0
    # Cursor held on partial failure.
    db.refresh(item)
    assert item.cursor is None
    assert result.status == "error"


def test_sync_one_item_removed_row_failure_holds_cursor(db):
    """P2-008: a per-row failure inside a `removed` event is isolated and holds
    the cursor, mirroring added/modified isolation."""
    item, acct = _mapped(db)
    client = mock.Mock()
    client.transactions_sync.side_effect = [
        _sync_resp(removed=[{"transaction_id": "rem1"}],
                   has_more=False, next_cursor="cur1")
    ]
    with mock.patch("src.adapters.plaid_transactions.decrypt_token", return_value="tok"), \
         mock.patch("src.adapters.plaid_transactions.process_removed",
                    side_effect=RuntimeError("remove boom")):
        result = sync_one_item(db, item, client=client)
    db.commit()
    assert result.failed == 1
    db.refresh(item)
    assert item.cursor is None
    assert result.status == "error"


def test_first_sync_with_only_modified_skips_supersede(db):
    """P3-002-TEST: first sync (item.cursor=None) returning only a modified event
    (no added rows) must NOT attempt supersede. Guards the `if first_sync and
    added:` short-circuit — a regression removing `and added` would incorrectly
    invoke supersede_csv_rows with an empty date range."""
    item, acct = _mapped(db, pm="Chase ****1234")
    # Pre-seed a bank_csv row; it must survive untouched (supersede skipped).
    db.add(Transaction(source="bank_csv", source_id="csv_keep", source_hash="hk",
                       date="2026-03-15", description="x", amount=Decimal("-5"),
                       currency="USD", entity="sparkry", status="confirmed",
                       confidence=0.0, payment_method="Chase ****1234", raw_data={}))
    # Seed a Plaid row so the modified event has a target.
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        process_added(db, item, [_plaid_txn(transaction_id="mod_only", account_id="acc_1",
                                            amount=10.0)],
                      account_index={"acc_1": acct})
    db.commit()
    assert item.cursor is None  # confirm first sync
    client = mock.Mock()
    client.transactions_sync.side_effect = [
        _sync_resp(modified=[_plaid_txn(transaction_id="mod_only", amount=11.0)],
                   has_more=False, next_cursor="cur_first")
    ]
    with mock.patch("src.adapters.plaid_transactions.decrypt_token", return_value="tok"):
        result = sync_one_item(db, item, client=client)
    db.commit()
    # No supersede occurred — the bank_csv row is unaffected.
    assert result.superseded == 0
    assert db.query(Transaction).filter_by(source_id="csv_keep").one().status == "confirmed"
    # Cursor advances (no failures).
    db.refresh(item)
    assert item.cursor == "cur_first"


def test_non_first_sync_does_not_supersede(db):
    """REQ-PT-011: supersede runs only on the first sync. With cursor already
    set, a pre-existing confirmed bank_csv row must survive."""
    item, acct = _mapped(db, pm="Chase ****1234")
    item.cursor = "existing_cursor"
    db.add(Transaction(source="bank_csv", source_id="keep", source_hash="hk",
                       date="2026-03-15", description="x", amount=Decimal("-5"),
                       currency="USD", entity="sparkry", status="confirmed",
                       confidence=0.0, payment_method="Chase ****1234", raw_data={}))
    db.commit()
    client = mock.Mock()
    client.transactions_sync.side_effect = [
        _sync_resp(added=[_plaid_txn(transaction_id="n1", account_id="acc_1",
                                     date="2026-03-15")],
                   has_more=False, next_cursor="next_cursor")
    ]
    with mock.patch("src.adapters.plaid_transactions.decrypt_token", return_value="tok"), \
         mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        result = sync_one_item(db, item, client=client)
    db.commit()
    assert result.superseded == 0
    assert db.query(Transaction).filter_by(source_id="keep").one().status == "confirmed"


# ── Task 12 tests: sync_all_active batch driver, DRY-RUN default (REQ-PT-001) ──


def test_sync_all_active_dry_run_rolls_back(db):
    item, acct = _mapped(db)
    client = mock.Mock()
    client.transactions_sync.side_effect = [
        _sync_resp(added=[_plaid_txn(transaction_id="d1", account_id="acc_1")], has_more=False, next_cursor="c")
    ]
    with mock.patch("src.adapters.plaid_transactions.decrypt_token", return_value="tok"), \
         mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        batch = sync_all_active(db, client=client, dry_run=True)
    assert batch.dry_run is True
    assert db.query(Transaction).filter_by(source_id="d1").count() == 0


def test_sync_all_active_apply_commits(db):
    item, acct = _mapped(db)
    client = mock.Mock()
    client.transactions_sync.side_effect = [
        _sync_resp(added=[_plaid_txn(transaction_id="a1", account_id="acc_1")], has_more=False, next_cursor="c")
    ]
    with mock.patch("src.adapters.plaid_transactions.decrypt_token", return_value="tok"), \
         mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        sync_all_active(db, client=client, dry_run=False)
    assert db.query(Transaction).filter_by(source_id="a1").count() == 1


# ── FIX 3: per-account supersede date range (REQ-PT-011) ──────────────────────


def _add_account(db, item, *, plaid_account_id, pm, entity="sparkry"):
    acct = Account(broker="chase", account_number=plaid_account_id[-4:],
                   account_name=plaid_account_id, account_type="checking",
                   entity=entity, payment_method=pm, plaid_item_id=item.id,
                   plaid_account_id=plaid_account_id)
    db.add(acct)
    db.commit()
    return acct


def test_supersede_range_is_per_account(db):
    """A multi-account Item must NOT widen one account's supersede range with
    another account's added-txn dates."""
    item, acct_a = _mapped(db, plaid_account_id="acc_A", pm="Chase A ****1111")
    _add_account(db, item, plaid_account_id="acc_B", pm="Chase B ****2222")

    # Pre-seed CSV rows: A overlaps March (will be superseded);
    # B's row is dated March but B's only added txn is in January (must survive).
    db.add(Transaction(source="bank_csv", source_id="csv_a", source_hash="ha",
                       date="2026-03-15", description="a", amount=Decimal("-5"),
                       currency="USD", entity="sparkry", status="confirmed",
                       confidence=0.0, payment_method="Chase A ****1111", raw_data={}))
    db.add(Transaction(source="bank_csv", source_id="csv_b", source_hash="hb",
                       date="2026-03-15", description="b", amount=Decimal("-5"),
                       currency="USD", entity="sparkry", status="confirmed",
                       confidence=0.0, payment_method="Chase B ****2222", raw_data={}))
    db.commit()

    client = mock.Mock()
    client.transactions_sync.side_effect = [
        _sync_resp(added=[
            _plaid_txn(transaction_id="pa1", account_id="acc_A", date="2026-03-01"),
            _plaid_txn(transaction_id="pa2", account_id="acc_A", date="2026-03-31"),
            _plaid_txn(transaction_id="pb", account_id="acc_B", date="2026-01-05"),
        ], has_more=False, next_cursor="c")
    ]
    with mock.patch("src.adapters.plaid_transactions.decrypt_token", return_value="tok"), \
         mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        sync_one_item(db, item, client=client)
    db.commit()

    # A's March CSV row IS rejected (A covers March).
    assert db.query(Transaction).filter_by(source_id="csv_a").one().status == "rejected"
    # B's March CSV row is NOT rejected (B's coverage is January only).
    assert db.query(Transaction).filter_by(source_id="csv_b").one().status == "confirmed"


# ── FIX 4: coverage gaps ──────────────────────────────────────────────────────


def test_process_added_known_mirror_account_is_skipped_not_ingested(db):
    """REQ-WBR-LED-014 case A: a KNOWN mirror account_id this Item does not
    own creates NO row and is skipped silently-but-counted.

    Replaces the prior behaviour (ingest with entity=None -> needs_review),
    which produced 50 phantom mirror rows in production once two Chase Items
    covered the same login and each /transactions/sync returned all three
    accounts."""
    item, acct = _mapped(db)
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        counts = process_added(
            db, item,
            [_plaid_txn(transaction_id="u1", account_id=_A_KNOWN_MIRROR_ID)],
            account_index={},
        )
    assert db.query(Transaction).filter_by(source_id="u1").first() is None
    assert counts.inserted == 0
    assert counts.skipped_unknown_account == {_A_KNOWN_MIRROR_ID: 1}


def test_process_added_unrecognized_account_raises_not_silently_skipped(db):
    """P1-002/P1-c4f (REQ-WBR-LED-014 case B): an account_id that is NEITHER
    mapped NOR a known mirror is a genuinely new, not-yet-mapped account.

    Raising (rather than the old silent skip) is what makes sync_one_item
    hold the cursor and trip the OnFailure alert instead of PERMANENTLY
    dropping this account's transactions (/transactions/sync never
    re-delivers a passed cursor)."""
    item, acct = _mapped(db)
    with (
        mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()),
        pytest.raises(UnrecognizedPlaidAccountError) as exc_info,
    ):
        process_added(
            db, item,
            [_plaid_txn(transaction_id="u1", account_id="acc_genuinely_new")],
            account_index={},
        )
    assert exc_info.value.account_id == "acc_genuinely_new"
    assert db.query(Transaction).filter_by(source_id="u1").first() is None


def test_pending_id_nonexistent_prior_inserts_new(db):
    """pending_transaction_id pointing at a row that doesn't exist -> insert."""
    item, acct = _mapped(db)
    posted = _plaid_txn(transaction_id="post_only", pending_transaction_id="never_seen")
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        process_added(db, item, [posted], account_index={"acc_1": acct})
    rows = db.query(Transaction).filter_by(source="plaid").all()
    assert len(rows) == 1
    assert rows[0].source_id == "post_only"


def test_sync_one_item_rerun_from_same_cursor_is_idempotent(db):
    """Re-running sync from the same starting cursor with the same added txn
    must not duplicate the row (source_id idempotency at orchestration level)."""
    item, acct = _mapped(db)

    def _fresh_client():
        c = mock.Mock()
        c.transactions_sync.side_effect = [
            _sync_resp(added=[_plaid_txn(transaction_id="dup1", account_id="acc_1")],
                       has_more=False, next_cursor="cur1")
        ]
        return c

    with mock.patch("src.adapters.plaid_transactions.decrypt_token", return_value="tok"), \
         mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        sync_one_item(db, item, client=_fresh_client())
        db.commit()
        item.cursor = None  # reset to original starting cursor
        db.commit()
        sync_one_item(db, item, client=_fresh_client())
        db.commit()
    assert db.query(Transaction).filter_by(source="plaid", source_id="dup1").count() == 1


def test_raw_data_json_safe_with_native_date_objects():
    """Phase-5: Plaid to_dict() returns native datetime.date values; raw_data must
    be coerced JSON-safe or the INSERT fails ('Object of type date is not JSON
    serializable'), which silently dropped all 60 first-sync transactions."""
    import json as _json
    from datetime import date as _date

    txn = _plaid_txn()
    txn.to_dict = lambda: {
        "transaction_id": "t1",
        "date": _date(2026, 3, 18),
        "authorized_date": _date(2026, 3, 17),
        "amount": 24.27,
    }
    f = build_tx_fields(txn)
    _json.dumps(f["raw_data"])  # must not raise
    assert f["raw_data"]["date"] == "2026-03-18"
    assert f["raw_data"]["authorized_date"] == "2026-03-17"


# ---------------------------------------------------------------------------
# REQ-FIX-ING-007: split parent/child handling
# ---------------------------------------------------------------------------


def _make_split_parent_and_child(db, *, source_id="sp_source_1", parent_amount=Decimal("100.00")):
    """Create a parent (status=split_parent) + one child sharing source_id,
    mirroring src/classification/splitter.py's convention (child copies
    parent.source_id; source_hash suffixed to stay unique)."""
    parent = Transaction(
        source="plaid",
        source_id=source_id,
        source_hash=f"hash_{source_id}",
        date="2026-05-01",
        description="Split Parent Vendor",
        amount=parent_amount,
        currency="USD",
        entity="sparkry",
        status=TransactionStatus.SPLIT_PARENT.value,
        confidence=0.0,
        raw_data={},
    )
    db.add(parent)
    db.flush()
    child = Transaction(
        source="plaid",
        source_id=source_id,  # copied from parent — the ING-007 root bug
        source_hash=f"hash_{source_id}__split_0",
        date="2026-05-01",
        description="Room charge",
        amount=parent_amount,
        currency="USD",
        entity="sparkry",
        status=TransactionStatus.NEEDS_REVIEW.value,
        confidence=0.0,
        parent_id=parent.id,
        raw_data={},
    )
    db.add(child)
    db.commit()
    return parent, child


def test_existing_by_source_id_excludes_children_returns_parent(db):
    """A split child shares source_id with its parent — the lookup used by
    every Plaid mutation path must always resolve to the PARENT, never the
    child (which would otherwise be silently mutated)."""
    import src.adapters.plaid_transactions as pt_mod

    parent, child = _make_split_parent_and_child(db, source_id="lookup1")
    found = pt_mod._existing_by_source_id(db, "lookup1")
    assert found is not None
    assert found.id == parent.id
    assert found.id != child.id


def test_modified_on_split_parent_flags_parent_and_children(db):
    parent, child = _make_split_parent_and_child(db, source_id="mod_sp1")
    original_amount = parent.amount

    count = process_modified(
        db, [_plaid_txn(transaction_id="mod_sp1", amount=999.0)]
    )
    db.refresh(parent)
    db.refresh(child)

    assert count == 0  # a flag is not counted as a modify
    assert parent.amount == original_amount  # no amount mutation
    assert parent.status == TransactionStatus.SPLIT_PARENT.value
    assert parent.review_reason is not None
    assert "re-verify split" in parent.review_reason

    assert child.status == TransactionStatus.NEEDS_REVIEW.value
    assert child.review_reason == parent.review_reason

    # Audit rows exist for the review_reason change.
    events = db.query(AuditEvent).filter_by(transaction_id=parent.id).all()
    assert any(e.field_changed == "review_reason" for e in events)


def test_modified_on_split_parent_does_not_reactivate_rejected_child(db):
    """A human-rejected child stays rejected — a stale-split re-verify flag
    must not resurrect a row the human explicitly threw out."""
    parent, child = _make_split_parent_and_child(db, source_id="mod_sp2")
    child.status = TransactionStatus.REJECTED.value
    db.commit()

    process_modified(db, [_plaid_txn(transaction_id="mod_sp2", amount=42.0)])
    db.refresh(child)
    assert child.status == TransactionStatus.REJECTED.value


def test_removed_on_split_parent_flags_parent_and_children(db):
    parent, child = _make_split_parent_and_child(db, source_id="rem_sp1")

    count = process_removed(db, [{"transaction_id": "rem_sp1"}])
    db.refresh(parent)
    db.refresh(child)

    assert count == 0
    assert parent.status == TransactionStatus.SPLIT_PARENT.value  # never rejected
    assert parent.review_reason is not None
    assert "re-verify split" in parent.review_reason
    assert child.status == TransactionStatus.NEEDS_REVIEW.value
    assert child.review_reason == parent.review_reason


def test_pending_posted_prior_split_parent_flags_for_review(db):
    """A posted txn arriving for a split-then-pending parent must not mutate
    the parent — it gets flagged, and the posted txn itself is skipped
    (never inserted as a duplicate, never reconciled onto the parent)."""
    item, acct = _mapped(db)
    parent, child = _make_split_parent_and_child(db, source_id="pp_sp1")
    original_amount = parent.amount

    posted = _plaid_txn(
        transaction_id="pp_sp1_posted", amount=512.50, pending=False,
        pending_transaction_id="pp_sp1",
    )
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        counts = process_added(db, item, [posted], account_index={"acc_1": acct})

    db.refresh(parent)
    db.refresh(child)
    assert parent.amount == original_amount
    assert parent.status == TransactionStatus.SPLIT_PARENT.value
    assert parent.review_reason is not None
    assert "re-verify split" in parent.review_reason
    assert child.status == TransactionStatus.NEEDS_REVIEW.value
    assert counts.inserted == 0 and counts.reactivated == 0
    # Posted txn was never inserted.
    assert db.query(Transaction).filter_by(source_id="pp_sp1_posted").count() == 0


def test_pending_posted_prior_human_rejected_status_never_flips(db):
    """REQ-FIX-ING-007: a human-rejected prior pending row has its id/hash
    promoted and fields refreshed, but status must NEVER flip back to
    needs_review — even when the posted txn is transfer-category."""
    item, acct = _mapped(db)
    pending = _plaid_txn(transaction_id="hr1", amount=75.00, pending=True)
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        process_added(db, item, [pending], account_index={"acc_1": acct})
    prior = db.query(Transaction).filter_by(source_id="hr1").one()
    prior.status = TransactionStatus.REJECTED.value
    prior.review_reason = "human rejected this charge"
    db.commit()

    posted = _plaid_txn(
        transaction_id="hr1_posted", amount=75.00, pending=False,
        pending_transaction_id="hr1",
        personal_finance_category=SimpleNamespace(primary="TRANSFER_IN_DEPOSIT"),
    )
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        process_added(db, item, [posted], account_index={"acc_1": acct})

    db.refresh(prior)
    assert prior.status == TransactionStatus.REJECTED.value
    # id/hash promoted to prevent a future duplicate insert.
    assert prior.source_id == "hr1_posted"


def test_supersede_excludes_split_children(db):
    """First-sync supersede must never reject a bank_csv split CHILD — only
    top-level (parent_id IS NULL) rows are eligible."""
    parent = Transaction(
        source="bank_csv", source_id="csv_p1", source_hash="csv_hash_p1",
        date="2026-05-01", description="CSV Parent", amount=Decimal("100.00"),
        currency="USD", payment_method="Chase ****1234",
        status=TransactionStatus.SPLIT_PARENT.value, confidence=0.0, raw_data={},
    )
    db.add(parent)
    db.flush()
    child = Transaction(
        source="bank_csv", source_id="csv_p1", source_hash="csv_hash_p1__split_0",
        date="2026-05-01", description="CSV child", amount=Decimal("100.00"),
        currency="USD", payment_method="Chase ****1234",
        status=TransactionStatus.NEEDS_REVIEW.value, confidence=0.0,
        parent_id=parent.id, raw_data={},
    )
    db.add(child)
    db.commit()

    count = supersede_csv_rows(
        db, payment_method="Chase ****1234",
        covered_min="2026-04-01", covered_max="2026-06-01",
    )
    db.refresh(parent)
    db.refresh(child)
    assert count == 0
    assert parent.status == TransactionStatus.SPLIT_PARENT.value
    assert child.status == TransactionStatus.NEEDS_REVIEW.value  # untouched


# ---------------------------------------------------------------------------
# REQ-FIX-ING-008: make_transaction honors result.status / clears stale reasons
# ---------------------------------------------------------------------------


def test_make_transaction_vetoed_high_confidence_needs_review_with_veto_text(db):
    """A sign-veto (result.status=NEEDS_REVIEW) with confidence >= threshold
    must still land as needs_review — never auto_classified — and the veto
    text must survive in review_reason regardless of the confidence score."""
    vetoed = ClassificationResult(
        entity=Entity.SPARKRY, tax_category=TaxCategory.OTHER_EXPENSE,
        direction=Direction.EXPENSE, confidence=0.95, tier_used=1,
        reasoning="rule", status=TransactionStatus.NEEDS_REVIEW,
        review_reason="Sign/category mismatch: outflow classified as income.",
        deductible_pct=1.0,
    )
    txn = _plaid_txn(transaction_id="veto1")
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=vetoed):
        tx = make_transaction(txn, session=db, entity="sparkry", payment_method="Chase ****1234")

    assert tx.status == TransactionStatus.NEEDS_REVIEW.value
    assert tx.review_reason is not None
    assert "Sign/category mismatch" in tx.review_reason


def test_make_transaction_clean_auto_classified_has_no_stale_review_reason(db):
    """A clean auto_classified row must have review_reason=None — no stale
    mismatch/low-confidence text survives from the classifier's internal
    ClassificationResult.review_reason field."""
    clean = ClassificationResult(
        entity=Entity.SPARKRY, tax_category=TaxCategory.SUPPLIES,
        direction=Direction.EXPENSE, confidence=0.95, tier_used=1,
        reasoning="rule", status=TransactionStatus.AUTO_CLASSIFIED,
        review_reason=None, deductible_pct=1.0,
    )
    txn = _plaid_txn(transaction_id="clean1")
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=clean):
        tx = make_transaction(txn, session=db, entity="sparkry", payment_method="Chase ****1234")

    assert tx.status == TransactionStatus.AUTO_CLASSIFIED.value
    assert tx.review_reason is None


# ── REQ-WBR-LED-014: duplicate-Item account allowlist ────────────────────────


def test_mirror_txn_skipped_while_mapped_txn_in_same_batch_is_ingested(db):
    """REQ-WBR-LED-014: one batch, one owned account_id and one mirror.

    Reproduces the production shape — a second Chase Item returning all three
    accounts of the shared login. Only the owned account's txn becomes a row;
    the mirror is counted, not ingested.
    """
    item, acct = _mapped(db)
    batch = [
        _plaid_txn(transaction_id="own1", account_id="acc_1"),
        _plaid_txn(transaction_id="mir1", account_id="Z0p7Yzg0MqI1x0rBjgnjs8zZnk6ek8F88QaKg"),
        _plaid_txn(transaction_id="mir2", account_id="Z0p7Yzg0MqI1x0rBjgnjs8zZnk6ek8F88QaKg"),
    ]
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        counts = process_added(db, item, batch, account_index={"acc_1": acct})

    assert counts.inserted == 1
    assert counts.skipped_unknown_account == {
        "Z0p7Yzg0MqI1x0rBjgnjs8zZnk6ek8F88QaKg": 2
    }
    assert [r.source_id for r in db.query(Transaction).filter_by(source="plaid").all()] == [
        "own1"
    ]


def test_mirror_txn_does_not_promote_a_pending_row(db):
    """REQ-WBR-LED-014: the allowlist is checked before EVERY other branch.

    A mirror carrying pending_transaction_id must not hijack an existing
    pending row (promoting its source_id would silently rewrite a real row from
    a duplicate Item's payload).
    """
    item, acct = _mapped(db)
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        process_added(
            db, item,
            [_plaid_txn(transaction_id="pend1", account_id="acc_1", amount=10.0)],
            account_index={"acc_1": acct},
        )
        counts = process_added(
            db, item,
            [_plaid_txn(transaction_id="mirror_post", account_id=_A_KNOWN_MIRROR_ID,
                        amount=10.0, pending_transaction_id="pend1")],
            account_index={"acc_1": acct},
        )

    assert counts.inserted == 0
    assert counts.skipped_unknown_account == {_A_KNOWN_MIRROR_ID: 1}
    assert db.query(Transaction).filter_by(source="plaid").one().source_id == "pend1"


def test_sync_one_item_surfaces_skipped_unknown_account_counts(db):
    """REQ-WBR-LED-014: skips reach TxItemResult / TxBatchResult so the sync log
    line names the unrecognised account_id instead of silently ingesting it."""
    item, acct = _mapped(db)
    client = mock.Mock()
    client.transactions_sync.return_value = _sync_resp(
        added=[
            _plaid_txn(transaction_id="own1", account_id="acc_1"),
            _plaid_txn(transaction_id="mir1", account_id=_A_KNOWN_MIRROR_ID),
        ]
    )
    with mock.patch("src.adapters.plaid_transactions.decrypt_token", return_value="tok"), \
         mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        result = sync_one_item(db, item, client=client)

    assert result.status == "ok"
    assert result.failed == 0          # a KNOWN mirror skip is not a failure
    assert result.added == 1
    assert result.skipped_unknown_account == {_A_KNOWN_MIRROR_ID: 1}
    assert result.skipped_unknown_total == 1


def test_sync_one_item_holds_cursor_and_fails_on_unrecognized_account(db):
    """P1-002/P1-c4f: a genuinely unrecognized account_id (not a known
    mirror) holds the cursor and marks the item failed — the OnFailure alert
    path — rather than silently, permanently dropping the transaction."""
    item, acct = _mapped(db)
    client = mock.Mock()
    client.transactions_sync.return_value = _sync_resp(
        added=[
            _plaid_txn(transaction_id="own1", account_id="acc_1"),
            _plaid_txn(transaction_id="new1", account_id="acc_genuinely_new"),
        ],
        next_cursor="cursor_after_new_account",
    )
    with mock.patch("src.adapters.plaid_transactions.decrypt_token", return_value="tok"), \
         mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        result = sync_one_item(db, item, client=client)

    assert result.status == "error"
    assert result.failed == 1
    assert result.added == 1
    assert result.unrecognized_account_ids == {"acc_genuinely_new": 1}
    # Cursor held — the next run re-fetches this transaction rather than
    # permanently losing it.
    assert item.cursor is None
    log_row = db.query(IngestionLog).filter_by(source=f"plaid_tx:{item.institution_name}").one()
    assert "acc_genuinely_new" in (log_row.error_detail or "")


def test_sync_all_active_totals_skipped_unknown_account(db):
    """REQ-WBR-LED-014: the batch-level total is what the CLI log line prints."""
    item, acct = _mapped(db)
    client = mock.Mock()
    client.transactions_sync.return_value = _sync_resp(
        added=[_plaid_txn(transaction_id="mir1", account_id=_A_KNOWN_MIRROR_ID)]
    )
    with mock.patch("src.adapters.plaid_transactions.decrypt_token", return_value="tok"), \
         mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        batch = sync_all_active(db, client=client, dry_run=True)

    assert batch.total_added == 0
    assert batch.total_skipped_unknown_account == 1


# ── REQ-WBR-LED-015: credit-card payment legs -> direction=transfer ──────────


def _card_txn(**kw: Any) -> Any:
    """A card-side payment credit: Plaid amount negative = money into the card."""
    base = dict(transaction_id="pay1", account_id="acc_1", amount=-1637.65,
                date="2026-07-19", name="ONLINE PAYMENT - THANK YOU",
                merchant_name=None, transaction_code="payment")
    base.update(kw)
    return _plaid_txn(**base)


def test_card_side_transaction_code_payment_is_transfer(db):
    """REQ-WBR-LED-015: transaction_code="payment" on the card (credit_card)
    account -> transfer, classifier skipped."""
    with mock.patch("src.adapters.plaid_transactions.classify") as classify_mock:
        tx = make_transaction(_card_txn(), session=db, entity="personal",
                              payment_method="amex_31004", account_type="credit_card")

    classify_mock.assert_not_called()
    assert tx.direction == Direction.TRANSFER.value
    assert tx.tax_category is None
    assert tx.status == TransactionStatus.AUTO_CLASSIFIED.value
    # Sign passes straight through build_tx_fields — the card-side credit stays
    # positive, exactly as the +1637.65 row that triggered this fix.
    assert tx.amount == Decimal("1637.65")


def test_transaction_code_payment_on_checking_account_still_hits_classifier(db):
    """P1-d7e: transaction_code="payment" alone is a generic bank-channel
    taxonomy (ANY bill payment), not specific to a card payoff — on a
    checking (non-credit_card) account with no corroborating PFC signal it
    must still reach the classifier, exactly like the bare-"AUTOPAY"
    counterexample above. Unscoped, this rule would silently drop a genuine
    deductible utility/bill payment from P&L and B&O gross."""
    txn = _plaid_txn(transaction_id="billpay1", transaction_code="payment",
                     name="ONLINE PAYMENT - THANK YOU", merchant_name=None)
    with mock.patch("src.adapters.plaid_transactions.classify",
                    return_value=_cls()) as classify_mock:
        tx = make_transaction(txn, session=db, entity="sparkry",
                              payment_method="Chase ****1234",
                              account_type="checking")

    classify_mock.assert_called_once()
    assert tx.direction == Direction.EXPENSE.value


def test_card_side_pfc_detailed_is_transfer(db):
    """REQ-WBR-LED-015: LOAN_PAYMENTS_CREDIT_CARD_PAYMENT alone is enough."""
    pfc = SimpleNamespace(primary="LOAN_PAYMENTS",
                          detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
    txn = _card_txn(transaction_id="pay2", transaction_code=None,
                    personal_finance_category=pfc)
    with mock.patch("src.adapters.plaid_transactions.classify") as classify_mock:
        tx = make_transaction(txn, session=db, entity="personal",
                              payment_method="amex_31004")

    classify_mock.assert_not_called()
    assert tx.direction == Direction.TRANSFER.value


def test_checking_side_amex_ach_descriptor_is_transfer(db):
    """REQ-WBR-LED-015: the outbound leg carries no Plaid metadata — only the
    bank descriptor identifies it. Its negative amount is preserved."""
    txn = _plaid_txn(
        transaction_id="pay3", account_id="acc_1", amount=1637.65,
        date="2026-07-20", merchant_name=None,
        name="ORIG CO NAME:AMERICAN EXPRESS ORIG ID:9493560001 DESC DATE:250719 "
             "CO ENTRY DESCR:ACH PMT SEC:WEB",
    )
    with mock.patch("src.adapters.plaid_transactions.classify") as classify_mock:
        tx = make_transaction(txn, session=db, entity="personal",
                              payment_method="chase_6372")

    classify_mock.assert_not_called()
    assert tx.direction == Direction.TRANSFER.value
    assert tx.amount == Decimal("-1637.65")


def test_chase_credit_crd_autopay_descriptor_is_transfer(db):
    """REQ-WBR-LED-015: Chase's own card-payoff descriptor, matched case-insensitively."""
    txn = _plaid_txn(transaction_id="pay4", account_id="acc_1", amount=588.78,
                     merchant_name=None, name="Chase Credit Crd Autopay Pmt")
    with mock.patch("src.adapters.plaid_transactions.classify") as classify_mock:
        tx = make_transaction(txn, session=db, entity="personal",
                              payment_method="chase_6372")

    classify_mock.assert_not_called()
    assert tx.direction == Direction.TRANSFER.value


def test_non_payment_row_still_runs_the_classifier(db):
    """REQ-WBR-LED-015: an ordinary purchase is untouched by the new rule."""
    with mock.patch("src.adapters.plaid_transactions.classify",
                    return_value=_cls()) as classify_mock:
        tx = make_transaction(_plaid_txn(transaction_id="buy1"), session=db,
                              entity="sparkry", payment_method="Chase ****1234")

    classify_mock.assert_called_once()
    assert tx.direction == Direction.EXPENSE.value
    assert tx.tax_category == TaxCategory.MEALS.value


def test_merchant_autopay_is_not_a_card_payment(db):
    """REQ-WBR-LED-015: a bare "AUTOPAY" descriptor must NOT match — a merchant
    autopay is a real deductible expense, and matching it would drop it from
    P&L and B&O gross."""
    txn = _plaid_txn(transaction_id="vz1", merchant_name=None,
                     name="VERIZON WIRELESS AUTOPAY")
    with mock.patch("src.adapters.plaid_transactions.classify",
                    return_value=_cls()) as classify_mock:
        tx = make_transaction(txn, session=db, entity="sparkry",
                              payment_method="Chase ****1234")

    classify_mock.assert_called_once()
    assert tx.direction == Direction.EXPENSE.value


def test_card_payment_with_unmapped_entity_still_needs_review(db):
    """REQ-WBR-LED-015: the transfer short-circuit does not bypass the
    unmapped-entity guard."""
    with mock.patch("src.adapters.plaid_transactions.classify"):
        tx = make_transaction(_card_txn(transaction_id="pay5"), session=db,
                              entity=None, payment_method=None,
                              account_type="credit_card")

    assert tx.direction == Direction.TRANSFER.value
    assert tx.status == TransactionStatus.NEEDS_REVIEW.value


def test_card_payment_signal_shared_between_live_txn_and_stored_raw_data(db):
    """REQ-WBR-LED-015/017: the remediation script re-reads stored raw_data, so
    both entry points must agree on every signal. `account_type="credit_card"`
    is passed on both sides for the transaction_code-only case (P1-d7e) since
    the metadata alone (no pfc primary) needs the account-type corroboration."""
    for txn in (
        _card_txn(),
        _card_txn(transaction_code=None,
                  personal_finance_category={"primary": "LOAN_PAYMENTS",
                                             "detailed": "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"}),
        _plaid_txn(name="CHASE CREDIT CRD AUTOPAY PMT"),
    ):
        live = card_payment_signal_for_txn(txn, account_type="credit_card")
        stored = card_payment_signal_for_raw(
            build_tx_fields(txn)["raw_data"], account_type="credit_card"
        )
        assert live is not None
        assert live == stored

    ordinary = _plaid_txn(name="STARBUCKS #123")
    assert card_payment_signal_for_txn(ordinary) is None
    assert card_payment_signal_for_raw(build_tx_fields(ordinary)["raw_data"]) is None
