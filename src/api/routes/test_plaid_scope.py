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


# ── exchange pushes the D1 account map for wealth-scope Items (P0-001) ──────


def test_exchange_wealth_scope_pushes_account_map(
    client: TestClient, db: Session, plaid_client_mock: MagicMock
) -> None:
    """A wealth-scope Item has no register /map-accounts step (B5) — exchange
    must push the D1 account↔plaid mapping itself, or a freshly re-linked
    Item's accounts can never resolve at A1/A2 (P0-001)."""
    _placeholder(db, nonce="wealth-map-nonce", scope="wealth")
    with patch("src.adapters.plaid_account_map.push_account_map") as mock_push:
        resp = _do_exchange(client, plaid_client_mock, "wealth-map-nonce")
    assert resp.status_code == 200, resp.text
    mock_push.assert_called_once()
    _, kwargs = mock_push.call_args
    assert kwargs["institution_name"] == "ETRADE"


def test_exchange_register_scope_never_pushes_account_map(
    client: TestClient, db: Session, plaid_client_mock: MagicMock
) -> None:
    """Register-scope Items keep the existing /map-accounts UI flow — the D1
    account map push is wealth-scope only."""
    _placeholder(db, nonce="reg-map-nonce", scope="register")
    with patch("src.adapters.plaid_account_map.push_account_map") as mock_push:
        resp = _do_exchange(client, plaid_client_mock, "reg-map-nonce")
    assert resp.status_code == 200, resp.text
    mock_push.assert_not_called()


def test_exchange_wealth_scope_account_map_failure_surfaced_on_response(
    client: TestClient, db: Session, plaid_client_mock: MagicMock
) -> None:
    """P1-002/P1-fnf: when push_account_map fails (returns None — e.g. a
    WealthClientError from a misconfigured WEALTH_API_BASE/WEALTH_INTERNAL_KEY),
    the exchange response must surface the failure rather than silently
    reporting success (the discard-and-hope pattern this fix closes)."""
    _placeholder(db, nonce="wealth-map-fail-nonce", scope="wealth")
    with patch("src.adapters.plaid_account_map.push_account_map", return_value=None):
        resp = _do_exchange(client, plaid_client_mock, "wealth-map-fail-nonce")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["account_map_pushed"] is False
    assert body["account_map_counts"]["failed"] == 1

    import src.models.ingestion_log as il_mod

    log = (
        db.query(il_mod.IngestionLog)
        .filter_by(source="wealth_cloud:plaid_account_map")
        .order_by(il_mod.IngestionLog.run_at.desc())
        .first()
    )
    assert log is not None
    assert log.status == "failure"


def test_exchange_wealth_scope_account_map_success_surfaced_on_response(
    client: TestClient, db: Session, plaid_client_mock: MagicMock
) -> None:
    """A clean account-map push surfaces account_map_pushed=True with counts,
    and writes a `success` IngestionLog row."""
    _placeholder(db, nonce="wealth-map-ok-nonce", scope="wealth")
    with patch(
        "src.adapters.plaid_account_map.push_account_map",
        return_value={"created": 1, "reattached": 0, "already_mapped": 0, "failed": 0},
    ):
        resp = _do_exchange(client, plaid_client_mock, "wealth-map-ok-nonce")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["account_map_pushed"] is True
    assert body["account_map_counts"]["created"] == 1

    import src.models.ingestion_log as il_mod

    log = (
        db.query(il_mod.IngestionLog)
        .filter_by(source="wealth_cloud:plaid_account_map")
        .order_by(il_mod.IngestionLog.run_at.desc())
        .first()
    )
    assert log is not None
    assert log.status == "success"


def test_exchange_account_map_conflicts_are_not_counted_as_processed(
    client: TestClient, db: Session, plaid_client_mock: MagicMock
) -> None:
    """P3-r3i: a `conflict` is an account the D1 endpoint refused to resolve
    (two rows share the broker+mask or the plaid_account_id). It must never be
    counted as processed — it goes in records_failed, drops the run to
    partial_failure, and clears account_map_pushed, because that account will
    not resolve at A1/A2 until an operator retires the duplicate."""
    _placeholder(db, nonce="wealth-map-conflict-nonce", scope="wealth")
    with patch(
        "src.adapters.plaid_account_map.push_account_map",
        return_value={
            "created": 1,
            "reattached": 0,
            "relinked": 0,
            "already_mapped": 0,
            "conflicts": 2,
            "failed": 0,
        },
    ):
        resp = _do_exchange(client, plaid_client_mock, "wealth-map-conflict-nonce")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["account_map_pushed"] is False
    assert body["account_map_counts"]["conflicts"] == 2

    import src.models.ingestion_log as il_mod

    log = (
        db.query(il_mod.IngestionLog)
        .filter_by(source="wealth_cloud:plaid_account_map")
        .order_by(il_mod.IngestionLog.run_at.desc())
        .first()
    )
    assert log is not None
    assert log.status == "partial_failure"
    assert log.records_processed == 1  # only the created row
    assert log.records_failed == 2  # the conflicts
    assert log.error_detail is not None and "conflicts=2" in log.error_detail


