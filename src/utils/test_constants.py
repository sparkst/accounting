"""Tests for src/utils/constants.py.

REQ-ID: REQ-FIX-API-004  Outbound email sender/contact addresses use the
controlled sparkry.ai domain via a single constant. sparkry.com is not a
domain we control.
"""

from __future__ import annotations

import ast
from pathlib import Path

from src.utils.constants import INVOICE_FROM_ADDRESS, SPARKRY_CONTACT_EMAIL

SRC_ROOT = Path(__file__).resolve().parent.parent

# Files/dirs allowlisted to keep an ``@sparkry.com`` literal:
#   - src/alerts/          REQ-FIX-ALR-003's scope (its own env-default), not this REQ's.
#   - src/classification/patterns.py  matches Travis's real inbound sparkry.com mailbox
#     for self-forwarded-email detection — not an outbound contact literal.
#   - test_*.py             fixtures/assertions about the above, not production contact info.
_ALLOWLISTED_DIRS = ("alerts",)
_ALLOWLISTED_FILES = {"classification/patterns.py"}


def test_sparkry_contact_email() -> None:
    assert SPARKRY_CONTACT_EMAIL == "travis@sparkry.ai"


def test_invoice_from_address() -> None:
    assert INVOICE_FROM_ADDRESS == "Sparkry LLC <travis@sparkry.ai>"


def test_no_sparkry_com_literal_remains_under_src() -> None:
    """Grep-gate: no production source file references the uncontrolled
    sparkry.com domain in a string literal, outside the documented allowlist.
    """
    offenders: list[str] = []
    for path in SRC_ROOT.rglob("*.py"):
        rel = path.relative_to(SRC_ROOT).as_posix()
        if rel.startswith(_ALLOWLISTED_DIRS) or rel in _ALLOWLISTED_FILES:
            continue
        if path.name.startswith("test_"):
            continue
        if "__pycache__" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        if "sparkry.com" not in source:
            continue
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "sparkry.com" in node.value
            ):
                offenders.append(f"{rel}: {node.value!r}")
    assert not offenders, f"Found @sparkry.com literals outside allowlist: {offenders}"
