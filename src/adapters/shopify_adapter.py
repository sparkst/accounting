"""Shopify adapter — ingests orders, refunds, and payouts for BlackLine MTB LLC.

REQ-ID: ADAPTER-SHOPIFY-001  Pulls orders from Shopify Admin REST API.
REQ-ID: ADAPTER-SHOPIFY-002  Pulls payouts from Shopify Payments API.
REQ-ID: ADAPTER-SHOPIFY-003  All transactions auto-tagged as BlackLine entity.
REQ-ID: ADAPTER-SHOPIFY-004  Deduplicates by source_hash — safe to re-run.
REQ-ID: ADAPTER-SHOPIFY-005  Auth failures (401/403) halt immediately.
REQ-ID: ADAPTER-SHOPIFY-006  Transient failures (429/5xx) retry with jittered backoff (3 attempts).
REQ-ID: ADAPTER-SHOPIFY-007  Minimum 500 ms delay between API calls.
REQ-ID: ADAPTER-SHOPIFY-008  Creates IngestionLog entry for every run.
REQ-ID: ADAPTER-SHOPIFY-009  Per-record error isolation — one bad record never halts a batch.
REQ-ID: ADAPTER-SHOPIFY-010  raw_data preserved verbatim from source.

Uses httpx directly against the Shopify Admin REST API — NOT the legacy
shopify-python SDK.

Authentication: Shopify Admin API access token via
``X-Shopify-Access-Token`` header.  Credentials are read from environment:
    SHOPIFY_API_KEY   — Admin API access token
    SHOPIFY_STORE_URL — e.g. ``blacklinemtb.myshopify.com``

Rate limiting:
    Shopify's leaky-bucket limit is 40 requests/second for the standard tier,
    but in practice you should stay well below that to avoid 429 responses.
    This adapter enforces a configurable minimum delay (default 500 ms) between
    every API call plus a jittered exponential back-off on 429/5xx responses.

Pagination:
    Uses cursor-based pagination via the ``Link`` header
    (``rel="next"`` link), falling back to ``page_info`` query params.

Data model:
    orders     → Transaction  (direction=income, tax_category=SALES_INCOME)
    refunds    → Transaction  (direction=expense, tax_category=SALES_INCOME,
                               amount negative)
    payouts    → Transaction  (direction=income, tax_category=SALES_INCOME)

Design spec: docs/superpowers/specs/2026-03-15-accounting-system-design.md §Shopify Adapter
"""

from __future__ import annotations

import logging
import os
import random
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from sqlalchemy.orm import Session

from src.adapters.base import AdapterResult, BaseAdapter
from src.models.enums import (
    Direction,
    Entity,
    IngestionStatus,
    Source,
    TaxCategory,
    TransactionStatus,
)
from src.models.ingestion_log import IngestionLog
from src.models.transaction import Transaction
from src.utils.dedup import compute_source_hash

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_MIN_DELAY_S = 0.5   # minimum seconds between API requests
_MAX_RETRIES = 3              # transient-error retry limit
_BACKOFF_BASE_S = 1.0         # base backoff for exponential jitter
_BACKOFF_MAX_S = 30.0         # cap on backoff duration
_PAGE_LIMIT = 250             # Shopify max page size
BATCH_SIZE = 100              # DB commit frequency — reduces per-record fsync overhead

# Link header pattern: <url>; rel="next"
_LINK_RE = re.compile(r'<([^>]+)>;\s*rel="next"')


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ShopifyAuthError(RuntimeError):
    """Raised when Shopify returns 401 or 403.

    These indicate a permanent credential problem — retrying is useless.
    """


class ShopifyAPIError(RuntimeError):
    """Raised when Shopify returns an unexpected non-2xx status after all retries."""


# ---------------------------------------------------------------------------
# Pure parsing helpers (public for testing)
# ---------------------------------------------------------------------------


