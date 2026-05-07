"""Seed default tags for every Account based on type + beneficiary + name.

Defaults:
    401k, roth_ira, trad_ira, brokeragelink → "retirement"
    hsa                                      → "tax-advantaged", "retirement"
    529 with beneficiary "Aiden"             → "529", "aiden"
    529 with beneficiary "Emerson"           → "529", "emerson"
    529 (other beneficiary)                  → "529", "<lower(beneficiary)>"
    taxable, tod, joint                      → "taxable"
    rsu                                       → "taxable", "rsu"

Tag values are normalised to lower-case. Re-runs are idempotent — the
composite PK (account_id, tag) catches duplicates.

Usage:
    python -m scripts.seed_account_tags                   # dry-run
    python -m scripts.seed_account_tags --apply           # write
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Iterable
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.db.connection import SessionLocal  # noqa: E402
from src.models.brokerage import Account  # noqa: E402
from src.models.history import AccountTag  # noqa: E402

logger = logging.getLogger(__name__)


def default_tags_for(account: Account) -> list[str]:
    """Return the default tag list for ``account``. Lower-case, deduped."""
    tags: set[str] = set()
    t = (account.account_type or "").lower()

    retirement_types = {"401k", "403b", "roth_ira", "trad_ira", "brokeragelink"}
    if t in retirement_types:
        tags.add("retirement")
        tags.add(t)

    if t == "hsa":
        tags.add("tax-advantaged")
        tags.add("retirement")  # HSAs are de-facto retirement vehicles after 65
        tags.add("hsa")

    if t == "529":
        tags.add("529")
        tags.add("tax-advantaged")
        if account.beneficiary:
            tags.add(account.beneficiary.strip().lower())

    if t in {"taxable", "tod", "joint"}:
        tags.add("taxable")

    if t == "rsu":
        tags.add("taxable")
        tags.add("rsu")

    return sorted(tags)


def seed_tags(session: Session, *, dry_run: bool = True) -> dict[str, int]:
    """Apply default tags. Returns counts: scanned, would_insert, inserted, dup_skipped."""
    counts = {"scanned": 0, "would_insert": 0, "inserted": 0, "dup_skipped": 0}
    for account in session.query(Account).all():
        counts["scanned"] += 1
        for tag in default_tags_for(account):
            existing = (
                session.query(AccountTag)
                .filter(
                    AccountTag.account_id == account.id, AccountTag.tag == tag
                )
                .first()
            )
            if existing is not None:
                counts["dup_skipped"] += 1
                continue
            counts["would_insert"] += 1
            if dry_run:
                continue
            try:
                with session.begin_nested():
                    session.add(AccountTag(account_id=account.id, tag=tag))
                counts["inserted"] += 1
            except IntegrityError:
                counts["dup_skipped"] += 1
    if not dry_run:
        session.commit()
    return counts


def _print_summary(counts: dict[str, int], dry_run: bool) -> None:
    label = "DRY-RUN" if dry_run else "APPLY"
    print(f"[{label}] account_tag seed:")
    for k, v in counts.items():
        print(f"  {k:>14}: {v}")


def main(argv: Iterable[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true", help="Commit (default: dry-run).")
    args = p.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    session = SessionLocal()
    try:
        counts = seed_tags(session, dry_run=not args.apply)
        _print_summary(counts, dry_run=not args.apply)
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
