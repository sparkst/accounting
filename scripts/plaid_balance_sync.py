#!/usr/bin/env python3
"""Plaid Balance daily sync — CLI wrapper around ``src.adapters.plaid_balance``.

DRY-RUN default per CLAUDE.md "DRY-RUN default for scripts" rule. Pass ``--apply``
to commit. Designed to run via launchd (``com.sparkry.plaid-balance-sync.plist``)
once daily at ~02:00; manual triggers are safe (idempotent on double-run).

Usage:
    doppler run -- python -m scripts.plaid_balance_sync           # dry-run
    doppler run -- python -m scripts.plaid_balance_sync --apply   # commit
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

from src.adapters.plaid_balance import (  # noqa: E402
    push_fresh_balances,
    sync_all_active,
)
from src.adapters.plaid_client import make_plaid_client  # noqa: E402
from src.db.connection import SessionLocal, init_db  # noqa: E402

logger = logging.getLogger("plaid_balance_sync")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit snapshot rows (default: dry-run, rolls back).",
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
        "plaid balance sync %s: items=%d processed=%d failed=%d",
        mode,
        len(batch.items),
        batch.total_processed,
        batch.total_failed,
    )
    for r in batch.items:
        logger.info(
            "  %s scope=%s status=%s processed=%d failed=%d unmapped=%d non_usd=%d "
            "fresh=%d error=%s",
            r.institution_name,
            r.scope,
            r.status,
            r.accounts_processed,
            r.accounts_failed,
            r.accounts_skipped_unmapped,
            r.accounts_skipped_non_usd,
            len(r.fresh_balances),
            r.error_code or "-",
        )

    # REQ-PC-B2: push WEALTH-scope fresh balances to the wealth D1 after the
    # local sync (P0-r3a: register-scope balances are local-only and never
    # pushed). DRY-RUN never POSTs — it only reports what would push.
    push_failed = False
    if args.apply:
        with SessionLocal() as session:
            push = push_fresh_balances(batch, session=session)
        logger.info("wealth D1 push: pushed=%d failed=%s", push.total_pushed, push.failed)
        for p in push.items:
            if p.error:
                logger.error("  %s push FAILED: %s", p.institution_name, p.error)
        push_failed = push.failed
    else:
        would_push = sum(
            len(r.fresh_balances) for r in batch.items if r.scope == "wealth"
        )
        logger.info("wealth D1 push skipped (dry-run): %d row(s) would push", would_push)

    # REQ-FIX-PLD-002: mirror plaid_transactions_sync.py's exit policy — any
    # accounts_failed>0 OR any Item not in a clean 'ok' state is a failure.
    # The prior policy (only terminal, non-retryable errors) silently exited 0
    # on a retryable INSTITUTION_DOWN or a partial per-account failure, hiding
    # them from the OnFailure alert. Idempotent double-runs stay exit-0
    # (IntegrityError collisions count as accounts_processed, not accounts_failed).
    # REQ-PC-B2: a failed D1 push is ALSO a failure — the non-zero exit trips
    # the OnFailure alert and replaces the wealth cron's silent-failure mode.
    has_failures = (
        batch.total_failed > 0
        or any(r.status != "ok" for r in batch.items)
        or push_failed
    )
    return 1 if has_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
