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
from src.classification.rules import ENTITY_TYPE_VENDOR_RULE
from src.models.audit_event import AuditEvent
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


# ── Audit trail (qreview P2-d1e / P2-002) ────────────────────────────────────
# The library function is unit-tested for the audit write in
# test_rules_pattern_integrity.py; these assert the actual production
# entrypoint (the committed CLI wrapper) persists the audit rows and honors
# --changed-by, since that is what will run against the real 34 dead rules.


def test_apply_via_cli_wrapper_persists_an_audit_row(session: Session) -> None:
    rule = _rule(session, POISONED)
    repair(session, dry_run=False, changed_by="human:travis@blacklinemtb.com")
    session.expire_all()
    audits = session.query(AuditEvent).all()
    assert len(audits) == 1
    a = audits[0]
    assert a.entity_id == rule.id
    assert a.entity_type == ENTITY_TYPE_VENDOR_RULE
    assert a.field_changed == "is_regex"
    assert (a.old_value, a.new_value) == ("False", "True")
    assert a.changed_by == "human:travis@blacklinemtb.com"


def test_default_changed_by_names_a_human_not_a_fictional_cron(
    session: Session,
) -> None:
    """P2-001: no cron/timer runs this script, so the default actor must not
    claim one — an audit row that lies about who ran it defeats the purpose."""
    _rule(session, POISONED)
    repair(session, dry_run=False)
    session.expire_all()
    changed_by = session.query(AuditEvent).one().changed_by
    assert not changed_by.startswith("cron:")
    assert changed_by == "human:operator"


def test_dry_run_via_cli_wrapper_writes_no_audit_row(session: Session) -> None:
    _rule(session, POISONED)
    repair(session, dry_run=True)
    session.expire_all()
    assert session.query(AuditEvent).count() == 0
