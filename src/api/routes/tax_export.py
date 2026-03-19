"""Tax export and summary API routes.

GET /api/tax-summary       — Per-category totals, IRS line mapping, readiness %
GET /api/export/freetaxusa — FreeTaxUSA text/CSV download
GET /api/export/taxact     — TaxAct text download
GET /api/export/bno        — WA B&O CSV download

All export endpoints refuse (HTTP 422) or warn when >20% of transactions for the
requested entity+year are unconfirmed.
"""

from __future__ import annotations

import logging
from datetime import date as _stdlib_date
from datetime import timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from src.api.deps import get_db
from src.export.bno_tax import generate_bno_export, generate_dor_upload
from src.export.freetaxusa import generate_freetaxusa_export
from src.export.taxact import generate_taxact_export
from src.models.enums import Entity, TransactionStatus
from src.models.transaction import Transaction

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tax_export"])

# ---------------------------------------------------------------------------
# IRS line number mapping (matches design spec table)
# ---------------------------------------------------------------------------

IRS_LINE_MAPPING: dict[str, str] = {
    "ADVERTISING": "L8",
    "CAR_AND_TRUCK": "L9",
    "CONTRACT_LABOR": "L11",
    "INSURANCE": "L15",
    "HEALTH_INSURANCE": "1040 Line 17 — Self-employed health insurance deduction",
    "LEGAL_AND_PROFESSIONAL": "L17",
    "OFFICE_EXPENSE": "L18",
    "SUPPLIES": "L22",
    "TAXES_AND_LICENSES": "L23",
    "TRAVEL": "L24a",
    "MEALS": "L24b",
    "COGS": "Part III",
    "CONSULTING_INCOME": "Gross receipts",
    "SUBSCRIPTION_INCOME": "Gross receipts",
    "SALES_INCOME": "Gross receipts",
    "WHOLESALE_INCOME": "Gross receipts",
    "REIMBURSABLE": "N/A",
    # Schedule A
    "CHARITABLE_CASH": "Sch A - Charitable",
    "CHARITABLE_STOCK": "Sch A - Charitable (non-cash)",
    "MEDICAL": "Sch A - Medical",
    "STATE_LOCAL_TAX": "Sch A - SALT",
    "MORTGAGE_INTEREST": "Sch A - Mortgage Interest",
    "INVESTMENT_INCOME": "Sch D / 8949",
    "PERSONAL_NON_DEDUCTIBLE": "N/A",
    "CAPITAL_CONTRIBUTION": "N/A",
    "OTHER_EXPENSE": "L27a",
}

INCOME_CATEGORIES = {"CONSULTING_INCOME", "SUBSCRIPTION_INCOME", "SALES_INCOME"}

# Threshold: warn/block when unconfirmed fraction exceeds this
UNCONFIRMED_WARN_THRESHOLD = 0.20

CONFIRMED_STATUSES = {
    TransactionStatus.CONFIRMED.value,
    TransactionStatus.SPLIT_PARENT.value,
    TransactionStatus.AUTO_CLASSIFIED.value,
}
ACTIVE_STATUSES = {
    TransactionStatus.CONFIRMED.value,
    TransactionStatus.SPLIT_PARENT.value,
    TransactionStatus.AUTO_CLASSIFIED.value,
    TransactionStatus.NEEDS_REVIEW.value,
}


def _validate_entity(entity: str) -> str:
    """Validate entity string against the Entity enum. Raises 422 on bad value."""
    try:
        return Entity(entity.lower()).value
    except ValueError:
        valid = [e.value for e in Entity]
        raise HTTPException(
            status_code=422,
            detail=f"Invalid entity '{entity}'. Must be one of: {valid}",
        ) from None


def _validate_year(year: int) -> None:
    if year < 2020 or year > 2100:
        raise HTTPException(
            status_code=422,
            detail=f"Year {year} out of range (2020\u20132100).",
        )


def _fetch_transactions(
    session: Session,
    entity: str,
    year: int,
) -> list[Transaction]:
    """Return non-rejected, non-split-parent transactions for the given entity + year.

    Split-parent transactions are excluded because their children carry
    the actual amounts — including both would double-count.
    """
    year_prefix = str(year)
    return (
        session.query(Transaction)
        .filter(
            Transaction.entity == entity,
            Transaction.date.like(f"{year_prefix}-%"),
            Transaction.status != TransactionStatus.REJECTED.value,
            Transaction.status != TransactionStatus.SPLIT_PARENT.value,
        )
        .all()
    )


