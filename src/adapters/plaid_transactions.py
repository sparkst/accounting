"""Plaid Transactions sync — REQ-PT-001..016.

Mirrors src/adapters/plaid_balance.py: DRY-RUN default, sync_one_item /
sync_all_active, three layers of error isolation. Cursor-based
/transactions/sync handles added/modified/removed; pending→posted reconcile
keys off Plaid's pending_transaction_id. payment_method is the join key for
entity-stamp, CSV supersede, and CSV-skip (the register has no account FK).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from src.adapters.plaid_client import (
    PlaidErrorBase,
    RetryablePlaidError,
    TerminalPlaidError,
    call_with_retry,
)
from src.classification.engine import classify
from src.models.audit_event import AuditEvent
from src.models.brokerage import Account
from src.models.enums import ConfirmedBy, Direction, IngestionStatus, TransactionStatus
from src.models.ingestion_log import IngestionLog
from src.models.plaid import PlaidItem
from src.models.transaction import Transaction
from src.utils.dedup import compute_source_hash
from src.utils.plaid_crypto import InvalidCiphertextError, decrypt_token

logger = logging.getLogger(__name__)

SOURCE = "plaid"
_AUTO_THRESHOLD = 0.7


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _audit_field_change(
    session: Session,
    tx: Transaction,
    *,
    field: str,
    old_value: Any,
    new_value: Any,
) -> None:
    """Append a transaction-mode AuditEvent for an automated field change.

    Mirrors the canonical register-edit pattern in
    src/api/routes/transactions.py::_create_audit_events (transaction_id set;
    entity_id/entity_type NULL). changed_by is 'auto' because the change is
    performed by the Plaid sync job, not a human.
    """
    session.add(
        AuditEvent(
            transaction_id=tx.id,
            field_changed=field,
            old_value=str(old_value) if old_value is not None else None,
            new_value=str(new_value) if new_value is not None else None,
            changed_by=ConfirmedBy.AUTO.value,
        )
    )


def _audit_status_change(
    session: Session, tx: Transaction, old_status: str | None, new_status: str
) -> None:
    """Append a transaction-mode AuditEvent for an automated status flip."""
    _audit_field_change(
        session, tx, field="status", old_value=old_status, new_value=new_status
    )


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


def _is_internal_transfer(plaid_txn: Any) -> bool:
    """Detect an account-to-account transfer leg from Plaid metadata.

    A Plaid transaction is a transfer when its personal_finance_category.primary
    starts with "TRANSFER" OR transaction_code == "transfer". Handles object,
    dict, and None shapes defensively (the SDK returns objects; tests use
    SimpleNamespace/None). Transfers are non-P&L and must override whatever the
    classifier guesses (an inflow leg looks exactly like income otherwise).
    """
    pfc = getattr(plaid_txn, "personal_finance_category", None)
    primary = getattr(pfc, "primary", None) if pfc is not None else None
    if primary is None and isinstance(pfc, dict):
        primary = pfc.get("primary")
    if isinstance(primary, str) and primary.upper().startswith("TRANSFER"):
        return True
    code = getattr(plaid_txn, "transaction_code", None)
    return isinstance(code, str) and code.lower() == "transfer"


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
    # Entity was set at construction (entity=entity above); classify() returns a
    # result object and never mutates tx.entity, so no re-assignment is needed.
    # Internal-transfer detection overrides the classifier's direction so a
    # sweep between two linked accounts nets to zero off-P&L instead of being
    # double-counted as income (inflow leg) + expense (outflow leg).
    if _is_internal_transfer(plaid_txn):
        tx.direction = Direction.TRANSFER.value
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


def _apply_update(session: Session, tx: Transaction, ptxn: Any) -> None:
    """Refresh volatile fields from a modified/posted Plaid txn. Preserves human
    classification (entity/tax_category/direction are NOT touched here).

    A material amount change (pending→posted settlement or a Plaid `modified`
    delta — tips/holds can differ) is audited so the register keeps a field-level
    trail of the automated mutation."""
    fields = build_tx_fields(ptxn)
    old_amount = tx.amount
    new_amount = fields["amount"]
    if old_amount != new_amount:
        _audit_field_change(
            session, tx, field="amount", old_value=old_amount, new_value=new_amount
        )
    tx.amount = new_amount
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
        _apply_update(session, row, ptxn)
        session.flush()
        updated += 1
    return updated


def process_removed(session: Session, removed: list[Any]) -> int:
    """Plaid removed a txn (e.g. a settled pending). Mark rejected, never delete
    (audit rule). No-op when already reconciled away or never seen."""
    count = 0
    for r in removed:
        # Plaid SDK removed-entries are typed objects; dict support is retained
        # for the test fixtures (which pass {"transaction_id": ...}).
        rid = r["transaction_id"] if isinstance(r, dict) else r.transaction_id
        row = _existing_by_source_id(session, rid)
        if row is None:
            continue
        if row.status == TransactionStatus.SPLIT_PARENT.value:
            # Rejecting a split parent would orphan its children — skip + warn.
            logger.warning("plaid removed skipped: split_parent row %s", row.id)
            continue
        old_status = row.status
        row.status = TransactionStatus.REJECTED.value
        row.review_reason = "plaid_removed"
        _audit_status_change(session, row, old_status, TransactionStatus.REJECTED.value)
        session.flush()
        count += 1
    return count


def _sync_request(access_token: str, cursor: str | None) -> Any:
    # Deferred import: the plaid SDK model modules are imported lazily so the
    # adapter module can be imported in environments where the Plaid SDK is not
    # installed/initialized (mirrors the deferred-import pattern in routes/plaid.py).
    from plaid.model.transactions_sync_request import TransactionsSyncRequest
    if cursor:
        return TransactionsSyncRequest(access_token=access_token, cursor=cursor)
    return TransactionsSyncRequest(access_token=access_token)


def fetch_all_pages(
    client: Any, access_token: str, *, cursor: str | None
) -> tuple[list[Any], list[Any], list[Any], str]:
    """Loop /transactions/sync until has_more is False. Returns
    (added, modified, removed, next_cursor)."""
    added: list[Any] = []
    modified: list[Any] = []
    removed: list[Any] = []
    while True:
        req = _sync_request(access_token, cursor)

        def _do_sync(r: Any = req) -> Any:  # default-bind r to this loop's req
            return client.transactions_sync(r)

        resp = call_with_retry(_do_sync)
        added += list(resp.added)
        modified += list(resp.modified)
        removed += list(resp.removed)
        cursor = resp.next_cursor
        if not resp.has_more:
            break
    return added, modified, removed, cursor


def supersede_csv_rows(
    session: Session, *, payment_method: str | None, covered_min: str, covered_max: str
) -> int:
    """Mark bank-CSV rows for this payment_method label, within Plaid's covered
    date range, as rejected (superseded). Audit rule: never delete. A blank label
    disables supersede (returns 0, logged).

    Scope is intentionally narrowed to ``source == 'bank_csv'`` — the only source
    the documented "Plaid replaces CSV history" supersede is meant to replace.
    Gmail/Stripe/Shopify rows (and reconciliation-pair legs) that merely share a
    payment_method label are NOT collateral. Confirmed rows ARE superseded (that
    is the intended purpose: replacing confirmed CSV history with Plaid), but
    split_parent rows are excluded so rejecting a parent never orphans its split
    children. Mutations are wrapped in a savepoint so a mid-supersede failure
    doesn't leave a partial audit trail.
    """
    if not payment_method:
        logger.warning("plaid supersede skipped: account has no payment_method label")
        return 0
    rows = (
        session.query(Transaction)
        .filter(
            Transaction.source == "bank_csv",
            Transaction.payment_method == payment_method,
            Transaction.date >= covered_min,
            Transaction.date <= covered_max,
            Transaction.status != TransactionStatus.REJECTED.value,
            Transaction.status != TransactionStatus.SPLIT_PARENT.value,
        )
        .all()
    )
    with session.begin_nested():
        for row in rows:
            old_status = row.status
            row.status = TransactionStatus.REJECTED.value
            row.review_reason = "superseded_by_plaid"
            _audit_status_change(session, row, old_status, TransactionStatus.REJECTED.value)
        session.flush()
    return len(rows)


def process_added(
    session: Session, item: PlaidItem, added: list[Any], *, account_index: dict[str, Account]
) -> int:
    """Insert added txns; idempotent on (source, source_id). Returns inserted count.

    Pending→posted reconcile: if a posted txn carries pending_transaction_id that
    matches an existing row, we UPDATE that row in place (promoting source_id to
    the posted id) rather than inserting a duplicate.

    Accepts a list and loops internally, but the orchestrator (sync_one_item)
    passes single-element slices so each row gets its own begin_nested() savepoint
    (per-row isolation lives in the caller; this function is idempotent either
    way and tests exercise it with full lists directly).
    """
    inserted = 0
    for ptxn in added:
        existing = _existing_by_source_id(session, ptxn.transaction_id)
        if existing is not None:
            # Removed-then-readded: Plaid can re-deliver a previously-removed id.
            # A row we rejected via process_removed represents real activity that
            # Plaid now considers live again, so reactivate it instead of skipping
            # (which would strand it as rejected forever, dropping it from P&L).
            if (
                existing.status == TransactionStatus.REJECTED.value
                and existing.review_reason == "plaid_removed"
            ):
                old_status = existing.status
                _apply_update(session, existing, ptxn)
                existing.status = TransactionStatus.NEEDS_REVIEW.value
                existing.review_reason = "plaid_readded"
                _audit_status_change(
                    session, existing, old_status, TransactionStatus.NEEDS_REVIEW.value
                )
                session.flush()
            continue
        pending_id = getattr(ptxn, "pending_transaction_id", None)
        if pending_id:
            prior = _existing_by_source_id(session, pending_id)
            if prior is not None:
                _apply_update(session, prior, ptxn)
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


@dataclass
class TxItemResult:
    item_id: str
    institution_name: str
    status: str = "ok"          # 'ok' | 'error' | 'institution_down'
    added: int = 0
    modified: int = 0
    removed: int = 0
    failed: int = 0
    superseded: int = 0
    error_code: str | None = None


def sync_one_item(session: Session, item: PlaidItem, *, client: Any) -> TxItemResult:
    """Sync one Item's transactions. Caller owns the outer commit.

    First sync (item.cursor is None) triggers CSV supersede. Cursor advances
    ONLY after a clean page-loop, so a crash re-fetches from the last good
    cursor (idempotent via source_hash)."""
    result = TxItemResult(item_id=item.id, institution_name=item.institution_name)
    pulled_at = _utcnow()
    log_row = IngestionLog(source=f"plaid_tx:{item.institution_name}",
                           status=IngestionStatus.SUCCESS.value, run_at=pulled_at)
    session.add(log_row)
    first_sync = item.cursor is None

    accounts = session.query(Account).filter_by(plaid_item_id=item.id).all()
    account_index = {a.plaid_account_id: a for a in accounts if a.plaid_account_id}

    try:
        try:
            access_token = decrypt_token(item.access_token_encrypted)
        except InvalidCiphertextError as exc:
            raise TerminalPlaidError("INVALID_ACCESS_TOKEN",
                                     message="cannot decrypt token") from exc

        added, modified, removed, next_cursor = fetch_all_pages(
            client, access_token, cursor=item.cursor
        )

        for ptxn in added:
            try:
                with session.begin_nested():
                    result.added += process_added(session, item, [ptxn],
                                                   account_index=account_index)
            except Exception:
                result.failed += 1
                logger.exception("plaid tx added failure",
                                 extra={"plaid_item_id": item.id,
                                        "txn": getattr(ptxn, "transaction_id", "?")})
        for ptxn in modified:
            try:
                with session.begin_nested():
                    result.modified += process_modified(session, [ptxn])
            except Exception:
                result.failed += 1
        # Per-row savepoint isolation for removals (mirrors added/modified) so one
        # bad removal doesn't roll back the rest (REQ-PT-007).
        for r in removed:
            try:
                with session.begin_nested():
                    result.removed += process_removed(session, [r])
            except Exception:
                result.failed += 1

        # Supersede is keyed on the dates of THIS sync's added txns, grouped per
        # account. A first sync that returns only modified/removed (no added)
        # therefore performs no supersede — acceptable, since nothing new was
        # ingested to supersede the CSV history against. Each account uses only
        # its own added-txn date range so a multi-account Item can't widen one
        # account's coverage with another account's dates.
        if first_sync and added:
            dates_by_acct: dict[str, list[str]] = defaultdict(list)
            for t in added:
                dates_by_acct[t.account_id].append(str(t.date))
            for acct in accounts:
                if not acct.plaid_account_id:
                    continue
                acct_dates = dates_by_acct.get(acct.plaid_account_id, [])
                if not acct_dates:
                    continue
                result.superseded += supersede_csv_rows(
                    session, payment_method=acct.payment_method,
                    covered_min=min(acct_dates), covered_max=max(acct_dates),
                )

        # Advance the cursor ONLY on a fully clean page-loop. If any row failed,
        # leave item.cursor UNCHANGED so the next sync re-fetches from the last
        # good cursor and re-delivers the failed rows (re-fetch is idempotent via
        # source_hash, so already-succeeded rows are no-ops). Advancing past a
        # failed `added` row would permanently drop it (Plaid's /transactions/sync
        # is incremental and never re-delivers a passed cursor). REQ-PT-006.
        if result.failed == 0:
            item.cursor = next_cursor
            item.last_sync_status = "ok"
            item.last_error = None
            result.status = "ok"
        else:
            # Partial failure: surface it on the Item and hold the cursor.
            item.last_sync_status = "error"
            item.last_error = "PARTIAL_FAILURE"
            result.status = "error"
        item.last_sync_at = pulled_at
        log_row.records_processed = result.added + result.modified + result.removed
        log_row.records_failed = result.failed
        log_row.status = (IngestionStatus.PARTIAL_FAILURE.value if result.failed
                          else IngestionStatus.SUCCESS.value)

    except RetryablePlaidError as exc:
        item.last_sync_status = ("institution_down"
            if exc.error_code in ("INSTITUTION_DOWN", "INSTITUTION_NOT_RESPONDING") else "error")
        item.last_error = exc.error_code
        item.last_sync_at = pulled_at
        log_row.status = IngestionStatus.FAILURE.value
        log_row.retryable = True
        log_row.error_detail = exc.error_code
        result.status = item.last_sync_status
        result.error_code = exc.error_code
    except (TerminalPlaidError, PlaidErrorBase) as exc:
        item.last_sync_status = "error"
        item.last_error = exc.error_code
        item.last_sync_at = pulled_at
        log_row.status = IngestionStatus.FAILURE.value
        log_row.error_detail = exc.error_code
        result.status = "error"
        result.error_code = exc.error_code
    except Exception as exc:
        item.last_sync_status = "error"
        item.last_error = "UNEXPECTED"
        item.last_sync_at = pulled_at
        log_row.status = IngestionStatus.FAILURE.value
        log_row.error_detail = f"unexpected: {type(exc).__name__}"
        result.status = "error"
        result.error_code = "UNEXPECTED"
        logger.exception("plaid tx per-item failure", extra={"plaid_item_id": item.id})

    return result


@dataclass
class TxBatchResult:
    items: list[TxItemResult] = field(default_factory=list)
    dry_run: bool = True

    @property
    def total_added(self) -> int:
        return sum(i.added for i in self.items)

    @property
    def total_modified(self) -> int:
        return sum(i.modified for i in self.items)

    @property
    def total_removed(self) -> int:
        return sum(i.removed for i in self.items)

    @property
    def total_failed(self) -> int:
        return sum(i.failed for i in self.items)

    @property
    def total_superseded(self) -> int:
        return sum(i.superseded for i in self.items)


def sync_all_active(session: Session, *, client: Any, dry_run: bool = True) -> TxBatchResult:
    """Sync transactions for every active PlaidItem. DRY-RUN default."""
    batch = TxBatchResult(dry_run=dry_run)
    items = (
        session.query(PlaidItem)
        .filter(PlaidItem.status == "active", ~PlaidItem.item_id.like("placeholder_%"))
        .all()
    )
    for item in items:
        batch.items.append(sync_one_item(session, item, client=client))
    if dry_run:
        session.rollback()
    else:
        session.commit()
    return batch
