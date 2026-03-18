"""Washington State B&O Tax Report module.

Produces CSV reports for:
  - Sparkry AI LLC: monthly breakdown (12 rows, Jan–Dec) — B&O due monthly
  - BlackLine MTB LLC: quarterly breakdown (4 rows, Q1–Q4) — B&O due quarterly

WA B&O classification codes:
  - Service income → "Service and Other Activities" (code: ServiceOther)
  - Product sales  → "Retailing" (code: Retailing)
  - Mixed          → separate lines per classification

All public functions are pure: accept a list of transaction dicts and return
formatted CSV strings — no I/O side-effects.
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal
from typing import Any

# ---------------------------------------------------------------------------
# WA B&O classification codes
# ---------------------------------------------------------------------------

# Income categories → B&O classification
BO_CLASSIFICATION: dict[str, tuple[str, str]] = {
    "CONSULTING_INCOME": ("ServiceOther", "Service and Other Activities"),
    "SUBSCRIPTION_INCOME": ("ServiceOther", "Service and Other Activities"),
    "SALES_INCOME": ("Retailing", "Retailing"),
}

# WA B&O tax rates (2025 — verify annually)
BO_RATE: dict[str, Decimal] = {
    "ServiceOther": Decimal("0.015"),   # 1.5% for services
    "Retailing": Decimal("0.00471"),    # 0.471% for retail sales
}

INCOME_CATEGORIES = set(BO_CLASSIFICATION.keys())

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

QUARTER_NAMES = ["Q1 (Jan-Mar)", "Q2 (Apr-Jun)", "Q3 (Jul-Sep)", "Q4 (Oct-Dec)"]


def _to_decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def _month_from_date(date_str: str) -> int | None:
    """Extract 1-based month from ISO date string YYYY-MM-DD."""
    try:
        return int(date_str[5:7])
    except (IndexError, ValueError):
        return None


def _quarter_from_month(month: int) -> int:
    """Return 1-based quarter from 1-based month."""
    return (month - 1) // 3 + 1


def _aggregate_income_by_month(
    transactions: list[dict[str, Any]],
    year: int,
) -> dict[int, dict[str, Decimal]]:
    """Return {month: {bo_code: total_revenue}} for the given year."""
    result: dict[int, dict[str, Decimal]] = {m: {} for m in range(1, 13)}
    for tx in transactions:
        cat = tx.get("tax_category", "")
        if cat not in INCOME_CATEGORIES:
            continue
        date_str = tx.get("date", "")
        if not date_str.startswith(str(year)):
            continue
        month = _month_from_date(date_str)
        if month is None:
            continue
        bo_code, _ = BO_CLASSIFICATION[cat]
        amt = abs(_to_decimal(tx.get("amount")))
        result[month][bo_code] = result[month].get(bo_code, Decimal("0")) + amt
    return result


def build_sparkry_bno_csv(
    transactions: list[dict[str, Any]],
    year: int,
) -> str:
    """Build monthly B&O report CSV for Sparkry AI LLC.

    Columns: period, bo_classification, gross_revenue, tax_rate, estimated_bo_tax
    12 data rows (one per month) + a totals row.
    """
    monthly = _aggregate_income_by_month(transactions, year)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "period",
            "bo_classification",
            "bo_code",
            "gross_revenue",
            "tax_rate",
            "estimated_bo_tax",
        ]
    )

    grand_revenue = Decimal("0")
    grand_tax = Decimal("0")

    for month_idx, name in enumerate(MONTH_NAMES, start=1):
        month_data = monthly[month_idx]
        month_revenue = sum(month_data.values(), Decimal("0"))
        # Use ServiceOther as primary classification for Sparkry (consulting/SaaS)
        bo_code = "ServiceOther"
        if month_data:
            # Use the code with the highest revenue this month
            bo_code = max(month_data, key=lambda k: month_data[k])
        rate = BO_RATE.get(bo_code, Decimal("0.015"))
        tax = month_revenue * rate

        writer.writerow(
            [
                f"{year}-{month_idx:02d} ({name})",
                BO_CLASSIFICATION.get(bo_code, (bo_code, bo_code))[1]
                if bo_code in {c for c, _ in BO_CLASSIFICATION.values()}
                else "Service and Other Activities",
                bo_code,
                f"{month_revenue:.2f}",
                f"{rate * 100:.3f}%",
                f"{tax:.2f}",
            ]
        )
        grand_revenue += month_revenue
        grand_tax += month_revenue * rate

    writer.writerow(
        [
            f"{year} TOTAL",
            "",
            "",
            f"{grand_revenue:.2f}",
            "",
            f"{grand_tax:.2f}",
        ]
    )

    return output.getvalue()


def build_blackline_bno_csv(
    transactions: list[dict[str, Any]],
    year: int,
) -> str:
    """Build quarterly B&O report CSV for BlackLine MTB LLC.

    Columns: period, bo_classification, gross_revenue, tax_rate, estimated_bo_tax
    4 data rows (Q1–Q4) + a totals row.

    BlackLine has mixed income (product sales = Retailing; events = ServiceOther).
    Separate B&O lines are written per classification code within each quarter.
    """
    monthly = _aggregate_income_by_month(transactions, year)

    # Roll up to quarters: {quarter: {bo_code: total}}
    quarterly: dict[int, dict[str, Decimal]] = {q: {} for q in range(1, 5)}
    for month_idx in range(1, 13):
        q = _quarter_from_month(month_idx)
        for bo_code, amt in monthly[month_idx].items():
            quarterly[q][bo_code] = quarterly[q].get(bo_code, Decimal("0")) + amt

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "period",
            "bo_classification",
            "bo_code",
            "gross_revenue",
            "tax_rate",
            "estimated_bo_tax",
        ]
    )

    grand_revenue = Decimal("0")
    grand_tax = Decimal("0")

    all_codes: set[str] = set()
    for q_data in quarterly.values():
        all_codes.update(q_data.keys())

    # If no income at all, still emit 4 empty quarter rows
    if not all_codes:
        all_codes = {"ServiceOther"}

    for q_idx, q_name in enumerate(QUARTER_NAMES, start=1):
        q_data = quarterly[q_idx]
        for bo_code in sorted(all_codes):
            amt = q_data.get(bo_code, Decimal("0"))
            rate = BO_RATE.get(bo_code, Decimal("0.015"))
            tax = amt * rate
            _, classification_label = next(
                (v for k, v in BO_CLASSIFICATION.items() if v[0] == bo_code),
                (bo_code, bo_code),
            )
            writer.writerow(
                [
                    f"{year} {q_name}",
                    classification_label,
                    bo_code,
                    f"{amt:.2f}",
                    f"{rate * 100:.3f}%",
                    f"{tax:.2f}",
                ]
            )
            grand_revenue += amt
            grand_tax += tax

    writer.writerow(
        [
            f"{year} TOTAL",
            "",
            "",
            f"{grand_revenue:.2f}",
            "",
            f"{grand_tax:.2f}",
        ]
    )

    return output.getvalue()


def generate_bno_export(
    transactions: list[dict[str, Any]],
    entity: str,
    year: int,
) -> tuple[str, str]:
    """Generate the B&O tax report CSV for the given entity.

    Returns (csv_content, filename).
    - sparkry   → monthly CSV (12 rows)
    - blackline → quarterly CSV (4 rows × classifications)
    """
    entity_lower = entity.lower()

    if entity_lower == "blackline":
        content = build_blackline_bno_csv(transactions, year)
    else:
        content = build_sparkry_bno_csv(transactions, year)

    filename = f"bno_{entity_lower}_{year}.csv"
    return content, filename


# ---------------------------------------------------------------------------
# WA DOR Data Upload format
# ---------------------------------------------------------------------------

# Account IDs (dashes removed for upload)
DOR_ACCOUNT_IDS: dict[str, str] = {
    "sparkry": "605965107",
    "blackline": "605922410",
}

# B&O line codes per entity classification
# Line 7 = Service and Other Activities
# Line 2 = Retailing
DOR_LINE_CODES: dict[str, int] = {
    "ServiceOther": 7,
    "Retailing": 2,
}


def generate_dor_upload(
    transactions: list[dict[str, Any]],
    entity: str,
    year: int,
    month: int,
) -> tuple[str, str]:
    """Generate WA DOR My DOR Data Upload file for a single filing period.

    Format follows the official My DOR Data Upload Instructions:
    - ACCOUNT tag: TRA, Period (MMYYYY), Preparer, Email, Phone
    - TAX tag: Line Code, Location Code (0 for state), Amount

    Returns (file_content, filename).
    """
    entity_lower = entity.lower()
    account_id = DOR_ACCOUNT_IDS.get(entity_lower, "000000000")
    period = f"{month:02d}{year}"

    lines: list[str] = []

    # ACCOUNT line
    lines.append(
        f"ACCOUNT,{account_id},{period},"
        f"Travis Sparks,travis@sparkry.com,919-491-3894"
    )

    # Aggregate income for the specified month
    monthly = _aggregate_income_by_month(transactions, year)
    month_data = monthly.get(month, {})

    if month_data:
        for bo_code, amount in sorted(month_data.items()):
            line_code = DOR_LINE_CODES.get(bo_code, 7)
            lines.append(f"TAX,{line_code},0,{amount:.2f}")
    else:
        # Even if zero, include a TAX line so the file is valid
        default_line = 7 if entity_lower == "sparkry" else 2
        lines.append(f"TAX,{default_line},0,0.00")

    content = "\n".join(lines) + "\n"
    filename = f"dor_upload_{entity_lower}_{year}_{month:02d}.csv"
    return content, filename
