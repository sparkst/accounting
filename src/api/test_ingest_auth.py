"""§7: /api/ingest/* accepts INGEST_API_KEY only — NOT the browser API_KEY.

REQ-ID: A7-001  Browser API_KEY must NOT drive /api/ingest/run (401).
REQ-ID: A7-002  INGEST_API_KEY must drive /api/ingest/run (auth passes; any non-401 status).
REQ-ID: A7-003  INGEST_API_KEY must NOT drive browser routes like /api/transactions (401).
"""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    """TestClient with API_KEY='k'*32 and INGEST_API_KEY='i'*32.

    The auth dependencies call os.environ.get() at request time, so
    monkeypatch.setenv is sufficient — no module reload needed.
    The ingest _run_ingest_locked is patched to a no-op so the test runs fast
    and purely validates auth routing, not adapter behaviour.
    """
    monkeypatch.setenv("API_KEY", "k" * 32)
    monkeypatch.setenv("INGEST_API_KEY", "i" * 32)

    import src.db.connection as _conn  # noqa: F401 — registers ORM models on Base
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    from src.models.base import Base

    _db_uri = "file:ingest_auth_test?mode=memory&cache=shared&uri=true"
    _engine = create_engine(
        "sqlite+pysqlite:///" + _db_uri.replace("file:", ""),
        connect_args={"check_same_thread": False, "uri": True},
    )
    Base.metadata.create_all(bind=_engine)
    _Session = sessionmaker(bind=_engine, autocommit=False, autoflush=False)

    with _engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
        conn.execute(text("PRAGMA foreign_keys=ON"))

    from src.api import main as _main_module
    from src.api.routes import health as _health_module
    from src.api.routes import ingest as _ingest_module
    from src.api.routes import transactions as _tx_module
    from src.api.routes.ingest import IngestSummary

    _fast_summary = IngestSummary(
        ingested_count=0,
        classified_count=0,
        needs_review_count=0,
        adapter_results=[],
        warnings=[],
        errors=[],
    )

    with (
        patch.object(_tx_module, "SessionLocal", _Session),
        patch.object(_health_module, "SessionLocal", _Session),
        patch.object(_ingest_module, "SessionLocal", _Session),
        patch.object(_main_module, "init_db", return_value=None),
        patch.object(_main_module, "seed_vendor_rules", return_value=0),
        patch.object(
            _main_module,
            "seed_customers",
            return_value={
                "customers_inserted": 0,
                "customers_updated": 0,
                "invoices_inserted": 0,
            },
        ),
        # Short-circuit actual ingest work — auth validation happens before
        # _run_ingest_locked is called, so this doesn't affect auth assertions.
        patch.object(_ingest_module, "_run_ingest_locked", return_value=_fast_summary),
    ):
        from src.api.main import app

        with TestClient(app, raise_server_exceptions=False) as c:
            yield c


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_browser_key_rejected_on_ingest(client: TestClient) -> None:
    """REQ-ID: A7-001 — API_KEY (browser key) must be rejected on /api/ingest/run."""
    r = client.post("/api/ingest/run", headers={"X-Api-Key": "k" * 32}, json={})
    assert r.status_code == 401, (
        f"Expected 401, got {r.status_code}: {r.text}. "
        "The browser API_KEY must NOT drive ingest."
    )


def test_ingest_key_accepted_on_ingest(client: TestClient) -> None:
    """REQ-ID: A7-002 — INGEST_API_KEY must pass auth on /api/ingest/run (non-401 response)."""
    r = client.post("/api/ingest/run", headers={"X-Api-Key": "i" * 32}, json={})
    assert r.status_code != 401, (
        f"Expected auth to pass (non-401), got {r.status_code}: {r.text}. "
        "The INGEST_API_KEY must be accepted on ingest."
    )


def test_ingest_key_rejected_on_browser_route(client: TestClient) -> None:
    """REQ-ID: A7-003 — INGEST_API_KEY must be rejected on /api/transactions (browser route)."""
    r = client.get("/api/transactions", headers={"X-Api-Key": "i" * 32})
    assert r.status_code == 401, (
        f"Expected 401, got {r.status_code}: {r.text}. "
        "The INGEST_API_KEY must NOT drive browser routes."
    )
