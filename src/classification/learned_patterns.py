"""Normalization and construction helpers for learned vendor rules."""

from __future__ import annotations

import logging
import re
from datetime import datetime

from src.models.enums import VendorRuleSource
from src.models.vendor_rule import VendorRule

logger = logging.getLogger(__name__)

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
        deductible_pct=deductible_pct if deductible_pct is not None else 1.0,
        confidence=confidence,
        source=VendorRuleSource.LEARNED.value,
        examples=examples,
        last_matched=last_matched,
    )
