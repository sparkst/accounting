"""Sparks Personal WBR — weekly ledger-summary feed (REQ-WBR-LED-001..013).

GET /api/ingest/wbr/ledger-summary?week_end=YYYY-MM-DD&entity=personal

Feeds the "This week's money in & out" section of the Sparks Personal Weekly
Business Review report: n8n fetches this JSON every Monday morning and forwards
it in the report-generate payload to the Cloudflare wealth app.

The path lives under ``/api/ingest/`` even though it is a read endpoint,
because n8n's Cloudflare Access service token (``books-ingest``) is scoped to
``/api/ingest/*``. Auth mirrors the other n8n-facing ingest routes exactly:
``require_api_or_ingest_key`` (browser ``API_KEY`` or machine
``INGEST_API_KEY`` via the ``X-Api-Key`` header) — BUT unlike the other
``/api/ingest/*`` routes (write-only triggers with no readable body), this
route returns transaction descriptions and amounts, so it additionally scopes
by WHICH credential authenticated (round-2 fix directive P1-a1c): the machine
``INGEST_API_KEY`` may only ever read ``entity=personal`` (403 otherwise);
only the full ``API_KEY`` may query other entities. ``week_end`` is also
bounded to no more than 120 days in the past (and never in the future),
regardless of credential.

Register conventions honored here:
- Amounts are signed per DB convention (income +, expense −) and computed with
  ``Decimal`` end-to-end (quantized to 2dp at the response boundary).
- ``status="rejected"`` rows are excluded (never deleted).
- Income rows stored negative (raw Gmail data classified later) are surfaced
  positive — the same correction ``TransactionOut.fix_income_sign`` applies.
- Split children (``parent_id`` set) are excluded so a split never
  double-counts against its parent row.
- ``direction=transfer`` rows stay visible in the ledger list (category label
  "Transfer") but are EXCLUDED from ``inflow_total``/``outflow_total`` so an
  internal account-to-account move can't inflate the "money in & out"
  headline (round-2 fix directive P1-tfr3).

``most_recent_sunday`` golden-date rule (round-2 fix directive P1-a1b,
authoritative — n8n's semantics win): the most recent Sunday STRICTLY BEFORE
the reference date, computed against the America/Los_Angeles calendar date.
The identical ≥6-case golden table lives in ``test_wbr_ledger.py`` and is
semantically identical in ``sparkry-crm-wbr/src/lib/server/wealth/wbr/dates.ts``
and ``n8n-render`` ``compute-week-end.js`` — do not diverge without updating
all three.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

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

#: `week_end` may not be older than this many days (round-2 fix directive
#: P1-a1c) — applies regardless of which credential authenticated.
MAX_WEEK_END_AGE_DAYS = 120

_TWO_DP = Decimal("0.01")
_ZERO = Decimal("0.00")
_LA_TZ = ZoneInfo("America/Los_Angeles")


def _today_la() -> date:
    """Current calendar date in America/Los_Angeles (the WBR's home tz)."""
    return datetime.now(_LA_TZ).date()


def most_recent_sunday(reference: date) -> date:
    """Return the most recent Sunday STRICTLY BEFORE ``reference``.

    This is the single shared rule (round-2 fix directive P1-a1b — n8n's
    semantics are authoritative): when ``reference`` is itself a Sunday, the
    week has not closed yet, so the answer is the PRIOR Sunday, not today.
    Mirrors n8n's ``compute-week-end.js``
    (``daysBack = dow === 0 ? 7 : dow``, JS ``getDay()`` where Sunday=0) and
    ``sparkry-crm-wbr``'s ``mostRecentSunday``.
    """
    days_back = 7 if reference.weekday() == 6 else reference.weekday() + 1
    return reference - timedelta(days=days_back)


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
)
def wbr_ledger_summary(
    week_end: str | None = _WEEK_END_QUERY,
    entity: str = _ENTITY_QUERY,
    credential: str = Depends(require_api_or_ingest_key),
) -> WbrLedgerSummary:
    """Return the 7-day ledger window ending ``week_end`` (inclusive).

    Rows are sorted by absolute amount descending and capped at ``MAX_ROWS``;
    ``inflow_total`` / ``outflow_total`` are positive 2dp sums over the FULL
    window (independent of the row cap), EXCLUDING ``direction=transfer`` rows
    (those still appear in ``transactions`` with category "Transfer").
    Rejected rows, NULL-amount rows, and split children are excluded.

    Scoping (round-2 fix directive P1-a1c): when ``credential == "ingest"``
    (n8n's machine key), only ``entity=personal`` is allowed — anything else
    is a 403. ``week_end`` may not be more than ``MAX_WEEK_END_AGE_DAYS`` days
    in the past, nor in the future, for any credential.
    """
    reference = _today_la()

    if week_end is None:
        end = most_recent_sunday(reference)
    else:
        try:
            end = date.fromisoformat(week_end)
        except ValueError as err:
            raise HTTPException(
                status_code=422,
                detail=f"week_end must be an ISO date (YYYY-MM-DD), got {week_end!r}",
            ) from err

    oldest_allowed = reference - timedelta(days=MAX_WEEK_END_AGE_DAYS)
    if end < oldest_allowed:
        raise HTTPException(
            status_code=422,
            detail=(
                f"week_end {end.isoformat()!r} is more than "
                f"{MAX_WEEK_END_AGE_DAYS} days old (oldest allowed: "
                f"{oldest_allowed.isoformat()})"
            ),
        )
    if end > reference:
        raise HTTPException(
            status_code=422,
            detail=(
                f"week_end {end.isoformat()!r} is in the future "
                f"(today in America/Los_Angeles: {reference.isoformat()})"
            ),
        )

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

    if credential == "ingest" and entity_value != Entity.PERSONAL.value:
        raise HTTPException(
            status_code=403,
            detail=(
                "INGEST_API_KEY may only query entity=personal; "
                "use the full API_KEY for other entities"
            ),
        )

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
        entries: list[tuple[Decimal, Transaction, bool]] = []
        for tx in rows:
            amount = Decimal(str(tx.amount))
            # Mirror TransactionOut.fix_income_sign: income classified after a
            # negative-store (raw Gmail data) is surfaced positive.
            if tx.direction == Direction.INCOME.value and amount < 0:
                amount = -amount
            amount = amount.quantize(_TWO_DP, rounding=ROUND_HALF_UP)
            is_transfer = tx.direction == Direction.TRANSFER.value
            # Transfer rows (internal account-to-account moves) stay visible
            # in the ledger list but are EXCLUDED from inflow/outflow totals
            # (round-2 fix directive P1-tfr3) — otherwise a Stripe payout or
            # similar internal move inflates the "money in & out" headline.
            if not is_transfer:
                if amount >= 0:
                    inflow += amount
                else:
                    outflow += -amount
            entries.append((amount, tx, is_transfer))

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
                    category=(
                        "Transfer"
                        if is_transfer
                        else (tx.tax_category or tx.direction or "uncategorized")
                    ),
                    amount=float(amount),
                )
                for amount, tx, is_transfer in entries
            ],
            inflow_total=float(inflow.quantize(_TWO_DP, rounding=ROUND_HALF_UP)),
            outflow_total=float(outflow.quantize(_TWO_DP, rounding=ROUND_HALF_UP)),
            entity=entity_value,
            truncated=truncated,
        )
    finally:
        session.close()
