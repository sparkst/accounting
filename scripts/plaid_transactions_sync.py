#!/usr/bin/env python3
"""Plaid Transactions daily sync — CLI wrapper around ``src.adapters.plaid_transactions``.

DRY-RUN default per CLAUDE.md "DRY-RUN default for scripts" rule. Pass ``--apply``
to commit. Designed to run via launchd (``com.sparkry.plaid-transactions-sync.plist``)
once daily at ~06:30; manual triggers are safe (idempotent on double-run).

Usage:
    doppler run -- python -m scripts.plaid_transactions_sync           # dry-run
    doppler run -- python -m scripts.plaid_transactions_sync --apply   # commit
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
from src.adapters.plaid_transactions import sync_all_active  # noqa: E402
from src.db.connection import SessionLocal, init_db  # noqa: E402

logger = logging.getLogger("plaid_transactions_sync")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit transaction rows (default: dry-run, rolls back).",
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
        batch = sync_all_active(session, client=client, dry_run=not args.apply)

    mode = "APPLIED" if args.apply else "DRY-RUN"
    logger.info(
        "plaid tx sync %s: items=%d added=%d",
        mode,
        len(batch.items),
        batch.total_added,
    )
    for r in batch.items:
        logger.info(
            "  %s status=%s added=%d error=%s",
            getattr(r, "institution_name", r),
            getattr(r, "status", "-"),
            getattr(r, "added", 0),
            getattr(r, "error_code", None) or "-",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
