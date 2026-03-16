"""Transaction endpoints.

GET  /api/transactions          — Paginated, filtered list.
GET  /api/transactions/review   — needs_review items ordered by priority.
GET  /api/transactions/{id}     — Single transaction.
PATCH /api/transactions/{id}    — Update fields + learning loop on confirm.
POST /api/transactions/{id}/extract-receipt — OCR an attachment via Claude Vision.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import func
from sqlalchemy.orm import Session

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


def get_db() -> Session:
    """Return a database session. Caller must close it."""
    session = SessionLocal()
    try:
        return session
    except Exception:
        session.close()
        raise


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
    amount: Any  # Decimal serialised as string to avoid float precision loss
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
    payment_method: str | None = None
    notes: str | None
    confirmed_by: str
    created_at: datetime
    updated_at: datetime
    raw_data: Any | None = None
    attachments: Any | None = None

    @field_validator("amount", mode="before")
    @classmethod
    def coerce_amount(cls, v: Any) -> str:
        """Serialise Decimal / float to a plain string to preserve precision."""
        if isinstance(v, Decimal):
            return str(v)
        return str(v) if v is not None else "0"


class TransactionListResponse(BaseModel):
    """Paginated transaction list."""

    items: list[TransactionOut]
    total: int
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


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/transactions", response_model=TransactionListResponse)
def list_transactions(
    entity: str | None = Query(default=None, description="Filter by entity"),
    status: str | None = Query(default=None, description="Filter by status"),
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
    sort_dir: str = Query(default="desc", description="asc | desc"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db),  # noqa: B008
) -> TransactionListResponse:
    """List transactions with optional filters and pagination."""
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
        if status is not None:
            try:
                TransactionStatus(status)
            except ValueError as exc:
                raise HTTPException(
                    status_code=422,
                    detail=f"Invalid status value: {status!r}",
                ) from exc

        query = session.query(Transaction)

        if entity is not None:
            query = query.filter(Transaction.entity == entity)
        if status is not None:
            query = query.filter(Transaction.status == status)
        if date_from is not None:
            query = query.filter(Transaction.date >= date_from)
        if date_to is not None:
            query = query.filter(Transaction.date <= date_to)
        if search is not None:
            query = query.filter(
                func.lower(Transaction.description).contains(search.lower())
            )

        total: int = query.count()

        # Sorting
        sort_col_map = {
            "date": Transaction.date,
            "amount": Transaction.amount,
            "description": Transaction.description,
        }
        sort_col = sort_col_map.get(sort_by, Transaction.date)
        if sort_dir.lower() == "asc":
            query = query.order_by(sort_col.asc())
        else:
            query = query.order_by(sort_col.desc())

        items = query.offset(offset).limit(limit).all()

        return TransactionListResponse(
            items=[TransactionOut.model_validate(tx) for tx in items],
            total=total,
            limit=limit,
            offset=offset,
        )
    finally:
        session.close()


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

        session.commit()
        session.refresh(tx)
        return TransactionOut.model_validate(tx)
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
