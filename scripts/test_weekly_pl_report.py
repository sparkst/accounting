"""Tests for ``scripts/weekly-pl-report.py`` (REQ-FIX-API-003).

Covers:
  * The report's Revenue/Expenses line ties out byte-for-byte to
    ``compute_entity_pl`` for a fixed, frozen window — the single-source-of-
    truth regression the reporting spec's WBR tie-out test depends on.
  * The footer prints the exact half-open [week_start, this_monday) window,
    independent of which day of the week the script runs on.
  * Reimbursable pairs net to zero in the printed Revenue/Expenses line.
"""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import Generator
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import src.db.connection  # noqa: F401 — registers all ORM models before create_all
from src.models.base import Base
from src.models.enums import ConfirmedBy, Direction, Entity, Source, TaxCategory, TransactionStatus
from src.models.transaction import Transaction
from src.reports.pl_engine import compute_entity_pl, week_window

# Load scripts/weekly-pl-report.py as a module (hyphenated filename).
_THIS_DIR = Path(__file__).parent
_SPEC = importlib.util.spec_from_file_location(
    "_weekly_pl_report", _THIS_DIR / "weekly-pl-report.py"
)
assert _SPEC is not None and _SPEC.loader is not None
weekly_pl = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(weekly_pl)


@pytest.fixture()
def session() -> Generator[Session, None, None]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    s = SessionLocal()
    yield s
    s.close()


def _make_tx(
    session: Session,
    *,
    amount: str,
    direction: str,
    date: str,
    entity: str = Entity.SPARKRY.value,
    status: str = TransactionStatus.CONFIRMED.value,
    description: str = "Test",
    reimbursement_link: str | None = None,
) -> Transaction:
    tx = Transaction(
        id=str(uuid.uuid4()),
        source=Source.GMAIL_N8N.value,
        source_id=str(uuid.uuid4()),
        source_hash=str(uuid.uuid4()),
        date=date,
        description=description,
        amount=Decimal(amount),
        currency="USD",
        entity=entity,
        direction=direction,
        tax_category=TaxCategory.CONSULTING_INCOME.value if direction == "income" else TaxCategory.SUPPLIES.value,
        status=status,
        confidence=0.9,
        raw_data={},
        confirmed_by=ConfirmedBy.AUTO.value,
        reimbursement_link=reimbursement_link,
    )
    session.add(tx)
    session.commit()
    session.refresh(tx)
    return tx


class _FrozenDatetime(datetime):
    """A datetime subclass whose .now() always returns a fixed instant."""

    _frozen: datetime

    @classmethod
    def now(cls, tz=None):  # noqa: ANN001, ARG003
        return cls._frozen


def _frozen_at(fixed: datetime):
    frozen_cls = type("_Frozen", (_FrozenDatetime,), {"_frozen": fixed})
    return patch.object(weekly_pl, "datetime", frozen_cls)


class TestWeeklyPLReportTiesOutToPLEngine:
    def test_revenue_expenses_line_matches_compute_entity_pl_byte_for_byte(
        self, session: Session
    ) -> None:
        # Wednesday 2026-06-10 -> window is [2026-06-01, 2026-06-08).
        fixed_now = datetime(2026, 6, 10, 9, 0, 0)
        week_start, week_end = week_window(fixed_now.date())

        _make_tx(session, amount="1234.56", direction=Direction.INCOME.value, date="2026-06-03")
        _make_tx(session, amount="-321.00", direction=Direction.EXPENSE.value, date="2026-06-04")
        # Out of window — must not affect either computation.
        _make_tx(session, amount="9999.00", direction=Direction.INCOME.value, date="2026-06-09")

        expected_pl = compute_entity_pl(session, week_start, week_end, entity=None)

        with (
            _frozen_at(fixed_now),
            patch.object(weekly_pl, "init_db", return_value=None),
            patch.object(weekly_pl, "SessionLocal", return_value=session),
            patch("src.api.routes.health._build_tax_deadlines", return_value=[]),
            patch.object(session, "close"),  # keep the fixture-owned session open
        ):
            report = weekly_pl.generate_report()

        revenue_expense_line = report.splitlines()[0]
        expected_line = (
            f"Revenue: ${float(expected_pl.revenue):,.2f} | "
            f"Expenses: ${float(expected_pl.expenses):,.2f}"
        )
        assert revenue_expense_line == expected_line

    def test_footer_prints_exact_half_open_window(self, session: Session) -> None:
        fixed_now = datetime(2026, 6, 10, 9, 0, 0)  # Wednesday
        week_start, week_end = week_window(fixed_now.date())

        with (
            _frozen_at(fixed_now),
            patch.object(weekly_pl, "init_db", return_value=None),
            patch.object(weekly_pl, "SessionLocal", return_value=session),
            patch("src.api.routes.health._build_tax_deadlines", return_value=[]),
            patch.object(session, "close"),
        ):
            report = weekly_pl.generate_report()

        footer = report.splitlines()[-1]
        assert week_start in footer
        assert week_end in footer
        assert "exclusive" in footer.lower()

    def test_reimbursable_pair_nets_to_zero_in_report(self, session: Session) -> None:
        fixed_now = datetime(2026, 6, 10, 9, 0, 0)
        income_tx = _make_tx(
            session, amount="150.00", direction=Direction.INCOME.value, date="2026-06-03",
            description="Reimbursement",
        )
        _make_tx(
            session, amount="-150.00", direction=Direction.REIMBURSABLE.value, date="2026-06-03",
            description="Reimbursable expense", reimbursement_link=income_tx.id,
        )

        with (
            _frozen_at(fixed_now),
            patch.object(weekly_pl, "init_db", return_value=None),
            patch.object(weekly_pl, "SessionLocal", return_value=session),
            patch("src.api.routes.health._build_tax_deadlines", return_value=[]),
            patch.object(session, "close"),
        ):
            report = weekly_pl.generate_report()

        revenue_expense_line = report.splitlines()[0]
        assert revenue_expense_line == "Revenue: $0.00 | Expenses: $0.00"
