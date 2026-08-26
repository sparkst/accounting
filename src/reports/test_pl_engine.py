"""Tests for src/reports/pl_engine.py — canonical entity P&L computation.

REQ-ID: REQ-FIX-API-003  Weekly P&L nets reimbursable pairs out of
revenue/expense and reports an exact 7-day [Mon, Mon) window regardless of
run day. This is the single-source-of-truth regression the reporting
spec's WBR tie-out test depends on.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import src.db.connection  # noqa: F401 — registers all ORM models before create_all
from src.models.base import Base
from src.models.enums import ConfirmedBy, Direction, Entity, Source, TaxCategory, TransactionStatus
from src.models.transaction import Transaction
from src.reports.pl_engine import EntityPL, compute_entity_pl, week_window


@pytest.fixture()
def session():
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
    entity: str = Entity.SPARKRY.value,
    status: str = TransactionStatus.CONFIRMED.value,
    date: str = "2026-06-10",
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


# ---------------------------------------------------------------------------
# week_window
# ---------------------------------------------------------------------------


class TestWeekWindow:
    def test_wednesday_window_is_exact_prior_monday_to_this_monday(self) -> None:
        # 2026-06-10 is a Wednesday.
        wednesday = date(2026, 6, 10)
        week_start, this_monday = week_window(wednesday)
        assert this_monday == "2026-06-08"  # Monday of the current week
        assert week_start == "2026-06-01"  # exactly 7 days earlier

    def test_monday_window_is_exact_prior_monday_to_today(self) -> None:
        # 2026-06-08 is itself a Monday.
        monday = date(2026, 6, 8)
        week_start, this_monday = week_window(monday)
        assert this_monday == "2026-06-08"
        assert week_start == "2026-06-01"

    def test_window_is_always_exactly_seven_days(self) -> None:
        for day_offset in range(7):
            d = date(2026, 6, 8 + day_offset)  # Mon..Sun
            week_start, this_monday = week_window(d)
            start = date.fromisoformat(week_start)
            end = date.fromisoformat(this_monday)
            assert (end - start).days == 7

    def test_sunday_window(self) -> None:
        sunday = date(2026, 6, 14)
        week_start, this_monday = week_window(sunday)
        assert this_monday == "2026-06-08"
        assert week_start == "2026-06-01"


# ---------------------------------------------------------------------------
# compute_entity_pl
# ---------------------------------------------------------------------------


class TestComputeEntityPL:
    def test_basic_revenue_and_expense(self, session: Session) -> None:
        _make_tx(session, amount="1000.00", direction=Direction.INCOME.value)
        _make_tx(session, amount="-200.00", direction=Direction.EXPENSE.value)

        pl = compute_entity_pl(session, "2026-06-01", "2026-06-15", entity=Entity.SPARKRY.value)
        assert pl.revenue == Decimal("1000.00")
        assert pl.expenses == Decimal("200.00")
        assert pl.net == Decimal("800.00")
        assert isinstance(pl, EntityPL)

    def test_window_is_half_open_end_exclusive(self, session: Session) -> None:
        _make_tx(session, amount="500.00", direction=Direction.INCOME.value, date="2026-06-08")
        _make_tx(session, amount="500.00", direction=Direction.INCOME.value, date="2026-06-15")  # on the boundary, excluded

        pl = compute_entity_pl(session, "2026-06-08", "2026-06-15")
        assert pl.revenue == Decimal("500.00")

    def test_rejected_excluded(self, session: Session) -> None:
        _make_tx(session, amount="1000.00", direction=Direction.INCOME.value)
        _make_tx(session, amount="-999.00", direction=Direction.EXPENSE.value, status=TransactionStatus.REJECTED.value)

        pl = compute_entity_pl(session, "2026-06-01", "2026-06-15")
        assert pl.expenses == Decimal("0")

    def test_split_parent_excluded(self, session: Session) -> None:
        _make_tx(session, amount="-500.00", direction=Direction.EXPENSE.value, status=TransactionStatus.SPLIT_PARENT.value)
        _make_tx(session, amount="-300.00", direction=Direction.EXPENSE.value, description="child A")
        _make_tx(session, amount="-200.00", direction=Direction.EXPENSE.value, description="child B")

        pl = compute_entity_pl(session, "2026-06-01", "2026-06-15")
        assert pl.expenses == Decimal("500.00")  # not 1000

    def test_linked_reimbursable_pair_nets_to_zero_both_sides(self, session: Session) -> None:
        """A reimbursable expense linked to its reimbursement income row
        must contribute ZERO to both revenue and expenses."""
        income_tx = _make_tx(session, amount="150.00", direction=Direction.INCOME.value, description="Reimbursement")
        _make_tx(
            session, amount="-150.00", direction=Direction.REIMBURSABLE.value,
            description="Reimbursable expense", reimbursement_link=income_tx.id,
        )

        pl = compute_entity_pl(session, "2026-06-01", "2026-06-15")
        assert pl.revenue == Decimal("0")
        assert pl.expenses == Decimal("0")
        assert pl.net == Decimal("0")

    def test_linked_direction_expense_leg_nets_to_zero_both_sides(self, session: Session) -> None:
        """REQ-FIX-API-003 / P2-r1a: link_reimbursement explicitly permits
        the expense leg of a reimbursement pair to be direction=expense (not
        just direction=reimbursable) and sets the link bidirectionally. Such
        a pair must also net to exactly zero on both sides — not just the
        canonical direction=reimbursable (Cardinal Health) case."""
        income_tx = _make_tx(
            session, amount="150.00", direction=Direction.INCOME.value,
            description="Reimbursement",
        )
        expense_tx = _make_tx(
            session, amount="-150.00", direction=Direction.EXPENSE.value,
            description="Reimbursed expense (direction=expense)",
            reimbursement_link=income_tx.id,
        )
        # Mirror link_reimbursement's bidirectional link (transactions.py
        # 1323/1326): the income leg also points back at the expense leg.
        income_tx.reimbursement_link = expense_tx.id
        session.add(income_tx)
        session.commit()

        pl = compute_entity_pl(session, "2026-06-01", "2026-06-15")
        assert pl.revenue == Decimal("0")
        assert pl.expenses == Decimal("0")
        assert pl.net == Decimal("0")

    def test_one_reimbursement_linked_to_many_expenses_nets_all_to_zero(
        self, session: Session
    ) -> None:
        """Issue #62: a single deposit can reimburse several expenses at
        once (e.g. one Cardinal Health trip reimbursement covering multiple
        June expenses). link_reimbursement has no 1:1 enforcement — nothing
        stops calling it N times against the same income row, once per
        expense leg. Every linked expense leg must net to zero, not just
        the last one linked (whose id happens to land in the income row's
        single-valued back-pointer)."""
        income_tx = _make_tx(
            session, amount="1446.18", direction=Direction.INCOME.value,
            description="Cardinal Health trip reimbursement",
        )
        expense_a = _make_tx(
            session, amount="-900.00", direction=Direction.EXPENSE.value,
            description="Flight", reimbursement_link=income_tx.id,
        )
        expense_b = _make_tx(
            session, amount="-546.18", direction=Direction.EXPENSE.value,
            description="Hotel", reimbursement_link=income_tx.id,
        )
        # Mirror link_reimbursement's bidirectional back-pointer: it is
        # single-valued, so the SECOND call to overwrite it clobbers the
        # first expense's back-reference — the income row can only ever
        # remember the last-linked expense.
        income_tx.reimbursement_link = expense_a.id
        session.add(income_tx)
        session.commit()
        income_tx.reimbursement_link = expense_b.id
        session.add(income_tx)
        session.commit()

        pl = compute_entity_pl(session, "2026-06-01", "2026-06-15")
        assert pl.revenue == Decimal("0")
        assert pl.expenses == Decimal("0")
        assert pl.net == Decimal("0")

    def test_unlinked_reimbursable_invisible_to_both_sides(self, session: Session) -> None:
        """An unlinked reimbursable expense (not yet reimbursed) is not P&L
        on either side — correct, it's still pending."""
        _make_tx(session, amount="-75.00", direction=Direction.REIMBURSABLE.value)

        pl = compute_entity_pl(session, "2026-06-01", "2026-06-15")
        assert pl.revenue == Decimal("0")
        assert pl.expenses == Decimal("0")

    def test_transfer_direction_excluded_from_both(self, session: Session) -> None:
        _make_tx(session, amount="5000.00", direction=Direction.TRANSFER.value)

        pl = compute_entity_pl(session, "2026-06-01", "2026-06-15")
        assert pl.revenue == Decimal("0")
        assert pl.expenses == Decimal("0")

    def test_entity_filter_scopes_to_one_entity(self, session: Session) -> None:
        _make_tx(session, amount="1000.00", direction=Direction.INCOME.value, entity=Entity.SPARKRY.value)
        _make_tx(session, amount="2000.00", direction=Direction.INCOME.value, entity=Entity.BLACKLINE.value)

        pl = compute_entity_pl(session, "2026-06-01", "2026-06-15", entity=Entity.SPARKRY.value)
        assert pl.revenue == Decimal("1000.00")

    def test_entity_none_combines_all_entities(self, session: Session) -> None:
        _make_tx(session, amount="1000.00", direction=Direction.INCOME.value, entity=Entity.SPARKRY.value)
        _make_tx(session, amount="2000.00", direction=Direction.INCOME.value, entity=Entity.BLACKLINE.value)

        pl = compute_entity_pl(session, "2026-06-01", "2026-06-15", entity=None)
        assert pl.revenue == Decimal("3000.00")

    def test_out_of_window_transactions_excluded(self, session: Session) -> None:
        _make_tx(session, amount="1000.00", direction=Direction.INCOME.value, date="2026-05-01")
        _make_tx(session, amount="1000.00", direction=Direction.INCOME.value, date="2026-07-01")

        pl = compute_entity_pl(session, "2026-06-01", "2026-06-15")
        assert pl.revenue == Decimal("0")

    def test_amounts_are_decimal_not_float(self, session: Session) -> None:
        _make_tx(session, amount="0.10", direction=Direction.INCOME.value)
        _make_tx(session, amount="0.20", direction=Direction.INCOME.value)

        pl = compute_entity_pl(session, "2026-06-01", "2026-06-15")
        assert pl.revenue == Decimal("0.30")  # exact — no 0.1+0.2 float drift
        assert isinstance(pl.revenue, Decimal)
