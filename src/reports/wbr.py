"""Weekly Business Review scorecard (REQ-WBR-001..003).

Amazon-WBR-discipline weekly email: per-entity P&L WoW + 6-week trend, AR
aging, Plaid cash positions, review-queue depth, and delivery health — every
number flagged against a threshold, diff-first, cause named for breaches.
Delivered via Resend (``src/reports/report_email.py``), never the n8n
severity webhook.

REQ-WBR-002 tie-out: every P&L number comes from
``src.reports.pl_engine.compute_entity_pl`` — this module never re-derives
entity math (see ``test_wbr.py::TestTieOut``).

Design spec: docs/superpowers/specs/2026-07-07-reporting-suite-design.md §3.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, TypedDict
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.alerts.models import AlertDispatch
from src.balance_alerts.rules import classify as _classify_account_kind
from src.models.audit_event import AuditEvent
from src.models.brokerage import Account
from src.models.enums import Entity, TransactionStatus
from src.models.history import ExpectedAccount
from src.models.invoice import Invoice
from src.models.plaid import PLAID_LIABILITY_TYPES, PlaidAccountBalanceSnapshot, PlaidItem
from src.models.transaction import Transaction
from src.reports.pl_engine import compute_entity_pl, week_window
from src.reports.report_config import load_config, to_decimal
from src.reports.report_email import dispatch_report

__all__ = [
    "WBRData",
    "compute_wbr",
    "render_report",
    "build_subject",
    "main",
]

DEFAULT_DB_PATH = str(Path(__file__).resolve().parents[2] / "data" / "accounting.db")

# ── Coded threshold defaults (design spec §3.3) ─────────────────────────────

WBR_DEFAULT_THRESHOLDS: dict[str, Any] = {
    "net_drop_pct": 30,
    "net_min_abs": 500,
    "exp_rise_pct": 30,
    "exp_min_abs": 500,
    "ar_total_max": 30000,
    "checking_floor": 2500,
    "credit_max": 8000,
    "review_max": 25,
    "plaid_stale_days": 2,
}
WBR_DEFAULT_FRESHNESS: dict[str, Any] = {
    "plaid": 2,
    "gmail": 3,
    "stripe": 3,
    "bank_csv": 35,
}

_ENTITY_ORDER = (Entity.SPARKRY.value, Entity.BLACKLINE.value, Entity.PERSONAL.value)
_ENTITY_LABEL = {
    Entity.SPARKRY.value: "Sparkry",
    Entity.BLACKLINE.value: "BlackLine",
    Entity.PERSONAL.value: "Personal",
}

_SPARK_CHARS = "▁▂▃▄▅▆▇█"

_FRESHNESS_SOURCE_LABEL = {
    "gmail_n8n": "gmail",
    "stripe": "stripe",
    "plaid": "plaid-txn",
    "bank_csv": "bank-csv",
}


def _today() -> date:
    """America/Los_Angeles calendar date (design spec §3.4) — overridden in
    tests for pinning."""
    return datetime.now(ZoneInfo("America/Los_Angeles")).date()


# ── TypedDicts (public data contract for render_report) ─────────────────────


class EntityWoWRow(TypedDict):
    entity: str
    label: str
    revenue_this: Decimal
    revenue_last: Decimal
    expenses_this: Decimal
    expenses_last: Decimal
    net_this: Decimal
    net_last: Decimal
    trend6: list[Decimal]
    revenue_warn: str | None
    expenses_warn: str | None
    net_warn: str | None


class HouseholdRow(TypedDict):
    net_this: Decimal
    net_last: Decimal
    trend6: list[Decimal]
    net_warn: str | None


class ARAgingData(TypedDict):
    current: Decimal
    d15_30: Decimal
    d31_45: Decimal
    d45_plus: Decimal
    total: Decimal
    warn: str | None


class CashLine(TypedDict):
    label: str
    balance: Decimal
    as_of: str | None
    kind: str
    is_liability: bool
    warn: str | None


class OpsData(TypedDict):
    review_queue: int
    review_queue_warn: str | None
    auto_confirmed: int


class DeliveryHealthData(TypedDict):
    plaid_ok: int
    plaid_total: int
    max_age_days: int
    plaid_warn: str | None
    alerts_sent_7d: int
    alerts_failed_7d: int
    alerts_warn: str | None
    unmapped_count: int
    unmapped_warn: str | None
    snapshot_gap_days: int
    snapshot_gap_warn: str | None


class FreshnessRow(TypedDict):
    label: str
    as_of: str | None
    stale: bool


class WBRData(TypedDict):
    week_start: str
    week_end: str
    run_at: str
    entities: list[EntityWoWRow]
    household: HouseholdRow
    ar_aging: ARAgingData
    cash: list[CashLine]
    ops: OpsData
    delivery: DeliveryHealthData
    freshness: list[FreshnessRow]
    plaid_balances_as_of: str | None
    used_config_defaults: bool
    warnings_summary: list[str]


# ── Formatting helpers ───────────────────────────────────────────────────────


def _q0(d: Decimal) -> Decimal:
    return d.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def _fmt0(d: Decimal) -> str:
    return f"${_q0(d):,.0f}"


def _fmt0_paren(d: Decimal) -> str:
    return f"(${_q0(abs(d)):,.0f})"


def _fmt0_signed(d: Decimal) -> str:
    return _fmt0_paren(d) if d < 0 else _fmt0(d)


def _fmt0_delta(impact_delta: Decimal) -> str:
    if impact_delta > 0:
        return f"+${_q0(impact_delta):,.0f}"
    if impact_delta < 0:
        return _fmt0_paren(impact_delta)
    return "$0"


def _fmt_pct(pct: Decimal | None) -> str:
    if pct is None:
        return "n/a"
    q = pct.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    sign = "+" if q >= 0 else ""
    return f"{sign}{q}%"


def _pct_change(this: Decimal, last: Decimal) -> Decimal | None:
    if last == 0:
        if this == 0:
            return Decimal("0")
        return None
    return (this - last) / abs(last) * Decimal("100")


def sparkline(values: list[Decimal]) -> str:
    """6 weekly values, min-max scaled to the 8-level block character ramp."""
    if not values:
        return ""
    lo, hi = min(values), max(values)
    if hi == lo:
        return _SPARK_CHARS[len(_SPARK_CHARS) // 2] * len(values)
    span = hi - lo
    out = []
    for v in values:
        idx = int((v - lo) / span * (len(_SPARK_CHARS) - 1))
        idx = max(0, min(len(_SPARK_CHARS) - 1, idx))
        out.append(_SPARK_CHARS[idx])
    return "".join(out)


def _iso_week_label(d: date) -> str:
    iso_year, iso_week, _ = d.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


# ── Compute: P&L WoW + trend ─────────────────────────────────────────────────


def _six_week_windows(this_monday: str) -> list[tuple[str, str]]:
    """6 consecutive [Mon, Mon) windows ending at this_monday, oldest first."""
    end = date.fromisoformat(this_monday)
    windows: list[tuple[str, str]] = []
    for _ in range(6):
        start = end - timedelta(days=7)
        windows.append((start.isoformat(), end.isoformat()))
        end = start
    windows.reverse()
    return windows


def _largest_expense_cause(
    session: Session, entity: str | None, start: str, end: str
) -> str | None:
    """REQ-WBR §3.1: name the largest contributing expense txn for a breach."""
    query = session.query(Transaction).filter(
        Transaction.date >= start,
        Transaction.date < end,
        Transaction.direction == "expense",
        Transaction.status.notin_(
            (TransactionStatus.REJECTED.value, TransactionStatus.SPLIT_PARENT.value)
        ),
    )
    if entity is not None:
        query = query.filter(Transaction.entity == entity)
    rows = query.all()
    if not rows:
        return None
    biggest = max(rows, key=lambda tx: abs(Decimal(str(tx.amount or 0))))
    amt = abs(Decimal(str(biggest.amount or 0)))
    desc = biggest.description or "(no description)"
    short_id = (biggest.id or "")[:4]
    return f"{desc} {_fmt0(amt)} (txn {short_id}…)"


def _compute_entity_row(
    session: Session,
    entity: str | None,
    label: str,
    this_window: tuple[str, str],
    last_window: tuple[str, str],
    trend_windows: list[tuple[str, str]],
    thresholds: dict[str, Decimal],
) -> tuple[EntityWoWRow, list[str]]:
    this_pl = compute_entity_pl(session, this_window[0], this_window[1], entity=entity)
    last_pl = compute_entity_pl(session, last_window[0], last_window[1], entity=entity)
    trend = [
        compute_entity_pl(session, w[0], w[1], entity=entity).net for w in trend_windows
    ]

    warnings: list[str] = []

    revenue_warn: str | None = None
    if entity == Entity.SPARKRY.value and this_pl.revenue == 0:
        revenue_warn = "$0 revenue this week"
        warnings.append(f"{label} revenue $0 this week")

    expenses_warn: str | None = None
    exp_delta = this_pl.expenses - last_pl.expenses
    exp_pct = _pct_change(this_pl.expenses, last_pl.expenses)
    if (
        exp_pct is not None
        and exp_pct > thresholds["exp_rise_pct"]
        and exp_delta > thresholds["exp_min_abs"]
    ):
        cause = _largest_expense_cause(session, entity, *this_window)
        expenses_warn = f">+{thresholds['exp_rise_pct']}%"
        line = f"{label} expenses {_fmt_pct(exp_pct)} WoW (threshold +{thresholds['exp_rise_pct']}%)"
        if cause:
            line += f": {cause}"
        warnings.append(line)

    net_warn: str | None = None
    net_delta = this_pl.net - last_pl.net
    net_pct = _pct_change(this_pl.net, last_pl.net)
    if (
        net_pct is not None
        and net_pct < -thresholds["net_drop_pct"]
        and -net_delta > thresholds["net_min_abs"]
    ):
        net_warn = f"<-{thresholds['net_drop_pct']}%"
        warnings.append(f"{label} net {_fmt_pct(net_pct)} WoW (threshold -{thresholds['net_drop_pct']}%)")

    row: EntityWoWRow = {
        "entity": entity or "household",
        "label": label,
        "revenue_this": this_pl.revenue,
        "revenue_last": last_pl.revenue,
        "expenses_this": this_pl.expenses,
        "expenses_last": last_pl.expenses,
        "net_this": this_pl.net,
        "net_last": last_pl.net,
        "trend6": trend,
        "revenue_warn": revenue_warn,
        "expenses_warn": expenses_warn,
        "net_warn": net_warn,
    }
    return row, warnings


# ── Compute: AR aging ─────────────────────────────────────────────────────


def _invoice_age_days(invoice: Invoice, today: date) -> int | None:
    ref: str | None = None
    if invoice.sent_at is not None:
        ref = invoice.sent_at.date().isoformat() if hasattr(invoice.sent_at, "date") else str(invoice.sent_at)[:10]
    elif invoice.submitted_date:
        ref = invoice.submitted_date
    if not ref:
        return None
    try:
        ref_date = date.fromisoformat(ref[:10])
    except ValueError:
        return None
    return (today - ref_date).days


def _compute_ar_aging(
    session: Session, today: date, thresholds: dict[str, Decimal]
) -> tuple[ARAgingData, list[str]]:
    rows = (
        session.query(Invoice)
        .filter(Invoice.status.in_(("sent", "overdue")))
        .all()
    )
    buckets = {"current": Decimal("0"), "d15_30": Decimal("0"), "d31_45": Decimal("0"), "d45_plus": Decimal("0")}
    for inv in rows:
        age = _invoice_age_days(inv, today)
        if age is None:
            age = 0
        amt = Decimal(str(inv.total)) if inv.total is not None else Decimal("0")
        if age <= 14:
            buckets["current"] += amt
        elif age <= 30:
            buckets["d15_30"] += amt
        elif age <= 45:
            buckets["d31_45"] += amt
        else:
            buckets["d45_plus"] += amt
    total = sum(buckets.values(), Decimal("0"))

    warnings: list[str] = []
    warn: str | None = None
    if buckets["d31_45"] > 0 or buckets["d45_plus"] > 0:
        warn = "31d+ balance outstanding"
        warnings.append(
            f"AR aging: {_fmt0(buckets['d31_45'])} in 31-45d, {_fmt0(buckets['d45_plus'])} in 45d+"
        )
    if total > thresholds["ar_total_max"]:
        warn = warn or ""
        warn = (warn + " ; " if warn else "") + f">{_fmt0(thresholds['ar_total_max'])} ceiling"
        warnings.append(f"AR total {_fmt0(total)} exceeds {_fmt0(thresholds['ar_total_max'])} ceiling")

    data: ARAgingData = {
        "current": buckets["current"],
        "d15_30": buckets["d15_30"],
        "d31_45": buckets["d31_45"],
        "d45_plus": buckets["d45_plus"],
        "total": total,
        "warn": warn,
    }
    return data, warnings


# ── Compute: cash + delivery health ──────────────────────────────────────


def _mask_account(account_number: str | None) -> str:
    if not account_number:
        return "…????"
    tail = account_number[-4:]
    return f"…{tail}"


def _compute_cash_and_delivery(
    session: Session, today: date, thresholds: dict[str, Decimal]
) -> tuple[list[CashLine], DeliveryHealthData, list[str], str | None]:
    account_ids = session.scalars(select(PlaidAccountBalanceSnapshot.account_id).distinct()).all()

    cash: list[CashLine] = []
    warnings: list[str] = []
    max_gap_days = 0
    latest_balance_dates: list[date] = []

    for account_id in account_ids:
        latest = session.scalars(
            select(PlaidAccountBalanceSnapshot)
            .where(
                PlaidAccountBalanceSnapshot.account_id == account_id,
                PlaidAccountBalanceSnapshot.snapshot_date <= today,
            )
            .order_by(PlaidAccountBalanceSnapshot.snapshot_date.desc())
            .limit(1)
        ).first()
        if latest is None:
            continue
        account = session.get(Account, account_id)
        entity_label = _ENTITY_LABEL.get(account.entity, "") if account else ""
        name = (account.account_name if account and account.account_name else account_id) or account_id
        # Avoid "BlackLine BlackLine Checking" when the account_name already
        # carries the entity name.
        if entity_label and not name.lower().startswith(entity_label.lower()):
            label = f"{entity_label} {name}"
        else:
            label = name
        label = f"{label} {_mask_account(account.account_number if account else None)}"

        kind = _classify_account_kind(latest.plaid_account_type, latest.plaid_account_subtype)
        is_liability = (latest.plaid_account_type or "").strip().lower() in PLAID_LIABILITY_TYPES
        balance = Decimal(str(latest.current_balance))
        signed_balance = -balance if is_liability else balance
        if is_liability:
            label += " (liability)"

        gap_days = (today - latest.snapshot_date).days
        max_gap_days = max(max_gap_days, gap_days)
        latest_balance_dates.append(latest.snapshot_date)

        warn: str | None = None
        if kind == "checking" and signed_balance < thresholds["checking_floor"]:
            warn = f"< {_fmt0(thresholds['checking_floor'])} floor"
            warnings.append(f"{label.strip()} {_fmt0(signed_balance)} < {_fmt0(thresholds['checking_floor'])} floor")
        if is_liability and balance > thresholds["credit_max"]:
            warn = f"> {_fmt0(thresholds['credit_max'])} ceiling"
            warnings.append(f"{label.strip()} owed {_fmt0(balance)} > {_fmt0(thresholds['credit_max'])} ceiling")

        cash.append(
            {
                "label": label.strip(),
                "balance": signed_balance,
                "as_of": latest.snapshot_date.isoformat(),
                "kind": kind or "other",
                "is_liability": is_liability,
                "warn": warn,
            }
        )

    cash.sort(key=lambda c: c["label"])

    # Plaid item sync health.
    items = session.query(PlaidItem).filter(PlaidItem.status == "active").all()
    plaid_total = len(items)
    plaid_ok = 0
    item_ages: list[int] = []
    for item in items:
        if item.last_sync_at is None:
            item_ages.append(9999)
            continue
        age = (today - item.last_sync_at.date()).days
        item_ages.append(age)
        if age <= thresholds["plaid_stale_days"]:
            plaid_ok += 1
    max_age_days = max(item_ages) if item_ages else 0
    plaid_warn = None
    if plaid_ok < plaid_total:
        plaid_warn = f"{plaid_total - plaid_ok} stale"
        warnings.append(f"{plaid_total - plaid_ok} of {plaid_total} Plaid item(s) stale (max age {max_age_days}d)")

    # 7-day alert-webhook sent/failed (n8n-routed alerts only — reports
    # themselves are delivery_channel='resend_email' and excluded).
    cutoff = (today - timedelta(days=7)).isoformat()
    alert_rows = (
        session.query(AlertDispatch)
        .filter(
            AlertDispatch.delivery_channel == "n8n_webhook",
            AlertDispatch.occurrence_date >= cutoff,
        )
        .all()
    )
    alerts_sent = sum(1 for r in alert_rows if r.status == "sent")
    alerts_failed = sum(1 for r in alert_rows if r.status == "failed")
    alerts_warn = None
    if alerts_failed > 0:
        alerts_warn = f"{alerts_failed} failed"
        warnings.append(f"{alerts_failed} alert(s) failed to deliver in the last 7 days")

    unmapped_rows = (
        session.query(ExpectedAccount).filter_by(source="plaid", status="unconfirmed").all()
    )
    unmapped_count = len(unmapped_rows)
    unmapped_warn = None
    if unmapped_count > 0:
        unmapped_warn = f"{unmapped_count} unmapped"
        warnings.append(f"{unmapped_count} unmapped Plaid account(s)")

    snapshot_gap_warn = None
    if max_gap_days > thresholds["plaid_stale_days"]:
        snapshot_gap_warn = f"{max_gap_days}d gap"
        warnings.append(f"Plaid snapshot gap of {max_gap_days} day(s)")

    delivery: DeliveryHealthData = {
        "plaid_ok": plaid_ok,
        "plaid_total": plaid_total,
        "max_age_days": max_age_days,
        "plaid_warn": plaid_warn,
        "alerts_sent_7d": alerts_sent,
        "alerts_failed_7d": alerts_failed,
        "alerts_warn": alerts_warn,
        "unmapped_count": unmapped_count,
        "unmapped_warn": unmapped_warn,
        "snapshot_gap_days": max_gap_days,
        "snapshot_gap_warn": snapshot_gap_warn,
    }
    plaid_balances_as_of = max(latest_balance_dates).isoformat() if latest_balance_dates else None
    return cash, delivery, warnings, plaid_balances_as_of


# ── Compute: ops + freshness ─────────────────────────────────────────────


def _compute_ops(
    session: Session, week_start: str, week_end: str, thresholds: dict[str, Decimal]
) -> tuple[OpsData, list[str]]:
    review_queue = (
        session.query(Transaction)
        .filter(Transaction.status == TransactionStatus.NEEDS_REVIEW.value)
        .count()
    )
    start_dt = datetime.fromisoformat(week_start)
    end_dt = datetime.fromisoformat(week_end)
    auto_confirmed = (
        session.query(AuditEvent)
        .filter(
            AuditEvent.changed_by.like("auto%"),
            AuditEvent.changed_at >= start_dt,
            AuditEvent.changed_at < end_dt,
        )
        .count()
    )
    warnings: list[str] = []
    warn: str | None = None
    if review_queue > thresholds["review_max"]:
        warn = f"> {int(thresholds['review_max'])}"
        warnings.append(f"Review queue depth {review_queue} exceeds {int(thresholds['review_max'])}")
    return {
        "review_queue": review_queue,
        "review_queue_warn": warn,
        "auto_confirmed": auto_confirmed,
    }, warnings


def _compute_freshness(
    session: Session, freshness_cfg: dict[str, int], today: date
) -> list[FreshnessRow]:
    rows: list[FreshnessRow] = []
    for source, label in _FRESHNESS_SOURCE_LABEL.items():
        max_date = session.query(Transaction.date).filter(Transaction.source == source).order_by(
            Transaction.date.desc()
        ).limit(1).scalar()
        stale = False
        if max_date:
            days_old = (today - date.fromisoformat(max_date)).days
            cadence = freshness_cfg.get(label.replace("-txn", "").replace("-", "_"), freshness_cfg.get(source, 9999))
            stale = days_old > cadence
        rows.append({"label": label, "as_of": max_date, "stale": stale})
    return rows


# ── Top-level compute + render ───────────────────────────────────────────


def compute_wbr(
    session: Session, today: date | None = None, *, config_dir: Path | None = None
) -> WBRData:
    today = today or _today()
    week_start, this_monday = week_window(today)
    last_window = week_window(date.fromisoformat(week_start))
    trend_windows = _six_week_windows(this_monday)

    cfg = load_config(
        "reporting.yaml",
        {"thresholds": WBR_DEFAULT_THRESHOLDS, "freshness": WBR_DEFAULT_FRESHNESS},
        config_dir=config_dir,
    )
    thresholds = {k: to_decimal(v) for k, v in cfg["thresholds"].items()}
    freshness_cfg = {k: int(v) for k, v in cfg.get("freshness", WBR_DEFAULT_FRESHNESS).items()}

    all_warnings: list[str] = []

    entities: list[EntityWoWRow] = []
    for entity in _ENTITY_ORDER:
        row, warns = _compute_entity_row(
            session,
            entity,
            _ENTITY_LABEL[entity],
            (week_start, this_monday),
            last_window,
            trend_windows,
            thresholds,
        )
        entities.append(row)
        all_warnings.extend(warns)

    hh_row, hh_warns = _compute_entity_row(
        session, None, "HOUSEHOLD", (week_start, this_monday), last_window, trend_windows, thresholds
    )
    all_warnings.extend(hh_warns)
    household: HouseholdRow = {
        "net_this": hh_row["net_this"],
        "net_last": hh_row["net_last"],
        "trend6": hh_row["trend6"],
        "net_warn": hh_row["net_warn"],
    }

    ar_aging, ar_warns = _compute_ar_aging(session, today, thresholds)
    all_warnings.extend(ar_warns)

    cash, delivery, cash_warns, plaid_balances_as_of = _compute_cash_and_delivery(session, today, thresholds)
    all_warnings.extend(cash_warns)

    ops, ops_warns = _compute_ops(session, week_start, this_monday, thresholds)
    all_warnings.extend(ops_warns)

    freshness = _compute_freshness(session, freshness_cfg, today)
    for fresh_row in freshness:
        if fresh_row["stale"]:
            all_warnings.append(f"{fresh_row['label']} data stale (as of {fresh_row['as_of']})")

    return {
        "week_start": week_start,
        "week_end": this_monday,
        # Deterministic scheduled-run label (design spec §3.4: Mon 06:00 PT) —
        # derived from the pinned `today`, never wall-clock now(), so two runs
        # over the same DB/date produce byte-identical output (design spec §2).
        "run_at": f"{today.isoformat()} 06:00 PT",
        "entities": entities,
        "household": household,
        "ar_aging": ar_aging,
        "cash": cash,
        "ops": ops,
        "delivery": delivery,
        "freshness": freshness,
        "plaid_balances_as_of": plaid_balances_as_of,
        "used_config_defaults": cfg.used_defaults,
        "warnings_summary": all_warnings,
    }


def build_subject(data: WBRData) -> str:
    week_label = _iso_week_label(date.fromisoformat(data["week_start"]))
    net = data["household"]["net_this"]
    last = data["household"]["net_last"]
    pct = _pct_change(net, last)
    arrow = "▲" if net >= last else "▼"
    n_warn = len(data["warnings_summary"])
    return f"[WBR] {week_label} · HH net {_fmt0_signed(net)} ({arrow}{_fmt_pct(pct).lstrip('+')}) · {n_warn}⚠️"


def _row_mark(warn: str | None) -> str:
    return f"⚠️ {warn}" if warn else "✅"


def render_report(data: WBRData) -> str:
    out: list[str] = []
    out.append(
        f"WBR — week {data['week_start']} → {data['week_end']} (Mon–Mon, half-open)"
        f"          run {data['run_at']}"
    )
    out.append("")
    out.append(f"{'P&L (WoW)':<30} {'this wk':<12} {'last wk':<12} {'Δ$':<10} {'Δ%':<8} 6wk trend")
    for row in data["entities"]:
        rev_delta = row["revenue_this"] - row["revenue_last"]
        rev_pct = _pct_change(row["revenue_this"], row["revenue_last"])
        exp_delta = -(row["expenses_this"]) - -(row["expenses_last"])
        exp_pct = _pct_change(row["expenses_this"], row["expenses_last"])
        net_delta = row["net_this"] - row["net_last"]
        net_pct = _pct_change(row["net_this"], row["net_last"])
        spark = sparkline(row["trend6"])
        out.append(
            f"{row['label']:<10} {'revenue':<19} {_fmt0(row['revenue_this']):<12} "
            f"{_fmt0(row['revenue_last']):<12} {_fmt0_delta(rev_delta):<10} {_fmt_pct(rev_pct):<8} "
            f"{spark}  {_row_mark(row['revenue_warn'])}"
        )
        out.append(
            f"{'':<10} {'expenses':<19} {_fmt0_paren(row['expenses_this']):<12} "
            f"{_fmt0_paren(row['expenses_last']):<12} {_fmt0_delta(exp_delta):<10} {_fmt_pct(exp_pct):<8} "
            f"{spark}  {_row_mark(row['expenses_warn'])}"
        )
        out.append(
            f"{'':<10} {'net':<19} {_fmt0_signed(row['net_this']):<12} "
            f"{_fmt0_signed(row['net_last']):<12} {_fmt0_delta(net_delta):<10} {_fmt_pct(net_pct):<8} "
            f"{spark}  {_row_mark(row['net_warn'])}"
        )
    hh = data["household"]
    hh_delta = hh["net_this"] - hh["net_last"]
    hh_pct = _pct_change(hh["net_this"], hh["net_last"])
    out.append(
        f"{'HOUSEHOLD':<10} {'net':<19} {_fmt0_signed(hh['net_this']):<12} "
        f"{_fmt0_signed(hh['net_last']):<12} {_fmt0_delta(hh_delta):<10} {_fmt_pct(hh_pct):<8} "
        f"{sparkline(hh['trend6'])}  {_row_mark(hh['net_warn'])}"
    )

    out.append("")
    ar = data["ar_aging"]
    out.append(f"{'AR AGING':<20} {'current':<10} {'15–30d':<10} {'31–45d':<10} {'45d+':<10} total")
    out.append(
        f"{'':<20} {_fmt0(ar['current']):<10} {_fmt0(ar['d15_30']):<10} "
        f"{_fmt0(ar['d31_45']):<10} {_fmt0(ar['d45_plus']):<10} {_fmt0(ar['total']):<10} {_row_mark(ar['warn'])}"
    )

    out.append("")
    out.append("CASH (Plaid, latest snapshot per account)")
    if not data["cash"]:
        out.append("  (no linked accounts)")
    else:
        for c in data["cash"]:
            out.append(
                f"{c['label']:<30} {_fmt0_signed(c['balance']):<10} "
                f"(as of {c['as_of']})  {_row_mark(c['warn'])}"
            )

    out.append("")
    ops = data["ops"]
    out.append(
        f"{'OPS':<20} review queue: {ops['review_queue']} {_row_mark(ops['review_queue_warn'])}   "
        f"auto-confirmed this wk: {ops['auto_confirmed']} ✅"
    )
    dl = data["delivery"]
    out.append(
        f"{'DELIVERY HEALTH':<20} plaid items: {dl['plaid_ok']}/{dl['plaid_total']} ok "
        f"(max age {dl['max_age_days']}d) {_row_mark(dl['plaid_warn'])} · "
        f"alerts 7d: {dl['alerts_sent_7d']} sent / {dl['alerts_failed_7d']} failed {_row_mark(dl['alerts_warn'])}"
    )
    out.append(
        f"{'':<20} unmapped plaid accounts: {dl['unmapped_count']} {_row_mark(dl['unmapped_warn'])} · "
        f"snapshot gap days: {dl['snapshot_gap_days']} {_row_mark(dl['snapshot_gap_warn'])}"
    )

    out.append("")
    if data["warnings_summary"]:
        out.append("⚠️ SUMMARY (act on these)")
        for i, w in enumerate(data["warnings_summary"], start=1):
            out.append(f"{i}. {w}")
    else:
        out.append("⚠️ SUMMARY — none. All metrics within threshold.")

    out.append("")
    fresh_parts = []
    for fresh_row in data["freshness"]:
        marker = " ⚠️stale" if fresh_row["stale"] else ""
        label = fresh_row["label"]
        as_of = fresh_row["as_of"][5:] if fresh_row["as_of"] else "never"
        fresh_parts.append(f"{label} {as_of}{marker}")
    out.append("Data as-of — register: " + " · ".join(fresh_parts))
    pb = data["plaid_balances_as_of"]
    thresholds_marker = "defaults" if data["used_config_defaults"] else "config v1"
    out.append(
        f"plaid balances: {pb[5:] if pb else 'never'} · invoices: live · alert ledger: live"
        f"        thresholds: {thresholds_marker}"
    )

    return "\n".join(out)


# ── CLI ───────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wbr_dispatch", description="Weekly Business Review scorecard (REQ-WBR-001..003)."
    )
    parser.add_argument("--apply", action="store_true", help="Send via Resend + record in the ledger (default: DRY-RUN).")
    parser.add_argument("--date", type=date.fromisoformat, default=None, help="Override today's date (YYYY-MM-DD).")
    parser.add_argument("--db", type=str, default=DEFAULT_DB_PATH)
    args = parser.parse_args(argv)

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    today = args.date or _today()
    engine = create_engine(f"sqlite:///{args.db}")
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    session = SessionLocal()
    try:
        data = compute_wbr(session, today)
        body = render_report(data)
        subject = build_subject(data)
        if not args.apply:
            print(body)
            return 0
        week_label = _iso_week_label(date.fromisoformat(data["week_start"]))
        result = dispatch_report(
            session,
            alert_key=f"wbr:{week_label}",
            occurrence_date=data["week_start"],
            alert_type="wbr_weekly",
            entity="all",
            subject=subject,
            body=body,
            apply=True,
        )
        print(f"[{result.status}] {subject}")
        return 1 if result.status == "failed" else 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
