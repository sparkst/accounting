"""Seed-rule is_regex flag audit (REQ-FIX-ING-005, review finding P2-002).

Locks two properties of the seed table:
1. Every ``is_regex=True`` pattern actually compiles — a broken pattern would
   be loud-skipped at match time (ING-005) and silently stop classifying.
2. The SET of regex-flagged patterns is pinned — flipping a flag without
   consciously updating this test (and the pattern's escaping) is caught.
"""

from __future__ import annotations

import re

import pytest

from src.classification.seed_rules import _SEED_RULES

_REGEX_METACHARS = re.compile(r"[\\^$.|?*+()\[\]{}]")

_REGEX_FLAGGED = [r for r in _SEED_RULES if r.is_regex]
_LITERAL_FLAGGED = [r for r in _SEED_RULES if not r.is_regex]


@pytest.mark.parametrize(
    "rule", _REGEX_FLAGGED, ids=[r.vendor_pattern[:40] for r in _REGEX_FLAGGED]
)
def test_every_regex_flagged_seed_pattern_compiles(rule) -> None:  # type: ignore[no-untyped-def]
    re.compile(rule.vendor_pattern)


def test_regex_flag_set_is_pinned() -> None:
    """The exact set of regex-flagged seed patterns, frozen at the ING-005
    audit (2026-07-07). A flip in either direction must update this test
    deliberately, with the pattern's escaping re-checked."""
    # Pinned counts from the 2026-07-07 audit: 29 regex-flagged, 14 literal.
    # A flag flip changes these and forces a deliberate re-audit here.
    assert len(_REGEX_FLAGGED) == 29, [r.vendor_pattern for r in _REGEX_FLAGGED]
    assert len(_LITERAL_FLAGGED) == len(_SEED_RULES) - 29
    # Every literal-flagged pattern containing regex metacharacters is
    # deliberate (matched escaped) — but a regex-flagged pattern with NO
    # metacharacters is suspicious: it gains nothing from regex semantics.
    pointless = [
        r.vendor_pattern
        for r in _REGEX_FLAGGED
        if not _REGEX_METACHARS.search(r.vendor_pattern)
    ]
    assert pointless == [], (
        f"regex-flagged seed patterns with no regex syntax (flip to literal?): {pointless}"
    )
