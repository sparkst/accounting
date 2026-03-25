"""Tests for API key authentication (S1-009).

REQ-ID: S1-009-01  With API_KEY set, requests without a key get 401.
REQ-ID: S1-009-02  With API_KEY set, requests with the correct key get 200.
REQ-ID: S1-009-03  With API_KEY set, requests with the wrong key get 401.
REQ-ID: S1-009-04  Without API_KEY set, all requests pass (dev mode).
REQ-ID: S1-009-05  Health endpoint is always accessible, even when API_KEY is set.
REQ-ID: S1-009-06  API key accepted via X-Api-Key header.
REQ-ID: S1-009-07  API key accepted via api_key query parameter.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

import src.db.connection as _conn  # noqa: F401  — registers ORM models on Base
from src.models.base import Base
from src.models.enums import Direction, Entity, Source, TaxCategory, TransactionStatus
from src.models.transaction import Transaction

# ---------------------------------------------------------------------------
# In-memory test database
# ---------------------------------------------------------------------------

_TEST_DB_URI = "file:auth_test?mode=memory&cache=shared&uri=true"

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
    with _test_engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
        conn.execute(text("PRAGMA foreign_keys=ON"))
    yield


# ---------------------------------------------------------------------------
# App client fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    """TestClient wired to the in-memory test DB."""
    from src.api import main as _main_module
    from src.api.routes import health as _health_module
    from src.api.routes import transactions as _tx_module

    with (
        patch.object(_tx_module, "SessionLocal", _TestSession),
        patch.object(_health_module, "SessionLocal", _TestSession),
        patch.object(_main_module, "init_db", return_value=None),
        patch.object(_main_module, "seed_vendor_rules", return_value=0),
        patch.object(_main_module, "seed_customers", return_value={
            "customers_inserted": 0, "customers_updated": 0, "invoices_inserted": 0
        }),
    ):
        from src.api.main import app

        with TestClient(app, raise_server_exceptions=True) as c:
            yield c


# ---------------------------------------------------------------------------
# Helper — insert a minimal transaction
# ---------------------------------------------------------------------------


def _insert_tx(session_factory: Any) -> str:
    tx_id = str(uuid.uuid4())
    with session_factory() as session:
        tx = Transaction(
            id=tx_id,
            date="2025-01-15",
            description="Test vendor",
            amount=Decimal("50.00"),
            direction=Direction.EXPENSE.value,
            source=Source.BANK_CSV.value,
            entity=Entity.SPARKRY.value,
            tax_category=TaxCategory.SOFTWARE.value,
            status=TransactionStatus.NEEDS_REVIEW.value,
            raw_data="{}",
        )
        session.add(tx)
        session.commit()
    return tx_id


# ---------------------------------------------------------------------------
# Tests: API_KEY not set (dev mode — all requests pass)
# ---------------------------------------------------------------------------


def test_no_api_key_env_all_requests_pass(client: TestClient) -> None:
    """REQ-ID: S1-009-04 — Without API_KEY, every request is allowed."""
    with patch.dict("os.environ", {}, clear=False):
        # Ensure API_KEY is absent
        import os
        os.environ.pop("API_KEY", None)

        resp = client.get("/api/transactions")
        assert resp.status_code == 200, resp.text


def test_no_api_key_env_health_passes(client: TestClient) -> None:
    """Health is accessible in dev mode too."""
    import os
    os.environ.pop("API_KEY", None)

    resp = client.get("/api/health")
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Tests: API_KEY set
# ---------------------------------------------------------------------------


def test_with_api_key_set_no_key_in_request_gets_401(client: TestClient) -> None:
    """REQ-ID: S1-009-01 — Missing key → 401."""
    with patch.dict("os.environ", {"API_KEY": "secret-test-key"}):
        resp = client.get("/api/transactions")
        assert resp.status_code == 401, resp.text


def test_with_api_key_set_wrong_key_gets_401(client: TestClient) -> None:
    """REQ-ID: S1-009-03 — Wrong key → 401."""
    with patch.dict("os.environ", {"API_KEY": "secret-test-key"}):
        resp = client.get("/api/transactions", headers={"X-Api-Key": "wrong-key"})
        assert resp.status_code == 401, resp.text


def test_with_api_key_set_correct_header_gets_200(client: TestClient) -> None:
    """REQ-ID: S1-009-02 & S1-009-06 — Correct key via X-Api-Key header → 200."""
    with patch.dict("os.environ", {"API_KEY": "secret-test-key"}):
        resp = client.get("/api/transactions", headers={"X-Api-Key": "secret-test-key"})
        assert resp.status_code == 200, resp.text


def test_with_api_key_set_correct_query_param_gets_200(client: TestClient) -> None:
    """REQ-ID: S1-009-07 — Correct key via api_key query param → 200."""
    with patch.dict("os.environ", {"API_KEY": "secret-test-key"}):
        resp = client.get("/api/transactions?api_key=secret-test-key")
        assert resp.status_code == 200, resp.text


def test_health_always_accessible_with_api_key_set(client: TestClient) -> None:
    """REQ-ID: S1-009-05 — Health endpoint bypasses auth entirely."""
    with patch.dict("os.environ", {"API_KEY": "secret-test-key"}):
        # No key supplied at all
        resp = client.get("/api/health")
        assert resp.status_code == 200, resp.text


def test_health_source_config_always_accessible_with_api_key_set(client: TestClient) -> None:
    """Health sub-routes also bypass auth."""
    with patch.dict("os.environ", {"API_KEY": "secret-test-key"}):
        resp = client.get("/api/health/source-config")
        assert resp.status_code == 200, resp.text
