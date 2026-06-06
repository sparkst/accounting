"""Live-input loaders.

The only file in src/planning/ that touches the register / wealth tables. If
those schemas move (e.g., during the in-flight Hetzner migration), this is the
one file that changes.

Convention (spec §4.2):
  - Pool defaults to live ("what we have" *is* reality).
  - Other inputs default to planning values; live actuals are surfaced
    alongside in LiveInputs for drift inspection.
  - ttm_personal_income is informational only (REQ-PLAN-019): never used to
    override amy_wage_income.

Schema notes (verified 2026-06-01 against actual models):
  - Account.account_type is a plain string column storing lowercase StrEnum
    values: "taxable", "checking", "savings", "trad_ira", "roth_ira",
    "401k", "403b", "hsa", "529", etc.  NOT the uppercase strings the plan
    originally assumed.
  - AccountBalanceSnapshot date column is "as_of" (not "snapshot_date").
    It is imported from src.models.history (not src.models.brokerage).
  - Transaction.date is String(10) storing "YYYY-MM-DD"; date range filters
    use ISO string comparison, which is correct for zero-padded dates.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.models.brokerage import Account
from src.models.history import AccountBalanceSnapshot
from src.models.transaction import Transaction

# AccountType values that count toward the taxable (non-sheltered) pool.
# Based on actual StrEnum values in src/models/brokerage.py (lowercase).
TAXABLE_TYPES = {
    "taxable",
    "joint",
    "checking",
    "savings",
    "tod",
    "rsu",
    "brokeragelink",
    "other",
}
# AccountType values that count toward the retirement (tax-sheltered) pool.
RETIREMENT_TYPES = {
    "trad_ira",
    "roth_ira",
    "401k",
    "403b",
    "hsa",
    "529",
}

STALE_DAYS = 7


@dataclass(frozen=True)
class LiveInputs:
    """Snapshot of live data captured at engine-run time.

    Persisted into PlanningRun.live_inputs_json (REQ-PLAN-007) regardless of
    whether values were used by the engine.
    """

    pool_taxable: float
    pool_retirement: float
    ttm_spend: float
    ttm_biz_income: float
    ttm_personal_income: float
    latest_snapshot_date: dt.date
    staleness_warning: str | None
    ttm_tax_effective: float | None  # informational; None if not computable in v1


def _latest_balance_per_account(session: Session) -> list[tuple[str, float]]:
    """Returns (account_type, latest_balance) for every account with snapshots."""
    # Subquery: latest as_of per account_id (only linked snapshots, not orphans)
    subq = (
        select(
            AccountBalanceSnapshot.account_id,
            func.max(AccountBalanceSnapshot.as_of).label("latest_date"),
        )
        .where(AccountBalanceSnapshot.account_id.isnot(None))
        .group_by(AccountBalanceSnapshot.account_id)
        .subquery()
    )
    stmt = (
        select(
            Account.account_type,
            AccountBalanceSnapshot.balance,
        )
        .join(subq, subq.c.account_id == AccountBalanceSnapshot.account_id)
        .where(AccountBalanceSnapshot.as_of == subq.c.latest_date)
        .join(Account, Account.id == AccountBalanceSnapshot.account_id)
    )
    return [(str(row[0]), float(row[1])) for row in session.execute(stmt)]


def _ttm_sum(
    session: Session, today: dt.date, *, entity: str, direction: str
) -> float:
    """Sum of abs(amount) for matching transactions in the trailing 365 days.

    Transaction.date is stored as String(10) "YYYY-MM-DD"; ISO string
    comparison is correct for zero-padded dates (lexicographic == chronological).
    """
    start = (today - dt.timedelta(days=365)).isoformat()
    end = today.isoformat()
    stmt = select(func.coalesce(func.sum(func.abs(Transaction.amount)), 0)).where(
        Transaction.entity == entity,
        Transaction.direction == direction,
        Transaction.date >= start,
        Transaction.date <= end,
    )
    return float(session.execute(stmt).scalar() or 0.0)


def load_live(session: Session, today: dt.date | None = None) -> LiveInputs:
    """Read live inputs from the register + wealth tables.

    Raises RuntimeError if no AccountBalanceSnapshot rows exist (REQ-PLAN-014):
    the caller must fix wealth ingestion before a planning run is meaningful.
    """
    if today is None:
        today = dt.date.today()

    rows = _latest_balance_per_account(session)
    if not rows:
        raise RuntimeError(
            "No AccountBalanceSnapshot rows found. Run scripts/plaid_balance_sync.py "
            "or pass --override pool_taxable=... pool_retirement=... to bypass."
        )

    pool_taxable = sum(b for t, b in rows if t in TAXABLE_TYPES)
    pool_retirement = sum(b for t, b in rows if t in RETIREMENT_TYPES)

    # as_of may come back as a date string (SQLite) or date object (depending
    # on driver). Normalise to dt.date in both cases.
    latest_date_scalar = session.execute(
        select(func.max(AccountBalanceSnapshot.as_of))
    ).scalar()
    assert latest_date_scalar is not None  # guaranteed by the non-empty rows check

    if isinstance(latest_date_scalar, str):
        latest_date: dt.date = dt.date.fromisoformat(latest_date_scalar)
    elif isinstance(latest_date_scalar, dt.datetime):
        latest_date = latest_date_scalar.date()
    else:
        latest_date = latest_date_scalar  # already dt.date

    age_days = (today - latest_date).days
    staleness_warning: str | None = None
    if age_days > STALE_DAYS:
        staleness_warning = (
            f"latest AccountBalanceSnapshot is {age_days} days old "
            f"(as_of={latest_date.isoformat()}); pool values may be stale"
        )

    ttm_spend = _ttm_sum(session, today, entity="personal", direction="expense")
    ttm_biz_income = _ttm_sum(session, today, entity="sparkry", direction="income")
    ttm_personal_income = _ttm_sum(
        session, today, entity="personal", direction="income"
    )

    return LiveInputs(
        pool_taxable=pool_taxable,
        pool_retirement=pool_retirement,
        ttm_spend=ttm_spend,
        ttm_biz_income=ttm_biz_income,
        ttm_personal_income=ttm_personal_income,
        latest_snapshot_date=latest_date,
        staleness_warning=staleness_warning,
        ttm_tax_effective=None,  # v1: not yet computed
    )
