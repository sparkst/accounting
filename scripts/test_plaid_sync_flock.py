"""REQ-HM-009: during --apply the backup lock is held across the write."""
import fcntl
from unittest.mock import MagicMock

import scripts.plaid_transactions_sync as sync_mod


def test_apply_holds_lock_across_write(monkeypatch, tmp_path):
    lock = tmp_path / ".backup.lock"
    monkeypatch.setattr(sync_mod, "_backup_lock_path", lambda: lock)
    monkeypatch.setattr(sync_mod, "init_db", lambda: None)
    monkeypatch.setattr(sync_mod, "make_plaid_client", lambda: MagicMock())

    held = {"during_write": None}

    def fake_sync_all_active(session, client, dry_run):
        # While the write runs, a non-blocking lock attempt must FAIL.
        with open(lock, "w") as probe:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held["during_write"] = False
                fcntl.flock(probe, fcntl.LOCK_UN)
            except BlockingIOError:
                held["during_write"] = True
        b = MagicMock()
        b.items = []
        b.total_added = b.total_reactivated = b.total_modified = 0
        b.total_removed = b.total_failed = b.total_superseded = 0
        return b

    monkeypatch.setattr(sync_mod, "sync_all_active", fake_sync_all_active)
    monkeypatch.setattr(sync_mod, "SessionLocal", lambda: _CtxMock())

    sync_mod.main(["--apply"])
    assert held["during_write"] is True  # lock held during the write


def test_dry_run_does_not_hold_lock(monkeypatch, tmp_path):
    lock = tmp_path / ".backup.lock"
    monkeypatch.setattr(sync_mod, "_backup_lock_path", lambda: lock)
    monkeypatch.setattr(sync_mod, "init_db", lambda: None)
    monkeypatch.setattr(sync_mod, "make_plaid_client", lambda: MagicMock())

    held = {"during": None}

    def fake_sync_all_active(session, client, dry_run):
        with open(lock, "w") as probe:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held["during"] = False  # lock was free
                fcntl.flock(probe, fcntl.LOCK_UN)
            except BlockingIOError:
                held["during"] = True
        b = MagicMock()
        b.items = []
        b.total_added = b.total_reactivated = b.total_modified = 0
        b.total_removed = b.total_failed = b.total_superseded = 0
        return b

    monkeypatch.setattr(sync_mod, "sync_all_active", fake_sync_all_active)
    monkeypatch.setattr(sync_mod, "SessionLocal", lambda: _CtxMock())

    sync_mod.main([])  # dry-run (no --apply)
    assert held["during"] is False  # NOT locked during dry-run


class _CtxMock:
    """Minimal context manager standing in for SessionLocal()."""

    def __enter__(self):
        return MagicMock()

    def __exit__(self, *a):
        return False
