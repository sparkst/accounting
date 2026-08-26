"""Tests for WA use-tax estimate on comped ($0) BlackLine Shopify orders.

REQ: sparkst/accounting#59 — WAC 458-20-178 gap. Comped orders correctly book
$0 revenue (no B&O impact), but nothing estimates the use-tax liability owed
on the cost of the goods given away. These tests are the fence: on the parent
commit (before this module exists) they fail with ModuleNotFoundError,
proving the gap; after the fix they pass.
"""

from decimal import Decimal

from src.export.use_tax_estimate import (
    UseTaxEstimate,
    estimate_use_tax_accrual,
    find_comped_orders,
)

COMP_ORDER = {
    "entity": "blackline",
    "source": "shopify",
    "status": "confirmed",
    "amount": "0.00",
    "raw_data": {
        "id": 1017,
        "name": "#1017",
        "total_price": "0.00",
        "total_discounts": "45.00",
        "payment_gateway_names": [],
        "line_items": [
            {"title": "BlackLine Jersey", "quantity": 1, "price": "45.00"},
        ],
    },
}

COMP_ORDER_MULTI_UNIT = {
    "entity": "blackline",
    "source": "shopify",
    "status": "confirmed",
    "amount": "0.00",
    "raw_data": {
        "id": 1018,
        "name": "#1018",
        "total_price": "0.00",
        "total_discounts": "90.00",
        "payment_gateway_names": [],
        "line_items": [
            {"title": "BlackLine Jersey", "quantity": 2, "price": "45.00"},
        ],
    },
}

PAID_ORDER = {
    "entity": "blackline",
    "source": "shopify",
    "status": "confirmed",
    "amount": "85.00",
    "raw_data": {
        "id": 1042,
        "name": "#1042",
        "total_price": "85.00",
        "total_discounts": "0.00",
        "line_items": [{"title": "BlackLine Jersey", "quantity": 1, "price": "75.00"}],
    },
}

SPARKRY_ZERO_ORDER = {
    "entity": "sparkry",
    "source": "shopify",
    "status": "confirmed",
    "amount": "0.00",
    "raw_data": {"id": 999, "total_price": "0.00", "line_items": [{"quantity": 1}]},
}

REJECTED_COMP_ORDER = {
    "entity": "blackline",
    "source": "shopify",
    "status": "rejected",
    "amount": "0.00",
    "raw_data": {"id": 1019, "total_price": "0.00", "line_items": [{"quantity": 1}]},
}

SHOPIFY_PAYOUT = {
    "entity": "blackline",
    "source": "shopify",
    "status": "confirmed",
    "amount": "0.00",
    "raw_data": {"id": 5001, "payout_type": "deposit"},
}


def test_find_comped_orders_selects_zero_amount_blackline_shopify_orders():
    txs = [COMP_ORDER, COMP_ORDER_MULTI_UNIT, PAID_ORDER]
    result = find_comped_orders(txs)
    assert result == [COMP_ORDER, COMP_ORDER_MULTI_UNIT]


def test_find_comped_orders_excludes_other_entities():
    result = find_comped_orders([SPARKRY_ZERO_ORDER])
    assert result == []


def test_find_comped_orders_excludes_rejected_status():
    result = find_comped_orders([REJECTED_COMP_ORDER])
    assert result == []


def test_find_comped_orders_excludes_non_order_shopify_rows():
    result = find_comped_orders([SHOPIFY_PAYOUT])
    assert result == []


def test_estimate_use_tax_accrual_sums_units_and_applies_rate():
    orders = [COMP_ORDER, COMP_ORDER_MULTI_UNIT]
    result = estimate_use_tax_accrual(
        orders, unit_cost=Decimal("30.00"), rate=Decimal("0.103")
    )
    assert result == UseTaxEstimate(
        order_count=2,
        unit_count=3,
        unit_cost=Decimal("30.00"),
        cost_basis=Decimal("90.00"),
        rate=Decimal("0.103"),
        estimated_tax=Decimal("9.27"),
    )


def test_estimate_use_tax_accrual_empty_orders_is_zero():
    result = estimate_use_tax_accrual([], unit_cost=Decimal("30.00"), rate=Decimal("0.103"))
    assert result.unit_count == 0
    assert result.estimated_tax == Decimal("0.00")
