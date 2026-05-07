"""Shared money parsing helpers for Phase-4 adapters.

Every adapter must convert source values to ``Decimal`` via ``Decimal(str(...))``
to preserve user-facing precision (CLAUDE.md "Critical Patterns"). These
helpers centralize the strip-and-quantize boundary so each adapter stays small.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Final

_BALANCE_QUANTUM: Final[Decimal] = Decimal("0.01")
_SHARES_QUANTUM: Final[Decimal] = Decimal("0.00000001")


def parse_currency(value: object) -> Decimal:
    """Parse a money value to ``Decimal`` with cents precision preserved.

    Accepts ``str`` ("$1,234.56", "(1,234.56)", " -42.00 "), ``int``, ``float``,
    or ``Decimal``. ``None`` and the empty string parse as ``Decimal("0")``.

    Raises ``ValueError`` on unparseable input.
    """
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if not isinstance(value, str):
        raise ValueError(f"unsupported money type: {type(value).__name__}")
    cleaned = value.strip()
    if not cleaned:
        return Decimal("0")
    negative = False
    # Accounting-style parens denote negatives.
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = cleaned[1:-1].strip()
        negative = True
    # Lone "-" is a CSV "no value" marker (Franklin Templeton uses it for
    # missing account numbers) — preserve as zero.
    if cleaned == "-":
        return Decimal("0")
    cleaned = cleaned.replace("$", "").replace(",", "").replace(" ", "")
    if cleaned == "":
        # Original input contained only currency punctuation ($$$) — garbage.
        raise ValueError(f"unparseable money value: {value!r}")
    try:
        result = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"unparseable money value: {value!r}") from exc
    return -result if negative else result


def quantize_balance(value: Decimal) -> Decimal:
    """Quantize to two decimal places (cents) — for hash inputs and display."""
    return value.quantize(_BALANCE_QUANTUM)


def quantize_shares(value: Decimal) -> Decimal:
    """Quantize to eight decimal places (broker share precision)."""
    return value.quantize(_SHARES_QUANTUM)
