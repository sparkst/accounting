"""Classification engine — 3-tier orchestrator.

Runs tiers in order: Tier 1 (vendor rules) → Tier 2 (structural patterns) →
Tier 3 (LLM via Claude API). Stops at the first result with confidence >= 0.7.
If no tier reaches the threshold the transaction is flagged needs_review.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from src.models.enums import Direction, Entity, TaxCategory, TransactionStatus

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
        return tier1

    # ── Tier 2: Structural patterns ─────────────────────────────────────────
    tier2 = _pat_mod.match_structural_pattern(transaction)
    if tier2 is not None and tier2.confidence >= _AUTO_CLASSIFY_THRESHOLD:
        tier2.tier_used = 2
        tier2.status = TransactionStatus.AUTO_CLASSIFIED
        return tier2

    # ── Tier 3: LLM classification ──────────────────────────────────────────
    tier3 = _llm_mod.llm_classify(transaction, api_key=anthropic_api_key)
    if tier3.confidence >= _AUTO_CLASSIFY_THRESHOLD:
        tier3.tier_used = 3
        tier3.status = TransactionStatus.AUTO_CLASSIFIED
        return tier3

    # ── Needs review ────────────────────────────────────────────────────────
    # Best partial result is kept so the reviewer has a pre-filled suggestion.
    tier3.tier_used = 3
    tier3.status = TransactionStatus.NEEDS_REVIEW
    tier3.review_reason = (
        f"Low confidence ({tier3.confidence:.2f}) from Tier 3 LLM: "
        f"{tier3.reasoning}"
    )
    return tier3


def apply_result(transaction: Transaction, result: ClassificationResult) -> None:
    """Write a :class:`ClassificationResult` back onto a Transaction ORM instance.

    Does **not** commit the session — that is the caller's responsibility.
    """
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
