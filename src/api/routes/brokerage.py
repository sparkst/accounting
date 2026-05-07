"""Read-only brokerage API endpoints.

Exposes the pure-function output from `src.reports.brokerage_summary` plus
historical-balance, benchmark, and per-holding views over the `history.py`
tables. Mostly GET; PUT on `/accounts/{id}/tags` for the small tag-edit
surface. Mounted at `/api/brokerage` behind the API-key auth dependency in
`src/api/main.py`.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Generator
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, field_serializer, field_validator
from sqlalchemy import func
from sqlalchemy.orm import Session

from src.db.connection import SessionLocal
from src.models.brokerage import Account, PositionSnapshot
from src.models.history import (
    AccountBalanceSnapshot,
    AccountTag,
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


def _today() -> date:
    """Wrapper around ``date.today`` that tests monkeypatch for determinism."""
    return date.today()


# ── Pydantic response models ────────────────────────────────────────────


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
    tags: list[str] = []

    @field_serializer("market_value")
    def _ser_mv(self, v: Decimal) -> float:
        return float(v)


# Tag pattern: lower-case letters, digits, hyphens, underscores; 1–32 chars.
_TAG_PATTERN = re.compile(r"^[a-z0-9_-]{1,32}$")


class AccountTagsUpdate(BaseModel):
    """Request body for PUT /accounts/{id}/tags — full replacement."""

    tags: list[str]

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, v: list[str]) -> list[str]:
        normalised: list[str] = []
        for raw in v:
            if not isinstance(raw, str):
                raise ValueError("tags must be strings")
            t = raw.strip().lower()
            if not _TAG_PATTERN.match(t):
                raise ValueError(
                    f"invalid tag {raw!r}: must match ^[a-z0-9_-]{{1,32}}$"
                )
            normalised.append(t)
        # Deduplicate while preserving insertion order.
        seen: set[str] = set()
        unique: list[str] = []
        for t in normalised:
            if t not in seen:
                seen.add(t)
                unique.append(t)
        return unique


class AccountTagsResponse(BaseModel):
    account_id: str
    tags: list[str]


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


def _load_tags_by_account(session: Session) -> dict[str, list[str]]:
    """Single-query fetch of all (account_id, tag) rows, grouped by account_id.

    Returned dict has tags sorted lower-case for deterministic API output.
    Avoids N+1 by reading the entire `account_tag` table once.
    """
    rows = session.query(AccountTag.account_id, AccountTag.tag).all()
    grouped: dict[str, list[str]] = {}
    for account_id, tag in rows:
        grouped.setdefault(account_id, []).append(tag)
    for account_id in grouped:
        grouped[account_id].sort()
    return grouped


@router.get("/brokerage/accounts", response_model=list[AccountSummaryRow])
def accounts(session: Session = Depends(get_db)) -> list[dict[str, Any]]:  # noqa: B008
    """All accounts (including plan-wrappers, flagged), sorted by market_value desc.

    Includes per-account `tags` (sorted, lower-case, possibly empty list).
    """
    rows = get_account_summary(session)
    tags_by_account = _load_tags_by_account(session)
    for row in rows:
        row["tags"] = tags_by_account.get(row["account_id"], [])
    return rows


@router.put(
    "/brokerage/accounts/{account_id}/tags",
    response_model=AccountTagsResponse,
)
def update_account_tags(
    payload: AccountTagsUpdate,
    account_id: str = Path(min_length=1, max_length=64),
    session: Session = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    """Replace the full set of tags on an account.

    Wholesale-replacement semantics: all existing AccountTag rows for the
    account are deleted, then the new (validated, lower-cased, de-duped)
    set is inserted in a single transaction. Returns the new tag list.
    """
    account_exists = session.query(Account.id).filter(Account.id == account_id).first()
    if account_exists is None:
        raise HTTPException(status_code=404, detail=f"account {account_id} not found")

    session.query(AccountTag).filter(AccountTag.account_id == account_id).delete(
        synchronize_session=False
    )
    for tag in payload.tags:
        session.add(AccountTag(account_id=account_id, tag=tag))
    session.commit()

    return {"account_id": account_id, "tags": list(payload.tags)}


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


# ── Net-worth history ──────────────────────────────────────────────────


def _parse_csv_param(raw: str | None) -> list[str]:
    """Split a comma-separated query param into a list of trimmed non-empty values."""
    if not raw:
        return []
    return [s.strip() for s in raw.split(",") if s.strip()]


def _resolve_filtered_account_ids(
    session: Session,
    *,
    tags_include: list[str],
    tags_exclude: list[str],
    account_ids: list[str],
) -> set[str] | None:
    """Compute the set of account_ids matching the tag/account filters.

    Semantics:
      - account_ids (if non-empty): start from this explicit set.
      - tags_include: AND-of-tags — account must carry every listed tag.
      - tags_exclude: account must carry NONE of these tags.
      - All filters combine with AND.

    Returns None when no filters were supplied (caller skips filtering).
    Returns an empty set when filters are supplied but resolve to nothing —
    caller should yield an empty series.
    """
    if not tags_include and not tags_exclude and not account_ids:
        return None

    # Normalise tags lower-case (matches insert-time normalisation).
    inc = [t.strip().lower() for t in tags_include if t.strip()]
    exc = [t.strip().lower() for t in tags_exclude if t.strip()]

    # Start population: explicit account_ids if provided, else all accounts.
    if account_ids:
        candidate_ids: set[str] = set(account_ids)
    else:
        candidate_ids = {a_id for a_id, in session.query(Account.id).all()}

    if inc:
        # AND-of-tags: count distinct matching tags per account_id and require
        # the count == len(inc). Done in Python from a single query rather than
        # building a HAVING-with-IN, which is cleaner with SQLAlchemy and small N.
        rows = (
            session.query(AccountTag.account_id, AccountTag.tag)
            .filter(AccountTag.tag.in_(inc))
            .all()
        )
        per_account: dict[str, set[str]] = {}
        for a_id, tag in rows:
            per_account.setdefault(a_id, set()).add(tag)
        required = set(inc)
        with_all = {a_id for a_id, tags in per_account.items() if required.issubset(tags)}
        candidate_ids &= with_all

    if exc:
        excluded_ids = {
            a_id
            for a_id, in session.query(AccountTag.account_id)
            .filter(AccountTag.tag.in_(exc))
            .distinct()
            .all()
        }
        candidate_ids -= excluded_ids

    return candidate_ids


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
    tags_include: str | None = Query(
        None,
        description=(
            "Comma-separated list of tags. Account must carry ALL listed tags "
            "(AND semantics)."
        ),
    ),
    tags_exclude: str | None = Query(
        None,
        description="Comma-separated list of tags to exclude (any match excludes).",
    ),
    account_ids: str | None = Query(
        None,
        description="Comma-separated explicit account_id allow-list.",
    ),
    session: Session = Depends(get_db),  # noqa: B008
) -> list[dict[str, Any]]:
    """Aggregated balance series from `account_balance_snapshot`.

    For each `as_of` date, sum balances across accounts. Rows linked to an
    `expected_account` whose status is `'closed'` are excluded.

    Optional filters: `tags_include` (AND), `tags_exclude` (NONE), `account_ids`
    (explicit allow-list). When supplied, only snapshots whose `account_id`
    is in the resolved set are aggregated. An empty resolved set yields an
    empty series (not an error).

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

    allowed_ids = _resolve_filtered_account_ids(
        session,
        tags_include=_parse_csv_param(tags_include),
        tags_exclude=_parse_csv_param(tags_exclude),
        account_ids=_parse_csv_param(account_ids),
    )
    if allowed_ids is not None and not allowed_ids:
        return []

    query = session.query(AccountBalanceSnapshot).order_by(AccountBalanceSnapshot.as_of.asc())
    if not include_unmatched:
        query = query.filter(AccountBalanceSnapshot.account_id.isnot(None))
    if allowed_ids is not None:
        query = query.filter(AccountBalanceSnapshot.account_id.in_(allowed_ids))

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


