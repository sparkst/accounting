"""Tests for scripts/remediate_plaid_mirrors.py.

REQ-ID: REQ-WBR-LED-016  Mirror rows (raw_data.account_id in MIRROR_ACCOUNT_IDS)
                         are rejected with review_reason
                         "superseded_by_duplicate_plaid_item"; never deleted.
REQ-ID: REQ-WBR-LED-017  Card-payment legs are reclassified to
                         direction=transfer / tax_category=NULL with the amount
                         untouched; the blank Chase 6380 Account.payment_method
                         is backfilled.
REQ-ID: REQ-WBR-LED-018  DRY-RUN by default, an AuditEvent per field change,
                         per-row savepoints, and idempotent (a second run
                         reports zero changes).
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

import src.models.plaid as _plaid  # noqa: F401  # registers PlaidItem for FK resolution
from scripts.remediate_plaid_mirrors import (
    CHASE_6380_PAYMENT_METHOD,
    MIRROR_ACCOUNT_IDS,
    MIRROR_REVIEW_REASON,
    find_card_payment_rows,
    find_mirror_rows,
    remediate,
)
from src.api.routes.wbr_ledger import ledger_window_rows
from src.models.audit_event import ENTITY_TYPE_ACCOUNT, AuditEvent
from src.models.base import Base
from src.models.brokerage import Account
from src.models.enums import (
    AccountType,
    Direction,
    Entity,
    Source,
    TaxCategory,
    TransactionStatus,
)
from src.models.transaction import Transaction


@pytest.fixture
def db() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _):  # type: ignore[no-untyped-def]
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _tx(
    db: Session,
    *,
    raw_data: dict,
    amount: Decimal = Decimal("-100.00"),
    date: str = "2026-07-20",
    description: str = "Test row",
    direction: str = Direction.EXPENSE.value,
    tax_category: str | None = TaxCategory.OTHER_EXPENSE.value,
    status: str = TransactionStatus.AUTO_CLASSIFIED.value,
    parent_id: str | None = None,
    source: str = Source.PLAID.value,
    payment_method: str | None = None,
) -> Transaction:
    tx = Transaction(
        id=str(uuid.uuid4()),
        source=source,
        source_id=str(uuid.uuid4()),
        source_hash=str(uuid.uuid4()),
        date=date,
        description=description,
        amount=amount,
        currency="USD",
        entity=Entity.PERSONAL.value,
        direction=direction,
        tax_category=tax_category,
        status=status,
        confidence=0.9,
        raw_data=raw_data,
        payment_method=payment_method,
        parent_id=parent_id,
    )
    db.add(tx)
    db.commit()
    return tx


def _mirror_raw(account_id: str = MIRROR_ACCOUNT_IDS[0]) -> dict:
    return {"account_id": account_id, "name": "AMAZON MKTPL"}


def _mapped_raw() -> dict:
    return {"account_id": "acc_real_chase_6372", "name": "AMAZON MKTPL"}


# ── REQ-WBR-LED-016: mirror rejection ────────────────────────────────────────


def test_mirror_rows_rejected_with_supersede_reason(db: Session) -> None:
    mirror = _tx(db, raw_data=_mirror_raw())
    keeper = _tx(db, raw_data=_mapped_raw())

    result = remediate(db, apply=True)

    assert len(result.mirrors) == 1
    db.refresh(mirror)
    db.refresh(keeper)
    assert mirror.status == TransactionStatus.REJECTED.value
    assert mirror.review_reason == MIRROR_REVIEW_REASON
    assert keeper.status == TransactionStatus.AUTO_CLASSIFIED.value


def test_mirror_rows_are_never_deleted(db: Session) -> None:
    """Register invariant: exclusion is a status flip, not a DELETE."""
    _tx(db, raw_data=_mirror_raw())
    remediate(db, apply=True)
    assert db.query(Transaction).count() == 1


def test_all_three_mirror_account_ids_match(db: Session) -> None:
    for account_id in MIRROR_ACCOUNT_IDS:
        _tx(db, raw_data=_mirror_raw(account_id))
    assert len(find_mirror_rows(db)) == 3
    assert len(remediate(db, apply=True).mirrors) == 3


def test_unrecognized_mirror_candidate_is_reported_not_touched(db: Session) -> None:
    """P2-006: an account_id that LOOKS mirror-shaped (a non-rejected plaid
    row whose account_id matches no known Account) but is NOT in
    MIRROR_ACCOUNT_IDS is drift since the point-in-time production audit —
    reported so an operator can add it by hand, never silently rejected."""
    drifted = _tx(
        db, raw_data={"account_id": "acc_new_mirror_candidate", "name": "AMAZON MKTPL"}
    )

    result = remediate(db, apply=True)

    assert result.mirrors == []
    assert result.unrecognized_mirror_candidates == {"acc_new_mirror_candidate": 1}
    db.refresh(drifted)
    assert drifted.status == TransactionStatus.AUTO_CLASSIFIED.value  # untouched


def test_row_matching_a_known_account_is_not_an_unrecognized_candidate(db: Session) -> None:
    """A plaid row whose account_id IS a real, mapped Account is not mirror
    activity at all — it must not show up as drift."""
    db.add(Account(
        broker="chase", account_number="****1234", account_name="Checking",
        account_type=AccountType.CHECKING.value, entity=Entity.PERSONAL.value,
        plaid_account_id="acc_real_mapped",
    ))
    db.commit()
    _tx(db, raw_data={"account_id": "acc_real_mapped", "name": "STARBUCKS"})

    result = remediate(db, apply=True)

    assert result.unrecognized_mirror_candidates == {}


def test_already_rejected_mirror_is_left_alone(db: Session) -> None:
    _tx(db, raw_data=_mirror_raw(), status=TransactionStatus.REJECTED.value)
    assert find_mirror_rows(db) == []
    assert remediate(db, apply=True).mirrors == []


def test_split_mirror_rows_are_skipped_and_reported(db: Session) -> None:
    """Rejecting half a split would break the split-sum invariant."""
    parent = _tx(db, raw_data=_mirror_raw(),
                 status=TransactionStatus.SPLIT_PARENT.value)
    child = _tx(db, raw_data=_mirror_raw(), parent_id=parent.id)

    result = remediate(db, apply=True)

    assert result.mirrors == []
    assert len(result.skipped_splits) == 2
    db.refresh(parent)
    db.refresh(child)
    assert parent.status == TransactionStatus.SPLIT_PARENT.value
    assert child.status == TransactionStatus.AUTO_CLASSIFIED.value


# ── REQ-WBR-LED-017: card-payment reclassification ───────────────────────────


def _credit_card_account(db: Session, payment_method: str) -> Account:
    """P1-d7e: `find_card_payment_rows` scopes the bare
    `transaction_code == "payment"` signal to credit_card accounts (resolved
    via the payment_method join, since a stored row has no account FK) — seed
    one so the tests below model a REAL card-side payment leg."""
    account = Account(
        broker="amex", account_number="****9999", account_name="Amex",
        account_type=AccountType.CREDIT_CARD.value, entity=Entity.PERSONAL.value,
        payment_method=payment_method,
    )
    db.add(account)
    db.commit()
    return account


def test_card_side_payment_credit_reclassified_to_transfer(db: Session) -> None:
    """The +1637.65 Amex credit that showed up as income in the WBR ledger."""
    _credit_card_account(db, "amex_31004")
    card_leg = _tx(
        db,
        raw_data={"account_id": "acc_amex_31004", "name": "ONLINE PAYMENT - THANK YOU",
                  "transaction_code": "payment"},
        amount=Decimal("1637.65"),
        date="2026-07-19",
        direction=Direction.INCOME.value,
        tax_category=TaxCategory.CONSULTING_INCOME.value,
        payment_method="amex_31004",
    )

    result = remediate(db, apply=True)

    assert len(result.card_payments) == 1
    db.refresh(card_leg)
    assert card_leg.direction == Direction.TRANSFER.value
    assert card_leg.tax_category is None
    assert card_leg.deductible_pct == 0.0
    # The amount is evidence from the source and is never rewritten.
    assert card_leg.amount == Decimal("1637.65")


def test_checking_side_ach_debit_reclassified_to_transfer(db: Session) -> None:
    """The matching -1637.65 Chase debit, matched on the bank descriptor."""
    checking_leg = _tx(
        db,
        raw_data={
            "account_id": "acc_chase_6372",
            "name": "ORIG CO NAME:AMERICAN EXPRESS ORIG ID:9493560001 "
                    "CO ENTRY DESCR:ACH PMT SEC:WEB",
        },
        amount=Decimal("-1637.65"),
        date="2026-07-20",
    )

    remediate(db, apply=True)

    db.refresh(checking_leg)
    assert checking_leg.direction == Direction.TRANSFER.value
    assert checking_leg.amount == Decimal("-1637.65")


def test_pfc_detailed_card_payment_reclassified(db: Session) -> None:
    row = _tx(
        db,
        raw_data={
            "account_id": "acc_chase_card",
            "name": "PAYMENT",
            "personal_finance_category": {
                "primary": "LOAN_PAYMENTS",
                "detailed": "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT",
            },
        },
        amount=Decimal("-588.78"),
    )

    remediate(db, apply=True)

    db.refresh(row)
    assert row.direction == Direction.TRANSFER.value


def test_ordinary_expense_and_non_plaid_rows_untouched(db: Session) -> None:
    """Only source=plaid rows matching the card-payment signals are candidates."""
    ordinary = _tx(db, raw_data={"account_id": "acc_x", "name": "STARBUCKS #123"})
    merchant_autopay = _tx(
        db, raw_data={"account_id": "acc_x", "name": "VERIZON WIRELESS AUTOPAY"}
    )
    csv_row = _tx(
        db,
        raw_data={"name": "CHASE CREDIT CRD AUTOPAY PMT"},
        source=Source.BANK_CSV.value,
    )

    result = remediate(db, apply=True)

    assert result.card_payments == []
    for row in (ordinary, merchant_autopay, csv_row):
        db.refresh(row)
        assert row.direction == Direction.EXPENSE.value


def test_rejected_mirror_is_not_also_reclassified(db: Session) -> None:
    """A mirror row that is ALSO a card-payment leg is rejected once and then
    excluded from the reclass pass — remediation must not double-count."""
    row = _tx(
        db,
        raw_data={"account_id": MIRROR_ACCOUNT_IDS[1],
                  "name": "CHASE CREDIT CRD AUTOPAY PMT"},
        amount=Decimal("-588.78"),
    )

    result = remediate(db, apply=True)

    assert len(result.mirrors) == 1
    assert result.card_payments == []
    db.refresh(row)
    assert row.status == TransactionStatus.REJECTED.value
    assert row.direction == Direction.EXPENSE.value


def test_chase_6380_payment_method_backfilled(db: Session) -> None:
    account = Account(
        broker="chase", account_number="****6380", account_name="Chase Personal",
        account_type=AccountType.CHECKING.value, entity=Entity.PERSONAL.value,
        payment_method=None,
    )
    db.add(account)
    db.commit()

    result = remediate(db, apply=True)

    assert len(result.accounts) == 1
    db.refresh(account)
    assert account.payment_method == CHASE_6380_PAYMENT_METHOD
    events = (
        db.query(AuditEvent)
        .filter_by(entity_id=account.id, entity_type=ENTITY_TYPE_ACCOUNT)
        .all()
    )
    assert [e.field_changed for e in events] == ["payment_method"]


def test_ambiguous_chase_6380_match_is_skipped_not_guessed(db: Session) -> None:
    for suffix in ("****6380", "9996380"):
        db.add(Account(
            broker="chase", account_number=suffix, account_name="Chase",
            account_type=AccountType.CHECKING.value, entity=Entity.PERSONAL.value,
            payment_method=None,
        ))
    db.commit()

    result = remediate(db, apply=True)

    assert result.accounts == []
    assert any("resolve by hand" in line for line in result.skipped_splits)


# ── REQ-WBR-LED-018: dry-run, audit trail, idempotency ───────────────────────


def test_dry_run_writes_nothing(db: Session) -> None:
    _credit_card_account(db, "amex")
    mirror = _tx(db, raw_data=_mirror_raw())
    card_leg = _tx(
        db,
        raw_data={"account_id": "acc_amex", "name": "PAYMENT",
                  "transaction_code": "payment"},
        amount=Decimal("1637.65"),
        direction=Direction.INCOME.value,
        payment_method="amex",
    )

    result = remediate(db, apply=False)

    # The plan is still reported in full...
    assert len(result.mirrors) == 1
    assert len(result.card_payments) == 1
    # ...but nothing survived the rollback, audit rows included.
    db.expire_all()
    assert db.get(Transaction, mirror.id).status == (
        TransactionStatus.AUTO_CLASSIFIED.value
    )
    assert db.get(Transaction, card_leg.id).direction == Direction.INCOME.value
    assert db.query(AuditEvent).count() == 0


def test_every_field_change_writes_an_audit_event(db: Session) -> None:
    _credit_card_account(db, "amex")
    mirror = _tx(db, raw_data=_mirror_raw())
    card_leg = _tx(
        db,
        raw_data={"account_id": "acc_amex", "name": "PAYMENT",
                  "transaction_code": "payment"},
        amount=Decimal("1637.65"),
        direction=Direction.INCOME.value,
        tax_category=TaxCategory.CONSULTING_INCOME.value,
        payment_method="amex",
    )

    remediate(db, apply=True)

    mirror_fields = sorted(
        e.field_changed
        for e in db.query(AuditEvent).filter_by(transaction_id=mirror.id).all()
    )
    assert mirror_fields == ["review_reason", "status"]
    card_events = db.query(AuditEvent).filter_by(transaction_id=card_leg.id).all()
    # P2-007: deductible_pct is now part of the shared field set the
    # remediation and the live adapter both apply — this row's default
    # (1.0, Transaction's model default) differs from the target 0.0, so it
    # gets its own audited change alongside direction/tax_category.
    assert sorted(e.field_changed for e in card_events) == [
        "deductible_pct", "direction", "tax_category"
    ]
    assert all(e.changed_by.startswith("remediation:") for e in card_events)


def test_second_run_is_a_no_op(db: Session) -> None:
    _credit_card_account(db, "amex")
    _tx(db, raw_data=_mirror_raw())
    _tx(
        db,
        raw_data={"account_id": "acc_amex", "name": "PAYMENT",
                  "transaction_code": "payment"},
        amount=Decimal("1637.65"),
        direction=Direction.INCOME.value,
        payment_method="amex",
    )
    db.add(Account(
        broker="chase", account_number="****6380", account_name="Chase Personal",
        account_type=AccountType.CHECKING.value, entity=Entity.PERSONAL.value,
        payment_method=None,
    ))
    db.commit()

    first = remediate(db, apply=True)
    assert first.total_changes == 3

    audit_count = db.query(AuditEvent).count()
    second = remediate(db, apply=True)

    assert second.total_changes == 0
    assert find_mirror_rows(db) == []
    assert find_card_payment_rows(db) == []
    assert db.query(AuditEvent).count() == audit_count


def test_one_bad_row_does_not_halt_the_batch(db: Session) -> None:
    """Per-row savepoints (REQ-WBR-LED-018): a failure is isolated and reported
    while every other row still applies."""
    good_a = _tx(db, raw_data=_mirror_raw(), date="2026-07-01")
    exploding = _tx(db, raw_data=_mirror_raw(), date="2026-07-02")
    good_b = _tx(db, raw_data=_mirror_raw(), date="2026-07-03")

    real_flush = Session.flush

    def _flush(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if exploding in self.dirty:
            raise RuntimeError("simulated write failure")
        return real_flush(self, *args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Session, "flush", _flush)
        result = remediate(db, apply=True)

    assert len(result.mirrors) == 2
    assert len(result.failed) == 1
    db.expire_all()
    assert db.get(Transaction, good_a.id).status == TransactionStatus.REJECTED.value
    assert db.get(Transaction, good_b.id).status == TransactionStatus.REJECTED.value
    assert db.get(Transaction, exploding.id).status == (
        TransactionStatus.AUTO_CLASSIFIED.value
    )


def test_rejected_rows_drop_out_of_the_wbr_ledger(db: Session) -> None:
    """REQ-WBR-LED-005/016: the whole point of the remediation — a rejected
    mirror no longer reaches the ledger feed, so it can't double-count in the
    week's money-in/money-out.

    P2-h8d: calls the REAL query builder (``ledger_window_rows``, shared with
    the accounting endpoint) rather than a hand-copied re-implementation of
    its filter — a copy cannot catch drift between the two."""
    from datetime import date as _date

    mirror = _tx(db, raw_data=_mirror_raw(), amount=Decimal("-238.03"),
                 date="2026-07-22")
    _tx(db, raw_data=_mapped_raw(), amount=Decimal("-238.03"), date="2026-07-22")

    def _ledger_rows() -> list[Transaction]:
        return ledger_window_rows(
            db,
            start=_date(2026, 7, 20),
            end=_date(2026, 7, 26),
            entity=Entity.PERSONAL.value,
        )

    assert len(_ledger_rows()) == 2  # the duplicate is visible before remediation

    remediate(db, apply=True)

    remaining = _ledger_rows()
    assert len(remaining) == 1
    assert mirror.id not in {row.id for row in remaining}
