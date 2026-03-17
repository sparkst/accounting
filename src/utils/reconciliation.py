"""Stripe/Shopify payout-to-bank-deposit reconciliation engine.

REQ-ID: RECON-001  Match payouts to bank deposits by amount (exact) and date (±window).
REQ-ID: RECON-002  Use payment_method card last-4 as a disambiguation signal when available.
REQ-ID: RECON-003  Multiple payouts with same amount: prefer closest date.
REQ-ID: RECON-004  Monthly total sanity check: flag if payout sum vs deposit sum >$1.
REQ-ID: RECON-005  Manual match: accept two transaction IDs and link them.

Reconciliation pairs are NOT duplicates — they are intentional accounting pairs
(a Stripe/Shopify payout + its corresponding bank deposit). The dedup layer uses
source_hash (SHA256 of source + source_id) and will never flag cross-source pairs.

Design spec: docs/superpowers/specs/2026-03-15-accounting-system-design.md §Reconciliation
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from src.models.enums import Source, TransactionStatus
from src.models.transaction import Transaction

logger = logging.getLogger(__name__)

# Sources that produce payout transactions (Stripe / Shopify settle to a bank account)
PAYOUT_SOURCES: frozenset[str] = frozenset([Source.STRIPE, Source.SHOPIFY])
# Sources that represent bank records
BANK_SOURCES: frozenset[str] = frozenset([Source.BANK_CSV])

# Field stored on reconciled transactions to link the pair
RECON_LINK_NOTE_PREFIX = "reconciled:"


@dataclass
class ReconciliationMatch:
    """A confirmed or candidate match between a payout and a bank deposit."""

    payout: Transaction
    bank: Transaction
    confidence: float  # 0.0–1.0
    date_diff_days: int
    card_match: bool  # True if payment_method last-4 matched


@dataclass
class ReconciliationResult:
    """Full output of a reconciliation run."""

    matched: list[ReconciliationMatch] = field(default_factory=list)
    unmatched_payouts: list[Transaction] = field(default_factory=list)
    unmatched_banks: list[Transaction] = field(default_factory=list)
    monthly_discrepancies: list[MonthlyDiscrepancy] = field(default_factory=list)


@dataclass
class MonthlyDiscrepancy:
    """Per-month comparison of payout totals vs bank deposit totals."""

    month: str  # YYYY-MM
    payout_total: Decimal
    bank_total: Decimal
    discrepancy: Decimal  # abs(payout_total - bank_total)
    flagged: bool  # True when discrepancy > $1.00


def _extract_last4(payment_method: str | None) -> str | None:
    """Extract 4-digit suffix from strings like 'VISA ****5482' or 'VISA 5482'."""
    if not payment_method:
        return None
    # Take the last 4 digits from the field
    digits = "".join(ch for ch in payment_method if ch.isdigit())
    return digits[-4:] if len(digits) >= 4 else None


def _parse_date(iso: str) -> date:
    return date.fromisoformat(iso)


def _month_key(iso: str) -> str:
    return iso[:7]  # YYYY-MM


def _amount_abs(txn: Transaction) -> Decimal:
    """Absolute value of the transaction amount as Decimal."""
    if txn.amount is None:
        return Decimal("0")
    return abs(Decimal(str(txn.amount)))


def _confidence_score(date_diff: int, card_match: bool, date_window: int) -> float:
    """Compute a confidence score for a payout/bank match candidate.

    Rules:
    - Base confidence starts at 1.0 (exact amount required for candidacy).
    - Date proximity: same-day = full score, each day away reduces by window fraction.
    - Card last-4 match adds a bonus capped at 1.0.
    """
    # Date component: 1.0 on same day, 0.0 at the edge of the window
    date_score = max(0.0, 1.0 - (date_diff / (date_window + 1)))
    score = 0.6 + 0.3 * date_score
    if card_match:
        score = min(1.0, score + 0.15)
    return round(score, 4)


def _load_payouts(session: Session) -> list[Transaction]:
    """Load all non-rejected income transactions from payout sources."""
    stmt = select(Transaction).where(
        and_(
            Transaction.source.in_(list(PAYOUT_SOURCES)),
            Transaction.status != TransactionStatus.REJECTED,
            Transaction.amount.is_not(None),
        )
    )
    return list(session.scalars(stmt).all())


def _load_bank_deposits(session: Session) -> list[Transaction]:
    """Load all non-rejected income transactions from bank sources."""
    stmt = select(Transaction).where(
        and_(
            Transaction.source.in_(list(BANK_SOURCES)),
            Transaction.status != TransactionStatus.REJECTED,
            Transaction.amount.is_not(None),
        )
    )
    return list(session.scalars(stmt).all())


def _already_reconciled(txn: Transaction) -> bool:
    """Return True if this transaction has been manually reconciled."""
    return bool(txn.notes and txn.notes.startswith(RECON_LINK_NOTE_PREFIX))


def find_matches(
    session: Session,
    date_window: int = 3,
) -> ReconciliationResult:
    """Match Stripe/Shopify payouts to bank deposits.

    Algorithm:
    1. Load all payout transactions (Stripe, Shopify) and bank deposit transactions.
    2. Already-reconciled pairs are included in matched output but skipped for re-matching.
    3. For each payout, find bank transactions with the exact same absolute amount
       within ±date_window days.
    4. If multiple candidates exist, prefer the one with card last-4 match, then
       closest date.
    5. Each bank transaction can only be matched once (greedy by confidence).
    6. Run monthly totals comparison and flag months with >$1 discrepancy.

    Args:
        session:     SQLAlchemy session.
        date_window: Maximum date offset (days) to consider as a candidate.

    Returns:
        ReconciliationResult with matched pairs, unmatched lists, and monthly flags.
    """
    payouts = _load_payouts(session)
    banks = _load_bank_deposits(session)

    # Separate already-reconciled transactions
    free_payouts: list[Transaction] = []
    free_banks: list[Transaction] = []

    # Build a lookup of manually-reconciled bank IDs → payout
    reconciled_bank_ids: set[str] = set()
    reconciled_payout_ids: set[str] = set()

    for p in payouts:
        if _already_reconciled(p):
            # Extract linked bank ID from notes field
            linked_id = p.notes.split(":", 1)[1] if p.notes else None
            if linked_id:
                reconciled_payout_ids.add(p.id)
        else:
            free_payouts.append(p)

    for b in banks:
        if _already_reconciled(b):
            linked_id = b.notes.split(":", 1)[1] if b.notes else None
            if linked_id:
                reconciled_bank_ids.add(b.id)
        else:
            free_banks.append(b)

    # Build matched list from manual pairs
    matched: list[ReconciliationMatch] = []

    # For manual pairs, find the payout-bank relationship
    for p in payouts:
        if p.id in reconciled_payout_ids and p.notes:
            linked_bank_id = p.notes.split(":", 1)[1]
            linked_bank = next((b for b in banks if b.id == linked_bank_id), None)
            if linked_bank:
                d_diff = abs((_parse_date(p.date) - _parse_date(linked_bank.date)).days)
                card_match = bool(
                    _extract_last4(p.payment_method)
                    and _extract_last4(p.payment_method) == _extract_last4(linked_bank.payment_method)
                )
                matched.append(
                    ReconciliationMatch(
                        payout=p,
                        bank=linked_bank,
                        confidence=1.0,  # manually confirmed
                        date_diff_days=d_diff,
                        card_match=card_match,
                    )
                )

    # Auto-match free payouts against free banks
    used_bank_ids: set[str] = set()

    # Sort payouts by amount desc so larger amounts are matched first (fewer ambiguous ties)
    sorted_payouts = sorted(free_payouts, key=lambda t: _amount_abs(t), reverse=True)

    # For each payout, collect candidates then pick the best
    for payout in sorted_payouts:
        payout_amount = _amount_abs(payout)
        payout_date = _parse_date(payout.date)
        payout_last4 = _extract_last4(payout.payment_method)

        candidates: list[tuple[float, int, bool, Transaction]] = []

        for bank in free_banks:
            if bank.id in used_bank_ids:
                continue

            bank_amount = _amount_abs(bank)
            if bank_amount != payout_amount:
                continue

            bank_date = _parse_date(bank.date)
            diff = abs((payout_date - bank_date).days)
            if diff > date_window:
                continue

            bank_last4 = _extract_last4(bank.payment_method)
            card_match = bool(payout_last4 and bank_last4 and payout_last4 == bank_last4)

            conf = _confidence_score(diff, card_match, date_window)
            candidates.append((conf, diff, card_match, bank))

        if not candidates:
            continue

        # Best candidate: highest confidence, then closest date
        best_conf, best_diff, best_card, best_bank = max(
            candidates, key=lambda c: (c[0], -c[1])
        )

        used_bank_ids.add(best_bank.id)
        matched.append(
            ReconciliationMatch(
                payout=payout,
                bank=best_bank,
                confidence=best_conf,
                date_diff_days=best_diff,
                card_match=best_card,
            )
        )

    # Unmatched
    matched_payout_ids = {m.payout.id for m in matched}
    matched_bank_ids = {m.bank.id for m in matched}

    unmatched_payouts = [p for p in free_payouts if p.id not in matched_payout_ids]
    unmatched_banks = [b for b in free_banks if b.id not in matched_bank_ids]

    # Monthly totals
    monthly_discrepancies = _compute_monthly_discrepancies(payouts, banks)

    return ReconciliationResult(
        matched=matched,
        unmatched_payouts=unmatched_payouts,
        unmatched_banks=unmatched_banks,
        monthly_discrepancies=monthly_discrepancies,
    )


def _compute_monthly_discrepancies(
    payouts: list[Transaction],
    banks: list[Transaction],
) -> list[MonthlyDiscrepancy]:
    """Compare payout totals to bank deposit totals per month."""
    payout_by_month: dict[str, Decimal] = {}
    bank_by_month: dict[str, Decimal] = {}

    for p in payouts:
        m = _month_key(p.date)
        payout_by_month[m] = payout_by_month.get(m, Decimal("0")) + _amount_abs(p)

    for b in banks:
        m = _month_key(b.date)
        bank_by_month[m] = bank_by_month.get(m, Decimal("0")) + _amount_abs(b)

    all_months = sorted(set(payout_by_month) | set(bank_by_month))
    result: list[MonthlyDiscrepancy] = []

    for month in all_months:
        pt = payout_by_month.get(month, Decimal("0"))
        bt = bank_by_month.get(month, Decimal("0"))
        disc = abs(pt - bt)
        result.append(
            MonthlyDiscrepancy(
                month=month,
                payout_total=pt,
                bank_total=bt,
                discrepancy=disc,
                flagged=disc > Decimal("1.00"),
            )
        )

    return result


def apply_manual_match(
    session: Session,
    transaction_id_a: str,
    transaction_id_b: str,
) -> tuple[Transaction, Transaction]:
    """Link two transactions as a reconciliation pair.

    Writes a ``reconciled:<other_id>`` note on both transactions.
    The pair is stored symmetrically so either side can find the other.

    Args:
        session:          Active SQLAlchemy session.
        transaction_id_a: ID of the first transaction (payout or bank).
        transaction_id_b: ID of the second transaction (bank or payout).

    Returns:
        The two updated Transaction objects.

    Raises:
        ValueError: If either ID is not found.
    """
    txn_a = session.get(Transaction, transaction_id_a)
    txn_b = session.get(Transaction, transaction_id_b)

    if txn_a is None:
        raise ValueError(f"Transaction not found: {transaction_id_a}")
    if txn_b is None:
        raise ValueError(f"Transaction not found: {transaction_id_b}")

    txn_a.notes = f"{RECON_LINK_NOTE_PREFIX}{txn_b.id}"
    txn_b.notes = f"{RECON_LINK_NOTE_PREFIX}{txn_a.id}"

    session.flush()
    return txn_a, txn_b