# ── Missing-accounts panel ─────────────────────────────────────────────


# 60 days = monthly statement cadence + ~30-day grace before we surface an
# account as needing attention. Tunable per-call via the `stale_days` query.
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
    today = _today()
    cutoff = today - timedelta(days=stale_days)

    # Latest as_of per account_id, taking the max across BOTH the
    # account_balance_snapshot table (XLSX historical balances) AND the
    # position_snapshot table (live brokerage feed). Without the position
    # source, accounts that are reporting freshly via brokerage CSV but
    # haven't been XLSX-matched would falsely appear as "never seen."
    latest_by_account: dict[str, date] = {}

    def _record(account_id: str | None, latest_as_of: object) -> None:
        if account_id is None or latest_as_of is None:
            return
        if isinstance(latest_as_of, datetime):
            latest_as_of = latest_as_of.date()
        if not isinstance(latest_as_of, date):
            return
        prev = latest_by_account.get(account_id)
        if prev is None or latest_as_of > prev:
            latest_by_account[account_id] = latest_as_of

    bal_rows = (
        session.query(
            AccountBalanceSnapshot.account_id,
            func.max(AccountBalanceSnapshot.as_of).label("latest_as_of"),
        )
        .filter(AccountBalanceSnapshot.account_id.isnot(None))
        .group_by(AccountBalanceSnapshot.account_id)
        .all()
    )
    for account_id, latest_as_of in bal_rows:
        _record(account_id, latest_as_of)

    pos_rows = (
        session.query(
            PositionSnapshot.account_id,
            func.max(PositionSnapshot.as_of).label("latest_as_of"),
        )
        .group_by(PositionSnapshot.account_id)
        .all()
    )
    for account_id, latest_as_of in pos_rows:
        _record(account_id, latest_as_of)

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


