"""Tax document endpoints.

GET    /api/tax-documents                  — List active documents, filterable by year + entity.
GET    /api/tax-documents/summary          — Filing-ready summary report (JSON).
GET    /api/tax-documents/{id}             — Single document detail.
POST   /api/tax-documents                  — Create new document.
PATCH  /api/tax-documents/{id}             — Partial update.
DELETE /api/tax-documents/{id}             — Soft delete (set status=inactive).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.api.deps import get_db
from src.api.routes.tax_year_locks import check_lock
from src.models.enums import Entity, TaxDocumentStatus, TaxFormType
from src.models.tax_document import TaxDocument

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tax-documents"])

# ---------------------------------------------------------------------------
# Amounts validation: allowed keys and primary (total_amount) key per form type
# ---------------------------------------------------------------------------

VALID_AMOUNT_KEYS: dict[str, tuple[set[str], str]] = {
    "1099-NEC": (
        {"box_1_nonemployee_comp", "box_4_federal_tax_withheld"},
        "box_1_nonemployee_comp",
    ),
    "1099-INT": (
        {"box_1_interest", "box_3_savings_bond_interest", "box_4_federal_tax_withheld"},
        "box_1_interest",
    ),
    "1099-DIV": (
        {"box_1a_ordinary_dividends", "box_1b_qualified_dividends", "box_2a_capital_gain"},
        "box_1a_ordinary_dividends",
    ),
    "1099-B": (
        {"proceeds", "cost_basis", "gain_loss", "short_term_count", "long_term_count"},
        "gain_loss",
    ),
    "1099-K": (
        {"box_1a_gross_amount", "box_1b_card_not_present"},
        "box_1a_gross_amount",
    ),
    "K-1": (
        {"box_1_ordinary_income", "box_14_se_earnings", "box_16_foreign_transactions"},
        "box_1_ordinary_income",
    ),
    "1098": (
        {"box_1_mortgage_interest", "box_2_outstanding_principal", "box_5_property_tax"},
        "box_1_mortgage_interest",
    ),
    "PROPERTY_TAX": (
        {"assessed_value", "tax_amount", "year"},
        "tax_amount",
    ),
    "OTHER": (
        set(),  # Any keys are permitted for OTHER
        "",
    ),
}

# IRS line mapping for summary report
IRS_LINE_MAPPING: dict[str, str] = {
    "1099-NEC": "Schedule C / Line 1",
    "1099-INT": "Schedule B / Line 1",
    "1099-DIV": "Schedule B / Line 5",
    "1099-B": "Schedule D + Form 8949",
    "1099-K": "Schedule C / Line 1",
    "K-1": "Schedule E Part II",
    "1098": "Schedule A / Line 8a",
    "PROPERTY_TAX": "Schedule A / SALT",
    "OTHER": "See notes",
}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


def _validate_source_file(v: str | None) -> str | None:
    """Reject source_file values with path traversal or absolute paths."""
    if v is None:
        return v
    if ".." in v:
        raise ValueError("source_file must not contain '..'")
    if v.startswith("/"):
        raise ValueError("source_file must be a relative path")
    if not v.startswith("data/"):
        raise ValueError("source_file must start with 'data/'")
    return v


class TaxDocumentCreate(BaseModel):
    """Request body for POST /api/tax-documents."""

    tax_year: int
    form_type: str
    entity: str
    payer_name: str
    payer_ein: str | None = None
    recipient_name: str
    recipient_tin_last4: str | None = None
    amounts: dict[str, Any]
    total_amount: Decimal | None = None  # Computed from amounts if omitted
    source_file: str | None = None
    notes: str | None = None

    @field_validator("tax_year")
    @classmethod
    def validate_tax_year(cls, v: int) -> int:
        if v < 2000 or v > 2100:
            raise ValueError(f"tax_year must be between 2000 and 2100, got {v}")
        return v

    @field_validator("form_type")
    @classmethod
    def validate_form_type(cls, v: str) -> str:
        TaxFormType(v)  # raises ValueError if invalid
        return v

    @field_validator("entity")
    @classmethod
    def validate_entity(cls, v: str) -> str:
        Entity(v)  # raises ValueError if invalid
        return v

    @field_validator("source_file")
    @classmethod
    def validate_source_file(cls, v: str | None) -> str | None:
        return _validate_source_file(v)

    @model_validator(mode="after")
    def validate_amounts_and_total(self) -> TaxDocumentCreate:
        """Validate amounts keys against form_type and compute total_amount."""
        form_type_val = self.form_type
        if form_type_val not in VALID_AMOUNT_KEYS:
            # form_type validator already caught unknown values; skip if OTHER
            return self

        allowed_keys, primary_key = VALID_AMOUNT_KEYS[form_type_val]

        # For OTHER, skip key validation
        if form_type_val != "OTHER":
            unknown_keys = set(self.amounts.keys()) - allowed_keys
            if unknown_keys:
                raise ValueError(
                    f"Unknown amounts keys for {form_type_val}: {sorted(unknown_keys)}. "
                    f"Allowed: {sorted(allowed_keys)}"
                )
            if primary_key and primary_key not in self.amounts:
                raise ValueError(
                    f"amounts must include primary key '{primary_key}' for form_type={form_type_val}"
                )

        # Compute total_amount from primary key if not provided
        if self.total_amount is None and primary_key and primary_key in self.amounts:
            self.total_amount = Decimal(str(self.amounts[primary_key]))

        if self.total_amount is None:
            raise ValueError("total_amount is required and could not be derived from amounts")

        return self


class TaxDocumentPatch(BaseModel):
    """Request body for PATCH /api/tax-documents/{id}.

    All fields optional; only provided fields are updated.
    If amounts or form_type changes, amounts are re-validated.
    """

    tax_year: int | None = None
    form_type: str | None = None
    entity: str | None = None
    payer_name: str | None = None
    payer_ein: str | None = None
    recipient_name: str | None = None
    recipient_tin_last4: str | None = None
    amounts: dict[str, Any] | None = None
    total_amount: Decimal | None = None
    source_file: str | None = None
    notes: str | None = None

    @field_validator("tax_year")
    @classmethod
    def validate_tax_year(cls, v: int | None) -> int | None:
        if v is not None and (v < 2000 or v > 2100):
            raise ValueError(f"tax_year must be between 2000 and 2100, got {v}")
        return v

    @field_validator("form_type")
    @classmethod
    def validate_form_type(cls, v: str | None) -> str | None:
        if v is not None:
            TaxFormType(v)
        return v

    @field_validator("entity")
    @classmethod
    def validate_entity(cls, v: str | None) -> str | None:
        if v is not None:
            Entity(v)
        return v

    @field_validator("source_file")
    @classmethod
    def validate_source_file(cls, v: str | None) -> str | None:
        return _validate_source_file(v)


class TaxDocumentOut(BaseModel):
    """Full tax document response schema."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    tax_year: int
    form_type: str
    entity: str
    payer_name: str
    payer_ein: str | None
    recipient_name: str
    recipient_tin_last4: str | None
    amounts: Any
    total_amount: float | None = None
    source_file: str | None
    notes: str | None
    status: str
    created_at: datetime
    updated_at: datetime

    @field_validator("total_amount", mode="before")
    @classmethod
    def coerce_total_amount(cls, v: Any) -> float | None:
        if v is None:
            return None
        return float(v)


