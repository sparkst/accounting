"""Deterministic Plaid-vs-register tie-out (REQ-MCA-001, spec §1.2).

Scope is the prior calendar month unless a ``month`` is given. Per active
``PlaidItem`` (skip ``status=disconnected``), per mapped account keyed by the
``payment_method`` label (the register has no account FK), we compute: sync
coverage + gap days, register count/sum, a depository balance tie-out, and four
unmatched listings. Pure read — no writes. All money is ``Decimal(str(...))`` at
the boundary and quantized to cents before comparison.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.models.brokerage import Account
from src.models.enums import Source, TransactionStatus
from src.models.ingestion_log import IngestionLog
from src.models.plaid import PlaidAccountBalanceSnapshot, PlaidItem
from src.models.transaction import Transaction
from src.utils.reconciliation import find_matches

_CENTS = Decimal("0.01")
_TIE_OUT_TOLERANCE = Decimal("0.01")
_PENDING_STALE_DAYS = 7
_DEPOSITORY = "depository"
_LIABILITY_TYPES = frozenset({"credit", "loan"})
_EXCLUDED_STATUSES = (
    TransactionStatus.REJECTED.value,
    TransactionStatus.SPLIT_PARENT.value,
)


# ── result types ──────────────────────────────────────────────────────────


@dataclass
class AccountRecon:
    payment_method: str | None
    account_name: str | None
    plaid_account_type: str | None
    is_depository: bool
    register_count: int
    register_sum: Decimal
    # Balance tie-out (depository only). None means not applicable (credit card,
    # investment, or missing snapshots).
    balance_delta: Decimal | None = None
    tie_out_ok: bool | None = None
    tie_out_gap: Decimal | None = None
    note: str = ""


@dataclass
class ItemRecon:
    item_id: str
    institution_name: str
    status: str
    covered_days: int
    gap_days: list[str]
    accounts: list[AccountRecon] = field(default_factory=list)

    @property
    def has_gap(self) -> bool:
        return len(self.gap_days) > 0


@dataclass
class StuckPending:
    transaction_id: str
    date: str
    description: str
    amount: Decimal


@dataclass
class NeedsReviewBacklog:
    entity: str | None
    count: int
    oldest_date: str | None


@dataclass
class ReconcileSummary:
    month: str
    items: list[ItemRecon] = field(default_factory=list)
    stuck_pending: list[StuckPending] = field(default_factory=list)
    needs_review_backlog: list[NeedsReviewBacklog] = field(default_factory=list)
    unmatched_payouts: list[Transaction] = field(default_factory=list)
    unmapped_accounts: list[str] = field(default_factory=list)

    @property
    def has_discrepancy(self) -> bool:
        return any(
            a.tie_out_ok is False for it in self.items for a in it.accounts
        )


# ── month helpers ─────────────────────────────────────────────────────────


def prior_month(today: date) -> str:
    """Return the ``YYYY-MM`` of the calendar month before *today*."""
    first_of_this = date(today.year, today.month, 1)
    last_prev = first_of_this - timedelta(days=1)
    return f"{last_prev.year:04d}-{last_prev.month:02d}"


def _month_bounds(month: str) -> tuple[date, date]:
    y, m = int(month[:4]), int(month[5:7])
    first = date(y, m, 1)
    nxt = date(y + (m == 12), (m % 12) + 1, 1)
    return first, nxt - timedelta(days=1)


def _dec(value: Any) -> Decimal:
    return Decimal(str(value))


# ── coverage ──────────────────────────────────────────────────────────────


def _coverage(session: Session, first: date, last: date) -> tuple[int, list[str]]:
    rows = (
        session.query(IngestionLog.run_at)
        .filter(
            IngestionLog.source.like("plaid%"),
            IngestionLog.status == "success",
            IngestionLog.run_at >= first,
            IngestionLog.run_at < last + timedelta(days=1),
        )
        .all()
    )
    covered = {r[0].date().isoformat() for r in rows}
    all_days = []
    d = first
    while d <= last:
        all_days.append(d.isoformat())
        d += timedelta(days=1)
    gaps = [day for day in all_days if day not in covered]
    return len(covered), gaps


# ── register aggregate ────────────────────────────────────────────────────


def _register_aggregate(
    session: Session, payment_method: str | None, first: date, last: date
) -> tuple[int, Decimal]:
    q = (
        session.query(
            func.count(Transaction.id),
            func.coalesce(func.sum(Transaction.amount), 0),
        )
        .filter(
            Transaction.source == Source.PLAID.value,
            Transaction.payment_method == payment_method,
            Transaction.status.notin_(_EXCLUDED_STATUSES),
            Transaction.date >= first.isoformat(),
            Transaction.date <= last.isoformat(),
        )
    )
    count, total = q.one()
    return int(count), _dec(total).quantize(_CENTS)


# ── balance tie-out ───────────────────────────────────────────────────────


def _latest_before(
    session: Session, account_id: str, first: date
) -> PlaidAccountBalanceSnapshot | None:
    return (
        session.query(PlaidAccountBalanceSnapshot)
        .filter(
            PlaidAccountBalanceSnapshot.account_id == account_id,
            PlaidAccountBalanceSnapshot.snapshot_date < first,
        )
        .order_by(PlaidAccountBalanceSnapshot.snapshot_date.desc())
        .first()
    )


def _latest_in_month(
    session: Session, account_id: str, first: date, last: date
) -> PlaidAccountBalanceSnapshot | None:
    return (
        session.query(PlaidAccountBalanceSnapshot)
        .filter(
            PlaidAccountBalanceSnapshot.account_id == account_id,
            PlaidAccountBalanceSnapshot.snapshot_date >= first,
            PlaidAccountBalanceSnapshot.snapshot_date <= last,
        )
        .order_by(PlaidAccountBalanceSnapshot.snapshot_date.desc())
        .first()
    )


def _account_recon(
    session: Session, acct: Account, first: date, last: date
) -> AccountRecon:
    count, reg_sum = _register_aggregate(session, acct.payment_method, first, last)
    baseline = _latest_before(session, acct.id, first)
    latest = _latest_in_month(session, acct.id, first, last)

    plaid_type: str | None = None
    if latest is not None:
        plaid_type = latest.plaid_account_type
    elif baseline is not None:
        plaid_type = baseline.plaid_account_type

    recon = AccountRecon(
        payment_method=acct.payment_method,
        account_name=acct.account_name,
        plaid_account_type=plaid_type,
        is_depository=(plaid_type == _DEPOSITORY),
        register_count=count,
        register_sum=reg_sum,
    )

    if plaid_type in _LIABILITY_TYPES:
        recon.note = "credit-card account: flows only, no balance tie-out"
        return recon
    if plaid_type != _DEPOSITORY:
        recon.note = "non-depository: no balance tie-out"
        return recon
    if baseline is None or latest is None:
        recon.note = "insufficient snapshots for tie-out"
        return recon

    # P2-c3f: baseline is the latest snapshot strictly BEFORE the month (which
    # can be several days earlier than the 1st) and latest is the latest
    # snapshot WITHIN the month (not necessarily the last day). Comparing that
    # delta against a register sum aggregated over the fixed calendar-month
    # window [first, last] manufactures false discrepancies whenever the
    # snapshot dates don't land exactly on the boundaries. Anchor the register
    # sum to the exact snapshot window instead, so delta and Σ always cover
    # the identical span.
    window_start = baseline.snapshot_date + timedelta(days=1)
    window_end = latest.snapshot_date
    if window_start > window_end:
        recon.note = "snapshot window inverted; skipping tie-out"
        return recon
    _, window_sum = _register_aggregate(
        session, acct.payment_method, window_start, window_end
    )

    delta = (_dec(latest.current_balance) - _dec(baseline.current_balance)).quantize(_CENTS)
    gap = (delta - window_sum).copy_abs().quantize(_CENTS)
    recon.balance_delta = delta
    recon.tie_out_gap = gap
    recon.tie_out_ok = gap <= _TIE_OUT_TOLERANCE
    if not recon.tie_out_ok:
        recon.note = (
            f"balance Δ {delta} ({window_start}..{window_end}) vs register Σ "
            f"{window_sum} differ by {gap}"
        )
    return recon


# ── unmatched listings ────────────────────────────────────────────────────


def _stuck_pending(session: Session, today: date) -> list[StuckPending]:
    cutoff = (today - timedelta(days=_PENDING_STALE_DAYS)).isoformat()
    rows = (
        session.query(Transaction)
        .filter(
            Transaction.source == Source.PLAID.value,
            Transaction.status.notin_(_EXCLUDED_STATUSES),
        )
        .all()
    )
    out: list[StuckPending] = []
    for r in rows:
        raw = r.raw_data if isinstance(r.raw_data, dict) else {}
        if raw.get("pending") is True and r.date < cutoff:
            out.append(
                StuckPending(
                    transaction_id=r.id,
                    date=r.date,
                    description=r.description,
                    amount=_dec(r.amount).quantize(_CENTS) if r.amount is not None else Decimal("0.00"),
                )
            )
    return out


def _needs_review_backlog(session: Session) -> list[NeedsReviewBacklog]:
    rows = (
        session.query(
            Transaction.entity,
            func.count(Transaction.id),
            func.min(Transaction.date),
        )
        .filter(Transaction.status == TransactionStatus.NEEDS_REVIEW.value)
        .group_by(Transaction.entity)
        .all()
    )
    return [
        NeedsReviewBacklog(entity=entity, count=int(count), oldest_date=oldest)
        for entity, count, oldest in rows
    ]


def _unmapped_accounts(session: Session) -> list[str]:
    rows = (
        session.query(IngestionLog.error_detail)
        .filter(
            IngestionLog.source.like("plaid%"),
            IngestionLog.error_detail.isnot(None),
            IngestionLog.error_detail.like("%unmapped%"),
        )
        .all()
    )
    return sorted({r[0] for r in rows if r[0]})


# ── entry point ───────────────────────────────────────────────────────────


def reconcile(
    session: Session,
    month: str | None = None,
    *,
    today: date | None = None,
) -> ReconcileSummary:
    """Run the deterministic tie-out for *month* (default = prior calendar month)."""
    today = today or date.today()
    month = month or prior_month(today)
    first, last = _month_bounds(month)

    summary = ReconcileSummary(month=month)

    covered_days, gap_days = _coverage(session, first, last)

    items = (
        session.query(PlaidItem)
        .filter(PlaidItem.status != "disconnected")
        .order_by(PlaidItem.institution_name)
        .all()
    )
    for item in items:
        item_recon = ItemRecon(
            item_id=item.id,
            institution_name=item.institution_name,
            status=item.status,
            covered_days=covered_days,
            gap_days=gap_days,
        )
        accounts = (
            session.query(Account)
            .filter(Account.plaid_item_id == item.id)
            .order_by(Account.account_name)
            .all()
        )
        for acct in accounts:
            item_recon.accounts.append(_account_recon(session, acct, first, last))
        summary.items.append(item_recon)

    summary.stuck_pending = _stuck_pending(session, today)
    summary.needs_review_backlog = _needs_review_backlog(session)
    summary.unmatched_payouts = list(find_matches(session).unmatched_payouts)
    summary.unmapped_accounts = _unmapped_accounts(session)

    return summary
