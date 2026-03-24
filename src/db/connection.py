"""SQLAlchemy engine, session factory, and database initialisation.

Design decisions:
- Synchronous SQLAlchemy (not async) — SQLite is single-user local, no benefit
  to async complexity.
- ``check_same_thread=False`` required for SQLite with multi-threaded FastAPI.
- WAL journal mode for better concurrent read performance.
- ``data/accounting.db`` is the production path; tests override DATABASE_PATH.
"""

import os
import sqlite3
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from src.models.audit_event import AuditEvent  # noqa: F401

# Import all models so their tables are registered on Base.metadata before
# init_db() calls create_all().
from src.models.base import Base
from src.models.ingested_file import IngestedFile  # noqa: F401
from src.models.ingestion_log import IngestionLog  # noqa: F401
from src.models.invoice import Customer, Invoice, InvoiceLineItem  # noqa: F401
from src.models.llm_usage import LLMUsageLog  # noqa: F401
from src.models.tax_year_lock import TaxYearLock  # noqa: F401
from src.models.transaction import Transaction  # noqa: F401
from src.models.vendor_rule import VendorRule  # noqa: F401

load_dotenv()

_DEFAULT_DB_PATH = "data/accounting.db"


def _get_database_url() -> str:
    """Build the SQLite URL from DATABASE_PATH env var, falling back to default."""
    db_path = os.getenv("DATABASE_PATH", _DEFAULT_DB_PATH)
    return f"sqlite:///{db_path}"


def _configure_sqlite(
    dbapi_connection: sqlite3.Connection,
    connection_record: object,  # noqa: ARG001
) -> None:
    """Apply SQLite PRAGMAs on every new connection."""
    # WAL mode: readers don't block writers; better for dashboard + background jobs.
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA synchronous=NORMAL")
    # Give concurrent writers up to 5 seconds before raising "database is locked".
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def _build_engine(database_url: str | None = None) -> Engine:
    url = database_url or _get_database_url()
    eng = create_engine(
        url,
        connect_args={"check_same_thread": False},
        echo=False,
    )
    event.listen(eng, "connect", _configure_sqlite)
    return eng


engine: Engine = _build_engine()

SessionLocal: sessionmaker[Session] = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
)


def get_session() -> Session:
    """Return a new Session. Caller is responsible for closing it.

    Intended for use with a context manager::

        with get_session() as session:
            ...
    """
    return SessionLocal()


def init_db(database_url: str | None = None) -> None:
    """Create all tables if they do not already exist.

    Ensures the parent directory of the SQLite file exists before attempting
    to create the database. Safe to call multiple times (idempotent).

    Args:
        database_url: Override the URL (used by tests to pass an in-memory DB).
    """
    target_engine = engine if database_url is None else _build_engine(database_url)

    # Ensure the data directory exists for file-based SQLite.
    if not target_engine.url.database or target_engine.url.database in (":memory:", ""):
        pass  # In-memory or blank — no directory needed.
    else:
        db_file = Path(target_engine.url.database)
        db_file.parent.mkdir(parents=True, exist_ok=True)

    Base.metadata.create_all(bind=target_engine)

    # Verify WAL mode is active.
    with target_engine.connect() as conn:
        result = conn.execute(text("PRAGMA journal_mode")).scalar()
        if result != "wal":
            conn.execute(text("PRAGMA journal_mode=WAL"))
            conn.commit()

    # Enforce "never delete transactions" rule at the DB level.
    # S1-007: A BEFORE DELETE trigger raises ABORT so no DELETE on the
    # transactions table can succeed — callers must use status=rejected instead.
    with target_engine.connect() as conn:
        conn.execute(
            text(
                """
                CREATE TRIGGER IF NOT EXISTS prevent_transaction_delete
                BEFORE DELETE ON transactions
                BEGIN
                    SELECT RAISE(ABORT, 'Transactions cannot be deleted. Use status=rejected instead.');
                END;
                """
            )
        )
        conn.commit()
