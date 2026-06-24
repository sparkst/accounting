"""Tests for the North American Builder Plus IUL balance importer.

REQ-IUL-001  Upsert the IUL Account (broker=north_american, type=other,
             entity=personal, tax_sheltered=True) and book a balance snapshot.
REQ-IUL-002  Book the SURRENDER value when provided; fall back to the
             accumulation value with a loud warning (overstates liquidation).
REQ-IUL-003  DRY-RUN default never writes; re-import is idempotent (dedup hash).
REQ-IUL-004  Decimal precision preserved end-to-end (no float corruption).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from src.adapters.north_american_iul import import_policy
from src.models.base import Base
from src.models.brokerage import Account
from src.models.enums import AccountType, Broker, Entity
from src.models.history import AccountBalanceSnapshot
from src.models.ingestion_log import IngestionLog  # noqa: F401 — register table for create_all
from src.models.plaid import (
    PlaidItem,  # noqa: F401 — Account FKs plaid_item; register for create_all
)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


_POLICY = "NA-IUL-0001"
_AS_OF = date(2026, 6, 24)


def test_dry_run_writes_nothing(session: Session) -> None:
    """REQ-IUL-003: dry-run parses/validates but never touches the DB."""
    result = import_policy(
        policy_number=_POLICY,
        as_of=_AS_OF,
        surrender_value=Decimal("450000.00"),
        dry_run=True,
    )
    assert result.errors == []
    assert session.scalars(select(Account)).all() == []
    assert session.scalars(select(AccountBalanceSnapshot)).all() == []


def test_apply_upserts_account_and_books_surrender_value(session: Session) -> None:
    """REQ-IUL-001 + REQ-IUL-002: account created, surrender value booked."""
    result = import_policy(
        policy_number=_POLICY,
        as_of=_AS_OF,
        surrender_value=Decimal("450000.00"),
        accumulation_value=Decimal("466928.72"),
        beneficiary="Amy Sparks",
        account_name="North American Builder Plus IUL4 — Travis",
        dry_run=False,
        session=session,
    )
    assert result.imported == 1
    assert result.errors == []

    acct = session.scalars(select(Account)).one()
    assert acct.broker == Broker.NORTH_AMERICAN.value
    assert acct.account_number == _POLICY
    assert acct.account_type == AccountType.OTHER.value
    assert acct.entity == Entity.PERSONAL.value
    assert acct.tax_sheltered is True
    assert acct.beneficiary == "Amy Sparks"

    snap = session.scalars(select(AccountBalanceSnapshot)).one()
    assert snap.account_id == acct.id
    assert snap.as_of == _AS_OF
    # REQ-IUL-002: surrender value wins over accumulation value.
    assert snap.balance == Decimal("450000.00")


def test_falls_back_to_accumulation_with_warning(session: Session) -> None:
    """REQ-IUL-002: no surrender value → book accumulation, warn loudly."""
    result = import_policy(
        policy_number=_POLICY,
        as_of=_AS_OF,
        accumulation_value=Decimal("466928.72"),
        dry_run=False,
        session=session,
    )
    assert result.imported == 1
    snap = session.scalars(select(AccountBalanceSnapshot)).one()
    assert snap.balance == Decimal("466928.72")
    assert any("surrender" in w.lower() for w in result.warnings)


def test_requires_some_value(session: Session) -> None:
    """Neither surrender nor accumulation → a clean error, no write."""
    result = import_policy(
        policy_number=_POLICY, as_of=_AS_OF, dry_run=False, session=session
    )
    assert result.imported == 0
    assert result.errors
    assert session.scalars(select(AccountBalanceSnapshot)).all() == []


def test_reimport_is_idempotent(session: Session) -> None:
    """REQ-IUL-003: same policy/date/balance re-import is a dedup no-op."""
    kw = dict(
        policy_number=_POLICY,
        as_of=_AS_OF,
        surrender_value=Decimal("450000.00"),
        dry_run=False,
        session=session,
    )
    first = import_policy(**kw)
    second = import_policy(**kw)
    assert first.imported == 1
    assert second.imported == 0
    assert second.dup_skipped == 1
    assert len(session.scalars(select(AccountBalanceSnapshot)).all()) == 1
    # The account is reused, not duplicated.
    assert len(session.scalars(select(Account)).all()) == 1


def test_string_input_preserves_precision(session: Session) -> None:
    """REQ-IUL-004: '$466,928.72' parses to the exact Decimal, no float drift."""
    result = import_policy(
        policy_number=_POLICY,
        as_of=_AS_OF,
        surrender_value="$466,928.72",
        dry_run=False,
        session=session,
    )
    snap = session.scalars(select(AccountBalanceSnapshot)).one()
    assert snap.balance == Decimal("466928.72")
    assert result.errors == []
