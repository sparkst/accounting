"""yfinance EOD price adapter.

Pure adapter that fetches end-of-day OHLCV data from Yahoo Finance via the
``yfinance`` library and returns a list of normalized rows ready to be persisted
into the ``historical_price`` table by a separate caller (T9 backfill script).

Phase 3 — Brokerage T8.

Design notes
------------
- No DB writes here. The caller handles persistence.
- All price values are converted via ``Decimal(str(value))`` to avoid float
  precision loss.
- ``Adj Close`` is intentionally dropped. We persist the raw close so downstream
  total-return calculations can apply their own adjustment policy.
- Rows whose Close is NaN (e.g. delisted dates, partial sessions) are skipped.
- Open/High/Low values that are NaN pass through as ``None`` rather than zero.
"""

from __future__ import annotations

import logging
import math
from datetime import date
from decimal import Decimal
from typing import Any, TypedDict, cast

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)


class HistoricalPriceRow(TypedDict):
    """Shape of a single normalized EOD price row.

    Mirrors the column shape of ``src.models.history.HistoricalPrice`` minus
    the auto-managed ``source`` and ``ingested_at`` fields.
    """

    symbol: str
    trade_date: date
    close: Decimal
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    volume: int | None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_decimal(value: Any) -> Decimal | None:
    """Convert a pandas/numpy scalar to Decimal, returning None for NaN/None."""
    if value is None:
        return None
    try:
        if isinstance(value, float) and math.isnan(value):
            return None
        # pandas may return numpy scalars; pd.isna catches NaT, NaN, None
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    # NEVER Decimal(value) on a float — go through str.
    return Decimal(str(value))


def _to_int(value: Any) -> int | None:
    """Convert a pandas/numpy scalar to int, returning None for NaN/None."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _row_for_symbol(
    symbol: str,
    trade_date: date,
    close_raw: Any,
    open_raw: Any,
    high_raw: Any,
    low_raw: Any,
    volume_raw: Any,
) -> HistoricalPriceRow | None:
    """Build a HistoricalPriceRow from raw scalars; return None if Close is NaN."""
    close = _to_decimal(close_raw)
    if close is None:
        return None
    return HistoricalPriceRow(
        symbol=symbol,
        trade_date=trade_date,
        close=close,
        open=_to_decimal(open_raw),
        high=_to_decimal(high_raw),
        low=_to_decimal(low_raw),
        volume=_to_int(volume_raw),
    )


def _index_to_date(idx: Any) -> date:
    """Coerce a DataFrame index value (Timestamp, datetime, or date) into a date."""
    if isinstance(idx, date) and not hasattr(idx, "hour"):
        return idx
    # pandas Timestamp / datetime both expose .date()
    if hasattr(idx, "date"):
        return cast(date, idx.date())
    # Fallback: parse via pandas
    return cast(date, pd.Timestamp(idx).date())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_eod(
    symbols: list[str],
    start: date,
    end: date,
) -> list[HistoricalPriceRow]:
    """Fetch end-of-day OHLCV rows for ``symbols`` between ``start`` and ``end``.

    Args:
        symbols: List of ticker symbols (e.g. ``["AAPL", "MSFT"]``).
        start:   Inclusive start date.
        end:     Exclusive end date (yfinance convention).

    Returns:
        A list of HistoricalPriceRow dicts, one per (symbol, trade_date) that
        had a non-NaN Close. Rows are returned in the order yfinance yields
        them (chronological per symbol).

    Notes:
        - Calls ``yf.download`` with ``auto_adjust=False`` so we get raw Close.
        - When ``len(symbols) > 1`` the returned DataFrame has a MultiIndex on
          its columns of the form ``(symbol, field)``; we iterate per symbol.
        - When ``len(symbols) == 1`` the columns are flat
          ('Open', 'High', 'Low', 'Close', 'Adj Close', 'Volume').
        - The caller is responsible for DB persistence and dedup.
    """
    if not symbols:
        return []

    multi = len(symbols) > 1
    df: pd.DataFrame = yf.download(
        symbols,
        start=start,
        end=end,
        progress=False,
        auto_adjust=False,
        group_by="ticker" if multi else None,
    )

    if df is None or df.empty:
        logger.info("yfinance returned empty frame for symbols=%s", symbols)
        return []

    rows: list[HistoricalPriceRow] = []

    if multi:
        # Multi-symbol: columns are MultiIndex of (symbol, field).
        for symbol in symbols:
            if symbol not in df.columns.get_level_values(0):
                logger.info("yfinance: %s — no data in response, skipping", symbol)
                continue
            sub = df[symbol]
            kept = 0
            skipped = 0
            for idx, sub_row in sub.iterrows():
                trade_date = _index_to_date(idx)
                row = _row_for_symbol(
                    symbol=symbol,
                    trade_date=trade_date,
                    close_raw=sub_row.get("Close"),
                    open_raw=sub_row.get("Open"),
                    high_raw=sub_row.get("High"),
                    low_raw=sub_row.get("Low"),
                    volume_raw=sub_row.get("Volume"),
                )
                if row is None:
                    skipped += 1
                    continue
                rows.append(row)
                kept += 1
            logger.info(
                "yfinance: %s — kept %d rows, skipped %d (NaN Close)",
                symbol,
                kept,
                skipped,
            )
    else:
        # Single-symbol: flat columns.
        symbol = symbols[0]
        kept = 0
        skipped = 0
        for idx, sub_row in df.iterrows():
            trade_date = _index_to_date(idx)
            row = _row_for_symbol(
                symbol=symbol,
                trade_date=trade_date,
                close_raw=sub_row.get("Close"),
                open_raw=sub_row.get("Open"),
                high_raw=sub_row.get("High"),
                low_raw=sub_row.get("Low"),
                volume_raw=sub_row.get("Volume"),
            )
            if row is None:
                skipped += 1
                continue
            rows.append(row)
            kept += 1
        logger.info(
            "yfinance: %s — kept %d rows, skipped %d (NaN Close)",
            symbol,
            kept,
            skipped,
        )

    return rows
