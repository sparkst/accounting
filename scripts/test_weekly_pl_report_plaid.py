"""Tests for the Plaid stale-Item section of weekly-pl-report.py (REQ-027)."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

# Plaid + brokerage models register on metadata when imported.
import src.models.audit_event  # noqa: F401
import src.models.brokerage  # noqa: F401
from src.models.base import Base
from src.models.plaid import PlaidItem
from src.utils.plaid_crypto import encrypt_token


def _load_weekly_module() -> Any:
    """Load scripts/weekly-pl-report.py as an importable module despite the dash in the name."""
    path = Path(__file__).resolve().parent / "weekly-pl-report.py"
    spec = importlib.util.spec_from_file_location("weekly_pl_report", path)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _enc_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from cryptography.fernet import Fernet

    monkeypatch.setenv("PLAID_TOKEN_ENC_KEY", Fernet.generate_key().decode())


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn: Any, _: Any) -> None:
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return Session(bind=engine)


def _make_item(
    session: Session,
    *,
    name: str,
    status: str = "active",
    sync_status: str | None = None,
    error: str | None = None,
) -> PlaidItem:
    item = PlaidItem(
        item_id=f"plaid_{name.lower()}",
        institution_id="ins_x",
        institution_name=name,
        access_token_encrypted=encrypt_token("access-sb-test"),
        status=status,
        last_sync_status=sync_status,
        last_error=error,
        last_sync_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=24),
    )
    session.add(item)
    session.commit()
    return item


def test_returns_terminal_error_items(session: Session) -> None:
    mod = _load_weekly_module()
    _make_item(session, name="Chase", sync_status="error", error="ITEM_LOGIN_REQUIRED")
    _make_item(session, name="Vanguard", sync_status="error", error="INVALID_CREDENTIALS")

    lines = mod._check_plaid_stale_items(session)
    assert len(lines) == 2
    assert any("Chase" in line and "ITEM_LOGIN_REQUIRED" in line for line in lines)
    assert any("Vanguard" in line and "INVALID_CREDENTIALS" in line for line in lines)


def test_excludes_transient_errors(session: Session) -> None:
    """REQ-027: INSTITUTION_DOWN and RATE_LIMIT are transient — not user-actionable."""
    mod = _load_weekly_module()
    _make_item(session, name="Schwab", sync_status="institution_down", error="INSTITUTION_DOWN")
    _make_item(session, name="Fidelity", sync_status="error", error="RATE_LIMIT_EXCEEDED")
    assert mod._check_plaid_stale_items(session) == []


def test_excludes_healthy_items(session: Session) -> None:
    mod = _load_weekly_module()
    _make_item(session, name="Chase", sync_status="ok", error=None)
    assert mod._check_plaid_stale_items(session) == []


def test_excludes_disconnected_items(session: Session) -> None:
    """A disconnected Item doesn't need re-link — the user already deleted it."""
    mod = _load_weekly_module()
    _make_item(
        session, name="OldBank", status="disconnected", sync_status="error", error="ITEM_LOGIN_REQUIRED"
    )
    assert mod._check_plaid_stale_items(session) == []


def test_no_plaid_items_returns_empty(session: Session) -> None:
    mod = _load_weekly_module()
    assert mod._check_plaid_stale_items(session) == []


def test_swallows_db_errors_gracefully(session: Session) -> None:
    """Best-effort: a Plaid query error should not break the entire weekly report."""
    mod = _load_weekly_module()
    session.close()  # closed session → queries fail
    assert mod._check_plaid_stale_items(session) == []