class TaxDocumentListResponse(BaseModel):
    """List response for tax documents."""

    items: list[TaxDocumentOut]
    count: int


# ---------------------------------------------------------------------------
# Summary schema
# ---------------------------------------------------------------------------


class TaxDocumentSummaryItem(BaseModel):
    """One row in the filing-ready summary."""

    id: str
    form_type: str
    payer_name: str
    total_amount: float | None
    tax_year: int
    entity: str
    irs_line: str
    source_file: str | None
    notes: str | None


class TaxDocumentSummaryResponse(BaseModel):
    """Filing-ready summary grouped by entity."""

    tax_year: int
    by_entity: dict[str, list[TaxDocumentSummaryItem]]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/tax-documents/summary", response_model=TaxDocumentSummaryResponse)
def get_tax_documents_summary(
    year: int = Query(..., description="Tax year, e.g. 2025"),
    session: Session = Depends(get_db),  # noqa: B008
) -> TaxDocumentSummaryResponse:
    """Return a filing-ready summary grouped by entity with IRS line mappings.

    Only active documents for the given year are included.
    """
    docs: list[TaxDocument] = (
        session.query(TaxDocument)
        .filter(
            TaxDocument.tax_year == year,
            TaxDocument.status == TaxDocumentStatus.ACTIVE.value,
        )
        .order_by(TaxDocument.entity, TaxDocument.form_type)
        .all()
    )

    by_entity: dict[str, list[TaxDocumentSummaryItem]] = {}
    for doc in docs:
        item = TaxDocumentSummaryItem(
            id=doc.id,
            form_type=doc.form_type,
            payer_name=doc.payer_name,
            total_amount=float(doc.total_amount) if doc.total_amount is not None else None,
            tax_year=doc.tax_year,
            entity=doc.entity,
            irs_line=IRS_LINE_MAPPING.get(doc.form_type, "See notes"),
            source_file=doc.source_file,
            notes=doc.notes,
        )
        by_entity.setdefault(doc.entity, []).append(item)

    return TaxDocumentSummaryResponse(tax_year=year, by_entity=by_entity)


