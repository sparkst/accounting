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

from src.adapters.plaid_transactions import build_tx_fields, make_transaction
from src.classification.engine import ClassificationResult
from src.models.base import Base
from src.models.enums import Direction, Entity, TaxCategory, TransactionStatus


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
