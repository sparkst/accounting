"""WA retail sales tax breakdown by DOR location code, per filing period.

Read-only report for the WA DOR return:
  - State Retail Sales Tax line: total WA taxable x 6.5%
  - Local City/County addendum: per location code, taxable amount x local rate

Taxable amount = pre-tax selling price (goods + shipping), sourced to the
destination via the order's city tax line. Flags any WA sale whose locality
can't be mapped to a known code (would mean a missing location code).
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from src.export.retail_sales_tax import UNKNOWN_WA_LOCATION, compute_retail_detail
from src.models.transaction import Transaction

try:
    from src.db.connection import get_session
except ImportError:  # pragma: no cover
    from src.db.session import get_session  # type: ignore

STATE_RATE = Decimal("0.065")
# WA DOR LOCAL rates (Q2 2026) for the codes BlackLine ships to.
LOCAL_RATE = {
    "1714": Decimal("0.040"),  # Issaquah RTA
    "1716": Decimal("0.039"),  # Kirkland
    "1720": Decimal("0.025"),  # Maple Valley
    "1739": Decimal("0.038"),  # Sammamish RTA
}
ENTITY = "blackline"


def _q2(v: Decimal) -> Decimal:
    return v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def main() -> None:
    session = get_session()
    try:
        txns = (
            session.query(Transaction)
            .filter(
                Transaction.entity == ENTITY,
                Transaction.status.notin_(("rejected", "split_parent")),
            )
            .all()
        )
        tx_dicts = [
            {
                "date": t.date,
                "amount": str(t.amount) if t.amount is not None else None,
                "tax_category": t.tax_category,
                "source": t.source,
                "raw_data": t.raw_data or {},
            }
            for t in txns
        ]
        years = sorted({(t.date or "")[:4] for t in txns if (t.date or "")[:4].isdigit()})

        for year in years:
            for q in (1, 2, 3, 4):
                d = compute_retail_detail(tx_dicts, int(year), quarter=q)
                if d.gross_retailing == 0 and not d.by_location:
                    continue
                # Sales-tax base = WA sales actually SOURCED to a location (where
                # Shopify collected tax). The B&O wa_taxable is higher because it
                # conservatively also includes unknown-destination sales — those
                # owe NO sales tax (no WA destination), so they're excluded here.
                state_taxable = _q2(sum((loc.taxable_amount for loc in d.by_location), Decimal("0")))
                print(f"\n===== BlackLine {year} Q{q} =====")
                print("  RETAIL SALES TAX (DOR return):")
                print(f"    State taxable (= sum of locations): {state_taxable:>10}"
                      f"  x 6.5% = {_q2(state_taxable * STATE_RATE)}")
                print("    --- Local City/County addendum ---")
                print(f"    {'Code':<6}{'Location':<16}{'Taxable':>10}{'Rate':>9}{'TaxDue':>9}")
                local_due_total = Decimal("0")
                for loc in d.by_location:
                    rate = LOCAL_RATE.get(loc.location_code)
                    if rate is None:
                        print(f"    {loc.location_code:<6}{loc.location_name:<16}"
                              f"{loc.taxable_amount:>10}  ??? UNMAPPED RATE")
                        continue
                    due = _q2(loc.taxable_amount * rate)
                    local_due_total += due
                    flag = "  <-- not on your DOR list" if loc.location_code == "1720" else ""
                    if loc.location_code == UNKNOWN_WA_LOCATION[0]:
                        flag = "  <-- UNMAPPED locality (needs a code)"
                    print(f"    {loc.location_code:<6}{loc.location_name:<16}"
                          f"{loc.taxable_amount:>10}{float(rate):>9.3f}{due:>9}{flag}")
                print(f"    {'':<32}{'local tax total:':>16} {local_due_total}")
                print(f"  (B&O retailing taxable, incl. unknown-dest sales: {d.wa_taxable})")
    finally:
        session.close()


if __name__ == "__main__":
    main()
