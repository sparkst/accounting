"""Tests for src/export/bno_tax.py — REQ-23 (B&O Tax Report)."""

from __future__ import annotations

import csv
import io
from decimal import Decimal

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
# Tests: confirmed out-of-state retail sales excluded from Retailing basis
# (REQ-FIX-TAX-002 / REQ-020) — parity with generate_dor_upload's wa_taxable.
# ---------------------------------------------------------------------------


class TestBlacklineRetailingExcludesConfirmedOutOfState:
    def _wa_and_oos_tx(self) -> list[dict]:
        wa_tx = {
            "date": "2025-01-15",
            "description": "WA retail order",
            "amount": "109.30",
            "tax_category": "SALES_INCOME",
            "deductible_pct": "1.0",
            "raw_data": {
                "total_price": "109.30",
                "total_tax": "9.30",
                "tax_lines": [{"title": "WA State Tax"}],
                "shipping_address": {"province_code": "WA"},
            },
        }
        oos_tx = {
            "date": "2025-01-20",
            "description": "Out-of-state retail order",
            "amount": "250.00",
            "tax_category": "SALES_INCOME",
            "deductible_pct": "1.0",
            "raw_data": {
                "total_price": "250.00",
                "total_tax": "0.00",
                "tax_lines": [],
                "shipping_address": {"province_code": "OR"},
            },
        }
        return [wa_tx, oos_tx]

    def test_confirmed_oos_sale_excluded_from_retailing_gross(self):
        rows = list(
            csv.reader(io.StringIO(build_blackline_bno_csv(self._wa_and_oos_tx(), 2025)))
        )
        q1_rows = [r for r in rows if "Q1" in str(r) and "Retailing" in str(r)]
        assert q1_rows, "Expected a Q1 Retailing row"
        # Only the WA order's pre-tax amount (109.30 - 9.30 = 100.00) counts —
        # the $250 confirmed-out-of-state order is excluded entirely,
        # mirroring compute_retail_detail's wa_taxable deduction.
        assert q1_rows[0][3] == "100.00"

    def test_estimated_bo_tax_matches_dor_wa_taxable_basis(self):
        from src.export.retail_sales_tax import compute_retail_detail

        transactions = self._wa_and_oos_tx()
        detail = compute_retail_detail(transactions, 2025, quarter=1)

        rows = list(
            csv.reader(io.StringIO(build_blackline_bno_csv(transactions, 2025)))
        )
        q1_rows = [r for r in rows if "Q1" in str(r) and "Retailing" in str(r)]
        assert Decimal(q1_rows[0][3]) == detail.wa_taxable == Decimal("100.00")

    def test_retailing_estimated_bo_tax_pinned(self):
        """REQ-FIX-TAX-002 / REQ-FIX-TAX-006: Retailing tax-owed cell (col 5)
        is pinned to prevent BO_RATE['Retailing'] drift from
        RETAILING_BO_RATE undetected. $90.70 wa_taxable * 0.00471 = $0.43."""
        rows = list(
            csv.reader(io.StringIO(build_blackline_bno_csv(self._wa_and_oos_tx(), 2025)))
        )
        q1_rows = [r for r in rows if "Q1" in str(r) and "Retailing" in str(r)]
        assert q1_rows, "Expected a Q1 Retailing row"
        # $100.00 wa_taxable * 0.00471 = $0.471 → $0.47
        assert q1_rows[0][5] == "0.47"


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


