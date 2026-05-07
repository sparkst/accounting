"""Tests for the yfinance EOD price adapter.

All tests mock ``yfinance.download`` — no live network calls.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import patch

import numpy as np
import pandas as pd

from src.adapters.yfinance_prices import fetch_eod

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _single_symbol_df() -> pd.DataFrame:
    """Build a flat-column DataFrame as yfinance returns for a single symbol."""
    idx = pd.DatetimeIndex(
        [pd.Timestamp("2025-01-02"), pd.Timestamp("2025-01-03")], name="Date"
    )
    return pd.DataFrame(
        {
            "Open": [100.10, 101.50],
            "High": [102.25, 103.00],
            "Low": [99.50, 101.00],
            "Close": [101.75, 102.80],
            "Adj Close": [101.50, 102.55],
            "Volume": [1_000_000, 1_200_000],
        },
        index=idx,
    )


def _multi_symbol_df() -> pd.DataFrame:
    """Build a MultiIndex-column DataFrame as yfinance returns for >1 symbols.

    Columns are (symbol, field): ('AAPL', 'Close'), ('AAPL', 'Open'), ...
    """
    idx = pd.DatetimeIndex(
        [pd.Timestamp("2025-01-02"), pd.Timestamp("2025-01-03")], name="Date"
    )
    cols = pd.MultiIndex.from_product(
        [["AAPL", "MSFT"], ["Open", "High", "Low", "Close", "Adj Close", "Volume"]]
    )
    data = {
        ("AAPL", "Open"): [100.10, 101.50],
        ("AAPL", "High"): [102.25, 103.00],
        ("AAPL", "Low"): [99.50, 101.00],
        ("AAPL", "Close"): [101.75, 102.80],
        ("AAPL", "Adj Close"): [101.50, 102.55],
        ("AAPL", "Volume"): [1_000_000, 1_200_000],
        ("MSFT", "Open"): [400.00, 402.50],
        ("MSFT", "High"): [405.00, 406.00],
        ("MSFT", "Low"): [399.00, 401.00],
        ("MSFT", "Close"): [404.50, 405.25],
        ("MSFT", "Adj Close"): [404.00, 404.75],
        ("MSFT", "Volume"): [800_000, 850_000],
    }
    df = pd.DataFrame(data, index=idx)
    df = df[cols]  # enforce column order
    return df


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_single_symbol_returns_normalized_rows() -> None:
    fixture = _single_symbol_df()

    with patch("src.adapters.yfinance_prices.yf.download", return_value=fixture) as mock:
        rows = fetch_eod(["AAPL"], date(2025, 1, 2), date(2025, 1, 4))

    # yf.download called with single-symbol kwargs (group_by=None)
    assert mock.call_count == 1
    _, kwargs = mock.call_args
    assert kwargs["group_by"] is None
    assert kwargs["auto_adjust"] is False
    assert kwargs["progress"] is False

    assert len(rows) == 2

    first = rows[0]
    assert first["symbol"] == "AAPL"
    assert first["trade_date"] == date(2025, 1, 2)
    assert first["close"] == Decimal("101.75")
    assert first["open"] == Decimal("100.1")
    assert first["high"] == Decimal("102.25")
    assert first["low"] == Decimal("99.5")
    assert first["volume"] == 1_000_000

    # Assert exact Decimal values, never floats
    assert isinstance(first["close"], Decimal)
    assert isinstance(first["open"], Decimal)
    # Adj Close must NOT appear anywhere in row
    assert "adj_close" not in first
    assert "Adj Close" not in first  # type: ignore[operator]


def test_multi_symbol_returns_rows_for_each_symbol() -> None:
    fixture = _multi_symbol_df()

    with patch("src.adapters.yfinance_prices.yf.download", return_value=fixture) as mock:
        rows = fetch_eod(["AAPL", "MSFT"], date(2025, 1, 2), date(2025, 1, 4))

    _, kwargs = mock.call_args
    assert kwargs["group_by"] == "ticker"

    aapl_rows = [r for r in rows if r["symbol"] == "AAPL"]
    msft_rows = [r for r in rows if r["symbol"] == "MSFT"]

    assert len(aapl_rows) == 2
    assert len(msft_rows) == 2

    assert aapl_rows[0]["close"] == Decimal("101.75")
    assert aapl_rows[0]["trade_date"] == date(2025, 1, 2)

    assert msft_rows[0]["close"] == Decimal("404.5")
    assert msft_rows[1]["close"] == Decimal("405.25")
    assert msft_rows[1]["trade_date"] == date(2025, 1, 3)


def test_nan_close_rows_are_skipped_single_symbol() -> None:
    idx = pd.DatetimeIndex(
        [
            pd.Timestamp("2025-01-02"),
            pd.Timestamp("2025-01-03"),
            pd.Timestamp("2025-01-04"),
        ],
        name="Date",
    )
    fixture = pd.DataFrame(
        {
            "Open": [100.0, 101.0, 102.0],
            "High": [101.0, 102.0, 103.0],
            "Low": [99.0, 100.0, 101.0],
            "Close": [100.5, np.nan, 102.5],
            "Adj Close": [100.5, np.nan, 102.5],
            "Volume": [1_000_000, np.nan, 1_500_000],
        },
        index=idx,
    )

    with patch("src.adapters.yfinance_prices.yf.download", return_value=fixture):
        rows = fetch_eod(["AAPL"], date(2025, 1, 2), date(2025, 1, 5))

    # Middle row (NaN Close) must be skipped
    assert len(rows) == 2
    assert rows[0]["trade_date"] == date(2025, 1, 2)
    assert rows[1]["trade_date"] == date(2025, 1, 4)


def test_nan_close_rows_are_skipped_multi_symbol() -> None:
    idx = pd.DatetimeIndex(
        [pd.Timestamp("2025-01-02"), pd.Timestamp("2025-01-03")], name="Date"
    )
    cols = pd.MultiIndex.from_product(
        [["AAPL", "MSFT"], ["Open", "High", "Low", "Close", "Adj Close", "Volume"]]
    )
    data = {
        ("AAPL", "Open"): [100.0, 101.0],
        ("AAPL", "High"): [101.0, 102.0],
        ("AAPL", "Low"): [99.0, 100.0],
        ("AAPL", "Close"): [100.5, np.nan],  # AAPL Jan 3 NaN
        ("AAPL", "Adj Close"): [100.5, np.nan],
        ("AAPL", "Volume"): [1_000_000, np.nan],
        ("MSFT", "Open"): [400.0, 401.0],
        ("MSFT", "High"): [405.0, 406.0],
        ("MSFT", "Low"): [399.0, 400.0],
        ("MSFT", "Close"): [np.nan, 405.0],  # MSFT Jan 2 NaN
        ("MSFT", "Adj Close"): [np.nan, 405.0],
        ("MSFT", "Volume"): [np.nan, 850_000],
    }
    fixture = pd.DataFrame(data, index=idx)[cols]

    with patch("src.adapters.yfinance_prices.yf.download", return_value=fixture):
        rows = fetch_eod(["AAPL", "MSFT"], date(2025, 1, 2), date(2025, 1, 4))

    aapl_rows = [r for r in rows if r["symbol"] == "AAPL"]
    msft_rows = [r for r in rows if r["symbol"] == "MSFT"]
    assert len(aapl_rows) == 1
    assert aapl_rows[0]["trade_date"] == date(2025, 1, 2)
    assert len(msft_rows) == 1
    assert msft_rows[0]["trade_date"] == date(2025, 1, 3)


def test_decimal_precision_is_preserved_via_str_conversion() -> None:
    """Floats must go through str() so that we don't get Decimal-from-float drift."""
    # 0.1 + 0.2 in float is famously 0.30000000000000004; here we use a
    # deliberately tricky value so a float-based Decimal would expose drift.
    tricky = 123.456
    idx = pd.DatetimeIndex([pd.Timestamp("2025-01-02")], name="Date")
    fixture = pd.DataFrame(
        {
            "Open": [tricky],
            "High": [tricky],
            "Low": [tricky],
            "Close": [tricky],
            "Adj Close": [tricky],
            "Volume": [1_000_000],
        },
        index=idx,
    )

    with patch("src.adapters.yfinance_prices.yf.download", return_value=fixture):
        rows = fetch_eod(["AAPL"], date(2025, 1, 2), date(2025, 1, 3))

    assert len(rows) == 1
    # Decimal(str(123.456)) -> Decimal("123.456")
    assert rows[0]["close"] == Decimal("123.456")
    # Should NOT match what Decimal(123.456) would give (a 17-digit binary expansion)
    assert rows[0]["close"] != Decimal(123.456)


