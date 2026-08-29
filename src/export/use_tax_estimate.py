"""WA use-tax estimate for comped ($0) BlackLine Shopify orders (WAC 458-20-178).

REQ-UTX-001..005 / sparkst/accounting#59.

BlackLine gives apparel away (contest prizes, photoshoot models, collaborators)
via $0 Shopify orders. Those orders correctly book $0 revenue (no B&O impact —
see CLAUDE.md "Critical Rules"), but WA owes use tax on the COST of goods
consumed/given away since no retail sales tax was collected (WAC 458-20-178).
This is a separate liability from B&O and is computed nowhere else.

Decided option (Option A, decided-by: travis 2026-08-28): a **report-only**
quarter-to-date estimate, surfaced in the monthly-close report. Nothing is
written to the register and no filing position is auto-committed — the two tax
parameters (assumed average per-unit COGS + local use-tax rate) are Travis's
WA DOR filing position, read from ``config/use_tax.yaml`` (gitignored). Until
that file is filled the section shows the comped-unit counts and prints
"estimate UNAVAILABLE", mirroring ``config/tax_profile.yaml``'s pattern.

The register has no per-SKU cost-of-goods linkage, so the estimate is
unit_count × assumed average per-unit cost × rate, not exact COGS (the issue's
own sizing method).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from src.export.basis import RETAIL_CATEGORIES  # noqa: F401  (kept for import parity)
from src.models.enums import Entity, Source, TransactionStatus
from src.models.transaction import Transaction
from src.reports.report_config import CONFIG_DIR, load_yaml, to_decimal

COMP_ENTITY = Entity.BLACKLINE.value
COMP_SOURCE = Source.SHOPIFY.value
_CONFIRMED = TransactionStatus.CONFIRMED.value

_CONFIG_PATH = CONFIG_DIR / "use_tax.yaml"


def _to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _q2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ── pure primitives (REQ-UTX-001/002) ───────────────────────────────────────


def find_comped_orders(txs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return **confirmed** BlackLine Shopify orders that netted to $0 (comps).

    Requires ``status == "confirmed"``: Shopify orders ingest at
    ``needs_review`` (``shopify_adapter.py:_parse_order``), so only
    human-verified comps feed the filing number (REQ-UTX-002 — the substantive
    fix over the PR #68 primitive, which counted needs_review rows). Excludes
    non-order Shopify rows (payouts, refunds) which share ``source == "shopify"``
    but carry no ``line_items``.
    """
    out = []
    for tx in txs:
        if tx.get("entity") != COMP_ENTITY:
            continue
        if tx.get("source") != COMP_SOURCE:
            continue
        if tx.get("status") != _CONFIRMED:
            continue
        raw = tx.get("raw_data") or {}
        if "line_items" not in raw:
            continue
        if _to_decimal(tx.get("amount", "0")) != Decimal("0"):
            continue
        out.append(tx)
    return out


def _order_unit_count(order: dict[str, Any]) -> int:
    raw = order.get("raw_data") or {}
    return sum(int(li.get("quantity", 0)) for li in raw.get("line_items", []))


@dataclass(frozen=True)
class UseTaxEstimate:
    order_count: int
    unit_count: int
    unit_cost: Decimal
    cost_basis: Decimal
    rate: Decimal
    estimated_tax: Decimal


def estimate_use_tax_accrual(
    orders: list[dict[str, Any]],
    unit_cost: Decimal,
    rate: Decimal,
) -> UseTaxEstimate:
    """Estimate the WAC 458-20-178 use-tax liability on comped units.

    ``unit_cost`` is the assumed average per-unit COGS; ``rate`` is the
    applicable combined local use-tax rate (~9.0–10.3% per the issue's sizing).
    """
    unit_count = sum(_order_unit_count(o) for o in orders)
    cost_basis = _q2(_to_decimal(unit_count) * unit_cost)
    estimated_tax = _q2(cost_basis * rate)
    return UseTaxEstimate(
        order_count=len(orders),
        unit_count=unit_count,
        unit_cost=unit_cost,
        cost_basis=cost_basis,
        rate=rate,
        estimated_tax=estimated_tax,
    )


# ── config (REQ-UTX-003) ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class UseTaxConfig:
    unit_cost: Decimal
    rate: Decimal


def load_use_tax_config(path: Path | None = None) -> UseTaxConfig | None:
    """Load the Travis-supplied tax parameters from ``config/use_tax.yaml``.

    Returns ``None`` (→ UNAVAILABLE) when the file is missing or either value
    is unset / ``<= 0``, so an unfilled config never invents a filing position
    (the ``tax_profile.yaml`` "business-only until filled" pattern).
    """
    path = path or _CONFIG_PATH
    profile = load_yaml(path)
    if not profile:
        return None
    unit_cost = to_decimal(profile.get("avg_unit_cost", 0))
    rate = to_decimal(profile.get("use_tax_rate", 0))
    if unit_cost <= 0 or rate <= 0:
        return None
    return UseTaxConfig(unit_cost=unit_cost, rate=rate)


