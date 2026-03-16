"""Tier 2: Structural pattern matching.

Applies source-based and metadata-based heuristics that are deterministic
and do not require a database lookup. Patterns are evaluated in priority order
and the first match is returned.

Returns ``None`` when no structural rule applies; the engine will then escalate
to Tier 3 (LLM).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from src.classification.engine import ClassificationResult
from src.models.enums import Direction, Entity, TaxCategory

if TYPE_CHECKING:
    from src.models.transaction import Transaction


# ---------------------------------------------------------------------------
# Structural pattern definitions
# ---------------------------------------------------------------------------

# Confidence assigned to all Tier 2 results.  High because structural rules
# are based on source provenance, which is reliable.
_TIER2_CONFIDENCE = 0.85


def match_structural_pattern(
    transaction: Transaction,
) -> ClassificationResult | None:
    """Return a :class:`ClassificationResult` if any structural rule fires.

    Evaluated in declaration order; the first matching rule wins.

    Args:
        transaction: The Transaction ORM instance (read-only).

    Returns:
        A pre-populated :class:`ClassificationResult` with ``tier_used=2``, or
        ``None`` when no rule matches.
    """
    source = (transaction.source or "").lower()
    description = (transaction.description or "").lower()

    # Raw email data fields: from_address and subject may be stored in raw_data.
    raw: dict[str, Any] = transaction.raw_data if isinstance(transaction.raw_data, dict) else {}
    from_address: str = (raw.get("from") or raw.get("from_address") or "").lower()
    subject: str = (raw.get("subject") or "").lower()

    # ── Rule 1: Shopify source → BlackLine income ───────────────────────────
    # Shopify adapter only ingests BlackLine MTB ecommerce orders.
    if source == "shopify":
        # Shopify fees are negative amounts — still BlackLine but expense.
        if _is_expense(transaction):
            return ClassificationResult(
                entity=Entity.BLACKLINE,
                tax_category=TaxCategory.SUPPLIES,
                direction=Direction.EXPENSE,
                confidence=_TIER2_CONFIDENCE,
                tier_used=2,
                reasoning="Source=shopify with negative amount → BlackLine fee/expense",
            )
        return ClassificationResult(
            entity=Entity.BLACKLINE,
            tax_category=TaxCategory.SALES_INCOME,
            direction=Direction.INCOME,
            confidence=_TIER2_CONFIDENCE,
            tier_used=2,
            reasoning="Source=shopify → BlackLine sales income",
        )

    # ── Rule 2: Stripe + Substack → Sparkry subscription income ────────────
    if source == "stripe" and (
        "substack" in description or "substack" in subject
    ):
        return ClassificationResult(
            entity=Entity.SPARKRY,
            tax_category=TaxCategory.SUBSCRIPTION_INCOME,
            direction=Direction.INCOME,
            confidence=_TIER2_CONFIDENCE,
            tier_used=2,
            reasoning="Source=stripe with 'substack' → Sparkry subscription income",
        )

    # ── Rule 3: SAP / Ariba notification → Sparkry consulting income ────────
    # Cardinal Health uses SAP Ariba for PO and payment notifications.
    if re.search(r"\bsap\b|\bariba\b", from_address) or re.search(
        r"\bsap\b|\bariba\b", description
    ):
        return ClassificationResult(
            entity=Entity.SPARKRY,
            tax_category=TaxCategory.CONSULTING_INCOME,
            direction=Direction.INCOME,
            confidence=_TIER2_CONFIDENCE,
            tier_used=2,
            reasoning="SAP/Ariba sender or description → Sparkry consulting income",
        )

    # ── Rule 4: Photo receipt source → defer to LLM (not enough structure) ─
    # Photo receipts always go to Tier 3; return None here.
    if source == "photo_receipt":
        return None

    # ── Rule 5: Self-forwarded email (from travis@sparkry.com) → defer ──────
    if "travis@sparkry.com" in from_address:
        return None

    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_expense(transaction: Transaction) -> bool:
    """Return True when the transaction amount is negative (expense)."""
    try:
        return float(transaction.amount) < 0
    except (TypeError, ValueError):
        return False
