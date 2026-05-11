"""Tests for the AuditEvent CHECK constraint (REQ-029).

The migration adds a CHECK that enforces exactly-one-of
(transaction_id, entity_id+entity_type). All other Plaid tests happen to write
the entity-mode shape correctly, so the CHECK is never exercised. These tests
directly assert the constraint fires when violated.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

# Register every model so create_all builds the dependent FKs.
import src.models.brokerage  # noqa: F401
import src.models.plaid  # noqa: F401
import src.models.transaction  # noqa: F401
from src.models.audit_event import AuditEvent
from src.models.base import Base


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _fk(c: Any, _: Any) -> None:
        c.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return Session(bind=engine)


def _audit_kwargs(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": str(uuid.uuid4()),
        "field_changed": "test_field",
        "old_value": "before",
        "new_value": "after",
        "changed_by": "human",
        "changed_at": datetime.now(UTC).replace(tzinfo=None),
    }
    base.update(overrides)
    return base


def test_entity_mode_row_succeeds(session: Session) -> None:
    """Baseline: a row with entity_id + entity_type and NULL transaction_id is valid."""
    row = AuditEvent(
        **_audit_kwargs(
            transaction_id=None,
            entity_id=str(uuid.uuid4()),
            entity_type="plaid_item",
        )
    )
    session.add(row)
    session.commit()
    assert session.query(AuditEvent).count() == 1


def test_both_targets_set_raises(session: Session) -> None:
    """A row with BOTH transaction_id AND entity_id violates the exactly-one-of CHECK."""
    row = AuditEvent(
        **_audit_kwargs(
            transaction_id=str(uuid.uuid4()),
            entity_id=str(uuid.uuid4()),
            entity_type="plaid_item",
        )
    )
    session.add(row)
    with pytest.raises(IntegrityError):
        session.commit()


def test_neither_target_set_raises(session: Session) -> None:
    """A row with NEITHER transaction_id nor entity_id violates the CHECK."""
    row = AuditEvent(
        **_audit_kwargs(transaction_id=None, entity_id=None, entity_type=None)
    )
    session.add(row)
    with pytest.raises(IntegrityError):
        session.commit()


def test_entity_id_without_type_raises(session: Session) -> None:
    """entity_id set but entity_type NULL: the CHECK requires BOTH for entity mode."""
    row = AuditEvent(
        **_audit_kwargs(
            transaction_id=None, entity_id=str(uuid.uuid4()), entity_type=None
        )
    )
    session.add(row)
    with pytest.raises(IntegrityError):
        session.commit()


def test_entity_type_without_id_raises(session: Session) -> None:
    """entity_type set but entity_id NULL is also invalid."""
    row = AuditEvent(
        **_audit_kwargs(transaction_id=None, entity_id=None, entity_type="plaid_item")
    )
    session.add(row)
    with pytest.raises(IntegrityError):
        session.commit()
