#!/usr/bin/env python
"""Cleanup stale / malformed PositionSnapshot rows.

Targets three classes of garbage rows that earlier (pre-fix) ingest runs left
in the live database:

1. ``symbol = 'TOTAL'`` (case-insensitive) — summary footer rows that some
   broker exports include alongside real positions.
2. ``symbol LIKE 'Generated %'`` — E*TRADE PortfolioDownload.csv footer
   rows like ``Generated at May 4 2026 02:47 PM ET``.
3. Duplicate groups by ``(account_id, COALESCE(symbol, description), as_of)``
   — keep ``MIN(id)``, delete the rest.

Default mode is DRY-RUN (logs what *would* be deleted). Pass ``--apply`` to
actually delete. The script always prints a summary with row counts and the
distinct ``source_file`` values touched, so the operator can audit before
running with ``--apply``.

Usage:
    python scripts/cleanup-stale-position-snapshots.py            # dry-run
    python scripts/cleanup-stale-position-snapshots.py --apply    # delete

TASK-13 (proposals/brokerage-visibility/PLAN-option1.md).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.db.connection import SessionLocal  # noqa: E402
from src.models.brokerage import PositionSnapshot  # noqa: E402
from src.models.ingestion_log import IngestionLog  # noqa: E402


def _select_total_rows(session: Session) -> list[PositionSnapshot]:
    """Rows with symbol = 'TOTAL' (case-insensitive)."""
    stmt = select(PositionSnapshot).where(
        func.upper(func.coalesce(PositionSnapshot.symbol, "")) == "TOTAL"
    )
    return list(session.execute(stmt).scalars())


def _select_generated_rows(session: Session) -> list[PositionSnapshot]:
    """Rows with symbol LIKE 'Generated %'."""
    stmt = select(PositionSnapshot).where(
        PositionSnapshot.symbol.like("Generated %")
    )
    return list(session.execute(stmt).scalars())


def _select_duplicate_extras(session: Session) -> list[PositionSnapshot]:
    """Duplicate snapshot rows: per ``(account_id, COALESCE(symbol, description),
    as_of)`` group, keep the row with the smallest ``id`` and return all others.
    """
    all_snaps = list(session.execute(select(PositionSnapshot)).scalars())
    grouped: dict[tuple[str, str, object], list[PositionSnapshot]] = {}
    for snap in all_snaps:
        key_text = (snap.symbol or snap.description or "").strip()
        key = (snap.account_id, key_text, snap.as_of)
        grouped.setdefault(key, []).append(snap)

    extras: list[PositionSnapshot] = []
    for rows in grouped.values():
        if len(rows) < 2:
            continue
        rows_sorted = sorted(rows, key=lambda s: s.id)
        # Keep [0], delete [1:]
        extras.extend(rows_sorted[1:])
    return extras


def _summarize(label: str, rows: Iterable[PositionSnapshot]) -> int:
    """Print a per-target summary; return the row count."""
    rows = list(rows)
    if not rows:
        print(f"  {label}: 0 rows")
        return 0
    files: Counter[str] = Counter(r.source_file or "" for r in rows)
    print(f"  {label}: {len(rows)} rows")
    for fname, count in files.most_common():
        print(f"    - {fname or '(no source_file)'}: {count}")
    return len(rows)


def cleanup(session: Session, *, apply: bool) -> tuple[int, int, int]:
    """Run cleanup. Returns ``(total_rows, deleted_rows, skipped_dupes)``.

    On ``apply=False`` (dry-run), nothing is written; on ``apply=True``, all
    matching rows are deleted in a single transaction (commit on success,
    rollback on any error).

    After a successful apply, an IngestionLog row is written with:
      source='cleanup-position-snapshots', status='success',
      records_processed=total_deleted, error_detail=JSON audit summary.
    """
    total_rows = (
        session.execute(select(func.count(PositionSnapshot.id))).scalar() or 0
    )

    print(f"Scanning {total_rows} PositionSnapshot rows...")
    print()
    print("Targets:")

    # Collect candidate IDs into a set during the display phase so that
    # overlapping rows (a row that is both TOTAL and a duplicate extra) are
    # counted only once. This also eliminates the second full-table scan in
    # apply mode (TOCTOU fix).
    total_rows_list = _select_total_rows(session)
    generated_rows_list = _select_generated_rows(session)
    dup_rows_list = _select_duplicate_extras(session)

    total_count = _summarize("symbol = 'TOTAL' (case-insensitive)", total_rows_list)
    generated_count = _summarize("symbol LIKE 'Generated %'", generated_rows_list)
    dup_count = _summarize("duplicate (account_id, symbol|description, as_of)", dup_rows_list)

    # Union IDs so overlapping rows are counted only once.
    combined_ids: set[str] = set()
    for r in total_rows_list:
        combined_ids.add(r.id)
    for r in generated_rows_list:
        combined_ids.add(r.id)
    for r in dup_rows_list:
        combined_ids.add(r.id)
    to_delete_count = len(combined_ids)

    print()
    print(f"Total candidate rows: {to_delete_count}")

    if to_delete_count == 0:
        print("Nothing to do.")
        return (total_rows, 0, 0)

    if not apply:
        print()
        print("DRY-RUN: no changes applied. Re-run with --apply to delete.")
        return (total_rows, 0, to_delete_count)

    # Apply: delete by the pre-collected ID set (no second DB scan).
    by_source_file: Counter[str] = Counter()
    for r in [
        *total_rows_list,
        *generated_rows_list,
        *dup_rows_list,
    ]:
        if r.id in combined_ids:
            by_source_file[r.source_file or ""] += 1

    deleted = 0
    try:
        for snap_id in combined_ids:
            obj = session.get(PositionSnapshot, snap_id)
            if obj is not None:
                session.delete(obj)
                deleted += 1

        # Write audit IngestionLog before final commit.
        audit_detail = json.dumps({
            "deleted_ids": sorted(combined_ids),
            "by_source_file": dict(by_source_file),
            "by_category": {
                "TOTAL": total_count,
                "Generated": generated_count,
                "duplicates": dup_count,
                "TOTAL_DELETED": deleted,
            },
        })
        log = IngestionLog(
            source="cleanup-position-snapshots",
            run_at=datetime.now(UTC).replace(tzinfo=None),
            status="success",
            records_processed=deleted,
            records_failed=0,
            error_detail=audit_detail,
            retryable=False,
        )
        session.add(log)
        session.commit()
    except Exception:
        session.rollback()
        raise

    print()
    print(f"Deleted {deleted} row(s).")
    return (total_rows, deleted, 0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Cleanup stale / malformed PositionSnapshot rows. "
        "Dry-run by default; pass --apply to delete."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete matched rows (default is dry-run).",
    )
    args = parser.parse_args(argv)

    session = SessionLocal()
    try:
        cleanup(session, apply=args.apply)
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
