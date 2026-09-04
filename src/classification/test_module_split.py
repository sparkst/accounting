"""Structural contract for the classification rule module split (#83)."""

from src.classification import learned_patterns, pattern_integrity, rules


def test_pattern_integrity_symbols_live_in_new_module_and_are_reexported() -> None:
    names = (
        "PatternFlagError",
        "PatternRepairResult",
        "looks_like_regex",
        "repair_literal_regex_rules",
        "validate_pattern_flag",
    )

    for name in names:
        assert getattr(rules, name) is getattr(pattern_integrity, name)


def test_learned_pattern_symbols_live_in_new_module_and_are_reexported() -> None:
    names = ("make_learned_vendor_rule", "normalize_learned_pattern")

    for name in names:
        assert getattr(rules, name) is getattr(learned_patterns, name)