@router.get("/tax-documents", response_model=TaxDocumentListResponse)
def list_tax_documents(
    year: int = Query(..., description="Tax year (required), e.g. 2025"),
    entity: str | None = Query(default=None, description="Filter by entity"),
    session: Session = Depends(get_db),  # noqa: B008
) -> TaxDocumentListResponse:
    """List active tax documents for a given year, optionally filtered by entity."""
    if entity is not None:
        try:
            Entity(entity)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid entity value: {entity!r}",
            ) from exc

    query = (
        session.query(TaxDocument)
        .filter(
            TaxDocument.tax_year == year,
            TaxDocument.status == TaxDocumentStatus.ACTIVE.value,
        )
    )
    if entity is not None:
        query = query.filter(TaxDocument.entity == entity)

    docs: list[TaxDocument] = query.order_by(TaxDocument.entity, TaxDocument.form_type).all()
    return TaxDocumentListResponse(
        items=[TaxDocumentOut.model_validate(d) for d in docs],
        count=len(docs),
    )


@router.get("/tax-documents/{doc_id}", response_model=TaxDocumentOut)
def get_tax_document(
    doc_id: str,
    session: Session = Depends(get_db),  # noqa: B008
) -> TaxDocumentOut:
    """Return a single tax document by ID, or 404 if not found."""
    doc: TaxDocument | None = (
        session.query(TaxDocument).filter(TaxDocument.id == doc_id).first()
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="Tax document not found")
    return TaxDocumentOut.model_validate(doc)


@router.post("/tax-documents", response_model=TaxDocumentOut, status_code=201)
def create_tax_document(
    body: TaxDocumentCreate,
    session: Session = Depends(get_db),  # noqa: B008
) -> TaxDocumentOut:
    """Create a new tax document.

    Returns 403 if the tax year is locked for the entity.
    Returns 409 if a document with the same (tax_year, form_type, entity, payer_ein)
    already exists.
    """
    check_lock(session, body.entity, f"{body.tax_year}-01-01")

    doc = TaxDocument(
        tax_year=body.tax_year,
        form_type=body.form_type,
        entity=body.entity,
        payer_name=body.payer_name,
        payer_ein=body.payer_ein,
        recipient_name=body.recipient_name,
        recipient_tin_last4=body.recipient_tin_last4,
        amounts=body.amounts,
        total_amount=body.total_amount,
        source_file=body.source_file,
        notes=body.notes,
        status=TaxDocumentStatus.ACTIVE.value,
        created_at=datetime.now(UTC).replace(tzinfo=None),
        updated_at=datetime.now(UTC).replace(tzinfo=None),
    )
    session.add(doc)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail="A tax document with the same year, form type, entity, and payer already exists.",
        ) from exc
    session.refresh(doc)
    logger.info(
        "Created tax document: id=%s year=%d form=%s entity=%s payer=%r",
        doc.id,
        doc.tax_year,
        doc.form_type,
        doc.entity,
        doc.payer_name,
    )
    return TaxDocumentOut.model_validate(doc)


