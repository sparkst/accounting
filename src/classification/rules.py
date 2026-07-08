"""Tier 1: Vendor rules lookup.

Queries the ``vendor_rules`` table and attempts a case-insensitive regex (or
plain substring) match against the transaction description. Returns the
highest-confidence match, or ``None`` if no rule matches or no rule exceeds
the confidence threshold.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from src.classification.engine import ClassificationResult
from src.models.enums import Direction, Entity, TaxCategory
from src.models.vendor_rule import VendorRule

logger = logging.getLogger(__name__)


def _match_candidates(
    rules: list[VendorRule], description: str
) -> list[tuple[VendorRule, re.Match[str]]]:
    """Return every rule in *rules* that matches *description*, paired with
    its match object (needed for specificity ranking). Shared by
    :func:`lookup_vendor_rule` and :func:`find_best_matching_rule` so both
    honor the same is_regex-driven matching semantics (REQ-FIX-ING-005)."""
    desc_lower = description.lower()
    matches: list[tuple[VendorRule, re.Match[str]]] = []

    for rule in rules:
        pattern = rule.vendor_pattern
        if rule.is_regex:
            try:
                m = re.search(pattern, desc_lower, re.IGNORECASE)
            except re.error:
                logger.warning(
                    "Invalid regex vendor rule %s (pattern=%r) — skipped",
                    rule.id,
                    pattern,
                )
                continue
        else:
            m = re.search(re.escape(pattern), desc_lower, re.IGNORECASE)
        if m:
            matches.append((rule, m))

    return matches


def _rank_best(matches: list[tuple[VendorRule, re.Match[str]]]) -> VendorRule:
    """Rank match candidates: matched-text length desc (specificity), then
    exact-literal over regex desc, then examples desc, then confidence desc,
    then id for deterministic tie-breaking (REQ-FIX-ING-009).

    The exact-literal-over-regex tiebreak (P2-a1c-2 / REQ-FIX-ING-004,
    REQ-FIX-ING-009) matters when a broad ``is_regex=True`` rule's matched
    span happens to cover the ENTIRE description (e.g. a seed rule like
    ``\\bshopify\\b`` against a description that cleans down to the single
    token "Shopify") — ``len(match.group(0))`` then ties with a newly-created
    precise literal rule for that same description, and without this
    tiebreak the tie fell through to ``examples``, where a well-established
    broad seed rule (multiple examples) would beat the brand-new precise
    rule (examples=1) and the human's correction would never take effect on
    the next lookup. An exact-literal rule for a description is always at
    least as specific as any regex that merely happens to match the same
    span, so literal wins ties outright.
    """
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
    """Return the best Tier-1-ranked VendorRule matching *description*,
    scoped to *entity* (REQ-FIX-ING-004).

    Unlike :func:`lookup_vendor_rule` (used by the classification engine
    across all entities), this is entity-scoped: the learning loop must find
    the rule that actually matches the confirmed transaction for ITS entity,
    so a correction never mutates a same-pattern rule that belongs to a
    different entity. Pure read — does not mutate ``last_matched``.
    """
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
    """Return the exact-literal rule for (description, entity), if any.

    "Exact-literal" means ``is_regex=False`` and ``vendor_pattern`` equals
    *description* case-insensitively (whitespace-trimmed) — used by the
    ING-004 divergent-correction logic to decide whether the matched rule is
    safe to overwrite in place, or whether a new precise rule must be
    created alongside a broader rule that must never be mutated.
    """
    if not description:
        return None
    needle = description.strip().lower()
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
    """Return a :class:`ClassificationResult` from the best matching VendorRule.

    Matching is case-insensitive and flag-driven (REQ-FIX-ING-005):

    - ``rule.is_regex is False`` (the default for every learned rule and every
      pre-migration row): ``vendor_pattern`` is matched as a literal
      case-insensitive substring — escaped at match time via
      ``re.search(re.escape(pattern), description, re.IGNORECASE)`` — so
      metacharacters in a verbatim vendor description (``.`` ``$`` ``(``
      ``+``) never get regex semantics.
    - ``rule.is_regex is True``: ``vendor_pattern`` is compiled as a regex.
      An invalid regex is skipped loudly (logged) rather than silently
      falling back to a substring match — a human must fix the pattern.

    When multiple rules match (e.g. same vendor used by two entities), they
    are ranked by match specificity — the actual matched-text length —
    descending, then ``examples`` descending, then ``confidence`` descending,
    then ``id`` for full determinism (REQ-FIX-ING-009). This means a precise
    rule ("amazon web services") always outranks a fatter generic rule
    ("amazon") for a description that satisfies both, regardless of example
    counts — the substrate the ING-004 broad-vs-specific correction design
    relies on.

    Args:
        description: Raw description / vendor string from the transaction.
        session: Open SQLAlchemy session.
        touch_last_matched: When True (default, used by the classification
            engine), stamps ``last_matched`` on the winning rule — not
            committed here, the engine/caller owns that. Callers that must
            remain pure reads (e.g. the REQ-MCA-002 auto-confirm backlog
            sweep, which is contractually forbidden from mutating
            ``vendor_rules``) pass ``False`` so no attribute is dirtied and
            nothing is written even in ``--apply`` mode.

    Returns:
        A pre-populated :class:`ClassificationResult` with ``tier_used=1``, or
        ``None`` when no rule matches.
    """
    rules: list[VendorRule] = session.query(VendorRule).all()
    if not rules:
        return None

    matches = _match_candidates(rules, description)
    if not matches:
        return None

    best_rule = _rank_best(matches)

    if touch_last_matched:
        # Update last_matched timestamp (not committed here — engine or caller does that).
        best_rule.last_matched = datetime.now(UTC).replace(tzinfo=None)

    return ClassificationResult(
        entity=Entity(best_rule.entity),
        tax_category=TaxCategory(best_rule.tax_category),
        direction=Direction(best_rule.direction),
        confidence=best_rule.confidence,
        tier_used=1,  # will be re-set by engine but set here for clarity
        reasoning=f"Matched vendor rule: pattern={best_rule.vendor_pattern!r}",
        tax_subcategory=best_rule.tax_subcategory,
        deductible_pct=best_rule.deductible_pct,
        rule_id=best_rule.id,  # REQ-MCA-002: auto-confirm keys on the winning rule
    )
