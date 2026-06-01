"""Plaid Transactions sync — REQ-PT-001..016.

Mirrors src/adapters/plaid_balance.py: DRY-RUN default, sync_one_item /
sync_all_active, three layers of error isolation. Cursor-based
/transactions/sync handles added/modified/removed; pending→posted reconcile
keys off Plaid's pending_transaction_id. payment_method is the join key for
entity-stamp, CSV supersede, and CSV-skip (the register has no account FK).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from src.classification.engine import classify
from src.models.enums import TransactionStatus
from src.models.transaction import Transaction
from src.utils.dedup import compute_source_hash

logger = logging.getLogger(__name__)

SOURCE = "plaid"
_AUTO_THRESHOLD = 0.7


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def build_tx_fields(plaid_txn: Any) -> dict[str, Any]:
    """Map a Plaid transaction object to register-Transaction field kwargs.

    Sign: Plaid depository convention is positive = money out. DB convention is
    expense negative / income positive, so db_amount = -plaid_amount.
    """
    txn_id = plaid_txn.transaction_id
    amount = Decimal(str(-plaid_txn.amount))
    description = getattr(plaid_txn, "merchant_name", None) or plaid_txn.name
    return {
        "source": SOURCE,
        "source_id": txn_id,
        "source_hash": compute_source_hash(SOURCE, txn_id),
        "date": str(plaid_txn.date),
        "description": description,
        "amount": amount,
        "currency": "USD",
        "raw_data": plaid_txn.to_dict(),
    }


def make_transaction(
    plaid_txn: Any, *, session: Session, entity: str | None, payment_method: str | None
) -> Transaction:
    """Build a classified Transaction. Entity is authoritative from the mapped
    account (overrides the classifier). Unmapped (entity None) -> needs_review."""
    fields = build_tx_fields(plaid_txn)
    tx = Transaction(
        **fields, entity=entity, payment_method=payment_method, confidence=0.0,
        status=TransactionStatus.NEEDS_REVIEW.value,
    )
    result = classify(tx, session)
    tx.tax_category = result.tax_category.value
    tx.tax_subcategory = result.tax_subcategory
    tx.direction = result.direction.value
    tx.deductible_pct = result.deductible_pct
    tx.confidence = result.confidence
    tx.review_reason = result.review_reason
    tx.entity = entity  # account entity is authoritative; classifier guess discarded
    needs_review = entity is None or result.confidence < _AUTO_THRESHOLD
    tx.status = (
        TransactionStatus.NEEDS_REVIEW.value if needs_review
        else TransactionStatus.AUTO_CLASSIFIED.value
    )
    if entity is None:
        tx.review_reason = "plaid: account not mapped to an entity"
    return tx
