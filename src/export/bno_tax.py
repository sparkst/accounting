"""Washington State B&O Tax Report module.

Produces CSV reports for:
  - Sparkry LLC: monthly breakdown (12 rows, Jan–Dec) — B&O due monthly
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
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from src.export.basis import pretax_abs_amount
from src.utils.constants import SPARKRY_CONTACT_EMAIL

# ---------------------------------------------------------------------------
# WA B&O classification codes
# ---------------------------------------------------------------------------

# Income categories → B&O classification
BO_CLASSIFICATION: dict[str, tuple[str, str]] = {
    "CONSULTING_INCOME": ("ServiceOther", "Service and Other Activities"),
    "SUBSCRIPTION_INCOME": ("ServiceOther", "Service and Other Activities"),
    "SALES_INCOME": ("Retailing", "Retailing"),
    "WHOLESALE_INCOME": ("Wholesaling", "Wholesaling"),
}

# WA B&O tax rates (2025 — verify annually)
BO_RATE: dict[str, Decimal] = {
    "ServiceOther": Decimal("0.015"),   # 1.5% for services
    "Retailing": Decimal("0.00471"),    # 0.471% for retail sales
    "Wholesaling": Decimal("0.00484"),  # 0.484% for wholesale sales
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
    """Return {month: {bo_code: total_revenue}} for the given year.

    REQ-FIX-TAX-002: uses ``pretax_abs_amount`` so SALES_INCOME rows report
    the pre-tax figure (collected WA sales tax excluded), matching the DOR
    upload basis instead of double-counting the tax as gross receipts.
    """
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
        amt = pretax_abs_amount(tx)
        result[month][bo_code] = result[month].get(bo_code, Decimal("0")) + amt
    return result


def build_sparkry_bno_csv(
    transactions: list[dict[str, Any]],
    year: int,
) -> str:
    """Build monthly B&O report CSV for Sparkry LLC.

    Columns: period, bo_classification, gross_revenue, tax_rate, estimated_bo_tax
    One row per classification per month + a totals row.

    Empty input yields a zero-filled report (a no-activity B&O return is still
    a valid filing) — callers must not require non-empty data.
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

    # Collect all classification codes across all months
    all_codes: set[str] = set()
    for m_data in monthly.values():
        all_codes.update(m_data.keys())
    # If no income at all, still emit 12 empty month rows
    if not all_codes:
        all_codes = {"ServiceOther"}

    for month_idx, name in enumerate(MONTH_NAMES, start=1):
        month_data = monthly[month_idx]
        for bo_code in sorted(all_codes):
            # REQ-FIX-TAX-006: quantize BEFORE accumulate so the TOTAL row is
            # the exact sum of the displayed (already-rounded) rows — not a
            # separately-rounded sum of unquantized Decimals, which drifts by
            # a cent when many rows round in different directions.
            amt = month_data.get(bo_code, Decimal("0")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            rate = BO_RATE.get(bo_code, Decimal("0.015"))
            tax = (amt * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            _, classification_label = next(
                (v for k, v in BO_CLASSIFICATION.items() if v[0] == bo_code),
                (bo_code, bo_code),
            )
            writer.writerow(
                [
                    f"{year}-{month_idx:02d} ({name})",
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


def build_blackline_bno_csv(
    transactions: list[dict[str, Any]],
    year: int,
) -> str:
    """Build quarterly B&O report CSV for BlackLine MTB LLC.

    Columns: period, bo_classification, gross_revenue, tax_rate, estimated_bo_tax
    4 data rows (Q1–Q4) + a totals row.

    BlackLine has mixed income (product sales = Retailing; events = ServiceOther).
    Separate B&O lines are written per classification code within each quarter.

    Empty input yields a zero-filled report (a no-activity B&O return is still
    a valid filing) — callers must not require non-empty data.
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
            # REQ-FIX-TAX-006: quantize before accumulate — see comment in
            # build_sparkry_bno_csv.
            amt = q_data.get(bo_code, Decimal("0")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            rate = BO_RATE.get(bo_code, Decimal("0.015"))
            tax = (amt * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
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

# WA Combined Excise Tax Return B&O "Code" column (verified against the
# Feb 2026 CETR form — these are the e-file/data-upload codes, NOT the line
# numbers). Both entities are under $1M prior-year revenue, so Service & Other
# uses code 40 (line 11, rate 1.5%). Codes 100/500 are the higher revenue tiers.
#   Service & Other Activities (<$1M) → 40   (rate .015)
#   Retailing                          → 2    (rate .00471)
#   Wholesaling                        → 3    (rate .00484)
DOR_LINE_CODES: dict[str, int] = {
    "ServiceOther": 40,
    "Retailing": 2,
    "Wholesaling": 3,
}

# Retail sales tax codes (WA CETR Section II/III, verified vs Feb 2026 form):
DOR_STATE_RETAIL_SALES_LINE = 1   # line 28 "Retail Sales", code 01, 6.5%
DOR_LOCAL_SALES_LINE = 45         # Section III local sales tax (with location code)


def generate_dor_upload(
    transactions: list[dict[str, Any]],
    entity: str,
    year: int,
    month: int | None = None,
    quarter: int | None = None,
) -> tuple[str, str]:
    """Generate a WA DOR My DOR Data Upload file for a single filing period.

    Provide exactly one of ``month`` (1-12, monthly filer) or ``quarter`` (1-4,
    quarterly filer). Format follows the official My DOR Data Upload Instructions:
    - ACCOUNT tag: TRA, Period, Preparer, Email, Phone
      Period is ``MMYYYY`` for monthly (e.g. 042026) and ``Q#YYYY`` for
      quarterly (e.g. Q12026) — per the instructions' two ACCOUNT examples.
    - TAX tag: Line Code, Location Code (0 for B&O/state), gross Amount.

    B&O TAX amounts are pre-tax. Retailing is reported on the corrected basis
    (pre-tax, net of confirmed interstate) from ``compute_retail_detail`` — NOT
    the tax-inclusive aggregation. For retail income the file also emits the
    State Retail Sales (code 1) line and a Local Sales (code 45) line per WA DOR
    location code, with the destination-sourced taxable amount.

    Returns (file_content, filename).
    """
    from src.export.basis import UNKNOWN_WA_LOCATION
    from src.export.retail_sales_tax import compute_retail_detail

    if (month is None) == (quarter is None):
        raise ValueError("Provide exactly one of month or quarter.")

    entity_lower = entity.lower()
    account_id = DOR_ACCOUNT_IDS.get(entity_lower, "000000000")
    monthly = _aggregate_income_by_month(transactions, year)

    if month is not None:
        period = f"{month:02d}{year}"
        period_suffix = f"{month:02d}"
        months = [month]
    else:
        assert quarter is not None
        period = f"Q{quarter}{year}"
        period_suffix = f"Q{quarter}"
        months = [3 * (quarter - 1) + i for i in (1, 2, 3)]

    # Non-retail B&O income (Service & Other, Wholesaling) — these carry no sales
    # tax, so the aggregated amount is already the pre-tax figure.
    period_income: dict[str, Decimal] = {}
    for m in months:
        for bo_code, amt in monthly.get(m, {}).items():
            period_income[bo_code] = period_income.get(bo_code, Decimal("0")) + amt

    detail = compute_retail_detail(transactions, year, month=month, quarter=quarter)

    # REQ-FIX-TAX-007: never silently emit the sentinel '____' location code —
    # hard-fail with an actionable error naming the unmapped locality so a
    # human adds it to WA_LOCATION_CODES before the upload is generated.
    unmapped = [loc for loc in detail.by_location if loc.location_code == UNKNOWN_WA_LOCATION[0]]
    if unmapped:
        localities = ", ".join(sorted({loc.location_name for loc in unmapped}))
        raise ValueError(
            f"DOR upload blocked: {len(unmapped)} order(s) map to unmapped WA "
            f"locality '{UNKNOWN_WA_LOCATION[0]}' — add the locality to "
            f"WA_LOCATION_CODES: {localities}"
        )

    state_taxable = sum(
        (loc.taxable_amount for loc in detail.by_location), Decimal("0")
    )

    lines: list[str] = [
        f"ACCOUNT,{account_id},{period},"
        f"Travis Sparks,{SPARKRY_CONTACT_EMAIL},919-491-3894"
    ]

    emitted = False
    # B&O: non-retailing classifications from the aggregation ...
    for bo_code, amount in sorted(period_income.items()):
        if bo_code == "Retailing":
            continue  # emitted below on the corrected pre-tax basis
        lines.append(f"TAX,{DOR_LINE_CODES.get(bo_code, 40)},0,{amount:.2f}")
        emitted = True
    # ... and Retailing on the pre-tax, net-of-interstate basis.
    if detail.wa_taxable > 0:
        lines.append(f"TAX,{DOR_LINE_CODES['Retailing']},0,{detail.wa_taxable:.2f}")
        emitted = True

    # Retail sales tax: state line + one local line per DOR location code.
    if state_taxable > 0:
        lines.append(f"TAX,{DOR_STATE_RETAIL_SALES_LINE},0,{state_taxable:.2f}")
        for loc in detail.by_location:
            lines.append(
                f"TAX,{DOR_LOCAL_SALES_LINE},{loc.location_code},{loc.taxable_amount:.2f}"
            )
        emitted = True

    if not emitted:
        default_line = 40 if entity_lower == "sparkry" else 2
        lines.append(f"TAX,{default_line},0,0.00")

    content = "\n".join(lines) + "\n"
    filename = f"dor_upload_{entity_lower}_{year}_{period_suffix}.csv"
    return content, filename
