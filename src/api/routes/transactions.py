"""Transaction endpoints.

GET  /api/transactions          — Paginated, filtered list.
GET  /api/transactions/review   — needs_review items ordered by priority.
GET  /api/transactions/{id}     — Single transaction.
PATCH /api/transactions/{id}    — Update fields + learning loop on confirm.
POST /api/transactions/{id}/split               — Split into child line items.
POST /api/transactions/{id}/extract-receipt     — OCR an attachment via Claude Vision.
POST /api/transactions/{id}/link-reimbursement  — Link an expense to its reimbursement.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from sqlalchemy import func
from collections.abc import Generator

from sqlalchemy.orm import Session

from src.api.routes.tax_year_locks import check_lock
from src.classification.splitter import (
    SplitLineItem,
    SplitValidationError,
    cascade_reject_children,
    split_transaction,
    suggest_hotel_splits,
    validate_split_amounts,
)
from src.db.connection import SessionLocal
from src.models.audit_event import AuditEvent
from src.models.enums import (
    ConfirmedBy,
    Direction,
    Entity,
    TaxCategory,
    TaxSubcategory,
    TransactionStatus,
    VendorRuleSource,
)
from src.models.transaction import Transaction
from src.models.vendor_rule import VendorRule

logger = logging.getLogger(__name__)

router = APIRouter(tags=["transactions"])


# ---------------------------------------------------------------------------
# Dependency
# ---------------------------------------------------------------------------


def get_db() -> Generator[Session, None, None]:
    """Yield a database session, ensuring cleanup."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Response / request schemas
# ---------------------------------------------------------------------------


