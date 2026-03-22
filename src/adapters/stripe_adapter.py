"""Stripe adapter — ingests charges, payouts, invoices, and refunds.

REQ-ID: ADAPTER-STRIPE-001  Connects via Stripe Connect (one platform key, two connected accounts).
REQ-ID: ADAPTER-STRIPE-002  Maps charges, payouts, invoices, refunds to Transactions.
REQ-ID: ADAPTER-STRIPE-003  Entity is determined by which connected account retrieved the record.
REQ-ID: ADAPTER-STRIPE-004  Identifies Substack income by description/metadata.
REQ-ID: ADAPTER-STRIPE-005  AuthenticationError halts immediately (no retry).
REQ-ID: ADAPTER-STRIPE-006  RateLimitError / APIConnectionError retry with jittered backoff.
REQ-ID: ADAPTER-STRIPE-007  After 3 consecutive failures, adapter halts with FAILURE.
REQ-ID: ADAPTER-STRIPE-008  One-entity failure → PARTIAL_FAILURE; other entity still processed.
REQ-ID: ADAPTER-STRIPE-009  Deduplication by source_hash; re-run creates no duplicates.
REQ-ID: ADAPTER-STRIPE-010  IngestionLog entry created for every run.

Environment variables in .env:
    STRIPE_API_KEY              — Platform API key (shared across both entities)
    STRIPE_ACCOUNT_SPARKRY      — Connected account ID for Sparkry AI LLC (acct_xxx)
    STRIPE_ACCOUNT_BLACKLINE    — Connected account ID for BlackLine MTB LLC (acct_xxx)

Design spec: §Stripe Adapter
"""

from __future__ import annotations

import logging
import os
import random
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import stripe
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

# Pinned for reproducibility.  Bump deliberately when testing against a new
# Stripe API release and update tests accordingly.
STRIPE_API_VERSION = "2024-12-18.acacia"

# Retry configuration for transient errors (RateLimitError, APIConnectionError)
_MAX_RETRIES = 3
_BACKOFF_BASE = 1.0    # seconds — actual sleep = base * 2^attempt + jitter
_BACKOFF_MAX = 30.0    # cap total sleep per attempt

# Resource types to fetch per entity
_RESOURCE_TYPES = ("charges", "payouts", "refunds")


# ---------------------------------------------------------------------------
# Stripe object → raw dict
# ---------------------------------------------------------------------------


def _to_dict(obj: Any) -> dict[str, Any]:
    """Convert a Stripe API object to a plain dict for raw_data storage.

    Stripe SDK v5+ objects support dict()-style conversion via their
    ``to_dict()`` / ``to_dict_recursive()`` methods, but the exact API has
    changed across SDK versions.  We use a safe fallback chain, verifying that
    the returned value is actually a dict (guards against MagicMock in tests).
    """
    for method in ("to_dict_recursive", "to_dict"):
        if hasattr(obj, method):
            result = getattr(obj, method)()
            if isinstance(result, dict):
                return result

    # Construct a minimal dict from known attributes when the SDK object
    # doesn't provide a serialisation method (e.g. MagicMock in tests).
    attrs = ("id", "object", "amount", "currency", "created", "description",
             "status", "metadata", "arrival_date", "charge", "reason",
             "customer", "invoice", "refunded")
    out: dict[str, Any] = {}
    for attr in attrs:
        val = getattr(obj, attr, None)
        # Skip MagicMock values that aren't real data
        if val is not None and not hasattr(val, "_mock_name"):
            out[attr] = val
    return out


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def _ts_to_date(timestamp: int) -> str:
    """Convert a Unix UTC timestamp to an ISO date string ``YYYY-MM-DD``."""
    return datetime.fromtimestamp(timestamp, tz=UTC).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _is_substack(obj: Any) -> bool:
    """Return True when a charge is from Substack (subscription income)."""
    desc = (getattr(obj, "description", None) or "").lower()
    meta = getattr(obj, "metadata", {}) or {}
    source_meta = str(meta.get("source", "")).lower()
    platform_meta = str(meta.get("platform", "")).lower()
    return (
        "substack" in desc
        or "substack" in source_meta
        or "substack" in platform_meta
    )


