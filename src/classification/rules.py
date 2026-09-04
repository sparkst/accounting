"""Tier 1 vendor-rule lookup and ranking.

Pattern-integrity and learned-pattern helpers remain re-exported here for
backward-compatible imports.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from src.classification.engine import ClassificationResult
from src.models.enums import Direction, Entity, TaxCategory
from src.models.vendor_rule import VendorRule

from .learned_patterns import make_learned_vendor_rule as make_learned_vendor_rule
from .learned_patterns import (  # noqa: F401 -- compatibility re-exports
    normalize_learned_pattern as normalize_learned_pattern,
)
from .pattern_integrity import ENTITY_TYPE_VENDOR_RULE as ENTITY_TYPE_VENDOR_RULE
from .pattern_integrity import PatternFlagError as PatternFlagError
from .pattern_integrity import PatternRepairResult as PatternRepairResult
from .pattern_integrity import looks_like_regex as looks_like_regex
from .pattern_integrity import (  # noqa: F401 -- compatibility re-exports
    repair_literal_regex_rules as repair_literal_regex_rules,
)
from .pattern_integrity import validate_pattern_flag as validate_pattern_flag

logger = logging.getLogger(__name__)


def _match_candidates(
    rules: list[VendorRule], description: str
) -> list[tuple[VendorRule, re.Match[str]]]:
    """Return matching rules paired with match objects for ranking."""
    desc_lower = description.lower()
    matches: list[tuple[VendorRule, re.Match[str]]] = []

    for rule in rules:
        pattern = rule.vendor_pattern
        if rule.is_regex:
            try:
                match = re.search(pattern, desc_lower, re.IGNORECASE)
            except re.error:
                logger.warning(
                    "Invalid regex vendor rule %s (pattern=%r) — skipped",
                    rule.id,
                    pattern,
                )
                continue
        else:
            match = re.search(re.escape(pattern), desc_lower, re.IGNORECASE)
        if match:
            matches.append((rule, match))

    return matches


def _rank_best(matches: list[tuple[VendorRule, re.Match[str]]]) -> VendorRule:
    """Rank by specificity, literal status, examples, confidence, then id."""
    best_rule, _best_match = max(
        matches,
        key=lambda rm: (
            len(rm[1].group(0)),
            0 if rm[0].is_regex else 1,
            rm[0].examples,
            rm[0].confidence,
            rm[0].id,
        ),
    )
    return best_rule


def find_best_matching_rule(
    session: Session,
    description: str,
    entity: str,
) -> VendorRule | None:
    """Return the best matching rule scoped to *entity*, without mutation."""
    if not description:
        return None
    rules: list[VendorRule] = (
        session.query(VendorRule).filter(VendorRule.entity == entity).all()
    )
    if not rules:
        return None
    matches = _match_candidates(rules, description)
    if not matches:
        return None
    return _rank_best(matches)


def find_exact_literal_rule(
    session: Session,
    description: str,
    entity: str,
) -> VendorRule | None:
    """Return the exact literal rule for ``(description, entity)``, if any."""
    if not description:
        return None
    needle = normalize_learned_pattern(description).strip().lower()
    candidates: list[VendorRule] = (
        session.query(VendorRule)
        .filter(VendorRule.entity == entity, VendorRule.is_regex.is_(False))
        .all()
    )
    for rule in candidates:
        if rule.vendor_pattern.strip().lower() == needle:
            return rule
    return None


def lookup_vendor_rule(
    description: str,
    session: Session,
    *,
    touch_last_matched: bool = True,
) -> ClassificationResult | None:
    """Return a classification from the highest-ranked matching vendor rule.

    Literal rules use escaped case-insensitive substring matching; regex rules
    use their authored regex. Invalid regexes are logged and skipped. The
    winning rule is optionally stamped with ``last_matched``; the caller owns
    the commit.
    """
    rules: list[VendorRule] = session.query(VendorRule).all()
    if not rules:
        return None

    matches = _match_candidates(rules, description)
    if not matches:
        return None

    best_rule = _rank_best(matches)

    if touch_last_matched:
        best_rule.last_matched = datetime.now(UTC).replace(tzinfo=None)

    return ClassificationResult(
        entity=Entity(best_rule.entity),
        tax_category=TaxCategory(best_rule.tax_category),
        direction=Direction(best_rule.direction),
        confidence=best_rule.confidence,
        tier_used=1,
        reasoning=f"Matched vendor rule: pattern={best_rule.vendor_pattern!r}",
        tax_subcategory=best_rule.tax_subcategory,
        deductible_pct=best_rule.deductible_pct,
        rule_id=best_rule.id,
    )
