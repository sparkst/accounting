"""Structural contract for the classification rule module split (#83)."""

import ast
from pathlib import Path

from src.classification import learned_patterns, pattern_integrity, rules

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CALL_SITE_FILES = (
    "src/api/routes/transactions.py",
    "src/api/routes/invoices.py",
    "scripts/repair_vendor_rule_patterns.py",
)


def test_call_sites_import_split_symbols_from_rules_reexport() -> None:
    """REQ-RULESPLIT-03: named call sites resolve moved names via rules.py."""
    for rel_path in _CALL_SITE_FILES:
        tree = ast.parse((_REPO_ROOT / rel_path).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in (
                "src.classification.pattern_integrity",
                "src.classification.learned_patterns",
                "classification.pattern_integrity",
                "classification.learned_patterns",
            ):
                raise AssertionError(
                    f"{rel_path} imports directly from {node.module}; "
                    "must import from src.classification.rules"
                )


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