def _tx_to_dict(tx: Transaction) -> dict[str, Any]:
    """Convert a Transaction ORM row to a plain dict for export functions."""
    return {
        "id": tx.id,
        "date": tx.date,
        "description": tx.description,
        "amount": str(tx.amount) if tx.amount is not None else None,
        "tax_category": tx.tax_category,
        "tax_subcategory": tx.tax_subcategory,
        "deductible_pct": str(tx.deductible_pct),
        "status": tx.status,
        "direction": tx.direction,
        "raw_data": tx.raw_data or {},
    }


def _readiness(transactions: list[Transaction]) -> dict[str, Any]:
    """Compute readiness stats: confirmed_count, total_count, pct, unconfirmed_ids."""
    total = len(transactions)
    confirmed = [tx for tx in transactions if tx.status in CONFIRMED_STATUSES]
    unconfirmed = [tx for tx in transactions if tx.status not in CONFIRMED_STATUSES]
    needs_review = [tx for tx in transactions if tx.status == TransactionStatus.NEEDS_REVIEW.value]
    auto_classified = [tx for tx in transactions if tx.status == TransactionStatus.AUTO_CLASSIFIED.value]
    pct = (len(confirmed) / total * 100) if total else 100.0
    return {
        "total_count": total,
        "confirmed_count": len(confirmed),
        "unconfirmed_count": len(unconfirmed),
        "needs_review_count": len(needs_review),
        "auto_classified_count": len(auto_classified),
        "readiness_pct": round(pct, 1),
        "unconfirmed_ids": [tx.id for tx in unconfirmed],
    }


def _check_unconfirmed_threshold(
    transactions: list[Transaction],
    *,
    hard_block: bool = False,
) -> dict[str, Any] | None:
    """Return a warning dict if >20% unconfirmed, or raise 422 if hard_block=True."""
    r = _readiness(transactions)
    total = r["total_count"]
    unconfirmed = r["unconfirmed_count"]
    if total == 0:
        return None
    fraction = unconfirmed / total
    if fraction > UNCONFIRMED_WARN_THRESHOLD:
        msg = (
            f"WARNING: {unconfirmed}/{total} transactions ({fraction * 100:.1f}%) "
            f"are unconfirmed. Tax export may be incomplete."
        )
        if hard_block:
            raise HTTPException(status_code=422, detail=msg)
        return {
            "warning": msg,
            "unconfirmed_count": unconfirmed,
            "unconfirmed_ids": r["unconfirmed_ids"],
        }
    return None


# ---------------------------------------------------------------------------
# Year-over-year comparison helpers
# ---------------------------------------------------------------------------