class TransactionOut(BaseModel):
    """Full transaction response schema."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    source: str
    source_id: str | None
    date: str
    description: str
    amount: float | None = None  # Decimal converted to float; None = unknown
    currency: str
    entity: str | None
    direction: str | None
    tax_category: str | None
    tax_subcategory: str | None
    deductible_pct: float
    status: str
    confidence: float
    review_reason: str | None
    parent_id: str | None
    reimbursement_link: str | None = None
    payment_method: str | None = None
    notes: str | None
    confirmed_by: str
    created_at: datetime
    updated_at: datetime
    raw_data: Any | None = None
    attachments: Any | None = None

    @field_validator("amount", mode="before")
    @classmethod
    def coerce_amount(cls, v: Any) -> float | None:
        """Serialise Decimal / float to a plain float for JSON.

        Returns a float rather than a string so the frontend can do
        arithmetic without extra parsing.
        """
        if v is None:
            return None
        return float(v)

    @model_validator(mode="after")
    def fix_income_sign(self) -> TransactionOut:
        """Ensure income transactions have positive amounts in the API response.

        The Gmail adapter stores all amounts as negative (expenses).  When the
        classification engine sets direction=income the sign is never flipped in
        the DB (preserving raw data).  We fix it here at the response layer so
        the frontend always sees positive income amounts.
        """
        if self.direction == "income" and self.amount is not None and self.amount < 0:
            self.amount = abs(self.amount)
        return self


class TransactionListResponse(BaseModel):
    """Paginated transaction list."""

    items: list[TransactionOut]
    total: int
    income_total: float = 0.0
    expense_total: float = 0.0
    limit: int
    offset: int


class TransactionPatch(BaseModel):
    """Fields allowed in a PATCH /api/transactions/{id} request.

    All fields are optional; only provided fields are updated.
    Setting ``status`` to ``"confirmed"`` triggers the learning loop.
    """

    entity: str | None = None
    tax_category: str | None = None
    tax_subcategory: str | None = None
    direction: str | None = None
    status: str | None = None
    notes: str | None = None
    deductible_pct: float | None = None
    amount: float | None = None

    @field_validator("entity")
    @classmethod
    def validate_entity(cls, v: str | None) -> str | None:
        if v is not None:
            Entity(v)  # raises ValueError if invalid
        return v

    @field_validator("tax_category")
    @classmethod
    def validate_tax_category(cls, v: str | None) -> str | None:
        if v is not None:
            TaxCategory(v)
        return v

    @field_validator("tax_subcategory")
    @classmethod
    def validate_tax_subcategory(cls, v: str | None) -> str | None:
        if v is not None:
            TaxSubcategory(v)
        return v

    @field_validator("direction")
    @classmethod
    def validate_direction(cls, v: str | None) -> str | None:
        if v is not None:
            Direction(v)
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str | None) -> str | None:
        if v is not None:
            TransactionStatus(v)
        return v


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Mutable fields that PATCH is allowed to change and that should be tracked
# in the audit log.
_PATCHABLE_FIELDS = (
    "entity",
    "tax_category",
    "tax_subcategory",
    "direction",
    "status",
    "notes",
    "deductible_pct",
    "amount",
)


def _create_audit_events(
    session: Session,
    tx: Transaction,
    changes: dict[str, tuple[Any, Any]],
) -> None:
    """Insert one AuditEvent per changed field.

    Args:
        session:  Open session (caller commits).
        tx:       The transaction that was changed.
        changes:  Mapping of field_name -> (old_value, new_value).
    """
    for field_name, (old_val, new_val) in changes.items():
        event = AuditEvent(
            transaction_id=tx.id,
            field_changed=field_name,
            old_value=str(old_val) if old_val is not None else None,
            new_value=str(new_val) if new_val is not None else None,
            changed_by=ConfirmedBy.HUMAN.value,
            changed_at=datetime.now(UTC).replace(tzinfo=None),
        )
        session.add(event)


def _upsert_vendor_rule(
    session: Session,
    tx: Transaction,
) -> None:
    """Create or update a VendorRule based on the confirmed transaction.

    Learning-loop logic:
    - If a rule already exists for (vendor_pattern == description, entity),
      increment ``examples`` and update ``last_matched``.
    - Otherwise create a new rule with source="learned" and confidence=0.8
      (below the human seed rules of 0.95 but above the auto-classify
      threshold of 0.7, so it will be used immediately).

    This is only called when the transaction has both ``entity`` and
    ``tax_category`` set (required to build a useful rule).
    """
    if not tx.entity or not tx.tax_category or not tx.direction:
        logger.debug(
            "Skipping vendor rule upsert for tx %s: missing entity/category/direction",
            tx.id,
        )
        return

    # Use the description as a literal vendor_pattern (escaped for regex safety).
    # Descriptions from real receipts are vendor names like "Anthropic, PBC"
    # or "AWS" — a literal match is the most precise starting point; the human
    # can widen it later via the dashboard.
    vendor_pattern = tx.description

    existing: VendorRule | None = (
        session.query(VendorRule)
        .filter(
            VendorRule.vendor_pattern == vendor_pattern,
            VendorRule.entity == tx.entity,
        )
        .first()
    )

    now = datetime.now(UTC).replace(tzinfo=None)

    if existing is not None:
        existing.examples += 1
        existing.last_matched = now
        # Gradually increase confidence as more examples confirm the rule,
        # capped at 0.95 (reserved for human-authored seed rules).
        if existing.source == VendorRuleSource.LEARNED.value:
            existing.confidence = min(0.95, 0.80 + existing.examples * 0.01)
        logger.debug(
            "Updated vendor rule %r for entity=%s — examples=%d",
            vendor_pattern,
            tx.entity,
            existing.examples,
        )
    else:
        rule = VendorRule(
            vendor_pattern=vendor_pattern,
            entity=tx.entity,
            tax_category=tx.tax_category,
            tax_subcategory=tx.tax_subcategory,
            direction=tx.direction,
            deductible_pct=tx.deductible_pct,
            confidence=0.80,
            source=VendorRuleSource.LEARNED.value,
            examples=1,
            last_matched=now,
        )
        session.add(rule)
        logger.info(
            "Created learned vendor rule %r for entity=%s category=%s",
            vendor_pattern,
            tx.entity,
            tx.tax_category,
        )


def _flag_reimbursement_partner_if_needed(
    session: Session,
    tx: Transaction,
    changes: dict[str, tuple[Any, Any]],
) -> None:
    """If a linked transaction's amount or status changed, flag the partner.

    When a reimbursable expense or its paired reimbursement income has its
    amount edited or is rejected, the linked partner transaction is set to
    needs_review so a human can reconcile the pair.
    """
    sensitive_fields = {"amount", "status"}
    if not (sensitive_fields & changes.keys()):
        return  # No sensitive field changed — nothing to do.

    if not tx.reimbursement_link:
        return  # Not part of a reimbursement pair.

    partner: Transaction | None = (
        session.query(Transaction)
        .filter(Transaction.id == tx.reimbursement_link)
        .first()
    )
    if partner is None:
        return

    # Only flag if partner is not already in needs_review or rejected.
    if partner.status in (
        TransactionStatus.NEEDS_REVIEW.value,
        TransactionStatus.REJECTED.value,
    ):
        return

    old_partner_status = partner.status
    partner.status = TransactionStatus.NEEDS_REVIEW.value
    partner.review_reason = (
        f"Linked reimbursement transaction {tx.id[:8]} was modified "
        f"(fields changed: {', '.join(sensitive_fields & changes.keys())}). "
        "Please review and reconcile this pair."
    )
    partner.updated_at = datetime.now(UTC).replace(tzinfo=None)

    _create_audit_events(
        session,
        partner,
        {
            "status": (old_partner_status, TransactionStatus.NEEDS_REVIEW.value),
            "review_reason": (None, partner.review_reason),
        },
    )
    logger.info(
        "Flagged reimbursement partner %s as needs_review because linked tx %s changed",
        partner.id,
        tx.id,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/transactions", response_model=TransactionListResponse)
def list_transactions(
    entity: str | None = Query(default=None, description="Filter by entity"),
    status: str | None = Query(default=None, description="Filter by status"),
    direction: str | None = Query(default=None, description="Filter by direction"),
    overdue: bool = Query(
        default=False,
        description=(
            "When true (and direction=reimbursable), return only reimbursable "
            "expenses older than 30 days with no reimbursement_link"
        ),
    ),
    date_from: str | None = Query(
        default=None, description="Inclusive start date YYYY-MM-DD"
    ),
    date_to: str | None = Query(
        default=None, description="Inclusive end date YYYY-MM-DD"
    ),
    search: str | None = Query(
        default=None, description="Case-insensitive substring match on description"
    ),
    sort_by: str = Query(
        default="date",
        description="Sort column: date | amount | description",
    ),
    sort_order: str = Query(default="desc", description="asc | desc"),
    sort_dir: str = Query(default="", description="(deprecated alias for sort_order)"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db),  # noqa: B008
) -> TransactionListResponse:
    """List transactions with optional filters and pagination.

    To find overdue reimbursable expenses use:
        GET /api/transactions?direction=reimbursable&overdue=true
    Returns reimbursable expenses older than 30 days with no reimbursement_link.
    """
    try:
        # Validate enum filters early for a clear 422 response.
        if entity is not None:
            try:
                Entity(entity)
            except ValueError as exc:
                raise HTTPException(
                    status_code=422,
                    detail=f"Invalid entity value: {entity!r}",
                ) from exc
        # Support comma-separated status values (e.g. "confirmed,auto_classified")
        status_list: list[str] | None = None
        if status is not None:
            status_list = [s.strip() for s in status.split(",") if s.strip()]
            for s in status_list:
                try:
                    TransactionStatus(s)
                except ValueError as exc:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Invalid status value: {s!r}",
                    ) from exc
        if direction is not None:
            try:
                Direction(direction)
            except ValueError as exc:
                raise HTTPException(
                    status_code=422,
                    detail=f"Invalid direction value: {direction!r}",
                ) from exc

        query = session.query(Transaction)

        if entity is not None:
            query = query.filter(Transaction.entity == entity)
        if status_list is not None:
            if len(status_list) == 1:
                query = query.filter(Transaction.status == status_list[0])
            else:
                query = query.filter(Transaction.status.in_(status_list))
        if direction is not None:
            query = query.filter(Transaction.direction == direction)
        if date_from is not None:
            query = query.filter(Transaction.date >= date_from)
        if date_to is not None:
            query = query.filter(Transaction.date <= date_to)
        if search is not None:
            query = query.filter(
                func.lower(Transaction.description).contains(search.lower())
            )

        # Overdue reimbursable filter: reimbursable expenses > 30 days, unlinked.
        if overdue:
            cutoff = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%d")
            query = query.filter(
                Transaction.direction == Direction.REIMBURSABLE.value,
                Transaction.date <= cutoff,
                Transaction.reimbursement_link.is_(None),
            )

        total: int = query.count()

        # Aggregate totals across all filtered results (before pagination).
        # Use direction-based aggregation because income transactions may be
        # stored with negative amounts (raw Gmail data).  Income amounts are
        # reported as positive (abs), expenses as negative.
        _ids_subq = query.with_entities(Transaction.id)
        raw_income: float = (
            session.query(func.sum(func.abs(Transaction.amount)))
            .filter(Transaction.id.in_(_ids_subq))
            .filter(Transaction.direction == Direction.INCOME.value)
            .scalar()
        ) or 0.0
        raw_expense: float = (
            session.query(func.sum(func.abs(Transaction.amount)))
            .filter(Transaction.id.in_(_ids_subq))
            .filter(
                Transaction.direction.in_([
                    Direction.EXPENSE.value,
                    Direction.REIMBURSABLE.value,
                ])
            )
            .scalar()
        ) or 0.0
        income_total: float = raw_income
        expense_total: float = -raw_expense

        # Sorting
        sort_col_map = {
            "date": Transaction.date,
            "amount": Transaction.amount,
            "description": Transaction.description,
            "vendor": Transaction.description,
            "entity": Transaction.entity,
            "tax_category": Transaction.tax_category,
            "status": Transaction.status,
        }
        sort_col = sort_col_map.get(sort_by, Transaction.date)
        effective_dir = sort_dir if sort_dir else sort_order
        if effective_dir.lower() == "asc":
            query = query.order_by(sort_col.asc())
        else:
            query = query.order_by(sort_col.desc())

        items = query.offset(offset).limit(limit).all()

        return TransactionListResponse(
            items=[TransactionOut.model_validate(tx) for tx in items],
            total=total,
            income_total=float(income_total),
            expense_total=float(expense_total),
            limit=limit,
            offset=offset,
        )
    finally:
        session.close()


@router.get("/transactions/aggregations")
def get_aggregations(
    entity: str | None = Query(default=None, description="Filter by entity"),
    date_from: str | None = Query(default=None, description="Start date YYYY-MM-DD"),
    date_to: str | None = Query(default=None, description="End date YYYY-MM-DD"),
    session: Session = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    """Return time-series, top-vendor, and month-over-month aggregation data.

    Buckets by month when the date range exceeds 14 days, otherwise by day.
    Rejected transactions are excluded from all aggregations.
    """
    from datetime import date as _date

    try:
        query = session.query(Transaction).filter(
            Transaction.status != TransactionStatus.REJECTED.value,
        )
        if entity is not None:
            query = query.filter(Transaction.entity == entity)
        if date_from is not None:
            query = query.filter(Transaction.date >= date_from)
        if date_to is not None:
            query = query.filter(Transaction.date <= date_to)

        txns: list[Transaction] = query.all()

        # Determine bucket size
        use_day = False
        if date_from and date_to:
            try:
                d0 = _date.fromisoformat(date_from)
                d1 = _date.fromisoformat(date_to)
                if (d1 - d0).days < 14:
                    use_day = True
            except ValueError:
                pass

        def _period(tx_date: str) -> str:
            if use_day:
                return tx_date[:10]  # YYYY-MM-DD
            return tx_date[:7]  # YYYY-MM

        # Build time series + category accumulator
        income_buckets: dict[str, float] = {}
        expense_buckets: dict[str, float] = {}
        vendor_income: dict[str, float] = {}
        vendor_expense: dict[str, float] = {}
        category_totals: dict[str, float] = {}

        # Per-vendor historical amounts across ALL non-rejected expense records
        # (not filtered to current date range — used as baseline for anomaly detection)
        all_expense_q = session.query(Transaction).filter(
            Transaction.status != TransactionStatus.REJECTED.value,
            Transaction.direction.in_([Direction.EXPENSE.value, Direction.REIMBURSABLE.value]),
        )
        if entity is not None:
            all_expense_q = all_expense_q.filter(Transaction.entity == entity)
        vendor_history: dict[str, list[float]] = {}
        for htx in all_expense_q.all():
            if htx.amount is not None and htx.description:
                vendor_history.setdefault(htx.description, []).append(float(abs(htx.amount)))

        for tx in txns:
            period = _period(tx.date) if tx.date else "unknown"
            amt = float(abs(tx.amount)) if tx.amount is not None else 0.0
            if tx.direction == Direction.INCOME.value:
                income_buckets[period] = income_buckets.get(period, 0.0) + amt
                vendor_income[tx.description] = vendor_income.get(tx.description, 0.0) + amt
            elif tx.direction in (Direction.EXPENSE.value, Direction.REIMBURSABLE.value):
                expense_buckets[period] = expense_buckets.get(period, 0.0) + amt
                vendor_expense[tx.description] = vendor_expense.get(tx.description, 0.0) + amt
                cat = tx.tax_category or "OTHER"
                category_totals[cat] = category_totals.get(cat, 0.0) + amt

        time_series = {
            "income": [{"period": p, "total": t} for p, t in sorted(income_buckets.items())],
            "expenses": [{"period": p, "total": t} for p, t in sorted(expense_buckets.items())],
        }

        # Top vendors: ranked by total, capped at 5, with percentages
        def _top_vendors(vendor_map: dict[str, float]) -> list[dict[str, Any]]:
            total = sum(vendor_map.values())
            if total == 0:
                return []
            ranked = sorted(vendor_map.items(), key=lambda kv: kv[1], reverse=True)[:5]
            return [
                {"vendor": v, "total": t, "pct": round(t / total * 100, 1)}
                for v, t in ranked
            ]

        top_vendors = {
            "income": _top_vendors(vendor_income),
            "expense": _top_vendors(vendor_expense),
        }

        # Concentration warnings: flag income vendors with >80% share
        def _concentration_warnings(vendor_map: dict[str, float]) -> list[dict[str, Any]]:
            total = sum(vendor_map.values())
            if total == 0:
                return []
            warnings = []
            for vendor, amount in vendor_map.items():
                pct = amount / total * 100
                if pct > 80:
                    warnings.append({
                        "vendor": vendor,
                        "pct": round(pct, 1),
                        "message": f"{round(pct)}% of income from {vendor} — diversification risk",
                    })
            return warnings

        concentration_warnings = _concentration_warnings(vendor_income)

        # Anomaly detection: expenses in current period > 2x vendor historical avg
        anomalies: list[dict[str, Any]] = []
        for tx in txns:
            if tx.direction not in (Direction.EXPENSE.value, Direction.REIMBURSABLE.value):
                continue
            if tx.amount is None or not tx.description:
                continue
            amt = float(abs(tx.amount))
            history = vendor_history.get(tx.description, [])
            if len(history) < 2:
                continue  # not enough history for a reliable baseline
            avg = sum(history) / len(history)
            if avg > 0 and amt > 2 * avg:
                cat_label = (tx.tax_category or "").replace("_", " ").title()
                anomalies.append({
                    "tx_id": tx.id,
                    "vendor": tx.description,
                    "amount": round(amt, 2),
                    "avg_for_vendor": round(avg, 2),
                    "message": (
                        f"Unusual: ${amt:,.0f} at {tx.description}"
                        f" (avg ${avg:,.0f} for {cat_label})"
                    ),
                })
        anomalies.sort(
            key=lambda a: a["amount"] / a["avg_for_vendor"] if a["avg_for_vendor"] else 0,
            reverse=True,
        )

        # Category breakdown: top 5 expense categories with percentages
        cat_total_all = sum(category_totals.values())
        if cat_total_all > 0:
            ranked_cats = sorted(category_totals.items(), key=lambda kv: kv[1], reverse=True)[:5]
            category_breakdown: list[dict[str, Any]] = [
                {
                    "category": cat,
                    "total": round(total, 2),
                    "pct": round(total / cat_total_all * 100, 1),
                }
                for cat, total in ranked_cats
            ]
        else:
            category_breakdown = []

        # Month-over-month: compare current range to prior period of equal length
        mom: dict[str, float] = {
            "income_delta": 0.0,
            "income_pct": 0.0,
            "expense_delta": 0.0,
            "expense_pct": 0.0,
        }
        prior_category_totals: dict[str, float] = {}
        if date_from and date_to:
            try:
                d0 = _date.fromisoformat(date_from)
                d1 = _date.fromisoformat(date_to)
                span = (d1 - d0).days
                prior_from = (d0 - timedelta(days=span + 1)).isoformat()
                prior_to = (d0 - timedelta(days=1)).isoformat()

                prior_q = session.query(Transaction).filter(
                    Transaction.status != TransactionStatus.REJECTED.value,
                    Transaction.date >= prior_from,
                    Transaction.date <= prior_to,
                )
                if entity is not None:
                    prior_q = prior_q.filter(Transaction.entity == entity)

                prior_txns = prior_q.all()
                prior_income = sum(
                    float(abs(t.amount)) for t in prior_txns
                    if t.direction == Direction.INCOME.value and t.amount
                )
                prior_expense = sum(
                    float(abs(t.amount)) for t in prior_txns
                    if t.direction in (Direction.EXPENSE.value, Direction.REIMBURSABLE.value) and t.amount
                )
                for t in prior_txns:
                    if t.direction in (Direction.EXPENSE.value, Direction.REIMBURSABLE.value) and t.amount:
                        pcat = t.tax_category or "OTHER"
                        prior_category_totals[pcat] = prior_category_totals.get(pcat, 0.0) + float(abs(t.amount))
                curr_income = sum(income_buckets.values())
                curr_expense = sum(expense_buckets.values())

                mom["income_delta"] = curr_income - prior_income
                mom["expense_delta"] = curr_expense - prior_expense
                if prior_income:
                    mom["income_pct"] = round((curr_income - prior_income) / prior_income * 100, 1)
                if prior_expense:
                    mom["expense_pct"] = round((curr_expense - prior_expense) / prior_expense * 100, 1)
            except ValueError:
                pass

        expense_attribution = _build_expense_attribution(mom, category_totals, prior_category_totals)

        return {
            "time_series": time_series,
            "top_vendors": top_vendors,
            "mom_change": mom,
            "concentration_warnings": concentration_warnings,
            "anomalies": anomalies,
            "category_breakdown": category_breakdown,
            "expense_attribution": expense_attribution,
        }
    finally:
        session.close()


def _build_expense_attribution(
    mom: dict[str, float],
    curr_cats: dict[str, float],
    prior_cats: dict[str, float],
) -> str:
    """Build a human-readable sentence explaining MoM expense movement.

    Example: "Expenses up $1,200 vs prior period — mainly from Travel (+$800)"
    """
    delta = mom.get("expense_delta", 0.0)
    if delta == 0.0:
        return "No change in expenses vs prior period"

    direction_word = "up" if delta > 0 else "down"
    abs_delta = abs(delta)

    all_cats = set(curr_cats) | set(prior_cats)
    if all_cats:
        cat_deltas = {
            cat: curr_cats.get(cat, 0.0) - prior_cats.get(cat, 0.0)
            for cat in all_cats
        }
        same_sign = {
            cat: d for cat, d in cat_deltas.items()
            if (delta > 0 and d > 0) or (delta < 0 and d < 0)
        }
        if same_sign:
            top_cat, top_delta = max(same_sign.items(), key=lambda kv: abs(kv[1]))
            cat_label = top_cat.replace("_", " ").title()
            sign = "+" if top_delta > 0 else "-"
            return (
                f"Expenses {direction_word} ${abs_delta:,.0f} vs prior period"
                f" — mainly from {cat_label} ({sign}${abs(top_delta):,.0f})"
            )

    return f"Expenses {direction_word} ${abs_delta:,.0f} vs prior period"


@router.get("/transactions/review", response_model=list[TransactionOut])
def list_review_transactions(
    session: Session = Depends(get_db),  # noqa: B008
) -> list[TransactionOut]:
    """Return needs_review transactions ordered by review priority.

    Priority ordering (per design spec):
      1. Amount extraction failures (review_reason contains "Amount could not")
      2. Duplicate suspects (review_reason contains "duplicate")
      3. Low confidence (confidence < 0.5)
      4. First-time vendors / everything else
    """
    try:
        txns: list[Transaction] = (
            session.query(Transaction)
            .filter(Transaction.status == TransactionStatus.NEEDS_REVIEW.value)
            .all()
        )

        def _priority(tx: Transaction) -> tuple[int, float]:
            reason = (tx.review_reason or "").lower()
            if "amount could not" in reason:
                return (0, -tx.confidence)
            if "duplicate" in reason:
                return (1, -tx.confidence)
            if tx.confidence < 0.5:
                return (2, -tx.confidence)
            return (3, -tx.confidence)

        txns.sort(key=_priority)
        return [TransactionOut.model_validate(tx) for tx in txns]
    finally:
        session.close()


@router.get("/transactions/{transaction_id}", response_model=TransactionOut)
def get_transaction(
    transaction_id: str,
    session: Session = Depends(get_db),  # noqa: B008
) -> TransactionOut:
    """Return a single transaction by UUID."""
    try:
        tx: Transaction | None = (
            session.query(Transaction)
            .filter(Transaction.id == transaction_id)
            .first()
        )
        if tx is None:
            raise HTTPException(status_code=404, detail="Transaction not found")
        return TransactionOut.model_validate(tx)
    finally:
        session.close()


@router.patch("/transactions/{transaction_id}", response_model=TransactionOut)
def patch_transaction(
    transaction_id: str,
    body: TransactionPatch,
    session: Session = Depends(get_db),  # noqa: B008
) -> TransactionOut:
    """Update transaction fields, record audit events, and run the learning loop.

    Behaviour on ``status == "confirmed"``:
      1. Sets ``confirmed_by = "human"``.
      2. Creates AuditEvent for every changed field.
      3. Creates or updates a VendorRule (learning loop).
    """
    try:
        tx: Transaction | None = (
            session.query(Transaction)
            .filter(Transaction.id == transaction_id)
            .first()
        )
        if tx is None:
            raise HTTPException(status_code=404, detail="Transaction not found")

        # ── Tax year lock guard ──────────────────────────────────────────────
        check_lock(session, tx.entity, tx.date)

        # ── Collect changes ───────────────────────────────────────────────────
        changes: dict[str, tuple[Any, Any]] = {}
        patch_data = body.model_dump(exclude_none=True)

        for field_name in _PATCHABLE_FIELDS:
            if field_name not in patch_data:
                continue
            new_val = patch_data[field_name]
            old_val = getattr(tx, field_name)
            if str(old_val) != str(new_val):
                changes[field_name] = (old_val, new_val)
                setattr(tx, field_name, new_val)

        # ── Confirm-specific logic ────────────────────────────────────────────
        is_confirming = patch_data.get("status") == TransactionStatus.CONFIRMED.value
        is_rejecting = patch_data.get("status") == TransactionStatus.REJECTED.value

        # Guard: cannot confirm a child whose parent is rejected.
        if is_confirming and tx.parent_id is not None:
            parent_tx: Transaction | None = (
                session.query(Transaction)
                .filter(Transaction.id == tx.parent_id)
                .first()
            )
            if parent_tx is not None and parent_tx.status == TransactionStatus.REJECTED.value:
                raise HTTPException(
                    status_code=422,
                    detail="Cannot confirm a child transaction whose parent is rejected.",
                )

        if is_confirming and tx.confirmed_by != ConfirmedBy.HUMAN.value:
            old_cb = tx.confirmed_by
            tx.confirmed_by = ConfirmedBy.HUMAN.value
            changes["confirmed_by"] = (old_cb, ConfirmedBy.HUMAN.value)

        tx.updated_at = datetime.now(UTC).replace(tzinfo=None)

        # ── Audit events ──────────────────────────────────────────────────────
        if changes:
            _create_audit_events(session, tx, changes)

        # ── Learning loop ─────────────────────────────────────────────────────
        if is_confirming:
            _upsert_vendor_rule(session, tx)

        # ── Cascade reject to children when parent is rejected ────────────────
        if is_rejecting and tx.status == TransactionStatus.REJECTED.value:
            cascade_reject_children(session, tx)

        # ── Flag linked reimbursement partner when amount or status changes ──
        _flag_reimbursement_partner_if_needed(session, tx, changes)

        session.commit()
        session.refresh(tx)
        return TransactionOut.model_validate(tx)
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Split endpoint
# ---------------------------------------------------------------------------


class SplitLineItemRequest(BaseModel):
    """One line item in a split request."""

    amount: Decimal
    entity: str | None = None
    tax_category: str | None = None
    description: str | None = None

    @field_validator("entity")
    @classmethod
    def validate_entity(cls, v: str | None) -> str | None:
        if v is not None:
            Entity(v)
        return v

    @field_validator("tax_category")
    @classmethod
    def validate_tax_category(cls, v: str | None) -> str | None:
        if v is not None:
            TaxCategory(v)
        return v


class SplitRequest(BaseModel):
    """Body for POST /api/transactions/{id}/split."""

    line_items: list[SplitLineItemRequest]


class HotelSuggestionOut(BaseModel):
    """Hotel split suggestion returned alongside a split_parent transaction."""

    room_amount: str
    meals_amount: str
    entity: str | None
    line_items: list[dict[str, Any]]


class SplitResponse(BaseModel):
    """Response body for POST /api/transactions/{id}/split."""

    parent: TransactionOut
    children: list[TransactionOut]
    hotel_suggestion: HotelSuggestionOut | None = None


@router.post(
    "/transactions/{transaction_id}/split",
    response_model=SplitResponse,
    status_code=201,
)
def split_transaction_endpoint(
    transaction_id: str,
    body: SplitRequest,
    session: Session = Depends(get_db),  # noqa: B008
) -> SplitResponse:
    """Split a transaction into child line items.

    Rules:
    - Parent must not already be split_parent (422 if so).
    - Parent must not be rejected.
    - Line item amounts must sum to parent total (422 if not).
    - On success: parent status → split_parent; children created.
    - AuditEvent rows are created for all changes.
    - If the parent description looks like a hotel, a split suggestion is
      returned in ``hotel_suggestion`` for the UI to pre-populate.
    """
    try:
        tx: Transaction | None = (
            session.query(Transaction)
            .filter(Transaction.id == transaction_id)
            .first()
        )
        if tx is None:
            raise HTTPException(status_code=404, detail="Transaction not found")

        # ── Tax year lock guard ──────────────────────────────────────────────
        check_lock(session, tx.entity, tx.date)

        # Cannot re-split an already-split parent.
        if tx.status == TransactionStatus.SPLIT_PARENT.value:
            raise HTTPException(
                status_code=422,
                detail="Transaction is already split. Cannot re-split a split_parent.",
            )

        # Cannot split a rejected transaction.
        if tx.status == TransactionStatus.REJECTED.value:
            raise HTTPException(
                status_code=422,
                detail="Cannot split a rejected transaction.",
            )

        # Children cannot themselves be split.
        if tx.parent_id is not None:
            raise HTTPException(
                status_code=422,
                detail="Cannot split a child transaction. Only top-level transactions can be split.",
            )

        # Build SplitLineItem list.
        line_items = [
            SplitLineItem(
                amount=item.amount,
                entity=item.entity,
                tax_category=item.tax_category,
                description=item.description,
            )
            for item in body.line_items
        ]

        # Validate amounts.
        parent_amount = Decimal(str(tx.amount)) if tx.amount is not None else None
        if parent_amount is None:
            raise HTTPException(
                status_code=422,
                detail="Cannot split a transaction with an unknown amount.",
            )

        try:
            validate_split_amounts(parent_amount, line_items)
        except SplitValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        # Perform the split.
        result = split_transaction(session, tx, line_items)
        session.commit()
        session.refresh(tx)
        for child in result.children:
            session.refresh(child)

        # Hotel suggestion (for UI pre-population on future reference).
        hotel_suggestion: HotelSuggestionOut | None = None
        suggestion = suggest_hotel_splits(tx)
        if suggestion is not None:
            hotel_suggestion = HotelSuggestionOut(
                room_amount=str(suggestion.room_amount),
                meals_amount=str(suggestion.meals_amount),
                entity=suggestion.entity,
                line_items=[
                    {
                        "amount": str(item.amount),
                        "entity": item.entity,
                        "tax_category": item.tax_category,
                        "description": item.description,
                    }
                    for item in suggestion.as_line_items
                ],
            )

        return SplitResponse(
            parent=TransactionOut.model_validate(tx),
            children=[TransactionOut.model_validate(c) for c in result.children],
            hotel_suggestion=hotel_suggestion,
        )
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Receipt extraction endpoint
# ---------------------------------------------------------------------------


class ExtractReceiptRequest(BaseModel):
    """Body for POST /api/transactions/{id}/extract-receipt."""

    attachment_index: int = 0


class ExtractReceiptResponse(BaseModel):
    """Response body for POST /api/transactions/{id}/extract-receipt."""

    transaction: TransactionOut
    extraction: dict[str, Any]
    fields_updated: list[str]


@router.post(
    "/transactions/{transaction_id}/extract-receipt",
    response_model=ExtractReceiptResponse,
)
def extract_transaction_receipt(
    transaction_id: str,
    body: ExtractReceiptRequest,
    session: Session = Depends(get_db),  # noqa: B008
) -> ExtractReceiptResponse:
    """Run Claude CLI vision on an attachment to extract receipt data."""
    from src.utils.receipt_ocr import extract_receipt, find_extractable_attachments

    tx: Transaction | None = (
        session.query(Transaction)
        .filter(Transaction.id == transaction_id)
        .first()
    )
    if tx is None:
        raise HTTPException(status_code=404, detail="Transaction not found")

    all_attachments: list[str] = tx.attachments or []
    extractable = find_extractable_attachments(all_attachments)

    if not extractable:
        raise HTTPException(
            status_code=422,
            detail="No image or PDF attachments to extract from.",
        )

    if body.attachment_index < 0 or body.attachment_index >= len(extractable):
        raise HTTPException(
            status_code=422,
            detail=f"attachment_index {body.attachment_index} out of range ({len(extractable)} available).",
        )

    ocr = extract_receipt(extractable[body.attachment_index])
    if not ocr.success:
        raise HTTPException(status_code=500, detail=ocr.error or "Extraction failed")

    extracted = ocr.raw_response

    # Apply extracted fields
    fields_updated: list[str] = []
    now = datetime.now(UTC).replace(tzinfo=None)

    vendor = ocr.vendor
    if vendor and (not tx.description or tx.description in ("", "Travis Sparks")):
        tx.description = vendor
        fields_updated.append("description")

    if ocr.amount is not None and tx.amount is None:
        tx.amount = -abs(ocr.amount)
        fields_updated.append("amount")

    if ocr.date and (not tx.date or tx.date == ""):
        tx.date = ocr.date
        fields_updated.append("date")

    if ocr.entity_hint and not tx.entity:
        tx.entity = ocr.entity_hint
        fields_updated.append("entity")

    # Store extraction in notes for audit
    import json as _json
    attachment_name = Path(extractable[body.attachment_index]).name
    audit = f"[Claude CLI extraction from {attachment_name}]\n{_json.dumps(extracted, indent=2)}"
    tx.notes = (tx.notes + "\n\n" + audit) if tx.notes else audit
    fields_updated.append("notes")

    tx.updated_at = now
    _create_audit_events(
        session, tx,
        {f: (None, getattr(tx, f)) for f in fields_updated if f != "notes"},
    )
    session.commit()
    session.refresh(tx)

    return ExtractReceiptResponse(
        transaction=TransactionOut.model_validate(tx),
        extraction=extracted,
        fields_updated=fields_updated,
    )


# ---------------------------------------------------------------------------
# Reimbursement link endpoint
# ---------------------------------------------------------------------------


class LinkReimbursementRequest(BaseModel):
    """Body for POST /api/transactions/{id}/link-reimbursement."""

    reimbursement_id: str


class LinkReimbursementResponse(BaseModel):
    """Response body for POST /api/transactions/{id}/link-reimbursement."""

    expense: TransactionOut
    reimbursement: TransactionOut


@router.post(
    "/transactions/{transaction_id}/link-reimbursement",
    response_model=LinkReimbursementResponse,
    status_code=200,
)
def link_reimbursement(
    transaction_id: str,
    body: LinkReimbursementRequest,
    session: Session = Depends(get_db),  # noqa: B008
) -> LinkReimbursementResponse:
    """Link a reimbursable expense to its matching reimbursement income transaction.

    Validation rules:
    - The expense transaction must have direction=reimbursable or direction=expense.
    - The reimbursement transaction must have direction=income.
    - Both transactions must exist and cannot be the same transaction.
    - The link is set bidirectionally: expense.reimbursement_link = reimbursement.id
      and reimbursement.reimbursement_link = expense.id.
    - AuditEvent rows are created for both transactions.

    When linked, both transactions net to zero on P&L because tax summary
    queries exclude linked pairs (expense is negative, income is positive,
    and they are matched 1-to-1).
    """
    try:
        if transaction_id == body.reimbursement_id:
            raise HTTPException(
                status_code=422,
                detail="A transaction cannot be linked to itself.",
            )

        expense_tx: Transaction | None = (
            session.query(Transaction)
            .filter(Transaction.id == transaction_id)
            .first()
        )
        if expense_tx is None:
            raise HTTPException(status_code=404, detail="Expense transaction not found")

        reimb_tx: Transaction | None = (
            session.query(Transaction)
            .filter(Transaction.id == body.reimbursement_id)
            .first()
        )
        if reimb_tx is None:
            raise HTTPException(
                status_code=404, detail="Reimbursement transaction not found"
            )

        # ── Direction validation ───────────────────────────────────────────────
        valid_expense_directions = {
            Direction.REIMBURSABLE.value,
            Direction.EXPENSE.value,
        }
        if expense_tx.direction not in valid_expense_directions:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Expense transaction direction must be 'reimbursable' or 'expense', "
                    f"got {expense_tx.direction!r}."
                ),
            )
        if reimb_tx.direction != Direction.INCOME.value:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Reimbursement transaction direction must be 'income', "
                    f"got {reimb_tx.direction!r}."
                ),
            )

        # ── Bidirectional link ────────────────────────────────────────────────
        now = datetime.now(UTC).replace(tzinfo=None)

        old_expense_link = expense_tx.reimbursement_link
        old_reimb_link = reimb_tx.reimbursement_link

        expense_tx.reimbursement_link = reimb_tx.id
        expense_tx.updated_at = now

        reimb_tx.reimbursement_link = expense_tx.id
        reimb_tx.updated_at = now

        # ── Audit events ──────────────────────────────────────────────────────
        _create_audit_events(
            session,
            expense_tx,
            {
                "reimbursement_link": (old_expense_link, reimb_tx.id),
            },
        )
        _create_audit_events(
            session,
            reimb_tx,
            {
                "reimbursement_link": (old_reimb_link, expense_tx.id),
            },
        )

        session.commit()
        session.refresh(expense_tx)
        session.refresh(reimb_tx)

        logger.info(
            "Linked reimbursement: expense=%s <-> reimbursement=%s",
            expense_tx.id,
            reimb_tx.id,
        )

        return LinkReimbursementResponse(
            expense=TransactionOut.model_validate(expense_tx),
            reimbursement=TransactionOut.model_validate(reimb_tx),
        )
    finally:
        session.close()
