"""Tests for scripts/quark_register_digest.py — the box-side Quark digest.

Focus: the plaid_reauth counter must count only connections a human actually
needs to fix, not superseded/abandoned zombie rows (2026-08-17 finding: it
reported 3 with zero real re-auths outstanding).
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

DIGEST = Path(__file__).with_name("quark_register_digest.py")


def _make_db(tmp_path: Path) -> Path:
    """Minimal schema covering every table the digest queries."""
    db = tmp_path / "digest.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE transactions (
            id INTEGER PRIMARY KEY, status TEXT, direction TEXT,
            amount REAL, date TEXT, entity TEXT
        );
        CREATE TABLE invoices (id INTEGER PRIMARY KEY, status TEXT, total REAL);
        CREATE TABLE plaid_item (
            id TEXT PRIMARY KEY, institution_id TEXT, institution_name TEXT,
            status TEXT, last_sync_status TEXT, scope TEXT, created_at TEXT
        );
        CREATE TABLE account (id TEXT PRIMARY KEY, plaid_item_id TEXT);
        """
    )
    con.commit()
    con.close()
    return db


def _run_digest(db: Path) -> dict:
    out = subprocess.run(
        [sys.executable, str(DIGEST), str(db)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


def _add_item(db: Path, *, id: str, inst: str, status: str,
              sync: str | None = "ok", scope: str = "register",
              created: str = "2026-06-07 04:00:00") -> None:
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO plaid_item VALUES (?,?,?,?,?,?,?)",
        (id, inst, inst, status, sync, scope, created),
    )
    con.commit()
    con.close()


def _map_account(db: Path, *, account_id: str, item_id: str) -> None:
    con = sqlite3.connect(db)
    con.execute("INSERT INTO account VALUES (?,?)", (account_id, item_id))
    con.commit()
    con.close()


def test_all_active_ok_counts_zero(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    _add_item(db, id="a1", inst="ins_chase", status="active")
    assert _run_digest(db)["plaid_reauth"] == 0


def test_abandoned_link_attempt_is_not_a_reauth(tmp_path: Path) -> None:
    """An abandoned row is a never-completed Link attempt, not a broken feed."""
    db = _make_db(tmp_path)
    _add_item(db, id="a1", inst="ins_chase", status="active")
    _add_item(db, id="a2", inst="ins_chase", status="abandoned")
    assert _run_digest(db)["plaid_reauth"] == 0


def test_superseded_disconnected_row_is_not_a_reauth(tmp_path: Path) -> None:
    """The 2026-08-17 Chase zombie: disconnected, zero mapped accounts, while
    the institution has other active items. Nothing for a human to fix."""
    db = _make_db(tmp_path)
    _add_item(db, id="reg", inst="ins_chase", status="active",
              created="2026-06-07 04:33:05")
    _add_item(db, id="dead", inst="ins_chase", status="disconnected",
              created="2026-06-07 05:25:50")
    _add_item(db, id="wlth", inst="ins_chase", status="active", scope="wealth",
              created="2026-07-26 03:36:27")
    _map_account(db, account_id="acc1", item_id="reg")
    assert _run_digest(db)["plaid_reauth"] == 0


def test_disconnected_with_mapped_accounts_counts(tmp_path: Path) -> None:
    """A disconnected item that still owns register accounts is a real outage."""
    db = _make_db(tmp_path)
    _add_item(db, id="d1", inst="ins_amex", status="disconnected")
    _map_account(db, account_id="acc1", item_id="d1")
    assert _run_digest(db)["plaid_reauth"] == 1


def test_disconnected_sole_connection_counts(tmp_path: Path) -> None:
    """Institution's only item is disconnected (e.g. a wealth-scope feed with
    no account rows): the institution is dark, so it counts."""
    db = _make_db(tmp_path)
    _add_item(db, id="w1", inst="ins_citi", status="disconnected", scope="wealth")
    assert _run_digest(db)["plaid_reauth"] == 1


def test_active_item_with_sync_error_counts(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    _add_item(db, id="e1", inst="ins_boa", status="active", sync="error")
    assert _run_digest(db)["plaid_reauth"] == 1


def test_missing_plaid_table_reports_null(tmp_path: Path) -> None:
    db = tmp_path / "bare.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE transactions (
            id INTEGER PRIMARY KEY, status TEXT, direction TEXT,
            amount REAL, date TEXT, entity TEXT
        );
        CREATE TABLE invoices (id INTEGER PRIMARY KEY, status TEXT, total REAL);
        """
    )
    con.commit()
    con.close()
    assert _run_digest(db)["plaid_reauth"] is None