@router.patch("/tax-documents/{doc_id}", response_model=TaxDocumentOut)
def patch_tax_document(
    doc_id: str,
    body: TaxDocumentPatch,
    session: Session = Depends(get_db),  # noqa: B008
) -> TaxDocumentOut:
    """Partially update a tax document.

    If amounts or form_type changes, revalidates amounts against the (new) form_type.
    Returns 403 if the tax year is locked for the entity.
    """
    doc: TaxDocument | None = (
        session.query(TaxDocument).filter(TaxDocument.id == doc_id).first()
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="Tax document not found")

    # Check lock for current entity+year
    check_lock(session, doc.entity, f"{doc.tax_year}-01-01")

    # If entity or tax_year is changing, also check the new target
    new_entity = body.entity if body.entity is not None else doc.entity
    new_tax_year = body.tax_year if body.tax_year is not None else doc.tax_year
    if new_entity != doc.entity or new_tax_year != doc.tax_year:
        check_lock(session, new_entity, f"{new_tax_year}-01-01")

    # Determine effective form_type and amounts for cross-field validation
    effective_form_type = body.form_type if body.form_type is not None else doc.form_type
    effective_amounts = body.amounts if body.amounts is not None else None

    # Re-validate amounts if either amounts or form_type is changing
    recomputed_total: Decimal | None = None
    if effective_amounts is not None or body.form_type is not None:
        check_amounts = effective_amounts if effective_amounts is not None else doc.amounts
        if effective_form_type in VALID_AMOUNT_KEYS:
            allowed_keys, primary_key = VALID_AMOUNT_KEYS[effective_form_type]
            if effective_form_type != "OTHER":
                unknown_keys = set(check_amounts.keys()) - allowed_keys
                if unknown_keys:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"Unknown amounts keys for {effective_form_type}: "
                            f"{sorted(unknown_keys)}. Allowed: {sorted(allowed_keys)}"
                        ),
                    )
                if primary_key and primary_key not in check_amounts:
                    raise HTTPException(
                        status_code=422,
                        detail=f"amounts must include primary key '{primary_key}' for form_type={effective_form_type}",
                    )
            # Recompute total_amount if amounts changed and caller didn't override it
            if effective_amounts is not None and body.total_amount is None and primary_key:
                primary_val = check_amounts.get(primary_key)
                if primary_val is not None:
                    recomputed_total = Decimal(str(primary_val))

    # Apply updates
    update_fields = body.model_dump(exclude_unset=True)
    for field, value in update_fields.items():
        setattr(doc, field, value)

    if recomputed_total is not None:
        doc.total_amount = recomputed_total

    doc.updated_at = datetime.now(UTC).replace(tzinfo=None)

    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail="A tax document with the same year, form type, entity, and payer already exists.",
        ) from exc
    session.refresh(doc)
    logger.info("Updated tax document: id=%s", doc.id)
    return TaxDocumentOut.model_validate(doc)


@router.delete("/tax-documents/{doc_id}", response_model=TaxDocumentOut)
def delete_tax_document(
    doc_id: str,
    session: Session = Depends(get_db),  # noqa: B008
) -> TaxDocumentOut:
    """Soft-delete a tax document by setting status to inactive.

    Returns 403 if the tax year is locked for the entity.
    Returns 404 if the document does not exist.
    """
    doc: TaxDocument | None = (
        session.query(TaxDocument).filter(TaxDocument.id == doc_id).first()
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="Tax document not found")

    check_lock(session, doc.entity, f"{doc.tax_year}-01-01")

    doc.status = TaxDocumentStatus.INACTIVE.value
    doc.updated_at = datetime.now(UTC).replace(tzinfo=None)
    session.commit()
    session.refresh(doc)
    logger.info(
        "Soft-deleted tax document: id=%s year=%d form=%s entity=%s",
        doc.id,
        doc.tax_year,
        doc.form_type,
        doc.entity,
    )
    return TaxDocumentOut.model_validate(doc)
