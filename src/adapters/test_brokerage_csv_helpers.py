"""Tests for brokerage CSV helpers. REQ-005e/f."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from src.adapters.brokerage_csv_helpers import (
    compute_brokerage_row_hash,
    compute_position_row_hash,
    find_header_row,
    parse_currency,
    parse_date_flexible,
    parse_date_with_as_of,
    parse_quantity,
    read_csv_tolerant,
)

# parse_currency --------------------------------------------------------------


@pytest.mark.parametrize(
    "input_str,expected",
    [
        ("$1,234.56", Decimal("1234.56")),
        ("-$1,234.56", Decimal("-1234.56")),
        ("$-3.92", Decimal("-3.92")),  # Vanguard 529 dollar-before-minus
        ("(123.45)", Decimal("-123.45")),  # parentheses-negative
        ("1,471", Decimal("1471")),
        ("$0.00", Decimal("0.00")),
        ("", None),
        ("   ", None),
        ("--", None),  # Schwab placeholder
        (None, None),
        ("$43,616.06", Decimal("43616.06")),
    ],
)
def test_parse_currency(input_str: str | None, expected: Decimal | None) -> None:
    """REQ-005f: parse_currency handles $, comma, $-, parens, --."""
    assert parse_currency(input_str) == expected


def test_parse_currency_invalid_returns_none() -> None:
    assert parse_currency("not-a-number") is None


# parse_quantity --------------------------------------------------------------


@pytest.mark.parametrize(
    "input_str,expected",
    [
        ("1,471", Decimal("1471")),
        ("-0.836", Decimal("-0.836")),
        ("0.221", Decimal("0.221")),
        ("0.00000001", Decimal("0.00000001")),  # 8 decimal places
        ("", None),
        (None, None),  # P2-009: None input
        ("--", None),  # P2-009: Schwab placeholder
    ],
)
def test_parse_quantity(input_str: str | None, expected: Decimal | None) -> None:
    """REQ-005f: parse_quantity strips commas, handles negatives, None, placeholder."""
    assert parse_quantity(input_str) == expected


# read_csv_tolerant -----------------------------------------------------------


def test_read_csv_tolerant_bom_stripped(tmp_path: Path) -> None:
    """P2-010 / REQ-005f: UTF-8 BOM is stripped by read_csv_tolerant."""
    bom = b"\xef\xbb\xbf"
    csv_bytes = bom + b"Symbol,Quantity\nAMZN,100\n"
    path = tmp_path / "bom.csv"
    path.write_bytes(csv_bytes)

    rows = list(read_csv_tolerant(path))
    assert len(rows) == 2
    # BOM must not appear in the first cell.
    assert rows[0][0] == "Symbol", f"Expected 'Symbol', got {rows[0][0]!r}"
    assert rows[1] == ["AMZN", "100"]


def test_read_csv_tolerant_crlf_stripped(tmp_path: Path) -> None:
    """P2-010 / REQ-005f: Windows CRLF line endings are handled correctly."""
    csv_bytes = b"Symbol,Quantity\r\nAMZN,100\r\nMSFT,50\r\n"
    path = tmp_path / "crlf.csv"
    path.write_bytes(csv_bytes)

    rows = list(read_csv_tolerant(path))
    assert len(rows) == 3
    assert rows[0] == ["Symbol", "Quantity"]
    assert rows[1] == ["AMZN", "100"]
    assert rows[2] == ["MSFT", "50"]


# parse_date_flexible ---------------------------------------------------------


@pytest.mark.parametrize(
    "input_str,expected",
    [
        ("05/01/26", date(2026, 5, 1)),  # 2-digit year
        ("1/2/2025", date(2025, 1, 2)),  # 4-digit year, no zero-pad
        ("12/31/2024", date(2024, 12, 31)),
        ("2026-05-04", date(2026, 5, 4)),  # ISO
        ("", None),
        ("   ", None),
    ],
)
def test_parse_date_flexible(input_str: str, expected: date | None) -> None:
    """REQ-005f: parse_date_flexible handles 2- and 4-digit years."""
    assert parse_date_flexible(input_str) == expected


# parse_date_with_as_of -------------------------------------------------------


def test_parse_date_with_as_of_schwab_format() -> None:
    """REQ-005f: 'X as of Y' -> Y is trade_date, X is settlement_date."""
    trade, settlement = parse_date_with_as_of("01/16/2025 as of 01/15/2025")
    assert trade == date(2025, 1, 15)
    assert settlement == date(2025, 1, 16)


def test_parse_date_with_as_of_simple_date_no_pair() -> None:
    """Simple date returns (date, None)."""
    trade, settlement = parse_date_with_as_of("04/30/2026")
    assert trade == date(2026, 4, 30)
    assert settlement is None


def test_parse_date_with_as_of_empty() -> None:
    assert parse_date_with_as_of("") == (None, None)
    assert parse_date_with_as_of(None) == (None, None)


# find_header_row -------------------------------------------------------------


def test_find_header_row_skips_metadata() -> None:
    """REQ-005f: find_header_row scans past metadata rows (E*TRADE 6-row preamble)."""
    rows = [
        ["All Transactions Activity Types"],
        [],
        ["Account Activity for Cap 1(-6084) -6354 from 2024-05-03 to 2026-05-03"],
        [],
        ["Total:", "-40452.83"],
        [],
        ["Activity/Trade Date", "Transaction Date", "Settlement Date", "Activity Type"],
        ["05/01/26", "05/01/26", "04/30/26", "Bought"],
    ]
    idx, header = find_header_row(
        iter(rows), {"Activity/Trade Date", "Activity Type"}
    )
    assert idx == 6
    assert header[0] == "Activity/Trade Date"


def test_find_header_row_raises_when_missing() -> None:
    rows = [["a", "b"], ["c", "d"]]
    with pytest.raises(ValueError, match="No header row"):
        find_header_row(iter(rows), {"trade_date"})


# compute_brokerage_row_hash --------------------------------------------------


def test_hash_idempotent_same_inputs() -> None:
    """REQ-005e: same inputs produce same hash."""
    h1 = compute_brokerage_row_hash(
        broker="schwab",
        account_number="X724",
        source_file="x.csv",
        row_index=5,
        trade_date=date(2025, 1, 15),
        action="Sell",
        symbol="AMZN",
        quantity=Decimal("200"),
        amount=Decimal("43616.06"),
    )
    h2 = compute_brokerage_row_hash(
        broker="schwab",
        account_number="X724",
        source_file="x.csv",
        row_index=5,
        trade_date=date(2025, 1, 15),
        action="Sell",
        symbol="AMZN",
        quantity=Decimal("200"),
        amount=Decimal("43616.06"),
    )
    assert h1 == h2
    assert len(h1) == 64


def test_hash_distinguishes_within_file_duplicates() -> None:
    """REQ-005e: P1-001 from review — two AMZN RSU rows with identical fields
    on the same day must have distinct hashes via row_index."""
    common = dict(
        broker="schwab",
        account_number="X144",
        source_file="AMZN_RSU.csv",
        trade_date=date(2025, 1, 15),
        action="Journaled Shares",
        symbol="AMZN",
        quantity=Decimal("-45"),
        amount=None,
    )
    h_row5 = compute_brokerage_row_hash(row_index=5, **common)  # type: ignore[arg-type]
    h_row6 = compute_brokerage_row_hash(row_index=6, **common)  # type: ignore[arg-type]
    assert h_row5 != h_row6


def test_hash_synthetic_suffix_distinguishes() -> None:
    """REQ-005e: synthetic_suffix produces a distinct stable hash."""
    common = dict(
        broker="etrade",
        account_number="6354",
        source_file="DownloadTxnHistory.csv",
        row_index=10,
        trade_date=date(2025, 12, 12),
        action="Dividend Reinvestment",
        symbol="VUG",
        quantity=Decimal("0.123"),
        amount=Decimal("-24.09"),
    )
    real = compute_brokerage_row_hash(**common)  # type: ignore[arg-type]
    synthetic = compute_brokerage_row_hash(synthetic_suffix="div_partner", **common)  # type: ignore[arg-type]
    assert real != synthetic
    # And idempotent
    synthetic2 = compute_brokerage_row_hash(synthetic_suffix="div_partner", **common)  # type: ignore[arg-type]
    assert synthetic == synthetic2


def test_hash_normalizes_decimal_precision() -> None:
    """REQ-005e: Decimal('0.221') and Decimal('0.22100000') hash identically."""
    common = dict(
        broker="fidelity",
        account_number="Z23257759",
        source_file="x.csv",
        row_index=1,
        trade_date=date(2026, 3, 31),
        action="REINVESTMENT",
        symbol="VOO",
        amount=Decimal("-129.13"),
    )
    h1 = compute_brokerage_row_hash(quantity=Decimal("0.221"), **common)  # type: ignore[arg-type]
    h2 = compute_brokerage_row_hash(quantity=Decimal("0.22100000"), **common)  # type: ignore[arg-type]
    assert h1 == h2


def test_hash_handles_none_quantity_amount() -> None:
    """Stock split rows have empty amount/quantity — must hash deterministically."""
    h1 = compute_brokerage_row_hash(
        broker="schwab",
        account_number="X724",
        source_file="x.csv",
        row_index=9,
        trade_date=date(2026, 4, 20),
        action="Stock Split",
        symbol="VUG",
        quantity=None,
        amount=None,
    )
    h2 = compute_brokerage_row_hash(
        broker="schwab",
        account_number="X724",
        source_file="x.csv",
        row_index=9,
        trade_date=date(2026, 4, 20),
        action="Stock Split",
        symbol="VUG",
        quantity=None,
        amount=None,
    )
    assert h1 == h2


# compute_position_row_hash ---------------------------------------------------


def test_position_hash_distinguishes_buckets() -> None:
    """REQ-005c: same symbol, same as_of, different row_index -> distinct hash
    (Vanguard VMFXX bucket case)."""
    common = dict(
        broker="vanguard",
        account_number="65344815",
        source_file="OfxDownload.csv",
        as_of_iso="2026-05-04T00:00:00",
        symbol="VMFXX",
    )
    h_a = compute_position_row_hash(row_index=2, quantity=Decimal("14873.89"), **common)  # type: ignore[arg-type]
    h_b = compute_position_row_hash(row_index=5, quantity=Decimal("240.03"), **common)  # type: ignore[arg-type]
    assert h_a != h_b
