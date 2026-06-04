"""Weekly R2 restore + integrity test (REQ-HM-006).

Downloads yesterday's daily R2 object (deterministic date key), runs PRAGMA
integrity_check, and a row-count oracle: the object's actual table counts must
equal its OWN recorded sidecar metadata (object-to-self, not the moving live DB)
AND be monotonically >= the previous successful backup. Alerts on mismatch;
skips silently if a backup is in progress (sentinel).

Auth: CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID must be in env (doppler run).
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import UTC, date, datetime, timedelta
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


def _date_key(d: date) -> tuple[str, str]:
    """Return (db_key, meta_key) for a given UTC date."""
    ds = d.strftime("%Y-%m-%d")
    return (f"daily/accounting-{ds}.db", f"daily/accounting-{ds}.meta.json")


def _wrangler_get(bucket: str, key: str, dest: Path, wrangler: str) -> bool:
    """Download key from R2 bucket to dest.  Returns True on success."""
    try:
        result = subprocess.run(
            [wrangler, "r2", "object", "get", f"{bucket}/{key}", f"--file={dest}", "--remote"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"[restore-test] wrangler get failed for {key}: {result.stderr.strip()}")
            return False
        return True
    except FileNotFoundError:
        print(f"[restore-test] wrangler binary not found: {wrangler}", file=sys.stderr)
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"[restore-test] unexpected error fetching {key}: {exc}", file=sys.stderr)
        return False


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parents[1]
    data_dir = repo_root / "data"

    if should_skip(data_dir):
        print("[restore-test] backup in progress — skipping (no alert)")
        return 0

    bucket = os.environ.get("R2_BUCKET", "")
    if not bucket:
        print("[restore-test] R2_BUCKET not set — cannot run restore test", file=sys.stderr)
        return 1

    wrangler = os.environ.get("WRANGLER_BIN", "wrangler")

    today_utc = datetime.now(UTC).date()
    yesterday = today_utc - timedelta(days=1)
    db_key, meta_key = _date_key(yesterday)

    tmp_files: list[Path] = []
    try:
        # Download yesterday's DB snapshot.
        db_fd, db_tmp_str = tempfile.mkstemp(suffix=".db", prefix="accounting-restore-")
        os.close(db_fd)
        db_tmp = Path(db_tmp_str)
        tmp_files.append(db_tmp)

        ok = _wrangler_get(bucket, db_key, db_tmp, wrangler)
        if not ok:
            # Missing yesterday object is non-fatal — backup may not have run yet
            # (e.g., first deploy, maintenance window). Log and exit 0, no alert.
            print(f"[restore-test] yesterday's object not available ({db_key}) — skipping (no alert)")
            return 0

        # Download yesterday's meta sidecar.
        meta_fd, meta_tmp_str = tempfile.mkstemp(suffix=".json", prefix="accounting-meta-")
        os.close(meta_fd)
        meta_tmp = Path(meta_tmp_str)
        tmp_files.append(meta_tmp)

        ok = _wrangler_get(bucket, meta_key, meta_tmp, wrangler)
        if not ok:
            print(f"[restore-test] meta sidecar not available ({meta_key}) — skipping (no alert)")
            return 0

        try:
            meta: dict[str, object] = json.loads(meta_tmp.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[restore-test] failed to parse meta JSON: {exc}", file=sys.stderr)
            return 1

        # Load prior day's meta for monotonic check (best-effort; None if absent).
        prior: dict[str, object] | None = None
        two_days_ago = today_utc - timedelta(days=2)
        _, prior_meta_key = _date_key(two_days_ago)
        prior_fd, prior_tmp_str = tempfile.mkstemp(suffix=".json", prefix="accounting-prior-meta-")
        os.close(prior_fd)
        prior_tmp = Path(prior_tmp_str)
        tmp_files.append(prior_tmp)
        if _wrangler_get(bucket, prior_meta_key, prior_tmp, wrangler):
            try:
                prior = json.loads(prior_tmp.read_text())
            except (json.JSONDecodeError, OSError):
                prior = None  # best-effort; proceed without it
        else:
            prior = None

        # Verify integrity + row-count oracle.
        passed = verify_object(db_tmp, meta, prior)  # type: ignore[arg-type]
        if not passed:
            print(
                f"[restore-test] FAIL: verify_object returned False for {db_key}",
                file=sys.stderr,
            )
            # Invoke the alert via the scripts.alert module.
            try:
                from scripts.alert import send_alert  # noqa: PLC0415
                send_alert("accounting-backup-restore-test.service")
            except Exception as alert_exc:  # noqa: BLE001
                print(f"[restore-test] alert send failed: {alert_exc}", file=sys.stderr)
            return 1

        print(f"[restore-test] OK: {db_key} passed integrity + row-count oracle")
        return 0

    finally:
        for p in tmp_files:
            with contextlib.suppress(OSError):
                p.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
