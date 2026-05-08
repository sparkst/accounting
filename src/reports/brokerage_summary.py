"""Brokerage summary report (Option 1 visibility — sanity-check CLI).

REQ-005a..g visibility. Reads `data/accounting.db` and prints a structured
report so Travis can eyeball net worth, holdings, recent transactions, and
realized G/L. No API, no UI. Read-only.

See proposals/brokerage-visibility/PLAN-option1.md (v3) for the spec.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, TypedDict

from sqlalchemy import (
    case,
    func,
    or_,
    select,
)
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, aliased, sessionmaker

from src.models.brokerage import (
    Account,
    BrokerageTransaction,
    PositionSnapshot,
    RealizedGainLoss,
)
from src.models.enums import BrokerageTxStatus, CanonicalAction
from src.models.history import AccountBalanceSnapshot

# ── Constants ────────────────────────────────────────────────────────────


# Cash sleeve identification: case-insensitive, exact 'CASH' or LIKE-prefix
# match. Suffixes like '**' on Fidelity tickers (FDRXX**, FCASH**) are matched
# via prefix LIKE. Case is normalised before matching.
CASH_SLEEVE_EXACT: frozenset[str] = frozenset({"CASH"})
CASH_SLEEVE_PREFIXES: tuple[str, ...] = (
    "SPAXX", "FDRXX", "FCASH", "VMFXX", "VMSXX",
    "SWVXX", "SWLXX", "SWTXX", "MMDA1",
)

# Suspect symbol patterns — defensive filter against bad ingest data.
SUSPECT_EXACT: frozenset[str] = frozenset({"TOTAL"})
SUSPECT_PREFIXES: tuple[str, ...] = ("Generated ",)

# IRS long-term threshold: holding period > 365 days.
LONG_TERM_DAYS_THRESHOLD = 365

DEFAULT_STALE_DAYS = 7
DEFAULT_DB_PATH = str(Path(__file__).resolve().parents[2] / "data" / "accounting.db")


# ── TypedDicts for render_report data contract ───────────────────────────


class NetWorthData(TypedDict):
    total: Decimal
    by_broker: dict[str, Decimal]
    by_entity: dict[str, Decimal]
    as_of_min: date | None
    as_of_max: date | None
    zero_snapshot_account_count: int
    plan_wrapper_excluded_count: int


class AccountSummaryRow(TypedDict):
    account_id: str
    broker: str
    account_number_masked: str
    account_name: str | None
    account_type: str
    entity: str
    tax_sheltered: bool
    is_plan_wrapper: bool
    as_of: date | None
    market_value: Decimal


class TopHoldingRow(TypedDict):
    symbol: str | None
    description: str | None
    total_quantity: Decimal
    total_market_value: Decimal
    account_count: int
    is_cash_sleeve: bool
    pct_of_net_worth: Decimal


class RecentTransactionRow(TypedDict):
    trade_date: date
    broker: str
    account_number_masked: str
    action: str
    canonical_action: str
    symbol: str | None
    quantity: Decimal | None
    amount: Decimal | None


class WashSalesData(TypedDict):
    lots: int
    total_disallowed_loss: Decimal


class RealizedGLYear(TypedDict):
    short_term: Decimal
    long_term: Decimal
    unknown: Decimal
    total: Decimal
    lots: int


class RealizedGLSummary(TypedDict):
    by_year: dict[int, RealizedGLYear]
    wash_sales: WashSalesData


class DataIntegrityData(TypedDict):
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


class BrokerageSummaryData(TypedDict):
    net_worth: NetWorthData
    accounts: list[AccountSummaryRow]
    top_holdings: list[TopHoldingRow]
    recent_transactions: list[RecentTransactionRow]
    realized_gl: RealizedGLSummary
    data_integrity: DataIntegrityData


# ── Time helpers (monkey-patchable for tests) ─────────────────────────────


def _today() -> date:
    """Return today's date. Indirection allows tests to pin it."""
    return datetime.now(UTC).date()


# ── Pure helpers ─────────────────────────────────────────────────────────


def _mask_account_number(s: str | None) -> str:
    """Return last-4 mask. Numbers shorter than 4 chars → '****'."""
    if not s or len(s) < 4:
        return "****"
    return f"****{s[-4:]}"


