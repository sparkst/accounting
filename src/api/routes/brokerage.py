"""Read-only brokerage API endpoints.

Exposes the pure-function output from `src.reports.brokerage_summary` as
JSON over HTTP. All endpoints are GET; no mutation. Mounted at `/api/brokerage`
behind the existing API-key auth dependency in `src/api/main.py`.

REQ-005a..g visibility — Option 2.
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, field_serializer
from sqlalchemy import func
from sqlalchemy.orm import Session

from src.db.connection import SessionLocal
from src.models.brokerage import PositionSnapshot
from src.models.history import (
    AccountBalanceSnapshot,
    CostBasisLot,
    ExpectedAccount,
    HistoricalPrice,
)
from src.reports.brokerage_summary import (
    compute_data_integrity,
    compute_net_worth,
    get_account_summary,
    get_realized_gl_summary,
    get_recent_transactions,
    get_top_holdings,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def get_db() -> Generator[Session, None, None]:
    """Yield a database session, ensuring cleanup."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


# ── Pydantic response models ────────────────────────────────────────────


def _decimal_to_float(d: Decimal | None) -> float | None:
    """Project convention: Decimal serialised as float in API responses
    (mirrors `transactions.py:107`)."""
    return float(d) if d is not None else None


class NetWorthResponse(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    total: Decimal
    by_broker: dict[str, Decimal]
    by_entity: dict[str, Decimal]
    as_of_min: date | None = None
    as_of_max: date | None = None
    zero_snapshot_account_count: int
    plan_wrapper_excluded_count: int

    @field_serializer("total")
    def _ser_total(self, v: Decimal) -> float:
        return float(v)

    @field_serializer("by_broker", "by_entity")
    def _ser_dict(self, v: dict[str, Decimal]) -> dict[str, float]:
        return {k: float(d) for k, d in v.items()}


class AccountSummaryRow(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    account_id: str
    broker: str
    account_number_masked: str
    account_name: str | None = None
    account_type: str
    entity: str
    tax_sheltered: bool
    is_plan_wrapper: bool
    as_of: date | None = None
    market_value: Decimal

    @field_serializer("market_value")
    def _ser_mv(self, v: Decimal) -> float:
        return float(v)


class TopHoldingRow(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    symbol: str | None = None
    description: str | None = None
    total_quantity: Decimal
    total_market_value: Decimal
    pct_of_net_worth: Decimal
    account_count: int
    is_cash_sleeve: bool

    @field_serializer("total_quantity", "total_market_value", "pct_of_net_worth")
    def _ser_dec(self, v: Decimal) -> float:
        return float(v)


class RecentTransactionRow(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    trade_date: date
    broker: str
    account_number_masked: str
    action: str
    canonical_action: str
    symbol: str | None = None
    quantity: Decimal | None = None
    amount: Decimal | None = None

    @field_serializer("quantity", "amount")
    def _ser_dec(self, v: Decimal | None) -> float | None:
        return float(v) if v is not None else None


class RealizedGLBucket(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    short_term: Decimal
    long_term: Decimal
    unknown: Decimal
    total: Decimal
    lots: int

    @field_serializer("short_term", "long_term", "unknown", "total")
    def _ser_dec(self, v: Decimal) -> float:
        return float(v)


class WashSalesSummary(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    lots: int
    total_disallowed_loss: Decimal

    @field_serializer("total_disallowed_loss")
    def _ser_dec(self, v: Decimal) -> float:
        return float(v)


class RealizedGLSummaryResponse(BaseModel):
    by_year: dict[int, RealizedGLBucket]
    wash_sales: WashSalesSummary


class DataIntegrityResponse(BaseModel):
    accounts: int
    transactions: int
    position_snapshots: int
    realized_lots: int
    orphan_transactions: int
    orphan_snapshots: int
    stale_snapshot_accounts: int
    suspect_symbols: int
    duplicate_position_groups: int
    duplicate_transaction_groups: int


# ── Routes ──────────────────────────────────────────────────────────────


@router.get("/brokerage/networth", response_model=NetWorthResponse)
def networth(session: Session = Depends(get_db)) -> dict[str, Any]:  # noqa: B008
    """Net worth total + per-broker / per-entity breakdowns."""
    return compute_net_worth(session)


@router.get("/brokerage/accounts", response_model=list[AccountSummaryRow])
def accounts(session: Session = Depends(get_db)) -> list[dict[str, Any]]:  # noqa: B008
    """All accounts (including plan-wrappers, flagged), sorted by market_value desc."""
    return get_account_summary(session)


@router.get("/brokerage/top-holdings", response_model=list[TopHoldingRow])
def top_holdings(
    n: int = Query(10, ge=1, le=200),
    session: Session = Depends(get_db),  # noqa: B008
) -> list[dict[str, Any]]:
    """Top N positions across accounts. Cash sleeves folded; wrapper excluded."""
    nw = compute_net_worth(session)
    return get_top_holdings(session, net_worth_total=nw["total"], n=n)


@router.get("/brokerage/recent-transactions", response_model=list[RecentTransactionRow])
def recent_transactions(
    days: int = Query(14, ge=1, le=365),
    session: Session = Depends(get_db),  # noqa: B008
) -> list[dict[str, Any]]:
    """Recent transactions (default 14 days). Filters REJECTED and reinvest partner."""
    return get_recent_transactions(session, days=days)


@router.get("/brokerage/realized-gl", response_model=RealizedGLSummaryResponse)
def realized_gl(session: Session = Depends(get_db)) -> dict[str, Any]:  # noqa: B008
    """Realized G/L by year + wash-sale summary."""
    return get_realized_gl_summary(session)


@router.get("/brokerage/data-integrity", response_model=DataIntegrityResponse)
def data_integrity(
    stale_days: int = Query(7, ge=1, le=365),
    session: Session = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    """Counts and integrity checks. Surfaces adapter-bug indicators."""
    return compute_data_integrity(session, stale_days=stale_days)


# ── Phase 3: net-worth history ──────────────────────────────────────────


class NetWorthHistoryPoint(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    as_of: date
    balance_total: Decimal
    account_count: int

    @field_serializer("balance_total")
    def _ser_total(self, v: Decimal) -> float:
        return float(v)


@router.get(
    "/brokerage/networth-history",
    response_model=list[NetWorthHistoryPoint],
)
def networth_history(
    include_unmatched: bool = Query(
        False,
        description=(
            "If True, include rows whose XLSX raw_account_name didn't match "
            "a live Account. Defaults False so users see the verified series."
        ),
    ),
    session: Session = Depends(get_db),  # noqa: B008
) -> list[dict[str, Any]]:
    """Aggregated balance series from `account_balance_snapshot`.

    For each `as_of` date, sum balances across accounts. Rows linked to an
    `expected_account` whose status is `'closed'` are excluded.

    Returned ascending by date.
    """
    closed_account_ids: set[str] = {
        row[0]
        for row in session.query(ExpectedAccount.resolved_account_id)
        .filter(
            ExpectedAccount.status == "closed",
            ExpectedAccount.resolved_account_id.isnot(None),
        )
        .all()
    }

    query = session.query(AccountBalanceSnapshot).order_by(AccountBalanceSnapshot.as_of.asc())
    if not include_unmatched:
        query = query.filter(AccountBalanceSnapshot.account_id.isnot(None))

    by_date: dict[date, dict[str, Any]] = {}
    for snap in query.all():
        if snap.account_id in closed_account_ids:
            continue
        slot = by_date.setdefault(
            snap.as_of, {"as_of": snap.as_of, "balance_total": Decimal("0"), "account_count": 0}
        )
        slot["balance_total"] += snap.balance
        slot["account_count"] += 1

    return [by_date[k] for k in sorted(by_date.keys())]


# ── Phase 3 T18: missing-accounts panel ────────────────────────────────────


# Snapshots older than this are considered "stale" — the resolved Account
# has not had a balance update recently enough to count as covered.
_STALE_SNAPSHOT_DAYS = 60


class MissingAccountRow(BaseModel):
    """One row in the /brokerage/missing-accounts panel.

    ``last_seen_days_ago`` is None when no resolved Account is linked, or
    when the linked Account has zero snapshots.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    institution: str
    account_name: str
    last_4: str | None = None
    status: str
    source: str
    resolved_account_id: str | None = None
    last_seen_days_ago: int | None = None


@router.get(
    "/brokerage/missing-accounts",
    response_model=list[MissingAccountRow],
)
def missing_accounts(
    stale_days: int = Query(
        _STALE_SNAPSHOT_DAYS,
        ge=1,
        le=365,
        description=(
            "Snapshots older than this many days count as stale. Defaults to 60."
        ),
    ),
    session: Session = Depends(get_db),  # noqa: B008
) -> list[dict[str, Any]]:
    """Active expected_accounts whose live coverage is missing or stale.

    A row is missing when:
      - status == 'active', AND
      - resolved_account_id IS NULL, OR
      - the resolved account's most-recent ``account_balance_snapshot.as_of``
        is older than ``stale_days``.
    """
    today = date.today()
    cutoff = today - timedelta(days=stale_days)

    # Pre-aggregate latest as_of per account_id (only for accounts that have
    # at least one snapshot).
    latest_rows = (
        session.query(
            AccountBalanceSnapshot.account_id,
            func.max(AccountBalanceSnapshot.as_of).label("latest_as_of"),
        )
        .filter(AccountBalanceSnapshot.account_id.isnot(None))
        .group_by(AccountBalanceSnapshot.account_id)
        .all()
    )
    latest_by_account: dict[str, date] = {}
    for account_id, latest_as_of in latest_rows:
        if account_id is None or latest_as_of is None:
            continue
        # SQLite may hand back a datetime where SQLAlchemy expects date —
        # normalise both to date for the comparison.
        if isinstance(latest_as_of, datetime):
            latest_as_of = latest_as_of.date()
        latest_by_account[account_id] = latest_as_of

    out: list[dict[str, Any]] = []
    expected_rows = (
        session.query(ExpectedAccount)
        .filter(ExpectedAccount.status == "active")
        .all()
    )
    for exp in expected_rows:
        if exp.resolved_account_id is None:
            out.append(
                {
                    "id": exp.id,
                    "institution": exp.institution,
                    "account_name": exp.account_name,
                    "last_4": exp.last_4,
                    "status": exp.status,
                    "source": exp.source,
                    "resolved_account_id": None,
                    "last_seen_days_ago": None,
                }
            )
            continue

        latest = latest_by_account.get(exp.resolved_account_id)
        if latest is None:
            # Linked account has zero snapshots — treat as missing.
            out.append(
                {
                    "id": exp.id,
                    "institution": exp.institution,
                    "account_name": exp.account_name,
                    "last_4": exp.last_4,
                    "status": exp.status,
                    "source": exp.source,
                    "resolved_account_id": exp.resolved_account_id,
                    "last_seen_days_ago": None,
                }
            )
            continue

        if latest < cutoff:
            out.append(
                {
                    "id": exp.id,
                    "institution": exp.institution,
                    "account_name": exp.account_name,
                    "last_4": exp.last_4,
                    "status": exp.status,
                    "source": exp.source,
                    "resolved_account_id": exp.resolved_account_id,
                    "last_seen_days_ago": (today - latest).days,
                }
            )
        # else: fresh snapshot — exclude.

    return out


# ── Phase 3 T12: benchmark-comparison series ───────────────────────────


class BenchmarkPoint(BaseModel):
    """One point in a benchmark buy-and-hold simulation aligned with the
    portfolio history series.

    The simulation is deliberately simple: buy ``initial_portfolio`` worth of
    the benchmark on the first ``as_of`` date and hold. We do NOT model
    contributions/withdrawals — interpret deltas vs portfolio_value as
    "vs simulated benchmark" rather than perfect attribution.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    as_of: date
    portfolio_value: Decimal
    benchmark_value: Decimal | None

    @field_serializer("portfolio_value")
    def _ser_p(self, v: Decimal) -> float:
        return float(v)

    @field_serializer("benchmark_value")
    def _ser_b(self, v: Decimal | None) -> float | None:
        return float(v) if v is not None else None


class BenchmarkComparisonResponse(BaseModel):
    """Response for /brokerage/networth-history-benchmark."""

    benchmark_symbol: str
    series: list[BenchmarkPoint]
    portfolio_pct: float | None
    benchmark_pct: float | None


def _nearest_price(
    session: Session, symbol: str, target: date
) -> Decimal | None:
    """Return the close on or just before ``target`` (forward-fill behavior)."""
    row = (
        session.query(HistoricalPrice.close)
        .filter(
            HistoricalPrice.symbol == symbol,
            HistoricalPrice.trade_date <= target,
        )
        .order_by(HistoricalPrice.trade_date.desc())
        .first()
    )
    return row[0] if row is not None else None


@router.get(
    "/brokerage/networth-history-benchmark",
    response_model=BenchmarkComparisonResponse,
)
def networth_history_benchmark(
    benchmark: str = Query("SPY", min_length=1, max_length=16),
    session: Session = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    """Portfolio history alongside a buy-and-hold benchmark simulation.

    Returns ``series`` aligned to portfolio dates with ``benchmark_value`` set
    when a price is available for that date (or earlier — forward-filled).
    """
    bench_symbol = benchmark.upper()

    # Reuse the matched-only portfolio series.
    closed_account_ids: set[str] = {
        row[0]
        for row in session.query(ExpectedAccount.resolved_account_id)
        .filter(
            ExpectedAccount.status == "closed",
            ExpectedAccount.resolved_account_id.isnot(None),
        )
        .all()
    }
    snapshots = (
        session.query(AccountBalanceSnapshot)
        .filter(AccountBalanceSnapshot.account_id.isnot(None))
        .order_by(AccountBalanceSnapshot.as_of.asc())
        .all()
    )
    by_date: dict[date, Decimal] = {}
    for snap in snapshots:
        if snap.account_id in closed_account_ids:
            continue
        by_date[snap.as_of] = by_date.get(snap.as_of, Decimal("0")) + snap.balance

    if not by_date:
        return {
            "benchmark_symbol": bench_symbol,
            "series": [],
            "portfolio_pct": None,
            "benchmark_pct": None,
        }

    sorted_dates = sorted(by_date.keys())
    initial_portfolio = by_date[sorted_dates[0]]

    # Anchor benchmark at first portfolio date.
    bench_anchor_price = _nearest_price(session, bench_symbol, sorted_dates[0])
    series: list[dict[str, Any]] = []
    benchmark_final: Decimal | None = None
    for d in sorted_dates:
        port_v = by_date[d]
        bench_price = _nearest_price(session, bench_symbol, d)
        if bench_anchor_price is not None and bench_anchor_price > 0 and bench_price is not None:
            shares = initial_portfolio / bench_anchor_price
            bench_v = (shares * bench_price).quantize(Decimal("0.01"))
        else:
            bench_v = None
        if bench_v is not None:
            benchmark_final = bench_v
        series.append(
            {
                "as_of": d,
                "portfolio_value": port_v,
                "benchmark_value": bench_v,
            }
        )

    portfolio_final = by_date[sorted_dates[-1]]
    portfolio_pct: float | None = (
        float((portfolio_final - initial_portfolio) / initial_portfolio)
        if initial_portfolio > 0
        else None
    )
    benchmark_pct: float | None = (
        float((benchmark_final - initial_portfolio) / initial_portfolio)
        if benchmark_final is not None and initial_portfolio > 0
        else None
    )

    return {
        "benchmark_symbol": bench_symbol,
        "series": series,
        "portfolio_pct": portfolio_pct,
        "benchmark_pct": benchmark_pct,
    }


# ── Phase 3 T14: per-holding history ───────────────────────────────────


class HoldingValuePoint(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    as_of: date
    market_value: Decimal
    quantity: Decimal

    @field_serializer("market_value", "quantity")
    def _ser(self, v: Decimal) -> float:
        return float(v)


class HoldingLotRow(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    open_date: date
    raw_account_name: str
    quantity: Decimal
    cost_per_share: Decimal
    cost_total: Decimal
    source: str

    @field_serializer("quantity", "cost_per_share", "cost_total")
    def _ser(self, v: Decimal) -> float:
        return float(v)


class HoldingHistoryResponse(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    symbol: str
    security_name: str | None = None
    current_value: Decimal
    current_quantity: Decimal
    cost_basis: Decimal
    unrealized_gain: Decimal
    unrealized_pct: float
    value_series: list[HoldingValuePoint]
    lots: list[HoldingLotRow]

    @field_serializer("current_value", "current_quantity", "cost_basis", "unrealized_gain")
    def _ser(self, v: Decimal) -> float:
        return float(v)


@router.get(
    "/brokerage/holdings/{symbol}/history",
    response_model=HoldingHistoryResponse,
)
def holding_history(
    symbol: str,
    session: Session = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    """Per-symbol time series of position value + lot-level cost basis.

    Aggregates ``PositionSnapshot`` rows for the symbol across all accounts
    by ``as_of`` date. Lots come from ``cost_basis_lot`` (XLSX-imported
    historical lots — read-only/reference).
    """
    sym = symbol.upper()

    # Aggregate snapshots across accounts per as_of date.
    rows = (
        session.query(PositionSnapshot)
        .filter(func.upper(PositionSnapshot.symbol) == sym)
        .order_by(PositionSnapshot.as_of.asc())
        .all()
    )
    by_date: dict[date, dict[str, Decimal]] = {}
    security_name: str | None = None
    for snap in rows:
        as_of_d = snap.as_of.date() if isinstance(snap.as_of, datetime) else snap.as_of
        slot = by_date.setdefault(
            as_of_d,
            {"market_value": Decimal("0"), "quantity": Decimal("0"), "cost_basis": Decimal("0")},
        )
        if snap.market_value is not None:
            slot["market_value"] += snap.market_value
        if snap.quantity is not None:
            slot["quantity"] += snap.quantity
        if snap.cost_basis is not None:
            slot["cost_basis"] += snap.cost_basis
        if security_name is None and snap.description:
            security_name = snap.description

    sorted_dates = sorted(by_date.keys())
    value_series = [
        {
            "as_of": d,
            "market_value": by_date[d]["market_value"],
            "quantity": by_date[d]["quantity"],
        }
        for d in sorted_dates
    ]

    if sorted_dates:
        latest = by_date[sorted_dates[-1]]
        current_value = latest["market_value"]
        current_quantity = latest["quantity"]
        cost_basis = latest["cost_basis"]
    else:
        current_value = Decimal("0")
        current_quantity = Decimal("0")
        cost_basis = Decimal("0")

    unrealized_gain = current_value - cost_basis
    unrealized_pct = float(unrealized_gain / cost_basis) if cost_basis > 0 else 0.0

    # Pull historical lots ingested from XLSX.
    lot_rows = (
        session.query(CostBasisLot)
        .filter(func.upper(CostBasisLot.symbol) == sym)
        .order_by(CostBasisLot.open_date.asc())
        .all()
    )
    lots = [
        {
            "open_date": lot.open_date,
            "raw_account_name": lot.raw_account_name,
            "quantity": lot.quantity,
            "cost_per_share": lot.cost_per_share,
            "cost_total": lot.cost_total,
            "source": lot.source,
        }
        for lot in lot_rows
    ]

    return {
        "symbol": sym,
        "security_name": security_name,
        "current_value": current_value,
        "current_quantity": current_quantity,
        "cost_basis": cost_basis,
        "unrealized_gain": unrealized_gain,
        "unrealized_pct": unrealized_pct,
        "value_series": value_series,
        "lots": lots,
    }
