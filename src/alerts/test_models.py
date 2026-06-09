"""Tests for the AlertDispatch ledger model.

REQ-ID: REQ-ALERT-006 (dedup uniqueness)
REQ-ID: REQ-ALERT-010 (table shape)
"""

from __future__ import annotations

from collections.abc import Generator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from src.alerts.models import AlertDispatch
from src.models.base import Base

_ENGINE = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
Base.metadata.create_all(_ENGINE)
_Session = sessionmaker(bind=_ENGINE)


@pytest.fixture()
def session() -> Generator[Session, None, None]:
    s = _Session()
    yield s
    s.query(AlertDispatch).delete()
    s.commit()
    s.close()


def test_alert_dispatch_persists_and_autofills(session: Session) -> None:
    row = AlertDispatch(
        alert_key="tax:sparkry:bo:2026-04",
        occurrence_date="2026-05-10",
        alert_type="tax_bo",
        entity="sparkry",
        subject="WA B&O due — Sparkry LLC",
        status="sent",
        http_status=200,
    )
    session.add(row)
    session.commit()
    assert row.id  # uuid autofilled
    assert row.created_at  # timestamp autofilled


def test_alert_key_occurrence_date_is_unique(session: Session) -> None:
    common = dict(
        alert_key="tax:sparkry:bo:2026-04",
        occurrence_date="2026-05-10",
        alert_type="tax_bo",
        entity="sparkry",
        subject="dup",
        status="sent",
    )
    session.add(AlertDispatch(**common))
    session.commit()
    session.add(AlertDispatch(**common))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
