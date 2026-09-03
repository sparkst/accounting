"""Tier 1: Vendor rules lookup.

Queries the ``vendor_rules`` table and attempts a case-insensitive regex (or
plain substring) match against the transaction description. Returns the
highest-confidence match, or ``None`` if no rule matches or no rule exceeds
the confidence threshold.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from src.classification.engine import ClassificationResult
from src.models.audit_event import AuditEvent
from src.models.enums import Direction, Entity, TaxCategory, VendorRuleSource
from src.models.vendor_rule import VendorRule

logger = logging.getLogger(__name__)

#: entity_type discriminator for entity-mode AuditEvents recording a
#: vendor-rule field change (REQ-FIX-ING-023 / qreview P2-d1e). The repair
#: mutates rules, not transactions, so it uses the AuditEvent entity mode.
ENTITY_TYPE_VENDOR_RULE = "vendor_rule"


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
    # REQ-FIX-ING-024: learned rules store the normalized originator head,
    # so the exact-literal comparison must normalize the needle too.
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


# ---------------------------------------------------------------------------
# Pattern integrity — REQ-FIX-ING-022/023/024
#
# Incident 2026-09-02: REQ-FIX-ING-005 made ``is_regex=False`` mean
# ``re.escape``-ed literal matching and defaulted every pre-migration row to
# False. The human-authored regex rules (``cardinal.*health|fascinate.*os``,
# ``\bshopify\b``, ``amazon.*aws|aws\.amazon``) were silently converted to
# literals that can never match a bank description, so 34 of 76 rules went
# dead and Tier 1 stopped answering for half the vendor book.
# ---------------------------------------------------------------------------

#: Constructs that only make sense as a regex. Deliberately NOT "any
#: metacharacter": real card descriptors carry ``*`` (``SQ *COFFEE SHOP``,
#: ``PAYPAL *VENDOR``) and real vendor names carry parentheses and dots
#: (``A.B Corp (West)``), and every one of those is a legitimate literal.
#: What no literal descriptor contains is a dot-quantifier, a backslash
#: escape class, an alternation, or a character class — which is exactly
#: what all 34 poisoned production rules contained.
_REGEX_CONSTRUCTS = re.compile(
    r"""
      \.[*+?]              # dot-quantifier:  cardinal.*health
    | \\[bBdDsSwWAZ]       # escape class:    \bshopify\b , \s+
    | \\[.^$*+?()\[\]{}|]  # escaped meta:    aws\.amazon
    | \|                   # alternation:     stickermule|sticker mule
    | \[[^\]]+\]           # character class: [0-9]
    """,
    re.VERBOSE,
)

#: Per-payment tokens in an ACH/wire description. Everything from the first of
#: these onward is unique to a single payment (trace number, settlement date,
#: remittance invoice, amount), so a rule learned from it can never match
#: another payment. The text before them is the stable originator header.
#:
#: ``DESC DATE`` is in the set even though it is often empty: when an
#: originator DOES populate it the value is the payment's own date, and a
#: pattern carrying it would again match exactly one transaction. Truncating
#: there still leaves ``ORIG CO NAME:<originator>, ORIG ID:<stable id>``,
#: which is the part worth learning.
_ACH_PAYMENT_MARKERS = re.compile(
    r"TRACE\s*#|TRN\s*:|EED\s*:|RMR\*|DESC\s*DATE", re.IGNORECASE
)

#: A truncated head shorter than this is not distinctive enough to be a rule.
_MIN_LEARNED_PATTERN_LEN = 12

#: ACH/wire network boilerplate. These words appear in EVERY ACH description,
#: so a truncated head made only of them would match every ACH payment in the
#: register and misclassify the lot. Stripping them leaves the originator's
#: own name, which is the only part worth learning.
_ACH_BOILERPLATE = re.compile(
    r"""
      ORIG \s* CO \s* NAME | ORIG \s* ID | DESC \s* DATE | CO \s* ENTRY
    | DESCR | EFT\s*PAYMENT | DIRECT \s* DEPOSIT | IND \s* ID | IND \s* NAME
    | SEC \s* : | \bCCD\b | \bPPD\b | \bCTX\b | \bWEB\b | \bTEL\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