# ── Benchmark-comparison series ────────────────────────────────────────


_ALLOWED_BENCHMARKS: frozenset[str] = frozenset({"SPY", "VTI", "QQQ", "BND"})


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


def _build_price_lookup(
    session: Session, symbol: str
) -> list[tuple[date, Decimal]]:
    """Single-query fetch of (trade_date, close) for ``symbol`` ascending.

    Caller uses the returned list with ``_nearest_price_lookup`` for O(N) total
    work across many target dates instead of N database round-trips.
    """
    rows = (
        session.query(HistoricalPrice.trade_date, HistoricalPrice.close)
        .filter(HistoricalPrice.symbol == symbol)
        .order_by(HistoricalPrice.trade_date.asc())
        .all()
    )
    return [(r[0], r[1]) for r in rows]


def _nearest_price_lookup(
    prices: list[tuple[date, Decimal]], target: date
) -> Decimal | None:
    """Forward-fill: return the close on or just before ``target``."""
    if not prices:
        return None
    # Linear scan from the end is cheap enough for the small N we deal with;
    # bisect would be faster but adds complexity not worth it at this scale.
    last: Decimal | None = None
    for d, close in prices:
        if d > target:
            break
        last = close
    return last


@router.get(
    "/brokerage/networth-history-benchmark",
    response_model=BenchmarkComparisonResponse,
)
def networth_history_benchmark(
    benchmark: str = Query("SPY", min_length=1, max_length=8),
    tags_include: str | None = Query(
        None,
        description="Comma-separated list of tags; account must carry ALL (AND).",
    ),
    tags_exclude: str | None = Query(
        None,
        description="Comma-separated list of tags to exclude (any match excludes).",
    ),
    account_ids: str | None = Query(
        None,
        description="Comma-separated explicit account_id allow-list.",
    ),
    session: Session = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    """Portfolio history alongside a buy-and-hold benchmark simulation.

    ``benchmark`` is restricted to a small allowlist; values outside the
    allowlist return 400. Buy-and-hold sim ignores contributions/withdrawals
    so deltas vs portfolio_value should be read as "vs simulated benchmark"
    rather than perfect attribution.

    Optional account-set filters mirror /networth-history. Empty resolved set
    returns an empty series.
    """
    bench_symbol = benchmark.upper()
    if bench_symbol not in _ALLOWED_BENCHMARKS:
        raise HTTPException(
            status_code=400,
            detail=f"benchmark must be one of {sorted(_ALLOWED_BENCHMARKS)}",
        )

    allowed_ids = _resolve_filtered_account_ids(
        session,
        tags_include=_parse_csv_param(tags_include),
        tags_exclude=_parse_csv_param(tags_exclude),
        account_ids=_parse_csv_param(account_ids),
    )
    if allowed_ids is not None and not allowed_ids:
        return {
            "benchmark_symbol": bench_symbol,
            "series": [],
            "portfolio_pct": None,
            "benchmark_pct": None,
        }

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
    snap_query = (
        session.query(AccountBalanceSnapshot)
        .filter(AccountBalanceSnapshot.account_id.isnot(None))
        .order_by(AccountBalanceSnapshot.as_of.asc())
    )
    if allowed_ids is not None:
        snap_query = snap_query.filter(
            AccountBalanceSnapshot.account_id.in_(allowed_ids)
        )
    snapshots = snap_query.all()
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

    # Single-query fetch of the benchmark's full price history; subsequent
    # per-date lookups walk the in-memory list. Avoids N+1.
    bench_prices = _build_price_lookup(session, bench_symbol)
    bench_anchor_price = _nearest_price_lookup(bench_prices, sorted_dates[0])
    shares: Decimal | None = (
        initial_portfolio / bench_anchor_price
        if bench_anchor_price is not None and bench_anchor_price > 0
        else None
    )

    series: list[dict[str, Any]] = []
    benchmark_final: Decimal | None = None
    for d in sorted_dates:
        port_v = by_date[d]
        bench_price = _nearest_price_lookup(bench_prices, d)
        if shares is not None and bench_price is not None:
            bench_v: Decimal | None = (shares * bench_price).quantize(Decimal("0.01"))
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


# ── Per-holding history ────────────────────────────────────────────────


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
    symbol: str = Path(min_length=1, max_length=16, pattern=r"^[A-Za-z0-9.\-]+$"),
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
