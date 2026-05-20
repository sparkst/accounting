"""REQ-PERF-005..009: Principal/growth series, TWR, MWR/XIRR, tracked coverage.

Sign convention (consistent with CLAUDE.md and BrokerageTransaction.amount):
  - BrokerageTransaction.amount: positive = cash entering portfolio, negative = exiting.
  - CashFlow.amount for money_weighted_return: negative = investor paid in (outflow
    from investor's perspective), positive = investor received (inflow). Callers
    feeding ``money_weighted_return`` with brokerage amounts must NEGATE them so
    that a deposit (which is positive in the brokerage column) becomes a negative
    XIRR cash flow (money the investor put in).

Edge case policy (anchor spec §6):
  1. Empty position (all shares sold): market_value=0, TWR ends at last sale.
  2. Single deposit no time: TWR=0, XIRR=None.
  3. Position opened mid-window: principal starts at 0 before first tx.
  4. Negative XIRR: bracket includes [-0.99, 10.0].
  5. Stock split: no cash flow, quantity/price adjust, market value continuous.
  6. Reinvested dividend: internal at portfolio+account scope; external_in at position scope.
  7. Internal transfer: both legs internal at portfolio scope; principal unchanged.
  8. Unpaired transfer: defaults to external_*; shows as principal step.
  9. RSU vest: external_in at all scopes at gross FMV.
  10. Window boundary: flows on exactly start or end date ARE included.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.analytics.classify import (
    AccountScope,
    ClassifyError,
    PortfolioScope,
    PositionScope,
    Scope,
    classify,
)
from src.models.brokerage import BrokerageTransaction, PositionSnapshot
from src.models.enums import BrokerageTxStatus, CashFlowType
from src.models.history import CostBasisLot, HistoricalPrice

# REJECTED rows are soft-deleted: never feed them into analytics. Used by
# every BrokerageTransaction query in this module to honour the CLAUDE.md
# "Never delete transactions — use status: rejected to exclude" rule.
_NON_REJECTED = BrokerageTransaction.status != BrokerageTxStatus.REJECTED.value

# ── Dataclasses ──────────────────────────────────────────────────────────────


@dataclass
class DailyPoint:
    """One date's market value decomposed into principal and growth."""

    date: date
    market_value: Decimal
    principal: Decimal
    growth: Decimal


@dataclass
class CashFlow:
    """A dated cash flow for TWR/XIRR calculation.

    Sign convention: negative = outflow from investor (deposit into portfolio),
    positive = inflow to investor (withdrawal or terminal value).
    """

    date: date
    amount: Decimal


@dataclass
class TrackedCoverage:
    """Coverage summary from ``tracked_value_at``."""

    tracked_value: Decimal
    total_value: Decimal
    tracked_account_ids: list[str] = field(default_factory=list)
    tracked_begin_date: date | None = None


# ── Type aliases ──────────────────────────────────────────────────────────────

_SnapData = tuple[Decimal, Decimal]  # (quantity, market_value_fallback)
_SnapIndex = dict[tuple[str, str | None], tuple[list[date], list[_SnapData]]]
_PriceIndex = dict[str, tuple[list[date], list[Decimal]]]


# ── Internal helpers ──────────────────────────────────────────────────────────


def _find_le_idx(sorted_dates: list[date], target: date) -> int:
    """Return the index of the rightmost date <= target, or -1 if none."""
    return bisect.bisect_right(sorted_dates, target) - 1


def _is_external_at_scope(tx: BrokerageTransaction, scope: Scope) -> bool:
    """Return True if tx is external_in or external_out at the given scope.

    Always derives via ``classify()`` — even for ``PortfolioScope`` — so the
    series stays consistent with on-the-fly callers (``_build_brokerage_cash_flows``
    in the API layer). Reading the stored ``cash_flow_type`` column here used
    to produce a divergence when newly-imported rows hadn't been re-backfilled
    yet (default ``'none'`` masked their true classification on the principal
    chart while the TWR/XIRR pipeline saw them as external).
    """
    try:
        cft = classify(tx, scope)
    except ClassifyError:
        return False
    return cft in (CashFlowType.EXTERNAL_IN, CashFlowType.EXTERNAL_OUT)


