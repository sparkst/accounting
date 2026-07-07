"""Tests for scripts/prune_dead_plaid_items.py (REQ-FIX-PLD-004 one-time data fix)."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from scripts.prune_dead_plaid_items import prune_dead_items
from src.models.base import Base
from src.models.plaid import REVOKED_TOKEN_SENTINEL, PlaidItem


@pytest.fixture()
def engine() -> Generator[Any, None, None]:
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(e)
    yield e
    e.dispose()


@pytest.fixture()
def session(engine: Any) -> Generator[Session, None, None]:
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    s = SessionLocal()
    yield s
    s.close()


def _placeholder_item(session: Session, *, item_id: str, status: str = "active") -> PlaidItem:
    item = PlaidItem(
        item_id=item_id,
        institution_id="ins_9",
        institution_name="Dead Bank",
        access_token_encrypted="undecryptable-garbage",
        status=status,
    )
    session.add(item)
    session.commit()
    return item


def test_dry_run_finds_but_does_not_write(session: Session) -> None:
    _placeholder_item(session, item_id="placeholder_abc123")
    items = prune_dead_items(session, apply=False)
    assert len(items) == 1
    session.refresh(items[0])
    assert items[0].status == "active"
    assert items[0].access_token_encrypted == "undecryptable-garbage"


def test_apply_disconnects_and_revokes_token(session: Session) -> None:
    _placeholder_item(session, item_id="placeholder_xyz789")
    items = prune_dead_items(session, apply=True)
    assert len(items) == 1
    row = session.query(PlaidItem).filter_by(item_id="placeholder_xyz789").one()
    assert row.status == "disconnected"
    assert row.access_token_encrypted == REVOKED_TOKEN_SENTINEL
    assert row.last_error is not None


def test_never_deletes_the_row(session: Session) -> None:
    _placeholder_item(session, item_id="placeholder_keep_me")
    prune_dead_items(session, apply=True)
    assert session.query(PlaidItem).filter_by(item_id="placeholder_keep_me").count() == 1


def test_non_placeholder_active_items_untouched(session: Session) -> None:
    real_item = PlaidItem(
        item_id="plaid_real_chase_123",
        institution_id="ins_3",
        institution_name="Chase",
        access_token_encrypted="fine",
        status="active",
    )
    session.add(real_item)
    session.commit()
    items = prune_dead_items(session, apply=True)
    assert items == []
    session.refresh(real_item)
    assert real_item.status == "active"


def test_already_disconnected_placeholder_not_reprocessed(session: Session) -> None:
    _placeholder_item(session, item_id="placeholder_done", status="disconnected")
    items = prune_dead_items(session, apply=True)
    assert items == []