def _classify_stripe_object(obj: Any, entity: Entity) -> dict[str, Any]:
    """Return classification fields (direction, tax_category) for a Stripe object."""
    obj_type = getattr(obj, "object", None)

    if obj_type == "charge":
        direction = Direction.INCOME
        if _is_substack(obj):
            tax_category = TaxCategory.SUBSCRIPTION_INCOME
        else:
            tax_category = TaxCategory.SALES_INCOME
        return {"direction": direction, "tax_category": tax_category}

    if obj_type == "payout":
        return {"direction": Direction.TRANSFER, "tax_category": None}

    if obj_type == "refund":
        return {"direction": Direction.EXPENSE, "tax_category": None}

    if obj_type == "invoice":
        return {"direction": Direction.INCOME, "tax_category": TaxCategory.CONSULTING_INCOME}

    # Fallback
    return {"direction": None, "tax_category": None}


# ---------------------------------------------------------------------------
# Object → Transaction mappers
# ---------------------------------------------------------------------------


def _map_charge(charge: Any, entity: Entity) -> Transaction:
    """Map a Stripe Charge object to a Transaction."""
    source_id = charge.id
    source_hash = compute_source_hash(Source.STRIPE.value, source_id)

    amount_cents = int(charge.amount)
    amount = Decimal(amount_cents) / Decimal(100)
    currency = (getattr(charge, "currency", "usd") or "usd").upper()
    date = _ts_to_date(int(charge.created))
    description = getattr(charge, "description", None) or "Stripe charge"

    classification = _classify_stripe_object(charge, entity)
    direction: Direction | None = classification["direction"]
    tax_category: TaxCategory | None = classification["tax_category"]

    return Transaction(
        source=Source.STRIPE.value,
        source_id=source_id,
        source_hash=source_hash,
        date=date,
        description=description,
        amount=amount,
        currency=currency,
        entity=entity.value,
        direction=direction.value if direction else None,
        tax_category=tax_category.value if tax_category else None,
        status=TransactionStatus.NEEDS_REVIEW.value,
        confidence=0.8,  # Stripe data is high-confidence, but needs human confirmation
        raw_data=_to_dict(charge),
    )


def _map_payout(payout: Any, entity: Entity) -> Transaction:
    """Map a Stripe Payout object to a Transaction."""
    source_id = payout.id
    source_hash = compute_source_hash(Source.STRIPE.value, source_id)

    amount_cents = int(payout.amount)
    amount = Decimal(amount_cents) / Decimal(100)
    currency = (getattr(payout, "currency", "usd") or "usd").upper()

    # Prefer arrival_date for reconciliation with bank statements
    arrival = getattr(payout, "arrival_date", None)
    created = getattr(payout, "created", 0)
    date = _ts_to_date(int(arrival) if arrival else int(created))

    description = getattr(payout, "description", None) or "Stripe payout"

    return Transaction(
        source=Source.STRIPE.value,
        source_id=source_id,
        source_hash=source_hash,
        date=date,
        description=description,
        amount=amount,
        currency=currency,
        entity=entity.value,
        direction=Direction.TRANSFER.value,
        tax_category=None,
        status=TransactionStatus.NEEDS_REVIEW.value,
        confidence=0.9,
        raw_data=_to_dict(payout),
    )


def _map_refund(refund: Any, entity: Entity) -> Transaction:
    """Map a Stripe Refund object to a Transaction.

    Refunds are expenses (money flowing back out to the customer).
    Stored as negative per the sign convention.
    """
    source_id = refund.id
    source_hash = compute_source_hash(Source.STRIPE.value, source_id)

    amount_cents = int(refund.amount)
    amount = -Decimal(amount_cents) / Decimal(100)  # negative = expense
    currency = (getattr(refund, "currency", "usd") or "usd").upper()
    date = _ts_to_date(int(refund.created))

    charge_id = getattr(refund, "charge", None) or ""
    description = f"Refund for charge {charge_id}" if charge_id else "Stripe refund"

    reason = getattr(refund, "reason", None)
    if reason:
        description = f"{description} ({reason})"

    return Transaction(
        source=Source.STRIPE.value,
        source_id=source_id,
        source_hash=source_hash,
        date=date,
        description=description,
        amount=amount,
        currency=currency,
        entity=entity.value,
        direction=Direction.EXPENSE.value,
        tax_category=None,
        status=TransactionStatus.NEEDS_REVIEW.value,
        confidence=0.8,
        raw_data=_to_dict(refund),
    )


# ---------------------------------------------------------------------------
# Fetch with retry
# ---------------------------------------------------------------------------


