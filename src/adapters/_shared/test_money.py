"""Tests for src.adapters._shared.money."""

from decimal import Decimal

import pytest

from src.adapters._shared.money import (
    parse_currency,
    quantize_balance,
    quantize_shares,
)


class TestParseCurrency:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("$1,234.56", Decimal("1234.56")),
            ("1234.56", Decimal("1234.56")),
            ("$ 660,218.55", Decimal("660218.55")),
            ("-$0.04", Decimal("-0.04")),
            ("(1,234.56)", Decimal("-1234.56")),
            (" 42.00 ", Decimal("42.00")),
            (1234.56, Decimal("1234.56")),
            (1234, Decimal("1234")),
            (Decimal("9.99"), Decimal("9.99")),
            ("", Decimal("0")),
            (None, Decimal("0")),
            ("-", Decimal("0")),
        ],
    )
    def test_happy(self, raw: object, expected: Decimal) -> None:
        assert parse_currency(raw) == expected

    @pytest.mark.parametrize("bad", ["abc", "12.3.4", "$$$"])
    def test_unparseable_raises(self, bad: str) -> None:
        with pytest.raises(ValueError):
            parse_currency(bad)

    def test_unsupported_type_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_currency([1, 2, 3])

    def test_float_does_not_lose_precision(self) -> None:
        # The whole point of Decimal(str(value)): "10.5" survives intact.
        assert str(parse_currency(10.5)) == "10.5"
        assert str(parse_currency("10.50")) == "10.50"


class TestQuantize:
    def test_balance_two_places(self) -> None:
        assert quantize_balance(Decimal("1234.567")) == Decimal("1234.57")
        assert quantize_balance(Decimal("0.001")) == Decimal("0.00")

    def test_shares_eight_places(self) -> None:
        assert quantize_shares(Decimal("1.123456789")) == Decimal("1.12345679")