#: Distinctive residue required after boilerplate, punctuation and digits are
#: removed, before a truncated head may be used as a rule pattern.
_MIN_DISTINCTIVE_RESIDUE_LEN = 4


def _distinctive_residue(head: str) -> str:
    """What is left of *head* once ACH boilerplate, digits and punctuation go.

    For a real payment this is the originator's name (``CARDINAL HEALTH``).
    For a header the bank filled with nothing but network fields it is empty,
    which is the signal that the head must not become a rule.
    """
    residue = _ACH_BOILERPLATE.sub(" ", head)
    residue = re.sub(r"[^A-Za-z]+", " ", residue)
    return " ".join(residue.split())


class PatternFlagError(ValueError):
    """A ``vendor_pattern`` / ``is_regex`` combination that must never be stored.

    Raised when a pattern carrying hard regex metacharacters would be saved as
    a literal (the ING-005 dead-rule trap), when a pattern flagged as a regex
    does not compile, or when the pattern is empty.
    """


def looks_like_regex(pattern: str) -> bool:
    """True when *pattern* contains a construct only a regex author would write.

    False for legitimate literals that merely contain punctuation, such as
    ``SQ *COFFEE SHOP`` or ``A.B Corp (West)`` — those still match themselves
    when escaped, so flagging them would be a false positive.
    """
    return bool(_REGEX_CONSTRUCTS.search(pattern))


def validate_pattern_flag(pattern: str, *, is_regex: bool) -> None:
    """Guard a (pattern, is_regex) pair before it reaches the database.

    REQ-FIX-ING-022. Call at every rule-creation and rule-edit site.

    Raises:
        PatternFlagError: pattern is empty; or it carries regex constructs
            while ``is_regex`` is False (it would be escaped and never match);
            or ``is_regex`` is True and the pattern does not compile.
    """
    if not pattern or not pattern.strip():
        raise PatternFlagError("vendor_pattern must not be empty")

    if is_regex:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise PatternFlagError(
                f"vendor_pattern {pattern!r} is flagged is_regex=True but does "
                f"not compile: {exc}"
            ) from exc
        return

    if looks_like_regex(pattern):
        raise PatternFlagError(
            f"vendor_pattern {pattern!r} carries regex constructs but "
            f"is_regex=False, so it would be escaped and could never match a "
            f"description. Set is_regex=True, or write a plain literal."
        )


@dataclass
class PatternRepairResult:
    """Outcome of :func:`repair_literal_regex_rules`."""

    repaired: int = 0
    skipped: int = 0
    dry_run: bool = True
    repaired_ids: list[str] = field(default_factory=list)
    skipped_ids: list[str] = field(default_factory=list)


def repair_literal_regex_rules(
    session: Session,
    *,
    dry_run: bool = True,
    changed_by: str = "cron:repair_vendor_rule_patterns",
) -> PatternRepairResult:
    """Flip ``is_regex`` to True on rules whose literal pattern is really a regex.

    REQ-FIX-ING-023. Repairs the rows the ING-005 migration poisoned: an
    ``is_regex=False`` rule whose pattern carries regex constructs and compiles
    cleanly was authored as a regex and is currently dead.

    A pattern that does not compile is left alone and counted in ``skipped`` —
    flipping it would only trade a silent miss for a logged one, and a human
    has to decide what it was meant to say.

    Every applied flip writes an entity-mode ``AuditEvent`` (qreview P2-d1e):
    the flip changes how a rule classifies money, and the system's audit-trail
    invariant covers every field-level change. ``changed_by`` records the actor
    (default: the repair cron). No audit row is written for skipped rules or in
    dry-run — an audit trail records changes, not non-changes.

    DRY-RUN default: the caller must pass ``dry_run=False`` to write. The
    caller owns the commit.
    """
    result = PatternRepairResult(dry_run=dry_run)

    candidates: list[VendorRule] = (
        session.query(VendorRule).filter(VendorRule.is_regex.is_(False)).all()
    )
    for rule in candidates:
        if not looks_like_regex(rule.vendor_pattern):
            continue
        try:
            re.compile(rule.vendor_pattern)
        except re.error:
            logger.warning(
                "Vendor rule %s pattern=%r is neither a valid literal nor a "
                "valid regex — left for a human",
                rule.id,
                rule.vendor_pattern,
            )
            result.skipped += 1
            result.skipped_ids.append(rule.id)
            continue

        result.repaired += 1
        result.repaired_ids.append(rule.id)
        if not dry_run:
            rule.is_regex = True
            session.add(
                AuditEvent(
                    entity_id=rule.id,
                    entity_type=ENTITY_TYPE_VENDOR_RULE,
                    field_changed="is_regex",
                    old_value="False",
                    new_value="True",
                    changed_by=changed_by,
                )
            )

    if not dry_run and result.repaired:
        session.flush()
    return result


