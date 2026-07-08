"""Tests for the concentration-drift dispatcher (REQ-IPD-004)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from scripts import policy_drift_dispatch as mod
from src.alerts.models import AlertDispatch
from src.alerts.webhook import WebhookResult
from src.models import brokerage as _b  # noqa: F401
from src.models import history as _h  # noqa: F401
from src.models import plaid as _p  # noqa: F401
from src.models.base import Base
from src.models.brokerage import Account, PositionSnapshot
from src.models.enums import AccountType, Broker, Entity


def _session() -> Any:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _seed_over_glide(s: Any) -> None:
    """AMZN+MSFT at 60% at the 2026-07 baseline (glide 51) → +9 pts drift."""
    a = Account(
        broker=Broker.SCHWAB.value, account_number="A",
        account_type=AccountType.TAXABLE.value, entity=Entity.PERSONAL.value,
    )
    s.add(a)
    s.flush()
    for sym, mv in [("AMZN", "6000"), ("VTI", "4000")]:
        s.add(PositionSnapshot(
            account_id=a.id, as_of=datetime(2026, 7, 1), symbol=sym,
            quantity=Decimal("1"), market_value=Decimal(mv),
            source_file="f", source_row_hash=f"{sym}", raw_data={},
        ))
    s.commit()


def test_no_drift_no_alert() -> None:
    """REQ-IPD-004: at/under threshold → no alert fired."""
    s = _session()
    a = Account(
        broker=Broker.SCHWAB.value, account_number="A",
        account_type=AccountType.TAXABLE.value, entity=Entity.PERSONAL.value,
    )
    s.add(a)
    s.flush()
    # 50% combined vs glide 51 → drift −1 → no alert.
    for sym, mv in [("AMZN", "5000"), ("VTI", "5000")]:
        s.add(PositionSnapshot(
            account_id=a.id, as_of=datetime(2026, 7, 1), symbol=sym,
            quantity=Decimal("1"), market_value=Decimal(mv),
            source_file="f", source_row_hash=sym, raw_data={},
        ))
    s.commit()
    summary = mod.dispatch_policy_drift(date(2026, 7, 1), s, apply=False)
    assert summary.fired is False
    assert summary.status == "no_drift"


def test_dry_run_writes_nothing() -> None:
    """REQ-IPD-004: DRY-RUN default builds no ledger row and makes no POST."""
    s = _session()
    _seed_over_glide(s)
    summary = mod.dispatch_policy_drift(date(2026, 7, 1), s, apply=False)
    assert summary.fired is True
    assert summary.status == "dry_run"
    assert s.query(AlertDispatch).count() == 0


def test_apply_persists_payload_and_dedups(monkeypatch: Any) -> None:
    """REQ-IPD-004 + REQ-FIX-ALR-002: apply POSTs, persists payload_json +
    delivery_channel, and dedups to one per calendar month."""
    s = _session()
    _seed_over_glide(s)
    monkeypatch.setattr(
        mod, "post_payload", lambda *a, **k: WebhookResult("sent", 200, None)
    )
    first = mod.dispatch_policy_drift(date(2026, 7, 1), s, apply=True)
    assert first.status == "sent"
    row = s.query(AlertDispatch).one()
    assert row.alert_key == "policy_drift:2026-07"
    assert row.delivery_channel == "n8n_webhook"
    assert row.payload_json is not None and "info" in row.payload_json

    # Second run same month → skipped (already sent), no duplicate row.
    second = mod.dispatch_policy_drift(date(2026, 7, 15), s, apply=True)
    assert second.status == "skipped"
    assert s.query(AlertDispatch).count() == 1


def test_apply_failure_records_for_retry(monkeypatch: Any) -> None:
    """REQ-FIX-ALR-002: a failed POST still persists payload_json so the sweep
    can retry it (delivery_channel=n8n_webhook)."""
    s = _session()
    _seed_over_glide(s)
    monkeypatch.setattr(
        mod, "post_payload", lambda *a, **k: WebhookResult("failed", None, "boom")
    )
    summary = mod.dispatch_policy_drift(date(2026, 7, 1), s, apply=True)
    assert summary.status == "failed"
    row = s.query(AlertDispatch).one()
    assert row.status == "failed"
    assert row.payload_json is not None
    assert row.delivery_channel == "n8n_webhook"
