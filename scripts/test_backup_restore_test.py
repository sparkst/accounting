"""REQ-HM-006: restore-test oracle — object-vs-own-metadata + monotonic + sentinel skip."""
import sqlite3
from datetime import date

from scripts import backup_restore_test as brt


def _db_with_counts(p, tx, ae, inv):
    con = sqlite3.connect(p)
    for name, n in (("transactions", tx), ("audit_events", ae), ("invoices", inv)):
        con.execute(f"CREATE TABLE {name} (id INTEGER PRIMARY KEY)")
        con.executemany(f"INSERT INTO {name} (id) VALUES (?)", [(i,) for i in range(n)])
    con.commit()
    con.close()


def test_passes_when_counts_match_metadata(tmp_path):
    p = tmp_path / "snap.db"
    _db_with_counts(p, 10, 20, 3)
    meta = {"rows-transactions": 10, "rows-audit_events": 20, "rows-invoices": 3}
    assert brt.verify_object(p, meta, prior={"rows-transactions": 9}) is True


def test_fails_when_counts_mismatch_metadata(tmp_path):
    p = tmp_path / "snap.db"
    _db_with_counts(p, 10, 20, 3)
    meta = {"rows-transactions": 999, "rows-audit_events": 20, "rows-invoices": 3}
    assert brt.verify_object(p, meta, prior=None) is False


def test_fails_on_non_monotonic(tmp_path):
    p = tmp_path / "snap.db"
    _db_with_counts(p, 5, 20, 3)
    meta = {"rows-transactions": 5, "rows-audit_events": 20, "rows-invoices": 3}
    assert brt.verify_object(p, meta, prior={"rows-transactions": 10}) is False


def test_sentinel_causes_skip(tmp_path):
    (tmp_path / ".backup.in-progress").touch()
    assert brt.should_skip(tmp_path) is True


def test_no_sentinel_no_skip(tmp_path):
    assert brt.should_skip(tmp_path) is False


def test_integrity_failure_fails_verify(tmp_path):
    p = tmp_path / "bad.db"
    p.write_bytes(b"not a sqlite file" * 8)
    assert brt.verify_object(p, {"rows-transactions": 0, "rows-audit_events": 0, "rows-invoices": 0}, prior=None) is False


# ── _date_key unit tests ──────────────────────────────────────────────────────

def test_date_key_format():
    db_key, meta_key = brt._date_key(date(2026, 6, 4))
    assert db_key == "daily/accounting-2026-06-04.db"
    assert meta_key == "daily/accounting-2026-06-04.meta.json"


def test_date_key_zero_padded():
    db_key, meta_key = brt._date_key(date(2026, 1, 5))
    assert db_key == "daily/accounting-2026-01-05.db"
    assert meta_key == "daily/accounting-2026-01-05.meta.json"


def test_date_key_year_boundary():
    db_key, meta_key = brt._date_key(date(2026, 12, 31))
    assert db_key == "daily/accounting-2026-12-31.db"
    assert meta_key == "daily/accounting-2026-12-31.meta.json"