def _format_currency(d: Decimal | None) -> str:
    """Quantize Decimal to 0.01 with ROUND_HALF_UP and format with thousands."""
    if d is None:
        return "$0.00"
    q = d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if q < 0:
        return f"-${(-q):,.2f}"
    return f"${q:,.2f}"


def _is_cash_symbol(symbol: str | None) -> bool:
    if symbol is None:
        return False
    upper = symbol.upper()
    if upper in CASH_SLEEVE_EXACT:
        return True
    return any(upper.startswith(p) for p in CASH_SLEEVE_PREFIXES)


def _is_suspect_symbol(symbol: str | None) -> bool:
    if symbol is None:
        return False
    if symbol.upper() in SUSPECT_EXACT:
        return True
    return any(symbol.startswith(p) for p in SUSPECT_PREFIXES)


# ── Latest-snapshot subquery ─────────────────────────────────────────────


def _latest_position_snapshots(session: Session) -> Any:
    """Return a subquery selecting the id of the latest valid PositionSnapshot
    per (account_id, has_symbol_flag, COALESCE(symbol, description, '__BROKERAGE_SUMMARY_NULL_SENTINEL__')).

    Defensive filters applied:
    - market_value IS NOT NULL
    - symbol NOT IN ('TOTAL') (case-insensitive)
    - symbol NOT LIKE 'Generated %'

    Tie-breaker for same-(account, key, as_of): MIN(id) (deterministic).
    The returned subquery has columns: id (from PositionSnapshot.id).

    NULL sentinel: when both symbol and description are NULL, COALESCE returns
    NULL which makes the join condition UNKNOWN (never matches). We use the
    literal sentinel '__BROKERAGE_SUMMARY_NULL_SENTINEL__' so these rows are still included.

    Collision fix: a row with ``symbol='X'`` and a row with ``symbol=None,
    description='X'`` would have the same COALESCE key. We add a ``has_symbol``
    flag (1 when symbol IS NOT NULL, 0 otherwise) to the partition key so these
    two rows are treated as distinct positions.
    """
    _sentinel = "__BROKERAGE_SUMMARY_NULL_SENTINEL__"

    def _has_symbol_flag(col: Any) -> Any:
        return case((col.isnot(None), 1), else_=0)

    has_sym = _has_symbol_flag(PositionSnapshot.symbol)
    coalesced_key = func.coalesce(
        PositionSnapshot.symbol, PositionSnapshot.description, _sentinel
    )

    # Build the per-(account, has_symbol, key) max as_of subquery.
    max_as_of_q = (
        select(
            PositionSnapshot.account_id.label("account_id"),
            has_sym.label("has_symbol"),
            coalesced_key.label("key"),
            func.max(PositionSnapshot.as_of).label("max_as_of"),
        )
        .where(PositionSnapshot.market_value.isnot(None))
        .where(
            or_(
                PositionSnapshot.symbol.is_(None),
                func.upper(PositionSnapshot.symbol) != "TOTAL",
            )
        )
        .where(
            or_(
                PositionSnapshot.symbol.is_(None),
                ~PositionSnapshot.symbol.like("Generated %"),
            )
        )
        .group_by(PositionSnapshot.account_id, has_sym, coalesced_key)
        .subquery()
    )

    ps2 = aliased(PositionSnapshot)
    has_sym_ps2 = _has_symbol_flag(ps2.symbol)
    coalesced_ps2 = func.coalesce(ps2.symbol, ps2.description, _sentinel)

    # Join PositionSnapshot to (account, has_symbol, key, max_as_of) and pick MIN(id).
    min_id_q = (
        select(func.min(ps2.id).label("id"))
        .join(
            max_as_of_q,
            (ps2.account_id == max_as_of_q.c.account_id)
            & (has_sym_ps2 == max_as_of_q.c.has_symbol)
            & (coalesced_ps2 == max_as_of_q.c.key)
            & (ps2.as_of == max_as_of_q.c.max_as_of),
        )
        .where(ps2.market_value.isnot(None))
        .where(or_(ps2.symbol.is_(None), func.upper(ps2.symbol) != "TOTAL"))
        .where(or_(ps2.symbol.is_(None), ~ps2.symbol.like("Generated %")))
        .group_by(ps2.account_id, has_sym_ps2, coalesced_ps2, ps2.as_of)
        .subquery()
    )

    return min_id_q


