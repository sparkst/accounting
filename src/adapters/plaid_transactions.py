"""Plaid Transactions sync — REQ-PT-001..016.

Mirrors src/adapters/plaid_balance.py: DRY-RUN default, sync_one_item /
sync_all_active, three layers of error isolation. Cursor-based
/transactions/sync handles added/modified/removed; pending→posted reconcile
keys off Plaid's pending_transaction_id. payment_method is the join key for
entity-stamp, CSV supersede, and CSV-skip (the register has no account FK).
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from src.classification.engine import classify
from src.models.brokerage import Account
from src.models.enums import TransactionStatus
from src.models.plaid import PlaidItem
from src.models.transaction import Transaction
from src.utils.dedup import compute_source_hash

logger = logging.getLogger(__name__)

SOURCE = "plaid"
_AUTO_THRESHOLD = 0.7


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


def _existing_by_source_id(session: Session, source_id: str) -> Transaction | None:
    return (
        session.query(Transaction)
        .filter(Transaction.source == SOURCE, Transaction.source_id == source_id)
        .first()
    )


def _apply_update(tx: Transaction, ptxn: Any) -> None:
    """Refresh volatile fields from a modified/posted Plaid txn. Preserves human
    classification (entity/tax_category/direction are NOT touched here)."""
    fields = build_tx_fields(ptxn)
    tx.amount = fields["amount"]
    tx.date = fields["date"]
    tx.description = fields["description"]
    tx.raw_data = fields["raw_data"]


def process_modified(session: Session, modified: list[Any]) -> int:
    """Refresh volatile fields on existing rows (amount/date/description/raw_data).
    Human classification on the row is preserved — _apply_update never touches
    entity/tax_category/direction/status."""
    updated = 0
    for ptxn in modified:
        row = _existing_by_source_id(session, ptxn.transaction_id)
        if row is None:
            continue
        _apply_update(row, ptxn)
        session.flush()
        updated += 1
    return updated


def process_removed(session: Session, removed: list[Any]) -> int:
    """Plaid removed a txn (e.g. a settled pending). Mark rejected, never delete
    (audit rule). No-op when already reconciled away or never seen."""
    count = 0
    for r in removed:
        rid = r["transaction_id"] if isinstance(r, dict) else r.transaction_id
        row = _existing_by_source_id(session, rid)
        if row is None:
            continue
        row.status = "rejected"
        row.review_reason = "plaid_removed"
        session.flush()
        count += 1
    return count


def _sync_request(access_token: str, cursor: str | None) -> Any:
    from plaid.model.transactions_sync_request import TransactionsSyncRequest
    if cursor:
        return TransactionsSyncRequest(access_token=access_token, cursor=cursor)
    return TransactionsSyncRequest(access_token=access_token)


def fetch_all_pages(
    client: Any, access_token: str, *, cursor: str | None
) -> tuple[list[Any], list[Any], list[Any], str]:
    """Loop /transactions/sync until has_more is False. Returns
    (added, modified, removed, next_cursor)."""
    from src.adapters.plaid_client import call_with_retry
    added: list[Any] = []
    modified: list[Any] = []
    removed: list[Any] = []
    while True:
        req = _sync_request(access_token, cursor)
        resp = call_with_retry(lambda r=req: client.transactions_sync(r))
        added += list(resp.added)
        modified += list(resp.modified)
        removed += list(resp.removed)
        cursor = resp.next_cursor
        if not resp.has_more:
            break
    return added, modified, removed, cursor


def process_added(
    session: Session, item: PlaidItem, added: list[Any], *, account_index: dict[str, Account]
) -> int:
    """Insert added txns; idempotent on (source, source_id). Returns inserted count.

    Pending→posted reconcile: if a posted txn carries pending_transaction_id that
    matches an existing row, we UPDATE that row in place (promoting source_id to
    the posted id) rather than inserting a duplicate.
    """
    inserted = 0
    for ptxn in added:
        if _existing_by_source_id(session, ptxn.transaction_id) is not None:
            continue
        pending_id = getattr(ptxn, "pending_transaction_id", None)
        if pending_id:
            prior = _existing_by_source_id(session, pending_id)
            if prior is not None:
                _apply_update(prior, ptxn)
                prior.source_id = ptxn.transaction_id
                prior.source_hash = compute_source_hash(SOURCE, ptxn.transaction_id)
                session.flush()
                continue
        acct = account_index.get(ptxn.account_id)
        entity = acct.entity if acct else None
        pm = acct.payment_method if acct else None
        tx = make_transaction(ptxn, session=session, entity=entity, payment_method=pm)
        session.add(tx)
        session.flush()
        inserted += 1
    return inserted
