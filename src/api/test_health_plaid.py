"""Tests for the Plaid stale-Item branch of /api/health (REQ-027)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

# Pull in dependent models so create_all builds everything.
import src.models.audit_event  # noqa: F401
import src.models.brokerage  # noqa: F401
from src.api.routes.health import (
    PLAID_STALE_THRESHOLD,
    _compute_plaid_stale_items,
)
from src.models.base import Base
from src.models.plaid import PlaidItem
from src.utils.plaid_crypto import encrypt_token


@pytest.fixture(autouse=True)
def _enc_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLAID_TOKEN_ENC_KEY", Fernet.generate_key().decode())


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _fk(c: Any, _: Any) -> None:
        c.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return Session(bind=engine)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _make_item(
    session: Session,
    *,
    name: str,
    status: str = "active",
    sync_status: str | None = "ok",
    error: str | None = None,
    last_sync_at: datetime | None = None,
    item_id: str | None = None,
) -> PlaidItem:
    item = PlaidItem(
        item_id=item_id or f"plaid_{name.lower()}",
        institution_id="ins_x",
        institution_name=name,
        access_token_encrypted=encrypt_token("x"),
        status=status,
        last_sync_status=sync_status,
        last_error=error,
        last_sync_at=last_sync_at,
    )
    session.add(item)
    session.commit()
    return item


def test_recent_ok_item_not_surfaced(session: Session) -> None:
    """A healthy, recently-synced Item is not flagged."""
    _make_item(session, name="Chase", last_sync_at=_now() - timedelta(hours=1))
    result = _compute_plaid_stale_items(session, now=_now())
    assert result == []


def test_stale_item_surfaced_with_reason_stale(session: Session) -> None:
    """Item with last_sync_at > 48h ago → reason='stale'."""
    _make_item(
        session,
        name="Chase",
        last_sync_at=_now() - PLAID_STALE_THRESHOLD - timedelta(hours=1),
    )
    result = _compute_plaid_stale_items(session, now=_now())
    assert len(result) == 1
    assert result[0].reason == "stale"
    assert result[0].institution_name == "Chase"


def test_never_synced_item_surfaced_as_stale(session: Session) -> None:
    """A newly-connected Item that has not yet been synced is also flagged."""
    _make_item(session, name="NewBank", last_sync_at=None, sync_status=None)
    result = _compute_plaid_stale_items(session, now=_now())
    assert len(result) == 1
    assert result[0].reason == "stale"


def test_terminal_error_surfaced_with_reason_error(session: Session) -> None:
    """ITEM_LOGIN_REQUIRED in last_error → reason='error' (priority over 'stale')."""
    _make_item(
        session,
        name="Chase",
        sync_status="error",
        error="ITEM_LOGIN_REQUIRED",
        last_sync_at=_now() - timedelta(hours=1),  # recent, but errored
    )
    result = _compute_plaid_stale_items(session, now=_now())
    assert len(result) == 1
    assert result[0].reason == "error"
    assert result[0].last_error == "ITEM_LOGIN_REQUIRED"


def test_transient_error_not_surfaced_when_recent(session: Session) -> None:
    """INSTITUTION_DOWN is transient — not user-actionable, not flagged."""
    _make_item(
        session,
        name="Schwab",
        sync_status="institution_down",
        error="INSTITUTION_DOWN",
        last_sync_at=_now() - timedelta(hours=1),
    )
    assert _compute_plaid_stale_items(session, now=_now()) == []


def test_disconnected_item_not_surfaced(session: Session) -> None:
    """Disconnected Items are gone from the user's POV — never flag them."""
    _make_item(
        session,
        name="OldBank",
        status="disconnected",
        sync_status="error",
        error="ITEM_LOGIN_REQUIRED",
        last_sync_at=_now() - timedelta(days=5),
    )
    assert _compute_plaid_stale_items(session, now=_now()) == []


def test_placeholder_items_not_surfaced(session: Session) -> None:
    """Placeholders (in-flight Link tokens) aren't real Items — exclude them."""
    _make_item(
        session,
        name="pending",
        item_id="placeholder_inflight_xyz",
        last_sync_at=None,
    )
    assert _compute_plaid_stale_items(session, now=_now()) == []
