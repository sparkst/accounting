#!/usr/bin/env python3
"""One-time backfill: stamp ``payment_method='chase_6380'`` on the Plaid rows
ingested while the personal Chase 6380 Account's ``payment_method`` was blank
(REQ-PC-B6 — the box audit found 17 such register rows).

``make_transaction`` stamps ``payment_method`` from the mapped Account at
INSERT time, so rows ingested before ``remediate_plaid_mirrors`` backfilled
the Account label carry a NULL/blank ``payment_method`` forever — dropping
them from every payment_method join (entity stamping context, CSV supersede,
WBR card-payment scoping, and the card-payment ``account_type`` resolution in
``find_card_payment_rows``).

Selection is derived, never hardcoded to 17: ``source='plaid'`` rows with a
blank ``payment_method`` whose ``raw_data.account_id`` equals the CURRENT
``plaid_account_id`` of the personal Chase …6380 account (which must already
carry ``payment_method='chase_6380'`` — the script refuses to guess).

Register invariants honored (same pattern as scripts/remediate_plaid_mirrors):
never deletes, one AuditEvent per field change, per-row savepoints, DRY-RUN
default, idempotent (a second run reports zero changes). Split parents and
children ARE included — ``payment_method`` is a label, not part of the
split-sum structure, and children must join the same account as their parent.

Usage:
    python -m scripts.backfill_chase6380_payment_method            # dry-run
    python -m scripts.backfill_chase6380_payment_method --apply    # commit
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Add project root to path when invoked as a script.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from sqlalchemy import func, or_  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.models.audit_event import AuditEvent  # noqa: E402
from src.models.brokerage import Account  # noqa: E402
from src.models.enums import Entity, Source  # noqa: E402
from src.models.transaction import Transaction  # noqa: E402

logger = logging.getLogger("backfill_chase6380_payment_method")

CHASE_6380_ACCOUNT_SUFFIX = "6380"
CHASE_6380_PAYMENT_METHOD = "chase_6380"

ACTOR = "remediation:chase6380-payment-method-backfill"


@dataclass
class BackfillResult:
    updated: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    #: Non-empty when the target Account could not be resolved unambiguously.
    resolution_error: str | None = None

    @property
    def total_changes(self) -> int:
        return len(self.updated)


def _force_real_transaction(session: Session) -> None:
    """Emit an explicit ``BEGIN`` before the first ``SAVEPOINT``.

    pysqlite does not emit ``BEGIN`` for a ``SAVEPOINT`` statement, so a
    savepoint opened as the first statement of a transaction runs in
    autocommit mode and its ``RELEASE`` COMMITS — silently defeating the
    dry-run rollback. Same guard as scripts/remediate_plaid_mirrors.py.
    """
    connection = session.connection()
    driver_connection = getattr(connection.connection, "driver_connection", None)
    if getattr(driver_connection, "in_transaction", False):
        return
    connection.exec_driver_sql("BEGIN")


def _resolve_chase_6380_account(session: Session, result: BackfillResult) -> Account | None:
    """Find THE personal Chase …6380 account already labeled chase_6380.

    Refuses (with a report, never a guess) when zero or multiple candidates
    match, or when the account has no ``plaid_account_id`` to key rows off.
    """
    accounts = (
        session.query(Account)
        .filter(
            Account.entity == Entity.PERSONAL.value,
            Account.account_number.like(f"%{CHASE_6380_ACCOUNT_SUFFIX}"),
            Account.payment_method == CHASE_6380_PAYMENT_METHOD,
        )
        .all()
    )
    if len(accounts) != 1:
        result.resolution_error = (
            f"expected exactly 1 personal …{CHASE_6380_ACCOUNT_SUFFIX} account with "
            f"payment_method={CHASE_6380_PAYMENT_METHOD!r}, found {len(accounts)} — "
            "run scripts/remediate_plaid_mirrors first (it backfills the Account "
            "label), then re-run"
        )
        return None
    account = accounts[0]
    if not account.plaid_account_id:
        result.resolution_error = (
            f"account {account.account_number} has no plaid_account_id — cannot "
            "attribute register rows to it"
        )
        return None
    return account


def find_blank_rows(session: Session, account: Account) -> list[Transaction]:
    """Plaid register rows on this account's feed with a blank payment_method."""
    return (
        session.query(Transaction)
        .filter(
            Transaction.source == Source.PLAID.value,
            or_(
                Transaction.payment_method.is_(None),
                Transaction.payment_method == "",
            ),
            func.json_extract(Transaction.raw_data, "$.account_id")
            == account.plaid_account_id,
        )
        .order_by(Transaction.date, Transaction.id)
        .all()
    )


def backfill(session: Session, *, apply: bool = False) -> BackfillResult:
    """Stamp payment_method on the blank rows. DRY-RUN unless ``apply``."""
    _force_real_transaction(session)
    result = BackfillResult()
    account = _resolve_chase_6380_account(session, result)
    if account is None:
        return result

    for tx in find_blank_rows(session, account):
        label = f"{tx.date} {str(tx.description or '')[:38]:38} {tx.amount or 0:>11.2f}"
        try:
            with session.begin_nested():
                old = tx.payment_method
                tx.payment_method = CHASE_6380_PAYMENT_METHOD
                session.add(
                    AuditEvent(
                        transaction_id=tx.id,
                        field_changed="payment_method",
                        old_value=old,
                        new_value=CHASE_6380_PAYMENT_METHOD,
                        changed_by=ACTOR,
                    )
                )
                session.flush()
            result.updated.append(label)
        except Exception:
            result.failed.append(label)
            logger.exception(
                "payment_method backfill failed", extra={"transaction_id": tx.id}
            )

    if apply:
        session.commit()
    else:
        session.rollback()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="Commit changes (default: dry-run)."
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    from src.db.connection import get_session  # late import keeps tests light

    session = get_session()
    try:
        result = backfill(session, apply=args.apply)
    finally:
        session.close()

    if result.resolution_error:
        print(f"RESOLUTION ERROR: {result.resolution_error}")
        return 1

    print(f"\nRows stamped payment_method={CHASE_6380_PAYMENT_METHOD!r}: "
          f"{len(result.updated)}")
    for line in result.updated:
        print(f"  {line}")
    if result.failed:
        print(f"\nFAILED rows (isolated, batch continued): {len(result.failed)}")
        for line in result.failed:
            print(f"  {line}")

    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"\n{mode}: {result.total_changes} row(s).")
    if not args.apply:
        print("Rolled back. Re-run with --apply to commit.")
    elif result.total_changes:
        print("Committed; an AuditEvent row was written for every change.")
    else:
        print("Nothing to do — already backfilled.")
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
