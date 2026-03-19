"""Migration: add 1099 payer tracking columns to transactions.

Adds two columns:
  - payer_1099       TEXT(255) — Payer name as it appears on the 1099 form
  - payer_1099_type  TEXT(8)   — Form type: NEC | K | MISC

Usage::

    python -m src.db.migrate_1099_fields

Safe to run multiple times (checks if column already exists before ALTER TABLE).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = "data/accounting.db"

_NEW_COLUMNS = [
    ("payer_1099", "TEXT(255)"),
    ("payer_1099_type", "TEXT(8)"),
]


def migrate(db_path: str = _DEFAULT_DB_PATH) -> None:
    """Add 1099 payer fields to the transactions table (idempotent)."""
    if not Path(db_path).exists():
        logger.info("Database not found at %s — nothing to migrate.", db_path)
        return

    conn = sqlite3.connect(db_path)

    # Get existing columns
    cursor = conn.execute("PRAGMA table_info(transactions)")
    existing_cols = {row[1] for row in cursor.fetchall()}

    added = 0
    for col_name, col_type in _NEW_COLUMNS:
        if col_name in existing_cols:
            logger.info("Column %s already exists — skipping.", col_name)
            continue
        sql = f"ALTER TABLE transactions ADD COLUMN {col_name} {col_type}"
        conn.execute(sql)
        logger.info("Added column: %s %s", col_name, col_type)
        added += 1

    conn.commit()
    conn.close()

    if added:
        logger.info("Migration complete — added %d column(s).", added)
    else:
        logger.info("All columns already present — nothing to do.")


def main() -> None:
    """CLI entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    db_path = os.getenv("DATABASE_PATH", _DEFAULT_DB_PATH)
    try:
        migrate(db_path)
    except Exception as exc:
        logger.error("Migration failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