def _latest_snapshot_rows(session: Session) -> list[PositionSnapshot]:
    """Convenience: return the actual PositionSnapshot rows referenced by
    `_latest_position_snapshots`. Used by net_worth / account_summary /
    top_holdings."""
    subq = _latest_position_snapshots(session)
    return (
        session.query(PositionSnapshot)
        .join(subq, PositionSnapshot.id == subq.c.id)
        .all()
    )


def _latest_balance_snapshot_per_account(
    session: Session,
) -> dict[str, AccountBalanceSnapshot]:
    """Return ``{account_id: latest AccountBalanceSnapshot}`` for every
    account that has at least one ``AccountBalanceSnapshot`` row with a
    non-null account_id.

    "Latest" = the row with the maximum ``as_of`` for that account; ties
    broken arbitrarily (in practice the per-account/per-date UNIQUE constraint
    avoids ties). Used to surface balance-only brokers (FG, GSK, NW Mutual,
    FT) whose statement-level data lives in ``account_balance_snapshot``
    rather than ``position_snapshot``.
    """
    out: dict[str, AccountBalanceSnapshot] = {}
    rows = (
        session.query(AccountBalanceSnapshot)
        .filter(AccountBalanceSnapshot.account_id.isnot(None))
        .all()
    )
    for row in rows:
        prev = out.get(row.account_id)
        if prev is None or row.as_of > prev.as_of:
            out[row.account_id] = row
    return out


def _per_account_value(session: Session) -> dict[str, dict[str, Any]]:
    """Per-account latest valuation, merged across PositionSnapshot and
    AccountBalanceSnapshot.

    Returns ``{account_id: {market_value, as_of, source}}`` where ``source`` is
    ``'position'`` for PositionSnapshot-derived rows and ``'balance'`` for
    AccountBalanceSnapshot-derived rows. ``market_value`` is the per-account
    sum (PositionSnapshot may have many rows per account; AccountBalanceSnapshot
    is one row per account-date). PositionSnapshot wins when both exist for the
    same account — actively-imported per-position data is more current and
    finer-grained than a statement total.

    Accounts with neither snapshot type are absent from the returned dict.
    """
    out: dict[str, dict[str, Any]] = {}

    for ps in _latest_snapshot_rows(session):
        if ps.market_value is None:
            continue
        slot = out.get(ps.account_id)
        if slot is None:
            slot = {
                "market_value": Decimal("0"),
                "as_of": None,
                "source": "position",
            }
            out[ps.account_id] = slot
        slot["market_value"] += Decimal(ps.market_value)
        if ps.as_of is not None:
            ps_date = ps.as_of.date() if isinstance(ps.as_of, datetime) else ps.as_of
            if slot["as_of"] is None or ps_date > slot["as_of"]:
                slot["as_of"] = ps_date

    for account_id, abs_row in _latest_balance_snapshot_per_account(session).items():
        if account_id in out:
            continue  # PositionSnapshot wins
        out[account_id] = {
            "market_value": Decimal(abs_row.balance),
            "as_of": abs_row.as_of,
            "source": "balance",
        }
    return out


# ── compute_net_worth ────────────────────────────────────────────────────


def compute_net_worth(session: Session) -> dict[str, Any]:
    """Net worth + per-broker / per-entity breakdowns.

    Sources per-account valuations from BOTH ``position_snapshot`` and
    ``account_balance_snapshot`` via :func:`_per_account_value`, so brokers
    that publish only statement-level totals (FG annuity, GSK pension, NW
    Mutual, Franklin Templeton) contribute to the headline.

    Excludes plan-wrapper accounts. Reports zero-snapshot account count.
    """
    per_account = _per_account_value(session)

    # Build account map for broker / entity / wrapper lookup.
    accounts: list[Account] = session.query(Account).all()
    by_id = {a.id: a for a in accounts}

    total = Decimal("0")
    by_broker: dict[str, Decimal] = {}
    by_entity: dict[str, Decimal] = {}
    as_of_dates: set[date] = set()

    for account_id, slot in per_account.items():
        acct = by_id.get(account_id)
        if acct is None or acct.is_plan_wrapper:
            continue
        mv = slot["market_value"]
        total += mv
        by_broker[acct.broker] = by_broker.get(acct.broker, Decimal("0")) + mv
        by_entity[acct.entity] = by_entity.get(acct.entity, Decimal("0")) + mv
        if slot["as_of"] is not None:
            as_of_dates.add(slot["as_of"])

    # An account counts as "zero-snapshot" only if it has NEITHER a
    # PositionSnapshot row NOR an AccountBalanceSnapshot row — checked at
    # the table level (independent of whether the row's value is null) to
    # preserve the historical semantic of this metric.
    snapshot_account_ids = {
        a_id for a_id, in session.query(PositionSnapshot.account_id).distinct()
    } | {
        a_id for a_id, in session.query(AccountBalanceSnapshot.account_id)
        .filter(AccountBalanceSnapshot.account_id.isnot(None))
        .distinct()
    }
    zero_snapshot = sum(
        1 for a in accounts if a.id not in snapshot_account_ids
    )

    plan_wrapper_count = sum(1 for a in accounts if a.is_plan_wrapper)

    return {
        "total": total,
        "by_broker": dict(sorted(by_broker.items())),
        "by_entity": dict(sorted(by_entity.items())),
        "as_of_min": min(as_of_dates) if as_of_dates else None,
        "as_of_max": max(as_of_dates) if as_of_dates else None,
        "zero_snapshot_account_count": zero_snapshot,
        "plan_wrapper_excluded_count": plan_wrapper_count,
    }


