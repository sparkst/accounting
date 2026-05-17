"""
Backfill SPY/QQQ/VTI/BND historical EOD prices into the Cloudflare D1 wealth
database via the internal ingest endpoint.

Source: yfinance (10 years of daily closes).
Sink:   POST /wealth/api/internal/ingest/historical-prices (X-Internal-Key auth).
Batch:  100 rows per POST per endpoint contract.
Idempotency: server-side ON CONFLICT(symbol, trade_date) DO NOTHING.

Usage (DRY-RUN — default):
    doppler run --project accounting --config dev -- \\
      python -m scripts.backfill_historical_prices_d1 --years 10

Apply:
    doppler run --project accounting --config dev -- \\
      python -m scripts.backfill_historical_prices_d1 --years 10 --apply
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from decimal import Decimal

import requests
import yfinance as yf

BENCHMARKS = ["SPY", "QQQ", "VTI", "BND"]
BATCH_SIZE = 100
ENDPOINT_PATH = "/wealth/api/internal/ingest/historical-prices"


def fetch_symbol_history(symbol: str, years: int) -> list[dict[str, str | int | None]]:
    """Pull `years` of daily closes for `symbol` from yfinance."""
    print(f"  fetching {symbol} ({years}y)...", end=" ", flush=True)
    df = yf.Ticker(symbol).history(period=f"{years}y", auto_adjust=False)
    if df.empty:
        print("no data")
        return []
    rows: list[dict[str, str | int | None]] = []
    for ts, row in df.iterrows():
        close_val = row["Close"]
        if close_val is None or close_val != close_val:
            continue
        close_str = str(Decimal(str(float(close_val))).quantize(Decimal("0.0001")))
        volume_val = row.get("Volume", None)
        try:
            volume_int = int(volume_val) if volume_val is not None and volume_val == volume_val else None
        except (TypeError, ValueError):
            volume_int = None
        rows.append({
            "symbol": symbol,
            "trade_date": ts.strftime("%Y-%m-%d"),
            "close": close_str,
            "volume": volume_int,
            "source": "yfinance",
        })
    print(f"{len(rows)} rows")
    return rows


def post_batch(base_url: str, key: str, batch: list[dict]) -> tuple[int, int, list[str]]:
    """POST one batch; return (processed, failed, errors)."""
    resp = requests.post(
        f"{base_url.rstrip('/')}{ENDPOINT_PATH}",
        headers={"X-Internal-Key": key, "Content-Type": "application/json"},
        json=batch,
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"POST failed {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    return body.get("records_processed", 0), body.get("records_failed", 0), body.get("errors", [])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--years", type=int, default=10, help="Years of history (default 10)")
    ap.add_argument("--symbols", nargs="+", default=BENCHMARKS, help="Symbols to fetch")
    ap.add_argument("--apply", action="store_true", help="POST to D1 (default: dry-run)")
    args = ap.parse_args()

    base = os.environ.get("WEALTH_API_BASE", "").strip()
    key = os.environ.get("WEALTH_INTERNAL_KEY", "").strip()
    if not base or not key:
        print("ERROR: WEALTH_API_BASE and WEALTH_INTERNAL_KEY must be set (use doppler run --).", file=sys.stderr)
        return 2

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] target={base}  symbols={args.symbols}  years={args.years}")

    total_rows: list[dict] = []
    for sym in args.symbols:
        rows = fetch_symbol_history(sym, args.years)
        total_rows.extend(rows)
        time.sleep(0.2)

    print(f"\nTotal rows fetched: {len(total_rows)}")
    if not args.apply:
        print(f"DRY-RUN — would POST {len(total_rows)} rows in {(len(total_rows) + BATCH_SIZE - 1) // BATCH_SIZE} batches.")
        if total_rows:
            print(f"Sample first row: {total_rows[0]}")
            print(f"Sample last row:  {total_rows[-1]}")
        return 0

    processed_total = 0
    failed_total = 0
    err_seen: list[str] = []
    for i in range(0, len(total_rows), BATCH_SIZE):
        batch = total_rows[i : i + BATCH_SIZE]
        print(f"  POST batch {i // BATCH_SIZE + 1}/{(len(total_rows) + BATCH_SIZE - 1) // BATCH_SIZE} ({len(batch)} rows)...", end=" ", flush=True)
        try:
            p, f, e = post_batch(base, key, batch)
            processed_total += p
            failed_total += f
            err_seen.extend(e[:3])
            print(f"processed={p} failed={f}")
        except Exception as exc:
            failed_total += len(batch)
            err_seen.append(str(exc)[:200])
            print(f"ERROR: {exc}")
        time.sleep(0.1)

    print(f"\nDONE  processed={processed_total}  failed={failed_total}")
    if err_seen:
        print("Sample errors:")
        for e in err_seen[:5]:
            print(f"  - {e}")
    return 0 if failed_total == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
