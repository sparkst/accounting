"""Alembic environment configuration.

Imports all SQLAlchemy models so autogenerate can detect schema changes.
Reads the database URL from the DATABASE_PATH env var (same logic as
src/db/connection.py) so the same override mechanism works for tests.
"""

import logging
import os
import sys
from logging.config import fileConfig

import sqlalchemy as sa
from alembic import context
from sqlalchemy import engine_from_config, pool

from src.alerts.models import AlertDispatch  # noqa: F401

# ---------------------------------------------------------------------------
# Import all models so their tables are registered on Base.metadata before
# autogenerate inspects it.  Mirror the imports in src/db/connection.py.
# ---------------------------------------------------------------------------
from src.models.ar_reminder import ArReminder  # noqa: F401
from src.models.audit_event import AuditEvent  # noqa: F401
from src.models.base import Base
from src.models.brokerage import (  # noqa: F401
    Account,
    BrokerageTransaction,
    PositionSnapshot,
    RealizedGainLoss,
)
from src.models.history import (  # noqa: F401
    AccountBalanceSnapshot,
    AccountTag,
    CostBasisLot,
    ExpectedAccount,
    HistoricalPrice,
)
from src.models.ingested_file import IngestedFile  # noqa: F401
from src.models.ingestion_log import IngestionLog  # noqa: F401
from src.models.invoice import Customer, Invoice, InvoiceLineItem  # noqa: F401
from src.models.llm_usage import LLMUsageLog  # noqa: F401
from src.models.plaid import PlaidAccountBalanceSnapshot, PlaidItem  # noqa: F401
from src.models.tax_document import TaxDocument  # noqa: F401
from src.models.tax_year_lock import TaxYearLock  # noqa: F401
from src.models.transaction import Transaction  # noqa: F401
from src.models.vendor_rule import VendorRule  # noqa: F401
from src.models.vision_promotion import VisionPromotion  # noqa: F401
from src.planning.models import PlanningRun  # noqa: F401

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


def _bootstrap_fresh_db(connection: sa.Connection) -> None:
    """Build the current schema directly and stamp `alembic_version` at head.

    This project's migration chain (starting at `1c8d9ab67214_initial_schema`)
    was authored against a database that already existed via
    `Base.metadata.create_all()` (the app's real bootstrap path — see
    `init_db()` in `src/db/connection.py`), so its early revisions are
    ALTER-only and raise `NoSuchTableError` if replayed against a genuinely
    empty database (P0-mig001). Rather than retrofitting every historical
    migration to be existence-guarded, mirror the app's own bootstrap
    semantics here: for a database with zero tables, create the full current
    schema in one shot and stamp it at head, instead of replaying decades of
    ALTER-only history that assumes tables already exist. Databases that
    already have tables (including ones this repo did not create, e.g. a
    restored backup) are unaffected — they take the normal migration path
    below.

    P2-r3e narrows this to ``upgrade head`` alone (see ``_is_upgrade_to_head``)
    so every other command still replays real history against a fresh DB.

    P2-004: this does NOT actually retrofit the broken migration chain — a
    genuinely-broken migration is invisible to a fresh-DB `upgrade head`,
    because that path skips migration replay entirely. It is also a new
    footgun: SQLite creates the file on connect, so a typo'd or mis-Doppler'd
    ``DATABASE_PATH`` used to fail loudly (``NoSuchTableError``) and would now
    silently create and stamp a brand-new empty database — against a project
    whose first Critical Rule is "SQLite is the single source of truth". Make
    an accidental empty-DB bootstrap during deploy unmissable: log a loud
    WARNING always, and require an explicit ``ALEMBIC_BOOTSTRAP_EMPTY=1`` env
    var opt-in unless the target is an in-memory/test database (identified by
    ``ALEMBIC_BOOTSTRAP_EMPTY`` being unset AND ``DATABASE_PATH`` unset/absent
    — i.e. tests that never set DATABASE_PATH keep working with zero config
    changes; a real deploy pointed at a fresh path must opt in explicitly).
    """
    from alembic.script import ScriptDirectory

    opted_in = os.getenv("ALEMBIC_BOOTSTRAP_EMPTY") == "1"
    is_explicit_db_path = bool(os.getenv("DATABASE_PATH"))
    logging.getLogger("alembic.env").warning(
        "Target database has ZERO tables — bootstrapping the FULL current "
        "schema via Base.metadata.create_all() and stamping alembic_version "
        "at head, INSTEAD of replaying migration history (P0-mig001/P2-004). "
        "If this DATABASE_PATH was supposed to point at an existing database, "
        "STOP: this is about to silently create a brand-new empty one."
    )
    if is_explicit_db_path and not opted_in:
        print(
            "ALEMBIC BOOTSTRAP REFUSED: DATABASE_PATH is set to a path with "
            "zero tables. Set ALEMBIC_BOOTSTRAP_EMPTY=1 to confirm you really "
            "want to create a brand-new empty database at this path (P2-004) "
            "— otherwise this is very likely a mis-set/typo'd DATABASE_PATH.",
            file=sys.stderr,
        )
        raise SystemExit(
            "refusing to bootstrap an empty database without "
            "ALEMBIC_BOOTSTRAP_EMPTY=1 (DATABASE_PATH is set)"
        )

    target_metadata.create_all(bind=connection)
    connection.execute(
        sa.text(
            "CREATE TABLE IF NOT EXISTS alembic_version ("
            "version_num VARCHAR(32) NOT NULL, "
            "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
        )
    )
    connection.execute(sa.text("DELETE FROM alembic_version"))
    script = ScriptDirectory.from_config(config)
    for head in script.get_heads():
        connection.execute(
            sa.text("INSERT INTO alembic_version (version_num) VALUES (:v)"),
            {"v": head},
        )
    connection.commit()


