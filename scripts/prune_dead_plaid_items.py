#!/usr/bin/env python3
"""One-time data fix: disconnect dead placeholder Plaid Items — REQ-FIX-PLD-004.

Two abandoned-OAuth placeholder rows (``item_id LIKE 'placeholder_%'``,
``status='active'``, undecryptable ``access_token_encrypted``) throw
``INVALID_ACCESS_TOKEN`` every night in the balance sync. The query-parity fix
in ``src.adapters.plaid_balance.sync_all_active`` already excludes them from
future sync rotation; this script is the one-time data fix that flips the
rows themselves so the UI (and any other status='active' query) stops
treating them as live:

  1. ``status = 'disconnected'``
  2. ``access_token_encrypted = REVOKED_TOKEN_SENTINEL`` (ciphertext does not
     linger in SQLite freed pages / WAL snapshots — same sentinel the
     ``/disconnect`` API route uses)
  3. ``last_error`` records the reason

Never deletes — the rows remain visible (as disconnected) in
``GET /api/plaid/reconciliation/summary`` and the items list.

DRY-RUN by default; pass ``--apply`` to write. Audited: every row touched is
printed (dry-run or applied) with its item_id/institution/prior status.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy.orm import Session  # noqa: E402

from src.db.connection import SessionLocal, init_db  # noqa: E402
from src.models.plaid import REVOKED_TOKEN_SENTINEL, PlaidItem  # noqa: E402

logger = logging.getLogger("prune_dead_plaid_items")

REASON = "PLD-004: dead placeholder, undecryptable token"  # fits last_error's String(64)


def prune_dead_items(session: Session, *, apply: bool) -> list[PlaidItem]:
    """Find and (when apply) disconnect placeholder items still marked active.

    Returns the list of rows touched (or that would be touched under DRY-RUN)
    for the caller to print/audit.
    """
    items = (
        session.query(PlaidItem)
        .filter(PlaidItem.status == "active", PlaidItem.item_id.like("placeholder_%"))
        .all()
    )
    if not apply:
        return items

    for item in items:
        item.access_token_encrypted = REVOKED_TOKEN_SENTINEL
        item.status = "disconnected"
        item.last_error = REASON
    session.commit()
    return items


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually disconnect the rows (default: dry-run, no writes).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    init_db()
    with SessionLocal() as session:
        items = prune_dead_items(session, apply=args.apply)

    mode = "APPLIED" if args.apply else "DRY-RUN"
    if not items:
        logger.info("%s: no dead placeholder items found", mode)
        return 0
    logger.info("%s: %d placeholder item(s)", mode, len(items))
    for item in items:
        logger.info(
            "  item_id=%s institution=%s status=%s",
            item.item_id,
            item.institution_name,
            "disconnected" if args.apply else item.status,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
