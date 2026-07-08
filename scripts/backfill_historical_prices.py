"""Backfill historical EOD prices from yfinance into the historical_price table.

Discovers symbols from existing position_snapshot and brokerage_transaction
rows, adds benchmark symbols (SPY, VTI, QQQ, BND), and fetches up to 10 years
of daily closes via the yfinance adapter. Idempotent on (symbol, trade_date)
PK — already-existing rows are skipped.

DRY-RUN by default. Use --apply to write.

Usage:
    python -m scripts.backfill_historical_prices                    # dry-run, all symbols
    python -m scripts.backfill_historical_prices --apply            # write
    python -m scripts.backfill_historical_prices --symbols SPY VTI  # subset
    python -m scripts.backfill_historical_prices --years 5 --apply  # last 5 years only
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from scripts.backfill_adjusted_closes import (  # noqa: E402
    FetchSplits,
    default_fetch_splits,
    refresh_adj_close_for_symbol,
    refresh_splits_for_symbol,
)
from src.adapters.yfinance_prices import HistoricalPriceRow, fetch_eod  # noqa: E402
from src.db.connection import SessionLocal  # noqa: E402
from src.models.brokerage import BrokerageTransaction, PositionSnapshot  # noqa: E402
from src.models.history import HistoricalPrice  # noqa: E402
from src.models.ingestion_log import IngestionLog  # noqa: E402

logger = logging.getLogger(__name__)

BENCHMARK_SYMBOLS: tuple[str, ...] = ("SPY", "VTI", "QQQ", "BND")


def discover_symbols(session: Session) -> set[str]:
    """Distinct non-null symbols across position_snapshot and brokerage_transaction.

    Excludes obviously-non-tradable values (cash sleeves with empty symbol,
    'TOTAL' rows, 'Generated %' adapter-bug rows).
    """
    symbols: set[str] = set()
    for sym in session.execute(select(PositionSnapshot.symbol).distinct()).scalars():
        if sym and sym.strip() and not sym.startswith("Generated") and sym.upper() != "TOTAL":
            symbols.add(sym.strip().upper())
    for sym in session.execute(select(BrokerageTransaction.symbol).distinct()).scalars():
        if sym and sym.strip():
            symbols.add(sym.strip().upper())
    return symbols


def _existing_dates_for_symbol(session: Session, symbol: str) -> set[date]:
    rows = session.execute(
        select(HistoricalPrice.trade_date).where(HistoricalPrice.symbol == symbol)
    ).scalars()
    return set(rows)


def _persist_rows(
    session: Session, rows: Iterable[HistoricalPriceRow]
) -> tuple[int, int]:
    """Insert HistoricalPrice rows, skipping PK conflicts. Returns (inserted, skipped).

    Per-row savepoint isolates IntegrityError so a conflicting row in a
    concurrent run can't roll back previously-inserted rows in the same batch.
    Caller commits.
    """
    inserted = 0
    skipped = 0
    for row in rows:
        existing = session.get(HistoricalPrice, (row["symbol"], row["trade_date"]))
        if existing is not None:
            skipped += 1
            continue
        try:
            with session.begin_nested():
                session.add(
                    HistoricalPrice(
                        symbol=row["symbol"],
                        trade_date=row["trade_date"],
                        close=row["close"],
                        # REQ-FIX-WLT-001: persist the total-return adjusted close
                        # for new rows going forward (None when the frame lacked it).
                        adj_close=row.get("adj_close"),
                        open=row["open"],
                        high=row["high"],
                        low=row["low"],
                        volume=row["volume"],
                        source="yfinance",
                    )
                )
            inserted += 1
        except IntegrityError:
            skipped += 1
    return inserted, skipped


def backfill(
    session: Session,
    symbols: list[str],
    start: date,
    end: date,
    *,
    dry_run: bool = True,
    fetch_splits: FetchSplits | None = None,
    refresh_adj_days: int = 30,
) -> dict[str, dict[str, int]]:
    """Fetch and persist historical prices. Returns per-symbol summary.

    Defaults to dry-run for safety — programmatic callers must explicitly
    pass ``dry_run=False`` to write. On apply runs, writes one IngestionLog
    audit row regardless of caller (CLI or library), and isolates yfinance
    failures per-symbol so one bad ticker can't kill the whole batch.

    REQ-FIX-WLT-001/-002 go-forward maintenance (apply runs only):
    - new rows persist ``adj_close`` (via ``_persist_rows``);
    - the trailing ``refresh_adj_days`` window of ``adj_close`` per symbol is
      re-written from the fetched frame to capture Yahoo's ex-dividend
      restatements without a full re-pull;
    - when ``fetch_splits`` is supplied, ``stock_split`` is upserted from the
      real splits API. ``fetch_splits`` defaults to ``None`` so library callers
      (tests) never touch the network unless they opt in; the CLI wires in the
      real fetcher.
    """
    apply = not dry_run
    refresh_cutoff = end - timedelta(days=refresh_adj_days)
    summary: dict[str, dict[str, int]] = {}
    failed_symbols: list[str] = []
    for symbol in sorted(symbols):
        existing_dates = _existing_dates_for_symbol(session, symbol)
        per_symbol: dict[str, int] = {
            "fetched": 0,
            "already_present": len(existing_dates),
            "new": 0,
            "inserted": 0,
            "skipped": 0,
            "adj_refreshed": 0,
            "splits_written": 0,
            "errored": 0,
        }
        try:
            all_rows = fetch_eod([symbol], start=start, end=end)
        except Exception as exc:  # noqa: BLE001 — per-symbol isolation
            logger.warning("backfill: fetch failed for %s: %s", symbol, exc)
            per_symbol["errored"] = 1
            failed_symbols.append(symbol)
            summary[symbol] = per_symbol
            continue

        new_rows = [r for r in all_rows if r["trade_date"] not in existing_dates]
        per_symbol["fetched"] = len(all_rows)
        per_symbol["new"] = len(new_rows)

        if apply and new_rows:
            inserted, skipped = _persist_rows(session, new_rows)
            per_symbol["inserted"] = inserted
            per_symbol["skipped"] = skipped

        if apply:
            # Refresh adj_close on the trailing window (already-present rows —
            # freshly inserted rows already carry adj_close).
            trailing = [r for r in all_rows if r["trade_date"] >= refresh_cutoff]
            if trailing:
                per_symbol["adj_refreshed"] = refresh_adj_close_for_symbol(
                    session, symbol, trailing, dry_run=False
                )
            if fetch_splits is not None:
                try:
                    splits = fetch_splits(symbol)
                    per_symbol["splits_written"] = refresh_splits_for_symbol(
                        session, symbol, splits, dry_run=False
                    )
                except Exception as exc:  # noqa: BLE001 — per-symbol isolation
                    logger.warning("backfill: splits fetch failed for %s: %s", symbol, exc)
                    per_symbol["errored"] = 1

        summary[symbol] = per_symbol
        logger.info(
            "%s: fetched=%d already=%d new=%d inserted=%d",
            symbol,
            per_symbol["fetched"],
            per_symbol["already_present"],
            per_symbol["new"],
            per_symbol["inserted"],
        )

    if apply:
        session.commit()
        _log_to_ingestion_log(session, summary, failed_symbols=failed_symbols)
    return summary


def _log_to_ingestion_log(
    session: Session,
    summary: dict[str, dict[str, int]],
    *,
    failed_symbols: list[str],
) -> None:
    """Audit row for the run. Stores per-symbol counts as JSON in error_detail
    (the IngestionLog model has no dedicated payload column)."""
    from src.models.enums import IngestionStatus

    total_inserted = sum(s.get("inserted", 0) for s in summary.values())
    total_new = sum(s.get("new", 0) for s in summary.values())
    payload = json.dumps(
        {
            "symbols": sorted(summary.keys()),
            "failed_symbols": failed_symbols,
            "total_new_rows": total_new,
            "total_inserted": total_inserted,
            "per_symbol": summary,
        }
    )
    status = (
        IngestionStatus.PARTIAL_FAILURE.value
        if failed_symbols
        else IngestionStatus.SUCCESS.value
    )
    log = IngestionLog(
        source="yfinance_backfill",
        run_at=datetime.now(UTC).replace(tzinfo=None),
        status=status,
        records_processed=total_inserted,
        records_failed=len(failed_symbols),
        error_detail=payload,
    )
    session.add(log)
    session.commit()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true", help="Write to DB (default: dry-run).")
    p.add_argument(
        "--symbols",
        nargs="+",
        help="Restrict to these symbols (default: discover from DB + benchmarks).",
    )
    p.add_argument(
        "--years", type=int, default=10, help="How far back to fetch (default: 10)."
    )
    p.add_argument(
        "--no-benchmarks",
        action="store_true",
        help="Skip the SPY/VTI/QQQ/BND benchmark symbols.",
    )
    return p.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _parse_args()

    end = date.today()
    start = end - timedelta(days=365 * args.years)

    session = SessionLocal()
    try:
        if args.symbols:
            symbols = [s.upper() for s in args.symbols]
        else:
            symbols = sorted(discover_symbols(session))
            if not args.no_benchmarks:
                for b in BENCHMARK_SYMBOLS:
                    if b not in symbols:
                        symbols.append(b)

        if not symbols:
            print("No symbols discovered. Nothing to do.")
            return 0

        print(f"{'APPLY' if args.apply else 'DRY-RUN'}: backfilling {len(symbols)} symbols "
              f"from {start} to {end}")
        # backfill() handles its own IngestionLog write on apply runs.
        # Wire in the real splits fetcher (network) only for the CLI path.
        summary = backfill(
            session,
            symbols,
            start=start,
            end=end,
            dry_run=not args.apply,
            fetch_splits=default_fetch_splits,
        )

        total_inserted = sum(s["inserted"] for s in summary.values())
        total_new = sum(s["new"] for s in summary.values())
        total_existing = sum(s["already_present"] for s in summary.values())
        print(f"\nTotals: existing={total_existing}, new candidates={total_new}, "
              f"inserted={total_inserted}")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
