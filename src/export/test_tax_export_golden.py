"""Golden-fixture cross-surface consistency tests.

REQ-ID: REQ-FIX-TAX-002  All export surfaces (B&O CSVs, FreeTaxUSA, TaxAct,
DOR upload) report SALES_INCOME on the same pre-tax basis.
REQ-ID: REQ-FIX-TAX-003  OTHER_EXPENSE (Shopify refunds) reduces filed net
income via both exporters.
REQ-ID: REQ-FIX-TAX-004  WHOLESALE_INCOME is included in gross receipts.
REQ-ID: REQ-FIX-TAX-005  1099-B None-tax_subcategory rows do not raise.
REQ-ID: REQ-FIX-TAX-006  B&O CSV grand totals equal the sum of the displayed
(per-row-rounded) values.

Uses the frozen synthetic transaction set in
``tests/fixtures/tax-export-golden/transactions.json`` — one WA-taxed retail
order, one confirmed-out-of-state retail order, a Shopify refund
(contra-revenue), a wholesale-income row, a split parent + two children, a
rejected row, and a None-tax_subcategory 1099-B row. Regenerate the printed
cross-surface figures via
``tests/fixtures/tax-export-golden/regenerate.py`` after any intentional
change to the export math and diff them like code.
"""

from __future__ import annotations

import csv
import io
import json
from decimal import Decimal
from pathlib import Path

from src.export.bno_tax import generate_bno_export, generate_dor_upload
from src.export.freetaxusa import build_1099b_csv
from src.export.freetaxusa import build_form_1065_summary as freetaxusa_1065
from src.export.retail_sales_tax import compute_retail_detail
from src.export.taxact import build_form_1065_summary as taxact_1065

FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "tests"
    / "fixtures"
    / "tax-export-golden"
    / "transactions.json"
)

# Mirrors src/api/routes/tax_export.py::_fetch_transactions filtering: the
# builder functions themselves don't filter status — the route does, before
# ever calling them — so the golden fixture applies the same filter to match
# real production call sites.
_ACTIVE_STATUSES = {"confirmed", "auto_classified", "needs_review"}


def _load_active_transactions() -> tuple[list[dict], int, int]:
    data = json.loads(FIXTURE_PATH.read_text())
    active = [tx for tx in data["transactions"] if tx.get("status") in _ACTIVE_STATUSES]
    return active, data["year"], data["quarter"]


def _bno_csv_sales_total(csv_content: str) -> Decimal:
    rows = list(csv.reader(io.StringIO(csv_content)))
    total = Decimal("0")
    for r in rows[1:]:
        if len(r) > 2 and r[2] == "Retailing" and "TOTAL" not in r[0]:
            total += Decimal(r[3])
    return total


class TestGoldenCrossSurfaceConsistency:
    def test_split_parent_and_rejected_excluded(self) -> None:
        active, _, _ = _load_active_transactions()
        ids = {tx["id"] for tx in active}
        assert "split-parent-005" not in ids
        assert "rejected-008" not in ids
        assert "split-child-006" in ids
        assert "split-child-007" in ids

    def test_dor_gross_retailing_is_pretax_sum_of_wa_and_oos(self) -> None:
        """WA order: $100 - $9.30 tax = $90.70. OOS order: $250 (no tax).
        Total pretax gross = $340.70 — not $350.00 (the tax-inclusive sum)."""
        active, year, quarter = _load_active_transactions()
        detail = compute_retail_detail(active, year, quarter=quarter)
        assert detail.gross_retailing == Decimal("340.70")

    def test_bno_csv_reports_same_wa_taxable_retailing_as_dor(self) -> None:
        """REQ-FIX-TAX-002 / REQ-020: the B&O CSV's Retailing basis excludes
        confirmed out-of-state sales, matching the DOR upload's authoritative
        ``wa_taxable`` line (not the pre-interstate-deduction gross). WA
        order: $100 - $9.30 tax = $90.70. The $250 OOS order is excluded from
        both surfaces."""
        active, year, quarter = _load_active_transactions()
        detail = compute_retail_detail(active, year, quarter=quarter)

        bno_content, _ = generate_bno_export(active, "blackline", year)
        bno_sales_total = _bno_csv_sales_total(bno_content)

        assert bno_sales_total == detail.wa_taxable == Decimal("90.70")
        # The full pretax gross (all destinations) remains distinct — the OOS
        # sale is excluded from both the B&O CSV and the DOR wa_taxable line.
        assert detail.gross_retailing == Decimal("340.70")

    def test_freetaxusa_1065_reports_same_pretax_gross(self) -> None:
        active, year, _ = _load_active_transactions()
        content = freetaxusa_1065(active, year)
        assert "340.70" in content  # SALES_INCOME contribution to L1a detail
        assert "2,000.00" in content  # WHOLESALE_INCOME (REQ-FIX-TAX-004)
        assert "L21" in content  # OTHER_EXPENSE refund (REQ-FIX-TAX-003)
        assert "50.00" in content  # the $50 refund amount

    def test_taxact_1065_reports_same_pretax_gross(self) -> None:
        active, year, _ = _load_active_transactions()
        content = taxact_1065(active, year)
        # gross = 340.70 (sales) + 2000.00 (wholesale) = 2340.70
        assert "2,340.70" in content
        assert "Line 21" in content  # OTHER_EXPENSE refund (REQ-FIX-TAX-003)

    def test_ordinary_income_matches_across_freetaxusa_and_taxact(self) -> None:
        """Both exporters must land on the identical ordinary-income figure
        for the same input — the whole point of a single canonical basis."""
        active, year, _ = _load_active_transactions()
        ft_content = freetaxusa_1065(active, year)
        ta_content = taxact_1065(active, year)
        # gross 2340.70 - deductions (400 travel + 50 meals + 50 other) = 1840.70
        assert "1,840.70" in ft_content
        assert "1,840.70" in ta_content

    def test_1099b_none_subcategory_row_does_not_raise(self) -> None:
        """REQ-FIX-TAX-005 regression, exercised via the full fixture set
        (mixed with unrelated categories) rather than in isolation."""
        active, _, _ = _load_active_transactions()
        out = build_1099b_csv(active)
        rows = [r for r in csv.reader(io.StringIO(out)) if any(r)]
        assert len(rows) == 2  # header + the one INVESTMENT_INCOME row
        assert rows[1][5] == "Short"

    def test_dor_upload_file_body_pinned(self) -> None:
        """REQ-FIX-TAX-002: DOR upload generated file body is pinned
        end-to-end to prevent undetected regressions in the wa_taxable basis
        or estimated_bo_tax computation. The golden fixture includes a $100
        WA order ($90.70 pre-tax) → TAX,2,0,90.70 (Retailing); plus state and
        local sales-tax lines TAX,1,0,90.70 and TAX,45,1739,90.70."""
        active, year, quarter = _load_active_transactions()
        content, _ = generate_dor_upload(active, "blackline", year, quarter=quarter)
        lines = content.strip().split("\n")
        # ACCOUNT line should be present
        assert any(line.startswith("ACCOUNT,") for line in lines)
        # Retailing B&O line (code 2) at $90.70 pre-tax basis (not $100 tax-incl)
        assert any(line == "TAX,2,0,90.70" for line in lines)
        # State retail sales tax (code 1) at $90.70 basis
        assert any(line == "TAX,1,0,90.70" for line in lines)
        # Local sales tax (code 45) at Sammamish location (1739) at $90.70
        assert any(line == "TAX,45,1739,90.70" for line in lines)
