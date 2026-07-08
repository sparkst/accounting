"""Tests for the pure field-level diff engine.

REQ-VIS-002: field-level diff report (match | mismatch | vision_only | legacy_only)
with Decimal-aware comparison and equal-or-better cleanliness.
"""

from __future__ import annotations

from src.vision import diff as diff_mod
from src.vision.diff import LEGACY_ONLY, MATCH, MISMATCH, VISION_ONLY, diff_fields


def _status(report: diff_mod.DiffReport, field: str) -> str:
    return next(d.status for d in report.diffs if d.field == field)


def test_all_match_is_clean() -> None:
    """REQ-VIS-002: identical dicts → all match, clean=True."""
    legacy = {"institution": "F&G", "as_of": "2026-05-07", "balance": "660218.55"}
    report = diff_fields(legacy, dict(legacy))
    assert report.n_match == 3
    assert report.n_mismatch == 0
    assert report.clean is True


def test_decimal_post_quantization_match() -> None:
    """REQ-VIS-002: 10.5 vs 10.50 compare equal (post-quantization)."""
    report = diff_fields({"balance": "10.5"}, {"balance": "10.50"})
    assert _status(report, "balance") == MATCH
    assert report.clean is True


def test_mismatch_records_both_sides() -> None:
    """REQ-VIS-002: differing value → mismatch carrying legacy + vision."""
    report = diff_fields({"balance": "100.00"}, {"balance": "200.00"})
    d = next(x for x in report.diffs if x.field == "balance")
    assert d.status == MISMATCH
    assert d.legacy == "100.00"
    assert d.vision == "200.00"
    assert report.clean is False


def test_vision_only_is_allowed_extra() -> None:
    """REQ-VIS-002/003: a vision-only extra field is equal-or-better (clean)."""
    report = diff_fields({"a": "1"}, {"a": "1", "extra": "x"})
    assert _status(report, "extra") == VISION_ONLY
    assert report.n_vision_only == 1
    assert report.clean is True


def test_legacy_only_is_disqualifying_miss() -> None:
    """REQ-VIS-002/003: a legacy-only miss disqualifies cleanliness."""
    report = diff_fields({"a": "1", "missing": "y"}, {"a": "1"})
    assert _status(report, "missing") == LEGACY_ONLY
    assert report.n_legacy_only == 1
    assert report.clean is False


def test_exhaustive_truth_table() -> None:
    """REQ-VIS-002: one field of each status classified correctly at once."""
    legacy = {"same": "1", "diff": "10.00", "only_legacy": "L"}
    vision = {"same": "1", "diff": "20.00", "only_vision": "V"}
    report = diff_fields(legacy, vision)
    assert _status(report, "same") == MATCH
    assert _status(report, "diff") == MISMATCH
    assert _status(report, "only_legacy") == LEGACY_ONLY
    assert _status(report, "only_vision") == VISION_ONLY
    assert report.n_match == 1
    assert report.n_mismatch == 1
    assert report.n_legacy_only == 1
    assert report.n_vision_only == 1
    assert report.clean is False


def test_nested_positions_flatten_and_diff() -> None:
    """REQ-VIS-002: positions lists flatten to per-symbol dotted keys."""
    legacy = {
        "account": "8291",
        "positions": [{"symbol": "AAPL", "quantity": "10", "price": "100.00", "value": "1000.00"}],
    }
    vision = {
        "account": "8291",
        "positions": [{"symbol": "AAPL", "quantity": "10", "price": "101.00", "value": "1000.00"}],
    }
    report = diff_fields(legacy, vision)
    assert _status(report, "positions[AAPL].price") == MISMATCH
    assert _status(report, "positions[AAPL].value") == MATCH
    assert report.clean is False


def test_to_dict_serializable() -> None:
    """REQ-VIS-002: report serializes to a JSON-friendly dict for the report file."""
    report = diff_fields({"a": "1"}, {"a": "2"})
    d = report.to_dict()
    assert d["clean"] is False
    assert d["n_mismatch"] == 1
    assert isinstance(d["fields"], list)