# ── get_account_summary ──────────────────────────────────────────────────


def get_account_summary(session: Session) -> list[dict[str, Any]]:
    """One row per account, sorted by market_value desc.

    Per-account valuations are merged across PositionSnapshot AND
    AccountBalanceSnapshot via :func:`_per_account_value`, so balance-only
    accounts (NW Mutual, GSK, FG, FT) appear with non-null ``as_of`` and a
    real ``market_value``.

    Plan-wrapper accounts ARE included with ``is_plan_wrapper=True``; the
    renderer is responsible for visually flagging them.
    """
    accounts: list[Account] = session.query(Account).all()
    per_account = _per_account_value(session)

    rows = []
    for a in accounts:
        slot = per_account.get(a.id)
        rows.append({
            "account_id": a.id,
            "broker": a.broker,
            "account_number_masked": _mask_account_number(a.account_number),
            "account_name": a.account_name,
            "account_type": a.account_type,
            "entity": a.entity,
            "tax_sheltered": a.tax_sheltered,
            "is_plan_wrapper": a.is_plan_wrapper,
            "as_of": slot["as_of"] if slot is not None else None,
            "market_value": slot["market_value"] if slot is not None else Decimal("0"),
        })
    def _sort_key(r: dict[str, Any]) -> tuple[Decimal, str]:
        mv = r["market_value"] if isinstance(r["market_value"], Decimal) else Decimal("0")
        return (-mv, str(r["account_number_masked"]))

    rows.sort(key=_sort_key)
    return rows


# ── get_top_holdings ─────────────────────────────────────────────────────


def get_top_holdings(
    session: Session,
    net_worth_total: Decimal,
    n: int | None = 10,
) -> list[dict[str, Any]]:
    """Top N positions by market value.

    - Cash sleeves folded into one 'Cash' row BEFORE truncation.
    - NULL-symbol non-cash positions kept individually, labeled by description.
    - Excludes plan-wrapper accounts and zero-quantity / zero-market-value rows.
    - `pct_of_net_worth` uses the passed-in denominator.
    """
    snaps = _latest_snapshot_rows(session)
    accounts: list[Account] = session.query(Account).all()
    by_id = {a.id: a for a in accounts}

    # Aggregate per (key, is_cash). Key is the canonical display key:
    # - cash: "Cash"
    # - else symbol if non-NULL else description
    aggregated: dict[tuple[str, bool, str | None], dict[str, Any]] = {}

    for ps in snaps:
        acct = by_id.get(ps.account_id)
        if acct is None or acct.is_plan_wrapper:
            continue
        if ps.market_value is None or ps.market_value <= 0:
            continue
        if ps.quantity is not None and ps.quantity <= 0:
            continue

        is_cash = _is_cash_symbol(ps.symbol)
        if is_cash:
            display_symbol = "Cash"
            description = None
        elif ps.symbol is not None:
            display_symbol = ps.symbol
            description = ps.description
        else:
            display_symbol = None
            description = ps.description

        key = (display_symbol or "", is_cash, description if not is_cash and display_symbol is None else None)
        existing = aggregated.get(key)
        if existing is None:
            aggregated[key] = {
                "symbol": display_symbol,
                "description": description,
                "total_quantity": Decimal(ps.quantity) if ps.quantity is not None else Decimal("0"),
                "total_market_value": Decimal(ps.market_value),
                "account_count": 1,
                "is_cash_sleeve": is_cash,
                "_account_ids": {ps.account_id},
            }
        else:
            existing["total_quantity"] += (
                Decimal(ps.quantity) if ps.quantity is not None else Decimal("0")
            )
            existing["total_market_value"] += Decimal(ps.market_value)
            existing["_account_ids"].add(ps.account_id)
            existing["account_count"] = len(existing["_account_ids"])

    rows: list[dict[str, Any]] = []
    for v in aggregated.values():
        del v["_account_ids"]
        if net_worth_total > 0:
            v["pct_of_net_worth"] = (v["total_market_value"] / net_worth_total).quantize(
                Decimal("0.0001")
            )
        else:
            v["pct_of_net_worth"] = Decimal("0")
        rows.append(v)

    rows.sort(key=lambda r: -r["total_market_value"])
    if n is not None:
        rows = rows[:n]
    return rows


