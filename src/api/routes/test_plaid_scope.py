"""Tests for REQ-PC-B5: per-link scope on the Plaid lifecycle routes.

- POST /link-token accepts an optional body {"scope": "register"|"wealth"};
  the scope is stored on the placeholder row and echoed in the response.
- POST /exchange promotes the placeholder KEEPING its scope and echoes it so
  the UI can skip the register account-mapping step for wealth links.
- GET /items surfaces each Item's scope.
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
from src.models.brokerage import Account  # noqa: F401 — registers FK target
from src.models.history import HistoricalPrice  # noqa: F401
from src.models.plaid import PlaidItem
from src.utils.plaid_crypto import encrypt_token

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


@pytest.fixture(autouse=True)
def _set_enc_key(monkeypatch: pytest.MonkeyPatch) -> str:
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("PLAID_TOKEN_ENC_KEY", key)
    return key


@pytest.fixture(autouse=True)
def clean_db() -> Generator[None, None, None]:
    # The app import (TestClient) registers more models on Base.metadata than
    # this module imports up-front — create any tables that appeared since.
    Base.metadata.create_all(bind=_test_engine)
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


@pytest.fixture
def plaid_client_mock() -> MagicMock:
    fake = MagicMock()
    fake.link_token_create.return_value = SimpleNamespace(link_token="link-sandbox-xyz")
    return fake


@pytest.fixture
def client(plaid_client_mock: MagicMock) -> Generator[TestClient, None, None]:
    import src.api.routes.plaid as plaid_routes_mod

    with (
        patch.object(plaid_routes_mod, "SessionLocal", _TestSession),
        patch.object(plaid_routes_mod, "_get_plaid_client", return_value=plaid_client_mock),
        patch("src.api.main.init_db", return_value=None),
        patch("src.api.main.seed_vendor_rules", return_value=0),
        patch("src.api.main.seed_customers", return_value={
            "customers_inserted": 0, "customers_updated": 0, "invoices_inserted": 0,
        }),
    ):
        from src.api.main import app

        with TestClient(app) as c:
            yield c


def _placeholder(db: Session, *, nonce: str, scope: str = "register") -> PlaidItem:
    ph = PlaidItem(
        item_id=f"placeholder_{uuid.uuid4().hex[:12]}",
        institution_id="pending",
        institution_name="pending",
        access_token_encrypted="REVOKED",
        state_nonce=nonce,
        state_nonce_expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=10),
        scope=scope,
    )
    db.add(ph)
    db.commit()
    return ph


# ── link-token scope ─────────────────────────────────────────────────────────


def test_link_token_default_scope_is_register(client: TestClient, db: Session) -> None:
    resp = client.post("/api/plaid/link-token", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["scope"] == "register"
    ph = db.query(PlaidItem).filter(PlaidItem.item_id.like("placeholder_%")).one()
    assert ph.scope == "register"


def test_link_token_no_body_defaults_to_register(client: TestClient, db: Session) -> None:
    resp = client.post("/api/plaid/link-token")
    assert resp.status_code == 200, resp.text
    assert resp.json()["scope"] == "register"


def test_link_token_wealth_scope_stored_on_placeholder(
    client: TestClient, db: Session
) -> None:
    resp = client.post("/api/plaid/link-token", json={"scope": "wealth"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["scope"] == "wealth"
    ph = db.query(PlaidItem).filter(PlaidItem.item_id.like("placeholder_%")).one()
    assert ph.scope == "wealth"


def test_link_token_rejects_invalid_scope(client: TestClient) -> None:
    resp = client.post("/api/plaid/link-token", json={"scope": "both"})
    assert resp.status_code == 422


# ── exchange keeps the scope ─────────────────────────────────────────────────


def _do_exchange(client: TestClient, plaid_client_mock: MagicMock, nonce: str) -> Any:
    plaid_client_mock.item_public_token_exchange.return_value = SimpleNamespace(
        access_token="access-sandbox-secret", item_id=f"item_{nonce}"
    )
    plaid_client_mock.accounts_get.return_value = SimpleNamespace(
        accounts=[
            SimpleNamespace(
                to_dict=lambda: {
                    "account_id": "p_acct_a",
                    "type": "investment",
                    "balances": {"current": 100.0, "iso_currency_code": "USD"},
                }
            )
        ]
    )
    return client.post(
        "/api/plaid/exchange",
        json={
            "public_token": "pt-test",
            "state_nonce": nonce,
            "institution_id": "ins_129473",
            "institution_name": "ETRADE",
        },
    )


def test_exchange_promotes_wealth_placeholder_keeping_scope(
    client: TestClient, db: Session, plaid_client_mock: MagicMock
) -> None:
    ph = _placeholder(db, nonce="wealth-nonce", scope="wealth")
    resp = _do_exchange(client, plaid_client_mock, "wealth-nonce")
    assert resp.status_code == 200, resp.text
    assert resp.json()["scope"] == "wealth"
    db2 = _TestSession()
    promoted = db2.query(PlaidItem).filter_by(id=ph.id).one()
    assert promoted.scope == "wealth"
    assert promoted.item_id == "item_wealth-nonce"
    db2.close()


def test_exchange_register_placeholder_echoes_register(
    client: TestClient, db: Session, plaid_client_mock: MagicMock
) -> None:
    _placeholder(db, nonce="reg-nonce", scope="register")
    resp = _do_exchange(client, plaid_client_mock, "reg-nonce")
    assert resp.status_code == 200, resp.text
    assert resp.json()["scope"] == "register"


# ── items listing surfaces scope ─────────────────────────────────────────────


def test_items_listing_includes_scope(client: TestClient, db: Session) -> None:
    db.add(
        PlaidItem(
            item_id="real_wealth_item",
            institution_id="ins_115616",
            institution_name="Vanguard",
            access_token_encrypted=encrypt_token("tok"),
            scope="wealth",
        )
    )
    db.add(
        PlaidItem(
            item_id="real_register_item",
            institution_id="ins_56",
            institution_name="Chase",
            access_token_encrypted=encrypt_token("tok"),
        )
    )
    db.commit()

    resp = client.get("/api/plaid/items")
    assert resp.status_code == 200
    scopes = {row["institution_name"]: row["scope"] for row in resp.json()}
    assert scopes == {"Vanguard": "wealth", "Chase": "register"}


# ── manual sync-transactions endpoint rejects wealth-scope items ─────────────


def test_manual_sync_transactions_rejects_wealth_scope(
    client: TestClient, db: Session
) -> None:
    """Contract-check fix: the batch path filters scope=='register', but the
    manual POST /items/{id}/sync-transactions could still reach a wealth-scope
    Item. It must 409 before the rate-limit stamp (no cooldown consumed)."""
    item = PlaidItem(
        item_id="wealth_item_manual",
        institution_id="ins_115616",
        institution_name="Vanguard",
        access_token_encrypted=encrypt_token("tok"),
        scope="wealth",
    )
    db.add(item)
    db.commit()

    resp = client.post(f"/api/plaid/items/{item.id}/sync-transactions")
    assert resp.status_code == 409
    assert "wealth-scope" in resp.json()["detail"]

    # 409 must not consume the cooldown: a second call still 409s (not 429).
    resp2 = client.post(f"/api/plaid/items/{item.id}/sync-transactions")
    assert resp2.status_code == 409
