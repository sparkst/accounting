"""Validation and repair helpers for vendor-rule pattern integrity."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from src.models.audit_event import AuditEvent
from src.models.vendor_rule import VendorRule

logger = logging.getLogger(__name__)

#: entity_type discriminator for entity-mode AuditEvents recording a
#: vendor-rule field change (REQ-FIX-ING-023 / qreview P2-d1e). The repair
#: mutates rules, not transactions, so it uses the AuditEvent entity mode.
ENTITY_TYPE_VENDOR_RULE = "vendor_rule"

#: Constructs that only make sense as a regex. Deliberately NOT "any
#: metacharacter": real card descriptors carry ``*`` (``SQ *COFFEE SHOP``),
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


def validate_pattern_flag(
    pattern: str, *, is_regex: bool, force_literal: bool = False
) -> None:
    """Guard a (pattern, is_regex) pair before it reaches the database.

    REQ-FIX-ING-022. Call at every rule-creation and rule-edit site.

    REQ-VRESC-01: ``force_literal=True`` is the escape hatch for a legitimate
    descriptor that happens to carry ``|`` or ``[...]`` (e.g. ``"FOO|BAR LLC"``).
    It skips the ``looks_like_regex`` guard on the ``is_regex=False`` path so
    the string is stored verbatim and matched via ``re.escape``. Ignored when
    ``is_regex=True`` (a real regex still must compile).

    Raises:
        PatternFlagError: pattern is empty; or it carries regex constructs
            while ``is_regex`` is False and ``force_literal`` is False (it would
            be escaped and never match); or ``is_regex`` is True and the pattern
            does not compile.
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

    if not force_literal and looks_like_regex(pattern):
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
    changed_by: str = "human:operator",
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
