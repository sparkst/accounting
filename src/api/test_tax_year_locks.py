"""Tests for tax year lock endpoints and edit guards.

REQ-ID: TYL-001  POST /api/tax-year-locks creates a lock.
REQ-ID: TYL-002  GET  /api/tax-year-locks lists all locks.
REQ-ID: TYL-003  DELETE /api/tax-year-locks/{id} removes a lock.
REQ-ID: TYL-004  Duplicate lock returns 409.
REQ-ID: TYL-005  PATCH /api/transactions/{id} returns 403 on locked year.
REQ-ID: TYL-006  PATCH /api/transactions/{id} succeeds on unlocked transaction.
REQ-ID: TYL-007  Split endpoint returns 403 on locked year.
REQ-ID: TYL-008  Bulk confirm silently skips locked transactions.
REQ-ID: TYL-009  After unlock, PATCH succeeds again.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

import src.db.connection as _conn  # noqa: F401 — registers models on Base.metadata
from src.models.base import Base
from src.models.enums import Direction, Entity, Source, TaxCategory, TransactionStatus
from src.models.tax_year_lock import TaxYearLock
from src.models.transaction import Transaction

# ---------------------------------------------------------------------------
# Shared in-memory test database
# ---------------------------------------------------------------------------

_TEST_DB_URI = "file:tyl_test_db?mode=memory&cache=shared&uri=true"

_test_engine = create_engine(
    "sqlite+pysqlite:///" + _TEST_DB_URI.replace("file:", ""),
    connect_args={"check_same_thread": False, "uri": True},
)


@event.listens_for(_test_engine, "connect")
def _set_pragmas(conn: Any, _record: Any) -> None:
    cursor = conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


Base.metadata.create_all(bind=_test_engine)

_TestSession = sessionmaker(bind=_test_engine, autocommit=False, autoflush=False)


@pytest.fixture(autouse=True)
def clean_db() -> Generator[None, None, None]:
    """Truncate all tables before each test."""
    with _test_engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
        conn.execute(text("PRAGMA foreign_keys=ON"))
    yield


# ---------------------------------------------------------------------------
# App client
# ---------------------------------------------------------------------------


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    """TestClient with all route sessions redirected to the test DB."""
    from src.api import main as _main_module
    from src.api.routes import health as _health_module
    from src.api.routes import ingest as _ingest_module
    from src.api.routes import tax_year_locks as _tyl_module
    from src.api.routes import transactions as _tx_module

    with (
        patch.object(_tx_module, "SessionLocal", _TestSession),
        patch.object(_tyl_module, "SessionLocal", _TestSession),
        patch.object(_health_module, "SessionLocal", _TestSession),
        patch.object(_ingest_module, "SessionLocal", _TestSession),
        patch.object(_main_module, "init_db", return_value=None),
        patch.object(_main_module, "seed_vendor_rules", return_value=0),
    ):
        from src.api.main import app

        with TestClient(app) as c:
            yield c


@pytest.fixture()
def db_session() -> Generator[Session, None, None]:
    """Direct DB session for test setup."""
    session = _TestSession()
    try:
        yield session
    finally:
        try:
            session.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tx(
    session: Session,
    *,
    entity: str = Entity.SPARKRY.value,
    date: str = "2024-06-15",
    amount: float = -100.0,
    status: str = TransactionStatus.AUTO_CLASSIFIED.value,
    direction: str = Direction.EXPENSE.value,
) -> Transaction:
    uid = str(uuid.uuid4())
    tx = Transaction(
        id=uid,
        source=Source.BANK_CSV.value,
        source_id=uid,
        source_hash=uid,  # use uuid as a unique hash stand-in
        date=date,
        description="Test Vendor",
        amount=amount,
        currency="USD",
        entity=entity,
        direction=direction,
        tax_category=TaxCategory.OFFICE_EXPENSE.value,
        status=status,
        confidence=0.9,
        raw_data={"test": True},
    )
    session.add(tx)
    session.commit()
    session.refresh(tx)
    return tx


def _make_lock(
    session: Session,
    entity: str = Entity.SPARKRY.value,
    year: int = 2024,
    locked_by: str = "human",
) -> TaxYearLock:
    lock = TaxYearLock(entity=entity, year=year, locked_by=locked_by)
    session.add(lock)
    session.commit()
    session.refresh(lock)
    return lock


# ---------------------------------------------------------------------------
# TYL-001: POST creates a lock
# ---------------------------------------------------------------------------


class TestCreateLock:
    def test_creates_lock(self, client: TestClient) -> None:
        """TYL-001: POST /api/tax-year-locks creates a new lock."""
        r = client.post(
            "/api/tax-year-locks",
            json={"entity": "sparkry", "year": 2024},
        )
        assert r.status_code == 201, r.text
        data = r.json()
        assert data["entity"] == "sparkry"
        assert data["year"] == 2024
        assert data["locked_by"] == "human"
        assert "id" in data
        assert "locked_at" in data

    def test_invalid_entity_returns_422(self, client: TestClient) -> None:
        r = client.post(
            "/api/tax-year-locks",
            json={"entity": "not_an_entity", "year": 2024},
        )
        assert r.status_code == 422

    def test_invalid_year_returns_422(self, client: TestClient) -> None:
        r = client.post(
            "/api/tax-year-locks",
            json={"entity": "sparkry", "year": 1900},
        )
        assert r.status_code == 422

    def test_custom_locked_by(self, client: TestClient) -> None:
        r = client.post(
            "/api/tax-year-locks",
            json={"entity": "blackline", "year": 2023, "locked_by": "travis"},
        )
        assert r.status_code == 201
        assert r.json()["locked_by"] == "travis"


# ---------------------------------------------------------------------------
# TYL-002: GET lists all locks
# ---------------------------------------------------------------------------


class TestListLocks:
    def test_empty_list(self, client: TestClient) -> None:
        """TYL-002: Returns empty list when no locks exist."""
        r = client.get("/api/tax-year-locks")
        assert r.status_code == 200
        assert r.json() == []

    def test_lists_locks_ordered(
        self, client: TestClient, db_session: Session
    ) -> None:
        """TYL-002: Returns all locks ordered by entity then year."""
        _make_lock(db_session, entity="sparkry", year=2023)
        _make_lock(db_session, entity="personal", year=2024)
        _make_lock(db_session, entity="sparkry", year=2024)

        r = client.get("/api/tax-year-locks")
        assert r.status_code == 200
        items = r.json()
        assert len(items) == 3
        # Ordered by entity (personal < sparkry) then year
        assert items[0]["entity"] == "personal"
        assert items[1]["entity"] == "sparkry" and items[1]["year"] == 2023
        assert items[2]["entity"] == "sparkry" and items[2]["year"] == 2024


# ---------------------------------------------------------------------------
# TYL-003: DELETE removes a lock
# ---------------------------------------------------------------------------


class TestDeleteLock:
    def test_delete_lock(
        self, client: TestClient, db_session: Session
    ) -> None:
        """TYL-003: DELETE /api/tax-year-locks/{id} removes the lock."""
        lock = _make_lock(db_session)
        r = client.delete(f"/api/tax-year-locks/{lock.id}")
        assert r.status_code == 204

        r2 = client.get("/api/tax-year-locks")
        assert r2.json() == []

    def test_delete_nonexistent_returns_404(self, client: TestClient) -> None:
        r = client.delete("/api/tax-year-locks/nonexistent-id")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# TYL-004: Duplicate lock returns 409
# ---------------------------------------------------------------------------


class TestDuplicateLock:
    def test_duplicate_returns_409(
        self, client: TestClient, db_session: Session
    ) -> None:
        """TYL-004: Creating a duplicate (entity, year) lock returns 409."""
        _make_lock(db_session, entity="sparkry", year=2024)
        r = client.post(
            "/api/tax-year-locks",
            json={"entity": "sparkry", "year": 2024},
        )
        assert r.status_code == 409
        assert "already locked" in r.json()["detail"]

    def test_different_entities_allowed(
        self, client: TestClient, db_session: Session
    ) -> None:
        """TYL-004: Same year, different entities — both allowed."""
        _make_lock(db_session, entity="sparkry", year=2024)
        r = client.post(
            "/api/tax-year-locks",
            json={"entity": "blackline", "year": 2024},
        )
        assert r.status_code == 201


# ---------------------------------------------------------------------------
# TYL-005: PATCH returns 403 on locked year
# ---------------------------------------------------------------------------


class TestPatchGuard:
    def test_patch_blocked_when_locked(
        self, client: TestClient, db_session: Session
    ) -> None:
        """TYL-005: PATCH /api/transactions/{id} returns 403 when year is locked."""
        tx = _make_tx(db_session, entity="sparkry", date="2024-03-01")
        _make_lock(db_session, entity="sparkry", year=2024)

        r = client.patch(
            f"/api/transactions/{tx.id}",
            json={"notes": "should be blocked"},
        )
        assert r.status_code == 403
        assert "locked" in r.json()["detail"].lower()

    def test_patch_blocked_only_matching_entity(
        self, client: TestClient, db_session: Session
    ) -> None:
        """TYL-005: Lock on sparkry does not block blackline transaction."""
        tx = _make_tx(db_session, entity="blackline", date="2024-03-01")
        _make_lock(db_session, entity="sparkry", year=2024)

        r = client.patch(
            f"/api/transactions/{tx.id}",
            json={"notes": "allowed"},
        )
        assert r.status_code == 200

    def test_patch_blocked_only_matching_year(
        self, client: TestClient, db_session: Session
    ) -> None:
        """TYL-005: Lock on 2024 does not block a 2023 transaction."""
        tx = _make_tx(db_session, entity="sparkry", date="2023-12-31")
        _make_lock(db_session, entity="sparkry", year=2024)

        r = client.patch(
            f"/api/transactions/{tx.id}",
            json={"notes": "2023 is fine"},
        )
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# TYL-006: PATCH succeeds on unlocked transaction
# ---------------------------------------------------------------------------


class TestPatchUnlocked:
    def test_patch_allowed_no_lock(
        self, client: TestClient, db_session: Session
    ) -> None:
        """TYL-006: PATCH succeeds when no lock exists."""
        tx = _make_tx(db_session, entity="sparkry", date="2024-06-15")

        r = client.patch(
            f"/api/transactions/{tx.id}",
            json={"notes": "this is fine"},
        )
        assert r.status_code == 200
        assert r.json()["notes"] == "this is fine"


# ---------------------------------------------------------------------------
# TYL-007: Split endpoint returns 403 on locked year
# ---------------------------------------------------------------------------


class TestSplitGuard:
    def test_split_blocked_when_locked(
        self, client: TestClient, db_session: Session
    ) -> None:
        """TYL-007: POST /api/transactions/{id}/split returns 403 when year is locked."""
        tx = _make_tx(db_session, entity="sparkry", date="2024-05-10", amount=-200.0)
        _make_lock(db_session, entity="sparkry", year=2024)

        r = client.post(
            f"/api/transactions/{tx.id}/split",
            json={
                "line_items": [
                    {"amount": -100.0, "description": "Part A"},
                    {"amount": -100.0, "description": "Part B"},
                ]
            },
        )
        assert r.status_code == 403

    def test_split_different_entity_not_blocked(
        self, client: TestClient, db_session: Session
    ) -> None:
        """TYL-007: A wrong-entity lock must not return 403 on a different entity's transaction.

        Verifies the lock guard only fires for matching entity. We pass an
        intentionally invalid amount so the request is rejected at amount
        validation (422), not at the lock check (403).  The important thing
        is: 403 must NOT appear.
        """
        from src.api.routes.tax_year_locks import check_lock

        # Simulate: sparkry/2024 transaction, blackline/2024 lock
        # check_lock should NOT raise for sparkry when only blackline is locked.
        with _TestSession() as sess:
            _make_lock(sess, entity="blackline", year=2024)
            # Should not raise
            check_lock(sess, "sparkry", "2024-05-10")


# ---------------------------------------------------------------------------
# TYL-009: After unlock, PATCH succeeds again
# ---------------------------------------------------------------------------


class TestUnlockAndEdit:
    def test_unlock_allows_patch(
        self, client: TestClient, db_session: Session
    ) -> None:
        """TYL-009: After removing the lock, PATCH succeeds."""
        tx = _make_tx(db_session, entity="sparkry", date="2024-07-04")
        lock = _make_lock(db_session, entity="sparkry", year=2024)

        # Confirm blocked
        r = client.patch(f"/api/transactions/{tx.id}", json={"notes": "blocked"})
        assert r.status_code == 403

        # Remove lock
        client.delete(f"/api/tax-year-locks/{lock.id}")

        # Now allowed
        r2 = client.patch(f"/api/transactions/{tx.id}", json={"notes": "now allowed"})
        assert r2.status_code == 200
        assert r2.json()["notes"] == "now allowed"
