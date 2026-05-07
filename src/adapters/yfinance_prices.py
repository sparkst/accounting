"""yfinance EOD price adapter.

Pure adapter that fetches end-of-day OHLCV data from Yahoo Finance via the
``yfinance`` library and returns a list of normalized rows. The caller handles
persistence — this module never writes to the database.

Conventions
-----------
- Float→Decimal conversions go through ``Decimal(str(value))`` to avoid the
  precision loss that ``Decimal(float_value)`` introduces.
- ``Adj Close`` is dropped; we persist the raw close so downstream total-return
  math can apply its own adjustment policy.
- Rows whose Close is NaN (delisted dates, partial sessions) are skipped.
- Open/High/Low NaN pass through as ``None`` rather than zero.
- Yahoo occasionally returns tz-aware Timestamps; we normalise to UTC before
  extracting the date so daily series don't drift by one day.
"""

from __future__ import annotations

import logging
import math
from datetime import date
from decimal import Decimal, InvalidOperation
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
    """Convert a pandas/numpy scalar to Decimal, returning None for NaN/None.

    A non-numeric string that yfinance unexpectedly returns is logged and
    coerced to None rather than crashing the batch — silent field-level
    failures here would still record the row's Close (the gate at line 96)
    but lose ancillary fields, which is the right tradeoff vs aborting.
    """
    if value is None:
        return None
    try:
        if isinstance(value, float) and math.isnan(value):
            return None
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        logger.warning("yfinance: could not coerce %r to Decimal (%s)", value, exc)
        return None


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
    except (TypeError, ValueError, OverflowError) as exc:
        logger.warning("yfinance: could not coerce %r to int (%s)", value, exc)
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
    """Coerce a DataFrame index value (Timestamp/datetime/date) into a UTC date.

    yfinance occasionally returns tz-aware Timestamps (notably for crypto
    symbols); calling ``.date()`` directly would strip the tz info and
    silently shift dates by ±1 day depending on the offset. Normalise to UTC
    first.
    """
    if isinstance(idx, date) and not hasattr(idx, "hour"):
        return idx
    ts = pd.Timestamp(idx)
    if ts.tz is not None:
        ts = ts.tz_convert("UTC")
    return cast(date, ts.date())


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
        - yfinance 1.x always returns a MultiIndex on columns even for a
          single symbol (in newer versions the level order is
          ``(field, symbol)``; in older multi-symbol calls with
          ``group_by='ticker'`` it's ``(symbol, field)``). We normalise to
          ``(symbol, field)`` before iterating so both paths share the same
          inner loop.
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

    # Normalise to (symbol, field) MultiIndex regardless of yfinance version.
    if isinstance(df.columns, pd.MultiIndex):
        # If level 0 holds field names ("Open", "Close", ...), swap so level
        # 0 is the symbol — that's the layout the loop below expects.
        level0 = set(df.columns.get_level_values(0))
        if level0 & {"Open", "Close", "High", "Low", "Adj Close", "Volume"}:
            df = df.swaplevel(axis=1).sort_index(axis=1)
    else:
        # Flat columns (older yfinance single-symbol path) — wrap in a
        # MultiIndex keyed by the lone symbol.
        df.columns = pd.MultiIndex.from_product([[symbols[0]], df.columns])

    rows: list[HistoricalPriceRow] = []
    # Iterate per symbol over the normalised (symbol, field) MultiIndex.
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

    return rows
