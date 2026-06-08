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

# Income categories that are retail sales (Retailing B&O classification).
RETAIL_CATEGORIES = {"SALES_INCOME"}

RETAILING_BO_RATE = Decimal("0.00471")

# Normalized locality (lowercased tax-line city, "city tax" stripped) → (code, name).
# Authoritative WA DOR Q2 2026 Local Sales & Use Tax Rates table.
WA_LOCATION_CODES: dict[str, tuple[str, str]] = {
    "issaquah": ("1714", "Issaquah"),
    "kirkland": ("1716", "Kirkland"),
    "maple valley": ("1720", "Maple Valley"),
    "sammamish": ("1739", "Sammamish"),
    "king county unincorp.": ("1700", "King County Unincorp."),
}
# Fallback when a WA sale's locality can't be mapped to a known city code.
UNKNOWN_WA_LOCATION = ("____", "WA — unmapped locality")


def _to_decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def _q2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass
class RetailFacts:
    """Per-transaction retail facts derived from a Shopify order dict."""

    pretax: Decimal
    sales_tax: Decimal
    is_wa: bool
    is_confirmed_oos: bool  # destination known AND outside WA
    location_code: str
    location_name: str


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


def _city_locality(raw: dict[str, Any]) -> str | None:
    """Return the normalized city locality from the order's city tax line."""
    for tl in raw.get("tax_lines") or []:
        title = str(tl.get("title") or "")
        low = title.lower()
        if "washington state tax" in low or "state tax" in low:
            continue
        # "Sammamish City Tax" -> "sammamish"; "... County Tax" handled too.
        loc = low.replace("city tax", "").replace("county tax", "").replace("tax", "").strip()
        if loc:
            return loc
    return None


def retail_facts(tx: dict[str, Any]) -> RetailFacts:
    """Extract pre-tax amount, collected tax, WA flag, and location code."""
    raw = tx.get("raw_data") or {}
    total_price = _to_decimal(raw.get("total_price"))
    if total_price == 0:
        total_price = _to_decimal(tx.get("amount"))
    sales_tax = _to_decimal(raw.get("total_tax"))
    pretax = _q2(total_price - sales_tax)

    tax_lines = raw.get("tax_lines") or []
    has_wa_state_line = any(
        "washington" in str(tl.get("title") or "").lower() for tl in tax_lines
    )
    ship = raw.get("shipping_address") or {}
    bill = raw.get("billing_address") or {}
    state = (ship.get("province_code") or bill.get("province_code") or "").upper()
    is_wa = has_wa_state_line or state == "WA"
    # Only deduct sales we can SUBSTANTIATE as out-of-state. Unknown-destination
    # sales (e.g. Stripe charges with no ship address) stay WA-taxable — never
    # claim an interstate deduction we can't prove (that would under-pay B&O).
    is_confirmed_oos = (not is_wa) and state != "" and state != "WA"

    location_code, location_name = "", ""
    if is_wa:
        loc = _city_locality(raw)
        if loc and loc in WA_LOCATION_CODES:
            location_code, location_name = WA_LOCATION_CODES[loc]
        elif loc:
            location_code, location_name = UNKNOWN_WA_LOCATION[0], f"WA — {loc.title()}"
        else:
            location_code, location_name = UNKNOWN_WA_LOCATION

    return RetailFacts(
        pretax=pretax,
        sales_tax=_q2(sales_tax),
        is_wa=is_wa,
        is_confirmed_oos=is_confirmed_oos,
        location_code=location_code,
        location_name=location_name,
    )


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
