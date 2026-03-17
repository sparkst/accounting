"""Tests for src/export/bno_tax.py — REQ-23 (B&O Tax Report)."""

from __future__ import annotations

import csv
import io

from src.export.bno_tax import (
    build_blackline_bno_csv,
    build_sparkry_bno_csv,
    generate_bno_export,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _income_tx(
    tax_category: str,
    amount: str,
    date: str,
) -> dict:
    return {
        "date": date,
        "description": f"Test {tax_category}",
        "amount": amount,
        "tax_category": tax_category,
        "deductible_pct": "1.0",
        "raw_data": {},
    }


SPARKRY_TRANSACTIONS = [
    _income_tx("CONSULTING_INCOME", "10000.00", "2025-01-15"),
    _income_tx("CONSULTING_INCOME", "10000.00", "2025-02-15"),
    _income_tx("SUBSCRIPTION_INCOME", "500.00", "2025-03-01"),
    _income_tx("CONSULTING_INCOME", "10000.00", "2025-04-15"),
    # expense — should be excluded
    {
        "date": "2025-01-10",
        "description": "Office supplies",
        "amount": "-200.00",
        "tax_category": "OFFICE_EXPENSE",
        "deductible_pct": "1.0",
        "raw_data": {},
    },
]

BLACKLINE_TRANSACTIONS = [
    _income_tx("SALES_INCOME", "5000.00", "2025-01-20"),
    _income_tx("SALES_INCOME", "6000.00", "2025-02-10"),
    _income_tx("SALES_INCOME", "4000.00", "2025-03-15"),
    _income_tx("CONSULTING_INCOME", "2000.00", "2025-04-05"),
    _income_tx("SALES_INCOME", "3000.00", "2025-07-08"),
]


# ---------------------------------------------------------------------------
# Tests: build_sparkry_bno_csv
# ---------------------------------------------------------------------------


class TestBuildSparkryBnoCsv:
    def _parse(self, transactions, year=2025):
        out = build_sparkry_bno_csv(transactions, year)
        return list(csv.reader(io.StringIO(out)))

    def test_header_row(self):
        rows = self._parse(SPARKRY_TRANSACTIONS)
        assert rows[0] == [
            "period",
            "bo_classification",
            "bo_code",
            "gross_revenue",
            "tax_rate",
            "estimated_bo_tax",
        ]

    def test_twelve_data_rows_plus_total(self):
        rows = self._parse(SPARKRY_TRANSACTIONS)
        data_rows = [r for r in rows[1:] if any(r)]
        # 12 months + 1 total row
        assert len(data_rows) == 13

    def test_january_revenue(self):
        rows = self._parse(SPARKRY_TRANSACTIONS)
        jan_row = next(r for r in rows if "January" in str(r))
        # Jan: $10,000 consulting income
        assert jan_row[3] == "10000.00"

    def test_march_includes_subscription(self):
        rows = self._parse(SPARKRY_TRANSACTIONS)
        mar_row = next(r for r in rows if "March" in str(r))
        assert mar_row[3] == "500.00"

    def test_total_row(self):
        rows = self._parse(SPARKRY_TRANSACTIONS)
        total_row = next(r for r in rows if "TOTAL" in str(r))
        # 10000 + 10000 + 500 + 10000 = 30500
        assert total_row[3] == "30500.00"

    def test_expense_excluded(self):
        """OFFICE_EXPENSE must not appear in revenue totals."""
        rows = self._parse(SPARKRY_TRANSACTIONS)
        total_row = next(r for r in rows if "TOTAL" in str(r))
        # No $200 office expense inflating totals
        assert total_row[3] == "30500.00"

    def test_zero_months_show_zero_revenue(self):
        rows = self._parse(SPARKRY_TRANSACTIONS)
        # May through December have no revenue
        may_row = next(r for r in rows if "May" in str(r))
        assert may_row[3] == "0.00"

    def test_estimated_tax_calculated(self):
        rows = self._parse(SPARKRY_TRANSACTIONS)
        jan_row = next(r for r in rows if "January" in str(r))
        # $10,000 * 1.5% = $150.00
        assert jan_row[5] == "150.00"

    def test_tax_rate_format(self):
        rows = self._parse(SPARKRY_TRANSACTIONS)
        jan_row = next(r for r in rows if "January" in str(r))
        assert "%" in jan_row[4]

    def test_wrong_year_excluded(self):
        txs = [_income_tx("CONSULTING_INCOME", "9999.00", "2024-06-01")]
        out = build_sparkry_bno_csv(txs, 2025)
        rows = list(csv.reader(io.StringIO(out)))
        total_row = next(r for r in rows if "TOTAL" in str(r))
        assert total_row[3] == "0.00"

    def test_filename_convention(self):
        _, filename = generate_bno_export([], "sparkry", 2025)
        assert filename == "bno_sparkry_2025.csv"


# ---------------------------------------------------------------------------
# Tests: build_blackline_bno_csv
# ---------------------------------------------------------------------------


class TestBuildBlacklineBnoCsv:
    def _parse(self, transactions, year=2025):
        out = build_blackline_bno_csv(transactions, year)
        return list(csv.reader(io.StringIO(out)))

    def test_header_row(self):
        rows = self._parse(BLACKLINE_TRANSACTIONS)
        assert rows[0][0] == "period"
        assert rows[0][1] == "bo_classification"

    def test_four_quarters_present(self):
        rows = self._parse(BLACKLINE_TRANSACTIONS)
        data_rows = [r for r in rows[1:] if any(r) and "TOTAL" not in str(r)]
        periods = {r[0] for r in data_rows}
        assert any("Q1" in p for p in periods)
        assert any("Q2" in p for p in periods)
        assert any("Q3" in p for p in periods)
        assert any("Q4" in p for p in periods)

    def test_q1_sales_total(self):
        rows = self._parse(BLACKLINE_TRANSACTIONS)
        # Q1: Jan $5000 + Feb $6000 + Mar $4000 = $15000 SALES_INCOME
        q1_rows = [r for r in rows if "Q1" in str(r) and "Retailing" in str(r)]
        assert q1_rows, "Expected a Q1 Retailing row"
        assert q1_rows[0][3] == "15000.00"

    def test_q2_consulting_income(self):
        rows = self._parse(BLACKLINE_TRANSACTIONS)
        q2_service_rows = [
            r for r in rows if "Q2" in str(r) and "ServiceOther" in str(r)
        ]
        assert q2_service_rows
        assert q2_service_rows[0][3] == "2000.00"

    def test_q3_sales(self):
        rows = self._parse(BLACKLINE_TRANSACTIONS)
        q3_rows = [r for r in rows if "Q3" in str(r) and "Retailing" in str(r)]
        assert q3_rows
        assert q3_rows[0][3] == "3000.00"

    def test_total_row_present(self):
        rows = self._parse(BLACKLINE_TRANSACTIONS)
        total_row = next((r for r in rows if "TOTAL" in str(r)), None)
        assert total_row is not None
        # 5000+6000+4000+2000+3000 = 20000
        assert total_row[3] == "20000.00"

    def test_empty_returns_four_quarter_rows(self):
        rows = self._parse([])
        data_rows = [r for r in rows[1:] if any(r) and "TOTAL" not in str(r)]
        assert len(data_rows) == 4

    def test_filename_convention(self):
        _, filename = generate_bno_export([], "blackline", 2025)
        assert filename == "bno_blackline_2025.csv"


# ---------------------------------------------------------------------------
# Tests: generate_bno_export
# ---------------------------------------------------------------------------


class TestGenerateBnoExport:
    def test_sparkry_monthly(self):
        content, filename = generate_bno_export(SPARKRY_TRANSACTIONS, "sparkry", 2025)
        rows = list(csv.reader(io.StringIO(content)))
        data_rows = [r for r in rows[1:] if any(r)]
        assert len(data_rows) == 13  # 12 months + total

    def test_blackline_quarterly(self):
        content, filename = generate_bno_export(
            BLACKLINE_TRANSACTIONS, "blackline", 2025
        )
        rows = list(csv.reader(io.StringIO(content)))
        data_rows = [r for r in rows[1:] if any(r) and "TOTAL" not in str(r)]
        # Should have rows for each quarter × classification code
        assert len(data_rows) >= 4

    def test_sparkry_filename(self):
        _, filename = generate_bno_export([], "sparkry", 2025)
        assert filename == "bno_sparkry_2025.csv"

    def test_blackline_filename(self):
        _, filename = generate_bno_export([], "blackline", 2025)
        assert filename == "bno_blackline_2025.csv"
