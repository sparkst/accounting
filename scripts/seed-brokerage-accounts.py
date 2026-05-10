#!/usr/bin/env python
"""Seed/enrich brokerage account metadata after first ingest.

Sets account_type, tax_sheltered, beneficiary, parent_account_id, and notes
for known accounts. Idempotent — safe to re-run.

Usage:
    python scripts/seed-brokerage-accounts.py            # apply changes
    python scripts/seed-brokerage-accounts.py --dry-run  # show what would change

REQ-005a.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from sqlalchemy.orm import Session

from src.db.connection import SessionLocal
from src.models.brokerage import Account
from src.models.enums import AccountType, Broker, Entity


@dataclass(frozen=True)
class AccountSpec:
    broker: Broker
    account_number: str
    account_type: AccountType
    tax_sheltered: bool
    is_plan_wrapper: bool = False
    parent_account_number: str | None = None  # resolved to FK id at apply time
    beneficiary: str | None = None
    notes: str | None = None


# Hard-coded mapping derived from the user's account inventory in IDEATION.md.
# Source: proposals/brokerage-ingest/IDEATION.md and PLAN.md TASK-11.
SEEDS: list[AccountSpec] = [
    # Fidelity
    AccountSpec(Broker.FIDELITY, "Z23257759", AccountType.TOD, False, beneficiary="Travis"),
    AccountSpec(
        Broker.FIDELITY,
        "89766",
        AccountType.K401,
        True,
        is_plan_wrapper=True,
        beneficiary="Travis",
    ),
    AccountSpec(
        Broker.FIDELITY,
        "653373015",
        AccountType.BROKERAGELINK,
        True,
        parent_account_number="89766",
        beneficiary="Travis",
    ),
    AccountSpec(Broker.FIDELITY, "241527012", AccountType.HSA, True, beneficiary="Travis"),
    # Schwab — account_number suffix as exposed in CSV filenames.
    # The Schwab adapter creates accounts with masked numbers like '724', '144'.
    AccountSpec(Broker.SCHWAB, "724", AccountType.JOINT, False, beneficiary="Travis (joint w/ Amy)"),
    AccountSpec(Broker.SCHWAB, "144", AccountType.RSU, False, beneficiary="Travis"),
    # E*TRADE
    AccountSpec(Broker.ETRADE, "6354", AccountType.TAXABLE, False, beneficiary="Travis"),
    # Vanguard — current accounts
    AccountSpec(Broker.VANGUARD, "65344815", AccountType.TAXABLE, False, beneficiary="Travis"),
    AccountSpec(Broker.VANGUARD, "70862729", AccountType.TAXABLE, False, beneficiary="Travis"),
    AccountSpec(
        Broker.VANGUARD, "208182839-01", AccountType.K529, True, beneficiary="Aiden"
    ),
    AccountSpec(
        Broker.VANGUARD, "252341309-01", AccountType.K529, True, beneficiary="Emerson"
    ),
    # Vanguard — historic accounts found in OfxDownload copy.csv
    AccountSpec(
        Broker.VANGUARD,
        "37737894",
        AccountType.TAXABLE,
        False,
        notes="historic — verify with user",
    ),
    AccountSpec(
        Broker.VANGUARD,
        "32628019",
        AccountType.TAXABLE,
        False,
        notes="historic — verify with user",
    ),
    AccountSpec(
        Broker.VANGUARD,
        "59309844",
        AccountType.TAXABLE,
        False,
        notes="historic — verify with user",
    ),
]


def _find_account(session: Session, broker: str, account_number: str) -> Account | None:
    return (
        session.query(Account)
        .filter(Account.broker == broker, Account.account_number == account_number)
        .one_or_none()
    )


def _diff(spec: AccountSpec, acct: Account, parent_id: str | None) -> dict[str, tuple[object, object]]:
    """Return field → (current, target) for fields that differ."""
    target = {
        "account_type": spec.account_type.value,
        "tax_sheltered": spec.tax_sheltered,
        "is_plan_wrapper": spec.is_plan_wrapper,
        "beneficiary": spec.beneficiary,
        "notes": spec.notes,
        "parent_account_id": parent_id,
        "entity": Entity.PERSONAL.value,
    }
    diff: dict[str, tuple[object, object]] = {}
    for field, target_val in target.items():
        current = getattr(acct, field)
        if current != target_val:
            diff[field] = (current, target_val)
    return diff


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without applying",
    )
    args = parser.parse_args(argv)

    session = SessionLocal()
    changed = 0
    skipped_missing: list[tuple[str, str]] = []
    try:
        # Two passes: pass 1 enriches non-FK fields; pass 2 sets parent_account_id.
        for spec in SEEDS:
            acct = _find_account(session, spec.broker.value, spec.account_number)
            if acct is None:
                skipped_missing.append((spec.broker.value, spec.account_number))
                continue
            parent_id: str | None = None
            if spec.parent_account_number:
                parent = _find_account(
                    session, spec.broker.value, spec.parent_account_number
                )
                if parent is not None:
                    parent_id = parent.id
            diff = _diff(spec, acct, parent_id)
            if not diff:
                continue
            print(f"{spec.broker.value}/{spec.account_number}:")
            for field, (cur, tgt) in diff.items():
                print(f"  {field}: {cur!r} -> {tgt!r}")
                if not args.dry_run:
                    setattr(acct, field, tgt)
            changed += 1

        if not args.dry_run:
            session.commit()
            print(f"\nApplied changes to {changed} account(s).")
        else:
            print(f"\nDRY-RUN: {changed} account(s) would be updated.")

        if skipped_missing:
            print()
            print(
                f"NOTE: {len(skipped_missing)} seed entries had no matching DB row "
                "(account not yet ingested):"
            )
            for broker, num in skipped_missing:
                print(f"  - {broker} / {num}")
    finally:
        session.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
