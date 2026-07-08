"""Backfill total-return ``adj_close`` and populate ``stock_split`` from yfinance.

REQ-FIX-WLT-001 / REQ-FIX-WLT-002.

For every distinct symbol already in ``historical_price`` this script:
- re-fetches the full-range EOD series and writes ``adj_close`` onto matching
  ``(symbol, trade_date)`` rows. ``adj_close`` is a **derived analytics column**
  — Yahoo restates it after every new dividend/split, so idempotent overwrites
  are sanctioned. The raw ``close``, ``source`` and ``ingested_at`` are never
  touched.
- upserts corporate splits from the real ``yf.Ticker(sym).splits`` API into
  ``stock_split``. Splits are **never** derived from a close/adj_close ratio
  (that ratio also embeds dividends and cannot be separated — §2).

The price fetch and split fetch are injectable seams (``fetch_prices`` /
``fetch_splits``) so tests run entirely offline.

DRY-RUN by default. Use --apply to write.

Usage:
    python -m scripts.backfill_adjusted_closes                    # dry-run, all symbols
    python -m scripts.backfill_adjusted_closes --apply            # write
    python -m scripts.backfill_adjusted_closes --symbols SPY AAPL # subset
    python -m scripts.backfill_adjusted_closes --years 5 --apply  # last 5 years only
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.adapters.yfinance_prices import (  # noqa: E402
    HistoricalPriceRow,
    _index_to_date,
    _to_decimal,
    fetch_eod,
)
from src.db.connection import SessionLocal  # noqa: E402
from src.models.history import HistoricalPrice, StockSplit  # noqa: E402

logger = logging.getLogger(__name__)

# Injectable seams — real implementations hit the network; tests substitute fakes.
FetchPrices = Callable[[list[str], date, date], list[HistoricalPriceRow]]
FetchSplits = Callable[[str], list[tuple[date, Decimal]]]


def default_fetch_splits(symbol: str) -> list[tuple[date, Decimal]]:
    """Return ``(ex_date, ratio)`` split events from ``yf.Ticker(sym).splits``.

    ``ratio`` is Yahoo's raw split value (post/pre; 2:1 forward → 2.0, 1:10
    reverse → 0.1) carried through the ``Decimal(str(...))`` boundary. Zero /
    unparseable values are dropped. Never derived from prices (§2).
    """
    import yfinance as yf

    ticker = yf.Ticker(symbol)
    splits = ticker.splits
    events: list[tuple[date, Decimal]] = []
    if splits is None or len(splits) == 0:
        return events
    for idx, value in splits.items():
        ratio = _to_decimal(value)
        if ratio is None or ratio == 0:
            continue
        events.append((_index_to_date(idx), ratio))
    return events


def discover_symbols(session: Session) -> list[str]:
    """Distinct non-empty symbols already present in ``historical_price``."""
    rows = session.execute(select(HistoricalPrice.symbol).distinct()).scalars()
    return sorted({s.strip().upper() for s in rows if s and s.strip()})


def _full_range(session: Session) -> tuple[date | None, date | None]:
    """Min/max ``trade_date`` across all of ``historical_price`` (None if empty)."""
    lo, hi = session.execute(
        select(func.min(HistoricalPrice.trade_date), func.max(HistoricalPrice.trade_date))
    ).one()
    return lo, hi


def refresh_adj_close_for_symbol(
    session: Session,
    symbol: str,
    rows: list[HistoricalPriceRow],
    *,
    dry_run: bool,
) -> int:
    """UPDATE ``adj_close`` on existing rows matching fetched ``(symbol, date)``.

    Only mutates when the fetched ``adj_close`` is non-NULL and the row exists.
    Returns the count of rows that would be / were updated. Writes nothing in
    dry-run. Idempotent: re-runs overwrite ``adj_close`` (sanctioned — it is a
    derived column), never raw ``close``.
    """
    by_date = {
        r["trade_date"]: r["adj_close"]
        for r in rows
        if r["symbol"] == symbol and r.get("adj_close") is not None
    }
    updated = 0
    for trade_date, adj in by_date.items():
        obj = session.get(HistoricalPrice, (symbol, trade_date))
        if obj is None:
            continue
        if not dry_run:
            obj.adj_close = adj
        updated += 1
    return updated


def refresh_splits_for_symbol(
    session: Session,
    symbol: str,
    splits: list[tuple[date, Decimal]],
    *,
    dry_run: bool,
) -> int:
    """Upsert ``stock_split`` rows for ``symbol``. Returns count written/changed.

    Insert when absent; update ``ratio`` when it drifted. Writes nothing in
    dry-run.
    """
    written = 0
    for ex_date, ratio in splits:
        existing = session.get(StockSplit, (symbol, ex_date))
        if existing is None:
            if not dry_run:
                session.add(
                    StockSplit(
                        symbol=symbol,
                        ex_date=ex_date,
                        ratio=ratio,
                        source="yfinance",
                    )
                )
            written += 1
        elif existing.ratio != ratio:
            if not dry_run:
                existing.ratio = ratio
            written += 1
    return written


def backfill_adjusted(
    session: Session,
    symbols: list[str],
    start: date,
    end: date,
    *,
    dry_run: bool = True,
    fetch_prices: FetchPrices = fetch_eod,
    fetch_splits: FetchSplits = default_fetch_splits,
) -> dict[str, dict[str, int]]:
    """Backfill adj_close + stock_split for ``symbols``. Returns per-symbol summary.

    Defaults to dry-run — programmatic callers must pass ``dry_run=False`` to
    write. Per-symbol error isolation: one bad ticker cannot abort the batch.
    """
    summary: dict[str, dict[str, int]] = {}
    for symbol in sorted(symbols):
        per: dict[str, int] = {"adj_updated": 0, "splits_written": 0, "errored": 0}
        try:
            rows = fetch_prices([symbol], start, end)
            per["adj_updated"] = refresh_adj_close_for_symbol(
                session, symbol, rows, dry_run=dry_run
            )
        except Exception as exc:  # noqa: BLE001 — per-symbol isolation
            logger.warning("adj_close backfill failed for %s: %s", symbol, exc)
            per["errored"] = 1
        try:
            splits = fetch_splits(symbol)
            per["splits_written"] = refresh_splits_for_symbol(
                session, symbol, splits, dry_run=dry_run
            )
        except Exception as exc:  # noqa: BLE001 — per-symbol isolation
            logger.warning("splits backfill failed for %s: %s", symbol, exc)
            per["errored"] = 1
        summary[symbol] = per
        logger.info(
            "%s: adj_updated=%d splits_written=%d errored=%d",
            symbol,
            per["adj_updated"],
            per["splits_written"],
            per["errored"],
        )

    if not dry_run:
        session.commit()
    return summary


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true", help="Write to DB (default: dry-run).")
    p.add_argument(
        "--symbols",
        nargs="+",
        help="Restrict to these symbols (default: all in historical_price).",
    )
    p.add_argument(
        "--years",
        type=int,
        default=None,
        help="How far back to fetch (default: full historical_price range).",
    )
    return p.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _parse_args()

    session = SessionLocal()
    try:
        symbols = (
            [s.upper() for s in args.symbols]
            if args.symbols
            else discover_symbols(session)
        )

        if not symbols:
            print("No symbols in historical_price. Nothing to do.")
            return 0

        end = date.today()
        if args.years is not None:
            start = end - timedelta(days=365 * args.years)
        else:
            lo, _ = _full_range(session)
            start = lo if lo is not None else end - timedelta(days=365 * 10)

        print(
            f"{'APPLY' if args.apply else 'DRY-RUN'}: adj_close + splits for "
            f"{len(symbols)} symbols from {start} to {end}"
        )
        summary = backfill_adjusted(
            session, symbols, start=start, end=end, dry_run=not args.apply
        )

        total_adj = sum(s["adj_updated"] for s in summary.values())
        total_splits = sum(s["splits_written"] for s in summary.values())
        total_err = sum(s["errored"] for s in summary.values())
        print(
            f"\nTotals: adj_close updated={total_adj}, splits written={total_splits}, "
            f"errored symbols={total_err}"
        )
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
