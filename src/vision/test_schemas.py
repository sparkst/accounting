"""Tests for vision statement schemas + boundary normalization.

REQ-VIS-001: normalized schema with Decimal quantization at the boundary.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from src.vision import schemas


def test_money_10_5_equals_10_50() -> None:
    """REQ-VIS-001: 10.5 and 10.50 normalize to the same quantized string."""
    assert schemas.normalize_money("10.5") == schemas.normalize_money("10.50")
    assert schemas.normalize_money(10.5) == "10.50"
    assert schemas.normalize_money(Decimal("10.5")) == "10.50"


def test_money_strips_currency_punctuation() -> None:
    """REQ-VIS-001: dollar signs and thousands separators are stripped."""
    assert schemas.normalize_money("$1,234.5") == "1234.50"


def test_money_none_and_empty_are_zero() -> None:
    """REQ-VIS-001: missing money values normalize to 0.00."""
    assert schemas.normalize_money(None) == "0.00"
    assert schemas.normalize_money("") == "0.00"


def test_money_unparseable_raises() -> None:
    """REQ-VIS-001: garbage money input raises ValueError for per-record isolation."""
    with pytest.raises(ValueError):
        schemas.normalize_money("not-money")


def test_date_coercions_to_iso() -> None:
    """REQ-VIS-001: date/datetime/US-string all coerce to ISO YYYY-MM-DD."""
    assert schemas.normalize_date(date(2026, 5, 7)) == "2026-05-07"
    assert schemas.normalize_date(datetime(2026, 5, 7, 9, 0)) == "2026-05-07"
    assert schemas.normalize_date("05/07/2026") == "2026-05-07"
    assert schemas.normalize_date("May 7, 2026") == "2026-05-07"
    assert schemas.normalize_date("2026-05-07") == "2026-05-07"


def test_normalize_fields_balances() -> None:
    """REQ-VIS-001: balances dict normalizes money + date fields."""
    out = schemas.normalize_fields(
        schemas.BALANCES,
        {
            "institution": "F&G",
            "account_number_mask": "****2585",
            "as_of": "05/07/2026",
            "balance": "660,218.5",
        },
    )
    assert out == {
        "institution": "F&G",
        "account_number_mask": "****2585",
        "as_of": "2026-05-07",
        "balance": "660218.50",
    }


def test_normalize_fields_positions_nested() -> None:
    """REQ-VIS-001: positions rows normalize symbol/quantity/price/value."""
    out = schemas.normalize_fields(
        schemas.POSITIONS,
        {
            "account": "8291",
            "as_of": "2026-03-31",
            "positions": [
                {"symbol": "aapl", "quantity": "10.5", "price": "100.1", "value": "1051.05"}
            ],
        },
    )
    pos = out["positions"][0]
    assert pos["symbol"] == "AAPL"
    assert pos["quantity"] == "10.50000000"
    assert pos["price"] == "100.10"
    assert pos["value"] == "1051.05"


def test_normalize_fields_policy_values() -> None:
    """REQ-VIS-001: policy_values money fields all quantize to cents."""
    out = schemas.normalize_fields(
        schemas.POLICY_VALUES,
        {
            "policy_number": "IUL0099",
            "as_of": "2026-06-01",
            "cash_value": "52000",
            "surrender_value": "45000.5",
            "death_benefit": "500000",
            "premium_paid": "60000",
        },
    )
    assert out["surrender_value"] == "45000.50"
    assert out["cash_value"] == "52000.00"


def test_schema_registry_has_three_types() -> None:
    """REQ-VIS-001: three statement-type schemas exist."""
    assert set(schemas.SCHEMAS) == {
        schemas.BALANCES,
        schemas.POSITIONS,
        schemas.POLICY_VALUES,
    }
