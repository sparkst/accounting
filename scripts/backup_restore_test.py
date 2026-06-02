"""Weekly R2 restore + integrity test (REQ-HM-006).

Downloads the latest daily R2 object (>= 30 min old), runs PRAGMA
integrity_check, and a row-count oracle: the object's actual table counts must
equal its OWN recorded R2 metadata (object-to-self, not the moving live DB) AND
be monotonically >= the previous successful backup. Alerts on mismatch; skips
silently if a backup is in progress (sentinel)."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_TABLES = ("transactions", "audit_events", "invoices")


def should_skip(data_dir: Path) -> bool:
    return (data_dir / ".backup.in-progress").exists()


def _count(db: Path, table: str) -> int:
    con = sqlite3.connect(db)
    try:
        return int(con.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
    finally:
        con.close()


def _integrity_ok(db: Path) -> bool:
    try:
        con = sqlite3.connect(db)
    except sqlite3.Error:
        return False
    try:
        row = con.execute("PRAGMA integrity_check").fetchone()
        return bool(row) and row[0] == "ok"
    except sqlite3.DatabaseError:
        return False
    finally:
        con.close()


def verify_object(db: Path, meta: dict[str, int], prior: dict[str, int] | None) -> bool:
    if not _integrity_ok(db):
        return False
    for table in _TABLES:
        try:
            actual = _count(db, table)
        except sqlite3.Error:
            return False
        recorded = int(meta.get(f"rows-{table}", -1))
        if actual != recorded:
            return False
        if prior is not None and f"rows-{table}" in prior and actual < int(prior[f"rows-{table}"]):
            return False  # non-monotonic shrink → suspicious
    return True


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parents[1]
    data_dir = repo_root / "data"
    if should_skip(data_dir):
        print("[restore-test] backup in progress — skipping (no alert)")
        return 0
    # Box-only (Phase 2): download newest daily object >= 30 min old + its R2
    # metadata + the prior backup's metadata, then call verify_object(), and
    # invoke `python -m scripts.alert` on failure. Not wired here so the timer
    # is not enabled before the R2 download is implemented + smoke-tested.
    raise SystemExit("box-only: implement R2 download wiring in Phase 2 before enabling the timer")


if __name__ == "__main__":
    sys.exit(main())
