"""Investment-policy analytics — concentration, glide, excise, bold bets.

REQ-IPD-001..003, REQ-BBT-001..002. Pure computation over the latest per-account
position snapshots (same inclusion rules as net worth: plan-wrappers excluded,
closed expected-accounts excluded, 529s and insurance-balance accounts excluded
from the investable base). All-Decimal; quantize only at the presentation edge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from src.analytics.policy_config import PolicyConfig, glide_pct
from src.models.brokerage import Account, RealizedGainLoss
from src.models.enums import AccountType, Broker, GainLossTerm
from src.models.history import ExpectedAccount
from src.reports.brokerage_summary import _is_cash_symbol, _latest_snapshot_rows

# Balance-only insurance/annuity carriers — beneficiary/non-tradeable value that
# is excluded from the investable base (REQ-IPD-001).
_INSURANCE_BROKERS: frozenset[str] = frozenset(
    {Broker.NW_MUTUAL.value, Broker.FG_ANNUITY.value, Broker.NORTH_AMERICAN.value}
)
_EXCLUDED_ACCOUNT_TYPES: frozenset[str] = frozenset({AccountType.K529.value})


@dataclass
class SymbolConcentration:
    symbol: str
    market_value: Decimal
    cost_basis: Decimal
    basis_missing: bool
    pct: Decimal  # fraction of investable base (0..1)
    embedded_gain: Decimal | None  # None when basis missing


@dataclass
class PolicyResult:
    as_of: date
    investable_base: Decimal
    equity_base: Decimal
    cash_value: Decimal
    cash_pct: Decimal
    international_value: Decimal
    international_pct_of_equity: Decimal
    international_target_pct: Decimal
    concentration: list[SymbolConcentration]
    combined_symbols: list[str]
    combined_value: Decimal
    combined_pct: Decimal
    current_pct: Decimal  # combined, in points (pct * 100)
    glide_pct: Decimal
    headroom_pts: Decimal
    drift_alert_threshold_pts: Decimal
    glide_series: list[tuple[date, Decimal]]
    wa_tax_year: int
    realized_lt_gains_ytd: Decimal
    excise_threshold: Decimal | None
    excise_threshold_headroom: Decimal | None
    excise_surcharge_threshold: Decimal | None
    excise_surcharge_headroom: Decimal | None
    warnings: list[str] = field(default_factory=list)


def _closed_account_ids(session: Session) -> set[str]:
    return {
        row[0]
        for row in session.query(ExpectedAccount.resolved_account_id)
        .filter(
            ExpectedAccount.status == "closed",
            ExpectedAccount.resolved_account_id.isnot(None),
        )
        .all()
    }


def _investable_account_ids(session: Session) -> set[str]:
    """Accounts contributing to the investable base: not plan-wrapper, not closed,
    not a 529, not a balance-only insurance carrier."""
    closed = _closed_account_ids(session)
    out: set[str] = set()
    for a in session.query(Account).all():
        if a.is_plan_wrapper or a.id in closed:
            continue
        if a.account_type in _EXCLUDED_ACCOUNT_TYPES:
            continue
        if a.broker in _INSURANCE_BROKERS:
            continue
        out.add(a.id)
    return out


def compute_realized_lt_gains_ytd(
    session: Session, config: PolicyConfig, today: date
) -> Decimal:
    """Σ realized LT gains in the current tax year, taxable accounts only.

    REQ-IPD-003: uses ``lt_gain_loss`` when present, else ``gain_loss`` where
    ``term`` is long; ``tax_sheltered=0`` accounts only (correct after §10).
    """
    taxable_ids = {
        a.id for a in session.query(Account).filter(Account.tax_sheltered.is_(False))
    }
    total = Decimal("0")
    lots: list[RealizedGainLoss] = (
        session.query(RealizedGainLoss)
        .filter(RealizedGainLoss.account_id.in_(taxable_ids))
        .all()
        if taxable_ids
        else []
    )
    for lot in lots:
        if lot.closed_date is None or lot.closed_date.year != today.year:
            continue
        if lot.lt_gain_loss is not None:
            total += Decimal(str(lot.lt_gain_loss))
        elif lot.term == GainLossTerm.LONG.value and lot.gain_loss is not None:
            total += Decimal(str(lot.gain_loss))
    return total


def compute_policy(
    session: Session, config: PolicyConfig, today: date
) -> PolicyResult:
    """Compute the full investment-policy snapshot (REQ-IPD-001..003)."""
    investable_ids = _investable_account_ids(session)

    # Aggregate latest position rows across investable accounts, per symbol.
    per_symbol_mv: dict[str, Decimal] = {}
    per_symbol_cost: dict[str, Decimal] = {}
    per_symbol_basis_missing: dict[str, bool] = {}
    warnings: list[str] = []

    for ps in _latest_snapshot_rows(session):
        if ps.account_id not in investable_ids:
            continue
        if ps.market_value is None or ps.market_value <= 0:
            continue
        symbol = (ps.symbol or ps.description or "UNKNOWN").upper()
        mv = Decimal(str(ps.market_value))
        per_symbol_mv[symbol] = per_symbol_mv.get(symbol, Decimal("0")) + mv
        if ps.cost_basis is not None:
            per_symbol_cost[symbol] = per_symbol_cost.get(symbol, Decimal("0")) + Decimal(
                str(ps.cost_basis)
            )
        else:
            per_symbol_basis_missing[symbol] = True

    investable_base = sum(per_symbol_mv.values(), Decimal("0"))

    cash_symbols = set(config.cash_symbols)
    intl_symbols = set(config.international_symbols)
    cash_value = sum(
        (mv for sym, mv in per_symbol_mv.items() if sym in cash_symbols or _is_cash_symbol(sym)),
        Decimal("0"),
    )
    international_value = sum(
        (mv for sym, mv in per_symbol_mv.items() if sym in intl_symbols), Decimal("0")
    )
    equity_base = investable_base - cash_value

    def _pct(numer: Decimal, denom: Decimal) -> Decimal:
        return numer / denom if denom > 0 else Decimal("0")

    concentration: list[SymbolConcentration] = []
    for symbol, mv in per_symbol_mv.items():
        basis_missing = per_symbol_basis_missing.get(symbol, False)
        cost = per_symbol_cost.get(symbol, Decimal("0"))
        concentration.append(
            SymbolConcentration(
                symbol=symbol,
                market_value=mv,
                cost_basis=cost,
                basis_missing=basis_missing,
                pct=_pct(mv, investable_base),
                embedded_gain=None if basis_missing else (mv - cost),
            )
        )
    concentration.sort(key=lambda c: c.market_value, reverse=True)

    combined_symbols = list(config.concentration.symbols)
    combined_value = sum(
        (per_symbol_mv.get(sym, Decimal("0")) for sym in combined_symbols), Decimal("0")
    )
    combined_pct = _pct(combined_value, investable_base)
    current_pct = combined_pct * Decimal("100")
    glide = glide_pct(config.concentration, date(today.year, today.month, 1))
    headroom_pts = glide - current_pct

    # Glide series: monthly from baseline_month through target_month for charting.
    glide_series: list[tuple[date, Decimal]] = []
    cursor = config.concentration.baseline_month
    end = config.concentration.target_month
    while cursor <= end:
        glide_series.append((cursor, glide_pct(config.concentration, cursor)))
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)

    realized_lt = compute_realized_lt_gains_ytd(session, config, today)
    excise = config.wa_excise_for_year(today.year)
    if excise is None:
        warnings.append(f"no wa_excise config for tax year {today.year}")

    return PolicyResult(
        as_of=today,
        investable_base=investable_base,
        equity_base=equity_base,
        cash_value=cash_value,
        cash_pct=_pct(cash_value, investable_base),
        international_value=international_value,
        international_pct_of_equity=_pct(international_value, equity_base),
        international_target_pct=config.international_target_pct_of_equity,
        concentration=concentration,
        combined_symbols=combined_symbols,
        combined_value=combined_value,
        combined_pct=combined_pct,
        current_pct=current_pct,
        glide_pct=glide,
        headroom_pts=headroom_pts,
        drift_alert_threshold_pts=config.concentration.drift_alert_threshold_pts,
        glide_series=glide_series,
        wa_tax_year=today.year,
        realized_lt_gains_ytd=realized_lt,
        excise_threshold=excise.threshold if excise else None,
        excise_threshold_headroom=(excise.threshold - realized_lt) if excise else None,
        excise_surcharge_threshold=excise.surcharge_threshold if excise else None,
        excise_surcharge_headroom=(
            excise.surcharge_threshold - realized_lt if excise else None
        ),
        warnings=warnings,
    )


# ── Bold bets (REQ-BBT-001..002) ────────────────────────────────────────


@dataclass
class BoldBetPositionResult:
    symbol: str
    account_id: str
    account_name: str | None
    market_value: Decimal
    cost_basis: Decimal | None
    unrealized_gain: Decimal | None
    realized_gain: Decimal
    thesis: str | None
    exit: str | None


@dataclass
class BoldBetsResult:
    positions: list[BoldBetPositionResult]
    sleeve_value: Decimal
    sleeve_cost_basis: Decimal
    sleeve_unrealized: Decimal
    sleeve_realized: Decimal
    cap: Decimal
    over_cap: bool
    pct_of_investable: Decimal
    investable_base: Decimal


def compute_bold_bets(
    session: Session, config: PolicyConfig, today: date
) -> BoldBetsResult:
    """Bold-bets sleeve = accounts tagged ``bold-bet`` ∪ watchlist symbols.

    REQ-BBT-001: per-position cost/value/unrealized + realized from
    RealizedGainLoss; sleeve totals + % of investable base; each row carries its
    thesis/exit notes. REQ-BBT-002: cap breach is a display flag only.
    """
    from src.models.history import AccountTag

    tagged_account_ids = {
        a_id
        for (a_id,) in session.query(AccountTag.account_id).filter(
            AccountTag.tag == "bold-bet"
        )
    }
    watchlist = {p.symbol: p for p in config.bold_bets.positions}
    watch_symbols = set(watchlist)

    accounts_by_id = {a.id: a for a in session.query(Account).all()}

    positions: list[BoldBetPositionResult] = []
    for ps in _latest_snapshot_rows(session):
        if ps.market_value is None or ps.market_value <= 0:
            continue
        symbol = (ps.symbol or "").upper()
        in_sleeve_account = ps.account_id in tagged_account_ids
        in_watchlist = symbol in watch_symbols
        if not (in_sleeve_account or in_watchlist):
            continue
        acct = accounts_by_id.get(ps.account_id)
        mv = Decimal(str(ps.market_value))
        cost = Decimal(str(ps.cost_basis)) if ps.cost_basis is not None else None
        realized = _realized_for(session, ps.account_id, symbol or None)
        meta = watchlist.get(symbol)
        positions.append(
            BoldBetPositionResult(
                symbol=symbol or "UNKNOWN",
                account_id=ps.account_id,
                account_name=acct.account_name if acct else None,
                market_value=mv,
                cost_basis=cost,
                unrealized_gain=(mv - cost) if cost is not None else None,
                realized_gain=realized,
                thesis=meta.thesis if meta else None,
                exit=meta.exit if meta else None,
            )
        )

    sleeve_value = sum((p.market_value for p in positions), Decimal("0"))
    sleeve_cost = sum(
        (p.cost_basis for p in positions if p.cost_basis is not None), Decimal("0")
    )
    sleeve_unrealized = sum(
        (p.unrealized_gain for p in positions if p.unrealized_gain is not None),
        Decimal("0"),
    )
    sleeve_realized = sum((p.realized_gain for p in positions), Decimal("0"))

    investable_base = sum(
        (
            Decimal(str(ps.market_value))
            for ps in _latest_snapshot_rows(session)
            if ps.account_id in _investable_account_ids(session)
            and ps.market_value is not None
            and ps.market_value > 0
        ),
        Decimal("0"),
    )
    pct = sleeve_value / investable_base if investable_base > 0 else Decimal("0")

    return BoldBetsResult(
        positions=positions,
        sleeve_value=sleeve_value,
        sleeve_cost_basis=sleeve_cost,
        sleeve_unrealized=sleeve_unrealized,
        sleeve_realized=sleeve_realized,
        cap=config.bold_bets.cap,
        over_cap=sleeve_value > config.bold_bets.cap,
        pct_of_investable=pct,
        investable_base=investable_base,
    )


def _realized_for(session: Session, account_id: str, symbol: str | None) -> Decimal:
    q = session.query(RealizedGainLoss).filter(
        RealizedGainLoss.account_id == account_id
    )
    if symbol is not None:
        q = q.filter(RealizedGainLoss.symbol == symbol)
    total = Decimal("0")
    for lot in q.all():
        if lot.gain_loss is not None:
            total += Decimal(str(lot.gain_loss))
    return total


__all__ = [
    "BoldBetPositionResult",
    "BoldBetsResult",
    "PolicyResult",
    "SymbolConcentration",
    "compute_bold_bets",
    "compute_policy",
    "compute_realized_lt_gains_ytd",
]
