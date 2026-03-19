"""FreeTaxUSA export module.

Produces two outputs:
  1. 1099-B CSV for brokerage transactions (INVESTMENT_INCOME category).
  2. A print-friendly text summary of Schedule C lines (Sparkry / BlackLine)
     and Schedule A deductions (Personal) aligned to IRS line numbers.

All public functions are pure: they accept a list of transaction dicts (or
dataclasses) and return formatted strings — no I/O side-effects.
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal
from typing import Any

# ---------------------------------------------------------------------------
# IRS Schedule C line mapping (Tax Category → line label)
# ---------------------------------------------------------------------------

SCHEDULE_C_LINES: dict[str, tuple[str, str]] = {
    # category: (irs_line, label)
    "ADVERTISING": ("L8", "Advertising"),
    "CAR_AND_TRUCK": ("L9", "Car and truck expenses"),
    "CONTRACT_LABOR": ("L11", "Contract labor"),
    "INSURANCE": ("L15", "Insurance (other than health)"),
    "LEGAL_AND_PROFESSIONAL": ("L17", "Legal and professional services"),
    "OFFICE_EXPENSE": ("L18", "Office expense"),
    "SUPPLIES": ("L22", "Supplies"),
    "TAXES_AND_LICENSES": ("L23", "Taxes and licenses"),
    "TRAVEL": ("L24a", "Travel"),
    "MEALS": ("L24b", "Meals (50% deductible)"),
    "COGS": ("Part III", "Cost of goods sold"),
    "CONSULTING_INCOME": ("Gross receipts", "Consulting income"),
    "SUBSCRIPTION_INCOME": ("Gross receipts", "Subscription income"),
    "SALES_INCOME": ("Gross receipts", "Sales income"),
}

# Schedule A personal deduction labels
SCHEDULE_A_LINES: dict[str, str] = {
    "CHARITABLE_CASH": "Cash charitable contributions",
    "CHARITABLE_STOCK": "Non-cash charitable contributions (stock)",
    "MEDICAL": "Medical and dental expenses",
    "STATE_LOCAL_TAX": "State and local taxes (SALT, $10k cap)",
    "MORTGAGE_INTEREST": "Home mortgage interest",
    "INVESTMENT_INCOME": "Investment income (Schedule D / 8949)",
}

# Income categories
INCOME_CATEGORIES = {"CONSULTING_INCOME", "SUBSCRIPTION_INCOME", "SALES_INCOME"}


def _to_decimal(value: Any) -> Decimal:
    """Safely convert a value to Decimal, returning Decimal(0) on failure."""
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def _abs_amount(tx: dict[str, Any]) -> Decimal:
    """Return the absolute deductible amount for a transaction.

    Applies deductible_pct (e.g. 0.5 for meals).
    """
    raw = _to_decimal(tx.get("amount"))
    pct = _to_decimal(tx.get("deductible_pct", "1.0"))
    return abs(raw) * pct


def build_1099b_csv(transactions: list[dict[str, Any]]) -> str:
    """Build a 1099-B CSV for brokerage transactions.

    Only processes transactions with tax_category == INVESTMENT_INCOME.
    Columns match what FreeTaxUSA accepts for manual 1099-B entry guidance:
      date_sold, description, proceeds, cost_basis, gain_loss, term
    """
    brokerage = [
        tx for tx in transactions if tx.get("tax_category") == "INVESTMENT_INCOME"
    ]

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        ["date_sold", "description", "proceeds", "cost_basis", "gain_loss", "term"]
    )

    for tx in brokerage:
        amount = _to_decimal(tx.get("amount"))
        raw = tx.get("raw_data") or {}
        proceeds = _to_decimal(raw.get("proceeds", amount if amount > 0 else 0))
        cost_basis = _to_decimal(raw.get("cost_basis", 0))
        gain_loss = proceeds - cost_basis

        # Determine short vs long term from subcategory
        subcategory = tx.get("tax_subcategory", "")
        term = "Long" if "long" in subcategory else "Short"

        writer.writerow(
            [
                tx.get("date", ""),
                tx.get("description", ""),
                f"{proceeds:.2f}",
                f"{cost_basis:.2f}",
                f"{gain_loss:.2f}",
                term,
            ]
        )

    return output.getvalue()


def build_schedule_c_summary(
    transactions: list[dict[str, Any]],
    entity: str,
    year: int,
) -> str:
    """Build a print-friendly Schedule C summary aligned to IRS line numbers.

    Returns a plain-text string suitable for manual entry in FreeTaxUSA.
    Skips REIMBURSABLE and PERSONAL_NON_DEDUCTIBLE categories.
    """
    # Aggregate totals per tax_category
    totals: dict[str, Decimal] = {}
    for tx in transactions:
        cat = tx.get("tax_category", "")
        if not cat or cat in ("REIMBURSABLE", "PERSONAL_NON_DEDUCTIBLE"):
            continue
        if cat not in SCHEDULE_C_LINES:
            continue
        amt = _abs_amount(tx)
        totals[cat] = totals.get(cat, Decimal("0")) + amt

    # Split into income and expense buckets
    gross_income = Decimal("0")
    income_lines: list[tuple[str, str, Decimal]] = []
    expense_lines: list[tuple[str, str, Decimal]] = []

    for cat, total in sorted(totals.items(), key=lambda x: SCHEDULE_C_LINES[x[0]][0]):
        line, label = SCHEDULE_C_LINES[cat]
        if cat in INCOME_CATEGORIES:
            gross_income += total
            income_lines.append((line, label, total))
        else:
            expense_lines.append((line, label, total))

    total_expenses = sum(amt for _, _, amt in expense_lines)
    net_profit = gross_income - total_expenses

    # ── Format output ──────────────────────────────────────────────────────────
    lines: list[str] = []
    lines.append("SCHEDULE C — PROFIT OR LOSS FROM BUSINESS")
    lines.append(f"Entity: {entity.upper()}  |  Tax Year: {year}")
    lines.append("=" * 60)

    lines.append("")
    lines.append("INCOME")
    lines.append("-" * 60)
    for line, label, amt in income_lines:
        lines.append(f"  {line:<12} {label:<38} ${amt:>10,.2f}")
    lines.append(f"  {'':12} {'Gross Income':<38} ${gross_income:>10,.2f}")

    lines.append("")
    lines.append("EXPENSES")
    lines.append("-" * 60)
    for line, label, amt in expense_lines:
        lines.append(f"  {line:<12} {label:<38} ${amt:>10,.2f}")
    lines.append(f"  {'':12} {'Total Expenses':<38} ${total_expenses:>10,.2f}")

    lines.append("")
    lines.append("=" * 60)
    lines.append(f"  {'':12} {'Net Profit (Loss)':<38} ${net_profit:>10,.2f}")
    lines.append("")
    lines.append("NOTE: Enter these amounts in FreeTaxUSA under Business Income.")
    lines.append("Meals (L24b) are already reduced to 50% deductible amount above.")

    return "\n".join(lines)


def build_schedule_a_summary(
    transactions: list[dict[str, Any]],
    year: int,
) -> str:
    """Build a print-friendly Schedule A (itemized deductions) summary.

    For use with the Personal entity in FreeTaxUSA.
    """
    totals: dict[str, Decimal] = {}
    for tx in transactions:
        cat = tx.get("tax_category", "")
        if cat not in SCHEDULE_A_LINES:
            continue
        amt = _abs_amount(tx)
        totals[cat] = totals.get(cat, Decimal("0")) + amt

    lines: list[str] = []
    lines.append("SCHEDULE A — ITEMIZED DEDUCTIONS")
    lines.append(f"Entity: PERSONAL  |  Tax Year: {year}")
    lines.append("=" * 60)
    lines.append("")

    total = Decimal("0")
    for cat, label in SCHEDULE_A_LINES.items():
        amt = totals.get(cat, Decimal("0"))
        if amt:
            lines.append(f"  {label:<42} ${amt:>10,.2f}")
            total += amt

    lines.append("-" * 60)
    lines.append(f"  {'Total Itemized Deductions':<42} ${total:>10,.2f}")
    lines.append("")
    lines.append("NOTE: SALT deduction is capped at $10,000 by federal law.")
    lines.append("Enter these amounts in FreeTaxUSA under Deductions > Itemized.")

    return "\n".join(lines)


def generate_freetaxusa_export(
    transactions: list[dict[str, Any]],
    entity: str,
    year: int,
) -> tuple[str, str]:
    """Generate the appropriate FreeTaxUSA export for the given entity.

    Returns (content, filename) where content is the file body and filename
    is the suggested download name.

    - sparkry / blackline: Schedule C print-friendly text (.txt)
    - personal: Schedule A text + 1099-B CSV combined (.txt)
    """
    entity_lower = entity.lower()

    if entity_lower == "personal":
        sched_a = build_schedule_a_summary(transactions, year)
        csv_content = build_1099b_csv(transactions)
        content = sched_a
        if csv_content.strip():
            content += "\n\n--- 1099-B DATA (copy to FreeTaxUSA CSV import) ---\n\n"
            content += csv_content
        filename = f"freetaxusa_personal_{year}.txt"
    else:
        content = build_schedule_c_summary(transactions, entity, year)
        filename = f"freetaxusa_{entity_lower}_{year}.txt"

    return content, filename
