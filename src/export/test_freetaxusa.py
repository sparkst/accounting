"""Tests for src/export/freetaxusa.py — REQ-21 (FreeTaxUSA export)."""

from __future__ import annotations

import csv
import io

from src.export.freetaxusa import (
    build_1099b_csv,
    build_schedule_a_summary,
    build_schedule_c_summary,
    generate_freetaxusa_export,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _tx(
    tax_category: str,
    amount: str,
    date: str = "2025-06-15",
    deductible_pct: str = "1.0",
    tax_subcategory: str = "",
    raw_data: dict | None = None,
) -> dict:
    return {
        "date": date,
        "description": f"Test {tax_category}",
        "amount": amount,
        "tax_category": tax_category,
        "deductible_pct": deductible_pct,
        "tax_subcategory": tax_subcategory,
        "raw_data": raw_data or {},
    }


SPARKRY_TRANSACTIONS = [
    _tx("CONSULTING_INCOME", "10000.00"),
    _tx("SUBSCRIPTION_INCOME", "500.00"),
    _tx("ADVERTISING", "-300.00"),
    _tx("MEALS", "-200.00", deductible_pct="0.5"),
    _tx("OFFICE_EXPENSE", "-150.00"),
    _tx("SUPPLIES", "-400.00"),
    _tx("REIMBURSABLE", "-100.00"),          # should be excluded
    _tx("PERSONAL_NON_DEDUCTIBLE", "-50.00"),  # should be excluded
]

PERSONAL_TRANSACTIONS = [
    _tx("CHARITABLE_CASH", "-500.00"),
    _tx("MORTGAGE_INTEREST", "-12000.00"),
    _tx("STATE_LOCAL_TAX", "-10000.00"),
    _tx("INVESTMENT_INCOME", "5000.00"),
    _tx("MEDICAL", "-2000.00"),
]

BROKERAGE_TRANSACTIONS = [
    {
        "date": "2025-04-10",
        "description": "AAPL sale",
        "amount": "3200.00",
        "tax_category": "INVESTMENT_INCOME",
        "deductible_pct": "1.0",
        "tax_subcategory": "capital_gain_long",
        "raw_data": {"proceeds": "3200.00", "cost_basis": "2800.00"},
    },
    {
        "date": "2025-09-22",
        "description": "MSFT sale",
        "amount": "1500.00",
        "tax_category": "INVESTMENT_INCOME",
        "deductible_pct": "1.0",
        "tax_subcategory": "capital_gain_short",
        "raw_data": {"proceeds": "1500.00", "cost_basis": "1600.00"},
    },
]


# ---------------------------------------------------------------------------
# Tests: build_schedule_c_summary
# ---------------------------------------------------------------------------


class TestBuildScheduleCSummary:
    def test_gross_income_included(self):
        out = build_schedule_c_summary(SPARKRY_TRANSACTIONS, "sparkry", 2025)
        assert "10,000.00" in out
        assert "500.00" in out

    def test_expenses_included(self):
        out = build_schedule_c_summary(SPARKRY_TRANSACTIONS, "sparkry", 2025)
        assert "300.00" in out  # advertising
        assert "150.00" in out  # office expense
        assert "400.00" in out  # supplies

    def test_meals_halved(self):
        """Meals should appear at 50% of raw amount (deductible_pct=0.5)."""
        out = build_schedule_c_summary(SPARKRY_TRANSACTIONS, "sparkry", 2025)
        # $200 * 0.5 = $100
        assert "100.00" in out

    def test_reimbursable_excluded(self):
        out = build_schedule_c_summary(SPARKRY_TRANSACTIONS, "sparkry", 2025)
        # Reimbursable line shows $100 but should not be counted in expenses
        # Net profit = (10000+500) - (300+100+150+400) = 9550
        assert "9,550.00" in out

    def test_irs_line_labels_present(self):
        out = build_schedule_c_summary(SPARKRY_TRANSACTIONS, "sparkry", 2025)
        assert "L8" in out   # Advertising
        assert "L24b" in out  # Meals
        assert "L18" in out  # Office

    def test_entity_and_year_in_header(self):
        out = build_schedule_c_summary(SPARKRY_TRANSACTIONS, "sparkry", 2025)
        assert "SPARKRY" in out
        assert "2025" in out

    def test_net_profit_line_present(self):
        out = build_schedule_c_summary(SPARKRY_TRANSACTIONS, "sparkry", 2025)
        assert "Net Profit" in out

    def test_empty_transactions(self):
        out = build_schedule_c_summary([], "sparkry", 2025)
        assert "SCHEDULE C" in out
        assert "Net Profit" in out


# ---------------------------------------------------------------------------
# Tests: build_schedule_a_summary
# ---------------------------------------------------------------------------


class TestBuildScheduleASummary:
    def test_charitable_included(self):
        out = build_schedule_a_summary(PERSONAL_TRANSACTIONS, 2025)
        assert "500.00" in out
        assert "Cash charitable" in out

    def test_mortgage_interest_included(self):
        out = build_schedule_a_summary(PERSONAL_TRANSACTIONS, 2025)
        assert "12,000.00" in out

    def test_salt_included(self):
        out = build_schedule_a_summary(PERSONAL_TRANSACTIONS, 2025)
        assert "10,000.00" in out
        assert "SALT" in out

    def test_year_in_header(self):
        out = build_schedule_a_summary(PERSONAL_TRANSACTIONS, 2025)
        assert "2025" in out

    def test_total_line(self):
        out = build_schedule_a_summary(PERSONAL_TRANSACTIONS, 2025)
        assert "Total Itemized Deductions" in out

    def test_empty_transactions(self):
        out = build_schedule_a_summary([], 2025)
        assert "SCHEDULE A" in out


# ---------------------------------------------------------------------------
# Tests: build_1099b_csv
# ---------------------------------------------------------------------------


class TestBuild1099BCSV:
    def test_header_row(self):
        out = build_1099b_csv(BROKERAGE_TRANSACTIONS)
        rows = list(csv.reader(io.StringIO(out)))
        assert rows[0] == [
            "date_sold",
            "description",
            "proceeds",
            "cost_basis",
            "gain_loss",
            "term",
        ]

    def test_two_data_rows(self):
        out = build_1099b_csv(BROKERAGE_TRANSACTIONS)
        rows = list(csv.reader(io.StringIO(out)))
        # Header + 2 data rows + possibly trailing blank
        data_rows = [r for r in rows[1:] if any(r)]
        assert len(data_rows) == 2

    def test_gain_loss_calculation(self):
        out = build_1099b_csv(BROKERAGE_TRANSACTIONS)
        rows = list(csv.reader(io.StringIO(out)))
        # AAPL: proceeds 3200 - cost_basis 2800 = 400 gain
        aapl_row = next(r for r in rows if "AAPL" in str(r))
        assert aapl_row[4] == "400.00"

    def test_loss_transaction(self):
        out = build_1099b_csv(BROKERAGE_TRANSACTIONS)
        rows = list(csv.reader(io.StringIO(out)))
        # MSFT: proceeds 1500 - cost_basis 1600 = -100 loss
        msft_row = next(r for r in rows if "MSFT" in str(r))
        assert msft_row[4] == "-100.00"

    def test_term_long_vs_short(self):
        out = build_1099b_csv(BROKERAGE_TRANSACTIONS)
        rows = list(csv.reader(io.StringIO(out)))
        aapl_row = next(r for r in rows if "AAPL" in str(r))
        msft_row = next(r for r in rows if "MSFT" in str(r))
        assert aapl_row[5] == "Long"
        assert msft_row[5] == "Short"

    def test_non_investment_excluded(self):
        """Non-investment transactions must not appear in 1099-B CSV."""
        txs = BROKERAGE_TRANSACTIONS + [_tx("ADVERTISING", "-100.00")]
        out = build_1099b_csv(txs)
        rows = [r for r in csv.reader(io.StringIO(out)) if any(r)]
        # Header + 2 brokerage rows only
        assert len(rows) == 3

    def test_empty_returns_header_only(self):
        out = build_1099b_csv([])
        rows = [r for r in csv.reader(io.StringIO(out)) if any(r)]
        assert len(rows) == 1  # just header


# ---------------------------------------------------------------------------
# Tests: generate_freetaxusa_export
# ---------------------------------------------------------------------------


class TestGenerateFreeTaxUSAExport:
    def test_sparkry_returns_txt_filename(self):
        _, filename = generate_freetaxusa_export(SPARKRY_TRANSACTIONS, "sparkry", 2025)
        assert filename == "freetaxusa_sparkry_2025.txt"

    def test_blackline_returns_txt_filename(self):
        _, filename = generate_freetaxusa_export([], "blackline", 2025)
        assert filename == "freetaxusa_blackline_2025.txt"

    def test_personal_returns_txt_filename(self):
        _, filename = generate_freetaxusa_export(
            PERSONAL_TRANSACTIONS + BROKERAGE_TRANSACTIONS, "personal", 2025
        )
        assert filename == "freetaxusa_personal_2025.txt"

    def test_personal_includes_1099b_section(self):
        content, _ = generate_freetaxusa_export(
            PERSONAL_TRANSACTIONS + BROKERAGE_TRANSACTIONS, "personal", 2025
        )
        assert "1099-B" in content
        assert "AAPL" in content

    def test_personal_without_brokerage_no_1099b_section(self):
        content, _ = generate_freetaxusa_export(PERSONAL_TRANSACTIONS, "personal", 2025)
        # No 1099-B data rows means section should be absent or header-only
        assert "AAPL" not in content

    def test_sparkry_content_is_schedule_c(self):
        content, _ = generate_freetaxusa_export(SPARKRY_TRANSACTIONS, "sparkry", 2025)
        assert "SCHEDULE C" in content

    def test_filename_convention(self):
        """Export filenames follow {format}_{entity}_{year} convention."""
        _, f1 = generate_freetaxusa_export([], "sparkry", 2024)
        _, f2 = generate_freetaxusa_export([], "personal", 2024)
        assert f1.startswith("freetaxusa_sparkry_")
        assert f2.startswith("freetaxusa_personal_")
