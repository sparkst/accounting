"""Plaid backend routes — Item lifecycle, balance sync trigger, reconciliation.

REQ-025: Item lifecycle (link → exchange → map → disconnect/relink).
REQ-026: ``POST /sync-now`` triggers the same flow as the cron.
REQ-028: ``GET /reconciliation/summary`` exposes Plaid-vs-computed deltas.
REQ-029: Lifecycle endpoints write AuditEvent rows with ``entity_type`` set.

All routes mounted at ``/api/plaid`` and protected by ``require_api_key`` in
``src/api/main.py`` (the standard pattern for admin routes). The Tailscale gate
fronting these endpoints is a *defense in depth* measure, not a substitute for
auth — in-network actors (other devices on the tailnet) must still present the
API key.

State-nonce CSRF protection on ``POST /exchange``: the client receives a
``state_nonce`` from ``POST /link-token``; the server stores it in a
placeholder ``PlaidItem`` row keyed by the nonce. The ``/exchange`` call MUST
echo the nonce back; the server rejects unmatched/expired nonces with 400.
This blocks an attacker from posting a forged ``public_token`` to ``/exchange``.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path
from plaid.exceptions import ApiException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func
from sqlalchemy.orm import Session

from src.adapters.plaid_balance import sync_all_active, sync_one_item
from src.adapters.plaid_client import PlaidErrorBase, make_plaid_client
from src.db.connection import SessionLocal
from src.models.audit_event import (
    ENTITY_TYPE_ACCOUNT,
    ENTITY_TYPE_PLAID_ITEM,
    AuditEvent,
)
from src.models.brokerage import Account, PositionSnapshot
from src.models.history import HistoricalPrice
from src.models.plaid import (
    PLAID_LIABILITY_TYPES,
    REVOKED_TOKEN_SENTINEL,
    PlaidAccountBalanceSnapshot,
    PlaidItem,
)
from src.utils.plaid_crypto import (
    InvalidCiphertextError,
    decrypt_token,
    encrypt_token,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/plaid", tags=["plaid"])


# ── Tunables (module-level so tests can override) ────────────────────────────

# Plaid link_token TTL is 30 min; the state_nonce shadows that.
STATE_NONCE_TTL = timedelta(minutes=30)

# Reconciliation thresholds. Either condition flips ``exceeds_threshold=True``.
RECONCILIATION_DELTA_PCT_THRESHOLD = Decimal("2.0")  # 2%
RECONCILIATION_DELTA_ABS_THRESHOLD = Decimal("100.00")  # $100

# In-memory rate limiter for /sync-now. Keyed by item_id → last-call timestamp.
_SYNC_NOW_COOLDOWN_SECONDS = 60.0
_sync_now_last_call: dict[str, float] = {}


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def get_db() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _get_plaid_client() -> Any:
    """Indirection so tests can monkeypatch ``make_plaid_client``."""
    return make_plaid_client()


# ── Request / response models ────────────────────────────────────────────────


class LinkTokenResponse(BaseModel):
    link_token: str
    state_nonce: str
    expires_at: datetime


class ExchangeRequest(BaseModel):
    public_token: str = Field(min_length=1, max_length=256)
    state_nonce: str = Field(min_length=1, max_length=128)
    # Plaid institution_id is opaque, alphanumeric, ≤16 chars in practice (e.g. "ins_3").
    institution_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    # Institution display name is shown in the audit log and UI. Restrict to
    # printable ASCII (U+0020..U+007E) so an attacker can't smuggle Unicode
    # line terminators (NEL U+0085, LINE/PARAGRAPH SEPARATOR U+2028/9), RTL
    # override (U+202E), or zero-width chars into the audit trail. All Plaid
    # institution names in the spec slot allocation (Chase, BofA, Vanguard,
    # Fidelity, Schwab, E*TRADE, Citi, PenFed, Franklin Templeton) are
    # printable ASCII.
    institution_name: str = Field(min_length=1, max_length=128, pattern=r"^[ -~]+$")


class ExchangeResponse(BaseModel):
    item_id: str
    plaid_item_id: str  # our internal UUID
    accounts: list[dict[str, Any]]


# Note: CreateNewAccount → AccountMapping → MapAccountsRequest defined in that
# order so forward refs aren't needed and `model_rebuild()` isn't required.
class CreateNewAccount(BaseModel):
    broker: str
    account_number: str
    account_name: str | None = None
    account_type: str
    entity: str = "personal"
    tax_sheltered: bool = False
    payment_method: str | None = None


class AccountMapping(BaseModel):
    plaid_account_id: str
    account_id: str | None = Field(
        default=None, description="Map to existing Account. Mutually exclusive with create_new."
    )
    create_new: CreateNewAccount | None = None
    payment_method: str | None = None

    @field_validator("create_new")
    @classmethod
    def _validate_mutually_exclusive(
        cls, v: CreateNewAccount | None, info: Any
    ) -> CreateNewAccount | None:
        existing_aid = info.data.get("account_id")
        if existing_aid and v:
            raise ValueError("account_id and create_new are mutually exclusive")
        if not existing_aid and not v:
            raise ValueError("exactly one of account_id or create_new is required")
        return v


class MapAccountsRequest(BaseModel):
    item_id: str = Field(description="Our internal plaid_item.id (NOT Plaid's item_id)")
    mappings: list[AccountMapping]


class ItemSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    item_id: str
    institution_id: str
    institution_name: str
    status: str
    last_sync_at: datetime | None
    last_sync_status: str | None
    last_error: str | None
    mapped_account_count: int = 0


class ReconciliationRow(BaseModel):
    account_id: str
    account_name: str | None
    snapshot_date: str
    plaid_account_type: str
    plaid_total: Decimal
    computed_total: Decimal | None
    delta: Decimal | None
    delta_pct: Decimal | None
    exceeds_threshold: bool


# ── Internal helpers ─────────────────────────────────────────────────────────


def _validate_state_nonce(session: Session, *, nonce: str) -> PlaidItem:
    """Find the placeholder PlaidItem keyed by nonce, fail 400 if missing/expired."""
    placeholder = (
        session.query(PlaidItem).filter_by(state_nonce=nonce, status="active").first()
    )
    if placeholder is None:
        raise HTTPException(status_code=400, detail="invalid state_nonce")
    if (
        placeholder.state_nonce_expires_at is None
        or placeholder.state_nonce_expires_at < _now()
    ):
        # Don't leak whether the nonce existed-and-expired vs never-existed.
        raise HTTPException(status_code=400, detail="invalid state_nonce")
    return placeholder


def _write_audit(
    session: Session,
    *,
    entity_id: str,
    entity_type: str,
    field_changed: str,
    old_value: str | None,
    new_value: str | None,
    changed_by: str = "human",
) -> None:
    """Append an entity-mode AuditEvent (REQ-029)."""
    session.add(
        AuditEvent(
            entity_id=entity_id,
            entity_type=entity_type,
            field_changed=field_changed,
            old_value=old_value,
            new_value=new_value,
            changed_by=changed_by,
        )
    )


def _enumerate_accounts_for_response(
    plaid_client: Any, access_token: str
) -> list[dict[str, Any]]:
    """Call /accounts/get and return the list as JSON-friendly dicts."""
    from plaid.model.accounts_get_request import AccountsGetRequest

    resp = plaid_client.accounts_get(AccountsGetRequest(access_token=access_token))
    return [acct.to_dict() for acct in resp.accounts]


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.post("/link-token", response_model=LinkTokenResponse)
def create_link_token(
    session: Session = Depends(get_db),  # noqa: B008
) -> LinkTokenResponse:
    """Create a Plaid Link token and a server-stored state nonce.

    The state nonce is stored on a placeholder PlaidItem row (status='active',
    encrypted_token=REVOKED_TOKEN_SENTINEL, no item_id yet) keyed by the nonce.
    On exchange we promote the placeholder by writing the real item_id and
    encrypted access_token; if exchange never happens, the placeholder is
    pruned the next time create_link_token runs.
    """
    from plaid.model.country_code import CountryCode
    from plaid.model.link_token_create_request import LinkTokenCreateRequest
    from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
    from plaid.model.products import Products

    _prune_stale_placeholders(session)

    nonce = secrets.token_urlsafe(32)
    expires_at = _now() + STATE_NONCE_TTL

    placeholder = PlaidItem(
        item_id=f"placeholder_{nonce[:24]}",  # unique placeholder; promoted on exchange
        institution_id="pending",
        institution_name="pending",
        access_token_encrypted=REVOKED_TOKEN_SENTINEL,
        state_nonce=nonce,
        state_nonce_expires_at=expires_at,
        status="active",
    )
    session.add(placeholder)
    session.flush()  # need placeholder.id for the Plaid request user payload

    client = _get_plaid_client()
    req = LinkTokenCreateRequest(
        user=LinkTokenCreateRequestUser(client_user_id=placeholder.id),
        client_name="Travis Accounting",
        # ``balance`` is intentionally omitted: Plaid rejects it in any product
        # field (INVALID_PRODUCT) because it auto-initializes whenever another
        # product — here ``transactions`` — is requested.
        products=[Products("transactions")],
        additional_consented_products=[Products("investments")],
        country_codes=[CountryCode("US")],
        language="en",
    )
    resp = client.link_token_create(req)
    session.commit()
    return LinkTokenResponse(
        link_token=resp.link_token, state_nonce=nonce, expires_at=expires_at
    )


def _prune_stale_placeholders(session: Session) -> None:
    """Delete placeholder rows whose nonce has expired and never got exchanged.

    Placeholders are identified by ``item_id`` starting with ``placeholder_``.
    Real Items have Plaid's opaque item_id which never matches that prefix.
    """
    now = _now()
    stale = (
        session.query(PlaidItem)
        .filter(
            PlaidItem.item_id.like("placeholder_%"),
            PlaidItem.state_nonce_expires_at < now,
        )
        .all()
    )
    for p in stale:
        session.delete(p)


@router.post("/exchange", response_model=ExchangeResponse)
def exchange_public_token(
    payload: ExchangeRequest,
    session: Session = Depends(get_db),  # noqa: B008
) -> ExchangeResponse:
    """Promote a placeholder PlaidItem after Plaid Link onSuccess.

    Rejects with 400 if state_nonce missing, mismatched, or expired (REQ-025
    CSRF defense).
    """
    from plaid.model.item_public_token_exchange_request import (
        ItemPublicTokenExchangeRequest,
    )

    placeholder = _validate_state_nonce(session, nonce=payload.state_nonce)

    client = _get_plaid_client()
    exchange_resp = client.item_public_token_exchange(
        ItemPublicTokenExchangeRequest(public_token=payload.public_token)
    )
    access_token = exchange_resp.access_token
    item_id = exchange_resp.item_id

    # Reject duplicates: an Item ID already in our DB means the user re-linked
    # without going through /relink. Refuse to create a second PlaidItem row.
    existing = session.query(PlaidItem).filter_by(item_id=item_id).first()
    if existing is not None and existing.id != placeholder.id:
        # Roll back the placeholder write.
        session.delete(placeholder)
        session.commit()
        raise HTTPException(
            status_code=409, detail=f"item_id {item_id} already connected (use /relink)"
        )

    placeholder.item_id = item_id
    placeholder.institution_id = payload.institution_id
    placeholder.institution_name = payload.institution_name
    placeholder.access_token_encrypted = encrypt_token(access_token)
    placeholder.state_nonce = None
    placeholder.state_nonce_expires_at = None
    placeholder.updated_at = _now()

    accounts = _enumerate_accounts_for_response(client, access_token)

    _write_audit(
        session,
        entity_id=placeholder.id,
        entity_type=ENTITY_TYPE_PLAID_ITEM,
        field_changed="connect",
        old_value=None,
        new_value=f"{payload.institution_name} ({item_id})",
    )
    session.commit()
    return ExchangeResponse(
        item_id=item_id, plaid_item_id=placeholder.id, accounts=accounts
    )


@router.post("/map-accounts", status_code=200)
def map_accounts(
    payload: MapAccountsRequest,
    session: Session = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    """Map Plaid accounts to existing Account rows OR create new ones.

    Writes one AuditEvent per mapping (REQ-029).
    """
    item = session.query(PlaidItem).filter_by(id=payload.item_id, status="active").first()
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")

    results: list[dict[str, str]] = []
    for m in payload.mappings:
        if m.account_id:
            account = session.query(Account).filter_by(id=m.account_id).first()
            if account is None:
                raise HTTPException(status_code=404, detail=f"account {m.account_id} not found")
            if m.payment_method is not None:
                account.payment_method = m.payment_method
        else:
            assert m.create_new is not None  # validator guarantees this
            account = Account(
                broker=m.create_new.broker,
                account_number=m.create_new.account_number,
                account_name=m.create_new.account_name,
                account_type=m.create_new.account_type,
                entity=m.create_new.entity,
                tax_sheltered=m.create_new.tax_sheltered,
                payment_method=m.create_new.payment_method,
            )
            session.add(account)
            session.flush()

        old_link = account.plaid_account_id
        account.plaid_item_id = item.id
        account.plaid_account_id = m.plaid_account_id
        _write_audit(
            session,
            entity_id=account.id,
            entity_type=ENTITY_TYPE_ACCOUNT,
            field_changed="plaid_link",
            old_value=old_link,
            new_value=m.plaid_account_id,
        )
        results.append({"plaid_account_id": m.plaid_account_id, "account_id": account.id})

    session.commit()
    return {"mappings": results}


@router.post("/disconnect/{item_id}", status_code=200)
def disconnect_item(
    item_id: str = Path(..., min_length=1),
    session: Session = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    """Disconnect a Plaid Item.

    Sequence (matters):
      1. Decrypt access_token, call Plaid ``/item/remove``.
      2. Overwrite ``access_token_encrypted = REVOKED_TOKEN_SENTINEL`` so the
         ciphertext does not linger in SQLite freed pages or WAL snapshots
         (REQ-025 / spec § Disconnect flow).
      3. Set ``status='disconnected'``.
      4. Null FK on associated Account rows.
      5. AuditEvent.
    """
    from plaid.model.item_remove_request import ItemRemoveRequest

    item = session.query(PlaidItem).filter_by(id=item_id).first()
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    if item.status == "disconnected":
        return {"status": "already_disconnected", "item_id": item_id}

    # Best-effort Plaid /item/remove. If the token can't be decrypted (key
    # rotated, sentinel), still null the row out — the user wants this gone.
    # Network errors / Plaid API errors also fall through to local revocation;
    # response surfaces ``plaid_remove_called=False`` so the caller knows the
    # institution may still consider the Item active server-side.
    plaid_removed = False
    try:
        access_token = decrypt_token(item.access_token_encrypted)
        client = _get_plaid_client()
        client.item_remove(ItemRemoveRequest(access_token=access_token))
        plaid_removed = True
    except InvalidCiphertextError:
        logger.warning(
            "disconnect: could not decrypt token, skipping /item/remove",
            extra={"item_id": item_id},
        )
    except (ApiException, PlaidErrorBase):
        logger.exception(
            "disconnect: Plaid /item/remove failed", extra={"item_id": item_id}
        )

    item.access_token_encrypted = REVOKED_TOKEN_SENTINEL
    item.status = "disconnected"
    item.updated_at = _now()

    # Null FK on linked Accounts.
    linked = session.query(Account).filter_by(plaid_item_id=item.id).all()
    for acct in linked:
        old_link = acct.plaid_account_id
        acct.plaid_item_id = None
        acct.plaid_account_id = None
        _write_audit(
            session,
            entity_id=acct.id,
            entity_type=ENTITY_TYPE_ACCOUNT,
            field_changed="plaid_link",
            old_value=old_link,
            new_value=None,
        )

    _write_audit(
        session,
        entity_id=item.id,
        entity_type=ENTITY_TYPE_PLAID_ITEM,
        field_changed="disconnect",
        old_value=item.institution_name,
        new_value=None,
    )
    session.commit()
    return {
        "status": "disconnected",
        "item_id": item_id,
        "accounts_unmapped": len(linked),
        "plaid_remove_called": plaid_removed,
    }


@router.post("/relink/{item_id}", response_model=LinkTokenResponse)
def relink_item(
    item_id: str = Path(..., min_length=1),
    session: Session = Depends(get_db),  # noqa: B008
) -> LinkTokenResponse:
    """Generate a Plaid Link token in update mode for an existing Item.

    Used for ITEM_LOGIN_REQUIRED recovery — keeps the same item_id so the
    accounts mapping stays intact (REQ-025 re-link flow).
    """
    from plaid.model.country_code import CountryCode
    from plaid.model.link_token_create_request import LinkTokenCreateRequest
    from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser

    item = session.query(PlaidItem).filter_by(id=item_id, status="active").first()
    if item is None:
        raise HTTPException(status_code=404, detail="item not found or disconnected")
    if item.access_token_encrypted == REVOKED_TOKEN_SENTINEL:
        raise HTTPException(status_code=400, detail="item is revoked, cannot relink")

    access_token = decrypt_token(item.access_token_encrypted)
    nonce = secrets.token_urlsafe(32)
    expires_at = _now() + STATE_NONCE_TTL
    item.state_nonce = nonce
    item.state_nonce_expires_at = expires_at

    client = _get_plaid_client()
    req = LinkTokenCreateRequest(
        user=LinkTokenCreateRequestUser(client_user_id=item.id),
        client_name="Travis Accounting",
        country_codes=[CountryCode("US")],
        language="en",
        access_token=access_token,  # update-mode signal
    )
    resp = client.link_token_create(req)
    session.commit()
    return LinkTokenResponse(
        link_token=resp.link_token, state_nonce=nonce, expires_at=expires_at
    )


@router.get("/items", response_model=list[ItemSummary])
def list_items(session: Session = Depends(get_db)) -> list[ItemSummary]:  # noqa: B008
    """List active PlaidItems with sync status + mapped-account count.

    Placeholders (item_id starting with 'placeholder_') are excluded.
    """
    rows = (
        session.query(PlaidItem)
        .filter(
            PlaidItem.status == "active",
            ~PlaidItem.item_id.like("placeholder_%"),
        )
        .order_by(PlaidItem.institution_name)
        .all()
    )
    count_rows = (
        session.query(Account.plaid_item_id, func.count(Account.id))
        .filter(Account.plaid_item_id.isnot(None))
        .group_by(Account.plaid_item_id)
        .all()
    )
    counts: dict[str | None, int] = {row[0]: int(row[1]) for row in count_rows}
    return [
        ItemSummary(
            id=r.id,
            item_id=r.item_id,
            institution_id=r.institution_id,
            institution_name=r.institution_name,
            status=r.status,
            last_sync_at=r.last_sync_at,
            last_sync_status=r.last_sync_status,
            last_error=r.last_error,
            mapped_account_count=int(counts.get(r.id, 0)),
        )
        for r in rows
    ]


@router.post("/sync-now")
def sync_now(
    item_id: str | None = None,
    session: Session = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    """Manual trigger of Plaid Balance sync (single Item or all).

    Rate-limited to 1 call per ``_SYNC_NOW_COOLDOWN_SECONDS`` per Item to keep
    Plaid happy — Balance is per-call billed in production.
    """
    cooldown_key = item_id or "*"
    now = time.monotonic()
    last = _sync_now_last_call.get(cooldown_key, 0.0)
    if now - last < _SYNC_NOW_COOLDOWN_SECONDS:
        wait = int(_SYNC_NOW_COOLDOWN_SECONDS - (now - last))
        raise HTTPException(
            status_code=429,
            detail=f"sync cooldown active, retry in {wait}s",
        )
    _sync_now_last_call[cooldown_key] = now

    client = _get_plaid_client()
    if item_id is None:
        batch = sync_all_active(session, client=client, dry_run=False)
        return {
            "items": [
                {
                    "institution_name": r.institution_name,
                    "status": r.status,
                    "accounts_processed": r.accounts_processed,
                    "accounts_failed": r.accounts_failed,
                    "error_code": r.error_code,
                }
                for r in batch.items
            ]
        }
    item = session.query(PlaidItem).filter_by(id=item_id, status="active").first()
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    result = sync_one_item(session, item, client=client)
    session.commit()
    return {
        "institution_name": result.institution_name,
        "status": result.status,
        "accounts_processed": result.accounts_processed,
        "accounts_failed": result.accounts_failed,
        "error_code": result.error_code,
    }


@router.get("/reconciliation/summary", response_model=list[ReconciliationRow])
def reconciliation_summary(
    session: Session = Depends(get_db),  # noqa: B008
) -> list[ReconciliationRow]:
    """Latest Plaid-vs-computed delta per account, with threshold flag.

    REQ-028: returns ``exceeds_threshold`` when delta exceeds 2% OR $100.
    Credit/loan account types are negated before comparison (the snapshot
    stores Plaid's positive-amount-owed convention as-returned).
    """
    # Get the latest snapshot per account.
    latest_dates = (
        session.query(
            PlaidAccountBalanceSnapshot.account_id,
            func.max(PlaidAccountBalanceSnapshot.snapshot_date).label("max_date"),
        )
        .group_by(PlaidAccountBalanceSnapshot.account_id)
        .subquery()
    )
    rows = (
        session.query(PlaidAccountBalanceSnapshot, Account)
        .join(
            latest_dates,
            (PlaidAccountBalanceSnapshot.account_id == latest_dates.c.account_id)
            & (PlaidAccountBalanceSnapshot.snapshot_date == latest_dates.c.max_date),
        )
        .join(Account, Account.id == PlaidAccountBalanceSnapshot.account_id)
        .all()
    )

    out: list[ReconciliationRow] = []
    for snap, account in rows:
        plaid_signed = snap.current_balance
        if snap.plaid_account_type in PLAID_LIABILITY_TYPES:
            plaid_signed = -plaid_signed

        computed = _compute_account_total(session, account_id=account.id)
        if computed is None:
            delta: Decimal | None = None
            delta_pct: Decimal | None = None
            exceeds = False
        else:
            delta = plaid_signed - computed
            if computed == 0:
                delta_pct = None
                exceeds = abs(delta) > RECONCILIATION_DELTA_ABS_THRESHOLD
            else:
                delta_pct = (delta / computed) * Decimal("100")
                exceeds = (
                    abs(delta) > RECONCILIATION_DELTA_ABS_THRESHOLD
                    or abs(delta_pct) > RECONCILIATION_DELTA_PCT_THRESHOLD
                )

        out.append(
            ReconciliationRow(
                account_id=account.id,
                account_name=account.account_name,
                snapshot_date=snap.snapshot_date.isoformat(),
                plaid_account_type=snap.plaid_account_type,
                plaid_total=plaid_signed,
                computed_total=computed,
                delta=delta,
                delta_pct=delta_pct,
                exceeds_threshold=exceeds,
            )
        )
    return out


def _compute_account_total(session: Session, *, account_id: str) -> Decimal | None:
    """Latest computed total = sum(latest_position.quantity × latest_historical_price).

    Returns None when there are no positions, OR when not a single held symbol has
    a HistoricalPrice row (caller treats None as 'not comparable'). Returning 0
    instead of None in the latter case would mis-flag accounts as reconciliation
    failures on a cold price-table — a stale price backfill should not look like
    a vanished account.
    """
    latest_by_symbol = (
        session.query(
            PositionSnapshot.symbol,
            func.max(PositionSnapshot.as_of).label("max_as_of"),
        )
        .filter(PositionSnapshot.account_id == account_id)
        .group_by(PositionSnapshot.symbol)
        .subquery()
    )
    positions = (
        session.query(PositionSnapshot)
        .join(
            latest_by_symbol,
            (PositionSnapshot.symbol == latest_by_symbol.c.symbol)
            & (PositionSnapshot.as_of == latest_by_symbol.c.max_as_of),
        )
        .filter(PositionSnapshot.account_id == account_id)
        .all()
    )
    if not positions:
        return None

    total = Decimal("0")
    priced_count = 0
    for pos in positions:
        price = (
            session.query(HistoricalPrice.close)
            .filter(HistoricalPrice.symbol == pos.symbol)
            .order_by(HistoricalPrice.trade_date.desc())
            .first()
        )
        if price is None:
            continue
        total += Decimal(str(pos.quantity)) * Decimal(str(price[0]))
        priced_count += 1
    if priced_count == 0:
        return None
    return total