class TestGrandTotalsMatchSummedRows:
    """REQ-FIX-TAX-006: quantize before accumulate — the TOTAL row must equal
    the sum of the displayed (per-row-rounded) values, not a separately
    rounded sum of unquantized Decimals (which drift by a cent)."""

    # Engineered amounts: unquantized accumulation of amt*0.015 rounds to
    # 947.21 while summing the per-row-quantized tax values gives 947.22 —
    # this pins the old off-by-a-cent drift as a regression.
    _DRIFT_AMOUNTS = [
        "4452.40", "620.81", "8671.17", "5930.21", "1299.15", "9935.73",
        "2341.83", "6613.59", "6580.11", "6114.16", "9938.44", "649.67",
    ]

    def test_sparkry_monthly_total_equals_sum_of_rows(self) -> None:
        txs = [
            _income_tx("CONSULTING_INCOME", amt, f"2025-{m:02d}-15")
            for m, amt in enumerate(self._DRIFT_AMOUNTS, start=1)
        ]
        out = build_sparkry_bno_csv(txs, 2025)
        rows = list(csv.reader(io.StringIO(out)))
        data_rows = [r for r in rows[1:] if any(r) and "TOTAL" not in str(r)]
        total_row = next(r for r in rows if "TOTAL" in str(r))

        summed_tax = sum((Decimal(r[5]) for r in data_rows), Decimal("0"))
        summed_revenue = sum((Decimal(r[3]) for r in data_rows), Decimal("0"))

        assert Decimal(total_row[5]) == summed_tax
        assert Decimal(total_row[3]) == summed_revenue

    def test_blackline_quarterly_total_equals_sum_of_rows(self) -> None:
        txs = [
            _income_tx("SALES_INCOME", amt, f"2025-{m:02d}-15")
            for m, amt in enumerate(self._DRIFT_AMOUNTS, start=1)
        ]
        out = build_blackline_bno_csv(txs, 2025)
        rows = list(csv.reader(io.StringIO(out)))
        data_rows = [r for r in rows[1:] if any(r) and "TOTAL" not in str(r)]
        total_row = next(r for r in rows if "TOTAL" in str(r))

        summed_tax = sum((Decimal(r[5]) for r in data_rows), Decimal("0"))
        assert Decimal(total_row[5]) == summed_tax


class TestDorUploadHardFailOnUnmappedLocation:
    """REQ-FIX-TAX-007: generate_dor_upload must hard-fail rather than
    silently emit the sentinel '____' location code."""

    def _unmapped_wa_order(self) -> dict:
        return {
            "date": "2026-02-10",
            "amount": "100.00",
            "tax_category": "SALES_INCOME",
            "source": "shopify",
            "raw_data": {
                "total_price": "100.00",
                "total_tax": "9.30",
                "shipping_address": {"province_code": "WA", "city": "Nowhereville"},
                "tax_lines": [
                    {"title": "Washington State Tax"},
                    {"title": "Nowhereville City Tax"},
                ],
            },
        }

    def test_raises_value_error_naming_unmapped_locality(self) -> None:
        with pytest.raises(ValueError, match="____"):
            generate_dor_upload(
                [self._unmapped_wa_order()], "blackline", 2026, quarter=1
            )

    def test_mapped_only_input_still_emits_code_45_lines(self) -> None:
        """Regression: the hard-fail must not break the happy path."""
        content, _ = generate_dor_upload(
            [
                {
                    "date": "2026-01-15",
                    "amount": "100.00",
                    "tax_category": "SALES_INCOME",
                    "source": "shopify",
                    "raw_data": {
                        "total_price": "100.00",
                        "total_tax": "9.30",
                        "shipping_address": {"province_code": "WA", "city": "Sammamish"},
                        "tax_lines": [
                            {"title": "Washington State Tax"},
                            {"title": "Sammamish City Tax"},
                        ],
                    },
                }
            ],
            "blackline",
            2026,
            quarter=1,
        )
        assert any(line.startswith("TAX,45,1739,") for line in content.splitlines())

    def test_bellingham_is_mapped_not_hard_failed(self) -> None:
        """Issue #57: Bellingham orders hard-blocked Q3 BlackLine DOR upload
        because 'bellingham' was missing from WA_LOCATION_CODES."""
        content, _ = generate_dor_upload(
            [
                {
                    "date": "2026-07-15",
                    "amount": "100.00",
                    "tax_category": "SALES_INCOME",
                    "source": "shopify",
                    "raw_data": {
                        "total_price": "100.00",
                        "total_tax": "9.20",
                        "shipping_address": {"province_code": "WA", "city": "Bellingham"},
                        "tax_lines": [
                            {"title": "Washington State Tax"},
                            {"title": "Bellingham City Tax"},
                        ],
                    },
                }
            ],
            "blackline",
            2026,
            quarter=3,
        )
        assert any(line.startswith("TAX,45,3701,") for line in content.splitlines())


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