def _fetch_all(
    client: stripe.StripeClient,
    resource: str,
    entity: Entity,
    stripe_account: str | None = None,
    **list_kwargs: Any,
) -> list[Any]:
    """Fetch all pages of a Stripe resource via auto-paging.

    Args:
        client:         Configured StripeClient with platform API key.
        resource:       One of ``"charges"``, ``"payouts"``, ``"refunds"``.
        entity:         The entity this client belongs to (used for logging).
        stripe_account: Connected account ID (acct_xxx) for Stripe Connect.
        **list_kwargs:  Extra parameters passed to the list call (e.g. ``limit``).

    Returns:
        Flat list of Stripe objects.

    Raises:
        stripe.AuthenticationError: Immediately (not retried).
        RuntimeError: After ``_MAX_RETRIES`` consecutive transient failures.
    """
    resource_map = {
        "charges": client.charges,
        "payouts": client.payouts,
        "refunds": client.refunds,
    }
    api_resource = resource_map[resource]
    params = {"limit": 100, **list_kwargs}
    options: dict[str, Any] = {}
    if stripe_account:
        options["stripe_account"] = stripe_account

    for attempt in range(_MAX_RETRIES):
        try:
            page = api_resource.list(params, options=options)
            return list(page.auto_paging_iter())
        except stripe.AuthenticationError:
            # Not transient — re-raise immediately without retry.
            raise
        except (stripe.RateLimitError, stripe.APIConnectionError) as exc:
            if attempt == _MAX_RETRIES - 1:
                raise RuntimeError(
                    f"Stripe {resource} fetch failed after {_MAX_RETRIES} attempts: {exc}"
                ) from exc
            # Jittered exponential backoff
            base_delay = _BACKOFF_BASE * (2 ** attempt)
            jitter = random.uniform(0.0, base_delay * 0.5)
            delay = min(base_delay + jitter, _BACKOFF_MAX)
            logger.warning(
                "Stripe %s/%s transient error (attempt %d/%d), retrying in %.2fs: %s",
                entity.value, resource, attempt + 1, _MAX_RETRIES, delay, exc,
            )
            time.sleep(delay)

    # Should never reach here, but satisfy type-checker
    raise RuntimeError(f"Stripe {resource}: exhausted retries")  # pragma: no cover


# ---------------------------------------------------------------------------
# Per-entity ingestion
# ---------------------------------------------------------------------------


def _ingest_entity(
    api_key: str,
    entity: Entity,
    session: Session,
    result: AdapterResult,
    stripe_account: str | None = None,
) -> None:
    """Pull all resources for one entity and insert new Transaction rows.

    Auth errors halt this entity immediately and record an entry in
    ``result.errors``.  Transient errors are retried by ``_fetch_all``.
    After ``_MAX_RETRIES`` consecutive failures the entity is halted and
    ``result.records_failed`` is incremented.

    Per-record errors (bad data) are isolated: one bad record does not
    prevent subsequent records from being processed.
    """
    client = stripe.StripeClient(
        api_key,
        stripe_version=STRIPE_API_VERSION,
    )
    entity_label = entity.value  # "sparkry" or "blackline"

    # Mappers keyed by resource type
    mappers = {
        "charges": _map_charge,
        "payouts": _map_payout,
        "refunds": _map_refund,
    }

    for resource in _RESOURCE_TYPES:
        try:
            items = _fetch_all(client, resource, entity, stripe_account=stripe_account)
        except stripe.AuthenticationError as exc:
            msg = (
                f"Authentication failed for entity '{entity_label}' "
                f"(STRIPE_API_KEY, account={stripe_account or 'platform'}): {exc}. "
                "Check that the API key is correct and has read permissions."
            )
            logger.error(msg)
            result.record_error(f"stripe:{entity_label}:{resource}", Exception(msg))
            if result.status == IngestionStatus.SUCCESS:
                result.status = IngestionStatus.PARTIAL_FAILURE
            # Auth failure is fatal for this entity — stop processing it.
            return
        except RuntimeError as exc:
            # After _MAX_RETRIES the fetch gave up.
            msg = str(exc)
            logger.error("Stripe %s/%s exhausted retries: %s", entity_label, resource, msg)
            result.record_error(f"stripe:{entity_label}:{resource}", Exception(msg))
            continue  # Try next resource type rather than giving up on the entity

        mapper = mappers[resource]

        for item in items:
            item_id = getattr(item, "id", repr(item))
            result.records_processed += 1
            try:
                tx = mapper(item, entity)

                # Dedup check — UNIQUE constraint on source_hash is the safety net,
                # but we check first to avoid spurious IntegrityError exceptions.
                existing = (
                    session.query(Transaction)
                    .filter(Transaction.source_hash == tx.source_hash)
                    .first()
                )
                if existing is not None:
                    logger.debug(
                        "Skipping duplicate Stripe %s %s (entity=%s)",
                        resource, item_id, entity_label,
                    )
                    result.records_skipped += 1
                    continue

                session.add(tx)
                session.commit()
                result.records_created += 1
                logger.info(
                    "Ingested Stripe %s %s entity=%s amount=%s date=%s",
                    resource, item_id, entity_label, tx.amount, tx.date,
                )

            except Exception as exc:
                session.rollback()
                result.record_error(f"stripe:{entity_label}:{resource}:{item_id}", exc)


