"""Alembic environment configuration.

Imports all SQLAlchemy models so autogenerate can detect schema changes.
Reads the database URL from the DATABASE_PATH env var (same logic as
src/db/connection.py) so the same override mechanism works for tests.
"""

import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# ---------------------------------------------------------------------------
# Import all models so their tables are registered on Base.metadata before
# autogenerate inspects it.  Mirror the imports in src/db/connection.py.
# ---------------------------------------------------------------------------
from src.models.audit_event import AuditEvent  # noqa: F401
from src.models.base import Base
from src.models.ingested_file import IngestedFile  # noqa: F401
from src.models.ingestion_log import IngestionLog  # noqa: F401
from src.models.invoice import Customer, Invoice, InvoiceLineItem  # noqa: F401
from src.models.llm_usage import LLMUsageLog  # noqa: F401
from src.models.tax_year_lock import TaxYearLock  # noqa: F401
from src.models.transaction import Transaction  # noqa: F401
from src.models.vendor_rule import VendorRule  # noqa: F401

# Alembic Config object — provides access to values within alembic.ini.
config = context.config

# Set up Python logging from the ini file.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Tell autogenerate which metadata to compare against.
target_metadata = Base.metadata

# ---------------------------------------------------------------------------
# Allow DATABASE_PATH env var to override the URL in alembic.ini, matching the
# same pattern used by src/db/connection.py.
# ---------------------------------------------------------------------------
_db_path = os.getenv("DATABASE_PATH", "data/accounting.db")
_db_url = f"sqlite:///{_db_path}"
config.set_main_option("sqlalchemy.url", _db_url)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL to stdout, no live DB)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite doesn't support ALTER TABLE DROP COLUMN in older versions;
        # render_as_batch wraps changes in a table-rebuild approach.
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (live DB connection)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # render_as_batch is required for SQLite column alterations.
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