def _is_upgrade_to_head() -> bool:
    """True only for an ``upgrade`` whose destination is the script head(s).

    P2-r3e: the zero-table bootstrap shortcut is a fast path for exactly one
    workflow — standing up a brand-new database at the current schema. Any
    other target (``upgrade <specific_rev>``, ``downgrade``, and read-only
    commands like ``current``/``history``, which pass no destination at all)
    must follow the real migration history, so a genuinely broken revision
    stays reachable and diagnosable instead of being masked by create_all().

    ``get_revision_argument()`` resolves ``head``/``heads`` to the concrete
    revision id(s), so an explicit head-revision argument is treated the same
    as the symbolic one.
    """
    from alembic.script import ScriptDirectory

    # `fn` is the per-command migration callable Alembic installs on the
    # environment context; its name is the only signal distinguishing
    # `upgrade` from `stamp`, which shares the same destination argument.
    # Best-effort: if Alembic ever renames it, fall back to allowing the
    # revision check alone to decide.
    fn = getattr(context, "context_opts", {}).get("fn")
    if fn is not None and getattr(fn, "__name__", "upgrade") != "upgrade":
        return False

    try:
        destination = context.get_revision_argument()
    except Exception:
        # No destination revision in the context opts (e.g. `alembic current`).
        return False
    if destination is None:  # 'base', or nothing requested
        return False

    requested = {destination} if isinstance(destination, str) else set(destination)
    heads = set(ScriptDirectory.from_config(config).get_heads())
    return bool(requested) and requested <= heads


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (live DB connection)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    # Inspect via the ENGINE (its own short-lived connection), not the
    # connection we are about to hand to `context.configure()` below.
    # Calling `inspect()` on that connection first triggers SQLAlchemy 2.x
    # autobegin on it before Alembic's own `begin_transaction()` runs,
    # which silently defeats Alembic's commit-at-the-end bookkeeping for
    # batch_alter_table's table-recreate dance (columns/constraints appear
    # to apply with no error, then vanish — the transaction rolls back on
    # connection close instead of committing). Keep reflection and the
    # migration connection fully separate.
    # P2-r3e: bootstrap ONLY for `upgrade head` against a zero-table database.
    # Everything else — a specific target revision, a downgrade, `current`,
    # `history` — takes the normal path below, whatever it finds there.
    if not sa.inspect(connectable).get_table_names() and _is_upgrade_to_head():
        with connectable.connect() as connection:
            _bootstrap_fresh_db(connection)
        return

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
