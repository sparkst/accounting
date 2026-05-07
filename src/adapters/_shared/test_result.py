"""Tests for the shared BaseImportResult dataclass (FIX-5).

Verifies that the base class has the expected fields and default values, and
that adapter subclasses inherit those fields without override collisions.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.adapters._shared.result import BaseImportResult


def test_base_import_result_default_int_fields() -> None:
    """All integer counters default to zero."""
    r = BaseImportResult()
    assert r.imported == 0
    assert r.matched == 0
    assert r.unmatched == 0
    assert r.dup_skipped == 0


def test_base_import_result_default_list_fields() -> None:
    """All list fields default to distinct empty lists (not a shared instance)."""
    r1 = BaseImportResult()
    r2 = BaseImportResult()
    assert r1.errors == []
    assert r1.warnings == []
    assert r1.distinct_accounts == []
    # Mutable default isolation: modifying one instance doesn't affect another.
    r1.errors.append("oops")
    assert r2.errors == []


def test_subclass_inherits_base_fields() -> None:
    """An adapter subclass that adds extra fields still exposes all base fields."""

    @dataclass
    class MyAdapterResult(BaseImportResult):
        parsed: int = 0
        files_seen: int = 0

    r = MyAdapterResult()
    # Base fields present.
    assert r.imported == 0
    assert r.matched == 0
    assert r.unmatched == 0
    assert r.dup_skipped == 0
    assert r.errors == []
    assert r.warnings == []
    assert r.distinct_accounts == []
    # Adapter-specific fields present.
    assert r.parsed == 0
    assert r.files_seen == 0


def test_subclass_fields_can_be_populated() -> None:
    """Base and subclass fields can all be set normally after construction."""

    @dataclass
    class SimpleResult(BaseImportResult):
        parsed: int = 0

    r = SimpleResult()
    r.imported = 5
    r.unmatched = 2
    r.parsed = 7
    r.errors.append("err1")
    r.distinct_accounts.append("ACCT001")

    assert r.imported == 5
    assert r.unmatched == 2
    assert r.parsed == 7
    assert r.errors == ["err1"]
    assert r.distinct_accounts == ["ACCT001"]
