"""Tests for the institution registry + legacy-extractor wrappers.

REQ-VIS-001: institution → (statement_type, prompt, schema) registry for the 5
legacy adapters; legacy_extract wraps each pure helper into the normalized dict.
"""

from __future__ import annotations

from datetime import date

import pytest

from src.vision import extract as extract_mod
from src.vision import schemas

_FG_ANNUAL_TEXT = (
    "F&G Annual Statement of Policy Values\n"
    "Contract #:        MZ152585\n"
    "Total Account Value as of 05/06/2025      $   600,000.00\n"
    "Total Account Value as of 05/07/2026      $   660,218.55\n"
)

_GSK_TEXT = "GSK Cash Balance\nClosing Balance as of May 7, 2026 $123,456.78\n"

_FT_TEXT = "STATEMENT\nPORTFOLIO OVERVIEW $12,345.67\n"


def test_registry_covers_five_institutions() -> None:
    """REQ-VIS-001: all five institutions are registered."""
    assert set(extract_mod.REGISTRY) == {"fg", "gsk", "nw_mutual", "ft", "na_iul"}
    assert extract_mod.REGISTRY["na_iul"].statement_type == schemas.POLICY_VALUES
    assert extract_mod.REGISTRY["fg"].statement_type == schemas.BALANCES


def test_mask_account_last_four() -> None:
    """REQ-VIS-001: account numbers mask all but the last 4 chars."""
    assert extract_mod.mask_account("MZ152585") == "****2585"
    assert extract_mod.mask_account("8291") == "8291"


def test_legacy_fg_annual_keeps_last_total() -> None:
    """REQ-VIS-001: F&G legacy wrapper returns the current (last) total, normalized."""
    out = extract_mod.legacy_extract("fg", text=_FG_ANNUAL_TEXT)
    assert out == {
        "institution": "F&G",
        "account_number_mask": "****2585",
        "as_of": "2026-05-07",
        "balance": "660218.55",
    }


def test_legacy_gsk_closing_balance() -> None:
    """REQ-VIS-001: GSK legacy wrapper parses the closing balance line."""
    out = extract_mod.legacy_extract("gsk", text=_GSK_TEXT)
    assert out["balance"] == "123456.78"
    assert out["as_of"] == "2026-05-07"
    assert out["account_number_mask"] == "GSK_PENSION"


def test_legacy_ft_uses_filename_date() -> None:
    """REQ-VIS-001: FT legacy wrapper derives as_of from the filename."""
    out = extract_mod.legacy_extract("ft", text=_FT_TEXT, filename="2026-03-31.pdf")
    assert out["as_of"] == "2026-03-31"
    assert out["balance"] == "12345.67"
    assert out["account_number_mask"] == "8291"


def test_legacy_nw_mutual_values_driven() -> None:
    """REQ-VIS-001: NW Mutual legacy wrapper builds from pre-extracted values."""
    out = extract_mod.legacy_extract(
        "nw_mutual",
        values={
            "policy_number": "NM123456",
            "net_accum_value": "250000",
            "as_of": date(2026, 5, 1),
        },
    )
    assert out == {
        "institution": "Northwestern Mutual",
        "account_number_mask": "****3456",
        "as_of": "2026-05-01",
        "balance": "250000.00",
    }


def test_legacy_na_iul_policy_values() -> None:
    """REQ-VIS-001: NA IUL legacy wrapper maps to the policy_values schema."""
    out = extract_mod.legacy_extract(
        "na_iul",
        values={
            "policy_number": "IUL0099",
            "as_of": date(2026, 6, 1),
            "surrender_value": "45000",
            "accumulation_value": "52000",
            "death_benefit": "500000",
            "premium_paid": "60000",
        },
    )
    assert out["surrender_value"] == "45000.00"
    assert out["cash_value"] == "52000.00"
    assert out["policy_number"] == "IUL0099"


def test_legacy_extract_unknown_institution_raises() -> None:
    """REQ-VIS-001: an unknown institution is rejected."""
    with pytest.raises(ValueError):
        extract_mod.legacy_extract("robinhood", text="x")


def test_legacy_extract_missing_input_raises() -> None:
    """REQ-VIS-001: missing required input raises (per-file isolation upstream)."""
    with pytest.raises(ValueError):
        extract_mod.legacy_extract("fg")  # no text
    with pytest.raises(ValueError):
        extract_mod.legacy_extract("nw_mutual", values={"policy_number": "x"})
