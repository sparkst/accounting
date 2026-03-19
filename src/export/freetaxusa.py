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

# Form 1040 above-the-line adjustment line mapping
FORM_1040_ADJUSTMENTS: dict[str, tuple[str, str]] = {
    "HEALTH_INSURANCE": ("Line 17", "Self-employed health insurance deduction"),
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
    home_office_deduction: float = 0,
) -> str:
    """Build a print-friendly Schedule C summary aligned to IRS line numbers.

    Returns a plain-text string suitable for manual entry in FreeTaxUSA.
    Skips REIMBURSABLE and PERSONAL_NON_DEDUCTIBLE categories.

    Args:
        transactions: List of transaction dicts.
        entity: Entity name (e.g. "sparkry").
        year: Tax year.
        home_office_deduction: Optional home office deduction amount (IRS simplified
            method). When > 0, appears as Line 30 in the expense section.
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

    # Add home office deduction (Line 30) if provided
    home_office_decimal = Decimal(str(home_office_deduction))
    if home_office_decimal > 0:
        total_expenses += home_office_decimal

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
    if home_office_decimal > 0:
        lines.append(
            f"  {'L30':<12} {'Business use of home':<38} ${home_office_decimal:>10,.2f}"
        )
    lines.append(f"  {'':12} {'Total Expenses':<38} ${total_expenses:>10,.2f}")

    lines.append("")
    lines.append("=" * 60)
    lines.append(f"  {'':12} {'Net Profit (Loss)':<38} ${net_profit:>10,.2f}")
    lines.append("")
    lines.append("NOTE: Enter these amounts in FreeTaxUSA under Business Income.")
    lines.append("Meals (L24b) are already reduced to 50% deductible amount above.")

    return "\n".join(lines)


def build_1040_adjustments_summary(
    transactions: list[dict[str, Any]],
    year: int,
) -> str:
    """Build a print-friendly Form 1040 adjustments summary.

    Covers above-the-line deductions such as self-employed health insurance
    (Line 17).  Returns an empty string if there are no qualifying transactions.
    """
    totals: dict[str, Decimal] = {}
    for tx in transactions:
        cat = tx.get("tax_category", "")
        if cat not in FORM_1040_ADJUSTMENTS:
            continue
        amt = _abs_amount(tx)
        totals[cat] = totals.get(cat, Decimal("0")) + amt

    if not totals:
        return ""

    lines: list[str] = []
    lines.append("FORM 1040 — ADJUSTMENTS TO INCOME (Schedule 1, Part II)")
    lines.append(f"Tax Year: {year}")
    lines.append("=" * 60)
    lines.append("")

    total = Decimal("0")
    for cat, (irs_line, label) in FORM_1040_ADJUSTMENTS.items():
        amt = totals.get(cat, Decimal("0"))
        if amt:
            lines.append(f"  {irs_line:<12} {label:<38} ${amt:>10,.2f}")
            total += amt

    lines.append("-" * 60)
    lines.append(f"  {'':12} {'Total Adjustments':<38} ${total:>10,.2f}")
    lines.append("")
    lines.append("NOTE: Enter these amounts in FreeTaxUSA under Deductions > Adjustments.")

    return "\n".join(lines)


def build_form_1065_summary(
    transactions: list[dict[str, Any]],
    year: int,
) -> str:
    """Build a print-friendly Form 1065 partnership return summary for BlackLine MTB LLC.

    Returns a plain-text string suitable for manual entry in FreeTaxUSA.
    Covers ordinary business income (Part II) and deductions aligned to IRS
    Form 1065 line numbers.  Skips REIMBURSABLE and PERSONAL_NON_DEDUCTIBLE.
    """
    # Form 1065 income/deduction line mapping
    FORM_1065_INCOME_CATEGORIES = {
        "CONSULTING_INCOME",
        "SUBSCRIPTION_INCOME",
        "SALES_INCOME",
        "WHOLESALE_INCOME",
    }

    FORM_1065_LINES: dict[str, tuple[str, str]] = {
        # Income
        "CONSULTING_INCOME": ("L1a", "Gross receipts or sales"),
        "SUBSCRIPTION_INCOME": ("L1a", "Gross receipts or sales"),
        "SALES_INCOME": ("L1a", "Gross receipts or sales"),
        "WHOLESALE_INCOME": ("L1a", "Gross receipts or sales"),
        # Deductions (Part II)
        "ADVERTISING": ("L20", "Other deductions — Advertising"),
        "CAR_AND_TRUCK": ("L20", "Other deductions — Car and truck"),
        "CONTRACT_LABOR": ("L20", "Other deductions — Contract labor"),
        "INSURANCE": ("L20", "Other deductions — Insurance"),
        "LEGAL_AND_PROFESSIONAL": ("L20", "Other deductions — Legal and professional"),
        "OFFICE_EXPENSE": ("L20", "Other deductions — Office expense"),
        "SUPPLIES": ("L20", "Other deductions — Supplies"),
        "TAXES_AND_LICENSES": ("L14", "Taxes and licenses"),
        "TRAVEL": ("L20", "Other deductions — Travel"),
        "MEALS": ("L20", "Other deductions — Meals (50% deductible)"),
        "COGS": ("L2", "Cost of goods sold"),
    }

    totals: dict[str, Decimal] = {}
    for tx in transactions:
        cat = tx.get("tax_category", "")
        if not cat or cat in ("REIMBURSABLE", "PERSONAL_NON_DEDUCTIBLE"):
            continue
        if cat not in FORM_1065_LINES:
            continue
        amt = _abs_amount(tx)
        totals[cat] = totals.get(cat, Decimal("0")) + amt

    gross_income = Decimal("0")
    income_lines: list[tuple[str, str, Decimal]] = []
    deduction_lines: list[tuple[str, str, Decimal]] = []

    # Consolidate income categories that share the same IRS line (L1a)
    l1a_total = Decimal("0")
    l1a_detail: list[str] = []
    for cat in list(FORM_1065_INCOME_CATEGORIES):
        if cat in totals:
            l1a_total += totals[cat]
            l1a_detail.append(f"{cat.replace('_', ' ').title()}: ${totals[cat]:,.2f}")

    if l1a_total > 0:
        gross_income = l1a_total
        income_lines.append(("L1a", "Gross receipts or sales", l1a_total))

    for cat, total in sorted(totals.items(), key=lambda x: FORM_1065_LINES[x[0]][0]):
        if cat in FORM_1065_INCOME_CATEGORIES:
            continue
        irs_line, label = FORM_1065_LINES[cat]
        deduction_lines.append((irs_line, label, total))

    total_deductions = sum(amt for _, _, amt in deduction_lines)
    ordinary_income = gross_income - total_deductions

    lines: list[str] = []
    lines.append("FORM 1065 — U.S. RETURN OF PARTNERSHIP INCOME")
    lines.append(f"Entity: BLACKLINE MTB LLC  |  Tax Year: {year}")
    lines.append("=" * 60)

    lines.append("")
    lines.append("INCOME (Part I)")
    lines.append("-" * 60)
    for irs_line, label, amt in income_lines:
        lines.append(f"  {irs_line:<12} {label:<38} ${amt:>10,.2f}")
    if l1a_detail:
        for detail in l1a_detail:
            lines.append(f"  {'':12}   {detail}")
    lines.append(f"  {'':12} {'Gross Income':<38} ${gross_income:>10,.2f}")

    lines.append("")
    lines.append("DEDUCTIONS (Part II)")
    lines.append("-" * 60)
    for irs_line, label, amt in deduction_lines:
        lines.append(f"  {irs_line:<12} {label:<38} ${amt:>10,.2f}")
    lines.append(f"  {'':12} {'Total Deductions':<38} ${total_deductions:>10,.2f}")

    lines.append("")
    lines.append("=" * 60)
    lines.append(f"  {'':12} {'Ordinary Business Income (Loss)':<38} ${ordinary_income:>10,.2f}")
    lines.append("")
    lines.append("NOTE: Enter these amounts in FreeTaxUSA under Partnership Income (Form 1065).")
    lines.append("This income flows to Schedule K-1 (Travis Sparks, 100% partner).")
    lines.append("Meals are already reduced to 50% deductible amount above.")

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
    home_office_deduction: float = 0,
) -> tuple[str, str]:
    """Generate the appropriate FreeTaxUSA export for the given entity.

    Returns (content, filename) where content is the file body and filename
    is the suggested download name.

    - personal:  Schedule A text + 1099-B CSV combined (.txt)
    - blackline: Form 1065 partnership return summary (.txt)
    - sparkry (and other business entities): Schedule C print-friendly text (.txt)

    Args:
        transactions: List of transaction dicts.
        entity: Entity name — "personal", "blackline", or "sparkry".
        year: Tax year.
        home_office_deduction: Home office deduction amount to include on Schedule C
            Line 30 (Sparkry only). Ignored for blackline and personal.
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
    elif entity_lower == "blackline":
        content = build_form_1065_summary(transactions, year)
        adjustments = build_1040_adjustments_summary(transactions, year)
        if adjustments:
            content += "\n\n" + adjustments
        filename = f"freetaxusa_blackline_{year}.txt"
    else:
        # sparkry and any future business entities → Schedule C
        content = build_schedule_c_summary(
            transactions, entity, year, home_office_deduction=home_office_deduction
        )
        adjustments = build_1040_adjustments_summary(transactions, year)
        if adjustments:
            content += "\n\n" + adjustments
        filename = f"freetaxusa_{entity_lower}_{year}.txt"

    return content, filename
