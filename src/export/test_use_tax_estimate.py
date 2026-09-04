"""Tests for the WA use-tax estimate on comped ($0) BlackLine Shopify orders.

REQ-UTX-001..005 / sparkst/accounting#59 — WAC 458-20-178 gap. Comped orders
correctly book $0 revenue (no B&O impact), but nothing estimates the use-tax
liability owed on the COST of the goods given away. This module is the
report-only estimate (Option A, decided-by: travis 2026-08-28), wired into the
monthly-close report so a quarter-to-date number surfaces where Travis reads it.

The fence: on the parent commit (before this module exists) these fail with
ModuleNotFoundError, proving the gap; after the fix they pass.
"""

from __future__ import annotations

import itertools
from collections.abc import Generator
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import src.db.connection  # noqa: F401  (register every ORM table on Base)
from src.export.use_tax_estimate import (
    UseTaxConfig,
    UseTaxEstimate,
    UseTaxQuarterSummary,
    build_use_tax_summary,
    estimate_use_tax_accrual,
    find_comped_orders,
    load_use_tax_config,
    quarter_of_month,
    render_use_tax_section,
)
from src.models.base import Base
from src.models.enums import Entity, Source, TransactionStatus
from src.models.transaction import Transaction

# ── pure-primitive fixtures (dict-shaped, REQ-UTX-001/002) ──────────────────

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
        "line_items": [{"title": "BlackLine Jersey", "quantity": 1, "price": "45.00"}],
    },
}

COMP_ORDER_MULTI_UNIT = {
    "entity": "blackline",
    "source": "shopify",
    "status": "confirmed",
    "amount": "0.00",
    "raw_data": {
        "id": 1018,
        "line_items": [{"title": "BlackLine Jersey", "quantity": 2, "price": "45.00"}],
    },
}

NEEDS_REVIEW_COMP_ORDER = {
    "entity": "blackline",
    "source": "shopify",
    "status": "needs_review",
    "amount": "0.00",
    "raw_data": {"id": 1099, "line_items": [{"quantity": 1}]},
}

PAID_ORDER = {
    "entity": "blackline",
    "source": "shopify",
    "status": "confirmed",
    "amount": "85.00",
    "raw_data": {"id": 1042, "line_items": [{"quantity": 1, "price": "75.00"}]},
}

SPARKRY_ZERO_ORDER = {
    "entity": "sparkry",
    "source": "shopify",
    "status": "confirmed",
    "amount": "0.00",
    "raw_data": {"id": 999, "line_items": [{"quantity": 1}]},
}

REJECTED_COMP_ORDER = {
    "entity": "blackline",
    "source": "shopify",
    "status": "rejected",
    "amount": "0.00",
    "raw_data": {"id": 1019, "line_items": [{"quantity": 1}]},
}

SHOPIFY_PAYOUT = {
    "entity": "blackline",
    "source": "shopify",
    "status": "confirmed",
    "amount": "0.00",
    "raw_data": {"id": 5001, "payout_type": "deposit"},  # no line_items
}


def test_find_comped_orders_selects_zero_amount_confirmed_blackline_shopify() -> None:
    txs = [COMP_ORDER, COMP_ORDER_MULTI_UNIT, PAID_ORDER]
    assert find_comped_orders(txs) == [COMP_ORDER, COMP_ORDER_MULTI_UNIT]


def test_find_comped_orders_requires_confirmed_excludes_needs_review() -> None:
    # REQ-UTX-002 / the ingest-status fix: Shopify orders land at needs_review;
    # only human-CONFIRMED comps feed the filing number (PR #68 wrongly counted
    # needs_review rows). This is the substantive correction over the primitive.
    assert find_comped_orders([NEEDS_REVIEW_COMP_ORDER]) == []


def test_find_comped_orders_excludes_rejected_status() -> None:
    assert find_comped_orders([REJECTED_COMP_ORDER]) == []


def test_find_comped_orders_excludes_other_entities() -> None:
    assert find_comped_orders([SPARKRY_ZERO_ORDER]) == []


def test_find_comped_orders_excludes_non_order_shopify_rows() -> None:
    assert find_comped_orders([SHOPIFY_PAYOUT]) == []


def test_estimate_use_tax_accrual_sums_units_and_applies_rate() -> None:
    result = estimate_use_tax_accrual(
        [COMP_ORDER, COMP_ORDER_MULTI_UNIT],
        unit_cost=Decimal("30.00"),
        rate=Decimal("0.103"),
    )
    assert result == UseTaxEstimate(
        order_count=2,
        unit_count=3,
        unit_cost=Decimal("30.00"),
        cost_basis=Decimal("90.00"),
        rate=Decimal("0.103"),
        estimated_tax=Decimal("9.27"),
    )


def test_estimate_use_tax_accrual_empty_orders_is_zero() -> None:
    result = estimate_use_tax_accrual([], unit_cost=Decimal("30.00"), rate=Decimal("0.103"))
    assert result.unit_count == 0
    assert result.estimated_tax == Decimal("0.00")


# ── config loader (REQ-UTX-003) ─────────────────────────────────────────────


def test_load_use_tax_config_missing_file_returns_none(tmp_path: Path) -> None:
    assert load_use_tax_config(tmp_path / "nope.yaml") is None


