"""Tests for the monthly-close CLI (REQ-MCA-001/004)."""

from __future__ import annotations

import itertools
from collections.abc import Generator
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import scripts.monthly_close as cli
import src.db.connection  # noqa: F401  (register every ORM table on Base)
from src.alerts.models import AlertDispatch
from src.models.base import Base
from src.models.enums import Direction, Entity, TaxCategory, TransactionStatus
from src.models.transaction import Transaction

_ENGINE = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
Base.metadata.create_all(_ENGINE)
_Session = sessionmaker(bind=_ENGINE)
_counter = itertools.count()
_TODAY = date(2026, 7, 7)


@pytest.fixture()
def session() -> Generator[Session, None, None]:
    s = _Session()
    yield s
    s.rollback()
    s.query(AlertDispatch).delete()
    s.query(Transaction).delete()
    s.commit()
    s.close()


def _tx(session: Session, **overrides: object) -> Transaction:
    defaults: dict[str, object] = {
        "source": "plaid",
        "source_hash": f"h-{next(_counter)}",
        "date": "2026-06-10",
        "description": "Vendor",
        "amount": Decimal("-25.00"),
        "entity": Entity.SPARKRY.value,
        "tax_category": TaxCategory.OFFICE_EXPENSE.value,
        "direction": Direction.EXPENSE.value,
        "status": TransactionStatus.CONFIRMED.value,
        "confirmed_by": "human",
        "raw_data": {},
    }
    defaults.update(overrides)
    tx = Transaction(**defaults)
    session.add(tx)
    session.flush()
    return tx


def test_dry_run_builds_report_no_ledger(session: Session) -> None:
    """REQ-MCA-001: DRY-RUN builds the report + HTML but writes no ledger row."""
    _tx(session)
    run = cli.run_close(session, month="2026-06", apply=False, today=_TODAY)
    assert run.dispatch is None
    assert "Monthly Close — 2026-06" in run.html
    assert run.report.rows_ingested == 1
    assert session.query(AlertDispatch).count() == 0


def test_apply_sends_and_records(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """REQ-MCA-001: --apply sends via Resend and records a monthly_close ledger row."""
    _tx(session)

    class _FakeEmails:
        @staticmethod
        def send(params: dict[str, object]) -> dict[str, str]:
            return {"id": "msg_1"}

    class _FakeResend:
        api_key = ""
        Emails = _FakeEmails

    monkeypatch.setitem(__import__("sys").modules, "resend", _FakeResend)
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("ALERT_TO_EMAIL", "travis@sparkry.ai")

    run = cli.run_close(session, month="2026-06", apply=True, today=_TODAY)
    session.commit()
    assert run.dispatch is not None
    assert run.dispatch.alert_type == "monthly_close"
    assert run.dispatch.status == "sent"
    assert session.query(AlertDispatch).count() == 1
