"""Tests for src/export/taxact.py — REQ-22 (TaxAct export)."""

from __future__ import annotations

from src.export.taxact import (
    build_form_1065_summary,
    build_schedule_c_summary_taxact,
    generate_taxact_export,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _tx(
    tax_category: str,
    amount: str,
    deductible_pct: str = "1.0",
) -> dict:
    return {
        "date": "2025-06-15",
        "description": f"Test {tax_category}",
        "amount": amount,
        "tax_category": tax_category,
        "deductible_pct": deductible_pct,
        "raw_data": {},
    }


BLACKLINE_TRANSACTIONS = [
    _tx("SALES_INCOME", "25000.00"),
    _tx("CONSULTING_INCOME", "5000.00"),
    _tx("COGS", "-8000.00"),
    _tx("ADVERTISING", "-1200.00"),
    _tx("INSURANCE", "-900.00"),
    _tx("TRAVEL", "-600.00"),
    _tx("MEALS", "-400.00", deductible_pct="0.5"),
    _tx("SUPPLIES", "-300.00"),
    _tx("REIMBURSABLE", "-100.00"),           # excluded
    _tx("CHARITABLE_CASH", "-200.00"),         # excluded from 1065
]

SPARKRY_TRANSACTIONS = [
    _tx("CONSULTING_INCOME", "12000.00"),
    _tx("ADVERTISING", "-500.00"),
    _tx("LEGAL_AND_PROFESSIONAL", "-800.00"),
    _tx("MEALS", "-300.00", deductible_pct="0.5"),
]


# ---------------------------------------------------------------------------
# Tests: build_form_1065_summary
# ---------------------------------------------------------------------------


class TestBuildForm1065Summary:
    def test_header_present(self):
        out = build_form_1065_summary(BLACKLINE_TRANSACTIONS, 2025)
        assert "FORM 1065" in out
        assert "BLACKLINE MTB LLC" in out
        assert "2025" in out

    def test_gross_receipts_line(self):
        out = build_form_1065_summary(BLACKLINE_TRANSACTIONS, 2025)
        # SALES_INCOME 25000 + CONSULTING_INCOME 5000 = 30000
        assert "30,000.00" in out
        assert "Line 1a" in out

    def test_cogs_line(self):
        out = build_form_1065_summary(BLACKLINE_TRANSACTIONS, 2025)
        assert "8,000.00" in out
        assert "Line 2" in out

    def test_gross_profit_line(self):
        out = build_form_1065_summary(BLACKLINE_TRANSACTIONS, 2025)
        # Gross profit = 30000 - 8000 = 22000
        assert "22,000.00" in out
        assert "Line 3" in out

    def test_deductions_section(self):
        out = build_form_1065_summary(BLACKLINE_TRANSACTIONS, 2025)
        assert "DEDUCTIONS" in out
        assert "1,200.00" in out  # advertising
        assert "900.00" in out    # insurance

    def test_meals_halved(self):
        """Meals 50% rule applied before display."""
        out = build_form_1065_summary(BLACKLINE_TRANSACTIONS, 2025)
        # $400 * 0.5 = $200
        assert "200.00" in out

    def test_reimbursable_excluded(self):
        out = build_form_1065_summary(BLACKLINE_TRANSACTIONS, 2025)
        # $100 reimbursable should not appear in totals
        # Total deductions = 1200 + 900 + 600 + 200 + 300 = 3200
        assert "3,200.00" in out

    def test_ordinary_income_line(self):
        out = build_form_1065_summary(BLACKLINE_TRANSACTIONS, 2025)
        # ordinary income = 22000 - 3200 = 18800
        assert "18,800.00" in out
        assert "Ordinary business income" in out

    def test_personal_categories_excluded(self):
        """Charitable/personal categories must not appear in 1065."""
        out = build_form_1065_summary(BLACKLINE_TRANSACTIONS, 2025)
        # Charitable $200 should not inflate deductions
        assert "Charitable" not in out

    def test_k1_note_present(self):
        out = build_form_1065_summary(BLACKLINE_TRANSACTIONS, 2025)
        assert "K-1" in out

    def test_empty_transactions(self):
        out = build_form_1065_summary([], 2025)
        assert "FORM 1065" in out
        assert "0.00" in out


# ---------------------------------------------------------------------------
# Tests: build_schedule_c_summary_taxact
# ---------------------------------------------------------------------------


class TestBuildScheduleCSummaryTaxAct:
    def test_header_present(self):
        out = build_schedule_c_summary_taxact(SPARKRY_TRANSACTIONS, "sparkry", 2025)
        assert "SCHEDULE C" in out
        assert "TaxAct" in out
        assert "SPARKRY" in out

    def test_income_included(self):
        out = build_schedule_c_summary_taxact(SPARKRY_TRANSACTIONS, "sparkry", 2025)
        assert "12,000.00" in out

    def test_advertising_expense(self):
        out = build_schedule_c_summary_taxact(SPARKRY_TRANSACTIONS, "sparkry", 2025)
        assert "500.00" in out

    def test_legal_professional_expense(self):
        out = build_schedule_c_summary_taxact(SPARKRY_TRANSACTIONS, "sparkry", 2025)
        assert "800.00" in out

    def test_meals_halved(self):
        out = build_schedule_c_summary_taxact(SPARKRY_TRANSACTIONS, "sparkry", 2025)
        # $300 * 0.5 = $150
        assert "150.00" in out

    def test_irs_line_numbers_present(self):
        out = build_schedule_c_summary_taxact(SPARKRY_TRANSACTIONS, "sparkry", 2025)
        assert "L8" in out    # Advertising
        assert "L24b" in out  # Meals

    def test_net_profit(self):
        out = build_schedule_c_summary_taxact(SPARKRY_TRANSACTIONS, "sparkry", 2025)
        # income 12000 - (500 + 800 + 150) = 10550
        assert "10,550.00" in out


# ---------------------------------------------------------------------------
# Tests: generate_taxact_export
# ---------------------------------------------------------------------------


class TestGenerateTaxActExport:
    def test_blackline_filename(self):
        _, filename = generate_taxact_export(BLACKLINE_TRANSACTIONS, "blackline", 2025)
        assert filename == "taxact_blackline_2025.txt"

    def test_sparkry_filename(self):
        _, filename = generate_taxact_export(SPARKRY_TRANSACTIONS, "sparkry", 2025)
        assert filename == "taxact_sparkry_2025.txt"

    def test_blackline_uses_1065(self):
        content, _ = generate_taxact_export(BLACKLINE_TRANSACTIONS, "blackline", 2025)
        assert "FORM 1065" in content

    def test_sparkry_uses_schedule_c(self):
        content, _ = generate_taxact_export(SPARKRY_TRANSACTIONS, "sparkry", 2025)
        assert "SCHEDULE C" in content

    def test_filename_convention(self):
        """Export filenames follow {format}_{entity}_{year} convention."""
        _, f = generate_taxact_export([], "blackline", 2024)
        assert f.startswith("taxact_") and f.endswith("_2024.txt")
