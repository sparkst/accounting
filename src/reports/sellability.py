"""Sellability metrics (REQ-SEL-001..002) — Sparkry SDE, client concentration,
recurring/project split, BlackLine burn.

SDE = Sparkry net income (``pl_engine.compute_entity_pl``, reimbursables
netted) + itemized add-backs from ``config/sellability.yaml`` — every
add-back line is printed, nothing implicit. Client attribution: paid
invoices first (customer via ``payment_transaction_id``), then a
Stripe-descriptor map for non-invoice income, with an explicit
``UNATTRIBUTED`` row for anything left over. Delivered via Resend
(``src/reports/report_email.py``), monthly on the 1st, prior-month scope.

Design spec: docs/superpowers/specs/2026-07-07-reporting-suite-design.md §5.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, TypedDict
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from src.models.brokerage import Account
from src.models.enums import BillingModel, Entity, TransactionStatus
from src.models.invoice import Customer, Invoice
from src.models.transaction import Transaction
from src.reports.pl_engine import compute_entity_pl
from src.reports.report_config import load_config, to_decimal
from src.reports.report_email import dispatch_report

__all__ = [
    "SellabilityData",
    "compute_sellability",
    "render_report",
    "build_subject",
    "main",
]

DEFAULT_DB_PATH = str(Path(__file__).resolve().parents[2] / "data" / "accounting.db")

UNATTRIBUTED = "UNATTRIBUTED"

_TOP1_WARN_PCT = Decimal("50")
_TOP3_WARN_PCT = Decimal("80")
_UNATTRIBUTED_WARN_PCT = Decimal("10")

_EXCLUDED_STATUSES = (TransactionStatus.REJECTED.value, TransactionStatus.SPLIT_PARENT.value)

_SELLABILITY_DEFAULTS: dict[str, Any] = {
    "addback_categories": ["HEALTH_INSURANCE", "PERSONAL_NON_DEDUCTIBLE"],
    "owner_salary_monthly": "0.00",
    "one_time_items": [],
    "recurring_customers": {},
    "stripe_client_map": [],
}


def _today() -> date:
    return datetime.now(ZoneInfo("America/Los_Angeles")).date()


def _q2(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _q0(d: Decimal) -> Decimal:
    return d.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def _fmt0(d: Decimal) -> str:
    return f"${_q0(d):,.0f}"


def _fmt0_signed(d: Decimal) -> str:
    return f"(${_q0(abs(d)):,.0f})" if d < 0 else _fmt0(d)


# ── Period windows ───────────────────────────────────────────────────────


def _add_months(d: date, n: int) -> date:
    """d (always day=1) shifted by n months (n may be negative)."""
    month_index = d.month - 1 + n
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, 1)


def scope_month(today: date) -> tuple[date, date]:
    """Prior calendar month's half-open [start, end) — the 1st-of-month
    DRY-RUN scope (design spec §5.4)."""
    end = today.replace(day=1)
    start = _add_months(end, -1)
    return start, end


def _month_label(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _six_month_windows(scope_end: date) -> list[tuple[date, date]]:
    """6 consecutive month windows ending at scope_end, oldest first."""
    windows = []
    end = scope_end
    for _ in range(6):
        start = _add_months(end, -1)
        windows.append((start, end))
        end = start
    windows.reverse()
    return windows


# ── TypedDicts ────────────────────────────────────────────────────────────


class SDELine(TypedDict):
    label: str
    amount: Decimal


class SDESection(TypedDict):
    net_income: Decimal
    addback_lines: list[SDELine]
    sde: Decimal


class ClientRevenueRow(TypedDict):
    client: str
    amount: Decimal
    pct: Decimal
    recurring: bool
    unattributed: bool


class MoMRow(TypedDict):
    month: str
    revenue: Decimal
    sde: Decimal
    top1_pct: Decimal


class BlackLineBurn(TypedDict):
    net_month: Decimal
    net_ttm: Decimal


class SellabilityData(TypedDict):
    scope_month: str
    scope_start: str
    scope_end: str
    month_sde: SDESection
    ttm_sde: SDESection
    client_rows_ttm: list[ClientRevenueRow]
    top1_pct_ttm: Decimal
    top3_pct_ttm: Decimal
    unattributed_pct_ttm: Decimal
    concentration_warn: str | None
    unattributed_warn: str | None
    recurring_revenue_ttm: Decimal
    project_revenue_ttm: Decimal
    mom_trend: list[MoMRow]
    blackline: BlackLineBurn
    fidelity_tod_prompt: bool
    used_config_defaults: bool


# ── Add-backs / SDE ──────────────────────────────────────────────────────


def _addback_total(
    session: Session, start: date, end: date, categories: list[str]
) -> list[SDELine]:
    if not categories:
        return []
    rows = (
        session.query(Transaction)
        .filter(
            Transaction.entity == Entity.SPARKRY.value,
            Transaction.date >= start.isoformat(),
            Transaction.date < end.isoformat(),
            Transaction.tax_category.in_(categories),
            Transaction.status.notin_(_EXCLUDED_STATUSES),
        )
        .all()
    )
    by_cat: dict[str, Decimal] = {}
    for tx in rows:
        cat = tx.tax_category
        if cat is None:
            continue  # excluded by the `.in_(categories)` filter above; guards mypy's Optional
        amt = abs(Decimal(str(tx.amount))) if tx.amount is not None else Decimal("0")
        by_cat[cat] = by_cat.get(cat, Decimal("0")) + amt
    return [{"label": cat, "amount": _q2(amt)} for cat, amt in sorted(by_cat.items())]


def _months_in_window(start: date, end: date) -> int:
    return (end.year - start.year) * 12 + (end.month - start.month)


def _one_time_items_in_window(items: list[dict[str, Any]], start: date, end: date) -> list[SDELine]:
    out: list[SDELine] = []
    for item in items:
        item_date = date.fromisoformat(str(item["date"]))
        if start <= item_date < end:
            out.append({"label": f"one-time: {item['description']}", "amount": _q2(to_decimal(item["amount"]))})
    return out


def _compute_sde(
    session: Session, start: date, end: date, cfg: dict[str, Any]
) -> SDESection:
    net = compute_entity_pl(session, start.isoformat(), end.isoformat(), entity=Entity.SPARKRY.value).net
    lines = _addback_total(session, start, end, cfg["addback_categories"])
    owner_salary = to_decimal(cfg["owner_salary_monthly"]) * _months_in_window(start, end)
    if owner_salary > 0:
        lines.append({"label": "owner_salary_monthly", "amount": _q2(owner_salary)})
    lines.extend(_one_time_items_in_window(cfg["one_time_items"], start, end))
    sde = net + sum((line["amount"] for line in lines), Decimal("0"))
    return {"net_income": _q2(net), "addback_lines": lines, "sde": _q2(sde)}


# ── Client attribution ───────────────────────────────────────────────────


def _stripe_client_match(tx: Transaction, rules: list[dict[str, str]]) -> str | None:
    raw = tx.raw_data or {}
    customer_id = None
    if isinstance(raw, dict):
        customer_id = raw.get("customer") or (raw.get("metadata") or {}).get("customer") if isinstance(raw.get("metadata"), dict) else raw.get("customer")
    description = (tx.description or "").lower()
    for rule in rules:
        match = str(rule.get("match", ""))
        if match.startswith("customer:"):
            if customer_id and match[len("customer:"):] == customer_id:
                return rule["client"]
        elif match.startswith("desc_contains:"):
            needle = match[len("desc_contains:"):].lower()
            if needle and needle in description:
                return rule["client"]
    return None


def _billing_model_recurring(customer: Customer | None) -> bool:
    if customer is None:
        return False
    if customer.billing_model == BillingModel.FLAT_RATE.value:
        return True
    return customer.billing_model == BillingModel.HOURLY.value and bool(customer.calendar_patterns)


def _client_revenue(
    session: Session, start: date, end: date, cfg: dict[str, Any]
) -> list[ClientRevenueRow]:
    reimbursement_target_ids = {
        row[0]
        for row in session.query(Transaction.reimbursement_link)
        .filter(Transaction.reimbursement_link.is_not(None))
        .all()
    }
    income_rows = (
        session.query(Transaction)
        .filter(
            Transaction.entity == Entity.SPARKRY.value,
            Transaction.direction == "income",
            Transaction.date >= start.isoformat(),
            Transaction.date < end.isoformat(),
            Transaction.status.notin_(_EXCLUDED_STATUSES),
        )
        .all()
    )
    income_rows = [tx for tx in income_rows if tx.id not in reimbursement_target_ids]

    paid_invoices = (
        session.query(Invoice)
        .filter(Invoice.entity == Entity.SPARKRY.value, Invoice.status == "paid", Invoice.payment_transaction_id.is_not(None))
        .all()
    )
    txn_to_customer_id = {inv.payment_transaction_id: inv.customer_id for inv in paid_invoices}
    customer_ids = set(txn_to_customer_id.values())
    customers = {c.id: c for c in session.query(Customer).filter(Customer.id.in_(customer_ids)).all()} if customer_ids else {}

    by_client: dict[str, Decimal] = {}
    recurring_by_client: dict[str, bool] = {}
    rules = cfg["stripe_client_map"]
    overrides = cfg["recurring_customers"]

    for tx in income_rows:
        amt = abs(Decimal(str(tx.amount))) if tx.amount is not None else Decimal("0")
        customer_id = txn_to_customer_id.get(tx.id)
        client: str
        customer_obj: Customer | None = None
        if customer_id is not None:
            customer_obj = customers.get(customer_id)
            client = customer_obj.name if customer_obj else customer_id
        else:
            mapped = _stripe_client_match(tx, rules)
            client = mapped if mapped is not None else UNATTRIBUTED

        by_client[client] = by_client.get(client, Decimal("0")) + amt
        if client not in recurring_by_client:
            if client in overrides:
                recurring_by_client[client] = bool(overrides[client])
            else:
                recurring_by_client[client] = _billing_model_recurring(customer_obj)

    total = sum(by_client.values(), Decimal("0"))
    rows: list[ClientRevenueRow] = []
    for client, amount in sorted(by_client.items(), key=lambda kv: -kv[1]):
        pct = (amount / total * Decimal("100")) if total > 0 else Decimal("0")
        rows.append(
            {
                "client": client,
                "amount": _q2(amount),
                "pct": pct.quantize(Decimal("0.1")),
                "recurring": recurring_by_client.get(client, False),
                "unattributed": client == UNATTRIBUTED,
            }
        )
    return rows


# ── BlackLine burn + Fidelity TOD prompt (REQ-FIX-DAT-002) ────────────────


def _compute_blackline_burn(session: Session, month_start: date, month_end: date, ttm_start: date) -> BlackLineBurn:
    net_month = compute_entity_pl(session, month_start.isoformat(), month_end.isoformat(), entity=Entity.BLACKLINE.value).net
    net_ttm = compute_entity_pl(session, ttm_start.isoformat(), month_end.isoformat(), entity=Entity.BLACKLINE.value).net
    return {"net_month": _q2(net_month), "net_ttm": _q2(net_ttm)}


def _fidelity_tod_needs_prompt(session: Session) -> bool:
    """REQ-FIX-DAT-002: the unnamed $50 Fidelity TOD taxable account rides
    along in the close report — a report-only nudge — until a human names or
    archives it. Presence of any unnamed Fidelity TOD account is sufficient;
    no balance-history lookup needed for a yes/no prompt."""
    return (
        session.query(Account)
        .filter(Account.broker == "fidelity", Account.account_type == "tod", Account.account_name.is_(None))
        .count()
        > 0
    )


# ── Top-level compute + render ───────────────────────────────────────────


def compute_sellability(session: Session, today: date | None = None) -> SellabilityData:
    today = today or _today()
    start, end = scope_month(today)
    ttm_start = _add_months(end, -12)

    cfg = load_config("sellability.yaml", _SELLABILITY_DEFAULTS)
    cfg_data = dict(cfg.data)

    month_sde = _compute_sde(session, start, end, cfg_data)
    ttm_sde = _compute_sde(session, ttm_start, end, cfg_data)

    client_rows_ttm = _client_revenue(session, ttm_start, end, cfg_data)
    top1_pct = client_rows_ttm[0]["pct"] if client_rows_ttm else Decimal("0")
    top3_pct = sum((r["pct"] for r in client_rows_ttm[:3]), Decimal("0"))
    unattributed_row = next((r for r in client_rows_ttm if r["unattributed"]), None)
    unattributed_pct = unattributed_row["pct"] if unattributed_row else Decimal("0")

    concentration_warn = None
    if top1_pct > _TOP1_WARN_PCT:
        concentration_warn = f"top-1 {top1_pct}% > {_TOP1_WARN_PCT}%"
    elif top3_pct > _TOP3_WARN_PCT:
        concentration_warn = f"top-3 {top3_pct}% > {_TOP3_WARN_PCT}%"

    unattributed_warn = None
    if unattributed_pct > _UNATTRIBUTED_WARN_PCT:
        unattributed_warn = f"{unattributed_pct}% unattributed > {_UNATTRIBUTED_WARN_PCT}%"

    recurring_revenue = sum((r["amount"] for r in client_rows_ttm if r["recurring"]), Decimal("0"))
    project_revenue = sum((r["amount"] for r in client_rows_ttm if not r["recurring"]), Decimal("0"))

    mom_trend: list[MoMRow] = []
    for m_start, m_end in _six_month_windows(end):
        pl = compute_entity_pl(session, m_start.isoformat(), m_end.isoformat(), entity=Entity.SPARKRY.value)
        m_sde = _compute_sde(session, m_start, m_end, cfg_data)
        m_clients = _client_revenue(session, m_start, m_end, cfg_data)
        m_top1 = m_clients[0]["pct"] if m_clients else Decimal("0")
        mom_trend.append({"month": _month_label(m_start), "revenue": _q2(pl.revenue), "sde": m_sde["sde"], "top1_pct": m_top1})

    blackline = _compute_blackline_burn(session, start, end, ttm_start)
    fidelity_prompt = _fidelity_tod_needs_prompt(session)

    return {
        "scope_month": _month_label(start),
        "scope_start": start.isoformat(),
        "scope_end": end.isoformat(),
        "month_sde": month_sde,
        "ttm_sde": ttm_sde,
        "client_rows_ttm": client_rows_ttm,
        "top1_pct_ttm": top1_pct,
        "top3_pct_ttm": top3_pct,
        "unattributed_pct_ttm": unattributed_pct,
        "concentration_warn": concentration_warn,
        "unattributed_warn": unattributed_warn,
        "recurring_revenue_ttm": _q2(recurring_revenue),
        "project_revenue_ttm": _q2(project_revenue),
        "mom_trend": mom_trend,
        "blackline": blackline,
        "fidelity_tod_prompt": fidelity_prompt,
        "used_config_defaults": cfg.used_defaults,
    }


def build_subject(data: SellabilityData) -> str:
    return f"[SELL] {data['scope_month']} close · SDE {_fmt0(data['ttm_sde']['sde'])} TTM · top-1 {data['top1_pct_ttm']}%"


def _render_sde_section(title: str, sde: SDESection) -> list[str]:
    out = [title]
    out.append(f"  Sparkry net income: {_fmt0_signed(sde['net_income'])}")
    for line in sde["addback_lines"]:
        out.append(f"  + {line['label']}: {_fmt0(line['amount'])}")
    out.append(f"  = SDE: {_fmt0_signed(sde['sde'])}")
    return out


def render_sellability_section(data: SellabilityData) -> str:
    """Composable section body — the monthly-close agent (REQ-MCA-001,
    separate spec) will embed this in the close email; standalone until then
    (design spec §5.4)."""
    out: list[str] = []
    out.extend(_render_sde_section(f"SDE — {data['scope_month']}", data["month_sde"]))
    out.append("")
    out.extend(_render_sde_section("SDE — TTM", data["ttm_sde"]))

    out.append("")
    out.append("CLIENT REVENUE (TTM)")
    if not data["client_rows_ttm"]:
        out.append("  (no attributed Sparkry income in the TTM window)")
    else:
        for client_row in data["client_rows_ttm"]:
            flag = " ⚠️" if client_row["unattributed"] and data["unattributed_warn"] else ""
            rec = "recurring" if client_row["recurring"] else "project"
            out.append(f"  {client_row['client']:<30} {_fmt0(client_row['amount']):<10} {client_row['pct']:>5}%   {rec}{flag}")
    conc = data["concentration_warn"] or "within thresholds"
    out.append(f"  concentration: top-1 {data['top1_pct_ttm']}% · top-3 {data['top3_pct_ttm']}% ({conc})")

    out.append("")
    out.append("RECURRING vs PROJECT (TTM)")
    out.append(f"  recurring: {_fmt0(data['recurring_revenue_ttm'])}   project: {_fmt0(data['project_revenue_ttm'])}")

    out.append("")
    out.append("6-MONTH TREND (revenue / SDE / top-1 concentration)")
    for trend_row in data["mom_trend"]:
        out.append(f"  {trend_row['month']}   revenue {_fmt0(trend_row['revenue']):<10} SDE {_fmt0_signed(trend_row['sde']):<10} top-1 {trend_row['top1_pct']}%")

    out.append("")
    bl = data["blackline"]
    out.append("BLACKLINE (investment-mode — not sellability-framed)")
    out.append(f"  net this month: {_fmt0_signed(bl['net_month'])}   TTM cumulative: {_fmt0_signed(bl['net_ttm'])}")

    if data["fidelity_tod_prompt"]:
        out.append("")
        out.append("⚠️ Unnamed $50 Fidelity TOD account — name it or archive it (REQ-FIX-DAT-002, human decision).")

    return "\n".join(out)


def render_report(data: SellabilityData) -> str:
    header = f"SELLABILITY — {data['scope_month']} close (Sparkry)"
    return header + "\n\n" + render_sellability_section(data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sellability_dispatch", description="Sellability metrics (REQ-SEL-001..002).")
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
        data = compute_sellability(session, today)
        body = render_report(data)
        subject = build_subject(data)
        if not args.apply:
            print(body)
            return 0
        result = dispatch_report(
            session,
            alert_key=f"sel:{data['scope_month']}",
            occurrence_date=data["scope_start"],
            alert_type="sellability_monthly",
            entity="sparkry",
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
