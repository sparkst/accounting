"""Tests for the shared write_ingestion_log helper (FIX-9).

Two contracts verified:
  1. Happy path: a real in-memory session produces one IngestionLog row with
     the expected field values.
  2. Swallow path: when the DB write itself raises (simulated via patching
     session.commit), write_ingestion_log returns normally without re-raising.
"""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from src.adapters._shared.ingestion import write_ingestion_log
from src.models.base import Base
from src.models.enums import IngestionStatus
from src.models.ingestion_log import IngestionLog


@pytest.fixture
def session() -> Generator[Session, None, None]:
    """Fresh in-memory SQLite session with FK enforcement."""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _):  # type: ignore[no-untyped-def]
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    SessionCls = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    s = SessionCls()
    yield s
    s.close()
    engine.dispose()


def test_write_ingestion_log_happy_path(session: Session) -> None:
    """write_ingestion_log inserts exactly one row with the supplied field values."""
    write_ingestion_log(
        session,
        source="test_adapter",
        records_processed=42,
        records_failed=3,
        status=IngestionStatus.PARTIAL_FAILURE,
        error_detail="something went wrong on row 7",
    )

    rows = session.query(IngestionLog).all()
    assert len(rows) == 1
    log = rows[0]
    assert log.source == "test_adapter"
    assert log.records_processed == 42
    assert log.records_failed == 3
    assert log.status == IngestionStatus.PARTIAL_FAILURE.value
    assert log.error_detail == "something went wrong on row 7"


def test_write_ingestion_log_swallows_commit_exception(session: Session) -> None:
    """If session.commit raises inside write_ingestion_log, the exception is
    suppressed — a logging failure must never mask the main import result."""
    with patch.object(session, "commit", side_effect=RuntimeError("db exploded")):
        # Must not raise.
        write_ingestion_log(
            session,
            source="test_adapter",
            records_processed=1,
            records_failed=0,
            status=IngestionStatus.SUCCESS,
        )
    # The function returned normally — test passes implicitly if no exception.
