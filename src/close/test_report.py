"""Tests for CloseReport assembly + evidence links (REQ-MCA-001, spec §1.4)."""

from __future__ import annotations

import itertools
from collections.abc import Generator
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import src.db.connection  # noqa: F401  (register every ORM table on Base)
from src.close.report import (
    DATA_HYGIENE_CALLOUTS,
    account_link,
    build_close_report,
    needs_review_link,
    vendor_link,
)
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


def test_link_helpers() -> None:
    """REQ-MCA-001: evidence links target the documented dashboard routes."""
    assert needs_review_link("sparkry") == (
        "https://books.sparkry.ai/?status=needs_review&entity=sparkry"
    )
    assert vendor_link("acme co", "2026-06") == (
        "https://books.sparkry.ai/transactions?vendor=acme%20co&month=2026-06"
    )
    assert account_link("abc-123") == "https://books.sparkry.ai/wealth/accounts/abc-123"


def test_build_close_report_kpis(session: Session) -> None:
    """REQ-MCA-001: header KPIs count in-month rows, auto-confirms, and backlog."""
    _tx(session, description="Alpha")
    _tx(session, description="Beta", confirmed_by="auto:rule:r1", amount=Decimal("-9.00"))
    _tx(
        session, description="Gamma", status=TransactionStatus.NEEDS_REVIEW.value,
        confirmed_by="auto",
    )
    # Out-of-month row is excluded from rows_ingested.
    _tx(session, description="Old", date="2026-05-01")

    report = build_close_report(session, "2026-06", today=_TODAY)
    assert report.month == "2026-06"
    assert report.rows_ingested == 3  # three June rows
    assert report.autoconfirm.total == 1
    assert report.autoconfirm.by_vendor[0].vendor == "Beta"
    assert report.needs_review_depth == 1


def test_data_hygiene_callouts_present(session: Session) -> None:
    """REQ-FIX-DAT-002: the two report-only data-hygiene lines are always included."""
    report = build_close_report(session, "2026-06", today=_TODAY)
    assert "Vanguard $0 stub (named 2026-07-08) — confirm archive" in report.data_hygiene
    assert "$50 Fidelity TOD — human closure decision" in report.data_hygiene
    assert report.data_hygiene == list(DATA_HYGIENE_CALLOUTS)


def test_build_close_report_includes_use_tax_section(session: Session) -> None:
    """REQ-UTX-005 (#59): the close report carries the comped-order use-tax section.

    A confirmed $0 BlackLine Shopify comp in-quarter surfaces in the section for
    the quarter containing the close month. No config/use_tax.yaml exists in the
    test tree, so the estimate degrades to UNAVAILABLE while the counts render.
    """
    _tx(
        session,
        description="Comp #1017",
        source="shopify",
        entity=Entity.BLACKLINE.value,
        amount=Decimal("0.00"),
        status=TransactionStatus.CONFIRMED.value,
        raw_data={"id": 1017, "line_items": [{"quantity": 2}]},
    )
    report = build_close_report(session, "2026-06", today=_TODAY)
    assert report.use_tax_text is not None
    assert "WAC 458-20-178" in report.use_tax_text
    assert "Q2 2026" in report.use_tax_text
    assert "1" in report.use_tax_text  # one confirmed comp order
    assert "UNAVAILABLE" in report.use_tax_text  # no config in the test tree
