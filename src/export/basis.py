"""Canonical pre-tax amount computation for all tax export surfaces.

REQ-ID: REQ-FIX-TAX-002  Before this module existed, ``retail_sales_tax.py``'s
pre-tax computation (``retail_facts``) was used only by ``generate_dor_upload``
— every other export surface (B&O summary CSVs, FreeTaxUSA, TaxAct, the
dashboard tax-summary aggregation) called ``abs(amount) * deductible_pct``
directly on the Shopify-stored ``total_price``, which INCLUDES collected WA
retail sales tax. That double-counted the sales tax as gross receipts on
every surface except the DOR upload. ``retail_facts``/``RetailFacts`` moved
here verbatim from ``retail_sales_tax.py`` (which re-exports them for
backward compatibility) so every surface can share one pre-tax figure via
``pretax_abs_amount``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

# Income categories that are retail sales (Retailing B&O classification).
RETAIL_CATEGORIES = {"SALES_INCOME"}

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


def pretax_abs_amount(tx: dict[str, Any]) -> Decimal:
    """Return the canonical gross/deductible amount for one transaction dict.

    - ``SALES_INCOME`` rows: ``retail_facts(tx).pretax`` (raw ``total_price -
      total_tax``, quantized to cents). Rows with no substantiating
      ``tax_lines``/``total_price`` degrade to ``abs(amount)`` — collected
      tax is only excludable when we can prove it was actually collected,
      mirroring the interstate-deduction rule in ``retail_facts``.
    - Everything else: ``abs(Decimal(str(amount))) * deductible_pct``.

    ``deductible_pct`` is NOT applied to the SALES_INCOME branch — retail
    income rows do not carry a meaningful deductible_pct.
    """
    cat = tx.get("tax_category")
    if cat in RETAIL_CATEGORIES:
        return abs(retail_facts(tx).pretax)

    raw_amt = _to_decimal(tx.get("amount"))
    pct = _to_decimal(tx.get("deductible_pct", "1.0"))
    return abs(raw_amt) * pct
