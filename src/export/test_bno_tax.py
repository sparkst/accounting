"""Tests for src/export/bno_tax.py — REQ-23 (B&O Tax Report)."""

from __future__ import annotations

import csv
import io

import pytest

from src.export.bno_tax import (
    DOR_LINE_CODES,
    build_blackline_bno_csv,
    build_sparkry_bno_csv,
    generate_bno_export,
    generate_dor_upload,
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


# ---------------------------------------------------------------------------
# Tests: generate_dor_upload (WA DOR My DOR data-upload file)
# ---------------------------------------------------------------------------


class TestDorUploadLineCodes:
    """Line codes must match the WA Combined Excise Tax Return 'Code' column."""

    def test_service_other_is_code_40(self):
        # Service & Other Activities (<$1M) = code 40, NOT 7 (07 = Manufacturing).
        assert DOR_LINE_CODES["ServiceOther"] == 40

    def test_retailing_is_code_2(self):
        assert DOR_LINE_CODES["Retailing"] == 2

    def test_wholesaling_is_code_3(self):
        assert DOR_LINE_CODES["Wholesaling"] == 3


class TestDorUploadMonthly:
    def test_monthly_period_is_mmyyyy(self):
        # Sparkry Feb 2025: one consulting charge → Service & Other (code 40).
        content, filename = generate_dor_upload(
            SPARKRY_TRANSACTIONS, "sparkry", 2025, month=2
        )
        rows = [r.split(",") for r in content.strip().split("\n")]
        account = rows[0]
        assert account[0] == "ACCOUNT"
        assert account[1] == "605965107"  # dashes removed
        assert account[2] == "022025"  # MMYYYY
        tax = rows[1]
        assert tax[0] == "TAX"
        assert tax[1] == "40"  # Service & Other, not 7
        assert tax[2] == "0"  # B&O location code
        assert tax[3] == "10000.00"

    def test_monthly_filename(self):
        _, filename = generate_dor_upload([], "sparkry", 2026, month=4)
        assert filename == "dor_upload_sparkry_2026_04.csv"

    def test_empty_month_emits_zero_service_line_for_sparkry(self):
        content, _ = generate_dor_upload([], "sparkry", 2026, month=4)
        tax = content.strip().split("\n")[1].split(",")
        assert tax == ["TAX", "40", "0", "0.00"]


class TestDorUploadQuarterly:
    def test_quarterly_period_is_qnyyyy(self):
        # BlackLine Q1 2025: SALES_INCOME 5000+6000+4000 = 15000 → Retailing (2).
        content, filename = generate_dor_upload(
            BLACKLINE_TRANSACTIONS, "blackline", 2025, quarter=1
        )
        rows = [r.split(",") for r in content.strip().split("\n")]
        assert rows[0][1] == "605922410"
        assert rows[0][2] == "Q12025"  # Q#YYYY
        tax = rows[1]
        assert tax[0] == "TAX"
        assert tax[1] == "2"  # Retailing
        assert tax[3] == "15000.00"  # quarter sum
        assert filename == "dor_upload_blackline_2025_Q1.csv"

    def test_empty_quarter_emits_zero_retailing_line_for_blackline(self):
        content, _ = generate_dor_upload([], "blackline", 2026, quarter=2)
        tax = content.strip().split("\n")[1].split(",")
        assert tax == ["TAX", "2", "0", "0.00"]

    def test_requires_exactly_one_of_month_or_quarter(self):
        with pytest.raises(ValueError):
            generate_dor_upload([], "sparkry", 2026)
        with pytest.raises(ValueError):
            generate_dor_upload([], "sparkry", 2026, month=1, quarter=1)
