"""Tests for REQ-HM-007: redirect_uri injected into Plaid LinkTokenCreateRequest.

Positive:  PLAID_ENV=production + PLAID_REDIRECT_URI set → both create_link_token
           and relink_item send redirect_uri to the Plaid SDK.
Negative:  neither env var set → redirect_uri absent (local/sandbox unaffected).
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import src.models.ingested_file  # noqa: F401
import src.models.ingestion_log  # noqa: F401
import src.models.invoice  # noqa: F401
import src.models.llm_usage  # noqa: F401
import src.models.tax_document  # noqa: F401
import src.models.tax_year_lock  # noqa: F401
import src.models.transaction  # noqa: F401
from src.models.base import Base
from src.models.brokerage import Account
from src.models.history import HistoricalPrice  # noqa: F401
from src.models.plaid import PlaidItem
from src.utils.plaid_crypto import encrypt_token

_REDIRECT_URI = "https://books.sparkry.ai/admin/connections/oauth-return"

# ── In-memory SQLite (same pattern as test_plaid_routes.py) ──────────────────

_test_engine = create_engine(
    "sqlite+pysqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)


@event.listens_for(_test_engine, "connect")
def _set_pragmas(conn: Any, _record: Any) -> None:
    cursor = conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


Base.metadata.create_all(bind=_test_engine)
_TestSession = sessionmaker(bind=_test_engine, autocommit=False, autoflush=False)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _set_enc_key(monkeypatch: pytest.MonkeyPatch) -> str:
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("PLAID_TOKEN_ENC_KEY", key)
    return key


@pytest.fixture(autouse=True)
def clean_db() -> Generator[None, None, None]:
    with _test_engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
        conn.execute(text("PRAGMA foreign_keys=ON"))
    yield


@pytest.fixture
def db() -> Generator[Session, None, None]:
    session = _TestSession()
    try:
        yield session
    finally:
        session.close()


def _make_client_fixture(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, dict[str, Any]]:
    """Build a TestClient with a capturing Plaid mock. Returns (client, captured)."""
    import src.api.routes.plaid as plaid_mod

    captured: dict[str, Any] = {}

    fake = MagicMock()

    def _create(req: Any) -> SimpleNamespace:
        captured["req"] = req
        r = MagicMock()
        r.link_token = "link-sandbox-xyz"
        return r

    fake.link_token_create.side_effect = _create

    with (
        patch.object(plaid_mod, "SessionLocal", _TestSession),
        patch.object(plaid_mod, "_get_plaid_client", return_value=fake),
        patch("src.api.main.init_db", return_value=None),
        patch("src.api.main.seed_vendor_rules", return_value=0),
        patch("src.api.main.seed_customers", return_value={
            "customers_inserted": 0, "customers_updated": 0, "invoices_inserted": 0,
        }),
    ):
        from src.api.main import app

        with TestClient(app) as c:
            yield c, captured


# ── Helper to create a real PlaidItem in the test DB ─────────────────────────


def _make_item(db: Session, *, institution_name: str = "Chase") -> PlaidItem:
    item = PlaidItem(
        item_id=f"plaid_real_{uuid.uuid4().hex[:8]}",
        institution_id="ins_3",
        institution_name=institution_name,
        access_token_encrypted=encrypt_token("access-sandbox-real"),
        status="active",
    )
    db.add(item)
    db.commit()
    return item


# ── Positive: create_link_token sends redirect_uri ────────────────────────────


def test_create_link_token_sends_redirect_uri_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REQ-HM-007: create_link_token sends redirect_uri when PLAID_REDIRECT_URI is set."""
    monkeypatch.setenv("PLAID_ENV", "production")
    monkeypatch.setenv("PLAID_REDIRECT_URI", _REDIRECT_URI)

    import src.api.routes.plaid as plaid_mod

    captured: dict[str, Any] = {}
    fake = MagicMock()

    def _create(req: Any) -> SimpleNamespace:
        captured["req"] = req
        r = MagicMock()
        r.link_token = "link-sandbox-xyz"
        return r

    fake.link_token_create.side_effect = _create

    with (
        patch.object(plaid_mod, "SessionLocal", _TestSession),
        patch.object(plaid_mod, "_get_plaid_client", return_value=fake),
        patch("src.api.main.init_db", return_value=None),
        patch("src.api.main.seed_vendor_rules", return_value=0),
        patch("src.api.main.seed_customers", return_value={
            "customers_inserted": 0, "customers_updated": 0, "invoices_inserted": 0,
        }),
        # assert_production_secrets() is imported locally inside lifespan; patch
        # at its module so PLAID_ENV=production doesn't demand real keys in tests.
        patch("src.api._startup_assert.assert_production_secrets", return_value=None),
    ):
        from src.api.main import app

        with TestClient(app) as c:
            resp = c.post("/api/plaid/link-token", json={})

    assert resp.status_code == 200, resp.text
    assert "req" in captured, "link_token_create was never called"
    req = captured["req"]
    payload = req.to_dict()
    # redirect_uri must be present and correct.
    assert payload.get("redirect_uri") == _REDIRECT_URI, (
        f"Expected redirect_uri={_REDIRECT_URI!r}, got {payload.get('redirect_uri')!r}"
    )


