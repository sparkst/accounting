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

#: Per-item skip set: Plaid signals "this Item has no investments product"
#: as INVALID_PRODUCT for some institutions and ADDITIONAL_CONSENT_REQUIRED
#: for others (observed live 2026-07-26 on Chase/PenFed/BofA/Citi).
INVESTMENTS_UNAVAILABLE_ERROR_CODES = frozenset(
    {INVALID_PRODUCT_ERROR_CODE, "ADDITIONAL_CONSENT_REQUIRED"}
)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _has_served_investments(session: Session, item: PlaidItem) -> bool:
    """True when this institution has ever logged a productive investments run.

    Discriminates an expiring consent (item previously delivered holdings —
    must page) from a structural "no investments product" login (never
    delivered — clean skip). Keyed by institution_name like every other
    ingestion_log source string.
    """
    return (
        session.query(IngestionLog)
        .filter(
            IngestionLog.source == f"plaid_investments:{item.institution_name}",
            IngestionLog.status == IngestionStatus.SUCCESS.value,
            IngestionLog.records_processed > 0,
        )
        .first()
        is not None
    )


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
    # P1-b2r/P1-002: counts parsed from the A2 response body — the endpoint
    # 200s a batch that resolved zero accounts, so `pushed=True` alone does
    # not mean anything landed in D1.
    holdings_written: int = 0
    holdings_skipped_unmapped: int = 0
    holdings_skipped_ambiguous: int = 0
    #: P1-r3c-2: informational ONLY — a non-USD holding is expected to be
    #: dropped endpoint-side and never counts as a push failure (mirrors the
    #: A1/balance decision).
    holdings_skipped_non_usd: int = 0
    holdings_failed_endpoint: int = 0


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


#: A2 rejects payloads with more than this many securities OR holdings (413).
A2_MAX_ROWS = 200


