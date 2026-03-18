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
            detail=f"Year {year} out of range (2020–2100).",
        )


def _fetch_transactions(
    session: Session,
    entity: str,
    year: int,
) -> list[Transaction]:
    """Return non-rejected transactions for the given entity + year."""
    year_prefix = str(year)
    return (
        session.query(Transaction)
        .filter(
            Transaction.entity == entity,
            Transaction.date.like(f"{year_prefix}-%"),
            Transaction.status != TransactionStatus.REJECTED.value,
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
# GET /api/tax-summary
# ---------------------------------------------------------------------------


@router.get("/tax-summary")
def get_tax_summary(
    entity: str = Query(..., description="Entity: sparkry | blackline | personal"),
    year: int = Query(..., description="Tax year (e.g. 2025)"),
    session: Session = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    """Return per-tax-category totals aggregated from confirmed transactions.

    Includes IRS line number mapping, readiness percentage, and net profit.
    Emits a warning (not a block) when unconfirmed transactions exist.
    """
    entity = _validate_entity(entity)
    _validate_year(year)

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
    content, filename = generate_freetaxusa_export(tx_dicts, entity, year)

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
