"""Vendor rule CRUD endpoints.

GET    /api/vendor-rules           — Paginated list with optional search.
GET    /api/vendor-rules/{id}      — Single rule + match_count + last 5 matched txns.
POST   /api/vendor-rules           — Create a new rule.
PATCH  /api/vendor-rules/{id}      — Update rule fields.
DELETE /api/vendor-rules/{id}      — Delete rule.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func
from collections.abc import Generator

from sqlalchemy.orm import Session

from src.db.connection import SessionLocal
from src.models.enums import Direction, Entity, TaxCategory, TaxSubcategory, VendorRuleSource
from src.models.transaction import Transaction
from src.models.vendor_rule import VendorRule

logger = logging.getLogger(__name__)

router = APIRouter(tags=["vendor-rules"])


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
# Schemas
# ---------------------------------------------------------------------------


class VendorRuleOut(BaseModel):
    """Full vendor rule response schema."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    vendor_pattern: str
    entity: str
    tax_category: str
    tax_subcategory: str | None
    direction: str
    deductible_pct: float
    confidence: float
    source: str
    examples: int
    last_matched: datetime | None
    created_at: datetime


class VendorRuleWithMatchesOut(VendorRuleOut):
    """Single rule response that includes match statistics."""

    match_count: int
    last_matches: list[dict[str, Any]]


class VendorRuleListResponse(BaseModel):
    """Paginated vendor rule list."""

    items: list[VendorRuleOut]
    total: int
    limit: int
    offset: int


class VendorRuleCreate(BaseModel):
    """Fields required to create a new vendor rule."""

    vendor_pattern: str
    entity: str
    tax_category: str
    tax_subcategory: str | None = None
    direction: str
    deductible_pct: float = Field(default=1.0, ge=0, le=1)
    confidence: float = Field(default=1.0, ge=0, le=1)
    source: str = VendorRuleSource.HUMAN.value

    def validate_enums(self) -> None:
        Entity(self.entity)
        TaxCategory(self.tax_category)
        if self.tax_subcategory is not None:
            TaxSubcategory(self.tax_subcategory)
        Direction(self.direction)
        VendorRuleSource(self.source)


class VendorRulePatch(BaseModel):
    """Fields allowed in a PATCH request. All optional."""

    vendor_pattern: str | None = None
    entity: str | None = None
    tax_category: str | None = None
    tax_subcategory: str | None = None
    direction: str | None = None
    deductible_pct: float | None = Field(default=None, ge=0, le=1)
    confidence: float | None = Field(default=None, ge=0, le=1)

    def validate_enums(self) -> None:
        if self.entity is not None:
            Entity(self.entity)
        if self.tax_category is not None:
            TaxCategory(self.tax_category)
        if self.tax_subcategory is not None:
            TaxSubcategory(self.tax_subcategory)
        if self.direction is not None:
            Direction(self.direction)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PATCHABLE = (
    "vendor_pattern",
    "entity",
    "tax_category",
    "tax_subcategory",
    "direction",
    "deductible_pct",
    "confidence",
)


def _match_count(session: Session, rule: VendorRule) -> int:
    """COUNT of transactions whose description LIKE vendor_pattern."""
    return (
        session.query(func.count(Transaction.id))
        .filter(
            func.lower(Transaction.description).contains(rule.vendor_pattern.lower())
        )
        .scalar()
        or 0
    )


def _last_matches(session: Session, rule: VendorRule) -> list[dict[str, Any]]:
    """Return the 5 most recent transactions matching the vendor pattern."""
    txns = (
        session.query(Transaction)
        .filter(
            func.lower(Transaction.description).contains(rule.vendor_pattern.lower())
        )
        .order_by(Transaction.date.desc(), Transaction.created_at.desc())
        .limit(5)
        .all()
    )
    return [
        {
            "id": tx.id,
            "date": tx.date,
            "description": tx.description,
            "amount": str(tx.amount) if tx.amount is not None else None,
            "entity": tx.entity,
            "tax_category": tx.tax_category,
            "status": tx.status,
        }
        for tx in txns
    ]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/vendor-rules", response_model=VendorRuleListResponse)
