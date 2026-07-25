#!/usr/bin/env python3
"""One-time remediation for the duplicate-Chase-Item incident (2026-07-24).

Two defects wrote bad rows into the register before the ingest-side fixes
(REQ-WBR-LED-014/015) landed. This script corrects the rows already stored.

  1. REQ-WBR-LED-016 — phantom MIRROR rows.
     Two active Chase Plaid Items cover the same bank login, so
     ``/transactions/sync`` on each returns all three Chase accounts under
     item-scoped ``account_id``s. The adapter used to ingest every returned
     transaction even when the account_id was not one the Item owned, producing
     50 duplicate rows (100% overlap with their correctly-mapped twins) under
     the three mirror account_ids in ``MIRROR_ACCOUNT_IDS``. Corrected to
     ``status="rejected"``, ``review_reason="superseded_by_duplicate_plaid_item"``.

  2. REQ-WBR-LED-017 — credit-card payment legs classified as income/expense.
     A card payoff is one internal transfer with two legs, but the classifier
     read the card-side credit as income and the checking-side debit as an
     expense — so a single $1,637.65 Amex payoff appeared as BOTH +1637.65
     income and -1637.65 expense in the same week's ledger. Corrected to
     ``direction="transfer"`` with ``tax_category=NULL``; the amount is never
     touched. Selection uses ``card_payment_signal_for_raw`` — the SAME
     implementation the live adapter uses, so the remediation and future
     ingests cannot drift apart. Also backfills the Chase 6380 personal
     ``Account.payment_method``, whose blank value is what left that account
     unable to participate in the payment_method join.

Register invariants honored (REQ-WBR-LED-018): rows are never deleted, every
field change writes an ``AuditEvent``, each row is wrapped in its own savepoint
so one bad row cannot halt the batch, and split parents / split children are
left alone and reported (rejecting or re-directing half a split would break the
split-sum invariant). DRY-RUN by default; re-running after ``--apply`` reports
zero changes.

Usage:
    python -m scripts.remediate_plaid_mirrors            # dry-run
    python -m scripts.remediate_plaid_mirrors --apply    # commit
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from src.adapters.plaid_transactions import card_payment_signal_for_raw
from src.db.connection import get_session
from src.models.audit_event import ENTITY_TYPE_ACCOUNT, AuditEvent
from src.models.brokerage import Account
from src.models.enums import Direction, Entity, Source, TransactionStatus
from src.models.transaction import Transaction

logger = logging.getLogger("remediate_plaid_mirrors")

#: Item-scoped Plaid account_ids that the DUPLICATE Chase Item reported for
#: accounts it does not own. Verified read-only against production on
#: 2026-07-24: 17 / 19 / 14 rows respectively, every one a duplicate of a row
#: already ingested under the correctly-mapped account.
MIRROR_ACCOUNT_IDS = (
    "rJLQP5OJJmTx1wPD4aEBI7QKLYYRadiVYdQAB",
    "Z0p7Yzg0MqI1x0rBjgnjs8zZnk6ek8F88QaKg",
    "8wBN3pLwXKUVx51oRzERUnR9J0b40nFYY54X4",
)

MIRROR_REVIEW_REASON = "superseded_by_duplicate_plaid_item"

#: The personal Chase account whose payment_method was never populated. Matched
#: on the account-number suffix + entity rather than a hardcoded row id so the
#: script is runnable against any copy of the DB; an ambiguous match is
#: reported and skipped rather than guessed at.
CHASE_6380_ACCOUNT_SUFFIX = "6380"
CHASE_6380_PAYMENT_METHOD = "chase_6380"

ACTOR = "remediation:plaid-mirror-2026-07-24"

#: Statuses whose rows are structural, not classifications — never mutated here.
_SPLIT_PARENT = TransactionStatus.SPLIT_PARENT.value
_REJECTED = TransactionStatus.REJECTED.value


# ---------------------------------------------------------------------------
# Result reporting
# ---------------------------------------------------------------------------


@dataclass
class Change:
    """One planned or applied field change, for the dry-run/apply table."""

    target: str          # short row identity (date + description, or account no.)
    detail: str          # why it matched
    field: str
    old: str | None
    new: str | None


@dataclass
class RemediationResult:
    mirrors: list[Change] = field(default_factory=list)
    card_payments: list[Change] = field(default_factory=list)
    accounts: list[Change] = field(default_factory=list)
    #: Rows that MATCHED but were deliberately left alone (split parents /
    #: children). Surfaced so a human can resolve them by hand.
    skipped_splits: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def total_changes(self) -> int:
        return len(self.mirrors) + len(self.card_payments) + len(self.accounts)


def _audit_transaction(
    session: Session, tx: Transaction, *, field_changed: str, old: object, new: object
) -> None:
    """Transaction-mode AuditEvent (mirrors src/api/routes/transactions.py)."""
    session.add(
        AuditEvent(
            transaction_id=tx.id,
            field_changed=field_changed,
            old_value=None if old is None else str(old),
            new_value=None if new is None else str(new),
            changed_by=ACTOR,
        )
    )


def _audit_account(
    session: Session, account: Account, *, field_changed: str, old: object, new: object
) -> None:
    """Entity-mode AuditEvent (mirrors src/api/routes/plaid.py::_write_audit)."""
    session.add(
        AuditEvent(
            entity_id=account.id,
            entity_type=ENTITY_TYPE_ACCOUNT,
            field_changed=field_changed,
            old_value=None if old is None else str(old),
            new_value=None if new is None else str(new),
            changed_by=ACTOR,
        )
    )


def _label(tx: Transaction) -> str:
    return f"{tx.date} {str(tx.description or '')[:38]:38} {float(tx.amount or 0):>11.2f}"


# ---------------------------------------------------------------------------
# Defect 1 — phantom mirror rows (REQ-WBR-LED-016)
# ---------------------------------------------------------------------------


def find_mirror_rows(session: Session) -> list[Transaction]:
    """Non-rejected register rows carrying a mirror ``raw_data.account_id``.

    ``json_extract`` keeps the scan in SQLite rather than deserialising every
    plaid row's raw_data in Python.
    """
    return (
        session.query(Transaction)
        .filter(
            func.json_extract(Transaction.raw_data, "$.account_id").in_(
                MIRROR_ACCOUNT_IDS
            ),
            Transaction.status != _REJECTED,
        )
        .order_by(Transaction.date, Transaction.id)
        .all()
    )


def reject_mirror_rows(
    session: Session, rows: list[Transaction], result: RemediationResult
) -> None:
    """Reject each mirror row in its own savepoint. Never deletes."""
    for tx in rows:
        if tx.status == _SPLIT_PARENT or tx.parent_id is not None:
            # Rejecting half a split breaks the split-sum invariant; a human
            # has to unwind these two by hand.
            result.skipped_splits.append(f"mirror (split) {_label(tx)}")
            continue
        try:
            with session.begin_nested():
                old_status, old_reason = tx.status, tx.review_reason
                tx.status = _REJECTED
                tx.review_reason = MIRROR_REVIEW_REASON
                _audit_transaction(
                    session, tx, field_changed="status", old=old_status, new=_REJECTED
                )
                _audit_transaction(
                    session, tx, field_changed="review_reason",
                    old=old_reason, new=MIRROR_REVIEW_REASON,
                )
                session.flush()
            account_id = (tx.raw_data or {}).get("account_id")
            result.mirrors.append(
                Change(_label(tx), f"account_id={account_id}", "status",
                       old_status, _REJECTED)
            )
        except Exception:
            result.failed.append(f"mirror {_label(tx)}")
            logger.exception("mirror rejection failed", extra={"transaction_id": tx.id})


# ---------------------------------------------------------------------------
# Defect 2 — card-payment legs (REQ-WBR-LED-017)
# ---------------------------------------------------------------------------


def find_card_payment_rows(session: Session) -> list[tuple[Transaction, str]]:
    """Non-rejected plaid rows matching the REQ-WBR-LED-015 card-payment rules.

    Rows already at ``direction="transfer"`` with a NULL ``tax_category`` are
    excluded in SQL, which is what makes a second run report zero changes.
    """
    candidates = (
        session.query(Transaction)
        .filter(
            Transaction.source == Source.PLAID.value,
            Transaction.status != _REJECTED,
            or_(
                Transaction.direction != Direction.TRANSFER.value,
                Transaction.direction.is_(None),
                Transaction.tax_category.isnot(None),
            ),
        )
        .order_by(Transaction.date, Transaction.id)
        .all()
    )
    matched: list[tuple[Transaction, str]] = []
    for tx in candidates:
        signal = card_payment_signal_for_raw(tx.raw_data)
        if signal is not None:
            matched.append((tx, signal))
    return matched


def reclassify_card_payments(
    session: Session, rows: list[tuple[Transaction, str]], result: RemediationResult
) -> None:
    """Set ``direction=transfer`` / ``tax_category=NULL``. Amount is NOT touched."""
    for tx, signal in rows:
        if tx.status == _SPLIT_PARENT or tx.parent_id is not None:
            # Re-directing a parent without its children (or vice versa) would
            # leave the two halves of one split disagreeing about P&L.
            result.skipped_splits.append(f"card-payment (split) {_label(tx)}")
            continue
        try:
            with session.begin_nested():
                old_direction, old_category = tx.direction, tx.tax_category
                if old_direction != Direction.TRANSFER.value:
                    _audit_transaction(
                        session, tx, field_changed="direction",
                        old=old_direction, new=Direction.TRANSFER.value,
                    )
                if old_category is not None:
                    _audit_transaction(
                        session, tx, field_changed="tax_category",
                        old=old_category, new=None,
                    )
                tx.direction = Direction.TRANSFER.value
                tx.tax_category = None
                session.flush()
            result.card_payments.append(
                Change(_label(tx), f"{signal} status={tx.status}", "direction",
                       old_direction, Direction.TRANSFER.value)
            )
        except Exception:
            result.failed.append(f"card-payment {_label(tx)}")
            logger.exception("card-payment reclass failed",
                             extra={"transaction_id": tx.id})


# ---------------------------------------------------------------------------
# Defect 2b — Chase 6380 payment_method backfill (REQ-WBR-LED-017)
# ---------------------------------------------------------------------------


def backfill_chase_6380_payment_method(
    session: Session, result: RemediationResult
) -> None:
    """Populate the blank ``payment_method`` on the personal Chase 6380 account."""
    accounts = (
        session.query(Account)
        .filter(
            Account.entity == Entity.PERSONAL.value,
            Account.account_number.like(f"%{CHASE_6380_ACCOUNT_SUFFIX}"),
            or_(Account.payment_method.is_(None), Account.payment_method == ""),
        )
        .all()
    )
    if len(accounts) > 1:
        # Never guess which of several accounts the label belongs to — the
        # payment_method IS the register's join key.
        result.skipped_splits.append(
            f"chase {CHASE_6380_ACCOUNT_SUFFIX}: {len(accounts)} candidate accounts, "
            "resolve by hand"
        )
        return
    for account in accounts:
        try:
            with session.begin_nested():
                old = account.payment_method
                account.payment_method = CHASE_6380_PAYMENT_METHOD
                _audit_account(
                    session, account, field_changed="payment_method",
                    old=old, new=CHASE_6380_PAYMENT_METHOD,
                )
                session.flush()
            result.accounts.append(
                Change(f"account {account.account_number}", account.account_name or "",
                       "payment_method", old, CHASE_6380_PAYMENT_METHOD)
            )
        except Exception:
            result.failed.append(f"account {account.account_number}")
            logger.exception("payment_method backfill failed",
                             extra={"account_id": account.id})


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _force_real_transaction(session: Session) -> None:
    """Emit an explicit ``BEGIN`` before the first ``SAVEPOINT``.

    pysqlite does not emit ``BEGIN`` for a ``SAVEPOINT`` statement, so a
    savepoint opened as the first statement of a transaction runs while SQLite
    is still in autocommit mode — SQLite then treats it as the outermost
    savepoint and its ``RELEASE`` COMMITS. Without this, the dry run's
    ``session.rollback()`` would silently keep every write (verified: the first
    version of this script committed its dry run). An explicit ``BEGIN`` opens
    a real transaction first, after which savepoints nest and roll back
    correctly.
    """
    connection = session.connection()
    driver_connection = getattr(connection.connection, "driver_connection", None)
    if getattr(driver_connection, "in_transaction", False):
        return
    connection.exec_driver_sql("BEGIN")


def remediate(session: Session, *, apply: bool = False) -> RemediationResult:
    """Run all three corrections. DRY-RUN unless ``apply`` is True.

    The caller keeps the session open; a dry run rolls back so nothing written
    here (including the AuditEvent rows) survives.
    """
    _force_real_transaction(session)
    result = RemediationResult()
    reject_mirror_rows(session, find_mirror_rows(session), result)
    reclassify_card_payments(session, find_card_payment_rows(session), result)
    backfill_chase_6380_payment_method(session, result)
    if apply:
        session.commit()
    else:
        session.rollback()
    return result


def _print_section(title: str, changes: list[Change]) -> None:
    print(f"\n{title}: {len(changes)} change(s)")
    for change in changes:
        print(
            f"  {change.target}  {change.field}: "
            f"{change.old or '-'} -> {change.new or 'NULL'}   [{change.detail}]"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="Commit changes (default: dry-run)."
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    session = get_session()
    try:
        result = remediate(session, apply=args.apply)
    finally:
        session.close()

    _print_section("Mirror rows rejected (REQ-WBR-LED-016)", result.mirrors)
    _print_section("Card-payment legs -> transfer (REQ-WBR-LED-017)",
                   result.card_payments)
    _print_section("Account payment_method backfilled (REQ-WBR-LED-017)",
                   result.accounts)

    if result.skipped_splits:
        print(f"\nSkipped, needs a human: {len(result.skipped_splits)}")
        for line in result.skipped_splits:
            print(f"  {line}")
    if result.failed:
        print(f"\nFAILED rows (isolated, batch continued): {len(result.failed)}")
        for line in result.failed:
            print(f"  {line}")

    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"\n{mode}: {result.total_changes} change(s) across "
          f"{len(result.mirrors)} mirror row(s), {len(result.card_payments)} "
          f"card-payment leg(s), {len(result.accounts)} account(s).")
    if not args.apply:
        print("Rolled back. Re-run with --apply to commit.")
    elif result.total_changes:
        print("Committed; an AuditEvent row was written for every field change.")
    else:
        print("Nothing to do — already remediated.")
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
