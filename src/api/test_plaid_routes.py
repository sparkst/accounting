"""Tests for src/api/routes/plaid.py.

Covers:
- REQ-025: link → exchange → map → disconnect lifecycle; CSRF state nonce
  required and validated; auth required on every endpoint.
- REQ-028: /reconciliation/summary returns expected shape and threshold flag.
- REQ-029: AuditEvent rows written with ``entity_type`` for lifecycle events.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import src.models.ingested_file  # noqa: E402,F401
import src.models.ingestion_log  # noqa: E402,F401
import src.models.invoice  # noqa: E402,F401
import src.models.llm_usage  # noqa: E402,F401
import src.models.tax_document  # noqa: E402,F401
import src.models.tax_year_lock  # noqa: E402,F401

# Force-register every model on Base.metadata so create_all builds the full schema.
# (The route module only imports a subset; the test DB needs everything that the
# auto-clean fixture sees in Base.metadata.sorted_tables.)
import src.models.transaction  # noqa: E402,F401
from src.models.audit_event import AuditEvent
from src.models.base import Base
from src.models.brokerage import Account, PositionSnapshot
from src.models.history import HistoricalPrice  # noqa: F401 — register on metadata
from src.models.plaid import PlaidAccountBalanceSnapshot, PlaidItem
from src.utils.plaid_crypto import encrypt_token

# In-memory SQLite via StaticPool — every connection sees the same in-memory DB
# without the closed-database-when-last-conn-dies issue that bites shared-cache.
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
    """A pre-wired Plaid SDK client mock that the route module uses everywhere."""
    return MagicMock()


@pytest.fixture
def client(plaid_client_mock: MagicMock) -> Generator[TestClient, None, None]:
    """FastAPI TestClient with the Plaid client patched + Session patched + auth disabled."""
    import src.api.routes.plaid as plaid_routes_mod

    with (
        patch.object(plaid_routes_mod, "SessionLocal", _TestSession),
        patch.object(plaid_routes_mod, "_get_plaid_client", return_value=plaid_client_mock),
        # Disable startup hooks; we don't need them.
        patch("src.api.main.init_db", return_value=None),
        patch("src.api.main.seed_vendor_rules", return_value=0),
        patch("src.api.main.seed_customers", return_value={
            "customers_inserted": 0, "customers_updated": 0, "invoices_inserted": 0,
        }),
    ):
        # No API_KEY set → auth bypassed. Auth-required tests set API_KEY explicitly.
        from src.api.main import app

        with TestClient(app) as c:
            yield c


@pytest.fixture
def auth_client(plaid_client_mock: MagicMock, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    """TestClient with API_KEY set — for auth-required assertions."""
    monkeypatch.setenv("API_KEY", "test-key-abc")
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


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_item(
    db: Session,
    *,
    institution_name: str = "Chase",
    item_id: str | None = None,
    status: str = "active",
) -> PlaidItem:
    item = PlaidItem(
        item_id=item_id or f"plaid_real_{uuid.uuid4().hex[:8]}",
        institution_id="ins_3",
        institution_name=institution_name,
        access_token_encrypted=encrypt_token("access-sandbox-real"),
        status=status,
    )
    db.add(item)
    db.commit()
    return item


def _make_account(
    db: Session,
    *,
    item: PlaidItem | None = None,
    plaid_account_id: str | None = None,
    account_number: str | None = None,
) -> Account:
    acct = Account(
        broker="schwab",
        account_number=account_number or f"a-{uuid.uuid4().hex[:6]}",
        account_name="Test Account",
        account_type="taxable",
        entity="personal",
        plaid_item_id=item.id if item else None,
        plaid_account_id=plaid_account_id,
    )
    db.add(acct)
    db.commit()
    return acct


# ── Auth required (REQ-025) ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/plaid/link-token"),
        ("POST", "/api/plaid/exchange"),
        ("POST", "/api/plaid/map-accounts"),
        ("POST", "/api/plaid/disconnect/some-id"),
        ("POST", "/api/plaid/relink/some-id"),
        ("GET", "/api/plaid/items"),
        ("POST", "/api/plaid/sync-now"),
        ("GET", "/api/plaid/reconciliation/summary"),
        ("POST", "/api/plaid/items/some-id/sync-transactions"),
    ],
)
def test_auth_required_on_every_endpoint(
    auth_client: TestClient, method: str, path: str
) -> None:
    """REQ-025: every /api/plaid/* endpoint rejects unauthenticated requests."""
    resp = auth_client.request(method, path, json={})
    assert resp.status_code == 401, f"{method} {path} expected 401, got {resp.status_code}"


def test_auth_accepted_with_correct_key(auth_client: TestClient, plaid_client_mock: MagicMock) -> None:
    """Same endpoint with X-Api-Key header succeeds (smoke-test of auth path)."""
    resp = auth_client.get("/api/plaid/items", headers={"X-Api-Key": "test-key-abc"})
    assert resp.status_code == 200


# ── link-token & state nonce CSRF ────────────────────────────────────────────


def test_link_token_creates_placeholder_with_state_nonce(
    client: TestClient, db: Session, plaid_client_mock: MagicMock
) -> None:
    plaid_client_mock.link_token_create.return_value = SimpleNamespace(
        link_token="link-sandbox-mock-token"
    )
    resp = client.post("/api/plaid/link-token", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["link_token"] == "link-sandbox-mock-token"
    assert len(body["state_nonce"]) >= 32  # token_urlsafe(32) → 43+ chars
    # Placeholder row exists, keyed by nonce.
    ph = db.query(PlaidItem).filter_by(state_nonce=body["state_nonce"]).one()
    assert ph.access_token_encrypted == "REVOKED"
    assert ph.item_id.startswith("placeholder_")


def test_link_token_request_omits_invalid_balance_product(
    client: TestClient, plaid_client_mock: MagicMock
) -> None:
    """REQ-025: ``balance`` must NOT be sent in ``required_if_supported_products``.

    Plaid rejects it with HTTP 400 INVALID_PRODUCT — ``balance`` auto-initializes
    whenever any other valid product (here ``transactions``) is requested. The
    request-shape was never asserted before, so the real-API 400 slipped past the
    mocked tests. Regression guard for the link-token 500.
    """
    plaid_client_mock.link_token_create.return_value = SimpleNamespace(
        link_token="link-token-x"
    )
    resp = client.post("/api/plaid/link-token", json={})
    assert resp.status_code == 200
    req = plaid_client_mock.link_token_create.call_args[0][0]
    payload = req.to_dict()
    products = [str(p) for p in payload.get("products", [])]
    assert "transactions" in products
    req_if = [str(p) for p in (payload.get("required_if_supported_products") or [])]
    assert "balance" not in req_if


def test_exchange_rejects_missing_state_nonce(client: TestClient) -> None:
    """REQ-025: /exchange MUST validate state_nonce. Empty nonce → 422 (Pydantic)."""
    resp = client.post(
        "/api/plaid/exchange",
        json={"public_token": "pt", "state_nonce": "", "institution_id": "ins_3", "institution_name": "Chase"},
    )
    assert resp.status_code == 422


def test_exchange_rejects_unknown_state_nonce(client: TestClient) -> None:
    """A nonce we never issued → 400."""
    resp = client.post(
        "/api/plaid/exchange",
        json={
            "public_token": "pt",
            "state_nonce": "totally-made-up-nonce",
            "institution_id": "ins_3",
            "institution_name": "Chase",
        },
    )
    assert resp.status_code == 400
    assert "state_nonce" in resp.json()["detail"]


def test_exchange_rejects_expired_state_nonce(client: TestClient, db: Session) -> None:
    """A nonce whose expiry passed → 400 (no replay after 30 min)."""
    ph = PlaidItem(
        item_id="placeholder_expired",
        institution_id="pending",
        institution_name="pending",
        access_token_encrypted="REVOKED",
        state_nonce="expired-nonce",
        state_nonce_expires_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1),
    )
    db.add(ph)
    db.commit()
    resp = client.post(
        "/api/plaid/exchange",
        json={
            "public_token": "pt",
            "state_nonce": "expired-nonce",
            "institution_id": "ins_3",
            "institution_name": "Chase",
        },
    )
    assert resp.status_code == 400


def test_exchange_happy_path_promotes_placeholder_and_writes_audit(
    client: TestClient, db: Session, plaid_client_mock: MagicMock
) -> None:
    """REQ-025 + REQ-029: successful exchange writes encrypted token + AuditEvent."""
    # Set up a valid placeholder.
    nonce = "valid-nonce-for-exchange"
    ph = PlaidItem(
        item_id="placeholder_valid",
        institution_id="pending",
        institution_name="pending",
        access_token_encrypted="REVOKED",
        state_nonce=nonce,
        state_nonce_expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=10),
    )
    db.add(ph)
    db.commit()
    ph_id = ph.id

    # Plaid SDK returns access_token + item_id.
    plaid_client_mock.item_public_token_exchange.return_value = SimpleNamespace(
        access_token="access-sandbox-real-secret", item_id="plaid_item_real_chase_xyz"
    )
    plaid_client_mock.accounts_get.return_value = SimpleNamespace(
        accounts=[
            SimpleNamespace(
                to_dict=lambda: {
                    "account_id": "p_acct_a",
                    "mask": "1234",
                    "name": "Chase Checking",
                    "type": "depository",
                    "subtype": "checking",
                    "balances": {"current": 100, "available": 100, "iso_currency_code": "USD"},
                }
            )
        ]
    )

    resp = client.post(
        "/api/plaid/exchange",
        json={
            "public_token": "pt-test",
            "state_nonce": nonce,
            "institution_id": "ins_3",
            "institution_name": "Chase",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["item_id"] == "plaid_item_real_chase_xyz"
    assert body["plaid_item_id"] == ph_id
    assert len(body["accounts"]) == 1

    # DB state: token encrypted, nonce cleared, audit row written.
    db2 = _TestSession()
    promoted = db2.query(PlaidItem).filter_by(id=ph_id).one()
    assert promoted.item_id == "plaid_item_real_chase_xyz"
    assert promoted.institution_name == "Chase"
    assert promoted.state_nonce is None
    assert promoted.access_token_encrypted != "REVOKED"
    assert promoted.access_token_encrypted != "access-sandbox-real-secret"  # encrypted
    audit = db2.query(AuditEvent).filter_by(entity_id=ph_id, entity_type="plaid_item").all()
    assert any(a.field_changed == "connect" for a in audit)


def test_exchange_rejects_duplicate_item_id(
    client: TestClient, db: Session, plaid_client_mock: MagicMock
) -> None:
    """If the exchanged item_id already exists in DB → 409 (must /relink instead)."""
    # Existing Item with the same item_id.
    _make_item(db, institution_name="Chase", item_id="plaid_item_existing")
    # Placeholder waiting to be exchanged.
    nonce = "dup-nonce"
    ph = PlaidItem(
        item_id="placeholder_dup",
        institution_id="pending",
        institution_name="pending",
        access_token_encrypted="REVOKED",
        state_nonce=nonce,
        state_nonce_expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=10),
    )
    db.add(ph)
    db.commit()

    plaid_client_mock.item_public_token_exchange.return_value = SimpleNamespace(
        access_token="x", item_id="plaid_item_existing"
    )
    resp = client.post(
        "/api/plaid/exchange",
        json={
            "public_token": "pt-test",
            "state_nonce": nonce,
            "institution_id": "ins_3",
            "institution_name": "Chase",
        },
    )
    assert resp.status_code == 409


# ── Disconnect (REQ-025) ─────────────────────────────────────────────────────


def test_disconnect_overwrites_token_and_unmaps_accounts(
    client: TestClient, db: Session, plaid_client_mock: MagicMock
) -> None:
    """REQ-025: /disconnect zeros encrypted token to ``REVOKED`` and nulls FK on Account."""
    item = _make_item(db)
    acct = _make_account(db, item=item, plaid_account_id="p_acct_a")
    assert item.access_token_encrypted != "REVOKED"

    plaid_client_mock.item_remove.return_value = SimpleNamespace(request_id="ok")
    resp = client.post(f"/api/plaid/disconnect/{item.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "disconnected"
    assert body["accounts_unmapped"] == 1
    assert body["plaid_remove_called"] is True

    db2 = _TestSession()
    item_after = db2.query(PlaidItem).filter_by(id=item.id).one()
    assert item_after.status == "disconnected"
    assert item_after.access_token_encrypted == "REVOKED"  # SENTINEL written
    acct_after = db2.query(Account).filter_by(id=acct.id).one()
    assert acct_after.plaid_item_id is None
    assert acct_after.plaid_account_id is None
    # AuditEvent rows written for both account and plaid_item.
    audits = db2.query(AuditEvent).filter(AuditEvent.entity_type.isnot(None)).all()
    by_type = {a.entity_type for a in audits}
    assert "plaid_item" in by_type
    assert "account" in by_type


def test_disconnect_idempotent_on_already_disconnected(
    client: TestClient, db: Session
) -> None:
    item = _make_item(db, status="disconnected")
    resp = client.post(f"/api/plaid/disconnect/{item.id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "already_disconnected"


def test_disconnect_404_on_unknown_item(client: TestClient) -> None:
    resp = client.post("/api/plaid/disconnect/nonexistent-uuid")
    assert resp.status_code == 404


# ── Map-accounts (REQ-029) ───────────────────────────────────────────────────


def test_map_accounts_writes_audit_per_mapping(
    client: TestClient, db: Session
) -> None:
    item = _make_item(db)
    acct = _make_account(db)  # unmapped initially

    resp = client.post(
        "/api/plaid/map-accounts",
        json={
            "item_id": item.id,
            "mappings": [{"plaid_account_id": "p_acct_new", "account_id": acct.id}],
        },
    )
    assert resp.status_code == 200
    db2 = _TestSession()
    acct_after = db2.query(Account).filter_by(id=acct.id).one()
    assert acct_after.plaid_item_id == item.id
    assert acct_after.plaid_account_id == "p_acct_new"
    audit = db2.query(AuditEvent).filter_by(entity_id=acct.id, entity_type="account").one()
    assert audit.field_changed == "plaid_link"
    assert audit.new_value == "p_acct_new"


def test_map_accounts_persists_payment_method(
    client: TestClient, db: Session
) -> None:
    """REQ-PT-017: create_new mapping with payment_method persists to Account.payment_method."""
    item = _make_item(db)

    resp = client.post(
        "/api/plaid/map-accounts",
        json={
            "item_id": item.id,
            "mappings": [
                {
                    "plaid_account_id": "acc_1",
                    "create_new": {
                        "broker": "chase",
                        "account_number": "****1234",
                        "account_name": "Sparkry Operating",
                        "account_type": "checking",
                        "entity": "sparkry",
                        "tax_sheltered": False,
                        "payment_method": "Chase ****1234",
                    },
                }
            ],
        },
    )
    assert resp.status_code == 200, resp.text

    db2 = _TestSession()
    acct_after = db2.query(Account).filter_by(plaid_account_id="acc_1").one()
    assert acct_after.payment_method == "Chase ****1234"


def test_map_accounts_persists_payment_method_existing_account(
    client: TestClient, db: Session
) -> None:
    """REQ-PT-017: mapping an existing account with payment_method sets Account.payment_method."""
    item = _make_item(db)
    acct = _make_account(db)
    assert acct.payment_method is None

    resp = client.post(
        "/api/plaid/map-accounts",
        json={
            "item_id": item.id,
            "mappings": [
                {
                    "plaid_account_id": "acc_2",
                    "account_id": acct.id,
                    "payment_method": "Chase ****5678",
                }
            ],
        },
    )
    assert resp.status_code == 200, resp.text

    db2 = _TestSession()
    acct_after = db2.query(Account).filter_by(id=acct.id).one()
    assert acct_after.payment_method == "Chase ****5678"


def test_map_accounts_validates_mutually_exclusive(client: TestClient, db: Session) -> None:
    item = _make_item(db)
    acct = _make_account(db)
    # Both account_id AND create_new → 422 from validator.
    resp = client.post(
        "/api/plaid/map-accounts",
        json={
            "item_id": item.id,
            "mappings": [
                {
                    "plaid_account_id": "p",
                    "account_id": acct.id,
                    "create_new": {
                        "broker": "schwab",
                        "account_number": "x",
                        "account_type": "taxable",
                    },
                }
            ],
        },
    )
    assert resp.status_code == 422


# ── List items ───────────────────────────────────────────────────────────────


def test_list_items_excludes_placeholders(client: TestClient, db: Session) -> None:
    """Placeholders are not real Items — filter them out of the UI list."""
    real = _make_item(db, institution_name="Chase")
    db.add(
        PlaidItem(
            item_id="placeholder_pending",
            institution_id="pending",
            institution_name="pending",
            access_token_encrypted="REVOKED",
            state_nonce="x",
            state_nonce_expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=10),
        )
    )
    db.commit()
    resp = client.get("/api/plaid/items")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["institution_name"] == "Chase"
    assert body[0]["id"] == real.id


def test_list_items_includes_mapped_account_count(client: TestClient, db: Session) -> None:
    item = _make_item(db)
    _make_account(db, item=item, plaid_account_id="p_a")
    _make_account(db, item=item, plaid_account_id="p_b")
    resp = client.get("/api/plaid/items")
    assert resp.status_code == 200
    assert resp.json()[0]["mapped_account_count"] == 2


# ── Reconciliation (REQ-028) ─────────────────────────────────────────────────


def _seed_for_reconciliation(
    db: Session,
    *,
    plaid_total: Decimal,
    computed_qty: Decimal,
    computed_price: Decimal,
    account_type: str = "brokerage",
    snapshot_date: date | None = None,
) -> Account:
    """Helper: set up an Account with a Plaid snapshot + position + price."""
    item = _make_item(db, institution_name=f"Inst-{uuid.uuid4().hex[:4]}")
    acct = _make_account(db, item=item, plaid_account_id=f"p_{uuid.uuid4().hex[:6]}")
    snap_date = snapshot_date or date.today()
    db.add(
        PlaidAccountBalanceSnapshot(
            account_id=acct.id,
            snapshot_date=snap_date,
            plaid_account_type=account_type,
            current_balance=plaid_total,
            pulled_at=datetime.now(UTC).replace(tzinfo=None),
            raw_data={},
        )
    )
    db.add(
        PositionSnapshot(
            account_id=acct.id,
            symbol="VTI",
            as_of=datetime.now(UTC).replace(tzinfo=None),
            quantity=computed_qty,
            cost_basis=Decimal("0"),
            market_value=computed_qty * computed_price,
            source_file="test-fixture.csv",
            source_row_hash=f"hash-{uuid.uuid4().hex}",
            raw_data={},
        )
    )
    db.add(
        HistoricalPrice(
            symbol="VTI",
            trade_date=date.today(),
            close=computed_price,
            source="test",
            ingested_at=datetime.now(UTC).replace(tzinfo=None),
        )
    )
    db.commit()
    return acct


def test_reconciliation_within_threshold_flag_false(
    client: TestClient, db: Session
) -> None:
    """Delta < 2% AND < $100 → exceeds_threshold=False."""
    _seed_for_reconciliation(
        db,
        plaid_total=Decimal("10050.00"),
        computed_qty=Decimal("100"),
        computed_price=Decimal("100.00"),  # computed = $10,000 → delta $50 = 0.5%
    )
    resp = client.get("/api/plaid/reconciliation/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    row = body[0]
    assert Decimal(row["plaid_total"]) == Decimal("10050.0000")
    assert Decimal(row["computed_total"]) == Decimal("10000.00")
    assert Decimal(row["delta"]) == Decimal("50.0000")
    assert row["exceeds_threshold"] is False


def test_reconciliation_exceeds_dollar_threshold(client: TestClient, db: Session) -> None:
    """Delta > $100 → exceeds_threshold=True even if pct is small."""
    _seed_for_reconciliation(
        db,
        plaid_total=Decimal("1000200.00"),
        computed_qty=Decimal("1000"),
        computed_price=Decimal("1000.00"),  # computed $1M → delta $200 = 0.02%
    )
    resp = client.get("/api/plaid/reconciliation/summary")
    body = resp.json()
    assert body[0]["exceeds_threshold"] is True


def test_reconciliation_exceeds_pct_threshold(client: TestClient, db: Session) -> None:
    """Delta > 2% → exceeds_threshold=True even if absolute is small."""
    _seed_for_reconciliation(
        db,
        plaid_total=Decimal("103.00"),
        computed_qty=Decimal("10"),
        computed_price=Decimal("10.00"),  # computed $100 → delta $3 = 3%
    )
    resp = client.get("/api/plaid/reconciliation/summary")
    body = resp.json()
    assert body[0]["exceeds_threshold"] is True


def test_reconciliation_includes_disconnected_items(client: TestClient, db: Session) -> None:
    """REQ-FIX-PLD-004: a disconnected Item's account remains visible in the
    reconciliation endpoint (it's keyed off snapshot rows, not item status —
    dead/disconnected items are excluded from future sync rotation, not from
    this read-only summary of whatever snapshots already exist)."""
    item = _make_item(db, institution_name="Dead Bank", status="disconnected")
    acct = _make_account(db, item=item, plaid_account_id="p_dead")
    db.add(
        PlaidAccountBalanceSnapshot(
            account_id=acct.id,
            snapshot_date=date.today(),
            plaid_account_type="depository",
            current_balance=Decimal("100.00"),
            pulled_at=datetime.now(UTC).replace(tzinfo=None),
            raw_data={},
        )
    )
    db.commit()
    resp = client.get("/api/plaid/reconciliation/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert Decimal(body[0]["plaid_total"]) == Decimal("100.0000")


def test_reconciliation_negates_credit_balance(client: TestClient, db: Session) -> None:
    """Credit card balance is a liability — Plaid returns positive, recon must negate.

    If computed_total=0 (no positions = a card has none) and plaid_total=$500 (debt),
    the signed Plaid value is -$500 and the abs delta is $500 > $100 → flagged.
    """
    item = _make_item(db, institution_name="Card")
    acct = _make_account(db, item=item, plaid_account_id="p_card")
    db.add(
        PlaidAccountBalanceSnapshot(
            account_id=acct.id,
            snapshot_date=date.today(),
            plaid_account_type="credit",
            current_balance=Decimal("500.00"),
            pulled_at=datetime.now(UTC).replace(tzinfo=None),
            raw_data={},
        )
    )
    db.commit()
    resp = client.get("/api/plaid/reconciliation/summary")
    body = resp.json()
    assert len(body) == 1
    row = body[0]
    assert Decimal(row["plaid_total"]) == Decimal("-500.0000")  # negated
    # No positions → computed_total is None.
    assert row["computed_total"] is None
    assert row["delta"] is None
    assert row["exceeds_threshold"] is False  # no comparison possible


# ── Threshold boundaries (REQ-028, exact `>` semantics) ──────────────────────


@pytest.mark.parametrize(
    "plaid_total,computed_total,expected_exceeds",
    [
        # delta = $100.00 exactly (computed $10,000, plaid $10,100) → 1.0% pct, $100 abs
        # The contract is `> $100` (strictly greater), so $100.00 should NOT exceed.
        (Decimal("10100.00"), Decimal("10000.00"), False),
        # delta = $100.01 → strictly greater than $100 → exceeds
        (Decimal("10100.01"), Decimal("10000.00"), True),
        # delta = exactly 2% ($200 on $10k) → strict `> 2%` means NOT exceeds (and $200 > $100 IS exceeds)
        # So this MUST flag via the dollar arm regardless of the pct comparator.
        (Decimal("10200.00"), Decimal("10000.00"), True),
        # delta = $50, computed = $2400 → pct ~= 2.08%, dollar arm clean (under $100)
        # Pct strictly greater than 2% → flag.
        (Decimal("2450.00"), Decimal("2400.00"), True),
        # delta = $48 on $2400 = exactly 2.0% pct, dollar arm clean
        # Strict `> 2%` → False on pct; dollar $48 < $100 → False. Overall False.
        (Decimal("2448.00"), Decimal("2400.00"), False),
    ],
)
def test_reconciliation_threshold_boundaries(
    client: TestClient,
    db: Session,
    plaid_total: Decimal,
    computed_total: Decimal,
    expected_exceeds: bool,
) -> None:
    """REQ-028: thresholds are `> 2%` AND `> $100` (strict). Boundary precision matters."""
    # Use 1 share × computed_total as the price so total = computed_total.
    _seed_for_reconciliation(
        db,
        plaid_total=plaid_total,
        computed_qty=Decimal("1"),
        computed_price=computed_total,
    )
    resp = client.get("/api/plaid/reconciliation/summary")
    body = resp.json()
    assert len(body) == 1
    assert body[0]["exceeds_threshold"] is expected_exceeds


def test_reconciliation_returns_none_when_no_positions_priced(
    client: TestClient, db: Session
) -> None:
    """REQ-028 + P0 fix: when an account has positions but NO historical prices exist
    for any symbol, computed_total must be None (not 0). A 0 would falsely flag the
    account on a cold price-table.
    """
    from src.models.brokerage import PositionSnapshot

    item = _make_item(db, institution_name="Vanguard")
    acct = _make_account(db, item=item, plaid_account_id="p_v1")
    db.add(
        PlaidAccountBalanceSnapshot(
            account_id=acct.id,
            snapshot_date=date.today(),
            plaid_account_type="brokerage",
            current_balance=Decimal("50000.00"),
            pulled_at=datetime.now(UTC).replace(tzinfo=None),
            raw_data={},
        )
    )
    db.add(
        PositionSnapshot(
            account_id=acct.id,
            symbol="UNPRICED_SYMBOL",  # no HistoricalPrice row for this
            as_of=datetime.now(UTC).replace(tzinfo=None),
            quantity=Decimal("100"),
            cost_basis=Decimal("0"),
            market_value=Decimal("0"),
            source_file="test.csv",
            source_row_hash=f"hash-{uuid.uuid4().hex}",
            raw_data={},
        )
    )
    db.commit()
    resp = client.get("/api/plaid/reconciliation/summary")
    body = resp.json()
    assert len(body) == 1
    row = body[0]
    assert row["computed_total"] is None
    assert row["delta"] is None
    assert row["delta_pct"] is None
    assert row["exceeds_threshold"] is False


# ── Relink endpoint (REQ-025) ────────────────────────────────────────────────


def test_relink_404_when_item_unknown(client: TestClient) -> None:
    resp = client.post("/api/plaid/relink/nonexistent-id")
    assert resp.status_code == 404


def test_relink_400_when_token_revoked(client: TestClient, db: Session) -> None:
    """A disconnected/revoked Item cannot be relinked — caller must connect anew."""
    item = _make_item(db)
    item.access_token_encrypted = "REVOKED"
    db.commit()
    resp = client.post(f"/api/plaid/relink/{item.id}")
    assert resp.status_code == 400


def test_relink_happy_path_preserves_item_id(
    client: TestClient, db: Session, plaid_client_mock: MagicMock
) -> None:
    """REQ-025: relink generates a new link_token + nonce but keeps the SAME item_id
    so accounts mapping stays intact (update mode)."""
    item = _make_item(db, institution_name="Chase")
    original_item_id = item.item_id

    plaid_client_mock.link_token_create.return_value = SimpleNamespace(
        link_token="link-update-mode-token-xyz"
    )
    resp = client.post(f"/api/plaid/relink/{item.id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["link_token"] == "link-update-mode-token-xyz"
    assert len(body["state_nonce"]) >= 32

    # Confirm Plaid SDK was called with access_token (update-mode signal).
    _, call_kwargs = plaid_client_mock.link_token_create.call_args
    if call_kwargs:
        req = call_kwargs.get("request")
    else:
        req = plaid_client_mock.link_token_create.call_args[0][0]
    # The request should carry the access_token. SDK request object is an
    # openapi model; we just confirm the call happened with a request object.
    assert req is not None

    # item_id must be preserved — that's the whole point of update mode.
    db2 = _TestSession()
    after = db2.query(PlaidItem).filter_by(id=item.id).one()
    assert after.item_id == original_item_id


# ── Sync-now (REQ-026) ───────────────────────────────────────────────────────


def test_sync_now_404_on_unknown_item(client: TestClient) -> None:
    # First clear cooldown so the rate-limiter doesn't intercept.
    import src.api.routes.plaid as plaid_routes_mod

    plaid_routes_mod._sync_now_last_call.clear()
    resp = client.post("/api/plaid/sync-now?item_id=does-not-exist")
    assert resp.status_code == 404
    # P2-004-SEC: the 404 must happen BEFORE the rate-limit write, so an unknown
    # id neither consumes its cooldown slot nor grows the limiter dict.
    assert "does-not-exist" not in plaid_routes_mod._sync_now_last_call


def test_sync_now_rate_limited_to_one_per_minute_per_item(
    client: TestClient, db: Session, plaid_client_mock: MagicMock
) -> None:
    """Calling /sync-now twice in quick succession for the same item → second 429."""
    import src.api.routes.plaid as plaid_routes_mod

    plaid_routes_mod._sync_now_last_call.clear()
    item = _make_item(db)
    plaid_client_mock.accounts_balance_get.return_value = SimpleNamespace(accounts=[])

    r1 = client.post(f"/api/plaid/sync-now?item_id={item.id}")
    assert r1.status_code == 200
    r2 = client.post(f"/api/plaid/sync-now?item_id={item.id}")
    assert r2.status_code == 429
    assert "cooldown" in r2.json()["detail"].lower()


def test_sync_now_all_items_smoke(
    client: TestClient, db: Session, plaid_client_mock: MagicMock
) -> None:
    import src.api.routes.plaid as plaid_routes_mod

    plaid_routes_mod._sync_now_last_call.clear()
    _make_item(db, institution_name="A")
    _make_item(db, institution_name="B")
    plaid_client_mock.accounts_balance_get.return_value = SimpleNamespace(accounts=[])

    resp = client.post("/api/plaid/sync-now")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 2


# ── Nonce replay + placeholder GC ────────────────────────────────────────────


def test_exchange_nonce_cannot_be_replayed(
    client: TestClient, db: Session, plaid_client_mock: MagicMock
) -> None:
    """REQ-025 CSRF: a successful /exchange clears state_nonce; the same nonce
    posted a second time must be rejected with 400."""
    nonce = "replay-nonce-test"
    ph = PlaidItem(
        item_id="placeholder_replay",
        institution_id="pending",
        institution_name="pending",
        access_token_encrypted="REVOKED",
        state_nonce=nonce,
        state_nonce_expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=10),
    )
    db.add(ph)
    db.commit()

    plaid_client_mock.item_public_token_exchange.return_value = SimpleNamespace(
        access_token="access-x", item_id="plaid_item_replay_target"
    )
    plaid_client_mock.accounts_get.return_value = SimpleNamespace(accounts=[])

    payload = {
        "public_token": "pt-replay",
        "state_nonce": nonce,
        "institution_id": "ins_3",
        "institution_name": "Chase",
    }
    r1 = client.post("/api/plaid/exchange", json=payload)
    assert r1.status_code == 200
    r2 = client.post("/api/plaid/exchange", json=payload)
    assert r2.status_code == 400  # nonce now NULL → invalid


def test_link_token_prunes_stale_placeholders(
    client: TestClient, db: Session, plaid_client_mock: MagicMock
) -> None:
    """Expired placeholder PlaidItem rows are deleted on next /link-token call."""
    stale_id = "placeholder_stale_to_prune"
    db.add(
        PlaidItem(
            item_id=stale_id,
            institution_id="pending",
            institution_name="pending",
            access_token_encrypted="REVOKED",
            state_nonce="stale-nonce",
            state_nonce_expires_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1),
        )
    )
    db.commit()
    assert db.query(PlaidItem).filter_by(item_id=stale_id).count() == 1

    plaid_client_mock.link_token_create.return_value = SimpleNamespace(
        link_token="link-token-after-prune"
    )
    resp = client.post("/api/plaid/link-token", json={})
    assert resp.status_code == 200
    # The stale placeholder is now gone; a new fresh placeholder exists.
    db2 = _TestSession()
    assert db2.query(PlaidItem).filter_by(item_id=stale_id).count() == 0
    fresh = db2.query(PlaidItem).filter(PlaidItem.item_id.like("placeholder_%")).all()
    assert len(fresh) == 1


# ── Institution name validation (REQ-025 audit-log integrity) ────────────────


@pytest.mark.parametrize(
    "poison",
    [
        "Chase\nfake-line",  # ASCII newline U+000A
        "Chase‮override",  # RTL override
        "Chase​zero-width",  # zero-width space
        "ChaseNEL",  # Unicode NEL line terminator
        "Chase LS",  # Unicode line separator
        "Banké",  # non-ASCII Latin (e-acute); printable but still rejected by ASCII allowlist
    ],
)
def test_exchange_rejects_non_printable_ascii_institution_name(
    client: TestClient, db: Session, poison: str
) -> None:
    """Defense-in-depth: institution_name allowlist is printable ASCII only.

    This rejects ASCII control chars (newline-smuggling into audit log),
    Unicode line terminators (NEL, LS, PS), bidi override (RTL flip in log
    viewers), zero-width chars (invisible payload), and non-ASCII Latin
    (would render fine but a slippery slope; all spec institutions are ASCII).
    """
    nonce = f"ctrl-test-{hash(poison)}"
    ph = PlaidItem(
        item_id=f"placeholder_ctrl_{abs(hash(poison))}",
        institution_id="pending",
        institution_name="pending",
        access_token_encrypted="REVOKED",
        state_nonce=nonce,
        state_nonce_expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=10),
    )
    db.add(ph)
    db.commit()

    resp = client.post(
        "/api/plaid/exchange",
        json={
            "public_token": "pt",
            "state_nonce": nonce,
            "institution_id": "ins_3",
            "institution_name": poison,
        },
    )
    assert resp.status_code == 422


# ── Sync-transactions-now (REQ-PT-015) ──────────────────────────────────────


def test_sync_transactions_now_rate_limited(
    client: TestClient, db: Session, plaid_client_mock: MagicMock
) -> None:
    """POST /items/{id}/sync-transactions: first call 200, immediate second 429."""
    import src.api.routes.plaid as plaid_routes_mod

    # Isolate from balance sync-now limiter state.
    plaid_routes_mod._tx_sync_now_last_call.clear()
    item = _make_item(db)
    plaid_client_mock.transactions_sync.return_value = SimpleNamespace(
        added=[], modified=[], removed=[], next_cursor="c", has_more=False
    )
    r1 = client.post(f"/api/plaid/items/{item.id}/sync-transactions")
    assert r1.status_code == 200
    # First call actually ran the sync: body carries the expected result shape.
    body = r1.json()
    assert body["status"] == "ok"
    # P2-002 / P3-004: 'reactivated' must be in the response so operators driving
    # a manual sync see reinstated plaid_readded rows distinctly from fresh adds.
    for key in ("added", "reactivated", "modified", "removed", "failed", "superseded"):
        assert key in body
    r2 = client.post(f"/api/plaid/items/{item.id}/sync-transactions")
    assert r2.status_code == 429


def test_sync_transactions_now_404_on_unknown_item(
    client: TestClient, db: Session, plaid_client_mock: MagicMock
) -> None:
    """REQ-PT-015: unknown item_id → 404, and the 404 does NOT consume the
    cooldown nor grow the limiter dict."""
    import src.api.routes.plaid as plaid_routes_mod

    plaid_routes_mod._tx_sync_now_last_call.clear()
    resp = client.post("/api/plaid/items/nonexistent-id/sync-transactions")
    assert resp.status_code == 404
    # 404 happened before the rate-limiter write — id not recorded.
    assert "nonexistent-id" not in plaid_routes_mod._tx_sync_now_last_call
