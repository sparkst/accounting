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

Environment variables (via Doppler):
    STRIPE_API_KEY              — Platform API key (shared across both entities)
    STRIPE_ACCOUNT_SPARKRY      — Connected account ID for Sparkry LLC (acct_xxx)
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

# DB commit frequency — reduces per-record fsync to per-batch
BATCH_SIZE = 100

# Account modes — how to classify charges on a given connected account.
#
# STANDARD       — dedicated business account (Sparkry/BlackLine). Every charge
#                  is assumed legitimate business revenue and classified via
#                  description/metadata (Substack → SUBSCRIPTION_INCOME, else
#                  SALES_INCOME).
# PERSONAL_MIXED — personal account where only subscription/invoice charges are
#                  expected (e.g. Substack running on a personal Stripe). Any
#                  charge without an ``invoice`` field is flagged via
#                  ``review_reason`` so the user sees the anomaly instead of it
#                  silently landing in the wrong bucket.
ACCOUNT_MODE_STANDARD = "standard"
ACCOUNT_MODE_PERSONAL_MIXED = "personal_mixed"

# Review reason emitted when a charge on a PERSONAL_MIXED account is not tied
# to an invoice. Kept as a module constant so tests can assert on it.
UNEXPECTED_CHARGE_REVIEW_REASON = (
    "Non-invoice charge on personal Stripe account — expected only "
    "subscription or manual-invoice charges. Verify entity and category."
)


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


def _classify_stripe_object(
    obj: Any,
    entity: Entity,
    mode: str = ACCOUNT_MODE_STANDARD,
) -> dict[str, Any]:
    """Return classification fields for a Stripe object.

    Returns a dict with keys:
      - ``direction``: :class:`Direction` or ``None``
      - ``tax_category``: :class:`TaxCategory` or ``None``
      - ``review_reason``: ``str`` or ``None`` — populated when the charge
        should be flagged for human review (only relevant in
        ``PERSONAL_MIXED`` mode)

    In ``STANDARD`` mode, behavior is unchanged from the dedicated-account
    path. In ``PERSONAL_MIXED`` mode, charges lacking an ``invoice`` field are
    flagged as anomalies — they're still inserted (we never drop data) but the
    review queue will surface them.
    """
    obj_type = getattr(obj, "object", None)

    if obj_type == "charge":
        direction = Direction.INCOME

        if mode == ACCOUNT_MODE_PERSONAL_MIXED:
            invoice_id = getattr(obj, "invoice", None)
            if not invoice_id:
                return {
                    "direction": direction,
                    "tax_category": None,
                    "review_reason": UNEXPECTED_CHARGE_REVIEW_REASON,
                }
            desc = (getattr(obj, "description", None) or "").lower()
            if "subscription" in desc or _is_substack(obj):
                tax_category = TaxCategory.SUBSCRIPTION_INCOME
            else:
                tax_category = TaxCategory.CONSULTING_INCOME
            return {
                "direction": direction,
                "tax_category": tax_category,
                "review_reason": None,
            }

        # STANDARD mode (existing behavior)
        if _is_substack(obj):
            tax_category = TaxCategory.SUBSCRIPTION_INCOME
        else:
            tax_category = TaxCategory.SALES_INCOME
        return {
            "direction": direction,
            "tax_category": tax_category,
            "review_reason": None,
        }

    if obj_type == "payout":
        return {"direction": Direction.TRANSFER, "tax_category": None, "review_reason": None}

    if obj_type == "refund":
        return {"direction": Direction.EXPENSE, "tax_category": None, "review_reason": None}

    if obj_type == "invoice":
        return {
            "direction": Direction.INCOME,
            "tax_category": TaxCategory.CONSULTING_INCOME,
            "review_reason": None,
        }

    # Fallback
    return {"direction": None, "tax_category": None, "review_reason": None}


# ---------------------------------------------------------------------------
# Object → Transaction mappers
# ---------------------------------------------------------------------------


def _map_charge(
    charge: Any,
    entity: Entity,
    mode: str = ACCOUNT_MODE_STANDARD,
) -> Transaction:
    """Map a Stripe Charge object to a Transaction.

    When ``mode`` is ``ACCOUNT_MODE_PERSONAL_MIXED`` and the charge is not
    tied to an invoice, the resulting Transaction will carry a
    ``review_reason`` explaining the anomaly so it surfaces in the review
    queue for the user to classify manually.
    """
    source_id = charge.id
    source_hash = compute_source_hash(Source.STRIPE.value, source_id)

    amount_cents = int(charge.amount)
    amount = Decimal(amount_cents) / Decimal(100)
    currency = (getattr(charge, "currency", "usd") or "usd").upper()
    date = _ts_to_date(int(charge.created))
    description = getattr(charge, "description", None) or "Stripe charge"

    classification = _classify_stripe_object(charge, entity, mode)
    direction: Direction | None = classification["direction"]
    tax_category: TaxCategory | None = classification["tax_category"]
    review_reason: str | None = classification["review_reason"]

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
        review_reason=review_reason,
        raw_data=_to_dict(charge),
    )


