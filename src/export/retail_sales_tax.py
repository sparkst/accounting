"""WA retail sales tax + retailing-B&O detail for BlackLine MTB Apparel.

BlackLine sells retail apparel via Shopify, which COLLECTED WA retail sales tax
but did not remit it (every tax line has ``channel_liable: false``), so BlackLine
must remit it to WA DOR. This module produces, per filing period:

  - Retailing B&O on the correct basis:
      * gross excludes the collected sales tax (the stored Shopify ``amount`` is
        ``total_price``, which includes it; WA B&O is measured on the pre-tax
        selling price), and
      * out-of-state (interstate) sales are deducted from the WA-taxable amount.
  - The retail sales tax owed, broken down by WA DOR 4-digit location code.

Location codes are the WA DOR Q2 2026 *Local Sales & Use Tax Rates* codes
(dor.wa.gov). Update when DOR changes them. The reliable locality signal is the
city ``tax_lines`` title (e.g. "Sammamish City Tax") — the address city/state
fields are sometimes blank or hold the billing city.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

# REQ-FIX-TAX-002: retail_facts/RetailFacts/WA_LOCATION_CODES/UNKNOWN_WA_LOCATION/
# RETAIL_CATEGORIES moved to src/export/basis.py (the canonical amount-computation
# module every export surface imports from). Re-exported here verbatim for
# backward compatibility — existing callers of
# ``from src.export.retail_sales_tax import retail_facts`` are unaffected.
from src.export.basis import (
    RETAIL_CATEGORIES as RETAIL_CATEGORIES,
)
from src.export.basis import (
    UNKNOWN_WA_LOCATION as UNKNOWN_WA_LOCATION,
)
from src.export.basis import (
    WA_LOCATION_CODES as WA_LOCATION_CODES,
)
from src.export.basis import (
    RetailFacts as RetailFacts,
)
from src.export.basis import (
    retail_facts as retail_facts,
)

RETAILING_BO_RATE = Decimal("0.00471")


def _q2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass
class LocationLine:
    location_code: str
    location_name: str
    taxable_amount: Decimal = Decimal("0")
    tax_collected: Decimal = Decimal("0")


@dataclass
class RetailDetail:
    gross_retailing: Decimal  # pre-tax retail sales (all destinations)
    interstate_deduction: Decimal  # confirmed out-of-state pre-tax sales
    wa_taxable: Decimal  # gross_retailing - interstate_deduction
    retailing_bo: Decimal  # wa_taxable * retailing rate
    sales_tax_collected: Decimal  # total WA retail sales tax collected (to remit)
    by_location: list[LocationLine] = field(default_factory=list)


def _period_months(month: int | None, quarter: int | None) -> set[int]:
    if (month is None) == (quarter is None):
        raise ValueError("Provide exactly one of month or quarter.")
    if month is not None:
        return {month}
    assert quarter is not None  # guaranteed by the XOR check above
    return {3 * (quarter - 1) + 1, 3 * (quarter - 1) + 2, 3 * (quarter - 1) + 3}


def compute_retail_detail(
    transactions: list[dict[str, Any]],
    year: int,
    month: int | None = None,
    quarter: int | None = None,
) -> RetailDetail:
    """Aggregate retail (SALES_INCOME) rows for the period into a RetailDetail."""
    months = _period_months(month, quarter)

    gross = Decimal("0")
    interstate = Decimal("0")
    sales_tax = Decimal("0")
    locations: dict[str, LocationLine] = {}

    for tx in transactions:
        if tx.get("tax_category") not in RETAIL_CATEGORIES:
            continue
        date_str = str(tx.get("date") or "")
        if not date_str.startswith(str(year)):
            continue
        try:
            m = int(date_str[5:7])
        except (IndexError, ValueError):
            continue
        if m not in months:
            continue

        f = retail_facts(tx)
        gross += f.pretax
        sales_tax += f.sales_tax
        if f.is_confirmed_oos:
            interstate += f.pretax
        elif f.is_wa and f.location_code:
            line = locations.setdefault(
                f.location_code, LocationLine(f.location_code, f.location_name)
            )
            line.taxable_amount += f.pretax
            line.tax_collected += f.sales_tax

    wa_taxable = _q2(gross - interstate)
    return RetailDetail(
        gross_retailing=_q2(gross),
        interstate_deduction=_q2(interstate),
        wa_taxable=wa_taxable,
        retailing_bo=_q2(wa_taxable * RETAILING_BO_RATE),
        sales_tax_collected=_q2(sales_tax),
        by_location=sorted(
            (
                LocationLine(
                    li.location_code, li.location_name,
                    _q2(li.taxable_amount), _q2(li.tax_collected),
                )
                for li in locations.values()
                if li.taxable_amount or li.tax_collected
            ),
            key=lambda li: li.location_name,
        ),
    )
