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
import fcntl
import logging
import os
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

# Add project root to path when invoked as a script.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from src.adapters.plaid_client import make_plaid_client  # noqa: E402
from src.adapters.plaid_transactions import sync_all_active  # noqa: E402
from src.db.connection import SessionLocal, init_db  # noqa: E402

logger = logging.getLogger("plaid_transactions_sync")


def _backup_lock_path() -> Path:
    return Path(__file__).resolve().parents[1] / "data" / ".backup.lock"


@contextmanager
def _backup_lock() -> Generator[None, None, None]:
    """Hold data/.backup.lock EX across the entire --apply write (acquire-before-
    begin, release-after-commit) so the pre-apply backup can't snapshot a partial
    supersession batch. backup.sh takes this same lock symmetrically."""
    path = _backup_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "w")  # noqa: SIM115  # must stay open across yield
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


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
    if args.apply:
        with _backup_lock(), SessionLocal() as session:
            batch = sync_all_active(session, client=client, dry_run=False)
    else:
        with SessionLocal() as session:
            batch = sync_all_active(session, client=client, dry_run=True)

    mode = "APPLIED" if args.apply else "DRY-RUN"
    logger.info(
        "plaid tx sync %s: items=%d added=%d reactivated=%d modified=%d removed=%d "
        "failed=%d superseded=%d",
        mode,
        len(batch.items),
        batch.total_added,
        batch.total_reactivated,
        batch.total_modified,
        batch.total_removed,
        batch.total_failed,
        batch.total_superseded,
    )
    for r in batch.items:
        logger.info(
            "  %s status=%s added=%d reactivated=%d failed=%d error=%s",
            r.institution_name,
            r.status,
            r.added,
            r.reactivated,
            r.failed,
            r.error_code or "-",
        )
    # Exit non-zero so launchd surfaces a failed/held-cursor sync to ops.
    # Any per-row failure (cursor held) OR any item not in a clean state
    # qualifies. A retryable INSTITUTION_DOWN sets status='institution_down'
    # (not 'error') and holds the cursor, so a status=='error'-only check would
    # exit 0 and leave ops blind to the held cursor — treat any non-ok status
    # as a failure (REQ-PT-007).
    has_failures = batch.total_failed > 0 or any(
        r.status != "ok" for r in batch.items
    )
    return 1 if has_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