def list_vendor_rules(
    search: str | None = Query(
        default=None, description="Filter by vendor_pattern substring (case-insensitive)"
    ),
    entity: str | None = Query(default=None, description="Filter by entity"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db),  # noqa: B008
) -> VendorRuleListResponse:
    """List vendor rules with optional search and pagination."""
    try:
        query = session.query(VendorRule)

        if search is not None:
            query = query.filter(
                func.lower(VendorRule.vendor_pattern).contains(search.lower())
            )
        if entity is not None:
            try:
                Entity(entity)
            except ValueError as exc:
                raise HTTPException(
                    status_code=422, detail=f"Invalid entity value: {entity!r}"
                ) from exc
            query = query.filter(VendorRule.entity == entity)

        total: int = query.count()
        rules = (
            query.order_by(VendorRule.examples.desc(), VendorRule.confidence.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

        return VendorRuleListResponse(
            items=[VendorRuleOut.model_validate(r) for r in rules],
            total=total,
            limit=limit,
            offset=offset,
        )
    finally:
        session.close()


@router.get("/vendor-rules/{rule_id}", response_model=VendorRuleWithMatchesOut)
def get_vendor_rule(
    rule_id: str,
    session: Session = Depends(get_db),  # noqa: B008
) -> VendorRuleWithMatchesOut:
    """Return a single vendor rule with match count and last 5 matched transactions."""
    try:
        rule: VendorRule | None = (
            session.query(VendorRule).filter(VendorRule.id == rule_id).first()
        )
        if rule is None:
            raise HTTPException(status_code=404, detail="Vendor rule not found")

        count = _match_count(session, rule)
        matches = _last_matches(session, rule)

        base = VendorRuleOut.model_validate(rule)
        return VendorRuleWithMatchesOut(
            **base.model_dump(),
            match_count=count,
            last_matches=matches,
        )
    finally:
        session.close()


@router.post("/vendor-rules", response_model=VendorRuleOut, status_code=201)
def create_vendor_rule(
    body: VendorRuleCreate,
    session: Session = Depends(get_db),  # noqa: B008
) -> VendorRuleOut:
    """Create a new vendor rule."""
    try:
        try:
            body.validate_enums()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        rule = VendorRule(
            vendor_pattern=body.vendor_pattern,
            entity=body.entity,
            tax_category=body.tax_category,
            tax_subcategory=body.tax_subcategory,
            direction=body.direction,
            deductible_pct=body.deductible_pct,
            confidence=body.confidence,
            source=body.source,
            examples=1,
            last_matched=None,
            created_at=datetime.now(UTC).replace(tzinfo=None),
        )
        session.add(rule)
        session.commit()
        session.refresh(rule)
        logger.info(
            "Created vendor rule %r entity=%s category=%s",
            rule.vendor_pattern,
            rule.entity,
            rule.tax_category,
        )
        return VendorRuleOut.model_validate(rule)
    finally:
        session.close()


@router.patch("/vendor-rules/{rule_id}", response_model=VendorRuleOut)
def patch_vendor_rule(
    rule_id: str,
    body: VendorRulePatch,
    session: Session = Depends(get_db),  # noqa: B008
) -> VendorRuleOut:
    """Update vendor rule fields."""
    try:
        rule: VendorRule | None = (
            session.query(VendorRule).filter(VendorRule.id == rule_id).first()
        )
        if rule is None:
            raise HTTPException(status_code=404, detail="Vendor rule not found")

        try:
            body.validate_enums()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        patch_data = body.model_dump(exclude_none=True)
        for field in _PATCHABLE:
            if field in patch_data:
                setattr(rule, field, patch_data[field])

        session.commit()
        session.refresh(rule)
        logger.info("Updated vendor rule %s: %s", rule_id, list(patch_data.keys()))
        return VendorRuleOut.model_validate(rule)
    finally:
        session.close()


@router.delete("/vendor-rules/{rule_id}", status_code=204)
def delete_vendor_rule(
    rule_id: str,
    session: Session = Depends(get_db),  # noqa: B008
) -> None:
    """Delete a vendor rule by ID."""
    try:
        rule: VendorRule | None = (
            session.query(VendorRule).filter(VendorRule.id == rule_id).first()
        )
        if rule is None:
            raise HTTPException(status_code=404, detail="Vendor rule not found")

        session.delete(rule)
        session.commit()
        logger.info("Deleted vendor rule %s (%r)", rule_id, rule.vendor_pattern)
    finally:
        session.close()
