#!/usr/bin/env python3
"""Plaid Investments daily sync — CLI wrapper around ``src.adapters.plaid_investments``.

REQ-PC-B3: for every active wealth-scope PlaidItem, fetch
``/investments/holdings/get`` and push securities + holdings to the wealth
Worker's ``ingest/plaid-holdings`` endpoint. ``INVALID_PRODUCT`` Items are
skipped-with-log (not failures). Any Plaid error or failed D1 push exits
non-zero so the systemd OnFailure alert fires.

DRY-RUN default per CLAUDE.md "DRY-RUN default for scripts" rule — a dry run
fetches from Plaid and reports what would push, but never POSTs.

Usage:
    doppler run -- python -m scripts.plaid_investments_sync           # dry-run
    doppler run -- python -m scripts.plaid_investments_sync --apply   # push
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Add project root to path when invoked as a script.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from src.adapters.plaid_client import make_plaid_client  # noqa: E402
from src.adapters.plaid_investments import sync_all_wealth  # noqa: E402
from src.db.connection import SessionLocal, init_db  # noqa: E402

logger = logging.getLogger("plaid_investments_sync")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Push holdings to the wealth D1 (default: dry-run, no POSTs).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    init_db()
    client = make_plaid_client()
    with SessionLocal() as session:
        batch = sync_all_wealth(session, client=client, dry_run=not args.apply)

    mode = "APPLIED" if args.apply else "DRY-RUN"
    logger.info(
        "plaid investments sync %s: items=%d holdings=%d failed_items=%d",
        mode,
        len(batch.items),
        batch.total_holdings,
        batch.total_failed_items,
    )
    for r in batch.items:
        logger.info(
            "  %s status=%s securities=%d holdings=%d pushed=%s error=%s",
            r.institution_name,
            r.status,
            r.securities,
            r.holdings,
            r.pushed,
            r.error_code or "-",
        )
    # Exit policy mirrors plaid_balance_sync: any Item not in a clean state
    # (ok / skipped_invalid_product) is a failure — including a failed D1
    # push, which would otherwise leave the wealth dashboard silently stale.
    has_failures = batch.total_failed_items > 0
    return 1 if has_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