# ── get_recent_transactions ──────────────────────────────────────────────


def get_recent_transactions(session: Session, days: int = 14) -> list[dict[str, Any]]:
    """Recent brokerage transactions, filtered for display.

    Filters: status != REJECTED, is_synthetic=False, suppress reinvest partner
    (paired_transaction_id IS NOT NULL AND canonical_action='reinvest'),
    exclude plan-wrapper accounts.
    """
    cutoff = _today() - timedelta(days=days)

    accounts: list[Account] = session.query(Account).all()
    wrapper_ids = {a.id for a in accounts if a.is_plan_wrapper}
    by_id = {a.id: a for a in accounts}

    txns: list[BrokerageTransaction] = (
        session.query(BrokerageTransaction)
        .filter(BrokerageTransaction.trade_date >= cutoff)
        .filter(BrokerageTransaction.status != BrokerageTxStatus.REJECTED.value)
        .filter(BrokerageTransaction.is_synthetic.is_(False))
        .order_by(BrokerageTransaction.trade_date.desc())
        .all()
    )

    rows = []
    for t in txns:
        if t.account_id in wrapper_ids:
            continue
        if (
            t.canonical_action == CanonicalAction.REINVEST.value
            and t.paired_transaction_id is not None
        ):
            continue
        acct = by_id.get(t.account_id)
        rows.append({
            "trade_date": t.trade_date,
            "broker": acct.broker if acct else "?",
            "account_number_masked": _mask_account_number(
                acct.account_number if acct else None
            ),
            "action": t.action,
            "canonical_action": t.canonical_action,
            "symbol": t.symbol,
            "quantity": t.quantity,
            "amount": t.amount,
        })
    return rows


# ── get_realized_gl_summary ──────────────────────────────────────────────


def get_realized_gl_summary(session: Session) -> dict[str, Any]:
    """Realized G/L by year + wash-sale summary.

    Priority chain for ST/LT bucketing:
    1. lt_gain_loss / st_gain_loss columns when either is non-NULL.
    2. term column ('long' / 'short').
    3. (closed - opened).days > 365 → LT, else ST.
    4. unknown.
    """
    lots: list[RealizedGainLoss] = session.query(RealizedGainLoss).all()
    by_year: dict[int, dict[str, Any]] = {}
    wash_lots = 0
    wash_total_disallowed = Decimal("0")

    for lot in lots:
        year = lot.closed_date.year
        bucket = by_year.setdefault(year, {
            "short_term": Decimal("0"),
            "long_term": Decimal("0"),
            "unknown": Decimal("0"),
            "total": Decimal("0"),
            "lots": 0,
        })

        gain = Decimal(lot.gain_loss) if lot.gain_loss is not None else Decimal("0")

        if lot.lt_gain_loss is not None or lot.st_gain_loss is not None:
            lt = Decimal(lot.lt_gain_loss) if lot.lt_gain_loss is not None else Decimal("0")
            st = Decimal(lot.st_gain_loss) if lot.st_gain_loss is not None else Decimal("0")
            bucket["long_term"] += lt
            bucket["short_term"] += st
        elif lot.term == "long":
            bucket["long_term"] += gain
        elif lot.term == "short":
            bucket["short_term"] += gain
        elif lot.opened_date is not None and lot.closed_date is not None:
            if (lot.closed_date - lot.opened_date).days > LONG_TERM_DAYS_THRESHOLD:
                bucket["long_term"] += gain
            else:
                bucket["short_term"] += gain
        else:
            bucket["unknown"] += gain

        bucket["total"] += gain
        bucket["lots"] += 1

        if lot.wash_sale:
            wash_lots += 1
            if lot.disallowed_loss is not None:
                wash_total_disallowed += Decimal(lot.disallowed_loss)

    # Sort years descending for deterministic output.
    by_year = dict(sorted(by_year.items(), reverse=True))

    return {
        "by_year": by_year,
        "wash_sales": {
            "lots": wash_lots,
            "total_disallowed_loss": wash_total_disallowed,
        },
    }


