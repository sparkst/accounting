"""Tests for src/adapters/plaid_transactions.py — REQ-PT-008..010.

Uses an in-memory SQLite so make_transaction can call the classifier
(which queries VendorRule rows). Plaid sign convention: positive = money out.
"""

from __future__ import annotations

import unittest.mock as mock
from collections.abc import Generator
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from src.adapters.plaid_client import RetryablePlaidError, TerminalPlaidError
from src.adapters.plaid_transactions import (
    build_tx_fields,
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


def test_process_added_unmapped_account_creates_needs_review(db):
    """Account not in account_index -> entity None, status needs_review."""
    item, acct = _mapped(db)
    with mock.patch("src.adapters.plaid_transactions.classify", return_value=_cls()):
        process_added(db, item, [_plaid_txn(transaction_id="u1", account_id="acc_unmapped")],
                      account_index={})
    row = db.query(Transaction).filter_by(source_id="u1").one()
    assert row.entity is None
    assert row.status == TransactionStatus.NEEDS_REVIEW.value


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
