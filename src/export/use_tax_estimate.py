"""WA use-tax estimate for comped ($0) BlackLine Shopify orders.

BlackLine gives apparel away (contest prizes, photoshoot models,
collaborators) via $0 Shopify orders. Those orders correctly book $0 revenue
(no B&O impact — see CLAUDE.md "Critical Rules"), but WA owes use tax on the
COST of goods consumed/given away since no retail sales tax was collected on
them (WAC 458-20-178). This is a separate liability from B&O and is not
computed anywhere else in the export pipeline.

The register has no per-SKU cost-of-goods linkage to trace exact COGS per
comped unit, so this module estimates the liability from unit counts times a
caller-supplied average per-unit cost, not exact COGS.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

COMP_ENTITY = "blackline"
COMP_SOURCE = "shopify"


def _to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _q2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def find_comped_orders(txs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return confirmed BlackLine Shopify orders that netted to $0 (comps).

    Excludes rejected rows and non-order Shopify rows (payouts, refunds)
    which share ``source == "shopify"`` but carry no ``line_items``.
    """
    out = []
    for tx in txs:
        if tx.get("entity") != COMP_ENTITY:
            continue
        if tx.get("source") != COMP_SOURCE:
            continue
        if tx.get("status") == "rejected":
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
    """Estimate WAC 458-20-178 use-tax liability on comped units.

    ``unit_cost`` is an assumed average per-unit COGS — supply the entity's
    known average landed cost per unit; ``rate`` is the applicable combined
    local use-tax rate (~9.0-10.3% per the issue's sizing method).
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
