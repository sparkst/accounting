"""Plaid Investments holdings sync — REQ-PC-B3 (Plaid consolidation).

For every active WEALTH-scope PlaidItem:
  1. Decrypt the access_token.
  2. Call ``/investments/holdings/get`` (with retry on RATE_LIMIT etc).
     ``INVALID_PRODUCT`` → the institution has no investments product for
     this Item (e.g. a pure-depository login) → per-Item skip-with-log,
     mirroring the retired wealth Worker's behavior. NOT a failure.
  3. Build the A2 payload (securities + holdings exactly as Plaid returned
     them, JSON-safe) and POST it to the wealth Worker's
     ``ingest/plaid-holdings`` endpoint. The endpoint mirrors the retired
     ``plaid-investments-sync.ts`` writer: ``plaid_security`` upsert
     idempotent on ``security_id``; ``plaid_investment_holding`` insert with
     UNIQUE(account_id, security_id, snapshot_date) same-day idempotency;
     unmapped/non-USD rows are per-row skipped + counted endpoint-side.
  4. Write one IngestionLog row per Item per run.

Register-scope Items are excluded: their holdings (none today — Chase/Amex
are depository/credit) have no wealth-D1 consumer, and the wealth D1's Tier-0
holdings precedence must only ever see the accounts it maps.

DRY-RUN default — fetches from Plaid and reports what would push, but never
POSTs and rolls back the local IngestionLog rows.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from src.adapters._shared.wealth_client import WealthClientError, post_to_wealth
from src.adapters.plaid_client import (
    PlaidErrorBase,
    RetryablePlaidError,
    TerminalPlaidError,
    call_with_retry,
)
from src.models.enums import IngestionStatus
from src.models.ingestion_log import IngestionLog
from src.models.plaid import PlaidItem
from src.utils.plaid_crypto import InvalidCiphertextError, decrypt_token

logger = logging.getLogger(__name__)

#: Ingest slug — POSTs land at WEALTH_API_BASE/wealth/api/internal/ingest/plaid-holdings.
WEALTH_HOLDINGS_INGEST_SOURCE = "plaid-holdings"

#: Plaid's "this Item has no investments product" error code. Per-Item
#: skip-with-log (the wealth Worker treated it the same) — NOT a failure.
INVALID_PRODUCT_ERROR_CODE = "INVALID_PRODUCT"


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _epoch_ms(dt: datetime) -> int:
    """Naive-UTC datetime → epoch milliseconds (D1 ``fetched_at`` ordering key)."""
    return int(dt.replace(tzinfo=UTC).timestamp() * 1000)


def _json_safe(obj: Any) -> Any:
    """Coerce a Plaid ``to_dict()`` payload into a JSON-serializable structure.

    Same rationale as ``plaid_transactions._json_safe``: the SDK keeps
    ``date``/``datetime`` values as native objects, which neither the JSON
    POST body nor the audit trail can encode directly.
    """
    return json.loads(json.dumps(obj, default=str))


# ── Result shapes ────────────────────────────────────────────────────────────


@dataclass
class InvestmentsItemResult:
    item_id: str
    institution_name: str
    status: str = "ok"  # 'ok' | 'skipped_invalid_product' | 'error' | 'institution_down'
    securities: int = 0
    holdings: int = 0
    pushed: bool = False
    error_code: str | None = None
    retryable: bool = False


@dataclass
class InvestmentsBatchResult:
    items: list[InvestmentsItemResult] = field(default_factory=list)
    dry_run: bool = True

    @property
    def total_holdings(self) -> int:
        return sum(i.holdings for i in self.items)

    @property
    def total_failed_items(self) -> int:
        return sum(1 for i in self.items if i.status not in ("ok", "skipped_invalid_product"))


# ── Plaid request / payload construction ─────────────────────────────────────


def _holdings_request(access_token: str) -> Any:
    """Construct the SDK's InvestmentsHoldingsGetRequest.

    Isolated (with a deferred import) for testability, mirroring
    ``plaid_balance._accounts_request``.
    """
    from plaid.model.investments_holdings_get_request import (
        InvestmentsHoldingsGetRequest,
    )

    return InvestmentsHoldingsGetRequest(access_token=access_token)


def build_holdings_payload(
    item: PlaidItem, resp: Any, *, pulled_at: datetime
) -> dict[str, Any]:
    """Build the A2 (``ingest/plaid-holdings``) POST body.

    Securities and holdings are the raw Plaid objects (JSON-safe), exactly as
    the retired wealth Worker consumed them — the endpoint derives
    ticker/name/type from the securities and resolves each holding's
    ``account_id`` (a PLAID account id) against its own account table.
    """
    return {
        "item_id": item.item_id,
        "institution_name": item.institution_name,
        "pulled_at": pulled_at.replace(tzinfo=UTC).isoformat(),
        "fetched_at": _epoch_ms(pulled_at),
        "securities": [_json_safe(s.to_dict()) for s in resp.securities],
        "holdings": [_json_safe(h.to_dict()) for h in resp.holdings],
    }


# ── Per-Item sync ────────────────────────────────────────────────────────────


def sync_one_item(
    session: Session,
    item: PlaidItem,
    *,
    client: Any,
    dry_run: bool = True,
    pulled_at: datetime | None = None,
    post: Any = post_to_wealth,
) -> InvestmentsItemResult:
    """Fetch one Item's holdings and (unless dry-run) push them to the D1.

    Caller owns the outer commit boundary for the IngestionLog row.
    ``post`` is injectable for tests.
    """
    pulled_at = pulled_at or _utcnow()
    result = InvestmentsItemResult(
        item_id=item.id, institution_name=item.institution_name
    )
    log_row = IngestionLog(
        source=f"plaid_investments:{item.institution_name}",
        status=IngestionStatus.SUCCESS.value,
        run_at=pulled_at,
    )
    session.add(log_row)

    try:
        try:
            access_token = decrypt_token(item.access_token_encrypted)
        except InvalidCiphertextError as exc:
            raise TerminalPlaidError(
                "INVALID_ACCESS_TOKEN",
                message="Could not decrypt access_token (key rotated?)",
            ) from exc

        resp = call_with_retry(
            lambda: client.investments_holdings_get(_holdings_request(access_token))
        )
        payload = build_holdings_payload(item, resp, pulled_at=pulled_at)
        result.securities = len(payload["securities"])
        result.holdings = len(payload["holdings"])

        if not dry_run:
            post(payload, WEALTH_HOLDINGS_INGEST_SOURCE)
            result.pushed = True

        log_row.records_processed = result.holdings
        log_row.records_failed = 0
        log_row.error_detail = (
            f"securities={result.securities} holdings={result.holdings}"
            f" pushed={result.pushed}"
        )

    except WealthClientError as exc:
        # The Plaid fetch succeeded but the D1 push failed — a real failure
        # (the D1 goes stale silently otherwise). Non-zero exit → OnFailure.
        result.status = "error"
        result.error_code = f"D1_PUSH:{type(exc).__name__}"
        log_row.status = IngestionStatus.FAILURE.value
        log_row.error_detail = result.error_code
        logger.error(
            "plaid investments D1 push failed for %s: %s",
            item.institution_name,
            type(exc).__name__,
        )
    except RetryablePlaidError as exc:
        result.status = (
            "institution_down"
            if exc.error_code in ("INSTITUTION_DOWN", "INSTITUTION_NOT_RESPONDING")
            else "error"
        )
        result.error_code = exc.error_code
        result.retryable = True
        log_row.status = IngestionStatus.FAILURE.value
        log_row.retryable = True
        log_row.error_detail = exc.error_code
    except (TerminalPlaidError, PlaidErrorBase) as exc:
        if exc.error_code == INVALID_PRODUCT_ERROR_CODE:
            # Expected for wealth Items without investment accounts —
            # skip-with-log, mirroring the retired wealth Worker.
            result.status = "skipped_invalid_product"
            result.error_code = exc.error_code
            log_row.status = IngestionStatus.SUCCESS.value
            log_row.error_detail = "skipped: INVALID_PRODUCT (no investments product)"
            logger.info(
                "plaid investments skipped %s: INVALID_PRODUCT",
                item.institution_name,
            )
        else:
            result.status = "error"
            result.error_code = exc.error_code
            log_row.status = IngestionStatus.FAILURE.value
            log_row.error_detail = exc.error_code
    except Exception as exc:
        result.status = "error"
        result.error_code = "UNEXPECTED"
        log_row.status = IngestionStatus.FAILURE.value
        log_row.error_detail = f"unexpected: {type(exc).__name__}"
        logger.exception(
            "plaid investments per-item unexpected failure",
            extra={"plaid_item_id": item.id},
        )

    return result


# ── Batch driver ─────────────────────────────────────────────────────────────


def sync_all_wealth(
    session: Session,
    *,
    client: Any,
    dry_run: bool = True,
    post: Any = post_to_wealth,
) -> InvestmentsBatchResult:
    """Sync holdings for every active WEALTH-scope PlaidItem. DRY-RUN default.

    Dry-run still calls Plaid (so operators can validate the fetch) but never
    POSTs to the D1 and rolls back the local IngestionLog rows.
    """
    batch = InvestmentsBatchResult(dry_run=dry_run)
    items = (
        session.query(PlaidItem)
        .filter(
            PlaidItem.status == "active",
            PlaidItem.scope == "wealth",
            ~PlaidItem.item_id.like("placeholder_%"),
        )
        .all()
    )
    pulled_at = _utcnow()
    for item in items:
        batch.items.append(
            sync_one_item(
                session,
                item,
                client=client,
                dry_run=dry_run,
                pulled_at=pulled_at,
                post=post,
            )
        )
    if dry_run:
        session.rollback()
    else:
        session.commit()
    return batch
