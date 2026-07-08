"""Tests for the local account_alias seeder (REQ-FIX-WLT-004)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from scripts.seed_local_account_aliases import seed_aliases
from src.models import brokerage as _b  # noqa: F401
from src.models import history as _h  # noqa: F401
from src.models import plaid as _p  # noqa: F401
from src.models.base import Base
from src.models.brokerage import Account
from src.models.enums import AccountType, Broker, Entity
from src.models.history import AccountAlias


def _session() -> Any:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _acct(s: Any) -> Account:
    a = Account(
        broker=Broker.VANGUARD.value, account_number="1", account_name="Amy IRA",
        account_type=AccountType.TRAD_IRA.value, entity=Entity.PERSONAL.value,
    )
    s.add(a)
    s.flush()
    return a


def test_dry_run_writes_nothing() -> None:
    s = _session()
    a = _acct(s)
    s.commit()
    entries = [{"raw_account_name": "Legacy Amy", "account_id": a.id}]
    summary = seed_aliases(s, entries, apply=False)
    assert summary.inserted == 1
    assert s.query(AccountAlias).count() == 0  # dry-run wrote nothing


def test_apply_lowercases_and_resolves_by_name() -> None:
    s = _session()
    _acct(s)
    s.commit()
    entries = [{"raw_account_name": "LEGACY AMY", "broker": "vanguard", "account_name": "Amy IRA"}]
    summary = seed_aliases(s, entries, apply=True)
    assert summary.inserted == 1
    row = s.query(AccountAlias).one()
    assert row.raw_account_name == "legacy amy"  # stored lowercased


def test_idempotent_and_unresolved() -> None:
    s = _session()
    a = _acct(s)
    s.commit()
    entries = [
        {"raw_account_name": "Legacy Amy", "account_id": a.id},
        {"raw_account_name": "Ghost", "account_id": "does-not-exist"},
    ]
    seed_aliases(s, entries, apply=True)
    again = seed_aliases(s, entries, apply=True)
    assert again.skipped == 1  # existing alias left untouched
    assert "ghost" in again.unresolved
    assert s.query(AccountAlias).count() == 1
