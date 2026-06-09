"""Tests for src/export/retail_sales_tax.py — BlackLine WA retail sales tax + B&O detail."""

from __future__ import annotations

from decimal import Decimal

from src.export.retail_sales_tax import (
    WA_LOCATION_CODES,
    compute_retail_detail,
    retail_facts,
)


def _shopify_order(
    *,
    date: str,
    total_price: str,
    total_tax: str = "0.00",
    ship_state: str | None = None,
    city_tax_title: str | None = None,
    tax_category: str = "SALES_INCOME",
) -> dict:
    """A transaction dict shaped like a Shopify order (amount = total_price)."""
    tax_lines = []
    if Decimal(total_tax) > 0:
        tax_lines.append({"title": "Washington State Tax", "rate": 0.065, "price": "0"})
        if city_tax_title:
            tax_lines.append({"title": city_tax_title, "rate": 0.038, "price": "0"})
    raw: dict = {
        "total_price": total_price,
        "total_tax": total_tax,
        "tax_lines": tax_lines,
    }
    if ship_state:
        raw["shipping_address"] = {"province_code": ship_state, "city": "X"}
    return {
        "date": date,
        "amount": total_price,  # adapter stores total_price (incl tax)
        "tax_category": tax_category,
        "source": "shopify",
        "raw_data": raw,
    }


class TestLocationCodes:
    def test_known_king_county_codes(self) -> None:
        # Authoritative WA DOR Q2 2026 codes.
        assert WA_LOCATION_CODES["issaquah"][0] == "1714"
        assert WA_LOCATION_CODES["kirkland"][0] == "1716"
        assert WA_LOCATION_CODES["maple valley"][0] == "1720"
        assert WA_LOCATION_CODES["sammamish"][0] == "1739"


class TestRetailFacts:
    def test_pretax_excludes_collected_tax(self) -> None:
        tx = _shopify_order(date="2026-04-05", total_price="89.36", total_tax="7.38",
                            ship_state="WA", city_tax_title="Sammamish City Tax")
        f = retail_facts(tx)
        assert f.pretax == Decimal("81.98")  # 89.36 - 7.38
        assert f.sales_tax == Decimal("7.38")
        assert f.is_wa is True
        assert f.location_code == "1739"

    def test_out_of_state_no_tax(self) -> None:
        tx = _shopify_order(date="2026-01-02", total_price="82.80", total_tax="0.00",
                            ship_state="MT")
        f = retail_facts(tx)
        assert f.pretax == Decimal("82.80")
        assert f.sales_tax == Decimal("0.00")
        assert f.is_wa is False

    def test_wa_detected_from_state_tax_line_when_address_blank(self) -> None:
        # Order #1022 case: blank ship address but a "Washington State Tax" line.
        tx = _shopify_order(date="2026-03-21", total_price="198.54", total_tax="18.54",
                            ship_state=None, city_tax_title="Sammamish City Tax")
        f = retail_facts(tx)
        assert f.is_wa is True
        assert f.location_code == "1739"


class TestComputeRetailDetail:
    def _txns(self) -> list[dict]:
        return [
            # WA — Sammamish, tax collected
            _shopify_order(date="2026-01-15", total_price="108.40", total_tax="8.40",
                          ship_state="WA", city_tax_title="Sammamish City Tax"),
            # WA — Issaquah, tax collected
            _shopify_order(date="2026-02-10", total_price="52.50", total_tax="5.00",
                          ship_state="WA", city_tax_title="Issaquah City Tax"),
            # Out-of-state — Oregon, no tax
            _shopify_order(date="2026-03-01", total_price="100.00", total_tax="0.00",
                          ship_state="OR"),
            # Non-retail income must be ignored
            _shopify_order(date="2026-02-01", total_price="500.00", total_tax="0.00",
                          tax_category="CONSULTING_INCOME"),
        ]

    def test_quarter_gross_is_pretax(self) -> None:
        d = compute_retail_detail(self._txns(), 2026, quarter=1)
        # pretax: (108.40-8.40) + (52.50-5.00) + 100.00 = 100 + 47.50 + 100 = 247.50
        assert d.gross_retailing == Decimal("247.50")

    def test_interstate_deduction_and_wa_taxable(self) -> None:
        d = compute_retail_detail(self._txns(), 2026, quarter=1)
        assert d.interstate_deduction == Decimal("100.00")  # the OR order
        assert d.wa_taxable == Decimal("147.50")  # 247.50 - 100.00

    def test_retailing_bo_uses_wa_taxable_at_retailing_rate(self) -> None:
        d = compute_retail_detail(self._txns(), 2026, quarter=1)
        # 147.50 * 0.00471 = 0.6947 → 0.69
        assert d.retailing_bo == Decimal("0.69")

    def test_sales_tax_collected_total(self) -> None:
        d = compute_retail_detail(self._txns(), 2026, quarter=1)
        assert d.sales_tax_collected == Decimal("13.40")  # 8.40 + 5.00

    def test_by_location_breakdown(self) -> None:
        d = compute_retail_detail(self._txns(), 2026, quarter=1)
        by = {loc.location_code: loc for loc in d.by_location}
        assert by["1739"].location_name == "Sammamish"
        assert by["1739"].taxable_amount == Decimal("100.00")  # 108.40 - 8.40
        assert by["1739"].tax_collected == Decimal("8.40")
        assert by["1714"].location_name == "Issaquah"
        assert by["1714"].tax_collected == Decimal("5.00")

    def test_unknown_destination_stays_wa_taxable(self) -> None:
        # A Stripe-style retail sale: no tax_lines, no address → destination
        # unknown. It must NOT be deducted as interstate (we can't prove it's
        # out of state) and must NOT create a sales-tax location bucket.
        txns = [
            {
                "date": "2026-01-20",
                "amount": "300.00",
                "tax_category": "SALES_INCOME",
                "source": "stripe",
                "raw_data": {},
            },
            _shopify_order(date="2026-02-01", total_price="100.00", total_tax="0.00",
                          ship_state="OR"),  # confirmed OOS
        ]
        d = compute_retail_detail(txns, 2026, quarter=1)
        assert d.gross_retailing == Decimal("400.00")
        assert d.interstate_deduction == Decimal("100.00")  # only the OR order
        assert d.wa_taxable == Decimal("300.00")  # unknown stays WA-taxable
        assert d.by_location == []  # no tax collected anywhere

    def test_month_filter(self) -> None:
        d = compute_retail_detail(self._txns(), 2026, month=1)
        # Only the Jan Sammamish order
        assert d.gross_retailing == Decimal("100.00")
        assert d.sales_tax_collected == Decimal("8.40")

    def test_requires_exactly_one_period(self) -> None:
        import pytest
        with pytest.raises(ValueError):
            compute_retail_detail([], 2026)
        with pytest.raises(ValueError):
            compute_retail_detail([], 2026, month=1, quarter=1)
