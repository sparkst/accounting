#!/usr/bin/env python3
"""Mark the 5 retired register account shells from the 2026-07-27 mislinks.

The Schwab/Vanguard wrong-scope links (repaired same night) left 5 register
`account` rows with their plaid ids NULLed. House rule says keep them — the
`remediation:{schwab,vanguard}-scope-fix-2026-07-27` AuditEvents reference
them — but an unmarked empty shell invites future confusion (and each holds a
`uq_account_broker_number` slot). This stamps `notes` and writes an
entity-mode AuditEvent per change.

DRY-RUN by default; --apply to write. Idempotent — already-marked shells are
skipped.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy.orm import Session as SessionType  # noqa: E402

from src.db.connection import SessionLocal, init_db  # noqa: E402
from src.models.audit_event import AuditEvent  # noqa: E402
from src.models.brokerage import Account  # noqa: E402

CHANGED_BY = "remediation:shell-retire-2026-07-27"
MARKER = (
    "RETIRED SHELL (2026-07-27): created by a wrong-scope Plaid link, unmapped "
    "same night; kept for the audit trail — do not map or delete."
)

#: The five shells verified in the 2026-07-27 reliability audit.
SHELL_IDS = (
    "3e3d66ed-1bc1-4454-9419-72cbfb2bf536",  # schwab Joint Tenant
    "c923a39e-96fd-4618-b57a-33b32b7f4ed1",  # schwab AMZN RSU
    "40bcb191-dd64-4aae-8db1-412d133990d1",  # vanguard Roth IRA
    "8d8d2609-e603-4503-8a03-30611eaaed56",  # vanguard Trad IRA
    "a70acddf-861f-4782-9f24-183eacef4d27",  # vanguard Aiden 529
)


def mark_shells(session: SessionType, *, apply: bool) -> tuple[int, int, int]:
    """Returns (marked, already_marked, missing)."""
    now = datetime.now(UTC).replace(tzinfo=None)
    marked = already = missing = 0
    for shell_id in SHELL_IDS:
        acct = session.get(Account, shell_id)
        if acct is None:
            missing += 1
            print(f"  MISSING {shell_id[:8]} — not found (already cleaned?)")
            continue
        if acct.notes and MARKER in acct.notes:
            already += 1
            continue
        if acct.plaid_item_id is not None:
            # Re-mapped since the audit — a live account must not be stamped.
            print(f"  SKIP {shell_id[:8]} {acct.account_name!r} — re-mapped, live")
            continue
        old_notes = acct.notes
        new_notes = f"{old_notes}\n{MARKER}" if old_notes else MARKER
        print(f"  MARK {shell_id[:8]} {acct.account_name!r}")
        if apply:
            acct.notes = new_notes
            session.add(
                AuditEvent(
                    entity_type="account",
                    entity_id=acct.id,
                    field_changed="notes",
                    old_value=old_notes,
                    new_value=new_notes,
                    changed_by=CHANGED_BY,
                    changed_at=now,
                )
            )
        marked += 1
    if apply:
        session.commit()
    return marked, already, missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--apply", action="store_true", help="Write (default: DRY-RUN)."
    )
    args = parser.parse_args(argv)
    init_db()
    session = SessionLocal()
    try:
        marked, already, missing = mark_shells(session, apply=args.apply)
    finally:
        session.close()
    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"mark_retired_shells {mode}: marked={marked} already={already} missing={missing}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
