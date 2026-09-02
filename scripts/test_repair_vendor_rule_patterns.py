"""Tests for the vendor-rule pattern repair CLI.

REQ-ID: REQ-FIX-ING-023  A DRY-RUN-default CLI actually repairs the 34 poisoned
                          production rows. qreview P1-c3d: the library function
                          existed with no caller, so shipping the fix repaired
                          nothing.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from scripts.repair_vendor_rule_patterns import repair
from src.models.base import Base
from src.models.enums import Direction, Entity, TaxCategory
from src.models.vendor_rule import VendorRule

POISONED = "cardinal.*health|fascinate.*os"


@pytest.fixture(scope="function")
def session() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    SessionCls = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    s = SessionCls()
    yield s
    s.close()
    engine.dispose()


def _rule(session: Session, pattern: str, *, is_regex: bool = False) -> VendorRule:
    rule = VendorRule(
        vendor_pattern=pattern,
        is_regex=is_regex,
        entity=Entity.SPARKRY.value,
        tax_category=TaxCategory.CONSULTING_INCOME.value,
        direction=Direction.INCOME.value,
        examples=10,
        confidence=0.97,
    )
    session.add(rule)
    session.commit()
    return rule


def test_dry_run_is_the_default_and_writes_nothing(session: Session) -> None:
    rule = _rule(session, POISONED)
    result = repair(session)
    assert result.dry_run is True
    assert result.repaired == 1
    session.expire_all()
    assert session.query(VendorRule).one().is_regex is False
    assert rule.vendor_pattern == POISONED


def test_apply_commits_the_flip(session: Session) -> None:
    _rule(session, POISONED)
    result = repair(session, dry_run=False)
    assert result.repaired == 1
    session.expire_all()
    assert session.query(VendorRule).one().is_regex is True


def test_apply_is_idempotent(session: Session) -> None:
    _rule(session, POISONED)
    repair(session, dry_run=False)
    second = repair(session, dry_run=False)
    assert second.repaired == 0


def test_real_descriptors_are_never_flipped(session: Session) -> None:
    for pattern in ("SQ *COFFEE SHOP", "A.B Corp (West)", "Anthropic Headquarters"):
        _rule(session, pattern)
    result = repair(session, dry_run=False)
    assert result.repaired == 0
    session.expire_all()
    assert all(r.is_regex is False for r in session.query(VendorRule).all())


def test_uncompilable_pattern_is_reported_not_flipped(session: Session) -> None:
    _rule(session, r"cardinal.*health|fascinate(os")
    result = repair(session, dry_run=False)
    assert result.repaired == 0
    assert result.skipped == 1
    session.expire_all()
    assert session.query(VendorRule).one().is_regex is False