def test_missing_ohl_fields_pass_through_as_none() -> None:
    """NaN Open/High/Low must produce None, not Decimal('0') or Decimal('NaN')."""
    idx = pd.DatetimeIndex([pd.Timestamp("2025-01-02")], name="Date")
    fixture = pd.DataFrame(
        {
            "Open": [np.nan],
            "High": [np.nan],
            "Low": [np.nan],
            "Close": [101.75],  # Close present, so row is kept
            "Adj Close": [101.50],
            "Volume": [np.nan],
        },
        index=idx,
    )

    with patch("src.adapters.yfinance_prices.yf.download", return_value=fixture):
        rows = fetch_eod(["AAPL"], date(2025, 1, 2), date(2025, 1, 3))

    assert len(rows) == 1
    row = rows[0]
    assert row["close"] == Decimal("101.75")
    assert row["open"] is None
    assert row["high"] is None
    assert row["low"] is None
    assert row["volume"] is None


def test_empty_symbols_returns_empty_list_without_calling_yfinance() -> None:
    with patch("src.adapters.yfinance_prices.yf.download") as mock:
        rows = fetch_eod([], date(2025, 1, 2), date(2025, 1, 3))
    assert rows == []
    mock.assert_not_called()


def test_empty_dataframe_returns_empty_list() -> None:
    with patch(
        "src.adapters.yfinance_prices.yf.download", return_value=pd.DataFrame()
    ):
        rows = fetch_eod(["AAPL"], date(2025, 1, 2), date(2025, 1, 3))
    assert rows == []
