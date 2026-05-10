"""Shared helpers for the per-broker CSV adapters.

REQ-005f: CSV parsing tolerant of BOM, CRLF, multi-section files, currency
formatting (`$`, `,`, `$-`), `"as of"` dates, 2-digit years.
REQ-005e: Idempotent re-ingest via length-framed `compute_source_hash`.

Helpers here are universally used across all 4 broker adapters. Adapter-specific
parsing (header signatures, action mapping, paired-row logic) lives in the
individual broker adapter modules.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterable, Iterator
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from src.utils.dedup import compute_source_hash

# Quantity precision used when normalizing values for the dedup hash.
# 8 decimal places covers fractional-share precision without loss.
_QUANTITY_QUANTIZE = Decimal("0.00000001")
_AMOUNT_QUANTIZE = Decimal("0.01")

# Regex matching Schwab/Fidelity composite dates like "04/22/2026 as of 04/20/2026"
_AS_OF_DATE_RE = re.compile(
    r"^\s*(\d{1,2}/\d{1,2}/\d{2,4})\s+as of\s+(\d{1,2}/\d{1,2}/\d{2,4})\s*$"
)


def parse_currency(s: str | None) -> Decimal | None:
    """Parse a brokerage currency string to Decimal.

    Handles the formats observed in real source files:
        "$1,234.56"     -> Decimal("1234.56")
        "-$1,234.56"    -> Decimal("-1234.56")
        "$-3.92"        -> Decimal("-3.92")   (Vanguard 529 dollar-before-minus)
        "(123.45)"      -> Decimal("-123.45") (parentheses-negative)
        "1,471"         -> Decimal("1471")
        ""              -> None
        None            -> None
        "--"            -> None               (Schwab placeholder)
    """
    if s is None:
        return None
    cleaned = s.strip()
    if not cleaned or cleaned == "--":
        return None

    negative = False
    # Parentheses-negative: "(123.45)"
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = cleaned[1:-1].strip()
        negative = True

    # Strip leading sign before $ if present (e.g. "-$3.92")
    if cleaned.startswith("-"):
        negative = not negative
        cleaned = cleaned[1:].lstrip()
    elif cleaned.startswith("+"):
        cleaned = cleaned[1:].lstrip()

    # Strip $ then any sign that followed it (e.g. "$-3.92")
    if cleaned.startswith("$"):
        cleaned = cleaned[1:].lstrip()
        if cleaned.startswith("-"):
            negative = not negative
            cleaned = cleaned[1:].lstrip()
        elif cleaned.startswith("+"):
            cleaned = cleaned[1:].lstrip()

    cleaned = cleaned.replace(",", "")
    if not cleaned:
        return None

    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None

    return -value if negative else value


def parse_quantity(s: str | None) -> Decimal | None:
    """Parse a quantity string. Same rules as parse_currency but no $ expected.

    Examples:
        "1,471"     -> Decimal("1471")
        "-0.836"    -> Decimal("-0.836")
        "0.221"     -> Decimal("0.221")
        ""          -> None
    """
    return parse_currency(s)  # same handling — quantities can have commas too


_DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d")


def parse_date_flexible(s: str | None) -> date | None:
    """Parse a date string trying common formats.

    Handles E*TRADE 2-digit years (`05/01/26`), 4-digit years (`1/2/2025`),
    and ISO format. Returns None on empty input.
    """
    if not s or not s.strip():
        return None
    cleaned = s.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def parse_date_with_as_of(s: str | None) -> tuple[date | None, date | None]:
    """Parse a Schwab/Fidelity 'X as of Y' date.

    Returns (trade_date, settlement_date):
        - "04/22/2026 as of 04/20/2026"  -> (2026-04-20, 2026-04-22)
          ("as of" date is the trade/effective date; leading is settlement.)
        - "04/30/2026"                    -> (2026-04-30, None)
        - ""                              -> (None, None)
    """
    if not s or not s.strip():
        return (None, None)
    m = _AS_OF_DATE_RE.match(s)
    if m:
        settlement = parse_date_flexible(m.group(1))
        trade = parse_date_flexible(m.group(2))
        return (trade, settlement)
    return (parse_date_flexible(s), None)


def read_csv_tolerant(
    path: Path | str, encoding: str = "utf-8-sig"
) -> Iterator[list[str]]:
    """Yield rows from a CSV file, tolerant of BOM and CRLF/LF line endings.

    Caller is responsible for skipping/parsing metadata rows. This helper just
    handles the encoding + newline mechanics correctly.
    """
    with open(path, encoding=encoding, newline="") as fh:
        yield from csv.reader(fh)


def find_header_row(
    rows: Iterable[list[str]], required_cols: set[str]
) -> tuple[int, list[str]]:
    """Scan rows for the first row that contains every required column name.

    Returns (row_index, header_row). Raises ValueError if no matching row is
    found in the first 50 rows (defensive bound — real headers are within the
    first ~10 rows of any file we've inspected).
    """
    for i, row in enumerate(rows):
        if i > 50:
            break
        cells = {c.strip() for c in row}
        if required_cols.issubset(cells):
            return i, row
    raise ValueError(
        f"No header row containing {required_cols} found in first 50 rows"
    )


def _normalize_decimal(d: Decimal | None, quantize: Decimal) -> str:
    """Render a Decimal at fixed precision for stable hashing.

    `Decimal("0.221") -> "0.22100000"` (8-place quantize) so two re-exports
    of the same row with cosmetic precision differences hash identically.
    """
    if d is None:
        return "0"
    return str(d.quantize(quantize))


def compute_brokerage_row_hash(
    *,
    broker: str,
    account_number: str,
    source_file: str,
    row_index: int,
    trade_date: date | None,
    action: str,
    symbol: str | None,
    quantity: Decimal | None,
    amount: Decimal | None,
    synthetic_suffix: str = "",
) -> str:
    """Stable per-row hash for brokerage_transaction dedup.

    Includes `source_file` and `row_index` to disambiguate within-file
    duplicates (e.g. two AMZN RSU vesting tranches with identical fields).

    For synthesized partner rows (e.g. E*TRADE single-row reinvest), pass
    `synthetic_suffix="div_partner"` so the synthetic row gets a stable
    distinct hash without using a fragile float offset.
    """
    parts = [
        broker,
        account_number,
        source_file,
        str(row_index),
        trade_date.isoformat() if trade_date else "",
        action,
        symbol or "",
        _normalize_decimal(quantity, _QUANTITY_QUANTIZE),
        _normalize_decimal(amount, _AMOUNT_QUANTIZE),
        synthetic_suffix,
    ]
    # Length-frame each part so '||' or ':' inside a value can't cause collisions.
    framed = "|".join(f"{len(p)}:{p}" for p in parts)
    return compute_source_hash("brokerage_row", framed)


def compute_position_row_hash(
    *,
    broker: str,
    account_number: str,
    source_file: str,
    row_index: int,
    as_of_iso: str,
    symbol: str | None,
    quantity: Decimal | None,
) -> str:
    """Stable per-row hash for position_snapshot dedup.

    Includes row_index so the same symbol can appear multiple times per file
    (Vanguard VMFXX bucket case).
    """
    parts = [
        broker,
        account_number,
        source_file,
        str(row_index),
        as_of_iso,
        symbol or "",
        _normalize_decimal(quantity, _QUANTITY_QUANTIZE),
    ]
    framed = "|".join(f"{len(p)}:{p}" for p in parts)
    return compute_source_hash("brokerage_position", framed)


def compute_realized_lot_hash(
    *,
    broker: str,
    account_number: str,
    source_file: str,
    row_index: int,
    symbol: str,
    closed_date: date | None,
    quantity: Decimal | None,
    proceeds: Decimal | None,
    cost_basis: Decimal | None,
) -> str:
    """Stable per-row hash for realized_gain_loss dedup."""
    parts = [
        broker,
        account_number,
        source_file,
        str(row_index),
        symbol,
        closed_date.isoformat() if closed_date else "",
        _normalize_decimal(quantity, _QUANTITY_QUANTIZE),
        _normalize_decimal(proceeds, _AMOUNT_QUANTIZE),
        _normalize_decimal(cost_basis, _AMOUNT_QUANTIZE),
    ]
    framed = "|".join(f"{len(p)}:{p}" for p in parts)
    return compute_source_hash("brokerage_realized", framed)


__all__ = [
    "compute_brokerage_row_hash",
    "compute_position_row_hash",
    "compute_realized_lot_hash",
    "find_header_row",
    "parse_currency",
    "parse_date_flexible",
    "parse_date_with_as_of",
    "parse_quantity",
    "read_csv_tolerant",
]