# ── Positive: relink_item sends redirect_uri ──────────────────────────────────


def test_relink_item_sends_redirect_uri_in_production(
    monkeypatch: pytest.MonkeyPatch,
    db: Session,
) -> None:
    """REQ-HM-007: relink_item sends redirect_uri when PLAID_REDIRECT_URI is set."""
    monkeypatch.setenv("PLAID_ENV", "production")
    monkeypatch.setenv("PLAID_REDIRECT_URI", _REDIRECT_URI)

    item = _make_item(db)

    import src.api.routes.plaid as plaid_mod

    captured: dict[str, Any] = {}
    fake = MagicMock()

    def _create(req: Any) -> SimpleNamespace:
        captured["req"] = req
        r = MagicMock()
        r.link_token = "link-update-mode-xyz"
        return r

    fake.link_token_create.side_effect = _create

    with (
        patch.object(plaid_mod, "SessionLocal", _TestSession),
        patch.object(plaid_mod, "_get_plaid_client", return_value=fake),
        patch("src.api.main.init_db", return_value=None),
        patch("src.api.main.seed_vendor_rules", return_value=0),
        patch("src.api.main.seed_customers", return_value={
            "customers_inserted": 0, "customers_updated": 0, "invoices_inserted": 0,
        }),
        patch("src.api._startup_assert.assert_production_secrets", return_value=None),
    ):
        from src.api.main import app

        with TestClient(app) as c:
            resp = c.post(f"/api/plaid/relink/{item.id}")

    assert resp.status_code == 200, resp.text
    assert "req" in captured, "link_token_create was never called"
    req = captured["req"]
    payload = req.to_dict()
    assert payload.get("redirect_uri") == _REDIRECT_URI, (
        f"Expected redirect_uri={_REDIRECT_URI!r}, got {payload.get('redirect_uri')!r}"
    )


# ── Positive: PLAID_REDIRECT_URI set without PLAID_ENV=production ─────────────


