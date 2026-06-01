"""Tests for scripts/plaid_transactions_sync.py (REQ-PT-014)."""

import unittest.mock as mock

from scripts import plaid_transactions_sync as cli


def test_main_dry_run_default_does_not_apply():
    with mock.patch.object(cli, "sync_all_active") as sync, \
         mock.patch.object(cli, "make_plaid_client", return_value=mock.Mock()), \
         mock.patch.object(cli, "SessionLocal", return_value=mock.MagicMock()):
        sync.return_value = mock.Mock(items=[], total_added=0, dry_run=True)
        cli.main([])
        _, kwargs = sync.call_args
        assert kwargs["dry_run"] is True


def test_main_apply_flag_writes():
    with mock.patch.object(cli, "sync_all_active") as sync, \
         mock.patch.object(cli, "make_plaid_client", return_value=mock.Mock()), \
         mock.patch.object(cli, "SessionLocal", return_value=mock.MagicMock()):
        sync.return_value = mock.Mock(items=[], total_added=0, dry_run=False)
        cli.main(["--apply"])
        _, kwargs = sync.call_args
        assert kwargs["dry_run"] is False