def _extract_charge_fee_cents(charge: Any) -> int:
    """Return the processing fee in cents for a charge, or 0 if unavailable.

    Works whether ``balance_transaction`` is an expanded object (has ``.fee``),
    a plain dict, or unset/None (older ingests, unexpanded list responses, or
    pending charges without a balance transaction yet).
    """
    bt = getattr(charge, "balance_transaction", None)
    if bt is None:
        return 0
    if isinstance(bt, str):
        return 0  # unexpanded reference — no fee data available
    fee = getattr(bt, "fee", None)
    if fee is None and isinstance(bt, dict):
        fee = bt.get("fee")
    try:
        return int(fee) if fee else 0
    except (TypeError, ValueError):
        return 0


def _map_charge_fee(charge: Any, entity: Entity) -> Transaction | None:
    """Map the processing fee on a Stripe Charge to its own expense Transaction.

    Returns ``None`` when the charge has no balance_transaction or a zero fee
    (e.g. POS-only charges, pending charges, or unexpanded list responses).
    The returned row is a sibling to the charge row, linked via metadata.
    """
    fee_cents = _extract_charge_fee_cents(charge)
    if fee_cents <= 0:
        return None

    charge_id = charge.id
    fee_source_id = f"fee_{charge_id}"
    source_hash = compute_source_hash(Source.STRIPE.value, fee_source_id)

    amount = -Decimal(fee_cents) / Decimal(100)  # negative = expense
    currency = (getattr(charge, "currency", "usd") or "usd").upper()
    date = _ts_to_date(int(charge.created))
    description = f"Stripe processing fee — {charge_id}"

    raw = {"fee_for_charge": charge_id, "fee_cents": fee_cents}
    bt = getattr(charge, "balance_transaction", None)
    if bt is not None and not isinstance(bt, str):
        raw["balance_transaction"] = _to_dict(bt) if hasattr(bt, "id") or hasattr(bt, "to_dict") else bt

    return Transaction(
        source=Source.STRIPE.value,
        source_id=fee_source_id,
        source_hash=source_hash,
        date=date,
        description=description,
        amount=amount,
        currency=currency,
        entity=entity.value,
        direction=Direction.EXPENSE.value,
        tax_category=TaxCategory.LEGAL_AND_PROFESSIONAL.value,
        status=TransactionStatus.NEEDS_REVIEW.value,
        confidence=0.95,
        raw_data=raw,
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
    mode: str = ACCOUNT_MODE_STANDARD,
) -> None:
    """Pull all resources for one entity and insert new Transaction rows.

    Auth errors halt this entity immediately and record an entry in
    ``result.errors``.  Transient errors are retried by ``_fetch_all``.
    After ``_MAX_RETRIES`` consecutive failures the entity is halted and
    ``result.records_failed`` is incremented.

    Per-record errors (bad data) are isolated: one bad record does not
    prevent subsequent records from being processed.

    ``mode`` controls how charges are classified. See the module-level
    ``ACCOUNT_MODE_*`` constants. Payouts, refunds, and fees behave the
    same in all modes.
    """
    client = stripe.StripeClient(
        api_key,
        stripe_version=STRIPE_API_VERSION,
    )
    entity_label = entity.value  # "sparkry" or "blackline"

    # Mappers keyed by resource type. Charges takes the mode; others don't.
    def _charge_mapper(obj: Any, ent: Entity) -> Transaction:
        return _map_charge(obj, ent, mode)

    mappers = {
        "charges": _charge_mapper,
        "payouts": _map_payout,
        "refunds": _map_refund,
    }

    for resource in _RESOURCE_TYPES:
        # Expand balance_transaction on charges so we can capture the processing
        # fee as a sibling transaction. Other resource types do not need expand.
        fetch_kwargs: dict[str, Any] = {}
        if resource == "charges":
            fetch_kwargs["expand"] = ["data.balance_transaction"]
        try:
            items = _fetch_all(
                client, resource, entity, stripe_account=stripe_account, **fetch_kwargs
            )
        except stripe.AuthenticationError as exc:
            msg = (
                f"Authentication failed for entity '{entity_label}': {exc}. "
                "Check that the per-account restricted key is correct and has "
                "read permissions (charges, payouts, refunds)."
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
        pending = 0  # records staged since last commit

        def _insert_with_dedup(tx: Transaction, label: str) -> bool:
            """Insert tx unless an existing row has the same source_hash. Returns True if inserted."""
            existing = (
                session.query(Transaction)
                .filter(Transaction.source_hash == tx.source_hash)
                .first()
            )
            if existing is not None:
                logger.debug("Skipping duplicate Stripe %s (entity=%s)", label, entity_label)
                result.records_skipped += 1
                return False
            with session.begin_nested():
                session.add(tx)
            result.records_created += 1
            logger.info(
                "Ingested Stripe %s entity=%s amount=%s date=%s",
                label, entity_label, tx.amount, tx.date,
            )
            return True

        for item in items:
            item_id = getattr(item, "id", repr(item))
            result.records_processed += 1
            try:
                tx = mapper(item, entity)
                if _insert_with_dedup(tx, f"{resource} {item_id}"):
                    pending += 1

                # For charges, also emit a sibling fee transaction when the
                # expanded balance_transaction carries a non-zero fee.
                if resource == "charges":
                    fee_tx = _map_charge_fee(item, entity)
                    if fee_tx is not None and _insert_with_dedup(
                        fee_tx, f"charge_fee {item_id}"
                    ):
                        pending += 1

                if pending >= BATCH_SIZE:
                    session.commit()
                    pending = 0

            except Exception as exc:
                result.record_error(f"stripe:{entity_label}:{resource}:{item_id}", exc)

        # Commit remaining records in the last partial batch
        if pending > 0:
            session.commit()


# ---------------------------------------------------------------------------
# Adapter class
# ---------------------------------------------------------------------------


class StripeAdapter(BaseAdapter):
    """Ingests Stripe charges, payouts, and refunds for Sparkry LLC and
    BlackLine MTB LLC.

    Sparkry, BlackLine, and the top-level Travis account are SEPARATE Stripe
    accounts (not Connect), so each authenticates with its OWN restricted key —
    no ``Stripe-Account`` header:
        ``STRIPE_API_KEY``                  — Sparkry LLC account (required)
        ``STRIPE_API_KEY_BLACKLINE``        — BlackLine MTB LLC account (optional)
        ``STRIPE_API_KEY_TRAVIS_PERSONAL``  — Travis's top-level account that
                                              processes Sparkry Substack
                                              subscriptions (optional). Runs in
                                              ``PERSONAL_MIXED`` mode: only
                                              invoice/subscription charges are
                                              auto-classified; anything else is
                                              flagged via ``review_reason``.

    At least one key must be present at construction time. Accounts whose key
    is absent are skipped during :meth:`run`.

    Args:
        api_key:             Sparkry key override for testing; omit to read STRIPE_API_KEY.
        key_blackline:       BlackLine key override; omit to read STRIPE_API_KEY_BLACKLINE.
        key_travis_personal: Top-level/Substack key override; omit to read
                             STRIPE_API_KEY_TRAVIS_PERSONAL. Ingested as
                             ``entity=sparkry`` in ``PERSONAL_MIXED`` mode.
    """

    def __init__(
        self,
        api_key: str | None = None,
        key_blackline: str | None = None,
        key_travis_personal: str | None = None,
    ) -> None:
        # Sparkry, BlackLine, and the top-level Travis/Substack account are
        # SEPARATE Stripe accounts (not Connect), so each authenticates with its
        # OWN restricted key — no Stripe-Account header. ``api_key`` /
        # STRIPE_API_KEY is the Sparkry account; the others are optional.
        self._key_sparkry = api_key or os.environ.get("STRIPE_API_KEY")
        self._key_blackline = key_blackline or os.environ.get("STRIPE_API_KEY_BLACKLINE")
        self._key_travis_personal = (
            key_travis_personal or os.environ.get("STRIPE_API_KEY_TRAVIS_PERSONAL")
        )

        if not (self._key_sparkry or self._key_blackline or self._key_travis_personal):
            raise OSError(
                "No Stripe key set. Set STRIPE_API_KEY (Sparkry) and optionally "
                "STRIPE_API_KEY_BLACKLINE / STRIPE_API_KEY_TRAVIS_PERSONAL. "
                "Add them to Doppler or pass to StripeAdapter()."
            )

    @property
    def source(self) -> str:
        return Source.STRIPE.value

    def run(self, session: Session) -> AdapterResult:
        """Execute a full ingestion pass across all configured connected accounts.

        Processes Sparkry, BlackLine, and (if configured) the personal-mixed
        account sequentially. If one account's API key is invalid
        (``AuthenticationError``), remaining accounts are still processed and
        the overall result is ``PARTIAL_FAILURE``.

        After all processing, an ``IngestionLog`` entry is written.
        """
        result = AdapterResult(source=self.source)

        # Each tuple is (entity, per_account_api_key, account_mode). Separate
        # accounts → each uses its own key with NO Stripe-Account header. The
        # top-level Travis account holds Sparkry Substack revenue + personal
        # charges, so PERSONAL_MIXED flags non-invoice charges for review.
        accounts: list[tuple[Entity, str | None, str]] = [
            (Entity.SPARKRY, self._key_sparkry, ACCOUNT_MODE_STANDARD),
            (Entity.BLACKLINE, self._key_blackline, ACCOUNT_MODE_STANDARD),
            (Entity.SPARKRY, self._key_travis_personal, ACCOUNT_MODE_PERSONAL_MIXED),
        ]

        for entity, key, mode in accounts:
            if not key:
                continue  # account not configured — skip
            _ingest_entity(
                key, entity, session, result,
                stripe_account=None, mode=mode,
            )

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
