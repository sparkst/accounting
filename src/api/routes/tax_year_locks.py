"""Tax year lock endpoints.

GET    /api/tax-year-locks           — List all locks.
POST   /api/tax-year-locks           — Create a lock for (entity, year).
DELETE /api/tax-year-locks/{id}      — Remove a lock (e.g. to allow amendment).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.db.connection import SessionLocal
from src.models.enums import Entity
from src.models.tax_year_lock import TaxYearLock

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tax-year-locks"])


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
# Schemas
# ---------------------------------------------------------------------------


class TaxYearLockOut(BaseModel):
    """Full tax year lock response schema."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    entity: str
    year: int
    locked_at: datetime
    locked_by: str


class TaxYearLockCreate(BaseModel):
    """Body for POST /api/tax-year-locks."""

    entity: str
    year: int
    locked_by: str = "human"

    @field_validator("entity")
    @classmethod
    def validate_entity(cls, v: str) -> str:
        Entity(v)  # raises ValueError if invalid
        return v

    @field_validator("year")
    @classmethod
    def validate_year(cls, v: int) -> int:
        if v < 2000 or v > 2100:
            raise ValueError(f"year must be between 2000 and 2100, got {v}")
        return v


# ---------------------------------------------------------------------------
# Helper: check if a transaction date is locked
# ---------------------------------------------------------------------------


def check_lock(session: Session, entity: str | None, date: str | None) -> None:
    """Raise HTTP 403 if the (entity, year) of the transaction is locked.

    Args:
        session: Open DB session.
        entity:  Entity value from the transaction (may be None).
        date:    ISO date string YYYY-MM-DD from the transaction (may be None).

    Raises:
        HTTPException(403): if a TaxYearLock exists for (entity, year).
    """
    if not entity or not date:
        return  # No entity or date — cannot determine lock; allow the edit.

    try:
        year = int(date[:4])
    except (ValueError, TypeError):
        return  # Unparseable date — allow the edit.

    lock: TaxYearLock | None = (
        session.query(TaxYearLock)
        .filter(TaxYearLock.entity == entity, TaxYearLock.year == year)
        .first()
    )
    if lock is not None:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Tax year {year} is locked for entity '{entity}'. "
                f"Locked on {lock.locked_at.date()} by '{lock.locked_by}'. "
                "Remove the lock via DELETE /api/tax-year-locks/{id} to allow edits."
            ),
        )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/tax-year-locks", response_model=list[TaxYearLockOut])
def list_tax_year_locks(
    session: Session = Depends(get_db),  # noqa: B008
) -> list[TaxYearLockOut]:
    """Return all tax year locks ordered by entity then year."""
    try:
        locks: list[TaxYearLock] = (
            session.query(TaxYearLock)
            .order_by(TaxYearLock.entity, TaxYearLock.year)
            .all()
        )
        return [TaxYearLockOut.model_validate(lock) for lock in locks]
    finally:
        session.close()


@router.post("/tax-year-locks", response_model=TaxYearLockOut, status_code=201)
def create_tax_year_lock(
    body: TaxYearLockCreate,
    session: Session = Depends(get_db),  # noqa: B008
) -> TaxYearLockOut:
    """Create a lock for (entity, year).

    Returns 409 Conflict if the (entity, year) pair is already locked.
    """
    try:
        lock = TaxYearLock(
            entity=body.entity,
            year=body.year,
            locked_at=datetime.now(UTC).replace(tzinfo=None),
            locked_by=body.locked_by,
        )
        session.add(lock)
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise HTTPException(
                status_code=409,
                detail=f"Tax year {body.year} is already locked for entity '{body.entity}'.",
            ) from exc
        session.refresh(lock)
        logger.info(
            "Created tax year lock: entity=%s year=%d locked_by=%r",
            lock.entity,
            lock.year,
            lock.locked_by,
        )
        return TaxYearLockOut.model_validate(lock)
    finally:
        session.close()


@router.delete("/tax-year-locks/{lock_id}", status_code=204)
def delete_tax_year_lock(
    lock_id: str,
    session: Session = Depends(get_db),  # noqa: B008
) -> None:
    """Remove a tax year lock, allowing edits to that entity+year again."""
    try:
        lock: TaxYearLock | None = (
            session.query(TaxYearLock).filter(TaxYearLock.id == lock_id).first()
        )
        if lock is None:
            raise HTTPException(status_code=404, detail="Tax year lock not found")

        entity, year = lock.entity, lock.year
        session.delete(lock)
        session.commit()
        logger.info("Removed tax year lock: entity=%s year=%d", entity, year)
    finally:
        session.close()