class TestDorUploadRetailSalesTax:
    """A WA retail order must emit pre-tax B&O + state + local sales-tax lines."""

    def _wa_order(self) -> dict:
        # $100 incl $9.30 WA tax → $90.70 pre-tax, Sammamish (1739).
        return {
            "date": "2026-01-15",
            "amount": "100.00",
            "tax_category": "SALES_INCOME",
            "source": "shopify",
            "raw_data": {
                "total_price": "100.00",
                "total_tax": "9.30",
                "shipping_address": {"province_code": "WA", "city": "Sammamish"},
                "tax_lines": [
                    {"title": "Washington State Tax"},
                    {"title": "Sammamish City Tax"},
                ],
            },
        }

    def test_retail_upload_has_pretax_bo_and_sales_tax_lines(self):
        content, _ = generate_dor_upload([self._wa_order()], "blackline", 2026, quarter=1)
        tax_lines = [r.split(",") for r in content.strip().split("\n") if r.startswith("TAX")]
        # B&O Retailing (code 2) on the PRE-TAX basis, not the $100 tax-inclusive.
        assert ["TAX", "2", "0", "90.70"] in tax_lines
        # State retail sales tax (code 1) on the taxable amount.
        assert ["TAX", "1", "0", "90.70"] in tax_lines
        # Local sales tax (code 45) at the Sammamish location code.
        assert ["TAX", "45", "1739", "90.70"] in tax_lines


# ---------------------------------------------------------------------------
# REQ-GMOBJ-04: needs_review income rows are not gross receipts
# ---------------------------------------------------------------------------


class TestNeedsReviewIncomeExcludedFromGross:
    """Issue #85 review round v2: the gmail income veto flips status to
    needs_review but leaves the income tax_category in place. B&O gross
    aggregation had no status filter, so a vetoed phantom income row was still
    counted as WA gross receipts (and taxed). Rows awaiting human review must
    not enter the B&O measure until they are resolved."""

    def _sparkry_gross(self, transactions: list[dict], year: int = 2025) -> Decimal:
        csv_text = build_sparkry_bno_csv(transactions, year)
        rows = list(csv.reader(io.StringIO(csv_text)))
        total = Decimal("0")
        for r in rows[1:]:
            if len(r) > 3 and r[0] and "TOTAL" not in r[0].upper():
                total += Decimal(r[3])
        return total

    def test_needs_review_income_row_is_not_counted_in_gross(self) -> None:
        confirmed = _income_tx("CONSULTING_INCOME", "10000.00", "2025-01-15")
        confirmed["status"] = "confirmed"
        needs_review = _income_tx("CONSULTING_INCOME", "10000.00", "2025-01-20")
        needs_review["status"] = "needs_review"

        assert self._sparkry_gross([confirmed, needs_review]) == Decimal("10000.00")

    def test_rows_without_a_status_key_are_still_counted(self) -> None:
        """Pure-function callers that pass status-less dicts must be unaffected."""
        assert self._sparkry_gross(
            [_income_tx("CONSULTING_INCOME", "10000.00", "2025-01-15")]
        ) == Decimal("10000.00")

    def test_blackline_quarterly_gross_also_skips_needs_review(self) -> None:
        confirmed = _income_tx("SALES_INCOME", "5000.00", "2025-01-20")
        confirmed["status"] = "confirmed"
        needs_review = _income_tx("SALES_INCOME", "5000.00", "2025-02-20")
        needs_review["status"] = "needs_review"

        csv_text = build_blackline_bno_csv([confirmed, needs_review], 2025)
        rows = list(csv.reader(io.StringIO(csv_text)))
        q1 = [r for r in rows[1:] if r and "Q1" in r[0]]
        assert sum(Decimal(r[3]) for r in q1) == Decimal("5000.00")
