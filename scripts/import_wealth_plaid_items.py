#!/usr/bin/env python3
"""Import wealth-side Plaid Items into the box register — REQ-PC-B4.

Reads a JSON list from **STDIN ONLY** (never a file — plaintext access tokens
must not touch disk):

    [{"item_id": "...", "institution_id": "ins_...",
      "institution_name": "...", "access_token_plain": "access-production-..."}]

For each entry: Fernet-encrypts the token via ``src.utils.plaid_crypto``
(PLAID_FERNET_KEY) and inserts a ``plaid_item`` row with ``scope='wealth'``,
``status='active'``, plus one entity-mode AuditEvent. Idempotent on
``item_id`` — existing Items are skipped and counted, never overwritten.

The cutover orchestrator produces the stdin payload by decrypting the D1
tokens with ``PLAID_TOKEN_ENC_KEY_MIGRATION`` (Doppler ``accounting/prd``);
this script never touches D1 or that key. Tokens are NEVER logged or printed.

DRY-RUN default per CLAUDE.md. Usage:

    doppler run -- python -m scripts.import_wealth_plaid_items < items.json   # dry-run
    ... | doppler run -- python -m scripts.import_wealth_plaid_items --apply  # commit
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

# Add project root to path when invoked as a script.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from sqlalchemy.orm import Session  # noqa: E402

from src.models.audit_event import ENTITY_TYPE_PLAID_ITEM, AuditEvent  # noqa: E402
from src.models.plaid import PlaidItem  # noqa: E402
from src.utils.plaid_crypto import PlaidCryptoError, encrypt_token  # noqa: E402

logger = logging.getLogger("import_wealth_plaid_items")

ACTOR = "import_wealth_plaid_items"

_REQUIRED_KEYS = ("item_id", "institution_id", "institution_name", "access_token_plain")


@dataclass
class ImportResult:
    imported: list[str] = field(default_factory=list)  # institution names
    skipped_existing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


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


def _validate_entry(index: int, entry: Any) -> str | None:
    """Return an error string, or None when the entry is well-formed.

    Error strings never include token material.
    """
    if not isinstance(entry, dict):
        return f"entry[{index}]: not an object"
    for key in _REQUIRED_KEYS:
        value = entry.get(key)
        if not isinstance(value, str) or not value.strip():
            return f"entry[{index}]: missing/empty {key!r}"
    return None


def import_items(
    session: Session, entries: list[Any], *, apply: bool = False
) -> ImportResult:
    """Insert wealth-scope PlaidItem rows. DRY-RUN unless ``apply``.

    Per-row savepoint isolation; idempotent on ``item_id`` (existing rows are
    skipped, never touched). One entity-mode AuditEvent per inserted Item.
    """
    _force_real_transaction(session)
    result = ImportResult()
    for index, entry in enumerate(entries):
        err = _validate_entry(index, entry)
        if err is not None:
            result.errors.append(err)
            continue
        item_id = entry["item_id"].strip()
        institution_name = entry["institution_name"].strip()
        existing = session.query(PlaidItem).filter_by(item_id=item_id).first()
        if existing is not None:
            result.skipped_existing.append(
                f"{institution_name} (item {item_id[:8]}… already present, "
                f"scope={existing.scope})"
            )
            continue
        try:
            with session.begin_nested():
                item = PlaidItem(
                    item_id=item_id,
                    institution_id=entry["institution_id"].strip(),
                    institution_name=institution_name,
                    access_token_encrypted=encrypt_token(entry["access_token_plain"]),
                    scope="wealth",
                    status="active",
                )
                session.add(item)
                session.flush()  # assign item.id for the audit row
                session.add(
                    AuditEvent(
                        entity_id=item.id,
                        entity_type=ENTITY_TYPE_PLAID_ITEM,
                        field_changed="connect",
                        old_value=None,
                        new_value=(
                            f"{institution_name} ({item_id}) "
                            "[migrated-to-books 2026-07-25, scope=wealth]"
                        ),
                        changed_by=ACTOR,
                    )
                )
                session.flush()
            result.imported.append(f"{institution_name} (item {item_id[:8]}…)")
        except PlaidCryptoError as exc:
            result.errors.append(
                f"entry[{index}] {institution_name}: encryption failed — {exc}"
            )
        except Exception as exc:
            # NEVER include entry contents here — tokens must not leak.
            result.errors.append(
                f"entry[{index}] {institution_name}: {type(exc).__name__}"
            )
            logger.exception("import failed for entry %d (%s)", index, institution_name)

    if apply:
        session.commit()
    else:
        session.rollback()
    return result


def _read_stdin(stream: IO[str]) -> list[Any]:
    """Parse the STDIN JSON payload; raises ValueError on a non-list."""
    payload = json.load(stream)
    if not isinstance(payload, list):
        raise ValueError("stdin payload must be a JSON list of item objects")
    return payload


def main(argv: list[str] | None = None, *, stdin: IO[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="Commit rows (default: dry-run)."
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    stream = stdin if stdin is not None else sys.stdin
    if stream is sys.stdin and sys.stdin.isatty():
        print(
            "Refusing to run interactively: pipe the JSON item list on STDIN "
            "(tokens must never be typed or read from a file).",
            file=sys.stderr,
        )
        return 2
    try:
        entries = _read_stdin(stream)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"Invalid stdin payload: {exc}", file=sys.stderr)
        return 2

    from src.db.connection import get_session, init_db  # late import keeps tests light

    init_db()
    session = get_session()
    try:
        result = import_items(session, entries, apply=args.apply)
    finally:
        session.close()

    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"[{mode}] imported={len(result.imported)} "
          f"skipped_existing={len(result.skipped_existing)} errors={len(result.errors)}")
    for line in result.imported:
        print(f"  + {line}")
    for line in result.skipped_existing:
        print(f"  = {line}")
    for line in result.errors:
        print(f"  ✗ {line}")
    if not args.apply:
        print("Rolled back. Re-run with --apply to commit.")
    return 1 if result.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
