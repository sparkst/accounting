"""Plaid Balance daily sync — REQ-026.

For every active PlaidItem:
  1. Decrypt the access_token.
  2. Call /accounts/get (with retry on RATE_LIMIT etc). REQ-FIX-PLD-001:
     switched from /accounts/balance/get (paid Balance product, lapsed
     2026-06-25) to /accounts/get, which returns cached balances refreshed by
     Plaid's regular Transactions syncs and needs no extra product
     entitlement. Response shape and snapshot write path are unchanged.
  3. For each Plaid account in the response:
      - If mapped to a local Account → INSERT a row in plaid_account_balance_snapshot
        (UNIQUE(account_id, snapshot_date) makes double-runs idempotent).
      - If unmapped → upsert an ExpectedAccount row with status='unconfirmed' so it
        surfaces in the missing-accounts panel.
      - Non-USD → skip with warning.
  4. Write one IngestionLog row per Item per run.

Three layers of error isolation:
  - per-row `session.begin_nested()` savepoint
  - per-account try/except
  - per-Item try/except

DRY-RUN default — programmatic callers must pass ``dry_run=False`` to write.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.adapters.plaid_client import (
    PlaidErrorBase,
    RetryablePlaidError,
    TerminalPlaidError,
    call_with_retry,
)
from src.models.brokerage import Account
from src.models.enums import IngestionStatus
from src.models.history import ExpectedAccount
from src.models.ingestion_log import IngestionLog
from src.models.plaid import PlaidAccountBalanceSnapshot, PlaidItem
from src.utils.plaid_crypto import InvalidCiphertextError, decrypt_token

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# ── Per-account / per-Item result shapes ─────────────────────────────────────


@dataclass
class ItemSyncResult:
    item_id: str
    institution_name: str
    status: str  # 'ok' | 'error' | 'institution_down'
    accounts_processed: int = 0
    accounts_failed: int = 0
    accounts_skipped_unmapped: int = 0
    accounts_skipped_non_usd: int = 0
    error_code: str | None = None
    retryable: bool = False
    # REQ-FIX-PLD-005: "name ·mask· subtype" per truly-unmapped account (ignore-
    # listed accounts are excluded — they no longer count as unmapped).
    unmapped: list[str] = field(default_factory=list)


@dataclass
class BatchResult:
    items: list[ItemSyncResult] = field(default_factory=list)
    dry_run: bool = True

    @property
    def total_processed(self) -> int:
        return sum(i.accounts_processed for i in self.items)

    @property
    def total_failed(self) -> int:
        return sum(i.accounts_failed for i in self.items)


# ── Per-Item sync ────────────────────────────────────────────────────────────


def _build_snapshot(
    *,
    account: Account,
    plaid_account: Any,
    pulled_at: datetime,
) -> PlaidAccountBalanceSnapshot | None:
    """Build a PlaidAccountBalanceSnapshot row from a Plaid account response.

    Returns None when the account has no usable balance (current is None) — caller
    treats this as a failure rather than writing a null balance.
    """
    balances = plaid_account.balances
    current = getattr(balances, "current", None)
    if current is None:
        return None

    available = getattr(balances, "available", None)
    # ``type`` and ``subtype`` come back as enum-ish objects from the SDK; calling
    # ``str(...)`` returns the underlying value (e.g. "depository").
    plaid_type = str(plaid_account.type)
    plaid_subtype = (
        str(plaid_account.subtype) if getattr(plaid_account, "subtype", None) else None
    )

    return PlaidAccountBalanceSnapshot(
        account_id=account.id,
        snapshot_date=date.today(),
        plaid_account_type=plaid_type,
        plaid_account_subtype=plaid_subtype,
        # Decimal at the JSON boundary, per CLAUDE.md.
        current_balance=Decimal(str(current)),
        available_balance=Decimal(str(available)) if available is not None else None,
        iso_currency_code=getattr(balances, "iso_currency_code", None),
        pulled_at=pulled_at,
        raw_data=plaid_account.to_dict(),
    )


def _plaid_account_name(plaid_account: Any) -> str:
    return (
        getattr(plaid_account, "name", None)
        or getattr(plaid_account, "official_name", None)
        or "Unknown"
    )


def _unmapped_label(plaid_account: Any) -> str:
    """REQ-FIX-PLD-005: "name ·mask· subtype" for the ingestion-log detail."""
    name = _plaid_account_name(plaid_account)
    mask = getattr(plaid_account, "mask", None) or "----"
    subtype = getattr(plaid_account, "subtype", None)
    subtype_str = str(subtype) if subtype else ""
    return f"{name} ·{mask}· {subtype_str}".rstrip()


def _existing_expected_account(
    session: Session, *, item: PlaidItem, plaid_account: Any
) -> ExpectedAccount | None:
    mask = getattr(plaid_account, "mask", None) or None
    name = _plaid_account_name(plaid_account)
    return (
        session.query(ExpectedAccount)
        .filter_by(institution=item.institution_name, account_name=name, last_4=mask)
        .first()
    )


def _upsert_unconfirmed_expected_account(
    session: Session,
    *,
    item: PlaidItem,
    plaid_account: Any,
    existing: ExpectedAccount | None,
) -> None:
    """Surface an unmapped Plaid account in the missing-accounts panel.

    Idempotent on the natural key (institution, account_name, last_4). If a row
    already exists we leave it alone — the user may have already triaged it
    (including flipping it to `ignored`, REQ-FIX-PLD-005).
    """
    if existing is not None:
        return
    mask = getattr(plaid_account, "mask", None) or None
    name = _plaid_account_name(plaid_account)
    session.add(
        ExpectedAccount(
            institution=item.institution_name,
            account_name=name,
            last_4=mask,
            status="unconfirmed",
            source="plaid",
            notes=f"Discovered via Plaid item={item.id[:8]} account_id={plaid_account.account_id}",
        )
    )


def sync_one_item(
    session: Session,
    item: PlaidItem,
    *,
    client: Any,
    pulled_at: datetime | None = None,
) -> ItemSyncResult:
    """Sync a single Plaid Item: write snapshot rows for mapped accounts.

    The caller owns the outer commit boundary — this function commits per-row
    via savepoints and writes the IngestionLog and PlaidItem status updates at
    the end. The caller MUST commit the session after invocation to persist
    the IngestionLog + Item updates.
    """
    pulled_at = pulled_at or _utcnow()
    result = ItemSyncResult(
        item_id=item.id,
        institution_name=item.institution_name,
        status="ok",
    )
    log_row = IngestionLog(
        source=f"plaid_balance:{item.institution_name}",
        status=IngestionStatus.SUCCESS.value,
        run_at=pulled_at,
    )
    session.add(log_row)

    try:
        try:
            access_token = decrypt_token(item.access_token_encrypted)
        except InvalidCiphertextError as exc:
            # Treat as terminal: the token can't be used until the user re-links
            # (or the operator rotates the key with the rotation script).
            raise TerminalPlaidError(
                "INVALID_ACCESS_TOKEN",
                message="Could not decrypt access_token (key rotated?)",
            ) from exc

        resp = call_with_retry(
            lambda: client.accounts_get(_accounts_request(access_token))
        )

        for plaid_account in resp.accounts:
            try:
                with session.begin_nested():
                    _process_plaid_account(
                        session,
                        item=item,
                        plaid_account=plaid_account,
                        result=result,
                        pulled_at=pulled_at,
                    )
            except IntegrityError:
                # UNIQUE(account_id, snapshot_date) — today already snapshotted.
                # Idempotent on double-run.
                result.accounts_processed += 1
            except Exception:
                result.accounts_failed += 1
                logger.exception(
                    "plaid per-account failure",
                    extra={
                        "plaid_item_id": item.id,
                        "plaid_account_id": getattr(plaid_account, "account_id", "?"),
                    },
                )

        # Item-level success bookkeeping.
        item.last_sync_at = pulled_at
        item.last_sync_status = "ok"
        item.last_error = None
        log_row.records_processed = result.accounts_processed
        log_row.records_failed = result.accounts_failed
        log_row.status = (
            IngestionStatus.PARTIAL_FAILURE.value
            if result.accounts_failed
            else IngestionStatus.SUCCESS.value
        )
        detail = (
            f"unmapped_skipped={result.accounts_skipped_unmapped}"
            f" non_usd_skipped={result.accounts_skipped_non_usd}"
        )
        if result.unmapped:
            # REQ-FIX-PLD-005: name+mask+subtype per unmapped account, so the
            # pulse/operator can see exactly which accounts need triage.
            detail += f" [{'; '.join(result.unmapped)}]"
        log_row.error_detail = detail

    except RetryablePlaidError as exc:
        # All retries exhausted — keep the Item active, mark transient error.
        item.last_sync_status = (
            "institution_down" if exc.error_code in ("INSTITUTION_DOWN", "INSTITUTION_NOT_RESPONDING") else "error"
        )
        item.last_error = exc.error_code
        item.last_sync_at = pulled_at
        log_row.status = IngestionStatus.FAILURE.value
        log_row.retryable = True
        log_row.error_detail = exc.error_code
        result.status = item.last_sync_status
        result.error_code = exc.error_code
        result.retryable = True
    except TerminalPlaidError as exc:
        # User must re-link. UI surfaces this via /api/plaid/items.
        item.last_sync_status = "error"
        item.last_error = exc.error_code
        item.last_sync_at = pulled_at
        log_row.status = IngestionStatus.FAILURE.value
        log_row.retryable = False
        log_row.error_detail = exc.error_code
        result.status = "error"
        result.error_code = exc.error_code
    except PlaidErrorBase as exc:  # pragma: no cover — defensive
        item.last_sync_status = "error"
        item.last_error = exc.error_code
        item.last_sync_at = pulled_at
        log_row.status = IngestionStatus.FAILURE.value
        log_row.error_detail = exc.error_code
        result.status = "error"
        result.error_code = exc.error_code
    except Exception as exc:
        # Final fail-safe: catch anything else so the outer batch never aborts.
        item.last_sync_status = "error"
        item.last_error = "UNEXPECTED"
        item.last_sync_at = pulled_at
        log_row.status = IngestionStatus.FAILURE.value
        log_row.error_detail = f"unexpected: {type(exc).__name__}"
        result.status = "error"
        result.error_code = "UNEXPECTED"
        logger.exception("plaid per-item unexpected failure", extra={"plaid_item_id": item.id})

    return result


def _process_plaid_account(
    session: Session,
    *,
    item: PlaidItem,
    plaid_account: Any,
    result: ItemSyncResult,
    pulled_at: datetime,
) -> None:
    """Inside the savepoint: classify, then insert snapshot OR upsert ExpectedAccount.

    Raises IntegrityError on UNIQUE collision (handled by caller). Any other
    raise propagates to the per-account except in ``sync_one_item``.
    """
    plaid_account_id = plaid_account.account_id

    # Currency check first — skip non-USD before any other work.
    iso = getattr(plaid_account.balances, "iso_currency_code", None)
    if iso is not None and iso != "USD":
        logger.warning(
            "plaid non-USD account skipped",
            extra={
                "plaid_item_id": item.id,
                "plaid_account_id": plaid_account_id,
                "iso_currency_code": iso,
            },
        )
        result.accounts_skipped_non_usd += 1
        return

    account = (
        session.query(Account)
        .filter_by(plaid_item_id=item.id, plaid_account_id=plaid_account_id)
        .first()
    )
    if account is None:
        # Unmapped — surface in missing-accounts panel, unless the user has
        # already ignore-listed this exact account (REQ-FIX-PLD-005).
        existing = _existing_expected_account(session, item=item, plaid_account=plaid_account)
        if existing is not None and existing.status == "ignored":
            return
        _upsert_unconfirmed_expected_account(
            session, item=item, plaid_account=plaid_account, existing=existing
        )
        result.accounts_skipped_unmapped += 1
        result.unmapped.append(_unmapped_label(plaid_account))
        return

    snap = _build_snapshot(
        account=account, plaid_account=plaid_account, pulled_at=pulled_at
    )
    if snap is None:
        # Missing current balance — count as a failure (we have nothing useful to write).
        result.accounts_failed += 1
        return
    session.add(snap)
    result.accounts_processed += 1


def _accounts_request(access_token: str) -> Any:
    """Construct the SDK's AccountsGetRequest (REQ-FIX-PLD-001).

    ``/accounts/get`` returns cached balances refreshed by Plaid's regular
    Transactions syncs — no paid Balance-product entitlement required (that
    product lapsed 2026-06-25, causing the balance-sync outage this fixes).
    Response shape (``resp.accounts[]`` of ``AccountBase``) is identical to
    ``/accounts/balance/get``; every field ``_build_snapshot`` consumes is
    unaffected. Isolated for testability — imported locally so unit tests that
    pass a mock client never need the real SDK request object.
    """
    from plaid.model.accounts_get_request import AccountsGetRequest

    return AccountsGetRequest(access_token=access_token)


# ── Batch driver ─────────────────────────────────────────────────────────────


def sync_all_active(
    session: Session,
    *,
    client: Any,
    dry_run: bool = True,
) -> BatchResult:
    """Sync every active PlaidItem. DRY-RUN default — must explicitly opt in to write.

    On dry_run=True, the function still runs the full sync flow but rolls back at
    the end instead of committing. Useful for cron-style verification without
    polluting the DB.
    """
    batch = BatchResult(dry_run=dry_run)
    # REQ-FIX-PLD-004: query parity with plaid_transactions.py's rotation —
    # dead abandoned-OAuth placeholder rows (item_id LIKE 'placeholder_%')
    # never enter the sync loop even if their status is still 'active'.
    items = (
        session.query(PlaidItem)
        .filter(PlaidItem.status == "active", ~PlaidItem.item_id.like("placeholder_%"))
        .all()
    )
    pulled_at = _utcnow()

    for item in items:
        result = sync_one_item(session, item, client=client, pulled_at=pulled_at)
        batch.items.append(result)

    if dry_run:
        session.rollback()
    else:
        session.commit()
    return batch
