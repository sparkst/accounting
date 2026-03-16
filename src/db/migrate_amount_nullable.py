"""One-time migration: make transactions.amount nullable.

SQLite does not support ALTER COLUMN to remove NOT NULL constraints.
This script recreates the transactions table with amount as nullable.

Usage::

    python -m src.db.migrate_amount_nullable

Safe to run multiple times (checks for constraint before migrating).
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = "data/accounting.db"


def migrate(db_path: str = _DEFAULT_DB_PATH) -> None:
    """Recreate transactions table with amount as nullable.

    Strategy: rename existing table, create new table with nullable amount,
    copy all rows, drop the old table.
    """
    if not Path(db_path).exists():
        logger.info("Database not found at %s — nothing to migrate.", db_path)
        return

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=OFF")

    # Check whether amount is already nullable by inspecting table_info.
    cursor = conn.execute("PRAGMA table_info(transactions)")
    cols = cursor.fetchall()
    amount_col = next((c for c in cols if c[1] == "amount"), None)
    if amount_col is None:
        conn.close()
        logger.error("No 'amount' column found in transactions table.")
        return

    # col format: (cid, name, type, notnull, dflt_value, pk)
    if amount_col[3] == 0:
        conn.close()
        logger.info("amount column is already nullable — nothing to migrate.")
        return

    logger.info("Migrating transactions.amount to nullable …")

    try:
        conn.execute("BEGIN EXCLUSIVE")

        # Rename existing table.
        conn.execute("ALTER TABLE transactions RENAME TO _transactions_old")

        # Create new table with nullable amount.
        conn.execute("""
            CREATE TABLE transactions (
                id                  TEXT(36)       NOT NULL PRIMARY KEY,
                source              TEXT(32)       NOT NULL,
                source_id           TEXT(255),
                source_hash         TEXT(64)       NOT NULL UNIQUE,
                date                TEXT(10)       NOT NULL,
                description         TEXT           NOT NULL,
                amount              NUMERIC(12, 2),
                currency            TEXT(3)        NOT NULL DEFAULT 'USD',
                entity              TEXT(16),
                direction           TEXT(16),
                tax_category        TEXT(32),
                tax_subcategory     TEXT(32),
                deductible_pct      REAL           NOT NULL DEFAULT 1.0,
                status              TEXT(24)       NOT NULL DEFAULT 'needs_review',
                confidence          REAL           NOT NULL DEFAULT 0.0,
                review_reason       TEXT,
                parent_id           TEXT(36)       REFERENCES transactions(id),
                reimbursement_link  TEXT(36)       REFERENCES transactions(id),
                attachments         TEXT,
                raw_data            TEXT           NOT NULL,
                created_at          TEXT           NOT NULL,
                updated_at          TEXT           NOT NULL,
                confirmed_by        TEXT(8)        NOT NULL DEFAULT 'auto',
                notes               TEXT
            )
        """)

        # Copy all rows.
        conn.execute("""
            INSERT INTO transactions
            SELECT * FROM _transactions_old
        """)

        # Recreate indexes.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_transactions_entity ON transactions(entity)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_transactions_tax_category ON transactions(tax_category)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_transactions_status ON transactions(status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_transactions_source_hash ON transactions(source_hash)"
        )

        # Drop old table.
        conn.execute("DROP TABLE _transactions_old")

        conn.execute("COMMIT")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.close()

        logger.info("Migration complete — transactions.amount is now nullable.")

    except Exception as exc:
        conn.execute("ROLLBACK")
        conn.close()
        raise RuntimeError(f"Migration failed: {exc}") from exc


def main() -> None:
    """CLI entry point."""
    import os

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
