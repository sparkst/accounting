"""Seed the LOCAL ``account_alias`` table from a checked-in D1 export (REQ-FIX-WLT-004).

Reads ``config/account_aliases.seed.json`` and upserts rows into the local
SQLite ``account_alias`` table so the networth-history per-name cutoff dedup
matches the sparkry-crm D1 port. ``raw_account_name`` is stored lowercased (the
PK / key-casing contract). Each entry resolves its account by explicit
``account_id`` or by ``(broker, account_name)``.

DRY-RUN by default (per CLAUDE.md); ``--apply`` to write. Idempotent — an
existing alias for a raw name is left untouched (ON CONFLICT DO NOTHING).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy.orm import Session  # noqa: E402

from src.models.brokerage import Account  # noqa: E402
from src.models.history import AccountAlias  # noqa: E402

_DEFAULT_SEED = PROJECT_ROOT / "config" / "account_aliases.seed.json"


@dataclass
class SeedSummary:
    inserted: int = 0
    skipped: int = 0
    unresolved: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.unresolved is None:
            self.unresolved = []


def _resolve_account_id(session: Session, entry: dict) -> str | None:
    if entry.get("account_id"):
        acct = session.query(Account.id).filter(Account.id == entry["account_id"]).first()
        return acct[0] if acct else None
    broker = entry.get("broker")
    name = entry.get("account_name")
    if broker and name:
        acct = (
            session.query(Account.id)
            .filter(Account.broker == broker, Account.account_name == name)
            .first()
        )
        return acct[0] if acct else None
    return None


def seed_aliases(
    session: Session, entries: list[dict], *, apply: bool
) -> SeedSummary:
    summary = SeedSummary()
    for entry in entries:
        raw = str(entry["raw_account_name"]).lower()
        account_id = _resolve_account_id(session, entry)
        if account_id is None:
            summary.unresolved.append(raw)
            continue
        existing = (
            session.query(AccountAlias.raw_account_name)
            .filter(AccountAlias.raw_account_name == raw)
            .first()
        )
        if existing is not None:
            summary.skipped += 1
            continue
        if apply:
            session.add(AccountAlias(raw_account_name=raw, account_id=account_id))
        summary.inserted += 1
    if apply:
        session.commit()
    return summary


def load_entries(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    return list(data.get("aliases", []))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write rows (default: dry-run).")
    parser.add_argument("--seed", default=str(_DEFAULT_SEED), help="Seed JSON path.")
    args = parser.parse_args(argv)

    entries = load_entries(Path(args.seed))
    from src.db.connection import get_session

    with get_session() as session:
        summary = seed_aliases(session, entries, apply=args.apply)
    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(
        f"[seed_local_account_aliases] {mode} "
        f"inserted={summary.inserted} skipped={summary.skipped} "
        f"unresolved={len(summary.unresolved)}"
    )
    for raw in summary.unresolved:
        print(f"  ! unresolved: {raw!r}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