def _build_snap_index(snaps: list[PositionSnapshot]) -> _SnapIndex:
    """Build a sorted lookup index from PositionSnapshot rows."""
    raw: dict[tuple[str, str | None], dict[date, _SnapData]] = defaultdict(dict)
    for snap in snaps:
        key = (snap.account_id, snap.symbol)
        snap_date: date = snap.as_of.date()
        qty = Decimal(str(snap.quantity)) if snap.quantity is not None else Decimal("0")
        mv = (
            Decimal(str(snap.market_value))
            if snap.market_value is not None
            else Decimal("0")
        )
        raw[key][snap_date] = (qty, mv)

    idx: _SnapIndex = {}
    for key, date_dict in raw.items():
        sorted_dates = sorted(date_dict.keys())
        idx[key] = (sorted_dates, [date_dict[d] for d in sorted_dates])
    return idx


def _build_price_index(prices: list[HistoricalPrice]) -> _PriceIndex:
    """Build a sorted price lookup index."""
    raw: dict[str, dict[date, Decimal]] = defaultdict(dict)
    for p in prices:
        raw[p.symbol][p.trade_date] = p.close

    idx: _PriceIndex = {}
    for sym, date_dict in raw.items():
        sorted_dates = sorted(date_dict.keys())
        idx[sym] = (sorted_dates, [date_dict[d] for d in sorted_dates])
    return idx


def _mv_at(snap_idx: _SnapIndex, price_idx: _PriceIndex, target_date: date) -> Decimal:
    """Compute total market value across all snap_idx entries at target_date.

    For each (account, symbol) position: use latest snapshot quantity × latest
    price close, falling back to snapshot.market_value when no price exists
    (e.g., money-market funds, balance-only sources).
    """
    total = Decimal("0")
    for (_acct_id, sym), (snap_dates, snap_data) in snap_idx.items():
        idx = _find_le_idx(snap_dates, target_date)
        if idx < 0:
            continue
        qty, mv_fallback = snap_data[idx]
        if sym is not None and sym in price_idx:
            p_dates, p_closes = price_idx[sym]
            pidx = _find_le_idx(p_dates, target_date)
            if pidx >= 0:
                total += qty * p_closes[pidx]
                continue
        total += mv_fallback
    return total


def load_transactions_for_scope(
    session: Session, scope: Scope
) -> list[BrokerageTransaction]:
    """Load non-rejected BrokerageTransactions relevant to scope.

    Public so callers (e.g., API endpoints, the auto-pair candidate generator)
    can apply the same scope/REJECTED filter as the analytics engine without
    reaching into a private symbol.
    """
    stmt = select(BrokerageTransaction).where(_NON_REJECTED)
    if isinstance(scope, AccountScope):
        stmt = stmt.where(BrokerageTransaction.account_id == scope.account_id)
    elif isinstance(scope, PositionScope):
        if scope.symbol is not None:
            stmt = stmt.where(BrokerageTransaction.symbol == scope.symbol)
        if scope.account_id is not None:
            stmt = stmt.where(BrokerageTransaction.account_id == scope.account_id)
    return list(session.execute(stmt).scalars())


# Back-compat alias for older callers; will be removed once the API switches.
_load_transactions_for_scope = load_transactions_for_scope


def _load_snapshots_for_scope(
    session: Session, scope: Scope
) -> list[PositionSnapshot]:
    """Load PositionSnapshot rows relevant to scope."""
    stmt = select(PositionSnapshot)
    if isinstance(scope, AccountScope):
        stmt = stmt.where(PositionSnapshot.account_id == scope.account_id)
    elif isinstance(scope, PositionScope):
        if scope.symbol is not None:
            stmt = stmt.where(PositionSnapshot.symbol == scope.symbol)
        if scope.account_id is not None:
            stmt = stmt.where(PositionSnapshot.account_id == scope.account_id)
    return list(session.execute(stmt).scalars())


def _load_lots_for_scope(session: Session, scope: Scope) -> list[CostBasisLot]:
    """Load CostBasisLot rows relevant to scope."""
    stmt = select(CostBasisLot)
    if isinstance(scope, AccountScope):
        stmt = stmt.where(CostBasisLot.account_id == scope.account_id)
    elif isinstance(scope, PositionScope):
        if scope.symbol is not None:
            stmt = stmt.where(CostBasisLot.symbol == scope.symbol)
        if scope.account_id is not None:
            stmt = stmt.where(CostBasisLot.account_id == scope.account_id)
    return list(session.execute(stmt).scalars())


# ── Public API ────────────────────────────────────────────────────────────────