def test_exchange_account_map_all_conflicts_is_a_failure(
    client: TestClient, db: Session, plaid_client_mock: MagicMock
) -> None:
    """P3-r3i: when NOTHING resolved, the run is a hard failure, not partial."""
    _placeholder(db, nonce="wealth-map-allconflict-nonce", scope="wealth")
    with patch(
        "src.adapters.plaid_account_map.push_account_map",
        return_value={"created": 0, "already_mapped": 0, "conflicts": 3, "failed": 0},
    ):
        resp = _do_exchange(client, plaid_client_mock, "wealth-map-allconflict-nonce")
    assert resp.status_code == 200, resp.text
    assert resp.json()["account_map_pushed"] is False

    import src.models.ingestion_log as il_mod

    log = (
        db.query(il_mod.IngestionLog)
        .filter_by(source="wealth_cloud:plaid_account_map")
        .order_by(il_mod.IngestionLog.run_at.desc())
        .first()
    )
    assert log is not None
    assert log.status == "failure"
    assert log.records_processed == 0
    assert log.records_failed == 3


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


# ── map-accounts rejects wealth-scope items (P0-002 / P2-mac) ───────────────


def test_map_accounts_rejects_wealth_scope_item(client: TestClient, db: Session) -> None:
    """A wealth-scope Item must never gain register Account mappings — no
    Account rows, no payment_method stamps — via a direct map-accounts call.
    Mirrors the sync_transactions_now 409 guard shape."""
    item = PlaidItem(
        item_id="wealth_item_map",
        institution_id="ins_115616",
        institution_name="Vanguard",
        access_token_encrypted=encrypt_token("tok"),
        scope="wealth",
    )
    db.add(item)
    db.commit()

    resp = client.post(
        "/api/plaid/map-accounts",
        json={
            "item_id": item.id,
            "mappings": [
                {
                    "plaid_account_id": "p_acct_wealth",
                    "create_new": {
                        "broker": "vanguard",
                        "account_number": "1234",
                        "account_type": "brokerage",
                    },
                }
            ],
        },
    )
    assert resp.status_code == 409
    assert "wealth-scope" in resp.json()["detail"]

    # No Account row was created by the rejected call.
    assert db.query(Account).filter_by(plaid_item_id=item.id).count() == 0


def test_map_accounts_succeeds_for_register_scope_item(
    client: TestClient, db: Session
) -> None:
    item = PlaidItem(
        item_id="register_item_map",
        institution_id="ins_56",
        institution_name="Chase",
        access_token_encrypted=encrypt_token("tok"),
        scope="register",
    )
    db.add(item)
    db.commit()

    resp = client.post(
        "/api/plaid/map-accounts",
        json={
            "item_id": item.id,
            "mappings": [
                {
                    "plaid_account_id": "p_acct_register",
                    "create_new": {
                        "broker": "chase",
                        "account_number": "5678",
                        "account_type": "checking",
                    },
                }
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["mappings"][0]["plaid_account_id"] == "p_acct_register"


# ── relink preserves scope (P0-002) ─────────────────────────────────────────


def test_relink_preserves_wealth_scope(
    client: TestClient, db: Session, plaid_client_mock: MagicMock
) -> None:
    """Relinking a wealth-scope Item must report scope='wealth' in the
    LinkTokenResponse, not silently default back to 'register' — otherwise
    the UI would offer the register account-mapping step on the exact path
    the consolidation cutover relies on for re-linking migrated Items."""
    item = PlaidItem(
        item_id="wealth_item_relink",
        institution_id="ins_115616",
        institution_name="Vanguard",
        access_token_encrypted=encrypt_token("tok"),
        scope="wealth",
    )
    db.add(item)
    db.commit()

    resp = client.post(f"/api/plaid/relink/{item.id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["scope"] == "wealth"


def test_relink_preserves_register_scope(
    client: TestClient, db: Session, plaid_client_mock: MagicMock
) -> None:
    item = PlaidItem(
        item_id="register_item_relink",
        institution_id="ins_56",
        institution_name="Chase",
        access_token_encrypted=encrypt_token("tok"),
        scope="register",
    )
    db.add(item)
    db.commit()

    resp = client.post(f"/api/plaid/relink/{item.id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["scope"] == "register"
