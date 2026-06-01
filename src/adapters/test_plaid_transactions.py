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
from src.models.enums import Direction, Entity, TaxCategory, TransactionStatus
from src.models.plaid import PlaidItem
from src.models.transaction import Transaction


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
    db.commit()
    process_modified(db, [_plaid_txn(transaction_id="m1", amount=11.5)])
    db.refresh(row)
    assert row.amount == Decimal("-11.50")
    assert row.tax_category == "OFFICE_EXPENSE"
    assert row.entity == "blackline"
    assert row.status == "confirmed"


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
