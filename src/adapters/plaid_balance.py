"""Plaid Balance daily sync — REQ-026 / REQ-PC-B1/B2.

For every active PlaidItem (BOTH scopes — register and wealth):
  1. Decrypt the access_token.
  2. Call /accounts/get (with retry on RATE_LIMIT etc). REQ-FIX-PLD-001:
     switched from /accounts/balance/get (paid Balance product, lapsed
     2026-06-25) to /accounts/get, which returns cached balances refreshed by
     Plaid's regular Transactions syncs and needs no extra product
     entitlement. Response shape and snapshot write path are unchanged.
  3. For each Plaid account in the response:
      - Collect a wealth-D1 payload row (``fresh_balances``) for every USD
        account with a non-null current balance — REQ-PC-B2: after the sync,
        ``push_fresh_balances`` POSTs these to the wealth Worker's
        ``ingest/plaid-balance`` endpoint (the D1 wants register-scope
        accounts' balances too, as it received them pre-consolidation).
      - REGISTER scope only:
          * If mapped to a local Account → INSERT a row in
            plaid_account_balance_snapshot (UNIQUE(account_id, snapshot_date)
            makes double-runs idempotent).
          * If unmapped → upsert an ExpectedAccount row with
            status='unconfirmed' so it surfaces in the missing-accounts panel.
      - WEALTH scope (REQ-PC-B1): NO local Account mapping, NO snapshot rows,
        NO expected_account writes — the payload row is the only output.
      - Non-USD → skip with warning (never collected, never written).
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
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.adapters._shared.ingestion import write_ingestion_log
from src.adapters._shared.wealth_client import WealthClientError, post_to_wealth
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

# ── REQ-PC-B2: wealth-D1 push wiring ─────────────────────────────────────────
#: Ingest slug — POSTs land at WEALTH_API_BASE/wealth/api/internal/ingest/plaid-balance.
WEALTH_BALANCE_INGEST_SOURCE = "plaid-balance"
#: The A1 endpoint caps a batch at 200 snapshot rows; the box chunks to match.
WEALTH_BALANCE_BATCH_CAP = 200
#: Local IngestionLog source for push runs (delivery-health surface, mirrors
#: the ``wealth_cloud:*`` convention from north_american_iul).
_CLOUD_LOG_SOURCE = "wealth_cloud:plaid_balance"


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# ── Per-account / per-Item result shapes ─────────────────────────────────────


@dataclass
class ItemSyncResult:
    item_id: str
    institution_name: str
    status: str  # 'ok' | 'error' | 'institution_down'
    scope: str = "register"
    accounts_processed: int = 0
    accounts_failed: int = 0
    accounts_skipped_unmapped: int = 0
    accounts_skipped_non_usd: int = 0
    error_code: str | None = None
    retryable: bool = False
    # REQ-FIX-PLD-005: "name ·mask· subtype" per truly-unmapped account (ignore-
    # listed accounts are excluded — they no longer count as unmapped).
    unmapped: list[str] = field(default_factory=list)
    #: REQ-PC-B2: A1-shaped payload rows (one per USD account with a non-null
    #: current balance, BOTH scopes) collected in memory for the post-sync D1
    #: push. Never persisted locally; survives per-row savepoint rollbacks by
    #: design (a same-day duplicate snapshot locally must still refresh D1).
    fresh_balances: list[dict[str, Any]] = field(default_factory=list)


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


def _epoch_ms(dt: datetime) -> int:
    """Naive-UTC datetime → epoch milliseconds (D1 ``fetched_at`` ordering key)."""
    return int(dt.replace(tzinfo=UTC).timestamp() * 1000)


def _money_str(value: Any) -> str:
    """Decimal-at-the-boundary → 2dp string, ROUND_HALF_UP.

    Mirrors the wealth writer's ``toFixed(2)`` convention (and the D1 read
    side's deliberate ROUND_HALF_UP, per the wealth audit) so the box-pushed
    ``current_balance`` is byte-identical to what the retired Worker cron
    would have written.
    """
    return str(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _fresh_balance_row(plaid_account: Any, *, pulled_at: datetime) -> dict[str, Any] | None:
    """Build one A1 (``ingest/plaid-balance``) payload row, or None.

    Returns None when there is no usable current balance — same condition
    under which the local snapshot path counts a failure. The D1 endpoint
    resolves ``plaid_account_id`` → its own ``account.id``; rows it cannot
    resolve are per-row skipped there, so no local-mapping filter applies.
    """
    balances = plaid_account.balances
    current = getattr(balances, "current", None)
    if current is None:
        return None
    available = getattr(balances, "available", None)
    subtype = getattr(plaid_account, "subtype", None)
    return {
        "plaid_account_id": plaid_account.account_id,
        "snapshot_date": date.today().isoformat(),
        "plaid_account_type": str(plaid_account.type),
        "plaid_account_subtype": str(subtype) if subtype else None,
        "current_balance": _money_str(current),
        "available_balance": _money_str(available) if available is not None else None,
        "iso_currency_code": getattr(balances, "iso_currency_code", None),
        "fetched_at": _epoch_ms(pulled_at),
    }


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
        scope=getattr(item, "scope", "register") or "register",
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
        if result.scope != "register":
            # REQ-PC-B1: wealth-scope Items have no register mapping concept —
            # record the scope + collected-payload count instead.
            detail = (
                f"scope={result.scope}"
                f" fresh_balances={len(result.fresh_balances)}"
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

    # REQ-PC-B2: collect the wealth-D1 payload row for BOTH scopes, before any
    # register-mapping decisions — the D1 wants register accounts' balances
    # too, and its own account table decides what maps (unknown ids are
    # per-row skipped endpoint-side). Appended before the local INSERT so a
    # same-day UNIQUE collision (savepoint rollback) still refreshes D1.
    fresh_row = _fresh_balance_row(plaid_account, pulled_at=pulled_at)
    if fresh_row is not None:
        result.fresh_balances.append(fresh_row)

    # REQ-PC-B1: wealth-scope Items stop here — NO local Account mapping, NO
    # snapshot rows, NO expected_account writes. The payload row above is the
    # only output; a missing balance counts as a failure like the local path.
    if result.scope != "register":
        if fresh_row is None:
            result.accounts_failed += 1
        else:
            result.accounts_processed += 1
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


# ── REQ-PC-B2: post-sync push of fresh balances to the wealth D1 ─────────────


@dataclass
class PushItemResult:
    """Outcome of pushing one Item's fresh balances to the wealth Worker."""

    item_id: str
    institution_name: str
    rows: int = 0
    pushed: int = 0
    error: str | None = None