def test_load_use_tax_config_zero_values_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "use_tax.yaml"
    p.write_text("avg_unit_cost: 0.00\nuse_tax_rate: 0.00\n")
    # Report-only + Travis-supplied: an unfilled config must NOT invent a
    # filing position; it degrades to UNAVAILABLE (like tax_profile.yaml).
    assert load_use_tax_config(p) is None


def test_load_use_tax_config_valid_returns_decimals(tmp_path: Path) -> None:
    p = tmp_path / "use_tax.yaml"
    p.write_text("avg_unit_cost: 30.00\nuse_tax_rate: 0.103\n")
    cfg = load_use_tax_config(p)
    assert cfg == UseTaxConfig(unit_cost=Decimal("30.00"), rate=Decimal("0.103"))


def test_quarter_of_month() -> None:
    assert quarter_of_month("2026-01") == 1
    assert quarter_of_month("2026-03") == 1
    assert quarter_of_month("2026-07") == 3
    assert quarter_of_month("2026-12") == 4


# ── quarter query + summary against a real session (REQ-UTX-004) ────────────

_ENGINE = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
Base.metadata.create_all(_ENGINE)
_Session = sessionmaker(bind=_ENGINE)
_counter = itertools.count()


@pytest.fixture()
def session() -> Generator[Session, None, None]:
    s = _Session()
    yield s
    s.rollback()
    s.query(Transaction).delete()
    s.commit()
    s.close()


def _comp(session: Session, *, dt: str, units: int, status: str, **over: object) -> Transaction:
    defaults: dict[str, object] = {
        "source": Source.SHOPIFY.value,
        "source_hash": f"h-{next(_counter)}",
        "date": dt,
        "description": "Comp order",
        "amount": Decimal("0.00"),
        "entity": Entity.BLACKLINE.value,
        "status": status,
        "confirmed_by": "human",
        "raw_data": {"id": next(_counter), "line_items": [{"quantity": units}]},
    }
    defaults.update(over)
    tx = Transaction(**defaults)
    session.add(tx)
    session.flush()
    return tx


def test_build_use_tax_summary_counts_confirmed_comps_in_quarter(session: Session, tmp_path: Path) -> None:
    # Q3 2026 = Jul/Aug/Sep. Two confirmed comps in-quarter (1 + 2 units),
    # plus noise that must be excluded.
    _comp(session, dt="2026-07-05", units=1, status=TransactionStatus.CONFIRMED.value)
    _comp(session, dt="2026-08-20", units=2, status=TransactionStatus.CONFIRMED.value)
    _comp(session, dt="2026-06-30", units=5, status=TransactionStatus.CONFIRMED.value)  # Q2, out
    _comp(session, dt="2026-07-10", units=9, status=TransactionStatus.NEEDS_REVIEW.value)  # unconfirmed
    _comp(session, dt="2026-07-11", units=4, amount=Decimal("85.00"),
          status=TransactionStatus.CONFIRMED.value)  # paid, not comp
    session.flush()

    cfg = tmp_path / "use_tax.yaml"
    cfg.write_text("avg_unit_cost: 30.00\nuse_tax_rate: 0.103\n")
    summary = build_use_tax_summary(session, "2026-08", config_path=cfg)

    assert summary.year == 2026
    assert summary.quarter == 3
    assert summary.order_count == 2
    assert summary.unit_count == 3
    assert summary.estimate is not None
    assert summary.estimate.cost_basis == Decimal("90.00")
    assert summary.estimate.estimated_tax == Decimal("9.27")
    assert summary.unavailable_reason is None


def test_build_use_tax_summary_unavailable_without_config(session: Session, tmp_path: Path) -> None:
    _comp(session, dt="2026-07-05", units=2, status=TransactionStatus.CONFIRMED.value)
    session.flush()
    summary = build_use_tax_summary(session, "2026-08", config_path=tmp_path / "absent.yaml")
    assert summary.order_count == 1
    assert summary.unit_count == 2
    assert summary.estimate is None
    assert summary.unavailable_reason is not None
    assert "use_tax.yaml" in summary.unavailable_reason


# ── section render (REQ-UTX-005) ────────────────────────────────────────────


def _summary(**over: object) -> UseTaxQuarterSummary:
    est = estimate_use_tax_accrual(
        [COMP_ORDER, COMP_ORDER_MULTI_UNIT], unit_cost=Decimal("30.00"), rate=Decimal("0.103")
    )
    base: dict[str, object] = {
        "year": 2026,
        "quarter": 3,
        "order_count": 2,
        "unit_count": 3,
        "estimate": est,
        "unavailable_reason": None,
    }
    base.update(over)
    return UseTaxQuarterSummary(**base)  # type: ignore[arg-type]


def test_render_use_tax_section_has_estimate_and_disclaimer() -> None:
    text = render_use_tax_section(_summary())
    assert "WAC 458-20-178" in text
    assert "Q3 2026" in text
    assert "$9.27" in text
    assert "report-only" in text.lower()


def test_render_use_tax_section_unavailable_names_config() -> None:
    text = render_use_tax_section(
        _summary(estimate=None, unavailable_reason="config/use_tax.yaml not set")
    )
    assert "UNAVAILABLE" in text
    assert "use_tax.yaml" in text
    assert "$" not in text.split("UNAVAILABLE")[1]  # no dollar figure once unavailable


def test_render_use_tax_section_zero_orders() -> None:
    text = render_use_tax_section(_summary(order_count=0, unit_count=0, estimate=None))
    assert "No confirmed comped" in text