def principal_growth_series(
    session: Session,
    scope: Scope,
    start: date,
    end: date,
    view: Literal["outside_money", "cost_basis"] = "outside_money",
) -> list[DailyPoint]:
    """REQ-PERF-005 / REQ-PERF-006: daily principal/growth decomposition.

    - outside_money view: principal = cumulative net external cash flows.
    - cost_basis view: principal = sum of open ``CostBasisLot.cost_total``.

    Both views agree on ``market_value`` (latest snapshot quantity × latest
    price close at or before that date, with snapshot.market_value fallback).
    Flows on exactly ``start`` or ``end`` ARE included (spec §6 edge case 10).
    """
    all_txs = _load_transactions_for_scope(session, scope)
    all_snaps = _load_snapshots_for_scope(session, scope)

    snap_idx = _build_snap_index(all_snaps)
    symbols = {sym for (_, sym) in snap_idx if sym is not None}
    prices: list[HistoricalPrice] = []
    if symbols:
        stmt = select(HistoricalPrice).where(
            HistoricalPrice.symbol.in_(list(symbols))
        )
        prices = list(session.execute(stmt).scalars())
    price_idx = _build_price_index(prices)

    date_range: list[date] = [
        start + timedelta(days=i) for i in range((end - start).days + 1)
    ]

    result: list[DailyPoint] = []

    if view == "outside_money":
        ext_txs = sorted(
            [t for t in all_txs if _is_external_at_scope(t, scope)],
            key=lambda t: t.trade_date,
        )
        principal = Decimal("0")
        ext_ptr = 0
        for d in date_range:
            while ext_ptr < len(ext_txs) and ext_txs[ext_ptr].trade_date <= d:
                amt = ext_txs[ext_ptr].amount
                if amt is not None:
                    principal += Decimal(str(amt))
                ext_ptr += 1
            mv = _mv_at(snap_idx, price_idx, d)
            result.append(
                DailyPoint(
                    date=d,
                    market_value=mv,
                    principal=principal,
                    growth=mv - principal,
                )
            )
    else:  # cost_basis
        lots = sorted(_load_lots_for_scope(session, scope), key=lambda lot: lot.open_date)
        lot_ptr = 0
        cb_principal = Decimal("0")
        for d in date_range:
            while lot_ptr < len(lots) and lots[lot_ptr].open_date <= d:
                cb_principal += Decimal(str(lots[lot_ptr].cost_total))
                lot_ptr += 1
            mv = _mv_at(snap_idx, price_idx, d)
            result.append(
                DailyPoint(
                    date=d,
                    market_value=mv,
                    principal=cb_principal,
                    growth=mv - cb_principal,
                )
            )

    return result


# ── Brent's method (pure Python, no scipy) ────────────────────────────────────


def _brentq(
    f: Callable[[float], float],
    a: float,
    b: float,
    *,
    tol: float = 1e-8,
    max_iter: int = 500,
) -> float | None:
    """Find root of f in [a, b] using Brent's method.

    Combines bisection, secant, and inverse quadratic interpolation. Returns
    None if there's no sign change in [a, b] or if max_iter is reached.
    """
    fa = f(a)
    fb = f(b)
    if fa * fb > 0:
        return None  # no sign change

    if abs(fa) < abs(fb):
        a, b = b, a
        fa, fb = fb, fa

    c, fc = a, fa
    mflag = True
    d = 0.0

    for _ in range(max_iter):
        if abs(b - a) < tol:
            return b
        if fb == 0.0:
            return b
        if fa != fc and fb != fc:
            # Inverse quadratic interpolation
            s = (
                a * fb * fc / ((fa - fb) * (fa - fc))
                + b * fa * fc / ((fb - fa) * (fb - fc))
                + c * fa * fb / ((fc - fa) * (fc - fb))
            )
        else:
            # Secant method
            s = b - fb * (b - a) / (fb - fa) if fb != fa else (a + b) / 2.0

        lo, hi = (min(a, b), max(a, b))
        cond1 = not (lo < s < hi)
        cond2 = mflag and abs(s - b) >= abs(b - c) / 2.0
        cond3 = (not mflag) and abs(s - b) >= abs(c - d) / 2.0
        cond4 = mflag and abs(b - c) < tol
        cond5 = (not mflag) and abs(c - d) < tol
        if cond1 or cond2 or cond3 or cond4 or cond5:
            s = (a + b) / 2.0
            mflag = True
        else:
            mflag = False

        fs = f(s)
        d = c
        c, fc = b, fb

        if fa * fs < 0:
            b, fb = s, fs
        else:
            a, fa = s, fs
        if abs(fa) < abs(fb):
            a, b = b, a
            fa, fb = fb, fa

    return None  # did not converge


# ── TWR ───────────────────────────────────────────────────────────────────────


