"""Vendor-rule creation defaults (reliability-audit P0, 2026-07-27).

A pre-filled confidence of 1.0 exceeded the seeded-rule ceiling (0.95/0.97)
and the auto-confirm threshold (0.90) — a rule created by accepting the form
defaults silently removed the human from the loop for every future match.
The default now matches the learning loop's mint value (0.80): the rule
suggests aggressively but its matches still reach the review queue until a
human deliberately raises it.
"""

from __future__ import annotations

from src.api.routes.vendor_rules import VendorRuleCreate
from src.close.autoconfirm import AUTO_CONFIRM_RULE_THRESHOLD


def _create(**overrides: object) -> VendorRuleCreate:
    base: dict[str, object] = {
        "vendor_pattern": "Test Vendor",
        "entity": "sparkry",
        "tax_category": "OFFICE_EXPENSE",
        "direction": "expense",
    }
    base.update(overrides)
    return VendorRuleCreate(**base)  # type: ignore[arg-type]


def test_default_confidence_is_below_auto_confirm_threshold() -> None:
    rule = _create()
    assert rule.confidence == 0.8
    assert rule.confidence < AUTO_CONFIRM_RULE_THRESHOLD


def test_explicit_high_confidence_still_allowed() -> None:
    assert _create(confidence=0.97).confidence == 0.97


def test_deductible_default_unchanged() -> None:
    assert _create().deductible_pct == 1.0