# ── compute_data_integrity ───────────────────────────────────────────────


def compute_data_integrity(
    session: Session, stale_days: int = DEFAULT_STALE_DAYS
) -> dict[str, Any]:
    """Counts and integrity checks for the Data Integrity footer."""
    today = _today()

    accounts_count = session.query(func.count(Account.id)).scalar() or 0
    txn_count = session.query(func.count(BrokerageTransaction.id)).scalar() or 0
    snap_count = session.query(func.count(PositionSnapshot.id)).scalar() or 0
    rgl_count = session.query(func.count(RealizedGainLoss.id)).scalar() or 0

    valid_account_ids = {a_id for a_id, in session.query(Account.id)}

    orphan_txn = sum(
        1
        for a_id, in session.query(BrokerageTransaction.account_id).distinct()
        if a_id not in valid_account_ids
    )
    orphan_snap = sum(
        1
        for a_id, in session.query(PositionSnapshot.account_id).distinct()
        if a_id not in valid_account_ids
    )

    # Stale: latest as_of < today - stale_days.
    threshold = today - timedelta(days=stale_days)
    snapshot_account_max: dict[str, date] = {}
    for a_id, max_as_of in session.query(
        PositionSnapshot.account_id, func.max(PositionSnapshot.as_of)
    ).group_by(PositionSnapshot.account_id):
        if max_as_of is None:
            continue
        d = max_as_of.date() if isinstance(max_as_of, datetime) else max_as_of
        snapshot_account_max[a_id] = d
    stale = sum(1 for d in snapshot_account_max.values() if d < threshold)

    # Suspect symbols.
    suspect = (
        session.query(func.count(PositionSnapshot.id))
        .filter(
            or_(
                func.upper(PositionSnapshot.symbol) == "TOTAL",
                PositionSnapshot.symbol.like("Generated %"),
            )
        )
        .scalar()
        or 0
    )

    # Duplicate position groups: (account, COALESCE(symbol, description), as_of)
    # appearing more than once.
    coalesced = func.coalesce(PositionSnapshot.symbol, PositionSnapshot.description)
    dup_q = (
        session.query(
            PositionSnapshot.account_id, coalesced, PositionSnapshot.as_of,
            func.count(PositionSnapshot.id).label("c"),
        )
        .group_by(PositionSnapshot.account_id, coalesced, PositionSnapshot.as_of)
        .having(func.count(PositionSnapshot.id) > 1)
        .all()
    )
    duplicate_groups = len(dup_q)

    # Duplicate transaction groups: same (account, trade_date, action, symbol, amount).
    # Indicates a within-file or cross-run dedup failure in the adapter.
    txn_dup_q = (
        session.query(
            BrokerageTransaction.account_id,
            BrokerageTransaction.trade_date,
            BrokerageTransaction.action,
            BrokerageTransaction.symbol,
            BrokerageTransaction.amount,
            func.count(BrokerageTransaction.id).label("c"),
        )
        .group_by(
            BrokerageTransaction.account_id,
            BrokerageTransaction.trade_date,
            BrokerageTransaction.action,
            BrokerageTransaction.symbol,
            BrokerageTransaction.amount,
        )
        .having(func.count(BrokerageTransaction.id) > 1)
        .all()
    )
    duplicate_transaction_groups = len(txn_dup_q)

    return {
        "accounts": accounts_count,
        "transactions": txn_count,
        "position_snapshots": snap_count,
        "realized_lots": rgl_count,
        "orphan_transactions": orphan_txn,
        "orphan_snapshots": orphan_snap,
        "stale_snapshot_accounts": stale,
        "suspect_symbols": suspect,
        "duplicate_position_groups": duplicate_groups,
        "duplicate_transaction_groups": duplicate_transaction_groups,
    }


