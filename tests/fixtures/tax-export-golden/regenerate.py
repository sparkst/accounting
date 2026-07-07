#!/usr/bin/env python3
"""Regenerate/print the expected cross-surface figures for the tax-export
golden fixture (transactions.json).

REQ-FIX-TAX-002..007: this is the "regenerable via a checked-in script" half
of the golden-fixture contract described in
docs/superpowers/specs/2026-07-07-tax-invoicing-correctness-design.md §13.
Run after any *intentional* change to the export math and diff the printed
values like code — src/export/test_tax_export_golden.py asserts the same
values programmatically (cross-surface agreement + specific pinned figures),
so a silent behavior change fails CI without needing to eyeball this output.

Usage: python tests/fixtures/tax-export-golden/regenerate.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = FIXTURE_DIR.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.export.bno_tax import generate_bno_export  # noqa: E402
from src.export.freetaxusa import build_1099b_csv, build_form_1065_summary  # noqa: E402
from src.export.retail_sales_tax import compute_retail_detail  # noqa: E402
from src.export.taxact import build_form_1065_summary as taxact_1065  # noqa: E402

ACTIVE_STATUSES = {"confirmed", "auto_classified", "needs_review"}


def _active(transactions: list[dict]) -> list[dict]:
    """Mirror src/api/routes/tax_export.py::_fetch_transactions filtering:
    excludes rejected and split_parent rows."""
    return [tx for tx in transactions if tx.get("status") in ACTIVE_STATUSES]


def main() -> None:
    data = json.loads((FIXTURE_DIR / "transactions.json").read_text())
    year = data["year"]
    quarter = data["quarter"]
    txs = _active(data["transactions"])

    detail = compute_retail_detail(txs, year, quarter=quarter)
    print(f"DOR gross_retailing (pretax, all destinations): {detail.gross_retailing}")
    print(f"DOR wa_taxable: {detail.wa_taxable}")

    bno_content, _ = generate_bno_export(txs, "blackline", year)
    print("\n--- BlackLine B&O CSV ---")
    print(bno_content)

    freetaxusa_1065 = build_form_1065_summary(txs, year)
    print("\n--- FreeTaxUSA Form 1065 ---")
    print(freetaxusa_1065)

    taxact_out = taxact_1065(txs, year)
    print("\n--- TaxAct Form 1065 ---")
    print(taxact_out)

    csv_1099b = build_1099b_csv(txs)
    print("\n--- 1099-B CSV ---")
    print(csv_1099b)


if __name__ == "__main__":
    main()