@dataclass
class PushResult:
    items: list[PushItemResult] = field(default_factory=list)

    @property
    def total_pushed(self) -> int:
        return sum(i.pushed for i in self.items)

    @property
    def failed(self) -> bool:
        return any(i.error is not None for i in self.items)


def _chunked(rows: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [rows[i : i + size] for i in range(0, len(rows), size)]


def push_fresh_balances(
    batch: BatchResult,
    *,
    session: Session | None = None,
    post: Any = post_to_wealth,
) -> PushResult:
    """POST every Item's ``fresh_balances`` to the wealth Worker (A1 endpoint).

    REQ-PC-B2: per-Item error isolation — one Item's failed push never blocks
    the rest. A failed push is recorded on the result (``failed`` property);
    the CLI exits non-zero on it, which IS the balance-staleness alert (it
    replaces the retired wealth-side cron's silent-failure mode).

    Batches are chunked to ``WEALTH_BALANCE_BATCH_CAP`` rows (the endpoint's
    cap). A chunk failure mid-Item records the error and stops that Item's
    remaining chunks (the endpoint's conditional upsert on ``fetched_at``
    makes re-pushing the whole Item on the next run safe).

    When ``session`` is provided, one local IngestionLog row summarizes the
    push run (source ``wealth_cloud:plaid_balance``) so delivery-health
    surfaces it like every other cloud adapter (REQ-FIX-WLT-007 convention).
    ``post`` is injectable for tests.
    """
    push = PushResult()
    for item_result in batch.items:
        pr = PushItemResult(
            item_id=item_result.item_id,
            institution_name=item_result.institution_name,
            rows=len(item_result.fresh_balances),
        )
        push.items.append(pr)
        if not item_result.fresh_balances:
            continue
        try:
            for chunk in _chunked(item_result.fresh_balances, WEALTH_BALANCE_BATCH_CAP):
                post({"snapshots": chunk}, WEALTH_BALANCE_INGEST_SOURCE)
                pr.pushed += len(chunk)
        except WealthClientError as exc:
            pr.error = f"{type(exc).__name__}: {exc}"
            logger.error(
                "plaid balance D1 push failed for %s: %s",
                pr.institution_name,
                type(exc).__name__,
            )

    if session is not None:
        errors = [
            f"{i.institution_name}: {i.error}" for i in push.items if i.error
        ]
        if errors and push.total_pushed == 0:
            status = IngestionStatus.FAILURE
        elif errors:
            status = IngestionStatus.PARTIAL_FAILURE
        else:
            status = IngestionStatus.SUCCESS
        write_ingestion_log(
            session,
            source=_CLOUD_LOG_SOURCE,
            records_processed=push.total_pushed,
            records_failed=len(errors),
            status=status,
            error_detail="\n".join(errors) or None,
        )
    return push
