"""Migration helper: create the tax_year_locks table if it does not exist.

Run directly::

    python -m src.db.migrate_tax_year_lock

Safe to run multiple times (idempotent via ``create_all(checkfirst=True)``).
"""

from __future__ import annotations

import logging

from src.db.connection import engine
from src.models.tax_year_lock import TaxYearLock  # noqa: F401 — registers on Base

logger = logging.getLogger(__name__)


def migrate() -> None:
    """Create the tax_year_locks table on the current engine if absent."""
    from src.models.base import Base

    Base.metadata.create_all(bind=engine, checkfirst=True)
    logger.info("tax_year_locks table ensured.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    migrate()
    print("Migration complete: tax_year_locks table is ready.")
