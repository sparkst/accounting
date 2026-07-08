"""Tests for the machine-block notes merge helper (REQ-FIX-WLT-009)."""

from __future__ import annotations

from src.adapters._shared.notes_merge import machine_block, merge_machine_block

_MARKER = "na_iul auto"


def test_append_to_human_notes_preserves_human_text() -> None:
    """REQ-FIX-WLT-009: human notes above the marker are kept verbatim."""
    human = "Call agent re: rider. Beneficiary change pending."
    block = machine_block(_MARKER, "2026-07-07", "accumulation=100.00")
    merged = merge_machine_block(human, _MARKER, block)
    assert merged.startswith(human)
    assert block in merged


def test_replace_existing_block_only_once() -> None:
    """REQ-FIX-WLT-009: a second import replaces the machine block, not the human text."""
    human = "Human curated line."
    first = merge_machine_block(
        human, _MARKER, machine_block(_MARKER, "2026-07-01", "accumulation=90.00")
    )
    second_block = machine_block(_MARKER, "2026-07-07", "accumulation=110.00")
    second = merge_machine_block(first, _MARKER, second_block)
    assert second.count("--- [na_iul auto") == 1
    assert "accumulation=90.00" not in second
    assert "accumulation=110.00" in second
    assert second.startswith(human)


def test_pure_machine_note_is_idempotent() -> None:
    """REQ-FIX-WLT-009: no human text → re-import with same block is a no-op."""
    block = machine_block(_MARKER, "2026-07-07", "accumulation=100.00")
    once = merge_machine_block(None, _MARKER, block)
    twice = merge_machine_block(once, _MARKER, block)
    assert once == block
    assert twice == block


def test_none_notes_returns_block() -> None:
    block = machine_block(_MARKER, "2026-07-07", "cost_basis=5.00")
    assert merge_machine_block(None, _MARKER, block) == block