def test_redirect_uri_sent_when_env_var_set_regardless_of_plaid_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PLAID_REDIRECT_URI alone (no PLAID_ENV=production) is enough to send redirect_uri.

    Useful for staging environments that want OAuth but are not production.
    """
    monkeypatch.delenv("PLAID_ENV", raising=False)
    monkeypatch.setenv("PLAID_REDIRECT_URI", _REDIRECT_URI)

    import src.api.routes.plaid as plaid_mod

    captured: dict[str, Any] = {}
    fake = MagicMock()

    def _create(req: Any) -> SimpleNamespace:
        captured["req"] = req
        r = MagicMock()
        r.link_token = "link-sandbox-xyz"
        return r

    fake.link_token_create.side_effect = _create

    with (
        patch.object(plaid_mod, "SessionLocal", _TestSession),
        patch.object(plaid_mod, "_get_plaid_client", return_value=fake),
        patch("src.api.main.init_db", return_value=None),
        patch("src.api.main.seed_vendor_rules", return_value=0),
        patch("src.api.main.seed_customers", return_value={
            "customers_inserted": 0, "customers_updated": 0, "invoices_inserted": 0,
        }),
    ):
        from src.api.main import app

        with TestClient(app) as c:
            resp = c.post("/api/plaid/link-token", json={})

    assert resp.status_code == 200, resp.text
    req = captured["req"]
    payload = req.to_dict()
    assert payload.get("redirect_uri") == _REDIRECT_URI


# ── Negative: no env vars → no redirect_uri (sandbox/local unaffected) ────────


def test_create_link_token_omits_redirect_uri_in_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REQ-HM-007 negative: without PLAID_REDIRECT_URI, redirect_uri must be absent."""
    monkeypatch.delenv("PLAID_ENV", raising=False)
    monkeypatch.delenv("PLAID_REDIRECT_URI", raising=False)

    import src.api.routes.plaid as plaid_mod

    captured: dict[str, Any] = {}
    fake = MagicMock()

    def _create(req: Any) -> SimpleNamespace:
        captured["req"] = req
        r = MagicMock()
        r.link_token = "link-sandbox-no-redirect"
        return r

    fake.link_token_create.side_effect = _create

    with (
        patch.object(plaid_mod, "SessionLocal", _TestSession),
        patch.object(plaid_mod, "_get_plaid_client", return_value=fake),
        patch("src.api.main.init_db", return_value=None),
        patch("src.api.main.seed_vendor_rules", return_value=0),
        patch("src.api.main.seed_customers", return_value={
            "customers_inserted": 0, "customers_updated": 0, "invoices_inserted": 0,
        }),
    ):
        from src.api.main import app

        with TestClient(app) as c:
            resp = c.post("/api/plaid/link-token", json={})

    assert resp.status_code == 200, resp.text
    req = captured["req"]
    payload = req.to_dict()
    # redirect_uri should not be set — absent from dict or explicitly None.
    redirect = payload.get("redirect_uri")
    assert redirect is None, (
        f"Expected no redirect_uri in sandbox, got {redirect!r}"
    )


def test_relink_item_omits_redirect_uri_in_sandbox(
    monkeypatch: pytest.MonkeyPatch,
    db: Session,
) -> None:
    """REQ-HM-007 negative: relink_item also omits redirect_uri when env var unset."""
    monkeypatch.delenv("PLAID_ENV", raising=False)
    monkeypatch.delenv("PLAID_REDIRECT_URI", raising=False)

    item = _make_item(db)

    import src.api.routes.plaid as plaid_mod

    captured: dict[str, Any] = {}
    fake = MagicMock()

    def _create(req: Any) -> SimpleNamespace:
        captured["req"] = req
        r = MagicMock()
        r.link_token = "link-sandbox-no-redirect"
        return r

    fake.link_token_create.side_effect = _create

    with (
        patch.object(plaid_mod, "SessionLocal", _TestSession),
        patch.object(plaid_mod, "_get_plaid_client", return_value=fake),
        patch("src.api.main.init_db", return_value=None),
        patch("src.api.main.seed_vendor_rules", return_value=0),
        patch("src.api.main.seed_customers", return_value={
            "customers_inserted": 0, "customers_updated": 0, "invoices_inserted": 0,
        }),
    ):
        from src.api.main import app

        with TestClient(app) as c:
            resp = c.post(f"/api/plaid/relink/{item.id}")

    assert resp.status_code == 200, resp.text
    req = captured["req"]
    payload = req.to_dict()
    redirect = payload.get("redirect_uri")
    assert redirect is None, (
        f"Expected no redirect_uri in sandbox relink, got {redirect!r}"
    )