def _iso_to_date(iso: str) -> str:
    """Truncate an ISO-8601 timestamp to YYYY-MM-DD.

    Handles offsets like ``2025-03-01T14:30:00-08:00`` or plain dates.
    """
    if len(iso) < 10:
        raise ValueError(f"Cannot parse date from {iso!r}")
    return iso[:10]


def _safe_decimal(value: str | int | float | None) -> Decimal:
    """Convert a Shopify money string or number to Decimal.

    Raises ``ValueError`` on failure (propagates to per-record isolation).
    """
    if value is None:
        raise ValueError("Amount is None")
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"Cannot convert {value!r} to Decimal") from exc


def _parse_order(order: dict[str, Any]) -> dict[str, Any]:
    """Convert a Shopify order object to a Transaction field dict.

    Args:
        order: Raw order dict from the Shopify Admin REST API.

    Returns:
        Dict of field names → values, ready to construct a ``Transaction``.
    """
    order_id = str(order["id"])
    source_id = f"order_{order_id}"
    source_hash = compute_source_hash(Source.SHOPIFY.value, source_id)

    amount = _safe_decimal(order.get("total_price"))
    date_raw = order.get("processed_at") or order.get("created_at", "")
    date = _iso_to_date(date_raw)

    order_name = order.get("name", f"#{order_id}")
    customer = order.get("customer") or {}
    if customer:
        first = customer.get("first_name", "")
        last = customer.get("last_name", "")
        customer_name = f"{first} {last}".strip()
    else:
        customer_name = ""

    description_parts = [f"Shopify Order {order_name}"]
    if customer_name:
        description_parts.append(f"— {customer_name}")
    description = " ".join(description_parts)

    currency = (order.get("currency") or "USD").upper()

    return {
        "source": Source.SHOPIFY.value,
        "source_id": source_id,
        "source_hash": source_hash,
        "date": date,
        "description": description,
        "amount": amount,
        "currency": currency,
        "entity": Entity.BLACKLINE.value,
        "direction": Direction.INCOME.value,
        "tax_category": TaxCategory.SALES_INCOME.value,
        "status": TransactionStatus.NEEDS_REVIEW.value,
        "confidence": 0.8,
        "raw_data": order,
    }


def _parse_refund(refund: dict[str, Any], order: dict[str, Any]) -> dict[str, Any]:
    """Convert a Shopify refund object to a Transaction field dict.

    Refunds are negative-amount expense transactions linked to the parent order.

    Args:
        refund: Raw refund dict nested inside an order from the Shopify API.
        order:  The parent order dict (used for description and currency context).

    Returns:
        Dict of field names → values, ready to construct a ``Transaction``.
    """
    refund_id = str(refund["id"])
    source_id = f"refund_{refund_id}"
    source_hash = compute_source_hash(Source.SHOPIFY.value, source_id)

    # Sum the transaction amounts in the refund
    total = Decimal("0")
    for txn in refund.get("transactions", []):
        if txn.get("kind") == "refund" and txn.get("status") == "success":
            total += _safe_decimal(txn.get("amount", "0"))

    amount = -abs(total)  # refunds are negative (money going out)

    date_raw = refund.get("created_at", order.get("created_at", ""))
    date = _iso_to_date(date_raw)

    order_name = order.get("name", f"#{order.get('id', 'unknown')}")
    description = f"Shopify Refund for {order_name}"

    currency = (order.get("currency") or "USD").upper()

    return {
        "source": Source.SHOPIFY.value,
        "source_id": source_id,
        "source_hash": source_hash,
        "date": date,
        "description": description,
        "amount": amount,
        "currency": currency,
        "entity": Entity.BLACKLINE.value,
        "direction": Direction.EXPENSE.value,
        "tax_category": TaxCategory.SALES_INCOME.value,
        "status": TransactionStatus.NEEDS_REVIEW.value,
        "confidence": 0.8,
        "raw_data": refund,
    }


