"""Classification engine — 3-tier orchestrator.

Runs tiers in order: Tier 1 (vendor rules) → Tier 2 (structural patterns) →
Tier 3 (LLM via Claude API). Stops at the first result with confidence >= 0.7.
If no tier reaches the threshold the transaction is flagged needs_review.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from src.models.enums import Direction, Entity, Source, TaxCategory, TransactionStatus

if TYPE_CHECKING:
    from src.models.transaction import Transaction


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class ClassificationResult:
    """Output of the classification engine.

    All fields are populated on every successful classification. When the
    engine cannot reach ``confidence >= 0.7`` the status is set to
    ``needs_review`` and ``review_reason`` explains why.
    """

    entity: Entity
    tax_category: TaxCategory
    direction: Direction
    confidence: float
    tier_used: int  # 1, 2, or 3
    reasoning: str
    status: TransactionStatus = field(default=TransactionStatus.AUTO_CLASSIFIED)
    review_reason: str | None = field(default=None)
    tax_subcategory: str | None = field(default=None)
    deductible_pct: float = field(default=1.0)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

# Minimum confidence required for auto-classification.
_AUTO_CLASSIFY_THRESHOLD = 0.7

# Sources where the ingesting adapter assigns the entity AUTHORITATIVELY — Stripe
# from the per-account API key, Shopify from the store. Classification (esp. the
# Tier-3 LLM) must never reassign which business owns these rows; it was guessing
# wrong (parent-account Substack charges/fees/payouts → BlackLine). Only the
# tax category is open to (re)classification for these.
_ENTITY_AUTHORITATIVE_SOURCES = frozenset({Source.STRIPE.value, Source.SHOPIFY.value})

# Sources whose stored ``amount`` sign is the authoritative cash direction
# (negative = real outflow). For these, an income classification on a negative
# amount is internally contradictory and must be vetoed. NOTE: gmail_n8n is
# deliberately excluded — it stores income as -abs(amount) *before* the
# classifier assigns direction=income (see CLAUDE.md "Adapter behavior"), so a
# negative Gmail amount tagged income is correct, not a mismatch.
_AUTHORITATIVE_SIGN_SOURCES = frozenset({Source.PLAID.value, Source.BANK_CSV.value})

_INCOME_TAX_CATEGORIES = frozenset({
    TaxCategory.CONSULTING_INCOME,
    TaxCategory.SUBSCRIPTION_INCOME,
    TaxCategory.SALES_INCOME,
    TaxCategory.WHOLESALE_INCOME,
})


def _reconcile_sign(
    transaction: Transaction, result: ClassificationResult
) -> ClassificationResult:
    """Veto income classification on an authoritative-signed outflow.

    A Plaid/bank row with ``amount < 0`` is real money leaving the account, so
    it can never be income. When a tier nonetheless labels it income (e.g. a
    vendor keyword like "subscription" or "shopify" on a credit-card *charge*),
    override to ``OTHER_EXPENSE`` and route to ``needs_review`` so a human picks
    the real expense category — rather than silently inflating B&O gross via the
    ``abs(amount)`` tax aggregation. Returns *result* unchanged when consistent.
    """
    if transaction.source not in _AUTHORITATIVE_SIGN_SOURCES:
        return result
    if transaction.amount is None:
        return result
    try:
        is_outflow = Decimal(str(transaction.amount)) < 0
    except (InvalidOperation, ValueError):
        return result

    is_income = (
        result.direction == Direction.INCOME
        or result.tax_category in _INCOME_TAX_CATEGORIES
    )
    if not (is_outflow and is_income):
        return result

    return replace(
        result,
        direction=Direction.EXPENSE,
        tax_category=TaxCategory.OTHER_EXPENSE,
        status=TransactionStatus.NEEDS_REVIEW,
        deductible_pct=1.0,
        review_reason=(
            f"Sign/category mismatch: {transaction.source} amount "
            f"{transaction.amount} is an outflow but was classified as income "
            f"({result.tax_category.value}, tier {result.tier_used}). Overridden "
            "to expense for review."
        ),
    )


def classify(
    transaction: Transaction,
    session: Session,
    *,
    anthropic_api_key: str | None = None,
) -> ClassificationResult:
    """Classify *transaction* using the 3-tier pipeline.

    Tiers are imported lazily to avoid circular imports during module load,
    but are referenced by their module-qualified names so that
    ``unittest.mock.patch`` can intercept them in tests.

    Args:
        transaction: The Transaction ORM instance to classify. The instance
            is **not** mutated here — callers are responsible for applying
            the result back to the model and committing.
        session: An open SQLAlchemy session used by Tier 1 to query
            VendorRule rows.
        anthropic_api_key: Optional API key override for Tier 3. When *None*
            the LLM classifier falls back to the ``ANTHROPIC_API_KEY``
            environment variable.

    Returns:
        A :class:`ClassificationResult` populated by whichever tier succeeded.
    """
    # Late imports break the circular-import cycle at load time while still
    # allowing patch() to intercept calls during tests — patch the functions
    # at their *home* module, e.g. ``src.classification.rules.lookup_vendor_rule``.
    from src.classification import llm_classifier as _llm_mod
    from src.classification import patterns as _pat_mod
    from src.classification import rules as _rules_mod

    # ── Tier 1: Vendor rules ────────────────────────────────────────────────
    tier1 = _rules_mod.lookup_vendor_rule(transaction.description, session)
    if tier1 is not None and tier1.confidence >= _AUTO_CLASSIFY_THRESHOLD:
        tier1.tier_used = 1
        tier1.status = TransactionStatus.AUTO_CLASSIFIED
        return _reconcile_sign(transaction, tier1)

    # ── Tier 2: Structural patterns ─────────────────────────────────────────
    tier2 = _pat_mod.match_structural_pattern(transaction)
    if tier2 is not None and tier2.confidence >= _AUTO_CLASSIFY_THRESHOLD:
        tier2.tier_used = 2
        tier2.status = TransactionStatus.AUTO_CLASSIFIED
        return _reconcile_sign(transaction, tier2)

    # ── Tier 3: LLM classification ──────────────────────────────────────────
    tier3 = _llm_mod.llm_classify(transaction, api_key=anthropic_api_key, _session=session)
    if tier3.confidence >= _AUTO_CLASSIFY_THRESHOLD:
        tier3.tier_used = 3
        tier3.status = TransactionStatus.AUTO_CLASSIFIED
        return _reconcile_sign(transaction, tier3)

    # ── Needs review ────────────────────────────────────────────────────────
    # Best partial result is kept so the reviewer has a pre-filled suggestion.
    tier3.tier_used = 3
    tier3.status = TransactionStatus.NEEDS_REVIEW
    tier3.review_reason = (
        f"Low confidence ({tier3.confidence:.2f}) from Tier 3 LLM: "
        f"{tier3.reasoning}"
    )
    return _reconcile_sign(transaction, tier3)


def apply_result(transaction: Transaction, result: ClassificationResult) -> None:
    """Write a :class:`ClassificationResult` back onto a Transaction ORM instance.

    Does **not** commit the session — that is the caller's responsibility.
    """
    # Preserve the adapter's entity for sources where it is structurally
    # determined (Stripe per-account, Shopify store). Classification may set the
    # entity only when the adapter could not (e.g. gmail/bank → entity is None).
    if (
        transaction.source not in _ENTITY_AUTHORITATIVE_SOURCES
        or transaction.entity is None
    ):
        transaction.entity = result.entity.value
    transaction.tax_category = result.tax_category.value
    transaction.direction = result.direction.value
    transaction.confidence = result.confidence
    transaction.status = result.status.value
    transaction.review_reason = result.review_reason
    if result.tax_subcategory:
        transaction.tax_subcategory = result.tax_subcategory
    transaction.deductible_pct = result.deductible_pct

    # Missing amounts always need review regardless of classification confidence
    if transaction.amount is None and transaction.status != TransactionStatus.NEEDS_REVIEW.value:
        transaction.status = TransactionStatus.NEEDS_REVIEW.value
        reason = (transaction.review_reason or "")
        transaction.review_reason = (
            reason + " Amount is missing — manual entry required."
        ).strip()
