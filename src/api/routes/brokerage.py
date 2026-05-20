"""Read-only brokerage API endpoints.

Exposes the pure-function output from `src.reports.brokerage_summary` plus
historical-balance, benchmark, and per-holding views over the `history.py`
tables. Mostly GET; PUT on `/accounts/{id}/tags` for the small tag-edit
surface; PATCH on `/accounts/{id}` for human-curated metadata updates.
Mounted at `/api/brokerage` behind the API-key auth dependency in
`src/api/main.py`.
"""

from __future__ import annotations

import logging
import re
from calendar import monthrange
from collections.abc import Generator
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator
from sqlalchemy import func
from sqlalchemy.orm import Session

from src.analytics.classify import AccountScope, PortfolioScope, PositionScope, Scope
from src.analytics.performance import (
    CashFlow,
    money_weighted_return,
    principal_growth_series,
    tracked_value_at,
)
from src.db.connection import SessionLocal
from src.models.audit_event import (
    ENTITY_TYPE_BROKERAGE_TRANSACTION,
    AuditEvent,
)
from src.models.brokerage import (
    Account,
    BrokerageTransaction,
    PositionSnapshot,
    RealizedGainLoss,
)
from src.models.enums import BrokerageTxStatus, CanonicalAction, CashFlowType, GainLossTerm
from src.models.history import (
    AccountBalanceSnapshot,
    AccountTag,
    CostBasisLot,
    ExpectedAccount,
    HistoricalPrice,
)
from src.models.ingestion_log import IngestionLog
from src.reports.brokerage_summary import (
    _latest_at_or_before,
    _load_history_state,
    _mask_account_number,
    _per_account_value_at,
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

    model_config = ConfigDict(extra="forbid")

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


class AccountPatchRequest(BaseModel):
    """Partial-update body for PATCH /accounts/{id}.

    All fields are optional. We rely on Pydantic v2's ``model_fields_set`` to
    distinguish "field omitted" (leave existing value alone) from "field
    explicitly set to null" (clear the column to NULL). Max-length validators
    mirror the SQLAlchemy column widths in `Account` (`account_name=128`,
    `beneficiary=64`); `notes` is unbounded in the DB but capped at 4096
    chars here to bound payload size (Tailscale-only audience).
    """

    model_config = ConfigDict(extra="forbid")

    account_name: str | None = Field(default=None, max_length=128)
    beneficiary: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=4096)


class AccountPatchResponse(BaseModel):
    """Response for PATCH /accounts/{id} — the post-update row."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    account_id: str
    account_name: str | None = None
    beneficiary: str | None = None
    notes: str | None = None
    updated_at: datetime


# ── Account-detail response models ─────────────────────────────────────


class AccountDetailAccount(BaseModel):
    """Full Account row + tags, for GET /accounts/{id}/detail."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    broker: str
    account_number_masked: str
    account_name: str | None = None
    account_type: str
    entity: str
    tax_sheltered: bool
    beneficiary: str | None = None
    notes: str | None = None
    parent_account_id: str | None = None
    is_plan_wrapper: bool
    created_at: datetime
    updated_at: datetime
    tags: list[str] = []