# ── render_report ────────────────────────────────────────────────────────


def _rule(width: int = 60) -> str:
    return "-" * width


def _hr(title: str, width: int = 60) -> str:
    return f"\n{title}\n{_rule(width)}"


def render_report(data: BrokerageSummaryData | dict[str, Any]) -> str:
    """Render the assembled data dict as a multiline text report."""
    if not data or "net_worth" not in data:
        return "No brokerage data ingested yet"

    out: list[str] = []
    nw = data["net_worth"]
    accounts = data.get("accounts", [])
    top_holdings = data.get("top_holdings", [])
    recent = data.get("recent_transactions", [])
    rgl = data.get("realized_gl", {})
    di = data.get("data_integrity", {})

    # ── Net Worth Summary ──
    out.append(_hr("Net Worth"))
    out.append(f"  Total: {_format_currency(nw['total'])}")
    if nw.get("as_of_min") and nw.get("as_of_max"):
        out.append(f"  Snapshot dates: {nw['as_of_min']} … {nw['as_of_max']}")
    if nw.get("plan_wrapper_excluded_count"):
        out.append(
            f"  ({nw['plan_wrapper_excluded_count']} plan-wrapper account(s) "
            f"excluded — held positions are in their child BrokerageLink accounts)"
        )
    if nw.get("zero_snapshot_account_count"):
        out.append(
            f"  ({nw['zero_snapshot_account_count']} account(s) have no snapshot data)"
        )
    out.append("  By broker:")
    for broker, val in nw["by_broker"].items():
        out.append(f"    {broker:<12} {_format_currency(val):>16}")

    # ── Accounts ──
    out.append(_hr("Accounts"))
    if not accounts:
        out.append("  (no accounts)")
    else:
        out.append(
            "  "
            + f"{'broker':<10} {'acct':<12} {'type':<14} {'entity':<10} "
            f"{'tax':<5} {'as_of':<12} {'market value':>16}"
        )
        with_data = [a for a in accounts if a["as_of"] is not None]
        no_data = [a for a in accounts if a["as_of"] is None]
        for a in with_data:
            wrapper_flag = " [wrapper]" if a["is_plan_wrapper"] else ""
            out.append(
                "  "
                + f"{a['broker']:<10} {a['account_number_masked']:<12} "
                f"{a['account_type']:<14} {a['entity']:<10} "
                f"{('Y' if a['tax_sheltered'] else 'N'):<5} "
                f"{str(a['as_of']):<12} "
                f"{_format_currency(a['market_value']):>16}"
                f"{wrapper_flag}"
            )
        if no_data:
            out.append(_hr("Awaiting Snapshot Data", width=40))
            for a in no_data:
                out.append(
                    f"  {a['broker']:<10} {a['account_number_masked']:<12} "
                    f"{a['account_type']:<14} {a['entity']:<10}"
                )

    # ── Top Holdings ──
    out.append(_hr("Top Holdings"))
    if not top_holdings:
        out.append("  (none)")
    else:
        out.append(
            "  "
            + f"{'symbol/desc':<32} {'qty':>14} {'value':>14} {'pct':>6} {'#acct':>5}"
        )
        for h in top_holdings:
            label = h["symbol"] or h.get("description") or "(unknown)"
            if h.get("is_cash_sleeve"):
                label = "Cash"
            label = label[:31]
            qty = h["total_quantity"]
            val = h["total_market_value"]
            pct = h["pct_of_net_worth"] * Decimal("100")
            out.append(
                "  "
                + f"{label:<32} {qty:>14,.4f} "
                f"{_format_currency(val):>14} {pct:>5.1f}% {h['account_count']:>5}"
            )

    # ── Recent Transactions ──
    out.append(_hr("Recent Transactions"))
    if not recent:
        out.append("  (none in window)")
    else:
        for t in recent:
            qty = t["quantity"] if t["quantity"] is not None else Decimal("0")
            amt = t["amount"] if t["amount"] is not None else Decimal("0")
            out.append(
                f"  {t['trade_date']}  {t['broker']:<10} "
                f"{t['account_number_masked']:<10} "
                f"{t['action']:<22} {(t['symbol'] or ''):<8} "
                f"{qty:>10,.2f}  {_format_currency(amt):>14}"
            )

    # ── Realized G/L ──
    out.append(_hr("Realized G/L by Year"))
    by_year = rgl.get("by_year") or {}
    if not by_year:
        out.append("  (no realized lots)")
    else:
        out.append(
            "  "
            + f"{'year':<6} {'short':>14} {'long':>14} {'unknown':>14} "
            f"{'total':>14} {'lots':>5}"
        )
        for year, b in by_year.items():
            out.append(
                "  "
                + f"{year:<6} "
                f"{_format_currency(b['short_term']):>14} "
                f"{_format_currency(b['long_term']):>14} "
                f"{_format_currency(b['unknown']):>14} "
                f"{_format_currency(b['total']):>14} "
                f"{b['lots']:>5}"
            )

    # ── Wash sales footer ──
    ws = rgl.get("wash_sales", {"lots": 0, "total_disallowed_loss": Decimal("0")})
    out.append(_hr("Wash Sales"))
    if ws["lots"] == 0:
        out.append("  No wash sales in ingested data (1099-B substantiation not yet ingested).")
    else:
        out.append(
            f"  {ws['lots']} lots flagged wash_sale, "
            f"total disallowed_loss: {_format_currency(ws['total_disallowed_loss'])}"
        )

    # ── Data Integrity footer ──
    out.append(_hr("Data Integrity"))
    out.append(
        f"  Accounts: {di.get('accounts', 0)}   "
        f"Transactions: {di.get('transactions', 0)}   "
        f"Snapshots: {di.get('position_snapshots', 0)}   "
        f"Realized lots: {di.get('realized_lots', 0)}"
    )
    if di.get("orphan_transactions") or di.get("orphan_snapshots"):
        out.append(
            f"  ⚠ Orphans: txn={di.get('orphan_transactions', 0)} "
            f"snap={di.get('orphan_snapshots', 0)}"
        )
    if di.get("stale_snapshot_accounts"):
        out.append(
            f"  ⚠ {di['stale_snapshot_accounts']} account(s) have stale snapshot data"
        )
    if di.get("suspect_symbols"):
        out.append(
            f"  ⚠ {di['suspect_symbols']} suspect symbol row(s) detected "
            "(adapter-bug indicator — should be 0)"
        )
    if di.get("duplicate_position_groups"):
        out.append(
            f"  ⚠ {di['duplicate_position_groups']} duplicate position group(s) "
            "(adapter-bug indicator — should be 0)"
        )
    if di.get("duplicate_transaction_groups"):
        out.append(
            f"  ⚠ {di['duplicate_transaction_groups']} duplicate transaction group(s) "
            "(adapter-bug indicator — should be 0)"
        )

    return "\n".join(out)


