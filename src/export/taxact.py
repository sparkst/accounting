"""TaxAct export module.

Produces a print-friendly text summary aligned to Form 1065 (BlackLine MTB LLC)
or Schedule C (Sparkry AI LLC) for manual entry in TaxAct Business.

All public functions are pure: they accept a list of transaction dicts and
return formatted strings — no I/O side-effects.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from src.export.freetaxusa import INCOME_CATEGORIES, SCHEDULE_C_LINES

# ---------------------------------------------------------------------------
# Form 1065 line mapping (BlackLine MTB LLC — partnership)
# ---------------------------------------------------------------------------

# (irs_line, label, is_income)
FORM_1065_LINES: dict[str, tuple[str, str, bool]] = {
    "CONSULTING_INCOME": ("1a", "Gross receipts or sales", True),
    "SUBSCRIPTION_INCOME": ("1a", "Gross receipts or sales (subscriptions)", True),
    "SALES_INCOME": ("1a", "Gross receipts or sales (product)", True),
    "COGS": ("2", "Cost of goods sold", False),
    "ADVERTISING": ("20", "Advertising", False),
    "CAR_AND_TRUCK": ("20", "Car and truck expenses", False),
    "CONTRACT_LABOR": ("20", "Contract labor", False),
    "INSURANCE": ("20", "Insurance", False),
    "LEGAL_AND_PROFESSIONAL": ("20", "Legal and professional fees", False),
    "OFFICE_EXPENSE": ("20", "Office expenses", False),
    "SUPPLIES": ("20", "Supplies", False),
    "TAXES_AND_LICENSES": ("20", "Taxes and licenses", False),
    "TRAVEL": ("20", "Travel", False),
    "MEALS": ("20", "Meals (50% deductible)", False),
}

SKIP_CATEGORIES = {"REIMBURSABLE", "PERSONAL_NON_DEDUCTIBLE"}
PERSONAL_CATEGORIES = {
    "CHARITABLE_CASH",
    "CHARITABLE_STOCK",
    "MEDICAL",
    "STATE_LOCAL_TAX",
    "MORTGAGE_INTEREST",
    "INVESTMENT_INCOME",
}


def _to_decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def _abs_deductible(tx: dict[str, Any]) -> Decimal:
    raw = _to_decimal(tx.get("amount"))
    pct = _to_decimal(tx.get("deductible_pct", "1.0"))
    return abs(raw) * pct


def _aggregate(
    transactions: list[dict[str, Any]],
    skip: set[str] | None = None,
) -> dict[str, Decimal]:
    """Aggregate absolute deductible amounts per tax_category."""
    skip = skip or set()
    totals: dict[str, Decimal] = {}
    for tx in transactions:
        cat = tx.get("tax_category", "")
        if not cat or cat in skip:
            continue
        amt = _abs_deductible(tx)
        totals[cat] = totals.get(cat, Decimal("0")) + amt
    return totals


def build_form_1065_summary(
    transactions: list[dict[str, Any]],
    year: int,
) -> str:
    """Build a print-friendly Form 1065 summary for TaxAct Business.

    Sections:
      - Income (Line 1a: Gross receipts)
      - COGS (Line 2)
      - Gross profit (Line 3 = Line 1a - Line 2)
      - Deductions (Line 20: Other deductions, itemized)
      - Ordinary business income/loss
    """
    skip = SKIP_CATEGORIES | PERSONAL_CATEGORIES
    totals = _aggregate(transactions, skip=skip)

    gross_income = Decimal("0")
    cogs = Decimal("0")
    deductions: list[tuple[str, str, Decimal]] = []

    for cat, amt in totals.items():
        if cat in ("CONSULTING_INCOME", "SUBSCRIPTION_INCOME", "SALES_INCOME"):
            gross_income += amt
        elif cat == "COGS":
            cogs += amt
        elif cat in FORM_1065_LINES:
            _, label, _ = FORM_1065_LINES[cat]
            deductions.append((label, amt))

    gross_profit = gross_income - cogs
    total_deductions = sum(amt for _, amt in deductions)
    ordinary_income = gross_profit - total_deductions

    lines: list[str] = []
    lines.append("FORM 1065 — U.S. RETURN OF PARTNERSHIP INCOME")
    lines.append(f"Entity: BLACKLINE MTB LLC  |  Tax Year: {year}")
    lines.append("=" * 60)

    lines.append("")
    lines.append("INCOME")
    lines.append("-" * 60)
    lines.append(f"  Line 1a  {'Gross receipts or sales':<34} ${gross_income:>10,.2f}")
    lines.append(f"  Line 2   {'Cost of goods sold':<34} ${cogs:>10,.2f}")
    lines.append(f"  Line 3   {'Gross profit':<34} ${gross_profit:>10,.2f}")

    lines.append("")
    lines.append("DEDUCTIONS (Line 20 — Other deductions, attach statement)")
    lines.append("-" * 60)
    for label, amt in sorted(deductions, key=lambda x: x[0]):
        lines.append(f"  {'':9} {label:<34} ${amt:>10,.2f}")
    lines.append(f"  {'':9} {'Total deductions':<34} ${total_deductions:>10,.2f}")

    lines.append("")
    lines.append("=" * 60)
    lines.append(
        f"  {'':9} {'Ordinary business income (loss)':<34} ${ordinary_income:>10,.2f}"
    )
    lines.append("")
    lines.append(
        "NOTE: Enter these amounts in TaxAct Business > Form 1065 > Income/Deductions."
    )
    lines.append("Meals are already reduced to 50% deductible amount above.")
    lines.append(
        "Schedule K-1: Travis Sparks 100% ownership — pass all amounts to K-1."
    )

    return "\n".join(lines)


def build_schedule_c_summary_taxact(
    transactions: list[dict[str, Any]],
    entity: str,
    year: int,
) -> str:
    """Build a print-friendly Schedule C summary for TaxAct (Sparkry).

    TaxAct uses the same IRS lines as FreeTaxUSA; this version adds
    TaxAct-specific navigation hints.
    """
    skip = SKIP_CATEGORIES | PERSONAL_CATEGORIES
    totals = _aggregate(transactions, skip=skip)

    gross_income = Decimal("0")
    income_lines: list[tuple[str, str, Decimal]] = []
    expense_lines: list[tuple[str, str, Decimal]] = []

    for cat, amt in sorted(totals.items(), key=lambda x: SCHEDULE_C_LINES.get(x[0], ("ZZ", ""))[0]):
        if cat not in SCHEDULE_C_LINES:
            continue
        irs_line, label = SCHEDULE_C_LINES[cat]
        if cat in INCOME_CATEGORIES:
            gross_income += amt
            income_lines.append((irs_line, label, amt))
        else:
            expense_lines.append((irs_line, label, amt))

    total_expenses = sum(amt for _, _, amt in expense_lines)
    net_profit = gross_income - total_expenses

    out: list[str] = []
    out.append("SCHEDULE C — PROFIT OR LOSS FROM BUSINESS (TaxAct)")
    out.append(f"Entity: {entity.upper()}  |  Tax Year: {year}")
    out.append("=" * 60)

    out.append("")
    out.append("INCOME")
    out.append("-" * 60)
    for irs_line, label, amt in income_lines:
        out.append(f"  {irs_line:<12} {label:<34} ${amt:>10,.2f}")
    out.append(f"  {'':12} {'Gross Income':<34} ${gross_income:>10,.2f}")

    out.append("")
    out.append("EXPENSES")
    out.append("-" * 60)
    for irs_line, label, amt in expense_lines:
        out.append(f"  {irs_line:<12} {label:<34} ${amt:>10,.2f}")
    out.append(f"  {'':12} {'Total Expenses':<34} ${total_expenses:>10,.2f}")

    out.append("")
    out.append("=" * 60)
    out.append(f"  {'':12} {'Net Profit (Loss)':<34} ${net_profit:>10,.2f}")
    out.append("")
    out.append("NOTE: Enter these amounts in TaxAct > Federal > Business Income.")
    out.append("Meals (L24b) already reduced to 50% deductible amount above.")

    return "\n".join(out)


def generate_taxact_export(
    transactions: list[dict[str, Any]],
    entity: str,
    year: int,
) -> tuple[str, str]:
    """Generate the TaxAct export for the given entity.

    Returns (content, filename).
    - blackline → Form 1065 summary (.txt)
    - sparkry   → Schedule C summary (.txt)
    """
    entity_lower = entity.lower()

    if entity_lower == "blackline":
        content = build_form_1065_summary(transactions, year)
    else:
        content = build_schedule_c_summary_taxact(transactions, entity, year)

    filename = f"taxact_{entity_lower}_{year}.txt"
    return content, filename