def _parse_payout(payout: dict[str, Any]) -> dict[str, Any]:
    """Convert a Shopify Payments payout object to a Transaction field dict.

    Payouts represent net bank deposits from Shopify Payments.

    Args:
        payout: Raw payout dict from the Shopify Payments API.

    Returns:
        Dict of field names → values, ready to construct a ``Transaction``.
    """
    payout_id = str(payout["id"])
    source_id = f"payout_{payout_id}"
    source_hash = compute_source_hash(Source.SHOPIFY.value, source_id)

    amount = _safe_decimal(payout.get("amount"))
    date = payout.get("date", "")
    if not date or len(date) < 10:
        raise ValueError(f"Payout {payout_id} has invalid date: {date!r}")

    currency = (payout.get("currency") or "usd").upper()
    status = payout.get("status", "unknown")
    description = f"Shopify Payout #{payout_id} ({status})"

    return {
        "source": Source.SHOPIFY.value,
        "source_id": source_id,
        "source_hash": source_hash,
        "date": date[:10],
        "description": description,
        "amount": amount,
        "currency": currency,
        "entity": Entity.BLACKLINE.value,
        "direction": Direction.INCOME.value,
        "tax_category": TaxCategory.SALES_INCOME.value,
        "status": TransactionStatus.NEEDS_REVIEW.value,
        "confidence": 0.9,
        "raw_data": payout,
    }


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class ShopifyAdapter(BaseAdapter):
    """Ingests orders, refunds, and payouts from the Shopify Admin REST API.

    All transactions are auto-tagged as the BlackLine MTB LLC entity.

    Args:
        api_key:     Shopify Admin API access token.  Defaults to
                     ``SHOPIFY_API_KEY`` environment variable.
        store_url:   Shopify store hostname, e.g.
                     ``"blacklinemtb.myshopify.com"``.  Defaults to
                     ``SHOPIFY_STORE_URL`` environment variable.
        min_delay_s: Minimum seconds to sleep between API calls.
                     Default 0.5 s (500 ms) per REQ-SHOPIFY-007.
    """

    def __init__(
        self,
        api_key: str | None = None,
        store_url: str | None = None,
        min_delay_s: float = _DEFAULT_MIN_DELAY_S,
    ) -> None:
        self._api_key = api_key or os.environ.get("SHOPIFY_API_KEY", "")
        self._store_url = store_url or os.environ.get("SHOPIFY_STORE_URL", "")
        self._min_delay_s = min_delay_s

        if not self._api_key:
            raise ValueError("SHOPIFY_API_KEY must be set (env var or constructor arg)")
        if not self._store_url:
            raise ValueError("SHOPIFY_STORE_URL must be set (env var or constructor arg)")

        # Normalise store URL — strip scheme and trailing slashes
        url = self._store_url.strip().rstrip("/")
        if url.startswith("https://"):
            url = url[len("https://"):]
        elif url.startswith("http://"):
            url = url[len("http://"):]
        self._base_url = f"https://{url}"

    @property
    def source(self) -> str:
        return Source.SHOPIFY.value

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self, session: Session) -> AdapterResult:
        """Execute a full ingestion pass for orders, refunds, and payouts.

        Creates an ``IngestionLog`` row for every run regardless of outcome.
        Per-record errors are isolated — one bad record does not halt the batch.

        Args:
            session: An open SQLAlchemy ``Session``.

        Returns:
            :class:`AdapterResult` summarising the run.

        Raises:
            ShopifyAuthError: When Shopify returns 401 or 403 (credential problem).
        """
        result = AdapterResult(source=self.source)

        headers = {
            "X-Shopify-Access-Token": self._api_key,
            "Content-Type": "application/json",
        }

        # Shared pending counter — committed every BATCH_SIZE records
        pending: list[int] = [0]

        try:
            with httpx.Client(headers=headers, timeout=30.0) as client:
                self._ingest_orders(client, session, result, pending)
                self._ingest_payouts(client, session, result, pending)
        except ShopifyAuthError:
            result.status = IngestionStatus.FAILURE
            result.errors.append(("auth", "Authentication failed (401/403)"))
            self._write_ingestion_log(session, result)
            raise

        # Commit any remaining records in the last partial batch
        if pending[0] > 0:
            session.commit()

        self._write_ingestion_log(session, result)
        return result

    # ------------------------------------------------------------------
    # Orders ingestion
    # ------------------------------------------------------------------

    def _ingest_orders(
        self,
        client: httpx.Client,
        session: Session,
        result: AdapterResult,
        pending: list[int],
    ) -> None:
        """Fetch all orders (paginated) and insert new Transaction rows."""
        url = (
            f"{self._base_url}/admin/api/2024-01/orders.json"
            f"?status=any&limit={_PAGE_LIMIT}&order=created_at+asc"
        )

        while url:
            response = self._get_with_retry(client, url)
            url = self._next_page_url(response)

            orders = response.json().get("orders", [])
            logger.info("Shopify orders page: %d records", len(orders))

            for order in orders:
                order_id = str(order.get("id", "unknown"))
                try:
                    self._insert_if_new(_parse_order(order), session, result, pending)
                except Exception as exc:
                    result.record_error(f"order_{order_id}", exc)

                # Ingest any refunds nested inside this order
                for refund in order.get("refunds", []):
                    refund_id = str(refund.get("id", "unknown"))
                    try:
                        self._insert_if_new(
                            _parse_refund(refund, order), session, result, pending
                        )
                    except Exception as exc:
                        result.record_error(f"refund_{refund_id}", exc)

    # ------------------------------------------------------------------
    # Payouts ingestion
    # ------------------------------------------------------------------

    def _ingest_payouts(
        self,
        client: httpx.Client,
        session: Session,
        result: AdapterResult,
        pending: list[int],
    ) -> None:
        """Fetch all Shopify Payments payouts (paginated) and insert Transaction rows."""
        url = (
            f"{self._base_url}/admin/api/2024-01/shopify_payments/payouts.json"
            f"?limit={_PAGE_LIMIT}&order=date+asc"
        )

        while url:
            response = self._get_with_retry(client, url)
            url = self._next_page_url(response)

            payouts = response.json().get("payouts", [])
            logger.info("Shopify payouts page: %d records", len(payouts))

            for payout in payouts:
                payout_id = str(payout.get("id", "unknown"))
                try:
                    self._insert_if_new(_parse_payout(payout), session, result, pending)
                except Exception as exc:
                    result.record_error(f"payout_{payout_id}", exc)

    # ------------------------------------------------------------------
    # Dedup + insert
    # ------------------------------------------------------------------

    def _insert_if_new(
        self,
        fields: dict[str, Any],
        session: Session,
        result: AdapterResult,
        pending: list[int],
    ) -> None:
        """Insert a Transaction if source_hash is not already present.

        On dedup hit: increments ``records_skipped`` and ``records_processed``.
        On new record: stages the row within a savepoint (per-record isolation),
        increments counters, and commits every BATCH_SIZE rows.
        """
        source_hash = fields["source_hash"]
        existing = (
            session.query(Transaction)
            .filter(Transaction.source_hash == source_hash)
            .first()
        )
        if existing is not None:
            logger.debug("Skipping already-ingested %s", fields["source_id"])
            result.records_skipped += 1
            result.records_processed += 1
            return

        tx = Transaction(
            source=fields["source"],
            source_id=fields["source_id"],
            source_hash=fields["source_hash"],
            date=fields["date"],
            description=fields["description"],
            amount=fields["amount"],
            currency=fields["currency"],
            entity=fields["entity"],
            direction=fields["direction"],
            tax_category=fields["tax_category"],
            status=fields["status"],
            confidence=fields["confidence"],
            raw_data=fields["raw_data"],
        )

        # Savepoint ensures a flush failure rolls back only this record
        with session.begin_nested():
            session.add(tx)

        pending[0] += 1
        result.records_created += 1
        result.records_processed += 1
        logger.info(
            "Ingested %s  date=%s  amount=%s",
            fields["source_id"],
            fields["date"],
            fields["amount"],
        )

        if pending[0] >= BATCH_SIZE:
            session.commit()
            pending[0] = 0

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _get_with_retry(self, client: httpx.Client, url: str) -> httpx.Response:
        """Perform a GET request with rate-limit enforcement and retry logic.

        - Sleeps ``min_delay_s`` before each request to respect Shopify limits.
        - Raises ``ShopifyAuthError`` immediately on 401/403.
        - Retries up to ``_MAX_RETRIES`` times on 429/5xx with jittered back-off.

        Args:
            client: The active ``httpx.Client`` (carries auth headers).
            url:    Full request URL.

        Returns:
            Successful ``httpx.Response``.

        Raises:
            ShopifyAuthError: On 401 or 403.
            ShopifyAPIError:  When all retries are exhausted.
        """
        attempt = 0
        while True:
            # Enforce minimum delay between all API calls
            if self._min_delay_s > 0:
                time.sleep(self._min_delay_s)

            response = client.get(url)

            if response.status_code in (401, 403):
                logger.error(
                    "Shopify auth failure %d at %s", response.status_code, url
                )
                raise ShopifyAuthError(
                    f"Shopify returned {response.status_code} — check API credentials"
                )

            if response.status_code == 200:
                return response

            if response.status_code in (429, 500, 502, 503, 504):
                attempt += 1
                if attempt > _MAX_RETRIES:
                    raise ShopifyAPIError(
                        f"Shopify {response.status_code} after {_MAX_RETRIES} retries: {url}"
                    )

                # Honour Retry-After header for 429; otherwise exponential jitter
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    wait = float(retry_after)
                else:
                    wait = min(
                        _BACKOFF_BASE_S * (2 ** (attempt - 1)) + random.uniform(0, 1),
                        _BACKOFF_MAX_S,
                    )

                logger.warning(
                    "Shopify %d on attempt %d/%d — retrying in %.1f s: %s",
                    response.status_code,
                    attempt,
                    _MAX_RETRIES,
                    wait,
                    url,
                )
                time.sleep(wait)
                continue

            # Unexpected non-2xx status
            raise ShopifyAPIError(
                f"Shopify unexpected status {response.status_code}: {url}"
            )

    @staticmethod
    def _next_page_url(response: httpx.Response) -> str | None:
        """Extract the next-page URL from the ``Link`` response header, if present.

        Shopify uses RFC 5988 link headers for cursor pagination:
            ``<https://...?page_info=abc>; rel="next"``

        Returns:
            The URL string, or ``None`` when there is no next page.
        """
        link_header = response.headers.get("Link") or response.headers.get("link", "")
        if not link_header:
            return None
        m = _LINK_RE.search(link_header)
        return m.group(1) if m else None

    # ------------------------------------------------------------------
    # IngestionLog
    # ------------------------------------------------------------------

    @staticmethod
    def _write_ingestion_log(session: Session, result: AdapterResult) -> None:
        """Persist an IngestionLog row summarising this run."""
        error_detail: str | None = None
        if result.errors:
            lines = [f"{rid}: {msg}" for rid, msg in result.errors]
            error_detail = "\n\n".join(lines)

        log = IngestionLog(
            source=result.source,
            run_at=result.run_at,
            status=result.status.value,
            records_processed=result.records_processed,
            records_failed=result.records_failed,
            error_detail=error_detail,
            retryable=result.status == IngestionStatus.PARTIAL_FAILURE,
        )
        session.add(log)
        session.commit()
        logger.info(
            "IngestionLog written: source=%s status=%s processed=%d failed=%d",
            result.source,
            result.status,
            result.records_processed,
            result.records_failed,
        )
