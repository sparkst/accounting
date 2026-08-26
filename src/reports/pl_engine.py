"""Canonical entity P&L computation — single source of truth.

REQ-ID: REQ-FIX-API-003  Weekly P&L nets reimbursable pairs out of
revenue/expense and reports an exact 7-day ``[Mon, Mon)`` window regardless
of run day.

``compute_entity_pl`` is the one place this arithmetic lives.
``scripts/weekly-pl-report.py`` is a thin render+dispatch wrapper around it;
the reporting-suite spec's ``wbr.py``, ``sellability.py``, and
``tax_forecast.py`` import this same function rather than re-deriving the
computation (that program's open item 10.1).

Semantics:
- Excludes ``rejected`` and ``split_parent`` rows (REQ-FIX-API-001/002
  exclusion semantics) — a split parent's children carry the actual amounts.
- Reimbursable netting (per CLAUDE.md — reimbursable pairs net to zero on
  P&L): the expense side sums ``direction=expense`` rows but excludes any
  such row whose own ``reimbursement_link`` is set (``link_reimbursement``
  permits the expense leg to be direction=expense, not just
  direction=reimbursable, and always sets the expense leg's own link when
  linking); rows with ``direction=reimbursable`` are never summed as
  expenses in the first place. The revenue side excludes income rows that
  are reimbursement receipts via ``reimbursement_target_ids`` (any
  transaction id targeted by another row's ``reimbursement_link``).
  ``link_reimbursement`` has no 1:1 enforcement, so one reimbursement
  income row may be the target of many expense legs' links (REQ-FIX-ING/
  issue #62, e.g. one deposit covering several trip expenses) — the
  expense-side check keys off each row's own link (always set per linked
  expense leg) rather than the income row's single-valued back-pointer
  (which only ever remembers the last-linked expense), so it correctly
  excludes every linked expense leg, not just one. An unlinked reimbursable
  expense stays invisible to both sides — correct, it is not yet P&L.
- All amounts are ``abs()``'d by direction and computed in Decimal
  end-to-end (``Decimal(str(x))`` at the boundary, per CLAUDE.md) — never a
  SQL-side float aggregate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date
from datetime import timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from src.models.enums import Direction, TransactionStatus
from src.models.transaction import Transaction

_EXCLUDED_STATUSES = (TransactionStatus.REJECTED.value, TransactionStatus.SPLIT_PARENT.value)


@dataclass(frozen=True)
class EntityPL:
    """Revenue/expense/net for one entity (or all entities, if ``entity`` is
    None) over a half-open ``[start, end)`` date window."""

    entity: str | None
    start: str  # ISO date, inclusive
    end: str  # ISO date, EXCLUSIVE
    revenue: Decimal
    expenses: Decimal  # positive magnitude
    net: Decimal


def week_window(today: _date | None = None) -> tuple[str, str]:
    """Return ``(week_start, this_monday)`` as ISO date strings for the
    exact half-open ``[week_start, this_monday)`` 7-day window, regardless of
    which day of the week ``today`` falls on.

    ``this_monday`` is the Monday of the current week (or today, if today
    IS Monday); ``week_start`` is exactly 7 days earlier. The window is
    therefore always precisely 7 days wide — never 8–13 days depending on
    run day, the REQ-FIX-API-003 bug.
    """
    today = today or _date.today()
    this_monday = today - timedelta(days=today.weekday())
    week_start = this_monday - timedelta(days=7)
    return week_start.isoformat(), this_monday.isoformat()


def compute_entity_pl(
    session: Session,
    start: str,
    end: str,
    entity: str | None = None,
) -> EntityPL:
    """Compute revenue, expenses, and net for one entity over ``[start, end)``.

    Args:
        session: Open SQLAlchemy session.
        start:   Inclusive ISO date (``YYYY-MM-DD``).
        end:     EXCLUSIVE ISO date (``YYYY-MM-DD``) — half-open window.
        entity:  Entity value to filter to, or ``None`` for all entities
                 combined.

    Returns:
        :class:`EntityPL` with Decimal revenue/expenses/net.
    """
    query = session.query(Transaction).filter(
        Transaction.date >= start,
        Transaction.date < end,
        Transaction.status.notin_(_EXCLUDED_STATUSES),
    )
    if entity is not None:
        query = query.filter(Transaction.entity == entity)

    # Reimbursement-receipt income rows: any transaction id targeted by
    # another row's reimbursement_link (which lives on the EXPENSE row,
    # pointing at the INCOME row that reimbursed it) is the income-side leg
    # of an already-netted pair and must not also count as revenue.
    reimbursement_target_ids = {
        row[0]
        for row in session.query(Transaction.reimbursement_link)
        .filter(Transaction.reimbursement_link.is_not(None))
        .all()
    }

    revenue = Decimal("0")
    expenses = Decimal("0")

    for tx in query.all():
        if tx.amount is None:
            continue
        amt = abs(Decimal(str(tx.amount)))

        if tx.direction == Direction.INCOME.value:
            if tx.id in reimbursement_target_ids:
                continue  # reimbursement receipt — already netted, not revenue
            revenue += amt
        elif tx.direction == Direction.EXPENSE.value:
            # Issue #62: link_reimbursement has no 1:1 enforcement — one
            # reimbursement income row can be the target of many expense
            # legs' reimbursement_link (e.g. one deposit covering several
            # trip expenses). The income row's own reimbursement_link is
            # single-valued and only ever remembers the LAST-linked expense,
            # so checking reimbursement_target_ids (built from that single
            # back-pointer) only excludes one of the N linked expenses.
            # Each expense leg always carries its own reimbursement_link
            # pointing at the income row when linked — check that directly
            # instead of relying on the income row's back-pointer.
            if tx.reimbursement_link is not None or tx.id in reimbursement_target_ids:
                continue
            expenses += amt
        # transfer / reimbursable-direction rows are never P&L on either side

    return EntityPL(
        entity=entity,
        start=start,
        end=end,
        revenue=revenue,
        expenses=expenses,
        net=revenue - expenses,
    )