def chunk_holdings_payload(
    payload: dict[str, Any], *, max_rows: int = A2_MAX_ROWS
) -> list[dict[str, Any]]:
    """Split an A2 payload into <=``max_rows`` POSTs.

    Securities ship BEFORE any holdings so every holding's ``security_id``
    is already upserted on the D1 side by the time it arrives (securities
    persist across requests, so cross-chunk references are safe). A payload
    already within the caps is returned unchanged as a single element.
    """
    securities = payload["securities"]
    holdings = payload["holdings"]
    if len(securities) <= max_rows and len(holdings) <= max_rows:
        return [payload]
    envelope = {k: v for k, v in payload.items() if k not in ("securities", "holdings")}
    chunks: list[dict[str, Any]] = []
    for i in range(0, len(securities), max_rows):
        chunks.append(
            {**envelope, "securities": securities[i : i + max_rows], "holdings": []}
        )
    for i in range(0, len(holdings), max_rows):
        chunks.append(
            {**envelope, "securities": [], "holdings": holdings[i : i + max_rows]}
        )
    return chunks


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
            for chunk in chunk_holdings_payload(payload):
                resp = post(chunk, WEALTH_HOLDINGS_INGEST_SOURCE)
                # P1-b2r/P1-002: A2 never 400s a batch for unresolvable
                # holdings — it 200s with per-row skip/failure counts.
                # Without inspecting them, an Item whose plaid_account_ids
                # are all unmapped in D1 (e.g. a freshly re-linked Item —
                # P0-001) looks identical to a fully successful push.
                # Tolerate a response shape mismatch by defaulting to 0.
                if isinstance(resp, dict):
                    result.holdings_written += int(resp.get("holdings_processed", 0) or 0)
                    result.holdings_skipped_unmapped += int(
                        resp.get("holdings_skipped_unmapped", 0) or 0
                    )
                    # P2-002: holdings_skipped_ambiguous parsed so a batch with
                    # duplicate-plaid_account_id collisions is visible rather
                    # than silently absorbed into apparent success.
                    result.holdings_skipped_ambiguous += int(
                        resp.get("holdings_skipped_ambiguous", 0) or 0
                    )
                    result.holdings_skipped_non_usd += int(
                        resp.get("holdings_skipped_non_usd", 0) or 0
                    )
                    result.holdings_failed_endpoint += int(
                        resp.get("holdings_failed", 0) or 0
                    )
            result.pushed = True

        if result.holdings_failed_endpoint > 0:
            raise WealthClientError(
                f"D1 reported {result.holdings_failed_endpoint} failed holding row(s)"
            )
        if result.holdings_skipped_ambiguous > 0:
            # P2-002: ambiguous plaid_account_id is a data-integrity problem
            # regardless of how many other holdings in the batch landed.
            raise WealthClientError(
                f"D1 reported {result.holdings_skipped_ambiguous} ambiguous "
                "plaid_account_id holding row(s) (multiple accounts share a "
                "plaid_account_id)"
            )
        # P1-r3c-2: non-USD holdings are subtracted before the "nothing landed"
        # check — an all-non-USD batch is a legitimate no-op, not a failure.
        deliverable = result.holdings - result.holdings_skipped_non_usd
        if not dry_run and deliverable > 0 and result.holdings_written == 0:
            raise WealthClientError(
                f"D1 wrote 0 of {deliverable} deliverable holding row(s) "
                f"(skipped_unmapped={result.holdings_skipped_unmapped}, "
                f"skipped_non_usd={result.holdings_skipped_non_usd}) — "
                "likely unmapped plaid_account_id(s) in D1"
            )
        deliverable_holdings = result.holdings - result.holdings_skipped_non_usd
        if (
            not dry_run
            and deliverable_holdings > 0
            and result.holdings_skipped_unmapped == deliverable_holdings
        ):
            # Cutover reality check (first live run 2026-07-26): wealth Items
            # legitimately carry sub-account holdings D1 never mapped (E*TRADE
            # 3 of 8), matching the retired wealth Worker's skip-and-count
            # behavior — a PARTIAL unmapped batch is informational (counted in
            # the log line below). Only a WHOLLY unmapped item — every
            # deliverable holding skipped-unmapped — is the mapping-broke
            # signature that must page.
            raise WealthClientError(
                f"D1 skipped ALL {result.holdings_skipped_unmapped} deliverable "
                f"holding row(s) as unmapped ({result.holdings} pushed) — the "
                "item's account mapping in D1 is missing or broken"
            )

        log_row.records_processed = result.holdings
        log_row.records_failed = 0
        log_row.error_detail = (
            f"securities={result.securities} holdings={result.holdings}"
            f" pushed={result.pushed} written={result.holdings_written}"
            f" skipped_unmapped={result.holdings_skipped_unmapped}"
            f" skipped_non_usd={result.holdings_skipped_non_usd}"
        )

    except WealthClientError as exc:
        # The Plaid fetch succeeded but the D1 push failed — a real failure
        # (the D1 goes stale silently otherwise). Non-zero exit → OnFailure.
        # Includes the endpoint-reported-skip case raised above, whose
        # message carries the skip counts for diagnosability (P2-log/P1-002).
        result.status = "error"
        result.error_code = f"D1_PUSH:{type(exc).__name__}"
        log_row.status = IngestionStatus.FAILURE.value
        log_row.error_detail = f"{result.error_code}: {exc}"
        logger.error(
            "plaid investments D1 push failed for %s: %s",
            item.institution_name,
            exc,
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
        if exc.error_code in INVESTMENTS_UNAVAILABLE_ERROR_CODES and not (
            exc.error_code == "ADDITIONAL_CONSENT_REQUIRED"
            and _has_served_investments(session, item)
        ):
            # Expected for wealth Items without investment accounts — Plaid
            # answers INVALID_PRODUCT for some institutions and
            # ADDITIONAL_CONSENT_REQUIRED for others (first live run
            # 2026-07-26: Chase/PenFed/BofA/Citi). Skip-with-log, mirroring
            # the retired wealth Worker's "no investment accounts" skip.
            # EXCEPTION (audit 2026-07-27): ADDITIONAL_CONSENT_REQUIRED on an
            # item that has previously DELIVERED holdings is an expiring
            # consent (user-actionable, must page), not a structural absence —
            # history is the discriminator, so a Schwab/E*TRADE consent lapse
            # cannot hide behind the depository logins' clean skip.
            result.status = "skipped_invalid_product"
            result.error_code = exc.error_code
            log_row.status = IngestionStatus.SUCCESS.value
            log_row.error_detail = f"skipped: {exc.error_code} (no investments product)"
            logger.info(
                "plaid investments skipped %s: %s",
                item.institution_name,
                exc.error_code,
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
