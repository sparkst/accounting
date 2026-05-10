#!/usr/bin/env python
"""Ingest brokerage CSVs from a folder structure into the isolated brokerage tables.

Usage:
    python scripts/ingest-brokerage.py /path/to/accounts/
    python scripts/ingest-brokerage.py ~/Downloads/accounts/

Expected folder layout (one subfolder per broker, name matched case-insensitively):
    accounts/
      Fidelity/   -> Accounts_History*.csv, Portfolio_Positions_*.csv
      schwab/     -> *_Transactions_*.csv, *-Positions-*.csv, *_GainLoss_Realized_*.csv
      etrade/     -> DownloadTxnHistory.csv, PortfolioDownload.csv
      vanguard/   -> OfxDownload*.csv, ofxdownload_*.csv

Idempotent: re-running with the same files produces 0 new rows.

REQ-005, REQ-005a..g.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from src.adapters.base import AdapterResult, BaseAdapter
from src.adapters.etrade_csv import EtradeCsvAdapter
from src.adapters.fidelity_csv import FidelityCsvAdapter
from src.adapters.schwab_csv import SchwabCsvAdapter
from src.adapters.vanguard_csv import VanguardCsvAdapter
from src.db.connection import SessionLocal
from src.models.enums import IngestionStatus

logger = logging.getLogger(__name__)


# Map broker subfolder name (lowercased) -> adapter class
ADAPTERS: dict[str, type[BaseAdapter]] = {
    "fidelity": FidelityCsvAdapter,
    "schwab": SchwabCsvAdapter,
    "etrade": EtradeCsvAdapter,
    "vanguard": VanguardCsvAdapter,
}


def _find_broker_folders(root: Path) -> dict[str, Path]:
    """Walk the root folder and return {broker: folder_path} for each known broker."""
    found: dict[str, Path] = {}
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        key = entry.name.lower()
        if key in ADAPTERS:
            found[key] = entry
    return found


def _print_summary(results: dict[str, AdapterResult]) -> None:
    """Pretty-print a per-adapter summary table."""
    rows = [
        ("Broker", "Status", "Created", "Skipped (dup)", "Failed"),
        ("─" * 10, "─" * 18, "─" * 8, "─" * 14, "─" * 7),
    ]
    for broker, r in results.items():
        rows.append(
            (
                broker,
                r.status.value,
                str(r.records_created),
                str(r.records_skipped),
                str(r.records_failed),
            )
        )
    widths = [max(len(row[i]) for row in rows) for i in range(5)]
    for row in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
    print()
    total_created = sum(r.records_created for r in results.values())
    total_failed = sum(r.records_failed for r in results.values())
    print(f"TOTAL: {total_created} new rows, {total_failed} failed.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "folder", help="Path containing per-broker subfolders (Fidelity, schwab, etrade, vanguard)"
    )
    parser.add_argument(
        "--brokers",
        help="Comma-separated subset of brokers to process (e.g. 'fidelity,vanguard'). Default: all found.",
        default=None,
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable DEBUG logging"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    root = Path(args.folder).expanduser().resolve()
    if not root.is_dir():
        print(f"ERROR: not a directory: {root}", file=sys.stderr)
        return 2

    found = _find_broker_folders(root)
    if not found:
        print(
            f"ERROR: no recognized broker subfolders found in {root}",
            file=sys.stderr,
        )
        print(f"Expected one of: {', '.join(sorted(ADAPTERS.keys()))}", file=sys.stderr)
        return 2

    if args.brokers:
        wanted = {b.strip().lower() for b in args.brokers.split(",")}
        unknown = wanted - set(ADAPTERS.keys())
        if unknown:
            print(f"ERROR: unknown broker(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
        found = {k: v for k, v in found.items() if k in wanted}

    print(f"Ingesting from {root}")
    print(f"Brokers: {', '.join(sorted(found.keys()))}")
    print()

    results: dict[str, AdapterResult] = {}
    session = SessionLocal()
    try:
        for broker, folder in found.items():
            print(f"→ {broker} ({folder.name}/)")
            adapter_cls = ADAPTERS[broker]
            adapter = adapter_cls(folder)  # type: ignore[call-arg]
            try:
                result = adapter.run(session)
                results[broker] = result
                print(
                    f"   created={result.records_created}, "
                    f"skipped={result.records_skipped}, "
                    f"failed={result.records_failed}, "
                    f"status={result.status.value}"
                )
            except Exception as exc:
                logger.exception("Adapter %s crashed", broker)
                # Synthesize a FAILURE result so the summary table is consistent.
                fail = AdapterResult(source=adapter.source, status=IngestionStatus.FAILURE)
                fail.errors.append(("adapter_crash", str(exc)))
                results[broker] = fail
            print()
    finally:
        session.close()

    _print_summary(results)

    any_failed = any(r.records_failed > 0 or r.status == IngestionStatus.FAILURE for r in results.values())
    return 1 if any_failed else 0


if __name__ == "__main__":
    sys.exit(main())