@dataclass
class TwrResult:
    """REQ-PERF-007 output: both raw cumulative and annualized TWR.

    ``raw`` is always the chain-linked Modified-Dietz cumulative return for
    the window (no annualization).  ``annualized`` is ``None`` when the window
    is < 30 days (annualizing very short windows is misleading per spec §9.4);
    otherwise it is the ``(1 + raw) ** (365/days) − 1`` annualized rate.

    Callers that want a single Decimal (the legacy contract) can use
    ``time_weighted_return`` which returns ``annualized`` when present and
    ``raw`` otherwise.
    """

    raw: Decimal
    annualized: Decimal | None


def time_weighted_return_breakdown(
    daily_values: list[DailyPoint],
    cash_flows: list[CashFlow],
    period_starts: list[date],
) -> TwrResult:
    """REQ-PERF-007: return BOTH raw and annualized TWR.

    Modified-Dietz monthly chain-link. ``cash_flows`` must use the *portfolio*
    sign convention (positive = inflow / deposit), not the XIRR convention.
    """
    raw, annualized = _twr_components(daily_values, cash_flows, period_starts)
    return TwrResult(raw=raw, annualized=annualized)


def time_weighted_return(
    daily_values: list[DailyPoint],
    cash_flows: list[CashFlow],
    period_starts: list[date],
) -> Decimal:
    """REQ-PERF-007: Modified-Dietz monthly chain-linked TWR (legacy API).

    Returns annualized value for windows ≥ 30 days, raw otherwise — the
    historical behaviour. Prefer ``time_weighted_return_breakdown`` for new
    callers that want both numbers.
    """
    raw, annualized = _twr_components(daily_values, cash_flows, period_starts)
    return annualized if annualized is not None else raw


def _twr_components(
    daily_values: list[DailyPoint],
    cash_flows: list[CashFlow],
    period_starts: list[date],
) -> tuple[Decimal, Decimal | None]:
    """Internal: compute (raw, annualized) Modified-Dietz TWR.

    Annualized is None for windows < 30 days. Skips periods where ``V_begin``
    is zero (no invested capital yet) or ``denom`` is zero (would div-zero).
    Guards against ``raw ≤ -1`` in the annualization step to avoid raising
    ValueError on a >100% loss.
    """
    if not daily_values or not period_starts:
        return Decimal("0.000000"), None

    mv_by_date: dict[date, Decimal] = {dp.date: dp.market_value for dp in daily_values}
    sorted_dp_dates: list[date] = sorted(mv_by_date.keys())
    sorted_mv_vals: list[Decimal] = [mv_by_date[d] for d in sorted_dp_dates]

    def _lookup_mv(d: date) -> Decimal:
        idx = _find_le_idx(sorted_dp_dates, d)
        if idx < 0:
            return Decimal("0")
        return sorted_mv_vals[idx]

    sorted_cfs = sorted(cash_flows, key=lambda cf: cf.date)
    last_dp_date = daily_values[-1].date

    chain = Decimal("1")
    any_computed = False

    for i, p_start in enumerate(period_starts):
        if i + 1 < len(period_starts):
            p_end = period_starts[i + 1] - timedelta(days=1)
        else:
            p_end = last_dp_date

        if p_end < p_start:
            continue
        total_days = (p_end - p_start).days
        if total_days == 0:
            continue

        v_begin = _lookup_mv(p_start)
        v_end = _lookup_mv(p_end)
        if v_begin == Decimal("0"):
            continue

        period_cfs = [cf for cf in sorted_cfs if p_start <= cf.date <= p_end]
        cf_net = sum((cf.amount for cf in period_cfs), Decimal("0"))
        weighted_cf = sum(
            (
                cf.amount
                * Decimal(str((p_end - cf.date).days))
                / Decimal(str(total_days))
                for cf in period_cfs
            ),
            Decimal("0"),
        )

        denom = v_begin + weighted_cf
        if denom == Decimal("0"):
            continue

        r_i = (v_end - v_begin - cf_net) / denom
        chain *= Decimal("1") + r_i
        any_computed = True

    if not any_computed:
        return Decimal("0.000000"), None

    twr_raw = chain - Decimal("1")
    raw_q = twr_raw.quantize(Decimal("0.000001"))

    window_days = (last_dp_date - period_starts[0]).days
    if window_days >= 30:
        twr_float = float(twr_raw)
        # Guard against a > 100% loss: (1 + r) <= 0 makes the power expression
        # raise. Annualized return is undefined in that case; return None.
        if 1.0 + twr_float <= 0:
            return raw_q, None
        annualized = Decimal(str((1.0 + twr_float) ** (365.0 / window_days) - 1.0))
        return raw_q, annualized.quantize(Decimal("0.000001"))

    return raw_q, None


