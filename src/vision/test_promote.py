"""Tests for the vision promotion ledger + qdecide-gated flip.

REQ-VIS-003: promotion only after 3 consecutive equal-or-better cycles, via a
manual qdecide-gated command; legacy stays fallback; NO auto-flip.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from src.db.connection import _configure_sqlite
from src.models import transaction as _transaction  # noqa: F401 — register tables
from src.models.audit_event import AuditEvent
from src.models.base import Base
from src.models.vision_promotion import VisionPromotion
from src.vision.diff import DiffReport, diff_fields
from src.vision.promote import (
    ENTITY_TYPE_VISION_PROMOTION,
    is_eligible,
    promote,
    record_cycle,
)


@pytest.fixture()
def session() -> Generator[Session, None, None]:
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    event.listen(eng, "connect", _configure_sqlite)
    Base.metadata.create_all(bind=eng)
    s = sessionmaker(bind=eng)()
    try:
        yield s
    finally:
        s.close()
        eng.dispose()


def _clean() -> DiffReport:
    return diff_fields({"a": "1"}, {"a": "1"})


def _clean_with_extra() -> DiffReport:
    """Equal-or-better: a vision_only extra, no legacy_only miss."""
    return diff_fields({"a": "1"}, {"a": "1", "extra": "x"})


def _dirty() -> DiffReport:
    return diff_fields({"a": "1"}, {"a": "2"})


def test_clean_cycle_increments(session: Session) -> None:
    """REQ-VIS-003: a clean cycle increments consecutive_clean."""
    row = record_cycle(session, "fg", _clean(), "/tmp/r1.json")
    assert row.consecutive_clean == 1
    row = record_cycle(session, "fg", _clean(), "/tmp/r2.json")
    assert row.consecutive_clean == 2
    assert row.last_report_path == "/tmp/r2.json"
    assert row.last_cycle_at is not None


def test_vision_only_extra_counts_as_clean(session: Session) -> None:
    """REQ-VIS-003: equal-or-better (vision_only extra) still counts as clean."""
    record_cycle(session, "fg", _clean(), None)
    row = record_cycle(session, "fg", _clean_with_extra(), None)
    assert row.consecutive_clean == 2


def test_dirty_cycle_resets(session: Session) -> None:
    """REQ-VIS-003: any dirty run resets the counter to 0."""
    record_cycle(session, "fg", _clean(), None)
    record_cycle(session, "fg", _clean(), None)
    row = record_cycle(session, "fg", _dirty(), None)
    assert row.consecutive_clean == 0


def test_no_auto_flip_even_at_threshold(session: Session) -> None:
    """REQ-VIS-003: record_cycle NEVER sets promoted, even at count >= 3."""
    row = None
    for _ in range(5):
        row = record_cycle(session, "fg", _clean(), None)
    assert row is not None
    assert row.consecutive_clean == 5
    assert row.promoted is False
    assert is_eligible(row) is True
    # No AuditEvent written by record_cycle (only promote writes one).
    assert session.query(AuditEvent).count() == 0


def test_promote_flips_and_audits(session: Session) -> None:
    """REQ-VIS-003: manual promote sets promoted + writes an entity-mode AuditEvent."""
    for _ in range(3):
        record_cycle(session, "fg", _clean(), None)
    row = promote(session, "fg", decision_ref="qd-123")
    assert row.promoted is True
    assert row.decision_ref == "qd-123"
    assert row.promoted_at is not None

    events = session.query(AuditEvent).filter_by(
        entity_type=ENTITY_TYPE_VISION_PROMOTION, entity_id="fg"
    ).all()
    assert len(events) == 1
    assert events[0].field_changed == "promoted"
    assert events[0].old_value == "False"
    assert events[0].new_value == "True"
    assert events[0].changed_by == "human"
    assert events[0].transaction_id is None


def test_revoke_demotes_and_audits(session: Session) -> None:
    """REQ-VIS-003: --revoke clears promoted and writes its own AuditEvent."""
    promote(session, "fg", decision_ref="qd-1")
    row = promote(session, "fg", decision_ref="qd-2", revoke=True)
    assert row.promoted is False
    events = session.query(AuditEvent).filter_by(
        entity_type=ENTITY_TYPE_VISION_PROMOTION
    ).all()
    assert len(events) == 2
    assert events[-1].new_value == "False"


def test_promote_persists_to_table(session: Session) -> None:
    """REQ-VIS-003: promotion is durable in vision_promotion."""
    promote(session, "gsk", decision_ref="qd-9")
    session.commit()
    row = session.get(VisionPromotion, "gsk")
    assert row is not None
    assert row.promoted is True