def normalize_learned_pattern(description: str) -> str:
    """Reduce an ACH/wire description to the part that repeats across payments.

    REQ-FIX-ING-024. The learning loop stored raw descriptions verbatim, so a
    rule learned from a Cardinal Health payment carried that payment's unique
    ``TRACE#``, ``EED``, remittance invoice and amount, and could only ever
    match the one transaction it came from. Nine such write-only rules existed
    in production.

    Truncating at the first per-payment marker keeps the stable originator
    header (``ORIG CO NAME:CARDINAL HEALTH, ORIG ID:1310958666 ...``), which is
    still a contiguous substring of every future payment, so the result stays a
    valid literal pattern and needs no regex.

    Card and vendor descriptions carry no such markers and pass through
    unchanged. A description that is *only* payment noise has no stable head,
    so the original is returned rather than an empty or useless pattern.
    """
    if not description:
        return description

    marker = _ACH_PAYMENT_MARKERS.search(description)
    if marker is None:
        return description

    head = description[: marker.start()].strip().rstrip(",;:-").strip()
    if len(head) < _MIN_LEARNED_PATTERN_LEN:
        return description

    # A head carrying no originator name is pure network boilerplate. Learning
    # it would produce a rule that matches every ACH payment in the register,
    # which is far worse than the one-shot rule this function exists to
    # replace — so degrade to the raw description instead.
    if len(_distinctive_residue(head)) < _MIN_DISTINCTIVE_RESIDUE_LEN:
        logger.debug(
            "Learned pattern head %r is ACH boilerplate only — keeping the raw "
            "description rather than a rule that would match every payment",
            head,
        )
        return description
    return head


def make_learned_vendor_rule(
    *,
    description: str,
    entity: str,
    tax_category: str,
    direction: str,
    tax_subcategory: str | None = None,
    deductible_pct: float | None = None,
    confidence: float = 0.80,
    examples: int = 1,
    last_matched: datetime | None = None,
) -> VendorRule:
    """Build a LEARNED VendorRule — the single construction chokepoint.

    qreview P1-d4e. The "every learned rule's pattern is normalized"
    invariant (REQ-FIX-ING-024) used to live as convention at three separate
    ``VendorRule(...)`` call sites; a fourth learn-site that forgot
    :func:`normalize_learned_pattern` would silently reopen the one-shot-rule
    bug. Every learn-site now routes through here, so the invariant is
    structural: the pattern is normalized exactly once, in one place, and a
    learned rule is always a literal (``is_regex=False`` — the normalized head
    is matched via ``re.escape``, so any residual metacharacter is verbatim).

    Returns an un-added, un-committed ``VendorRule``; the caller owns
    ``session.add`` and the commit (and any dedup lookup, keyed off the
    returned ``vendor_pattern`` so the dedup value is the stored value).
    """
    return VendorRule(
        vendor_pattern=normalize_learned_pattern(description),
        is_regex=False,
        entity=entity,
        tax_category=tax_category,
        tax_subcategory=tax_subcategory,
        direction=direction,
        # deductible_pct is NOT NULL with a 1.0 default; an explicit None would
        # override that default and fail the flush, so coalesce (matches the
        # invoices learn-site, which omitted the field and took the 1.0 default).
        deductible_pct=deductible_pct if deductible_pct is not None else 1.0,
        confidence=confidence,
        source=VendorRuleSource.LEARNED.value,
        examples=examples,
        last_matched=last_matched,
    )