class PositionSnapshotDetailRow(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    as_of: datetime
    symbol: str | None = None
    description: str | None = None
    quantity: Decimal | None = None
    price: Decimal | None = None
    market_value: Decimal | None = None
    source_file: str
    source_row_hash: str

    @field_serializer("quantity", "price", "market_value")
    def _ser_dec(self, v: Decimal | None) -> float | None:
        return float(v) if v is not None else None


class BalanceSnapshotDetailRow(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    as_of: date
    raw_account_name: str
    balance: Decimal
    source: str

    @field_serializer("balance")
    def _ser_bal(self, v: Decimal) -> float:
        return float(v)


class AccountRealizedGLSummary(BaseModel):
    """Lifetime realized G/L across all years for one account."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    short_term: Decimal
    long_term: Decimal
    total: Decimal
    lots: int

    @field_serializer("short_term", "long_term", "total")
    def _ser_dec(self, v: Decimal) -> float:
        return float(v)


class IngestionLogDetailRow(BaseModel):
    """Subset of IngestionLog fields surfaced in the detail panel.

    ``error_detail`` is truncated to 200 chars server-side to bound the
    payload. Full details are available in the server logs. This endpoint
    is Tailscale-only (operator audience).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    source: str
    run_at: datetime
    status: str
    records_processed: int
    records_failed: int
    error_detail: str | None = None


class AccountDetailResponse(BaseModel):
    """Full account-detail payload for GET /accounts/{id}/detail."""

    account: AccountDetailAccount
    latest_position_snapshots: list[PositionSnapshotDetailRow]
    latest_balance_snapshots: list[BalanceSnapshotDetailRow]
    transaction_count_by_action: dict[str, int]
    realized_gl_summary: AccountRealizedGLSummary
    ingestion_log_recent: list[IngestionLogDetailRow]


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


@router.patch(
    "/brokerage/accounts/{account_id}",
    response_model=AccountPatchResponse,
)
def patch_account(
    payload: AccountPatchRequest,
    account_id: str = Path(min_length=1, max_length=64),
    session: Session = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    """Partial-update of human-curated account metadata.

    Patchable fields: ``account_name``, ``beneficiary``, ``notes``.

    Semantics: only fields the caller actually supplied (per Pydantic v2's
    ``model_fields_set``) are written. Omitting a field leaves the DB column
    untouched. Sending ``null`` explicitly clears the column to NULL. An
    empty body is a valid 200 — returns the unchanged row.

    ``updated_at`` only bumps when at least one column is actually written
    (the SQLAlchemy ``onupdate`` trigger fires per UPDATE statement).
    """
    account = session.query(Account).filter(Account.id == account_id).first()
    if account is None:
        raise HTTPException(status_code=404, detail=f"account {account_id} not found")

    supplied = payload.model_fields_set
    changed = False
    if "account_name" in supplied:
        account.account_name = payload.account_name
        changed = True
    if "beneficiary" in supplied:
        account.beneficiary = payload.beneficiary
        changed = True
    if "notes" in supplied:
        account.notes = payload.notes
        changed = True

    if changed:
        session.commit()
        session.refresh(account)

    return {
        "account_id": account.id,
        "account_name": account.account_name,
        "beneficiary": account.beneficiary,
        "notes": account.notes,
        "updated_at": account.updated_at,
    }


def _summarise_realized_gl_for_account(
    session: Session, account_id: str
) -> dict[str, Any]:
    """Lifetime realized G/L summary for one account.

    Aggregates all RealizedGainLoss rows for the account and buckets them by
    ``term`` into short_term / long_term. Rows missing both ``term`` and
    explicit ``st_gain_loss``/``lt_gain_loss`` (the "unknown" bucket from the
    portfolio-wide summary) fall through to ``total`` only — but at the
    per-account scale we just sum what we can attribute.

    Returns zeros (not None) when the account has no realized lots.
    """
    rows = (
        session.query(RealizedGainLoss)
        .filter(RealizedGainLoss.account_id == account_id)
        .all()
    )
    short_term = Decimal("0")
    long_term = Decimal("0")
    total = Decimal("0")
    for row in rows:
        # Prefer the explicit term-bucketed columns; fall back to ``term``.
        if row.st_gain_loss is not None:
            short_term += row.st_gain_loss
        elif row.term == GainLossTerm.SHORT.value and row.gain_loss is not None:
            short_term += row.gain_loss

        if row.lt_gain_loss is not None:
            long_term += row.lt_gain_loss
        elif row.term == GainLossTerm.LONG.value and row.gain_loss is not None:
            long_term += row.gain_loss

        if row.gain_loss is not None:
            total += row.gain_loss

    return {
        "short_term": short_term,
        "long_term": long_term,
        "total": total,
        "lots": len(rows),
    }


@router.get(
    "/brokerage/accounts/{account_id}/detail",
    response_model=AccountDetailResponse,
)
def account_detail(
    account_id: str = Path(min_length=1, max_length=64),
    session: Session = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    """Full account view: metadata, recent snapshots, transaction roll-up,
    realized G/L, and recent ingestion runs touching this broker.

    Returns 404 when the account_id doesn't exist. All list-shaped fields
    return empty arrays (not nulls) when no data is available so the frontend
    can render uniformly. ``realized_gl_summary`` returns explicit zero
    Decimals when the account has no realized lots.

    Sub-list caps:
      - ``latest_position_snapshots``: 10 most recent by ``as_of`` desc.
      - ``latest_balance_snapshots``: 10 most recent by ``as_of`` desc.
      - ``ingestion_log_recent``: 5 most recent by ``run_at`` desc, filtered
        to logs whose ``source`` contains the account's broker (substring
        match — adapter source values like ``fidelity_csv`` carry the
        broker name).
    """
    account = session.query(Account).filter(Account.id == account_id).first()
    if account is None:
        raise HTTPException(status_code=404, detail=f"account {account_id} not found")

    # Tags (sorted lower-case for deterministic output).
    tag_rows = (
        session.query(AccountTag.tag)
        .filter(AccountTag.account_id == account_id)
        .all()
    )
    tags = sorted(t for (t,) in tag_rows)

    # Latest 10 position snapshots.
    pos_snaps = (
        session.query(PositionSnapshot)
        .filter(PositionSnapshot.account_id == account_id)
        .order_by(PositionSnapshot.as_of.desc())
        .limit(10)
        .all()
    )
    latest_position_snapshots = [
        {
            "id": ps.id,
            "as_of": ps.as_of,
            "symbol": ps.symbol,
            "description": ps.description,
            "quantity": ps.quantity,
            "price": ps.price,
            "market_value": ps.market_value,
            "source_file": ps.source_file,
            "source_row_hash": ps.source_row_hash,
        }
        for ps in pos_snaps
    ]

    # Latest 10 balance snapshots.
    bal_snaps = (
        session.query(AccountBalanceSnapshot)
        .filter(AccountBalanceSnapshot.account_id == account_id)
        .order_by(AccountBalanceSnapshot.as_of.desc())
        .limit(10)
        .all()
    )
    latest_balance_snapshots = [
        {
            "id": bs.id,
            "as_of": bs.as_of,
            "raw_account_name": bs.raw_account_name,
            "balance": bs.balance,
            "source": bs.source,
        }
        for bs in bal_snaps
    ]

    # Transaction count by canonical_action (single GROUP BY query).
    tx_counts_rows = (
        session.query(
            BrokerageTransaction.canonical_action,
            func.count(BrokerageTransaction.id),
        )
        .filter(BrokerageTransaction.account_id == account_id)
        .group_by(BrokerageTransaction.canonical_action)
        .all()
    )
    transaction_count_by_action = {action: count for action, count in tx_counts_rows}

    realized_gl_summary = _summarise_realized_gl_for_account(session, account_id)

    # Ingestion logs whose source carries the broker name (substring match).
    # Adapter source values follow ``<broker>_<source>`` (e.g. fidelity_csv).
    ingestion_rows = (
        session.query(IngestionLog)
        .filter(IngestionLog.source.like(f"%{account.broker}%"))
        .order_by(IngestionLog.run_at.desc())
        .limit(5)
        .all()
    )
    ingestion_log_recent = [
        {
            "id": log.id,
            "source": log.source,
            "run_at": log.run_at,
            "status": log.status,
            "records_processed": log.records_processed,
            "records_failed": log.records_failed,
            # Truncate to 200 chars to bound payload (Tailscale-only audience).
            "error_detail": (log.error_detail[:200] if log.error_detail else None),
        }
        for log in ingestion_rows
    ]

    return {
        "account": {
            "id": account.id,
            "broker": account.broker,
            "account_number_masked": _mask_account_number(account.account_number),
            "account_name": account.account_name,
            "account_type": account.account_type,
            "entity": account.entity,
            "tax_sheltered": account.tax_sheltered,
            "beneficiary": account.beneficiary,
            "notes": account.notes,
            "parent_account_id": account.parent_account_id,
            "is_plan_wrapper": account.is_plan_wrapper,
            "created_at": account.created_at,
            "updated_at": account.updated_at,
            "tags": tags,
        },
        "latest_position_snapshots": latest_position_snapshots,
        "latest_balance_snapshots": latest_balance_snapshots,
        "transaction_count_by_action": transaction_count_by_action,
        "realized_gl_summary": realized_gl_summary,
        "ingestion_log_recent": ingestion_log_recent,
    }


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


def _generate_target_dates(
    start: date, end: date, granularity: Literal["daily", "weekly", "monthly"]
) -> list[date]:
    """Generate target dates from ``start`` through ``end`` inclusive.

    - daily: every calendar day.
    - weekly: every Saturday on or after ``start``.
    - monthly: the last day of every month on or after ``start``.

    The caller is responsible for appending the "today" point if it isn't
    already in the series — the chart should always end at today.
    """
    if start > end:
        return []
    out: list[date] = []
    if granularity == "daily":
        cur = start
        while cur <= end:
            out.append(cur)
            cur = cur + timedelta(days=1)
    elif granularity == "weekly":
        # Saturday = weekday 5. Round start UP to the next Saturday.
        days_to_sat = (5 - start.weekday()) % 7
        cur = start + timedelta(days=days_to_sat)
        while cur <= end:
            out.append(cur)
            cur = cur + timedelta(days=7)
    else:  # monthly
        # Last day of each calendar month on or after start.
        year, month = start.year, start.month
        while True:
            last_day = monthrange(year, month)[1]
            d = date(year, month, last_day)
            if d > end:
                break
            out.append(d)
            month += 1
            if month > 12:
                month = 1
                year += 1
    return out


@router.get(
    "/brokerage/networth-history",
    response_model=list[NetWorthHistoryPoint],
)
def networth_history(
    include_unmatched: bool = Query(
        False,
        description=(
            "If True, include AccountBalanceSnapshot rows whose XLSX "
            "raw_account_name didn't match a live Account; treats each "
            "raw_account_name as its own pseudo-account contributing to the "
            "total. Defaults False so users see only verified accounts."
        ),
    ),
    granularity: Literal["daily", "weekly", "monthly"] = Query(
        "weekly",
        description=(
            "Sampling cadence for the series. ``weekly`` (default) samples "
            "every Saturday. The 'today' point is always appended last."
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
    """Net-worth series with **forward-fill + live re-pricing**.

    For each target date in the generated series, every account's value is
    taken from the most-defensible source:

    1. The latest ``PositionSnapshot`` at-or-before the target date, with
       each held position's ``market_value`` recomputed from
       ``HistoricalPrice`` when available (live re-pricing); otherwise the
       snapshot's stored value (forward-fill).
    2. If no PositionSnapshot exists at-or-before, the latest
       ``AccountBalanceSnapshot`` at-or-before the target date is used.
    3. Accounts with neither snapshot type at-or-before contribute nothing.

    See ``src/reports/brokerage_summary._per_account_value_at`` for the full
    sourcing rules.

    Sampling cadence is controlled by ``granularity`` (default ``weekly``).
    Today's point is always appended last regardless of week alignment.

    Filters:
    - ``tags_include`` (AND), ``tags_exclude`` (NONE), ``account_ids``
      (explicit allow-list) — when supplied, only matching accounts
      contribute. An empty resolved set yields an empty series.
    - ``include_unmatched`` (default False): when True, also includes
      AccountBalanceSnapshot rows whose ``account_id`` is NULL by treating
      ``raw_account_name`` as a pseudo-account in the per-date sum.
    - Closed ``expected_account``-linked accounts are always excluded.
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

    # Series start anchors on the earliest matched snapshot across both
    # tables. Orphan-only rows alone don't generate a long zero-valued series.
    candidates: list[date] = []
    earliest_matched = _earliest_matched_snapshot_date(session)
    if earliest_matched is not None:
        candidates.append(earliest_matched)

    # When include_unmatched=True we surface AccountBalanceSnapshot rows whose
    # account_id is NULL — they have no live Account to forward-fill across,
    # so we use legacy "literal as_of" behaviour and merge into the per-date
    # series. raw_account_name is the natural identity for forward-fill.
    unmatched_by_pseudo: dict[str, list[tuple[date, Decimal]]] = {}
    if include_unmatched:
        for snap in (
            session.query(AccountBalanceSnapshot)
            .filter(AccountBalanceSnapshot.account_id.is_(None))
            .order_by(AccountBalanceSnapshot.as_of.asc())
            .all()
        ):
            unmatched_by_pseudo.setdefault(snap.raw_account_name, []).append(
                (snap.as_of, Decimal(str(snap.balance)))
            )
            candidates.append(snap.as_of)

    if not candidates:
        return []

    start = min(candidates)
    today = _today()
    end = today
    if start > end:
        # Snapshot is in the future — emit just today (forward-fill from there).
        start = end

    # Pre-load once and walk in Python — avoids N+1 across many target dates.
    state = _load_history_state(session)

    target_dates = _generate_target_dates(start, end, granularity)
    if not target_dates or target_dates[-1] != today:
        target_dates.append(today)

    accounts_by_id = state["accounts_by_id"]

    # ── Suppression index for unmatched (legacy XLSX) rows ──────────────
    # Two-tier rule (either fires):
    #   (1) Per-name: a *matched* ABS exists with the same raw_account_name
    #       at-or-before target (lists are sorted asc, so the first element
    #       being <= target proves the suppression for every later target).
    #   (2) Global cutoff: target is at-or-after the earliest matched
    #       PositionSnapshot — live broker data has taken over even when the
    #       XLSX label ("Amy IRA") doesn't string-match the live rollup label
    #       ("Vanguard Amy 65344815"), preventing double-count.
    matched_name_first_date: dict[str, date] = {}
    global_position_cutoff: date | None = None
    if include_unmatched and unmatched_by_pseudo:
        # Derive from already-loaded state instead of re-querying.
        for series in state["balance_snapshots_by_account"].values():
            for as_of, abs_row in series:
                key = abs_row.raw_account_name.lower()
                if key not in matched_name_first_date or as_of < matched_name_first_date[key]:
                    matched_name_first_date[key] = as_of
        global_position_cutoff = _earliest_matched_position_date(session)

    def _unmatched_contribution(target: date) -> tuple[Decimal, int]:
        total = Decimal("0")
        count = 0
        for raw_name, series in unmatched_by_pseudo.items():
            first_matched = matched_name_first_date.get(raw_name.lower())
            if first_matched is not None and first_matched <= target:
                continue  # tier 1
            if global_position_cutoff is not None and target >= global_position_cutoff:
                continue  # tier 2
            latest_bal = _latest_at_or_before(series, target)
            if latest_bal is not None:
                total += latest_bal
                count += 1
        return total, count

    out: list[dict[str, Any]] = []
    for target in target_dates:
        per_account = _per_account_value_at(session, target, history_state=state)
        balance_total, account_count = _sum_per_account_filtered(
            per_account,
            accounts_by_id=accounts_by_id,
            closed_account_ids=closed_account_ids,
            allowed_ids=allowed_ids,
        )
        # Unmatched rows have no Account row and cannot satisfy tag/id filters.
        if include_unmatched and allowed_ids is None and unmatched_by_pseudo:
            extra_total, extra_count = _unmatched_contribution(target)
            balance_total += extra_total
            account_count += extra_count

        out.append({
            "as_of": target,
            "balance_total": balance_total,
            "account_count": account_count,
        })
    return out


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

    Caller uses the returned list with ``_latest_at_or_before`` for O(N) total
    work across many target dates instead of N database round-trips.
    """
    rows = (
        session.query(HistoricalPrice.trade_date, HistoricalPrice.close)
        .filter(HistoricalPrice.symbol == symbol)
        .order_by(HistoricalPrice.trade_date.asc())
        .all()
    )
    return [(r[0], Decimal(str(r[1]))) for r in rows]


def _to_date(v: object) -> date | None:
    """Coerce a SQLAlchemy datetime/date/None to a plain date or None."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return None


def _earliest_matched_snapshot_date(session: Session) -> date | None:
    """Earliest (account_id IS NOT NULL) as_of across both snapshot tables."""
    pos_min = (
        session.query(func.min(PositionSnapshot.as_of))
        .filter(PositionSnapshot.account_id.isnot(None))
        .scalar()
    )
    bal_min = (
        session.query(func.min(AccountBalanceSnapshot.as_of))
        .filter(AccountBalanceSnapshot.account_id.isnot(None))
        .scalar()
    )
    candidates = [d for d in (_to_date(pos_min), _to_date(bal_min)) if d is not None]
    return min(candidates) if candidates else None


def _earliest_matched_position_date(session: Session) -> date | None:
    """Earliest (account_id IS NOT NULL) PositionSnapshot.as_of, as a date."""
    return _to_date(
        session.query(func.min(PositionSnapshot.as_of))
        .filter(PositionSnapshot.account_id.isnot(None))
        .scalar()
    )


def _sum_per_account_filtered(
    per_account: dict[str, dict[str, Any]],
    *,
    accounts_by_id: dict[str, Account],
    closed_account_ids: set[str],
    allowed_ids: set[str] | None,
) -> tuple[Decimal, int]:
    """Sum slot.market_value across per_account, applying the canonical
    three-filter set (closed expected_account, tag/account_ids allow-list,
    plan-wrapper). Returns (balance_total, account_count).
    """
    balance_total = Decimal("0")
    account_count = 0
    for acct_id, slot in per_account.items():
        if acct_id in closed_account_ids:
            continue
        if allowed_ids is not None and acct_id not in allowed_ids:
            continue
        acct = accounts_by_id.get(acct_id)
        if acct is None or acct.is_plan_wrapper:
            continue
        balance_total += slot["market_value"]
        account_count += 1
    return balance_total, account_count


@router.get(
    "/brokerage/networth-history-benchmark",
    response_model=BenchmarkComparisonResponse,
)
def networth_history_benchmark(
    benchmark: str = Query("SPY", min_length=1, max_length=8),
    granularity: Literal["daily", "weekly", "monthly"] = Query(
        "weekly",
        description=(
            "Sampling cadence matching /networth-history. "
            "``weekly`` (default) samples every Saturday."
        ),
    ),
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

    Uses the same forward-fill + live re-pricing logic as ``/networth-history``
    (shared ``_load_history_state`` + ``_per_account_value_at`` + same target-
    date generator). The benchmark simulation (shares × close) layers on top of
    the same portfolio_value series.

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

    # Closed expected accounts — excluded from the portfolio series.
    closed_account_ids: set[str] = {
        row[0]
        for row in session.query(ExpectedAccount.resolved_account_id)
        .filter(
            ExpectedAccount.status == "closed",
            ExpectedAccount.resolved_account_id.isnot(None),
        )
        .all()
    }

    start = _earliest_matched_snapshot_date(session)
    if start is None:
        return {
            "benchmark_symbol": bench_symbol,
            "series": [],
            "portfolio_pct": None,
            "benchmark_pct": None,
        }
    today = _today()
    end = today
    if start > end:
        start = end

    # Pre-load once — avoids N+1 across many target dates.
    state = _load_history_state(session)
    accounts_by_id = state["accounts_by_id"]

    target_dates = _generate_target_dates(start, end, granularity)
    if not target_dates or target_dates[-1] != today:
        target_dates.append(today)

    # Single-query fetch of the benchmark's full price history; subsequent
    # per-date lookups walk the in-memory list. Avoids N+1.
    bench_prices = _build_price_lookup(session, bench_symbol)

    def _portfolio_at(target: date) -> Decimal:
        per_account = _per_account_value_at(session, target, history_state=state)
        total, _count = _sum_per_account_filtered(
            per_account,
            accounts_by_id=accounts_by_id,
            closed_account_ids=closed_account_ids,
            allowed_ids=allowed_ids,
        )
        return total

    # Anchor the benchmark simulation on the first target date's portfolio
    # value, computed inline as part of the main loop to avoid a duplicate call.
    initial_portfolio: Decimal | None = None
    shares: Decimal | None = None
    series: list[dict[str, Any]] = []
    benchmark_final: Decimal | None = None
    for d in target_dates:
        port_v = _portfolio_at(d)
        if initial_portfolio is None:
            initial_portfolio = port_v
            anchor_price = _latest_at_or_before(bench_prices, d)
            if anchor_price is not None and anchor_price > 0:
                shares = initial_portfolio / anchor_price
        bench_price = _latest_at_or_before(bench_prices, d)
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

    if not series:
        return {
            "benchmark_symbol": bench_symbol,
            "series": [],
            "portfolio_pct": None,
            "benchmark_pct": None,
        }

    portfolio_final = series[-1]["portfolio_value"]
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


# ── REQ-PERF-010..015: Performance API ─────────────────────────────────


PerfView = Literal["outside_money", "cost_basis"]


class DailyPointOut(BaseModel):
    """One day of the principal/growth decomposition. Decimal-as-string."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    date: date
    market_value: Decimal
    principal: Decimal
    growth: Decimal

    @field_serializer("market_value", "principal", "growth")
    def _ser(self, v: Decimal) -> str:
        return str(v)


class PerformanceSummary(BaseModel):
    """Top-line metrics returned by holding/account/portfolio endpoints."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    twr: Decimal
    twr_annualized: Decimal | None
    xirr: Decimal | None
    benchmark_twr: Decimal | None
    current_value: Decimal
    total_principal: Decimal
    total_growth: Decimal
    # Portfolio-only tracked-coverage fields (None elsewhere)
    tracked_value: Decimal | None = None
    total_value: Decimal | None = None
    tracked_pct: Decimal | None = None
    tracked_begin_date: date | None = None

    @field_serializer(
        "twr",
        "twr_annualized",
        "xirr",
        "benchmark_twr",
        "current_value",
        "total_principal",
        "total_growth",
        "tracked_value",
        "total_value",
        "tracked_pct",
    )
    def _ser(self, v: Decimal | None) -> str | None:
        return str(v) if v is not None else None


class PerformanceResponse(BaseModel):
    """Shape returned by /performance/holding, /account, /portfolio."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    symbol: str | None = None
    account_id: str | None = None
    view: PerfView
    series: list[DailyPointOut]
    summary: PerformanceSummary


class PeriodRow(BaseModel):
    """One row of the periods grid."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    period: str
    twr: Decimal
    mwr: Decimal | None
    spy: Decimal | None
    qqq: Decimal | None

    @field_serializer("twr", "mwr", "spy", "qqq")
    def _ser(self, v: Decimal | None) -> str | None:
        return str(v) if v is not None else None


class PeriodsResponse(BaseModel):
    rows: list[PeriodRow]


class BrokerageTransactionOut(BaseModel):
    """Pydantic projection of a ``BrokerageTransaction`` row."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    account_id: str
    trade_date: date
    action: str
    canonical_action: str
    symbol: str | None = None
    description: str | None = None
    quantity: Decimal | None = None
    amount: Decimal | None = None
    paired_transaction_id: str | None = None
    cash_flow_type: str

    @field_serializer("quantity", "amount")
    def _ser(self, v: Decimal | None) -> str | None:
        return str(v) if v is not None else None

    @classmethod
    def from_orm_row(cls, tx: BrokerageTransaction) -> BrokerageTransactionOut:
        return cls(
            id=tx.id,
            account_id=tx.account_id,
            trade_date=tx.trade_date,
            action=tx.action,
            canonical_action=tx.canonical_action,
            symbol=tx.symbol,
            description=tx.description,
            quantity=Decimal(str(tx.quantity)) if tx.quantity is not None else None,
            amount=Decimal(str(tx.amount)) if tx.amount is not None else None,
            paired_transaction_id=tx.paired_transaction_id,
            cash_flow_type=tx.cash_flow_type,
        )


class PairActionRequest(BaseModel):
    """Body for POST /transactions/{id}/pair."""

    model_config = ConfigDict(extra="forbid")

    paired_transaction_id: str | None = Field(default=None, min_length=1, max_length=64)
    action: Literal["confirm", "reject"]


class PairConfirmResponse(BaseModel):
    tx_a: BrokerageTransactionOut
    tx_b: BrokerageTransactionOut


class PairRejectResponse(BaseModel):
    rejected: bool


class UnpairedCandidateRow(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    tx_a: BrokerageTransactionOut
    tx_b: BrokerageTransactionOut
    confidence: float
    reason: str


class UnpairedTransfersResponse(BaseModel):
    candidates: list[UnpairedCandidateRow]


# ── Helpers ────────────────────────────────────────────────────────────


def _month_period_starts(start: date, end: date) -> list[date]:
    """Return month-aligned period start dates for chain-linked TWR.

    Always includes ``start``. Subsequent entries are the first-of-month after
    ``start`` up through ``end``. ``time_weighted_return`` defines period i as
    ``[starts[i], starts[i+1] - 1]`` so this yields monthly periods.
    """
    starts: list[date] = [start]
    if start.month == 12:
        cursor = date(start.year + 1, 1, 1)
    else:
        cursor = date(start.year, start.month + 1, 1)
    while cursor <= end:
        starts.append(cursor)
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)
    return starts


def _filtered_portfolio_series(
    session: Session,
    account_ids: list[str],
    start: date,
    end: date,
    view: PerfView,
) -> list[Any]:
    """Sum per-account principal/growth series across an account_ids filter.

    Non-overlapping accounts means the sum equals the would-be portfolio
    series restricted to that filter — same MV, same principal accumulation.
    """
    summed: dict[date, dict[str, Decimal]] = {}
    for acct_id in account_ids:
        per_acct = principal_growth_series(
            session, AccountScope(acct_id), start, end, view=view
        )
        for dp in per_acct:
            bucket = summed.setdefault(
                dp.date,
                {
                    "market_value": Decimal("0"),
                    "principal": Decimal("0"),
                    "growth": Decimal("0"),
                },
            )
            bucket["market_value"] += dp.market_value
            bucket["principal"] += dp.principal
            bucket["growth"] += dp.growth

    from src.analytics.performance import DailyPoint  # noqa: PLC0415

    return [
        DailyPoint(
            date=d,
            market_value=v["market_value"],
            principal=v["principal"],
            growth=v["growth"],
        )
        for d, v in sorted(summed.items())
    ]


def _build_brokerage_cash_flows(
    txs: list[BrokerageTransaction],
    scope: Scope,
    start: date,
    end: date,
) -> list[CashFlow]:
    """Build cash flows from external_* txs using the *brokerage* sign.

    Sign convention: positive = inflow to portfolio (deposit), negative =
    outflow (withdrawal). This matches ``BrokerageTransaction.amount`` directly
    and is the convention Modified-Dietz TWR expects in its denominator
    (``v_begin + Σ CF × weight``).
    """
    from src.analytics.classify import ClassifyError, classify  # noqa: PLC0415

    flows: list[CashFlow] = []
    for tx in txs:
        if tx.amount is None:
            continue
        if not (start <= tx.trade_date <= end):
            continue
        try:
            cft = classify(tx, scope)
        except ClassifyError:
            # An unclassifiable row is a data-integrity issue, not a runtime
            # crash; skip it and let the per-row error isolation pattern
            # surface in logs elsewhere.
            continue
        if cft not in (CashFlowType.EXTERNAL_IN, CashFlowType.EXTERNAL_OUT):
            continue
        flows.append(CashFlow(date=tx.trade_date, amount=Decimal(str(tx.amount))))
    return flows


def _build_external_cash_flows(
    txs: list[BrokerageTransaction],
    scope: Scope,
    start: date,
    end: date,
) -> list[CashFlow]:
    """Build XIRR cash flows from external_* txs within [start, end].

    Sign convention (per ``CashFlow`` docstring): negative = investor paid in
    (i.e., money flowed INTO the portfolio — brokerage amount positive →
    XIRR amount negated). Positive = investor received. Internally wraps
    :func:`_build_brokerage_cash_flows` and flips the sign so the two
    helpers stay in lockstep.
    """
    return [
        CashFlow(date=cf.date, amount=-cf.amount)
        for cf in _build_brokerage_cash_flows(txs, scope, start, end)
    ]


def _benchmark_twr(
    session: Session, symbol: str, start: date, end: date
) -> Decimal | None:
    """Return total return for ``symbol`` from start_close to end_close.

    Uses the latest ``HistoricalPrice`` at or before each endpoint. Returns
    ``None`` if either endpoint lacks a price. Annualizes for windows ≥ 30
    days (same convention as ``time_weighted_return``).
    """
    start_row = (
        session.query(HistoricalPrice)
        .filter(
            HistoricalPrice.symbol == symbol,
            HistoricalPrice.trade_date <= start,
        )
        .order_by(HistoricalPrice.trade_date.desc())
        .first()
    )
    end_row = (
        session.query(HistoricalPrice)
        .filter(
            HistoricalPrice.symbol == symbol,
            HistoricalPrice.trade_date <= end,
        )
        .order_by(HistoricalPrice.trade_date.desc())
        .first()
    )
    if start_row is None or end_row is None:
        return None
    if Decimal(str(start_row.close)) == Decimal("0"):
        return None
    raw = (Decimal(str(end_row.close)) - Decimal(str(start_row.close))) / Decimal(
        str(start_row.close)
    )
    window_days = (end - start).days
    if window_days >= 30 and window_days > 0:
        raw_f = float(raw)
        ann = Decimal(str((1.0 + raw_f) ** (365.0 / window_days) - 1.0))
        return ann.quantize(Decimal("0.000001"))
    return raw.quantize(Decimal("0.000001"))


def _scope_summary(
    session: Session,
    scope: Scope,
    txs: list[BrokerageTransaction],
    series: list[Any],
    start: date,
    end: date,
) -> dict[str, Any]:
    """Compute TWR / XIRR / benchmark / current_value / principal / growth.

    ``twr`` is the raw (un-annualized) chain-linked return; ``twr_annualized``
    is the same value scaled to a one-year basis for windows ≥ 30 days, or
    ``None`` for shorter windows (per spec §9.4 — annualizing a sub-30-day
    return is misleading).
    """
    from src.analytics.performance import (  # noqa: PLC0415 - avoid import cycle
        time_weighted_return_breakdown,
    )

    if series:
        current_value = series[-1].market_value
        total_principal = series[-1].principal
        total_growth = series[-1].growth
    else:
        current_value = Decimal("0")
        total_principal = Decimal("0")
        total_growth = Decimal("0")

    period_starts = _month_period_starts(start, end)
    # Modified-Dietz needs portfolio-signed cash flows (positive = inflow).
    brokerage_flows = _build_brokerage_cash_flows(txs, scope, start, end)
    if series:
        twr_result = time_weighted_return_breakdown(series, brokerage_flows, period_starts)
        twr_raw = twr_result.raw
        twr_ann = twr_result.annualized
    else:
        twr_raw = Decimal("0.000000")
        twr_ann = None

    # XIRR uses investor-signed cash flows (deposits negative).
    xirr_flows = _build_external_cash_flows(txs, scope, start, end)
    mwr = (
        money_weighted_return(xirr_flows, current_value, end) if xirr_flows else None
    )

    benchmark = _benchmark_twr(session, "SPY", start, end)

    return {
        "twr": twr_raw,
        "twr_annualized": twr_ann,
        "xirr": mwr,
        "benchmark_twr": benchmark,
        "current_value": current_value,
        "total_principal": total_principal,
        "total_growth": total_growth,
    }


def _series_to_out(series: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "date": dp.date,
            "market_value": dp.market_value,
            "principal": dp.principal,
            "growth": dp.growth,
        }
        for dp in series
    ]


# ── REQ-PERF-010: Per-holding ───────────────────────────────────────────


@router.get(
    "/brokerage/performance/holding/{symbol}",
    response_model=PerformanceResponse,
)
def performance_holding(
    symbol: str = Path(min_length=1, max_length=16, pattern=r"^[A-Za-z0-9.\-]+$"),
    start_date: date | None = Query(None),  # noqa: B008
    end_date: date | None = Query(None),  # noqa: B008
    account_ids: list[str] | None = Query(None, max_length=50),  # noqa: B008
    view: PerfView = Query("outside_money"),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    """REQ-PERF-010: per-holding principal/growth + summary."""
    sym = symbol.upper()
    # Per-element length cap on account_ids: max_length on Query() caps the
    # LIST count; individual string elements have no built-in upper bound.
    # Enforce it manually before any DB filtering touches the values.
    if account_ids and any(len(a) > 64 for a in account_ids):
        raise HTTPException(
            status_code=422, detail="account_id values must be ≤ 64 characters"
        )
    # 404 if no positions for this symbol anywhere
    has_position = (
        session.query(PositionSnapshot)
        .filter(func.upper(PositionSnapshot.symbol) == sym)
        .first()
    )
    if has_position is None:
        raise HTTPException(status_code=404, detail=f"Unknown symbol {sym!r}")

    end = end_date or _today()
    start = start_date or (end - timedelta(days=365))

    # account_ids filter: when present, restrict to PositionScope per account
    # and union; simpler path (and matches spec) is to use a single scope.
    # For now we honour the (single) symbol scope and intersect series with
    # account_ids by filtering the seeded txs/snapshots at scope level.
    if account_ids and len(account_ids) == 1:
        scope: Scope = PositionScope(symbol=sym, account_id=account_ids[0])
    else:
        scope = PositionScope(symbol=sym)

    series = principal_growth_series(session, scope, start, end, view=view)

    # Load txs for scope to compute XIRR. Filter by account_ids if multi.
    from src.analytics.performance import (  # noqa: PLC0415
        _load_transactions_for_scope,
    )

    txs = _load_transactions_for_scope(session, scope)
    if account_ids and len(account_ids) > 1:
        ids = set(account_ids)
        txs = [t for t in txs if t.account_id in ids]

    summary = _scope_summary(session, scope, txs, series, start, end)

    return {
        "symbol": sym,
        "view": view,
        "series": _series_to_out(series),
        "summary": summary,
    }


# ── REQ-PERF-011: Per-account ───────────────────────────────────────────


@router.get(
    "/brokerage/performance/account/{account_id}",
    response_model=PerformanceResponse,
)
def performance_account(
    account_id: str = Path(min_length=1, max_length=64),
    start_date: date | None = Query(None),  # noqa: B008
    end_date: date | None = Query(None),  # noqa: B008
    view: PerfView = Query("outside_money"),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    """REQ-PERF-011: per-account principal/growth + summary."""
    acct = session.query(Account).filter(Account.id == account_id).first()
    if acct is None:
        raise HTTPException(status_code=404, detail=f"Unknown account {account_id!r}")

    end = end_date or _today()
    start = start_date or (end - timedelta(days=365))
    scope: Scope = AccountScope(account_id=account_id)

    series = principal_growth_series(session, scope, start, end, view=view)
    from src.analytics.performance import _load_transactions_for_scope  # noqa: PLC0415

    txs = _load_transactions_for_scope(session, scope)
    summary = _scope_summary(session, scope, txs, series, start, end)

    return {
        "account_id": account_id,
        "view": view,
        "series": _series_to_out(series),
        "summary": summary,
    }


# ── REQ-PERF-012: Portfolio ─────────────────────────────────────────────


@router.get(
    "/brokerage/performance/portfolio",
    response_model=PerformanceResponse,
)
def performance_portfolio(
    start_date: date | None = Query(None),  # noqa: B008
    end_date: date | None = Query(None),  # noqa: B008
    account_ids: list[str] | None = Query(None, max_length=50),  # noqa: B008
    view: PerfView = Query("outside_money"),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    """REQ-PERF-012: portfolio-wide principal/growth + tracked coverage.

    ``account_ids`` filters the underlying transactions and snapshots to the
    given set; tracked-coverage numbers still reflect the entire portfolio so
    the "Tracked $X of $Y" comparison stays meaningful across filter changes.
    """
    if account_ids and any(len(a) > 64 for a in account_ids):
        raise HTTPException(
            status_code=422, detail="account_id values must be ≤ 64 characters"
        )
    end = end_date or _today()
    start = start_date or (end - timedelta(days=365))
    scope: Scope = PortfolioScope()

    if account_ids:
        # Compute the principal/growth series and txs scoped to the filter by
        # iterating per-account and summing. Sum-of-account-series equals
        # portfolio-scope series when accounts are non-overlapping (which they
        # are: an account belongs to exactly one (entity, broker, account#)).
        series = _filtered_portfolio_series(session, account_ids, start, end, view)
        txs: list[BrokerageTransaction] = []
        for acct_id in account_ids:
            from src.analytics.performance import (  # noqa: PLC0415
                load_transactions_for_scope,
            )

            txs.extend(load_transactions_for_scope(session, AccountScope(acct_id)))
    else:
        series = principal_growth_series(session, scope, start, end, view=view)
        from src.analytics.performance import (  # noqa: PLC0415
            load_transactions_for_scope,
        )

        txs = load_transactions_for_scope(session, scope)

    summary = _scope_summary(session, scope, txs, series, start, end)

    coverage = tracked_value_at(session, end)
    summary["tracked_value"] = coverage.tracked_value
    summary["total_value"] = coverage.total_value
    if coverage.total_value > Decimal("0"):
        summary["tracked_pct"] = (
            coverage.tracked_value / coverage.total_value
        ).quantize(Decimal("0.000001"))
    else:
        summary["tracked_pct"] = Decimal("0.000000")
    summary["tracked_begin_date"] = coverage.tracked_begin_date

    return {
        "view": view,
        "series": _series_to_out(series),
        "summary": summary,
    }


# ── REQ-PERF-013: Periods grid ──────────────────────────────────────────


_PERIODS: tuple[tuple[str, int | None], ...] = (
    ("1M", 30),
    ("YTD", None),  # special-cased
    ("1Y", 365),
    ("3Y", 3 * 365),
    ("5Y", 5 * 365),
    ("10Y", 10 * 365),
    ("ITD", None),  # special-cased
)


def _scope_window(
    session: Session, scope: Scope
) -> tuple[date | None, date | None]:
    """Return (earliest_tx_date, latest_tx_date) for ``scope``, or (None, None).

    For PortfolioScope the earliest date is ``tracked_begin_date`` (when
    available) per spec; otherwise we use the earliest classified transaction.
    """
    from src.analytics.performance import _load_transactions_for_scope  # noqa: PLC0415

    txs = _load_transactions_for_scope(session, scope)
    if not txs:
        return (None, None)
    dates = [t.trade_date for t in txs]
    return (min(dates), max(dates))


@router.get("/brokerage/performance/periods", response_model=PeriodsResponse)
def performance_periods(
    scope: Literal["portfolio", "account", "holding"] = Query(...),  # noqa: B008
    id: str | None = Query(None),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    """REQ-PERF-013: standard-period TWR/MWR/SPY/QQQ grid."""
    if scope == "account":
        if not id:
            raise HTTPException(
                status_code=422, detail="`id` (account_id) required for scope=account"
            )
        if session.query(Account).filter(Account.id == id).first() is None:
            raise HTTPException(status_code=404, detail=f"Unknown account {id!r}")
        s_scope: Scope = AccountScope(account_id=id)
    elif scope == "holding":
        if not id:
            raise HTTPException(
                status_code=422, detail="`id` (symbol) required for scope=holding"
            )
        sym = id.upper()
        has_pos = (
            session.query(PositionSnapshot)
            .filter(func.upper(PositionSnapshot.symbol) == sym)
            .first()
        )
        if has_pos is None:
            raise HTTPException(status_code=404, detail=f"Unknown symbol {sym!r}")
        s_scope = PositionScope(symbol=sym)
    else:
        s_scope = PortfolioScope()

    end = _today()
    earliest_data, _latest = _scope_window(session, s_scope)
    # Portfolio override: use tracked_begin_date when populated.
    if isinstance(s_scope, PortfolioScope):
        cov = tracked_value_at(session, end)
        if cov.tracked_begin_date is not None:
            earliest_data = cov.tracked_begin_date

    rows: list[dict[str, Any]] = []
    for label, days in _PERIODS:
        if label == "YTD":
            p_start = date(end.year, 1, 1)
        elif label == "ITD":
            if earliest_data is None:
                continue
            p_start = earliest_data
        else:
            assert days is not None
            p_start = end - timedelta(days=days)

        # Omit periods whose start predates available data
        if earliest_data is not None and p_start < earliest_data and label != "ITD":
            continue

        series = principal_growth_series(session, s_scope, p_start, end)
        if not series:
            continue
        period_starts = _month_period_starts(p_start, end)

        from src.analytics.performance import (  # noqa: PLC0415 - avoid cycle
            load_transactions_for_scope,
            time_weighted_return_breakdown,
        )

        txs = load_transactions_for_scope(session, s_scope)
        brokerage_flows = _build_brokerage_cash_flows(txs, s_scope, p_start, end)
        twr_result = time_weighted_return_breakdown(
            series, brokerage_flows, period_starts
        )
        # Period grid surfaces a single TWR number; for windows >= 30 days
        # the annualized rate is the natural comparison against SPY/QQQ
        # (which are also annualized in _benchmark_twr), otherwise the raw
        # period return is shown.
        twr = twr_result.annualized if twr_result.annualized is not None else twr_result.raw

        xirr_flows = _build_external_cash_flows(txs, s_scope, p_start, end)
        terminal = series[-1].market_value if series else Decimal("0")
        mwr = (
            money_weighted_return(xirr_flows, terminal, end)
            if xirr_flows
            else None
        )

        spy = _benchmark_twr(session, "SPY", p_start, end)
        qqq = _benchmark_twr(session, "QQQ", p_start, end)

        rows.append(
            {
                "period": label,
                "twr": twr,
                "mwr": mwr,
                "spy": spy,
                "qqq": qqq,
            }
        )

    return {"rows": rows}


# ── REQ-PERF-014: Pair confirm/reject ───────────────────────────────────


@router.post(
    "/brokerage/transactions/{tx_id}/pair",
    response_model=PairConfirmResponse | PairRejectResponse,
)
def pair_action(
    tx_id: str = Path(min_length=1, max_length=64),
    body: PairActionRequest = Body(...),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    """REQ-PERF-014: confirm or reject a transfer-pair candidate."""
    tx_a = (
        session.query(BrokerageTransaction)
        .filter(BrokerageTransaction.id == tx_id)
        .first()
    )
    if tx_a is None:
        raise HTTPException(status_code=404, detail=f"Unknown transaction {tx_id!r}")

    if body.paired_transaction_id is None:
        raise HTTPException(
            status_code=422,
            detail="paired_transaction_id is required for both confirm and reject",
        )

    # Self-pair guard (security finding): cannot pair a transaction with itself.
    if tx_id == body.paired_transaction_id:
        raise HTTPException(
            status_code=422,
            detail="A transaction cannot be paired with itself",
        )

    tx_b = (
        session.query(BrokerageTransaction)
        .filter(BrokerageTransaction.id == body.paired_transaction_id)
        .first()
    )
    if tx_b is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown paired transaction {body.paired_transaction_id!r}",
        )

    # Honour the never-delete rule: rejected rows can't be confirmed or rejected.
    if (
        tx_a.status == BrokerageTxStatus.REJECTED.value
        or tx_b.status == BrokerageTxStatus.REJECTED.value
    ):
        raise HTTPException(
            status_code=409,
            detail="cannot pair a rejected transaction",
        )

    if body.action == "reject":
        from scripts.auto_pair_transfers import reject_pair  # noqa: PLC0415

        # changed_by="human:pair_reject" distinguishes this API-originated
        # rejection from a script-originated one (round-2 security finding).
        reject_pair(session, tx_a.id, tx_b.id, changed_by="human:pair_reject")
        return {"rejected": True}

    # ── Confirm-path validation ──────────────────────────────────────
    # Both legs must be transfer-like canonical actions.
    _TRANSFER_LIKE = (
        CanonicalAction.TRANSFER.value,
        CanonicalAction.JOURNAL.value,
        CanonicalAction.EXCHANGE.value,
    )
    if (
        tx_a.canonical_action not in _TRANSFER_LIKE
        or tx_b.canonical_action not in _TRANSFER_LIKE
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                "Both transactions must have canonical_action in "
                f"{_TRANSFER_LIKE} to be paired as a transfer"
            ),
        )
    # Different accounts (an intra-account journal is not a portfolio transfer).
    if tx_a.account_id == tx_b.account_id:
        raise HTTPException(
            status_code=422,
            detail="Both transactions must belong to different accounts",
        )
    # Opposite signs (one outflow, one inflow).
    if tx_a.amount is not None and tx_b.amount is not None:
        amt_a = Decimal(str(tx_a.amount))
        amt_b = Decimal(str(tx_b.amount))
        if amt_a == Decimal("0") or amt_b == Decimal("0") or amt_a * amt_b >= 0:
            raise HTTPException(
                status_code=422,
                detail="Paired transactions must have opposite-sign non-zero amounts",
            )

    # Reject re-pair when either leg is already paired with a different partner —
    # silently abandoning the previous partner leaves a dangling pair.
    if (
        tx_a.paired_transaction_id is not None
        and tx_a.paired_transaction_id != tx_b.id
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                f"tx {tx_a.id!r} is already paired with "
                f"{tx_a.paired_transaction_id!r}; reject that pair first"
            ),
        )
    if (
        tx_b.paired_transaction_id is not None
        and tx_b.paired_transaction_id != tx_a.id
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                f"tx {tx_b.id!r} is already paired with "
                f"{tx_b.paired_transaction_id!r}; reject that pair first"
            ),
        )

    # confirm path — idempotent
    already = (
        tx_a.paired_transaction_id == tx_b.id
        and tx_b.paired_transaction_id == tx_a.id
        and tx_a.cash_flow_type == CashFlowType.INTERNAL.value
        and tx_b.cash_flow_type == CashFlowType.INTERNAL.value
    )
    if already:
        return {
            "tx_a": BrokerageTransactionOut.from_orm_row(tx_a).model_dump(),
            "tx_b": BrokerageTransactionOut.from_orm_row(tx_b).model_dump(),
        }

    old_a_paired = tx_a.paired_transaction_id
    old_b_paired = tx_b.paired_transaction_id
    old_a_cft = tx_a.cash_flow_type
    old_b_cft = tx_b.cash_flow_type

    tx_a.paired_transaction_id = tx_b.id
    tx_b.paired_transaction_id = tx_a.id
    tx_a.cash_flow_type = CashFlowType.INTERNAL.value
    tx_b.cash_flow_type = CashFlowType.INTERNAL.value

    for leg_id, other_id, old_paired, old_cft in (
        (tx_a.id, tx_b.id, old_a_paired, old_a_cft),
        (tx_b.id, tx_a.id, old_b_paired, old_b_cft),
    ):
        session.add(
            AuditEvent(
                entity_type=ENTITY_TYPE_BROKERAGE_TRANSACTION,
                entity_id=leg_id,
                field_changed="paired_transaction_id",
                old_value=old_paired,
                new_value=other_id,
                changed_by="human:pair_confirm",
            )
        )
        # Audit the cash_flow_type flip too — security review found the
        # mutation was previously invisible in the audit trail.
        session.add(
            AuditEvent(
                entity_type=ENTITY_TYPE_BROKERAGE_TRANSACTION,
                entity_id=leg_id,
                field_changed="cash_flow_type",
                old_value=old_cft,
                new_value=CashFlowType.INTERNAL.value,
                changed_by="human:pair_confirm",
            )
        )
    session.commit()

    return {
        "tx_a": BrokerageTransactionOut.from_orm_row(tx_a).model_dump(),
        "tx_b": BrokerageTransactionOut.from_orm_row(tx_b).model_dump(),
    }


# ── REQ-PERF-015: Unpaired transfer candidates ──────────────────────────


def _candidate_reason(
    amount_a: Decimal, amount_b: Decimal, date_a: date, date_b: date
) -> str:
    """Build human-readable explanation for a candidate pair."""
    from scripts.auto_pair_transfers import _business_days_between  # noqa: PLC0415

    diff = abs(abs(amount_a) - abs(amount_b))
    bdays = _business_days_between(date_a, date_b)
    return (
        f"amount match within ${diff} ({amount_a} / {amount_b}), "
        f"{bdays} business day{'' if bdays == 1 else 's'} apart"
    )


@router.get(
    "/brokerage/performance/unpaired-transfers",
    response_model=UnpairedTransfersResponse,
)
def unpaired_transfers(
    session: Session = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    """REQ-PERF-015: list candidate transfer pairs awaiting human review."""
    from scripts.auto_pair_transfers import find_candidates  # noqa: PLC0415

    candidates = find_candidates(session)

    rows: list[dict[str, Any]] = []
    # Hydrate tx rows in batch
    all_ids = {c.tx_a_id for c in candidates} | {c.tx_b_id for c in candidates}
    if not all_ids:
        return {"candidates": []}
    by_id: dict[str, BrokerageTransaction] = {
        tx.id: tx
        for tx in session.query(BrokerageTransaction)
        .filter(BrokerageTransaction.id.in_(list(all_ids)))
        .all()
    }
    for c in candidates:
        tx_a = by_id.get(c.tx_a_id)
        tx_b = by_id.get(c.tx_b_id)
        if tx_a is None or tx_b is None:
            continue
        rows.append(
            {
                "tx_a": BrokerageTransactionOut.from_orm_row(tx_a).model_dump(),
                "tx_b": BrokerageTransactionOut.from_orm_row(tx_b).model_dump(),
                "confidence": c.confidence,
                "reason": _candidate_reason(
                    c.amount_a, c.amount_b, c.date_a, c.date_b
                ),
            }
        )
    return {"candidates": rows}
