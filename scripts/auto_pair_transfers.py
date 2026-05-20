"""REQ-PERF-004: Auto-pair candidate generator for TRANSFER / JOURNAL / EXCHANGE rows.

Finds candidate pairs meeting ALL of:
  - canonical_action in {transfer, journal, exchange}
  - opposite signs
  - abs(|amount_a| − |amount_b|) ≤ $0.01
  - ≤ 5 BUSINESS days apart (Mon–Fri, skip Sat/Sun)
  - different account_ids
  - both currently unpaired (paired_transaction_id IS NULL)
  - not already rejected

Confidence = 1.0 / max(partners_of_a, partners_of_b).

Rejection tracking uses AuditEvent entity-mode rows:
    entity_type   = 'brokerage_transaction'
    entity_id     = tx_a_id
    field_changed = 'transfer_pair_rejected'
    new_value     = tx_b_id

CLI: outputs JSON to stdout (read-only; does NOT write pairings).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import and_, or_, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.models.audit_event import AuditEvent  # noqa: E402
from src.models.brokerage import BrokerageTransaction  # noqa: E402
from src.models.enums import BrokerageTxStatus, CanonicalAction  # noqa: E402

logger = logging.getLogger(__name__)

REJECTION_EVENT_TYPE = "transfer_pair_rejected"
ENTITY_TYPE_BROKERAGE_TX = "brokerage_transaction"
_TRANSFER_LIKE: frozenset[str] = frozenset(
    {
        CanonicalAction.TRANSFER.value,
        CanonicalAction.JOURNAL.value,
        CanonicalAction.EXCHANGE.value,
    }
)
_AMOUNT_TOL = Decimal("0.01")
_MAX_BDAYS = 5
# Max calendar days for 5 business days: Mon→Mon = 7 calendar days.
_MAX_CAL_DAYS = 7


def _business_days_between(d1: date, d2: date) -> int:
    """Count Mon–Fri days in the half-open interval (min(d1,d2), max(d1,d2)].

    Same day → 0.  Monday → Friday = 4.  Monday → next Monday = 5.
    """
    lo, hi = (d1, d2) if d1 <= d2 else (d2, d1)
    count = 0
    cur = lo + timedelta(days=1)
    while cur <= hi:
        if cur.weekday() < 5:  # 0=Mon … 4=Fri
            count += 1
        cur += timedelta(days=1)
    return count


@dataclass(frozen=True)
class CandidatePair:
    """An unconfirmed transfer-pair candidate (REQ-PERF-004)."""

    tx_a_id: str
    tx_b_id: str
    amount_a: Decimal
    amount_b: Decimal
    date_a: date
    date_b: date
    account_id_a: str
    account_id_b: str
    confidence: float  # 1.0 / max(partners_of_a, partners_of_b)


def _load_rejected_pairs(session: Session) -> set[frozenset[str]]:
    """Load all previously-rejected pair keys as unordered frozensets."""
    stmt = select(AuditEvent).where(
        AuditEvent.field_changed == REJECTION_EVENT_TYPE,
        AuditEvent.entity_type == ENTITY_TYPE_BROKERAGE_TX,
        AuditEvent.entity_id.isnot(None),
        AuditEvent.new_value.isnot(None),
    )
    rejected: set[frozenset[str]] = set()
    for evt in session.execute(stmt).scalars():
        if evt.entity_id and evt.new_value:
            rejected.add(frozenset({evt.entity_id, evt.new_value}))
    return rejected


def find_candidates(session: Session) -> list[CandidatePair]:
    """REQ-PERF-004: Return sorted candidate transfer-pair list.

    Pairs sorted by confidence (desc), then date_a, date_b (asc).
    """
    rejected = _load_rejected_pairs(session)

    stmt = (
        select(BrokerageTransaction)
        .where(
            # Rejected transactions are soft-deleted: never surface them as
            # pairing candidates (CLAUDE.md never-delete rule).
            BrokerageTransaction.status != BrokerageTxStatus.REJECTED.value,
            BrokerageTransaction.canonical_action.in_(_TRANSFER_LIKE),
            BrokerageTransaction.paired_transaction_id.is_(None),
        )
        .order_by(BrokerageTransaction.trade_date, BrokerageTransaction.id)
    )
    rows: list[BrokerageTransaction] = list(session.execute(stmt).scalars())

    raw_pairs: list[tuple[BrokerageTransaction, BrokerageTransaction]] = []
    n = len(rows)
    for i in range(n):
        a = rows[i]
        if a.amount is None:
            continue
        a_amt = Decimal(str(a.amount))
        if a_amt == Decimal("0"):
            continue

        for j in range(i + 1, n):
            b = rows[j]
            # Early exit: rows are sorted by date; once too far, all further rows are farther.
            if (b.trade_date - a.trade_date).days > _MAX_CAL_DAYS:
                break
            if b.amount is None:
                continue
            b_amt = Decimal(str(b.amount))
            if b_amt == Decimal("0"):
                continue
            # Must be different accounts
            if a.account_id == b.account_id:
                continue
            # Must have opposite signs
            if a_amt * b_amt >= Decimal("0"):
                continue
            # Amount within tolerance
            if abs(abs(a_amt) - abs(b_amt)) > _AMOUNT_TOL:
                continue
            # Within 5 business days
            if _business_days_between(a.trade_date, b.trade_date) > _MAX_BDAYS:
                continue
            # Not previously rejected
            if frozenset({a.id, b.id}) in rejected:
                continue

            raw_pairs.append((a, b))

    if not raw_pairs:
        return []

    # Count how many valid partners each tx has.
    pair_count: defaultdict[str, int] = defaultdict(int)
    for a, b in raw_pairs:
        pair_count[a.id] += 1
        pair_count[b.id] += 1

    candidates: list[CandidatePair] = []
    for a, b in raw_pairs:
        conf = 1.0 / max(pair_count[a.id], pair_count[b.id])
        candidates.append(
            CandidatePair(
                tx_a_id=a.id,
                tx_b_id=b.id,
                amount_a=Decimal(str(a.amount)),
                amount_b=Decimal(str(b.amount)),
                date_a=a.trade_date,
                date_b=b.trade_date,
                account_id_a=a.account_id,
                account_id_b=b.account_id,
                confidence=conf,
            )
        )

    candidates.sort(key=lambda c: (-c.confidence, c.date_a, c.date_b))
    return candidates


def reject_pair(session: Session, tx_a_id: str, tx_b_id: str) -> None:
    """Record that pair (tx_a_id, tx_b_id) was rejected. Idempotent."""
    if is_rejected(session, tx_a_id, tx_b_id):
        return
    evt = AuditEvent(
        entity_type=ENTITY_TYPE_BROKERAGE_TX,
        entity_id=tx_a_id,
        field_changed=REJECTION_EVENT_TYPE,
        old_value=None,
        new_value=tx_b_id,
        changed_by="auto:transfer_pair",
    )
    session.add(evt)
    session.commit()


def is_rejected(session: Session, tx_a_id: str, tx_b_id: str) -> bool:
    """Return True if either ordering of the pair has been rejected."""
    stmt = select(AuditEvent).where(
        AuditEvent.field_changed == REJECTION_EVENT_TYPE,
        AuditEvent.entity_type == ENTITY_TYPE_BROKERAGE_TX,
        or_(
            and_(
                AuditEvent.entity_id == tx_a_id,
                AuditEvent.new_value == tx_b_id,
            ),
            and_(
                AuditEvent.entity_id == tx_b_id,
                AuditEvent.new_value == tx_a_id,
            ),
        ),
    )
    return session.execute(stmt).scalars().first() is not None


def _pair_to_json_dict(c: CandidatePair) -> dict[str, object]:
    return {
        "tx_a_id": c.tx_a_id,
        "tx_b_id": c.tx_b_id,
        "amount_a": str(c.amount_a),
        "amount_b": str(c.amount_b),
        "date_a": c.date_a.isoformat(),
        "date_b": c.date_b.isoformat(),
        "account_id_a": c.account_id_a,
        "account_id_b": c.account_id_b,
        "confidence": c.confidence,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI: find and print candidate transfer pairs as JSON to stdout."""
    parser = argparse.ArgumentParser(description="REQ-PERF-004 auto-pair candidate finder")
    parser.add_argument("--limit", type=int, default=None, help="Max pairs to output")
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level)

    from src.db.connection import SessionLocal  # noqa: PLC0415

    session = SessionLocal()
    try:
        candidates = find_candidates(session)
    finally:
        session.close()

    limit: int | None = args.limit
    if limit is not None:
        candidates = candidates[:limit]
    print(json.dumps([_pair_to_json_dict(c) for c in candidates], indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
