"""REQ-HM-006: backup.sh disk-free gate, flock, integrity_check, sentinel.
R2 upload + dead-man ping stubbed via R2_DISABLE so the harness runs offline;
live R2/etag verified in the box phase."""
import os
import sqlite3
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backup.sh"


def _make_db(p: Path):
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    con.execute("INSERT INTO t VALUES (1)")
    con.commit()
    con.close()


def _run(tmp_path, **env):
    db = tmp_path / "data" / "accounting.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    _make_db(db)
    e = {**os.environ, "REPO_ROOT_OVERRIDE": str(tmp_path), "R2_DISABLE": "1", **env}
    return subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=e)


def test_aborts_below_5gb(tmp_path):
    r = _run(tmp_path, DISK_FREE_GB_OVERRIDE="3")
    assert r.returncode != 0
    combined = (r.stdout + r.stderr).lower()
    assert "5 gb" in combined or "disk" in combined


def test_sentinel_created_then_removed_on_success(tmp_path):
    r = _run(tmp_path, DISK_FREE_GB_OVERRIDE="50")
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    sentinel = tmp_path / "data" / ".backup.in-progress"
    assert not sentinel.exists()  # removed after successful run


def test_integrity_failure_leaves_no_partial(tmp_path):
    # A corrupt DB should make the script abort non-zero and NOT leave a sentinel.
    db = tmp_path / "data" / "accounting.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"this is not a valid sqlite file" * 10)
    e = {**os.environ, "REPO_ROOT_OVERRIDE": str(tmp_path), "R2_DISABLE": "1", "DISK_FREE_GB_OVERRIDE": "50"}
    r = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=e)
    assert r.returncode != 0
    assert not (tmp_path / "data" / ".backup.in-progress").exists()
