"""Tests for scripts/seed_account_tags.py."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from scripts.seed_account_tags import default_tags_for, seed_tags
from src.models.base import Base
from src.models.brokerage import Account
from src.models.history import AccountTag


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _):  # type: ignore[no-untyped-def]
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return Session(bind=engine)


def _make_account(
    session: Session,
    *,
    broker: str = "vanguard",
    account_number: str = "X1",
    account_type: str = "taxable",
    beneficiary: str | None = None,
) -> Account:
    a = Account(
        broker=broker,
        account_number=account_number,
        account_type=account_type,
        entity="personal",
        tax_sheltered=False,
        beneficiary=beneficiary,
    )
    session.add(a)
    session.flush()
    return a


def test_default_tags_for_401k() -> None:
    a = Account(
        broker="fidelity", account_number="A1", account_type="401k",
        entity="personal", tax_sheltered=True,
    )
    assert default_tags_for(a) == ["401k", "retirement"]


def test_default_tags_for_roth_ira() -> None:
    a = Account(
        broker="vanguard", account_number="A1", account_type="roth_ira",
        entity="personal", tax_sheltered=True,
    )
    assert default_tags_for(a) == ["retirement", "roth_ira"]


def test_default_tags_for_hsa() -> None:
    a = Account(
        broker="fidelity", account_number="A1", account_type="hsa",
        entity="personal", tax_sheltered=True,
    )
    assert default_tags_for(a) == ["hsa", "retirement", "tax-advantaged"]


def test_default_tags_for_529_with_beneficiary() -> None:
    a = Account(
        broker="vanguard", account_number="A1", account_type="529",
        entity="personal", tax_sheltered=True, beneficiary="Aiden",
    )
    assert default_tags_for(a) == ["529", "aiden", "tax-advantaged"]


def test_default_tags_for_529_emerson_lowercase() -> None:
    a = Account(
        broker="vanguard", account_number="A1", account_type="529",
        entity="personal", tax_sheltered=True, beneficiary="EMERSON",
    )
    assert default_tags_for(a) == ["529", "emerson", "tax-advantaged"]


def test_default_tags_for_taxable() -> None:
    a = Account(
        broker="schwab", account_number="A1", account_type="taxable",
        entity="personal", tax_sheltered=False,
    )
    assert default_tags_for(a) == ["taxable"]


def test_default_tags_for_rsu() -> None:
    a = Account(
        broker="schwab", account_number="A1", account_type="rsu",
        entity="personal", tax_sheltered=False,
    )
    assert default_tags_for(a) == ["rsu", "taxable"]


def test_seed_tags_dry_run_writes_nothing(session: Session) -> None:
    _make_account(session, account_type="401k")
    counts = seed_tags(session, dry_run=True)
    assert counts["would_insert"] >= 1
    assert counts["inserted"] == 0
    assert session.query(AccountTag).count() == 0


def test_seed_tags_apply_writes_correct_count(session: Session) -> None:
    a1 = _make_account(session, account_number="A1", account_type="roth_ira")
    a2 = _make_account(
        session, account_number="A2", account_type="529", beneficiary="Aiden"
    )
    counts = seed_tags(session, dry_run=False)
    assert counts["inserted"] == 5  # 2 for roth_ira, 3 for 529-aiden
    tags_a1 = {t.tag for t in session.query(AccountTag).filter_by(account_id=a1.id)}
    assert tags_a1 == {"retirement", "roth_ira"}
    tags_a2 = {t.tag for t in session.query(AccountTag).filter_by(account_id=a2.id)}
    assert tags_a2 == {"529", "aiden", "tax-advantaged"}


def test_seed_tags_idempotent(session: Session) -> None:
    _make_account(session, account_type="401k")
    seed_tags(session, dry_run=False)
    counts2 = seed_tags(session, dry_run=False)
    assert counts2["inserted"] == 0
    assert counts2["dup_skipped"] >= 2