# ── quarter query + summary (REQ-UTX-004) ────────────────────────────────────


def quarter_of_month(month: str) -> int:
    """Quarter (1–4) containing the ``YYYY-MM`` month."""
    m = int(month[5:7])
    return (m - 1) // 3 + 1


def _quarter_bounds(year: int, quarter: int) -> tuple[str, str]:
    first_month = 3 * (quarter - 1) + 1
    first = date(year, first_month, 1)
    end_month = first_month + 3
    end_year = year + (end_month > 12)
    end_month = (end_month - 1) % 12 + 1
    last = date(end_year, end_month, 1) - timedelta(days=1)
    return first.isoformat(), last.isoformat()


def comped_orders_in_quarter(session: Session, year: int, quarter: int) -> list[dict[str, Any]]:
    """Confirmed BlackLine Shopify $0 orders dated within the quarter."""
    first, last = _quarter_bounds(year, quarter)
    rows = (
        session.query(Transaction)
        .filter(
            Transaction.entity == COMP_ENTITY,
            Transaction.source == COMP_SOURCE,
            Transaction.status == _CONFIRMED,
            Transaction.amount == 0,
            Transaction.date >= first,
            Transaction.date <= last,
        )
        .all()
    )
    # Re-use the pure filter for the line_items / amount invariants so the DB
    # path and the dict path apply exactly one predicate.
    return find_comped_orders(
        [
            {
                "entity": r.entity,
                "source": r.source,
                "status": r.status,
                "amount": r.amount,
                "raw_data": r.raw_data,
            }
            for r in rows
        ]
    )


@dataclass(frozen=True)
class UseTaxQuarterSummary:
    year: int
    quarter: int
    order_count: int
    unit_count: int
    estimate: UseTaxEstimate | None
    unavailable_reason: str | None


def build_use_tax_summary(
    session: Session,
    month: str,
    *,
    config_path: Path | None = None,
) -> UseTaxQuarterSummary:
    """Assemble the quarter-to-date comped-order use-tax summary for *month*.

    Always reports the confirmed comped-order and unit counts (so the liability
    is visible even before config is filled); the dollar estimate is present
    only when ``config/use_tax.yaml`` supplies both parameters.
    """
    year = int(month[:4])
    quarter = quarter_of_month(month)
    orders = comped_orders_in_quarter(session, year, quarter)
    order_count = len(orders)
    unit_count = sum(_order_unit_count(o) for o in orders)

    cfg = load_use_tax_config(config_path)
    if cfg is None:
        return UseTaxQuarterSummary(
            year=year,
            quarter=quarter,
            order_count=order_count,
            unit_count=unit_count,
            estimate=None,
            unavailable_reason="set config/use_tax.yaml (avg per-unit COGS + local use-tax rate)",
        )
    estimate = estimate_use_tax_accrual(orders, unit_cost=cfg.unit_cost, rate=cfg.rate)
    return UseTaxQuarterSummary(
        year=year,
        quarter=quarter,
        order_count=order_count,
        unit_count=unit_count,
        estimate=estimate,
        unavailable_reason=None,
    )


# ── section render (REQ-UTX-005) ─────────────────────────────────────────────


def render_use_tax_section(summary: UseTaxQuarterSummary) -> str:
    """Plain-text section for the monthly-close email (mono block)."""
    header = f"WA use tax — comped orders (WAC 458-20-178) · Q{summary.quarter} {summary.year}"
    if summary.order_count == 0:
        return f"{header}\n  No confirmed comped BlackLine orders this quarter."

    counts = (
        f"  Comped BlackLine orders (confirmed): {summary.order_count}"
        f"  ·  units given away: {summary.unit_count}"
    )
    if summary.estimate is None:
        return (
            f"{header}\n{counts}\n"
            f"  Estimate UNAVAILABLE — {summary.unavailable_reason}."
        )

    e = summary.estimate
    rate_pct = (e.rate * Decimal("100")).quantize(Decimal("0.01"))
    return (
        f"{header}\n{counts}\n"
        f"  Assumed avg per-unit COGS: ${e.unit_cost:,.2f}  ·  local use-tax rate: {rate_pct}%\n"
        f"  Cost basis: ${e.cost_basis:,.2f}  ·  estimated use tax: ${e.estimated_tax:,.2f}\n"
        f"  Report-only estimate — not booked; file on the BlackLine CETR (quarterly)."
    )