# ── XIRR / MWR ────────────────────────────────────────────────────────────────


def money_weighted_return(
    cash_flows: list[CashFlow],
    terminal_value: Decimal,
    terminal_date: date,
) -> Decimal | None:
    """REQ-PERF-008: XIRR via Brent's method (pure Python, no scipy).

    NPV: f(r) = Σ CF_k / (1+r)^((t_k − t_0)/365) + terminal / (1+r)^((terminal_date − t_0)/365).
    Bracket [-0.99, 10.0]. Returns ``None`` on non-convergence, all-zero flows,
    or no-time-elapsed inputs.
    """
    if not cash_flows:
        return None

    t0 = min(cf.date for cf in cash_flows)
    if all(cf.date == t0 for cf in cash_flows) and terminal_date == t0:
        return None
    if all(cf.amount == Decimal("0") for cf in cash_flows) and terminal_value == Decimal("0"):
        return None

    def npv(r: float) -> float:
        total = 0.0
        for cf in cash_flows:
            t = (cf.date - t0).days / 365.0
            total += float(cf.amount) / (1.0 + r) ** t
        t_end = (terminal_date - t0).days / 365.0
        total += float(terminal_value) / (1.0 + r) ** t_end
        return float(total)

    r = _brentq(npv, -0.99, 10.0)
    if r is None:
        return None
    return Decimal(str(r)).quantize(Decimal("0.000001"))


# ── Tracked coverage ──────────────────────────────────────────────────────────


def tracked_value_at(session: Session, target_date: date) -> TrackedCoverage:
    """REQ-PERF-009: compute tracked vs total market value coverage.

    Tracked accounts: those with at least one non-``none`` ``cash_flow_type``
    BrokerageTransaction within 365 days ending on ``target_date`` (proxy for
    "we have transaction-level detail"). REJECTED rows are excluded.
    ``tracked_begin_date`` is the earliest non-``none`` tx date across tracked
    accounts.
    """
    cutoff = target_date - timedelta(days=365)

    stmt_tracked = (
        select(BrokerageTransaction.account_id)
        .where(
            _NON_REJECTED,
            BrokerageTransaction.cash_flow_type != CashFlowType.NONE.value,
            BrokerageTransaction.trade_date >= cutoff,
            BrokerageTransaction.trade_date <= target_date,
        )
        .distinct()
    )
    tracked_ids: set[str] = {row[0] for row in session.execute(stmt_tracked)}

    # Date pre-filter at the DB layer. ``PositionSnapshot.as_of`` is a
    # datetime; we want all snapshots dated on or before ``target_date``, so
    # compare against the end of that day to avoid string-comparison surprises
    # in SQLite (where datetime is ISO-string-stored).
    cutoff_dt = datetime.combine(target_date, datetime.max.time())
    snap_stmt = select(PositionSnapshot).where(PositionSnapshot.as_of <= cutoff_dt)
    all_snaps: list[PositionSnapshot] = list(session.execute(snap_stmt).scalars())

    latest: dict[tuple[str, str | None], PositionSnapshot] = {}
    for snap in all_snaps:
        key = (snap.account_id, snap.symbol)
        existing = latest.get(key)
        if existing is None or snap.as_of > existing.as_of:
            latest[key] = snap

    acct_mv: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for (acct_id, _sym), snap in latest.items():
        mv = (
            Decimal(str(snap.market_value))
            if snap.market_value is not None
            else Decimal("0")
        )
        acct_mv[acct_id] += mv

    tracked_value = sum(
        (mv for acct_id, mv in acct_mv.items() if acct_id in tracked_ids),
        Decimal("0"),
    )
    total_value = sum(acct_mv.values(), Decimal("0"))

    tracked_begin_date: date | None = None
    if tracked_ids:
        stmt_begin = select(BrokerageTransaction.trade_date).where(
            _NON_REJECTED,
            BrokerageTransaction.account_id.in_(list(tracked_ids)),
            BrokerageTransaction.cash_flow_type != CashFlowType.NONE.value,
        )
        all_dates: list[date] = [row[0] for row in session.execute(stmt_begin)]
        if all_dates:
            tracked_begin_date = min(all_dates)

    return TrackedCoverage(
        tracked_value=tracked_value,
        total_value=total_value,
        tracked_account_ids=sorted(tracked_ids),
        tracked_begin_date=tracked_begin_date,
    )
