"""Sparks Personal WBR — weekly ledger-summary feed (REQ-WBR-LED-001..011).

GET /api/ingest/wbr/ledger-summary?week_end=YYYY-MM-DD&entity=personal

Feeds the "This week's money in & out" section of the Sparks Personal Weekly
Business Review report: n8n fetches this JSON every Monday morning and forwards
it in the report-generate payload to the Cloudflare wealth app.

The path lives under ``/api/ingest/`` even though it is a read endpoint,
because n8n's Cloudflare Access service token (``books-ingest``) is scoped to
``/api/ingest/*``. Auth mirrors the other n8n-facing ingest routes exactly:
``require_api_or_ingest_key`` (browser ``API_KEY`` or machine
``INGEST_API_KEY`` via the ``X-Api-Key`` header).

Register conventions honored here:
- Amounts are signed per DB convention (income +, expense −) and computed with
  ``Decimal`` end-to-end (quantized to 2dp at the response boundary).
- ``status="rejected"`` rows are excluded (never deleted).
- Income rows stored negative (raw Gmail data classified later) are surfaced
  positive — the same correction ``TransactionOut.fix_income_sign`` applies.
- Split children (``parent_id`` set) are excluded so a split never
  double-counts against its parent row.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from src.api.auth import require_api_or_ingest_key
from src.db.connection import SessionLocal
from src.models.enums import Direction, Entity, TransactionStatus
from src.models.transaction import Transaction

router = APIRouter(tags=["wbr"])

#: Maximum transaction rows returned; the JSON reports ``truncated=true``
#: when the window held more (totals always cover the full window).
MAX_ROWS = 40

_TWO_DP = Decimal("0.01")
_ZERO = Decimal("0.00")


def most_recent_sunday(today: date) -> date:
    """Return ``today`` if it is a Sunday, else the closest prior Sunday."""
    return today - timedelta(days=(today.weekday() + 1) % 7)


# ---------------------------------------------------------------------------
# Response schema (contract shared with the sparkry-crm WBR generate endpoint)
# ---------------------------------------------------------------------------


class WbrLedgerTransaction(BaseModel):
    """One register row in the WBR weekly ledger."""

    date: str
    name: str
    category: str
    amount: float


class WbrLedgerSummary(BaseModel):
    """Result of GET /api/ingest/wbr/ledger-summary."""

    week_end: str
    transactions: list[WbrLedgerTransaction]
    inflow_total: float
    outflow_total: float
    entity: str
    truncated: bool = False


# ---------------------------------------------------------------------------
# Query parameter singletons (avoids B008 — no function call in default arg)
# ---------------------------------------------------------------------------

_WEEK_END_QUERY = Query(
    default=None,
    description=(
        "Week-end date YYYY-MM-DD (inclusive; normally a Sunday). "
        "Omit to default to the most recent Sunday."
    ),
)
_ENTITY_QUERY = Query(
    default=Entity.PERSONAL.value,
    description="Entity filter: sparkry | blackline | personal (default personal).",
)


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.get(
    "/ingest/wbr/ledger-summary",
    response_model=WbrLedgerSummary,
    dependencies=[Depends(require_api_or_ingest_key)],
)
def wbr_ledger_summary(
    week_end: str | None = _WEEK_END_QUERY,
    entity: str = _ENTITY_QUERY,
) -> WbrLedgerSummary:
    """Return the 7-day ledger window ending ``week_end`` (inclusive).

    Rows are sorted by absolute amount descending and capped at ``MAX_ROWS``;
    ``inflow_total`` / ``outflow_total`` are positive 2dp sums over the FULL
    window (independent of the row cap). Rejected rows, NULL-amount rows, and
    split children are excluded.
    """
    if week_end is None:
        end = most_recent_sunday(date.today())
    else:
        try:
            end = date.fromisoformat(week_end)
        except ValueError as err:
            raise HTTPException(
                status_code=422,
                detail=f"week_end must be an ISO date (YYYY-MM-DD), got {week_end!r}",
            ) from err

    try:
        entity_value = Entity(entity).value
    except ValueError as err:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unknown entity {entity!r}. "
                f"Expected one of: {[e.value for e in Entity]}"
            ),
        ) from err

    start = end - timedelta(days=6)

    session = SessionLocal()
    try:
        rows: list[Transaction] = (
            session.query(Transaction)
            .filter(
                Transaction.date >= start.isoformat(),
                Transaction.date <= end.isoformat(),
                Transaction.status != TransactionStatus.REJECTED.value,
                Transaction.entity == entity_value,
                Transaction.amount.isnot(None),
                Transaction.parent_id.is_(None),
            )
            .all()
        )

        inflow = _ZERO
        outflow = _ZERO
        entries: list[tuple[Decimal, Transaction]] = []
        for tx in rows:
            amount = Decimal(str(tx.amount))
            # Mirror TransactionOut.fix_income_sign: income classified after a
            # negative-store (raw Gmail data) is surfaced positive.
            if tx.direction == Direction.INCOME.value and amount < 0:
                amount = -amount
            amount = amount.quantize(_TWO_DP, rounding=ROUND_HALF_UP)
            if amount >= 0:
                inflow += amount
            else:
                outflow += -amount
            entries.append((amount, tx))

        # |amount| descending; date/description tie-break keeps output stable.
        entries.sort(key=lambda pair: (-abs(pair[0]), pair[1].date, pair[1].description))
        truncated = len(entries) > MAX_ROWS
        entries = entries[:MAX_ROWS]

        return WbrLedgerSummary(
            week_end=end.isoformat(),
            transactions=[
                WbrLedgerTransaction(
                    date=tx.date,
                    name=tx.description,
                    category=tx.tax_category or tx.direction or "uncategorized",
                    amount=float(amount),
                )
                for amount, tx in entries
            ],
            inflow_total=float(inflow.quantize(_TWO_DP, rounding=ROUND_HALF_UP)),
            outflow_total=float(outflow.quantize(_TWO_DP, rounding=ROUND_HALF_UP)),
            entity=entity_value,
            truncated=truncated,
        )
    finally:
        session.close()
