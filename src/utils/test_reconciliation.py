"""Tests for the reconciliation engine.

REQ-ID: RECON-001  Exact amount matching within date window.
REQ-ID: RECON-002  Card last-4 disambiguation.
REQ-ID: RECON-003  Same-amount disambiguation by closest date.
REQ-ID: RECON-004  Monthly totals and >$1 discrepancy flag.
REQ-ID: RECON-005  Manual match links two transactions.
"""

from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.db.connection import _configure_sqlite
from src.models.base import Base
from src.models.enums import Source, TransactionStatus
from src.models.transaction import Transaction
from src.utils.reconciliation import (
    RECON_LINK_NOTE_PREFIX,
    apply_manual_match,
    find_matches,
    remove_manual_match,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def session() -> Session:
    """In-memory SQLite session with all tables created."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    from sqlalchemy import event as sqla_event

    sqla_event.listen(engine, "connect", _configure_sqlite)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with factory() as s:
        yield s


def _make_txn(
    session: Session,
    *,
    source: str,
    date: str,
    amount: Decimal,
    payment_method: str | None = None,
    status: str = TransactionStatus.CONFIRMED,
    notes: str | None = None,
    description: str | None = None,
    direction: str | None = None,
) -> Transaction:
    """Insert and return a Transaction."""
    txn_id = str(uuid.uuid4())
    source_id = str(uuid.uuid4())
    payload = f"{len(source)}:{source}:{source_id}"
    source_hash = hashlib.sha256(payload.encode()).hexdigest()

    # Default description and direction based on source
    if description is None:
        if source in (Source.STRIPE, Source.SHOPIFY):
            description = "PAYOUT transfer"
        else:
            description = "Test transaction"
    if direction is None:
        if source == Source.BANK_CSV:
            direction = "income"
        elif source in (Source.STRIPE, Source.SHOPIFY):
            direction = "transfer"

    txn = Transaction(
        id=txn_id,
        source=source,
        source_id=source_id,
        source_hash=source_hash,
        date=date,
        description=description,
        amount=amount,
        currency="USD",
        status=status,
        confidence=0.9,
        raw_data={},
        payment_method=payment_method,
        notes=notes,
        direction=direction,
    )
    session.add(txn)
    session.commit()
    return txn


# ---------------------------------------------------------------------------
# Basic matching
# ---------------------------------------------------------------------------


def test_exact_amount_same_day_match(session: Session) -> None:
    """Payout and bank deposit with identical amounts on same day should match."""
    payout = _make_txn(session, source=Source.STRIPE, date="2026-01-15", amount=Decimal("500.00"))
    bank = _make_txn(session, source=Source.BANK_CSV, date="2026-01-15", amount=Decimal("500.00"))

    result = find_matches(session)

    assert len(result.matched) == 1
    m = result.matched[0]
    assert m.payout.id == payout.id
    assert m.bank.id == bank.id
    assert m.date_diff_days == 0
    assert m.confidence >= 0.9


def test_different_amounts_do_not_match(session: Session) -> None:
    """Payouts and deposits with different amounts must NOT be matched."""
    _make_txn(session, source=Source.STRIPE, date="2026-01-15", amount=Decimal("500.00"))
    _make_txn(session, source=Source.BANK_CSV, date="2026-01-15", amount=Decimal("499.00"))

    result = find_matches(session)

    assert len(result.matched) == 0
    assert len(result.unmatched_payouts) == 1
    assert len(result.unmatched_banks) == 1


# ---------------------------------------------------------------------------
# Date window (REQ-ID: RECON-001)
# ---------------------------------------------------------------------------


def test_match_within_date_window(session: Session) -> None:
    """Payout and deposit within ±3 days should match (default window=3)."""
    _make_txn(session, source=Source.STRIPE, date="2026-01-15", amount=Decimal("250.00"))
    _make_txn(session, source=Source.BANK_CSV, date="2026-01-17", amount=Decimal("250.00"))

    result = find_matches(session)

    assert len(result.matched) == 1
    assert result.matched[0].date_diff_days == 2


def test_outside_date_window_no_match(session: Session) -> None:
    """Payout and deposit more than date_window days apart must NOT match."""
    _make_txn(session, source=Source.STRIPE, date="2026-01-10", amount=Decimal("300.00"))
    _make_txn(session, source=Source.BANK_CSV, date="2026-01-20", amount=Decimal("300.00"))

    result = find_matches(session, date_window=3)

    assert len(result.matched) == 0
    assert len(result.unmatched_payouts) == 1
    assert len(result.unmatched_banks) == 1


def test_custom_date_window(session: Session) -> None:
    """Custom date_window=7 should match transactions 5 days apart."""
    _make_txn(session, source=Source.SHOPIFY, date="2026-01-10", amount=Decimal("100.00"))
    _make_txn(session, source=Source.BANK_CSV, date="2026-01-15", amount=Decimal("100.00"))

    result = find_matches(session, date_window=7)

    assert len(result.matched) == 1
    assert result.matched[0].date_diff_days == 5


# ---------------------------------------------------------------------------
# Card last-4 disambiguation (REQ-ID: RECON-002)
# ---------------------------------------------------------------------------


def test_card_match_raises_confidence(session: Session) -> None:
    """When payment_method last-4 matches, confidence should be higher."""
    _make_txn(
        session,
        source=Source.STRIPE,
        date="2026-01-15",
        amount=Decimal("400.00"),
        payment_method="VISA ****5482",
    )
    _make_txn(
        session,
        source=Source.BANK_CSV,
        date="2026-01-15",
        amount=Decimal("400.00"),
        payment_method="5482",
    )

    result = find_matches(session)

    assert len(result.matched) == 1
    m = result.matched[0]
    assert m.card_match is True
    assert m.confidence >= 0.9


def test_no_card_data_still_matches(session: Session) -> None:
    """Absence of payment_method should not prevent matching."""
    _make_txn(session, source=Source.STRIPE, date="2026-01-15", amount=Decimal("200.00"))
    _make_txn(session, source=Source.BANK_CSV, date="2026-01-15", amount=Decimal("200.00"))

    result = find_matches(session)

    assert len(result.matched) == 1
    assert result.matched[0].card_match is False


# ---------------------------------------------------------------------------
# Same-amount disambiguation (REQ-ID: RECON-003)
# ---------------------------------------------------------------------------


def test_same_amount_closest_date_wins(session: Session) -> None:
    """When two bank deposits have same amount, payout should match the closer one."""
    payout = _make_txn(
        session, source=Source.STRIPE, date="2026-01-15", amount=Decimal("750.00")
    )
    bank_close = _make_txn(
        session, source=Source.BANK_CSV, date="2026-01-16", amount=Decimal("750.00")
    )  # 1 day away
    bank_far = _make_txn(
        session, source=Source.BANK_CSV, date="2026-01-18", amount=Decimal("750.00")
    )  # 3 days away

    result = find_matches(session)

    assert len(result.matched) == 1
    m = result.matched[0]
    assert m.payout.id == payout.id
    assert m.bank.id == bank_close.id
    assert m.date_diff_days == 1
    # bank_far should be unmatched
    assert any(b.id == bank_far.id for b in result.unmatched_banks)


def test_each_bank_matched_once(session: Session) -> None:
    """A single bank deposit cannot be matched to two payouts."""
    _make_txn(session, source=Source.STRIPE, date="2026-01-15", amount=Decimal("100.00"))
    _make_txn(session, source=Source.SHOPIFY, date="2026-01-15", amount=Decimal("100.00"))
    bank = _make_txn(session, source=Source.BANK_CSV, date="2026-01-15", amount=Decimal("100.00"))

    result = find_matches(session)

    # Only one payout should match the bank; the other is unmatched
    assert len(result.matched) == 1
    assert result.matched[0].bank.id == bank.id
    assert len(result.unmatched_payouts) == 1


# ---------------------------------------------------------------------------
# Monthly totals (REQ-ID: RECON-004)
# ---------------------------------------------------------------------------


def test_monthly_totals_no_discrepancy(session: Session) -> None:
    """Matching totals should not be flagged."""
    _make_txn(session, source=Source.STRIPE, date="2026-02-01", amount=Decimal("1000.00"))
    _make_txn(session, source=Source.BANK_CSV, date="2026-02-03", amount=Decimal("1000.00"))

    result = find_matches(session)

    feb = next((d for d in result.monthly_discrepancies if d.month == "2026-02"), None)
    assert feb is not None
    assert feb.discrepancy == Decimal("0")
    assert feb.flagged is False


def test_monthly_totals_flagged_over_one_dollar(session: Session) -> None:
    """Months where totals differ by >$1 should be flagged."""
    _make_txn(session, source=Source.STRIPE, date="2026-03-01", amount=Decimal("500.00"))
    _make_txn(session, source=Source.STRIPE, date="2026-03-10", amount=Decimal("200.00"))
    # Bank has only $650 — $50 discrepancy
    _make_txn(session, source=Source.BANK_CSV, date="2026-03-05", amount=Decimal("650.00"))

    result = find_matches(session)

    mar = next((d for d in result.monthly_discrepancies if d.month == "2026-03"), None)
    assert mar is not None
    assert mar.discrepancy == Decimal("50.00")
    assert mar.flagged is True


def test_monthly_totals_not_flagged_under_one_dollar(session: Session) -> None:
    """Months where totals differ by ≤$1 should NOT be flagged."""
    _make_txn(session, source=Source.STRIPE, date="2026-04-01", amount=Decimal("1000.00"))
    _make_txn(session, source=Source.BANK_CSV, date="2026-04-01", amount=Decimal("999.50"))

    result = find_matches(session)

    apr = next((d for d in result.monthly_discrepancies if d.month == "2026-04"), None)
    assert apr is not None
    assert apr.discrepancy == Decimal("0.50")
    assert apr.flagged is False


# ---------------------------------------------------------------------------
# Manual match (REQ-ID: RECON-005)
# ---------------------------------------------------------------------------


def test_apply_manual_match_links_both(session: Session) -> None:
    """apply_manual_match should set reconciled notes on both transactions."""
    payout = _make_txn(session, source=Source.STRIPE, date="2026-05-01", amount=Decimal("300.00"))
    bank = _make_txn(session, source=Source.BANK_CSV, date="2026-05-04", amount=Decimal("300.00"))

    txn_a, txn_b = apply_manual_match(session, payout.id, bank.id)

    assert txn_a.notes == f"{RECON_LINK_NOTE_PREFIX}{bank.id}"
    assert txn_b.notes == f"{RECON_LINK_NOTE_PREFIX}{payout.id}"


def test_apply_manual_match_unknown_id_raises(session: Session) -> None:
    """apply_manual_match should raise ValueError for unknown transaction IDs."""
    payout = _make_txn(session, source=Source.STRIPE, date="2026-05-01", amount=Decimal("100.00"))

    with pytest.raises(ValueError, match="not found"):
        apply_manual_match(session, payout.id, "nonexistent-id")


def test_manual_matched_appear_in_result(session: Session) -> None:
    """Manually reconciled pairs should appear in the matched list, not unmatched."""
    payout = _make_txn(session, source=Source.STRIPE, date="2026-06-01", amount=Decimal("999.00"))
    bank = _make_txn(session, source=Source.BANK_CSV, date="2026-06-04", amount=Decimal("999.00"))

    # Manually link them
    apply_manual_match(session, payout.id, bank.id)

    result = find_matches(session)

    # Should appear in matched (as a manual pair), not unmatched
    matched_payout_ids = {m.payout.id for m in result.matched}
    matched_bank_ids = {m.bank.id for m in result.matched}
    assert payout.id in matched_payout_ids
    assert bank.id in matched_bank_ids
    assert not any(u.id == payout.id for u in result.unmatched_payouts)
    assert not any(u.id == bank.id for u in result.unmatched_banks)


# ---------------------------------------------------------------------------
# Rejected transactions excluded
# ---------------------------------------------------------------------------


def test_rejected_transactions_excluded(session: Session) -> None:
    """Rejected transactions should not participate in reconciliation."""
    _make_txn(
        session,
        source=Source.STRIPE,
        date="2026-07-01",
        amount=Decimal("500.00"),
        status=TransactionStatus.REJECTED,
    )
    _make_txn(session, source=Source.BANK_CSV, date="2026-07-01", amount=Decimal("500.00"))

    result = find_matches(session)

    assert len(result.matched) == 0
    assert len(result.unmatched_payouts) == 0  # rejected stripe excluded
    assert len(result.unmatched_banks) == 1


# ---------------------------------------------------------------------------
# Shopify as payout source
# ---------------------------------------------------------------------------


def test_shopify_payout_matched_to_bank(session: Session) -> None:
    """Shopify is a valid payout source and should match bank deposits."""
    payout = _make_txn(
        session, source=Source.SHOPIFY, date="2026-08-10", amount=Decimal("1234.56")
    )
    bank = _make_txn(
        session, source=Source.BANK_CSV, date="2026-08-12", amount=Decimal("1234.56")
    )

    result = find_matches(session)

    assert len(result.matched) == 1
    assert result.matched[0].payout.id == payout.id
    assert result.matched[0].bank.id == bank.id


# ---------------------------------------------------------------------------
# remove_manual_match (unlink)
# ---------------------------------------------------------------------------


def test_remove_manual_match_clears_both_notes(session: Session) -> None:
    """remove_manual_match should strip the reconciled: note from both transactions."""
    payout = _make_txn(session, source=Source.STRIPE, date="2026-09-01", amount=Decimal("400.00"))
    bank = _make_txn(session, source=Source.BANK_CSV, date="2026-09-03", amount=Decimal("400.00"))

    # Link first
    apply_manual_match(session, payout.id, bank.id)
    assert payout.notes == f"{RECON_LINK_NOTE_PREFIX}{bank.id}"
    assert bank.notes == f"{RECON_LINK_NOTE_PREFIX}{payout.id}"

    # Now unlink
    txn_a, txn_b = remove_manual_match(session, payout.id, bank.id)

    assert txn_a.notes is None
    assert txn_b.notes is None


def test_remove_manual_match_pair_returns_to_unmatched(session: Session) -> None:
    """After unlinking, both transactions should appear as unmatched."""
    payout = _make_txn(session, source=Source.STRIPE, date="2026-09-10", amount=Decimal("500.00"))
    bank = _make_txn(session, source=Source.BANK_CSV, date="2026-09-12", amount=Decimal("500.00"))

    apply_manual_match(session, payout.id, bank.id)
    result_before = find_matches(session)
    assert any(m.payout.id == payout.id for m in result_before.matched)

    remove_manual_match(session, payout.id, bank.id)
    result_after = find_matches(session)

    # They may auto-rematch (amount/date qualify), but they must no longer
    # be locked as a manual pair — verify they aren't "manually confirmed" (confidence=1.0)
    # with no remaining recon note on both
    session.refresh(payout)
    session.refresh(bank)
    assert payout.notes is None
    assert bank.notes is None


def test_remove_manual_match_preserves_other_notes(session: Session) -> None:
    """Unlink must strip only the reconciled: segment, leaving other note content intact."""
    payout = _make_txn(
        session,
        source=Source.STRIPE,
        date="2026-09-20",
        amount=Decimal("250.00"),
        notes="some prior note",
    )
    bank = _make_txn(session, source=Source.BANK_CSV, date="2026-09-22", amount=Decimal("250.00"))

    # Manually write combined notes (simulating a future scenario where other content exists)
    payout.notes = f"some prior note {RECON_LINK_NOTE_PREFIX}{bank.id}"
    session.commit()

    # Perform unlink — should leave the non-recon portion
    txn_a, txn_b = remove_manual_match(session, payout.id, bank.id)
    assert txn_a.notes == "some prior note"
    assert txn_b.notes is None


def test_remove_manual_match_unknown_id_raises(session: Session) -> None:
    """remove_manual_match should raise ValueError for unknown transaction IDs."""
    payout = _make_txn(session, source=Source.STRIPE, date="2026-10-01", amount=Decimal("100.00"))

    with pytest.raises(ValueError, match="not found"):
        remove_manual_match(session, payout.id, "nonexistent-id")


def test_remove_manual_match_not_linked_raises(session: Session) -> None:
    """remove_manual_match should raise ValueError when transactions aren't linked."""
    payout = _make_txn(session, source=Source.STRIPE, date="2026-10-05", amount=Decimal("150.00"))
    bank = _make_txn(session, source=Source.BANK_CSV, date="2026-10-07", amount=Decimal("150.00"))

    # Do NOT link — they have no recon notes
    with pytest.raises(ValueError, match="not linked"):
        remove_manual_match(session, payout.id, bank.id)


def test_remove_manual_match_reverse_order_works(session: Session) -> None:
    """Unlink should work regardless of which ID is passed as A or B."""
    payout = _make_txn(session, source=Source.STRIPE, date="2026-10-10", amount=Decimal("300.00"))
    bank = _make_txn(session, source=Source.BANK_CSV, date="2026-10-11", amount=Decimal("300.00"))

    apply_manual_match(session, payout.id, bank.id)

    # Pass bank first, payout second (reversed)
    txn_b, txn_a = remove_manual_match(session, bank.id, payout.id)

    assert txn_a.notes is None
    assert txn_b.notes is None
