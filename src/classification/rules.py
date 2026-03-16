"""Tier 1: Vendor rules lookup.

Queries the ``vendor_rules`` table and attempts a case-insensitive regex (or
plain substring) match against the transaction description. Returns the
highest-confidence match, or ``None`` if no rule matches or no rule exceeds
the confidence threshold.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from src.classification.engine import ClassificationResult
from src.models.enums import Direction, Entity, TaxCategory
from src.models.vendor_rule import VendorRule


def lookup_vendor_rule(
    description: str,
    session: Session,
) -> ClassificationResult | None:
    """Return a :class:`ClassificationResult` from the best matching VendorRule.

    Matching is case-insensitive. Each ``vendor_pattern`` is first tried as a
    compiled regex; if it raises :class:`re.error` it falls back to a plain
    substring (``in``) check.

    When multiple rules match (e.g. same vendor used by two entities), they are
    ranked by ``examples`` descending then ``confidence`` descending. The
    highest-ranked match is returned.

    Args:
        description: Raw description / vendor string from the transaction.
        session: Open SQLAlchemy session.

    Returns:
        A pre-populated :class:`ClassificationResult` with ``tier_used=1``, or
        ``None`` when no rule matches.
    """
    rules: list[VendorRule] = session.query(VendorRule).all()
    if not rules:
        return None

    desc_lower = description.lower()
    matches: list[VendorRule] = []

    for rule in rules:
        pattern = rule.vendor_pattern
        try:
            if re.search(pattern, desc_lower, re.IGNORECASE):
                matches.append(rule)
        except re.error:
            # Treat invalid regex as a literal substring match.
            if pattern.lower() in desc_lower:
                matches.append(rule)

    if not matches:
        return None

    # Rank: examples desc, then confidence desc.
    best: VendorRule = max(matches, key=lambda r: (r.examples, r.confidence))

    # Update last_matched timestamp (not committed here — engine or caller does that).
    best.last_matched = datetime.now(UTC).replace(tzinfo=None)

    return ClassificationResult(
        entity=Entity(best.entity),
        tax_category=TaxCategory(best.tax_category),
        direction=Direction(best.direction),
        confidence=best.confidence,
        tier_used=1,  # will be re-set by engine but set here for clarity
        reasoning=f"Matched vendor rule: pattern={best.vendor_pattern!r}",
        tax_subcategory=best.tax_subcategory,
        deductible_pct=best.deductible_pct,
    )