def _aggregate_for_yoy(
    transactions: list[Transaction],
    entity: str,
    year: int,
) -> dict[str, Any]:
    """Aggregate transactions into a lightweight dict for YoY comparison.

    Returns line_items, gross_income, total_expenses, net_profit,
    bno_monthly, bno_quarterly.
    """
    category_totals: dict[str, Decimal] = {}
    for tx in transactions:
        cat = tx.tax_category
        if not cat or cat in ("PERSONAL_NON_DEDUCTIBLE", "CAPITAL_CONTRIBUTION"):
            continue
        amt = Decimal(str(tx.amount)) if tx.amount is not None else Decimal("0")
        pct = Decimal(str(tx.deductible_pct))
        deductible = abs(amt) * pct
        category_totals[cat] = category_totals.get(cat, Decimal("0")) + deductible

    line_items = []
    gross_income = Decimal("0")
    total_expenses = Decimal("0")

    for cat, total in sorted(category_totals.items()):
        irs_line = IRS_LINE_MAPPING.get(cat, "Other")
        is_income = cat in INCOME_CATEGORIES
        is_reimbursable = cat == "REIMBURSABLE"

        if is_income:
            gross_income += total
        elif not is_reimbursable:
            total_expenses += total

        line_items.append(
            {
                "tax_category": cat,
                "irs_line": irs_line,
                "total": float(total),
                "is_income": is_income,
                "is_reimbursable": is_reimbursable,
            }
        )

    # Include home office deduction for the entity
    home_office = Decimal(str(_HOME_OFFICE_DEDUCTION.get(entity, 0.0)))
    if home_office > 0:
        total_expenses += home_office

    net_profit = gross_income - total_expenses

    monthly_income: dict[int, float] = {m: 0.0 for m in range(1, 13)}
    for tx in transactions:
        if tx.tax_category not in INCOME_CATEGORIES:
            continue
        date_str = tx.date or ""
        try:
            month_num = int(date_str[5:7])
        except (IndexError, ValueError):
            continue
        monthly_income[month_num] += abs(float(tx.amount)) if tx.amount is not None else 0.0

    bno_monthly = [
        {"month": f"{year}-{m:02d}", "income": round(monthly_income[m], 2)}
        for m in range(1, 13)
    ]

    quarterly_income = [0.0] * 4
    for m in range(1, 13):
        quarterly_income[(m - 1) // 3] += monthly_income[m]
    bno_quarterly = [
        {"quarter": f"Q{q + 1}", "income": round(quarterly_income[q], 2)}
        for q in range(4)
    ]

    return {
        "line_items": line_items,
        "gross_income": float(gross_income),
        "total_expenses": float(total_expenses),
        "net_profit": float(net_profit),
        "bno_monthly": bno_monthly,
        "bno_quarterly": bno_quarterly,
    }


def _build_yoy_comparison(
    current_agg: dict[str, Any],
    prior_agg: dict[str, Any],
    prior_year: int,
) -> dict[str, Any]:
    """Build a year-over-year comparison object from two _aggregate_for_yoy results."""
    prior_by_cat: dict[str, dict[str, Any]] = {
        item["tax_category"]: item for item in prior_agg["line_items"]
    }
    current_by_cat: dict[str, dict[str, Any]] = {
        item["tax_category"]: item for item in current_agg["line_items"]
    }

    all_cats = sorted(set(prior_by_cat) | set(current_by_cat))
    deltas = []
    for cat in all_cats:
        current_val = current_by_cat[cat]["total"] if cat in current_by_cat else 0.0
        prior_val = prior_by_cat[cat]["total"] if cat in prior_by_cat else 0.0
        delta = current_val - prior_val
        delta_pct = ((delta / prior_val) * 100.0) if prior_val != 0.0 else None
        meta = current_by_cat.get(cat) or prior_by_cat.get(cat, {})
        deltas.append(
            {
                "tax_category": cat,
                "irs_line": meta.get("irs_line", "Other"),
                "is_income": meta.get("is_income", False),
                "is_reimbursable": meta.get("is_reimbursable", False),
                "current": current_val,
                "prior": prior_val,
                "delta": round(delta, 2),
                "delta_pct": round(delta_pct, 1) if delta_pct is not None else None,
            }
        )

    profit_delta = current_agg["net_profit"] - prior_agg["net_profit"]
    prior_profit = prior_agg["net_profit"]
    profit_delta_pct = (
        (profit_delta / abs(prior_profit) * 100.0) if prior_profit != 0.0 else None
    )

    prior_bno_by_month: dict[str, float] = {
        item["month"][-2:]: item["income"] for item in prior_agg["bno_monthly"]
    }
    bno_monthly_deltas = [
        {
            "month": item["month"],
            "current": item["income"],
            "prior": prior_bno_by_month.get(item["month"][-2:], 0.0),
            "delta": round(
                item["income"] - prior_bno_by_month.get(item["month"][-2:], 0.0), 2
            ),
        }
        for item in current_agg["bno_monthly"]
    ]

    prior_bno_by_q: dict[str, float] = {
        item["quarter"]: item["income"] for item in prior_agg["bno_quarterly"]
    }
    bno_quarterly_deltas = [
        {
            "quarter": item["quarter"],
            "current": item["income"],
            "prior": prior_bno_by_q.get(item["quarter"], 0.0),
            "delta": round(item["income"] - prior_bno_by_q.get(item["quarter"], 0.0), 2),
        }
        for item in current_agg["bno_quarterly"]
    ]

    return {
        "prior_year": prior_year,
        "prior_year_items": prior_agg["line_items"],
        "prior_gross_income": prior_agg["gross_income"],
        "prior_total_expenses": prior_agg["total_expenses"],
        "prior_net_profit": prior_agg["net_profit"],
        "deltas": deltas,
        "net_profit_delta": round(profit_delta, 2),
        "net_profit_delta_pct": (
            round(profit_delta_pct, 1) if profit_delta_pct is not None else None
        ),
        "bno_monthly_deltas": bno_monthly_deltas,
        "bno_quarterly_deltas": bno_quarterly_deltas,
    }


# ---------------------------------------------------------------------------
# Home office and mileage constants
# ---------------------------------------------------------------------------

# IRS Simplified Method: 36 sqft × $5/sqft = $180/year (max 300 sqft, cap $1,500)
_HOME_OFFICE_DEDUCTION: dict[str, float] = {
    Entity.SPARKRY.value: 180.0,
    Entity.BLACKLINE.value: 0.0,
    Entity.PERSONAL.value: 0.0,
}

# IRS standard mileage rate for business use
_IRS_MILEAGE_RATE_CENTS = 70  # cents per mile (2025)
_IRS_MILEAGE_RATE_YEAR = 2025


# ---------------------------------------------------------------------------
# 1099 income breakdown
# ---------------------------------------------------------------------------


def _build_1099_breakdown(transactions: list[Transaction]) -> list[dict[str, Any]]:
    """Group income transactions by payer_1099 and return sorted by total desc.

    Only income transactions with payer_1099 set are included.
    """
    payer_totals: dict[str, dict[str, Any]] = {}
    for tx in transactions:
        if not tx.payer_1099:
            continue
        if tx.direction != "income":
            continue
        payer = tx.payer_1099
        if payer not in payer_totals:
            payer_totals[payer] = {
                "payer": payer,
                "type": getattr(tx, "payer_1099_type", None),
                "total": 0.0,
            }
        amt = abs(float(tx.amount)) if tx.amount is not None else 0.0
        payer_totals[payer]["total"] += amt

    result = sorted(payer_totals.values(), key=lambda x: x["total"], reverse=True)
    for item in result:
        item["total"] = round(item["total"], 2)
    return result


# ---------------------------------------------------------------------------
# Tax tips generation
# ---------------------------------------------------------------------------


def _generate_tax_tips(
    transactions: list[Transaction],
    entity: str,
    year: int,
    home_office_deduction: float,
    net_profit: Decimal,
) -> list[dict[str, Any]]:
    """Generate actionable tax optimization tips based on transaction data.

    Each tip has: id, type, title, detail, action_url (optional), dismissible.

    Tips generated:
      - home_office:    Sparkry with home_office_deduction > 0 but no HOME_OFFICE transactions
      - estimated_tax:  income > $10K and no estimated tax payments recorded
      - reimbursable:   unlinked reimbursable expenses older than 30 days
      - vehicle:        no CAR_AND_TRUCK transactions
      - unlinked_income: income transactions without payer_1099 set
    """
    today = _stdlib_date.today()
    tips: list[dict[str, Any]] = []

    # ── Home office tip ────────────────────────────────────────────────────
    if entity == Entity.SPARKRY.value and home_office_deduction > 0:
        has_home_office_tx = any(
            tx.tax_category == "HOME_OFFICE" for tx in transactions
        )
        if not has_home_office_tx:
            tips.append({
                "id": f"home_office_{entity}_{year}",
                "type": "home_office",
                "title": f"You qualify for a ${home_office_deduction:,.0f}/year home office deduction",
                "detail": (
                    "IRS simplified method: 36 sq ft × $5/sq ft. "
                    "Already included in your net profit calculation. "
                    "No HOME_OFFICE transactions are required — this deduction is automatic."
                ),
                "action_url": "/tax",
                "dismissible": True,
            })

    # ── Estimated tax tip ──────────────────────────────────────────────────
    if entity in (Entity.SPARKRY.value, Entity.BLACKLINE.value):
        gross_income = sum(
            abs(float(tx.amount))
            for tx in transactions
            if tx.tax_category in INCOME_CATEGORIES and tx.amount is not None
        )
        has_estimated_payments = any(
            "estimated" in (tx.tax_subcategory or "").lower()
            for tx in transactions
        )
        if gross_income > 10_000 and not has_estimated_payments:
            tips.append({
                "id": f"estimated_tax_{entity}_{year}",
                "type": "estimated_tax",
                "title": "No estimated tax payments recorded — you may owe penalties",
                "detail": (
                    f"Based on ${gross_income:,.0f} YTD income, the IRS expects quarterly "
                    "estimated payments. Tag any payments with subcategory 'estimated'."
                ),
                "action_url": "/register",
                "dismissible": True,
            })

    # ── Reimbursable tip ───────────────────────────────────────────────────
    cutoff = today - timedelta(days=30)
    cutoff_str = cutoff.isoformat()
    unlinked_reimbursable = [
        tx for tx in transactions
        if tx.direction == "reimbursable"
        and tx.reimbursement_link is None
        and (tx.date or "") < cutoff_str
    ]
    if unlinked_reimbursable:
        total_pending = sum(
            abs(float(tx.amount)) for tx in unlinked_reimbursable if tx.amount is not None
        )
        count = len(unlinked_reimbursable)
        tips.append({
            "id": f"reimbursable_{entity}_{year}",
            "type": "reimbursable",
            "title": f"${total_pending:,.2f} in expenses pending reimbursement",
            "detail": (
                f"{count} reimbursable {'expense' if count == 1 else 'expenses'} "
                f"older than 30 days {'has' if count == 1 else 'have'} not been linked "
                "to a reimbursement payment."
            ),
            "action_url": f"/register?entity={entity}&direction=reimbursable",
            "dismissible": True,
        })

    # ── Vehicle tip ────────────────────────────────────────────────────────
    if entity in (Entity.SPARKRY.value, Entity.BLACKLINE.value):
        has_vehicle = any(tx.tax_category == "CAR_AND_TRUCK" for tx in transactions)
        if not has_vehicle:
            tips.append({
                "id": f"vehicle_{entity}_{year}",
                "type": "vehicle",
                "title": "No vehicle expenses recorded — do you drive for business?",
                "detail": (
                    f"Business mileage is deductible "
                    f"(IRS standard rate: {_IRS_MILEAGE_RATE_CENTS}¢/mile for {_IRS_MILEAGE_RATE_YEAR}). "
                    "Add CAR_AND_TRUCK transactions if you use a vehicle for work."
                ),
                "action_url": "/register",
                "dismissible": True,
            })

    # ── Unlinked income (Stripe charges not matched to invoices) ───────────
    stripe_income = [
        tx for tx in transactions
        if tx.source in ("stripe_sparkry", "stripe_blackline", "stripe")
        and tx.tax_category in INCOME_CATEGORIES
        and not tx.payer_1099
    ]
    if stripe_income:
        total_unlinked = sum(
            abs(float(tx.amount)) for tx in stripe_income if tx.amount is not None
        )
        n = len(stripe_income)
        tips.append({
            "id": f"unlinked_income_{entity}_{year}",
            "type": "unlinked_income",
            "title": f"{n} Stripe {'charge' if n == 1 else 'charges'} (${total_unlinked:,.2f}) not matched to invoices",
            "detail": (
                "These Stripe income transactions have no 1099 payer set. "
                "Match them to invoices or tag the payer for accurate 1099 documentation."
            ),
            "action_url": "/register",
            "dismissible": True,
        })

    return tips


# ---------------------------------------------------------------------------
# Estimated tax computation
# ---------------------------------------------------------------------------


def _compute_estimated_tax(
    transactions: list[Any],
    net_profit: Decimal,
    year: int,
) -> dict[str, Any]:
    """Compute estimated quarterly tax liability for self-employment income.

    Formulas:
      SE tax = projected_annual_net × 92.35% × 15.3%
      Income tax = (projected_annual_net - 50% of SE tax) × 22%
      quarterly_payment = total_annual / 4

    Args:
        transactions: Transaction-like objects (need .tax_subcategory, .amount).
        net_profit:   Year-to-date net profit (income - expenses).
        year:         Tax year being computed.

    Returns:
        Dict with projected amounts, quarter details, and total_paid.
    """
    today = _stdlib_date.today()

    # Determine months elapsed
    if year < today.year:
        months_elapsed = 12
    elif year == today.year:
        months_elapsed = max(1, today.month)
    else:
        months_elapsed = 1

    ytd = float(net_profit)
    projected = ytd * (12 / months_elapsed) if months_elapsed > 0 else 0.0

    # SE tax: projected × 92.35% × 15.3%
    se_tax = max(0.0, projected * 0.9235 * 0.153)
    se_tax = round(se_tax, 2)

    # Income tax: (projected - 50% SE) × 22%
    income_tax = max(0.0, (projected - se_tax * 0.5) * 0.22)
    income_tax = round(income_tax, 2)

    total_annual = round(se_tax + income_tax, 2)
    quarterly = round(total_annual / 4, 2)

    # Total paid: sum of estimated tax payments
    total_paid = 0.0
    for tx in transactions:
        subcat = getattr(tx, "tax_subcategory", None) or ""
        if "estimated" in subcat.lower():
            total_paid += abs(float(tx.amount)) if tx.amount is not None else 0.0
    total_paid = round(total_paid, 2)

    # Quarter due dates
    q_due = [
        ("Q1", f"{year}-04-15"),
        ("Q2", f"{year}-06-15"),
        ("Q3", f"{year}-09-15"),
        ("Q4", f"{year + 1}-01-15"),
    ]

    quarters = []
    remaining_paid = total_paid
    for q_name, due_date in q_due:
        due = _stdlib_date.fromisoformat(due_date)
        paid_for_quarter = min(remaining_paid, quarterly)
        remaining_paid -= paid_for_quarter

        if paid_for_quarter >= quarterly:
            state = "paid"
        elif due < today:
            state = "overdue"
        else:
            state = "upcoming"

        remaining = max(0.0, round(quarterly - paid_for_quarter, 2))

        quarters.append({
            "quarter": q_name,
            "due_date": due_date,
            "projected_amount": quarterly,
            "paid": round(paid_for_quarter, 2),
            "remaining": remaining,
            "state": state,
        })

    return {
        "months_elapsed": months_elapsed,
        "ytd_net_profit": round(ytd, 2),
        "projected_annual_net": round(projected, 2),
        "se_tax_annual": se_tax,
        "income_tax_annual": income_tax,
        "total_annual": total_annual,
        "quarterly_payment": quarterly,
        "total_paid": total_paid,
        "quarters": quarters,
        "warning": (
            "ESTIMATE ONLY — This uses a flat 22% income tax rate and per-entity calculation. "
            "Does not include QBI deduction or combined 1040-ES worksheet. "
            "Consult your CPA for actual 1040-ES voucher amounts. "
            "At higher income levels, this may understate quarterly payments by 30-40%."
        ),
    }


# ---------------------------------------------------------------------------
# GET /api/tax-summary
# ---------------------------------------------------------------------------


@router.get("/tax-summary")
def get_tax_summary(
    entity: str = Query(..., description="Entity: sparkry | blackline | personal"),
    year: int = Query(..., description="Tax year (e.g. 2025)"),
    compare_year: int | None = Query(
        default=None,
        description=(
            "Optional prior year to compare against (e.g. 2024). "
            "When present the response includes a 'comparison' object with "
            "prior_year_items and per-category deltas."
        ),
    ),
    session: Session = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    """Return per-tax-category totals aggregated from confirmed transactions.

    Includes IRS line number mapping, readiness percentage, and net profit.
    Emits a warning (not a block) when unconfirmed transactions exist.

    When compare_year is provided, the response includes a 'comparison' object
    with prior_year_items and per-category deltas (delta, delta_pct).
    """
    entity = _validate_entity(entity)
    _validate_year(year)

    if compare_year is not None:
        _validate_year(compare_year)
        if compare_year == year:
            raise HTTPException(
                status_code=422,
                detail="compare_year must differ from year.",
            )

    transactions = _fetch_transactions(session, entity, year)
    readiness = _readiness(transactions)

    # Aggregate totals per category (absolute deductible amounts)
    category_totals: dict[str, Decimal] = {}
    for tx in transactions:
        cat = tx.tax_category
        if not cat or cat in ("PERSONAL_NON_DEDUCTIBLE", "CAPITAL_CONTRIBUTION"):
            continue
        amt = Decimal(str(tx.amount)) if tx.amount is not None else Decimal("0")
        pct = Decimal(str(tx.deductible_pct))
        deductible = abs(amt) * pct
        category_totals[cat] = category_totals.get(cat, Decimal("0")) + deductible

    # Build line items
    line_items = []
    gross_income = Decimal("0")
    total_expenses = Decimal("0")

    for cat, total in sorted(category_totals.items()):
        irs_line = IRS_LINE_MAPPING.get(cat, "Other")
        is_income = cat in INCOME_CATEGORIES
        is_reimbursable = cat == "REIMBURSABLE"

        if is_income:
            gross_income += total
        elif not is_reimbursable:
            total_expenses += total

        line_items.append(
            {
                "tax_category": cat,
                "irs_line": irs_line,
                "total": float(total),
                "is_income": is_income,
                "is_reimbursable": is_reimbursable,
            }
        )

    net_profit = gross_income - total_expenses

    # Warning if unconfirmed transactions exist
    warnings: list[dict[str, Any]] = []
    warn = _check_unconfirmed_threshold(transactions, hard_block=False)
    if warn:
        warnings.append(warn)

    # ── Per-month / per-quarter income breakdown for B&O table ────────────
    monthly_income: dict[int, float] = {m: 0.0 for m in range(1, 13)}
    for tx in transactions:
        cat = tx.tax_category
        if cat not in INCOME_CATEGORIES:
            continue
        date_str = tx.date or ""
        try:
            month_num = int(date_str[5:7])
        except (IndexError, ValueError):
            continue
        amt = abs(float(tx.amount)) if tx.amount is not None else 0.0
        monthly_income[month_num] += amt

    bno_monthly = [
        {"month": f"{year}-{m:02d}", "income": round(monthly_income[m], 2)}
        for m in range(1, 13)
    ]

    # Quarterly rollup
    quarterly_income = [0.0] * 4
    for m in range(1, 13):
        q = (m - 1) // 3
        quarterly_income[q] += monthly_income[m]
    bno_quarterly = [
        {"quarter": f"Q{q + 1}", "income": round(quarterly_income[q], 2)}
        for q in range(4)
    ]

    # ── Year-over-year comparison ─────────────────────────────────────────
    comparison: dict[str, Any] | None = None
    if compare_year is not None:
        prior_transactions = _fetch_transactions(session, entity, compare_year)
        current_agg = _aggregate_for_yoy(transactions, entity, year)
        prior_agg = _aggregate_for_yoy(prior_transactions, entity, compare_year)
        comparison = _build_yoy_comparison(current_agg, prior_agg, compare_year)

    # ── Home office deduction ────────────────────────────────────────────
    home_office = _HOME_OFFICE_DEDUCTION.get(entity, 0.0)
    if home_office > 0:
        total_expenses += Decimal(str(home_office))
        net_profit = gross_income - total_expenses

    # ── 1099 income breakdown ─────────────────────────────────────────
    income_1099_breakdown = _build_1099_breakdown(transactions)

    # ── Undocumented income warning ───────────────────────────────────
    tagged_amount = sum(entry["total"] for entry in income_1099_breakdown)
    total_income_amount = float(gross_income)
    if total_income_amount > 0 and tagged_amount < total_income_amount:
        undocumented = total_income_amount - tagged_amount
        warnings.append({
            "warning": (
                f"1099 documentation gap: ${undocumented:,.2f} of ${total_income_amount:,.2f} "
                f"gross income is not tagged to a 1099 payer."
            ),
            "undocumented_amount": round(undocumented, 2),
            "tagged_amount": round(tagged_amount, 2),
        })

    # ── Estimated tax ─────────────────────────────────────────────────
    estimated_tax: dict[str, Any] | None = None
    if entity in (Entity.SPARKRY.value, Entity.BLACKLINE.value):
        estimated_tax = _compute_estimated_tax(transactions, net_profit, year)

    # ── Tax tips ──────────────────────────────────────────────────────
    tax_tips = _generate_tax_tips(transactions, entity, year, home_office, net_profit)

    return {
        "entity": entity,
        "year": year,
        "line_items": line_items,
        "gross_income": float(gross_income),
        "total_expenses": float(total_expenses),
        "net_profit": float(net_profit),
        "readiness": readiness,
        "warnings": warnings,
        "bno_monthly": bno_monthly,
        "bno_quarterly": bno_quarterly,
        "comparison": comparison,
        "home_office_deduction": home_office,
        "income_1099_breakdown": income_1099_breakdown,
        "estimated_tax": estimated_tax,
        "tax_tips": tax_tips,
    }


# ---------------------------------------------------------------------------
# GET /api/export/freetaxusa
# ---------------------------------------------------------------------------


@router.get("/export/freetaxusa")
def export_freetaxusa(
    entity: str = Query(..., description="Entity: sparkry | blackline | personal"),
    year: int = Query(..., description="Tax year (e.g. 2025)"),
    session: Session = Depends(get_db),  # noqa: B008
) -> Response:
    """Download a FreeTaxUSA-compatible tax summary file.

    Returns a .txt file (Schedule C or Schedule A) or combined .txt with 1099-B CSV.
    Adds a bold WARNING header if >20% of transactions are unconfirmed.
    """
    entity = _validate_entity(entity)
    _validate_year(year)

    transactions = _fetch_transactions(session, entity, year)
    warn = _check_unconfirmed_threshold(transactions, hard_block=False)

    tx_dicts = [_tx_to_dict(tx) for tx in transactions]
    home_office = _HOME_OFFICE_DEDUCTION.get(entity, 0.0)
    content, filename = generate_freetaxusa_export(
        tx_dicts, entity, year, home_office_deduction=home_office
    )

    if warn:
        banner = f"*** {warn['warning']} ***\n\n"
        content = banner + content

    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# GET /api/export/taxact
# ---------------------------------------------------------------------------


@router.get("/export/taxact")
def export_taxact(
    entity: str = Query(..., description="Entity: sparkry | blackline | personal"),
    year: int = Query(..., description="Tax year (e.g. 2025)"),
    session: Session = Depends(get_db),  # noqa: B008
) -> Response:
    """Download a TaxAct-compatible tax summary file.

    Returns a .txt file (Form 1065 for BlackLine, Schedule C for others).
    Adds a bold WARNING header if >20% of transactions are unconfirmed.
    """
    entity = _validate_entity(entity)
    _validate_year(year)

    transactions = _fetch_transactions(session, entity, year)
    warn = _check_unconfirmed_threshold(transactions, hard_block=False)

    tx_dicts = [_tx_to_dict(tx) for tx in transactions]
    content, filename = generate_taxact_export(tx_dicts, entity, year)

    if warn:
        banner = f"*** {warn['warning']} ***\n\n"
        content = banner + content

    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# GET /api/export/bno
# ---------------------------------------------------------------------------


@router.get("/export/bno")
def export_bno(
    entity: str = Query(..., description="Entity: sparkry | blackline"),
    year: int = Query(..., description="Tax year (e.g. 2025)"),
    month: int | None = Query(
        default=None,
        ge=1,
        le=12,
        description="Month (1-12) for single-month filing (DOR format)",
    ),
    format: str = Query(
        default="summary",
        description="Export format: 'summary' (default CSV) or 'dor' (WA DOR upload)",
    ),
    session: Session = Depends(get_db),  # noqa: B008
) -> Response:
    """Download a WA B&O tax report CSV.

    Sparkry: monthly breakdown (12 rows).
    BlackLine: quarterly breakdown (4 rows per classification).
    Adds a WARNING row if >20% of transactions are unconfirmed.

    With format=dor and month=N, returns a WA DOR My DOR Data Upload file
    for the specified single month.
    """
    entity = _validate_entity(entity)
    _validate_year(year)

    if entity == Entity.PERSONAL.value:
        raise HTTPException(
            status_code=422,
            detail="B&O tax reports are only available for sparkry and blackline entities.",
        )

    transactions = _fetch_transactions(session, entity, year)
    warn = _check_unconfirmed_threshold(transactions, hard_block=False)

    tx_dicts = [_tx_to_dict(tx) for tx in transactions]

    # DOR upload format (single-month filing)
    if format.lower() == "dor":
        if month is None:
            raise HTTPException(
                status_code=422,
                detail="month parameter is required for DOR upload format.",
            )
        content, filename = generate_dor_upload(tx_dicts, entity, year, month)
        return Response(
            content=content,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # Standard summary format
    content, filename = generate_bno_export(tx_dicts, entity, year)

    if warn:
        warning_row = f"# WARNING: {warn['warning']}\n"
        content = warning_row + content

    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
