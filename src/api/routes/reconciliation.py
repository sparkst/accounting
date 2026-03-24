"""Reconciliation API routes.

POST /api/reconcile/run       — Run matching engine, return summary.
GET  /api/reconcile/matched   — Return matched payout/bank pairs.
GET  /api/reconcile/unmatched — Return unmatched items grouped by side.
POST /api/reconcile/manual-match — Manually link two transactions.

Design note: Reconciliation pairs are intentional accounting pairs
(Stripe/Shopify payout + bank deposit). They must NOT be flagged as duplicates.
The dedup layer uses source_hash (SHA256 of source + source_id) and will never
see cross-source pairs as duplicates.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from collections.abc import Generator

from sqlalchemy.orm import Session

from src.db.connection import SessionLocal
from src.utils.reconciliation import (
    MonthlyDiscrepancy,
    ReconciliationMatch,
    apply_manual_match,
    find_matches,
    remove_manual_match,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reconcile", tags=["reconciliation"])


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
# Response schemas
# ---------------------------------------------------------------------------


class TransactionSlim(BaseModel):
    """Minimal transaction shape for reconciliation responses."""

    id: str
    source: str
    date: str
    description: str
    amount: float | None
    payment_method: str | None
    status: str
    notes: str | None


class MatchedPairOut(BaseModel):
    payout: TransactionSlim
    bank: TransactionSlim
    confidence: float
    date_diff_days: int
    card_match: bool


class UnmatchedOut(BaseModel):
    payouts: list[TransactionSlim]
    banks: list[TransactionSlim]


class MonthlyTotalOut(BaseModel):
    month: str
    payout_total: float
    bank_total: float
    discrepancy: float
    flagged: bool


class ReconcileRunSummary(BaseModel):
    matched_count: int
    unmatched_payout_count: int
    unmatched_bank_count: int
    flagged_months: int
    monthly_totals: list[MonthlyTotalOut]


class ManualMatchRequest(BaseModel):
    transaction_id_a: str
    transaction_id_b: str


class ManualMatchResult(BaseModel):
    transaction_a: TransactionSlim
    transaction_b: TransactionSlim
    message: str


class UnlinkRequest(BaseModel):
    transaction_id_a: str
    transaction_id_b: str


class UnlinkResult(BaseModel):
    transaction_a: TransactionSlim
    transaction_b: TransactionSlim
    message: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slim(txn: object) -> TransactionSlim:
    return TransactionSlim(
        id=txn.id,  # type: ignore[attr-defined]
        source=txn.source,  # type: ignore[attr-defined]
        date=txn.date,  # type: ignore[attr-defined]
        description=txn.description,  # type: ignore[attr-defined]
        amount=float(txn.amount) if txn.amount is not None else None,  # type: ignore[attr-defined]
        payment_method=txn.payment_method,  # type: ignore[attr-defined]
        status=txn.status,  # type: ignore[attr-defined]
        notes=txn.notes,  # type: ignore[attr-defined]
    )


def _monthly_out(m: MonthlyDiscrepancy) -> MonthlyTotalOut:
    return MonthlyTotalOut(
        month=m.month,
        payout_total=float(m.payout_total),
        bank_total=float(m.bank_total),
        discrepancy=float(m.discrepancy),
        flagged=m.flagged,
    )


def _match_out(m: ReconciliationMatch) -> MatchedPairOut:
    return MatchedPairOut(
        payout=_slim(m.payout),
        bank=_slim(m.bank),
        confidence=m.confidence,
        date_diff_days=m.date_diff_days,
        card_match=m.card_match,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/run", response_model=ReconcileRunSummary)
def run_reconciliation(
    date_window: int = 3,
    db: Session = Depends(get_db),  # noqa: B008
) -> ReconcileRunSummary:
    """Run the reconciliation matching engine.

    Args:
        date_window: Maximum days between payout and bank deposit to consider a match.
                     Defaults to 3.

    Returns:
        Summary counts and monthly totals comparison.
    """
    try:
        result = find_matches(db, date_window=date_window)
    except Exception as exc:
        logger.exception("Reconciliation run failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    flagged_months = sum(1 for m in result.monthly_discrepancies if m.flagged)

    return ReconcileRunSummary(
        matched_count=len(result.matched),
        unmatched_payout_count=len(result.unmatched_payouts),
        unmatched_bank_count=len(result.unmatched_banks),
        flagged_months=flagged_months,
        monthly_totals=[_monthly_out(m) for m in result.monthly_discrepancies],
    )


@router.get("/matched", response_model=list[MatchedPairOut])
def get_matched(
    date_window: int = 3,
    db: Session = Depends(get_db),  # noqa: B008
) -> list[MatchedPairOut]:
    """Return matched payout/bank deposit pairs.

    Args:
        date_window: Maximum days between payout and bank deposit.

    Returns:
        List of matched pairs sorted by payout date descending.
    """
    try:
        result = find_matches(db, date_window=date_window)
    except Exception as exc:
        logger.exception("Reconciliation matched query failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    sorted_matches = sorted(result.matched, key=lambda m: m.payout.date, reverse=True)
    return [_match_out(m) for m in sorted_matches]


@router.get("/unmatched", response_model=UnmatchedOut)
def get_unmatched(
    date_window: int = 3,
    db: Session = Depends(get_db),  # noqa: B008
) -> UnmatchedOut:
    """Return unmatched payouts and bank deposits, grouped by side.

    Args:
        date_window: Maximum days between payout and bank deposit.

    Returns:
        Object with ``payouts`` list and ``banks`` list.
    """
    try:
        result = find_matches(db, date_window=date_window)
    except Exception as exc:
        logger.exception("Reconciliation unmatched query failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return UnmatchedOut(
        payouts=[_slim(p) for p in sorted(result.unmatched_payouts, key=lambda t: t.date, reverse=True)],
        banks=[_slim(b) for b in sorted(result.unmatched_banks, key=lambda t: t.date, reverse=True)],
    )


@router.post("/manual-match", response_model=ManualMatchResult)
def manual_match(
    body: ManualMatchRequest,
    db: Session = Depends(get_db),  # noqa: B008
) -> ManualMatchResult:
    """Manually link two transactions as a reconciliation pair.

    Both transactions receive a ``reconciled:<other_id>`` note.
    Either order (payout first or bank first) is accepted.

    Args:
        body: ``{ transaction_id_a, transaction_id_b }``

    Returns:
        Both updated transactions plus a confirmation message.
    """
    try:
        txn_a, txn_b = apply_manual_match(db, body.transaction_id_a, body.transaction_id_b)
        db.commit()
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Manual match failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return ManualMatchResult(
        transaction_a=_slim(txn_a),
        transaction_b=_slim(txn_b),
        message=f"Linked {txn_a.id[:8]} ↔ {txn_b.id[:8]}",
    )


@router.post("/unlink", response_model=UnlinkResult)
def unlink_match(
    body: UnlinkRequest,
    db: Session = Depends(get_db),  # noqa: B008
) -> UnlinkResult:
    """Remove the reconciliation link between two matched transactions.

    Strips the ``reconciled:<other_id>`` note from both transactions atomically.
    Returns 404 if either transaction is not found or if they are not currently
    linked to each other.

    Args:
        body: ``{ transaction_id_a, transaction_id_b }``

    Returns:
        Both updated transactions plus a confirmation message.
    """
    try:
        txn_a, txn_b = remove_manual_match(db, body.transaction_id_a, body.transaction_id_b)
        db.commit()
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Unlink match failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return UnlinkResult(
        transaction_a=_slim(txn_a),
        transaction_b=_slim(txn_b),
        message=f"Unlinked {txn_a.id[:8]} ↔ {txn_b.id[:8]}",
    )