# ── main / CLI ──────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="brokerage_summary",
        description="One-shot CLI report for the Phase 1 brokerage data.",
    )
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--db", type=str, default=DEFAULT_DB_PATH)
    parser.add_argument("--stale-days", type=int, default=DEFAULT_STALE_DAYS)
    args = parser.parse_args(argv)

    from sqlalchemy import create_engine

    sqlite_url = f"sqlite:///{args.db}"
    try:
        engine = create_engine(sqlite_url)
        SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
        session = SessionLocal()
        try:
            nw = compute_net_worth(session)
            data = {
                "net_worth": nw,
                "accounts": get_account_summary(session),
                "top_holdings": get_top_holdings(
                    session, net_worth_total=nw["total"], n=args.top
                ),
                "recent_transactions": get_recent_transactions(session, days=args.days),
                "realized_gl": get_realized_gl_summary(session),
                "data_integrity": compute_data_integrity(
                    session, stale_days=args.stale_days
                ),
            }
            print(render_report(data))
            return 0
        finally:
            session.close()
    except OperationalError as exc:
        print(f"DB error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"Unexpected error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    # TypedDicts — data contract for render_report
    "BrokerageSummaryData",
    "NetWorthData",
    "AccountSummaryRow",
    "TopHoldingRow",
    "RecentTransactionRow",
    "WashSalesData",
    "RealizedGLYear",
    "RealizedGLSummary",
    "DataIntegrityData",
    # Public functions
    "compute_net_worth",
    "get_account_summary",
    "get_top_holdings",
    "get_recent_transactions",
    "get_realized_gl_summary",
    "compute_data_integrity",
    "render_report",
    "main",
]
