"""Table-driven parity tests for the networth-history dedup predicate.

REQ-FIX-WLT-004. Consumes the shared fixture
``tests/fixtures/wealth-parity/networth_dedup_cases.json`` (the cross-repo
contract) and asserts a checked-in SHA-256 so silent drift fails CI here and in
the sparkry-crm vitest port.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from src.utils.networth_dedup import unmatched_active_at

_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "wealth-parity"
    / "networth_dedup_cases.json"
)

# Byte-for-byte SHA-256 of the fixture. If the fixture legitimately changes,
# recompute and update BOTH repos (this constant and the sparkry-crm vitest).
_FIXTURE_SHA256 = "56a9783005fbc37799656edd0f29365e4eb2cc237e9e1a7dc2f89a07d34142a0"


def _d(s: str) -> date:
    return date.fromisoformat(s)


def _latest_at_or_before(
    series: list[list[str]], target: date
) -> Decimal | None:
    last: Decimal | None = None
    for d_str, val in series:
        if _d(d_str) > target:
            break
        last = Decimal(val)
    return last


def test_fixture_sha256_guard() -> None:
    """REQ-FIX-WLT-004: the fixture is the contract; drift must fail CI."""
    digest = hashlib.sha256(_FIXTURE.read_bytes()).hexdigest()
    assert digest == _FIXTURE_SHA256, (
        "networth_dedup_cases.json changed — update _FIXTURE_SHA256 here AND the "
        "sparkry-crm vitest SHA (both repos assert this file byte-for-byte)."
    )


def test_dedup_cases_parity() -> None:
    """REQ-FIX-WLT-004: every fixture case recovers its expected contribution."""
    data = json.loads(_FIXTURE.read_text())
    for case in data["cases"]:
        matched_first = {k: _d(v) for k, v in case["matched_first"].items()}
        alias_cutoff = {k: _d(v) for k, v in case["alias_cutoff"].items()}
        target = _d(case["target_date"])
        total = Decimal("0")
        for raw_name, series in case["unmatched_series"].items():
            if unmatched_active_at(raw_name, target, matched_first, alias_cutoff):
                val = _latest_at_or_before(series, target)
                if val is not None:
                    total += val
        assert total == Decimal(case["expected_contribution"]), (
            f"case {case['name']!r}: got {total}, "
            f"expected {case['expected_contribution']}"
        )
