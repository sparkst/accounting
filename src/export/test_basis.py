"""Tests for src/export/basis.py — canonical pre-tax amount computation.

REQ-ID: REQ-FIX-TAX-002  All export surfaces (B&O CSVs, FreeTaxUSA, TaxAct,
DOR upload) report SALES_INCOME on the same pre-tax basis; collected WA
sales tax is excluded from gross receipts everywhere, not just the DOR
upload.
"""

from __future__ import annotations

from decimal import Decimal

from src.export.basis import RetailFacts, pretax_abs_amount, retail_facts


def _taxed_order(*, total_price: str, total_tax: str, ship_state: str = "WA") -> dict:
    return {
        "date": "2026-04-05",
        "amount": total_price,
        "tax_category": "SALES_INCOME",
        "source": "shopify",
        "raw_data": {
            "total_price": total_price,
            "total_tax": total_tax,
            "tax_lines": [{"title": "Washington State Tax", "rate": 0.065, "price": "0"}],
            "shipping_address": {"province_code": ship_state},
        },
    }


class TestPretaxAbsAmountIncome:
    def test_taxed_order_excludes_collected_tax(self) -> None:
        """A SALES_INCOME row with tax_lines reports the pre-tax figure, not
        the tax-inclusive stored amount."""
        tx = _taxed_order(total_price="108.40", total_tax="8.40")
        assert pretax_abs_amount(tx) == Decimal("100.00")

    def test_no_raw_data_order_degrades_to_abs_amount(self) -> None:
        """A SALES_INCOME row with no raw_data/tax_lines (e.g. a manually
        entered or Stripe-adjacent row) degrades to abs(amount) — collected
        tax is only excludable when substantiated."""
        tx = {
            "date": "2026-01-20",
            "amount": "300.00",
            "tax_category": "SALES_INCOME",
            "source": "stripe",
            "raw_data": {},
        }
        assert pretax_abs_amount(tx) == Decimal("300.00")

    def test_wholesale_income_is_not_a_retail_category(self) -> None:
        """WHOLESALE_INCOME never carries sales tax lines — treated as a
        plain abs(amount) row, not routed through retail_facts."""
        tx = {"amount": "500.00", "tax_category": "WHOLESALE_INCOME", "raw_data": {}}
        assert pretax_abs_amount(tx) == Decimal("500.00")


class TestPretaxAbsAmountExpense:
    def test_expense_row_uses_abs_amount(self) -> None:
        tx = {"amount": "-238.03", "tax_category": "SUPPLIES"}
        assert pretax_abs_amount(tx) == Decimal("238.03")

    def test_deductible_pct_applied_to_expense(self) -> None:
        tx = {"amount": "-100.00", "tax_category": "MEALS", "deductible_pct": "0.5"}
        assert pretax_abs_amount(tx) == Decimal("50.00")

    def test_deductible_pct_defaults_to_one(self) -> None:
        tx = {"amount": "-42.00", "tax_category": "OFFICE_EXPENSE"}
        assert pretax_abs_amount(tx) == Decimal("42.00")


class TestRetailFactsReExport:
    """retail_facts / RetailFacts live in basis.py; sanity check they still
    behave as before the move (verbatim relocation, no behavior change)."""

    def test_retail_facts_returns_dataclass(self) -> None:
        tx = _taxed_order(total_price="89.36", total_tax="7.38")
        f = retail_facts(tx)
        assert isinstance(f, RetailFacts)
        assert f.pretax == Decimal("81.98")
        assert f.is_wa is True
