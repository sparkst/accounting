"""Pure balance-milestone-alert rule functions (REQ-BAL-001..006).

Design: the *pure* crossing logic (`evaluate_account`) takes a prior-day baseline
and the latest balance and returns the `BalanceAlert`s that a day-over-day move
should fire — no DB, no network, fully unit-testable. `compute_balance_alerts`
is the thin DB layer that reads the latest + prior-calendar-day
`plaid_account_balance_snapshot` per account and delegates to `evaluate_account`.

Account-type tiering keys off Plaid's own `plaid_account_type` / subtype (most
reliable for alerting):

  depository + 'checking'  → checking milestones [10k, 5k, 1k, 0]  (downward)
  depository (other)/other → savings floor 100                    (downward)
  credit                   → credit milestones 10k, 20k, …        (upward, owed amount)
  investment / brokerage   → drift 15% AND $25k
  loan                     → muted

A milestone fires only on a day-over-day *directional crossing* vs the prior
calendar day (REQ-BAL-005). Dedup is per `(alert_key, occurrence_date)` where
occurrence_date is the snapshot's calendar day — the dispatcher's `alert_dispatch`
ledger enforces it (REQ-BAL-006).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

SOURCE = "accounting"

# --- Rule constants (shared conceptually with sparkry-crm Track B) ----------
CHECKING_MILESTONES: tuple[Decimal, ...] = (
    Decimal("10000"),
    Decimal("5000"),
    Decimal("1000"),
    Decimal("0"),
)
SAVINGS_FLOOR = Decimal("100")
CREDIT_STEP = Decimal("10000")
INVESTMENT_DRIFT_PCT = Decimal("15")
INVESTMENT_DRIFT_ABS = Decimal("25000")

# Credit milestones are unbounded ($10k, $20k, …); cap generation so a corrupt
# Plaid value can't spin out an unbounded list.
_CREDIT_MAX_MILESTONE = Decimal("10000000")  # $10M ceiling on generated bands

_LIABILITY_TYPES = frozenset({"credit", "loan"})
_INVESTMENT_TYPES = frozenset({"investment", "brokerage"})

# Severity levels routed by n8n (`type` field). sev2 > sev3 > info.
SEV_INFO = "info"
SEV3 = "sev3"
SEV2 = "sev2"


@dataclass(frozen=True)
class BalanceAlert:
    """One fired balance alert, ready for the dispatcher + n8n payload."""

    alert_key: str
    occurrence_date: str  # the snapshot's calendar day (UTC), ISO
    alert_type: str  # "balance_milestone" | "balance_drift"
    severity: str  # "info" | "sev3" | "sev2"
    entity: str
    account_id: str
    account_name: str
    kind: str  # "checking" | "savings" | "credit" | "investment"
    level: str | None  # milestone value as string, None for drift
    baseline: str
    new_balance: str
    title: str
    message: str


def _q2(d: Decimal) -> Decimal:
    """Quantize to scale-2 (Plaid current_balance is scale-4)."""
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def classify(plaid_account_type: str, plaid_account_subtype: str | None) -> str | None:
    """Map a Plaid account to an alert kind, or None when muted.

    Returns one of: "checking" | "savings" | "credit" | "investment" | None.
    """
    t = (plaid_account_type or "").strip().lower()
    sub = (plaid_account_subtype or "").strip().lower()
    if t == "loan":
        return None  # REQ-BAL-004: loans muted
    if t == "credit":
        return "credit"
    if t in _INVESTMENT_TYPES:
        return "investment"
    if t == "depository":
        return "checking" if sub == "checking" else "savings"
    if t == "other":
        return "savings"  # treat unknown depository-like as a floor account
    return None


def _credit_severity(level: Decimal) -> str:
    # REQ-BAL-003: $10k → info; ≥$20k → sev3.
    return SEV_INFO if level == Decimal("10000") else SEV3


def _checking_severity(level: Decimal) -> str:
    # REQ-BAL-001: <$10k & <$5k → info; <$1k → sev3; <$0 → sev2.
    if level <= Decimal("0"):
        return SEV2
    if level <= Decimal("1000"):
        return SEV3
    return SEV_INFO


def _fmt(d: Decimal) -> str:
    return f"${_q2(d):,.2f}"


def _checking_alerts(
    account_id: str, account_name: str, entity: str, occ: str, baseline: Decimal, current: Decimal
) -> list[BalanceAlert]:
    out: list[BalanceAlert] = []
    for level in CHECKING_MILESTONES:
        # Downward crossing: was above L yesterday, at/below L today.
        if baseline > level and current <= level:
            sev = _checking_severity(level)
            out.append(
                BalanceAlert(
                    alert_key=f"balance:{account_id}:checking:{level}",
                    occurrence_date=occ,
                    alert_type="balance_milestone",
                    severity=sev,
                    entity=entity,
                    account_id=account_id,
                    account_name=account_name,
                    kind="checking",
                    level=str(level),
                    baseline=str(_q2(baseline)),
                    new_balance=str(_q2(current)),
                    title=f"{account_name} below {_fmt(level)}",
                    message=(
                        f"{account_name} fell from {_fmt(baseline)} to "
                        f"{_fmt(current)}, crossing the {_fmt(level)} floor."
                    ),
                )
            )
    return out


def _savings_alerts(
    account_id: str, account_name: str, entity: str, occ: str, baseline: Decimal, current: Decimal
) -> list[BalanceAlert]:
    if baseline > SAVINGS_FLOOR and current <= SAVINGS_FLOOR:
        return [
            BalanceAlert(
                alert_key=f"balance:{account_id}:savings:{SAVINGS_FLOOR}",
                occurrence_date=occ,
                alert_type="balance_milestone",
                severity=SEV3,
                entity=entity,
                account_id=account_id,
                account_name=account_name,
                kind="savings",
                level=str(SAVINGS_FLOOR),
                baseline=str(_q2(baseline)),
                new_balance=str(_q2(current)),
                title=f"{account_name} below {_fmt(SAVINGS_FLOOR)}",
                message=(
                    f"{account_name} fell from {_fmt(baseline)} to "
                    f"{_fmt(current)}, below the {_fmt(SAVINGS_FLOOR)} minimum."
                ),
            )
        ]
    return []


def _credit_milestones_up_to(value: Decimal) -> list[Decimal]:
    """[$10k, $20k, …] up to (and including the band containing) `value`."""
    out: list[Decimal] = []
    level = CREDIT_STEP
    while level <= value and level <= _CREDIT_MAX_MILESTONE:
        out.append(level)
        level += CREDIT_STEP
    return out


def _credit_alerts(
    account_id: str, account_name: str, entity: str, occ: str, baseline: Decimal, current: Decimal
) -> list[BalanceAlert]:
    # Credit balances are the positive amount owed; alert as the owed amount climbs.
    out: list[BalanceAlert] = []
    for level in _credit_milestones_up_to(current):
        # Upward crossing: was below L yesterday, at/above L today.
        if baseline < level and current >= level:
            out.append(
                BalanceAlert(
                    alert_key=f"balance:{account_id}:credit:{level}",
                    occurrence_date=occ,
                    alert_type="balance_milestone",
                    severity=_credit_severity(level),
                    entity=entity,
                    account_id=account_id,
                    account_name=account_name,
                    kind="credit",
                    level=str(level),
                    baseline=str(_q2(baseline)),
                    new_balance=str(_q2(current)),
                    title=f"{account_name} reached {_fmt(level)}",
                    message=(
                        f"{account_name} rose from {_fmt(baseline)} to "
                        f"{_fmt(current)}, crossing {_fmt(level)} owed."
                    ),
                )
            )
    return out


def _investment_alerts(
    account_id: str, account_name: str, entity: str, occ: str, baseline: Decimal, current: Decimal
) -> list[BalanceAlert]:
    # REQ-BAL-004: drift, tightened to 15% AND $25k.
    if baseline == Decimal("0") and current == Decimal("0"):
        return []
    delta = current - baseline
    abs_delta = abs(delta)
    near_zero = abs(baseline) < Decimal("1.00")
    delta_pct = None if near_zero else (delta / abs(baseline) * Decimal("100"))
    pct_ok = delta_pct is not None and abs(delta_pct) >= INVESTMENT_DRIFT_PCT
    abs_ok = abs_delta >= INVESTMENT_DRIFT_ABS
    if not (pct_ok and abs_ok):  # AND, not OR
        return []
    pct_str = "n/a" if delta_pct is None else f"{_q2(delta_pct)}%"
    return [
        BalanceAlert(
            alert_key=f"balance:{account_id}:drift",
            occurrence_date=occ,
            alert_type="balance_drift",
            severity=SEV3,
            entity=entity,
            account_id=account_id,
            account_name=account_name,
            kind="investment",
            level=None,
            baseline=str(_q2(baseline)),
            new_balance=str(_q2(current)),
            title=f"{account_name}: {_q2(delta):+,.2f} ({pct_str}) — review",
            message=(
                f"{account_name} drifted from {_fmt(baseline)} to {_fmt(current)} "
                f"(Δ {_q2(delta):+,.2f}, {pct_str})."
            ),
        )
    ]


def evaluate_account(
    *,
    account_id: str,
    account_name: str,
    entity: str,
    plaid_account_type: str,
    plaid_account_subtype: str | None,
    baseline: Decimal | None,
    current: Decimal,
    occurrence_date: str,
) -> list[BalanceAlert]:
    """Pure crossing evaluation for one account. No DB, no network.

    `baseline` is the prior-calendar-day balance, or None when there is no
    prior-day row (REQ-BAL-005: never fire without a baseline).
    """
    if baseline is None:
        return []
    kind = classify(plaid_account_type, plaid_account_subtype)
    if kind is None:
        return []
    if kind == "checking":
        return _checking_alerts(account_id, account_name, entity, occurrence_date, baseline, current)
    if kind == "savings":
        return _savings_alerts(account_id, account_name, entity, occurrence_date, baseline, current)
    if kind == "credit":
        return _credit_alerts(account_id, account_name, entity, occurrence_date, baseline, current)
    if kind == "investment":
        return _investment_alerts(account_id, account_name, entity, occurrence_date, baseline, current)
    return []


def compute_balance_alerts(today: date, session: Session) -> list[BalanceAlert]:
    """DB layer: for every Plaid-linked account, read the latest snapshot and the
    prior-calendar-day snapshot, then evaluate crossings (REQ-BAL-001..006)."""
    from src.models.brokerage import Account
    from src.models.plaid import PlaidAccountBalanceSnapshot as Snap

    alerts: list[BalanceAlert] = []
    account_ids = session.scalars(select(Snap.account_id).distinct()).all()
    for account_id in account_ids:
        latest = session.scalars(
            select(Snap)
            .where(Snap.account_id == account_id, Snap.snapshot_date <= today)
            .order_by(Snap.snapshot_date.desc())
            .limit(1)
        ).first()
        if latest is None:
            continue
        prior_day = latest.snapshot_date - timedelta(days=1)
        baseline_row = session.scalars(
            select(Snap).where(
                Snap.account_id == account_id, Snap.snapshot_date == prior_day
            )
        ).first()
        account = session.get(Account, account_id)
        name = account.account_name if account and account.account_name else account_id
        name = name[:80]  # Plaid/institution-controlled — cap before it enters subject/payload
        entity = account.entity if account else "personal"
        alerts.extend(
            evaluate_account(
                account_id=account_id,
                account_name=name,
                entity=entity,
                plaid_account_type=latest.plaid_account_type,
                plaid_account_subtype=latest.plaid_account_subtype,
                baseline=(baseline_row.current_balance if baseline_row else None),
                current=latest.current_balance,
                occurrence_date=latest.snapshot_date.isoformat(),
            )
        )
    return alerts