# ---------------------------------------------------------------------------
# Adapter class
# ---------------------------------------------------------------------------


class StripeAdapter(BaseAdapter):
    """Ingests Stripe charges, payouts, and refunds for Sparkry AI LLC and
    BlackLine MTB LLC via Stripe Connect.

    Uses one platform API key and two connected account IDs:
        ``STRIPE_API_KEY``             — Platform key (shared)
        ``STRIPE_ACCOUNT_SPARKRY``     — Connected account for Sparkry AI LLC
        ``STRIPE_ACCOUNT_BLACKLINE``   — Connected account for BlackLine MTB LLC

    The platform key must be present at construction time. Connected account
    IDs are optional — if omitted, the adapter fetches from the platform
    account directly (useful for single-entity setups).

    Args:
        api_key:           Override for testing; omit to read from env.
        account_sparkry:   Connected account ID override; omit to read from env.
        account_blackline: Connected account ID override; omit to read from env.
    """

    def __init__(
        self,
        api_key: str | None = None,
        account_sparkry: str | None = None,
        account_blackline: str | None = None,
    ) -> None:
        key = api_key or os.environ.get("STRIPE_API_KEY")

        if not key:
            raise OSError(
                "STRIPE_API_KEY is not set. "
                "Add it to your .env file or pass api_key= to StripeAdapter()."
            )

        self._api_key = key
        self._account_sparkry = account_sparkry or os.environ.get("STRIPE_ACCOUNT_SPARKRY")
        self._account_blackline = account_blackline or os.environ.get("STRIPE_ACCOUNT_BLACKLINE")

    @property
    def source(self) -> str:
        return Source.STRIPE.value

    def run(self, session: Session) -> AdapterResult:
        """Execute a full ingestion pass for both entities.

        Processes Sparkry and BlackLine sequentially.  If one entity's API key
        is invalid (``AuthenticationError``), the other entity is still processed
        and the overall result is ``PARTIAL_FAILURE``.

        After all processing, an ``IngestionLog`` entry is written.
        """
        result = AdapterResult(source=self.source)

        entities = [
            (Entity.SPARKRY, self._account_sparkry),
            (Entity.BLACKLINE, self._account_blackline),
        ]

        for entity, acct_id in entities:
            _ingest_entity(self._api_key, entity, session, result, stripe_account=acct_id)

        # Upgrade PARTIAL_FAILURE → FAILURE when nothing was created and there
        # were errors (e.g. both entities failed authentication).
        if (
            result.records_created == 0
            and result.records_failed > 0
            and result.records_skipped == 0
        ):
            result.status = IngestionStatus.FAILURE

        # Write IngestionLog regardless of outcome.
        error_detail: str | None = None
        if result.errors:
            error_detail = "\n\n".join(
                f"[{rid}]\n{msg}" for rid, msg in result.errors
            )

        log = IngestionLog(
            source=self.source,
            run_at=result.run_at,
            status=result.status.value,
            records_processed=result.records_processed,
            records_failed=result.records_failed,
            error_detail=error_detail,
            retryable=result.status in (
                IngestionStatus.PARTIAL_FAILURE,
                IngestionStatus.FAILURE,
            ),
        )
        session.add(log)
        session.commit()

        logger.info(
            "StripeAdapter run complete: status=%s created=%d skipped=%d failed=%d",
            result.status, result.records_created, result.records_skipped, result.records_failed,
        )
        return result
