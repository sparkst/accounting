"""Tests for scripts/plaid_transactions_sync.py (REQ-PT-014)."""

import unittest.mock as mock

from scripts import plaid_transactions_sync as cli


def test_main_dry_run_default_does_not_apply():
    with mock.patch.object(cli, "sync_all_active") as sync, \
         mock.patch.object(cli, "make_plaid_client", return_value=mock.Mock()), \
         mock.patch.object(cli, "SessionLocal", return_value=mock.MagicMock()):
        sync.return_value = mock.Mock(
            items=[], total_added=0, total_reactivated=0, total_modified=0,
            total_removed=0, total_failed=0, total_superseded=0, dry_run=True,
        )
        cli.main([])
        _, kwargs = sync.call_args
        assert kwargs["dry_run"] is True


def test_main_apply_flag_writes():
    with mock.patch.object(cli, "sync_all_active") as sync, \
         mock.patch.object(cli, "make_plaid_client", return_value=mock.Mock()), \
         mock.patch.object(cli, "SessionLocal", return_value=mock.MagicMock()):
        sync.return_value = mock.Mock(
            items=[], total_added=0, total_reactivated=0, total_modified=0,
            total_removed=0, total_failed=0, total_superseded=0, dry_run=False,
        )
        cli.main(["--apply"])
        _, kwargs = sync.call_args
        assert kwargs["dry_run"] is False


def test_main_returns_zero_on_clean_sync():
    """No failures and no error-status items → exit code 0."""
    with mock.patch.object(cli, "sync_all_active") as sync, \
         mock.patch.object(cli, "make_plaid_client", return_value=mock.Mock()), \
         mock.patch.object(cli, "SessionLocal", return_value=mock.MagicMock()):
        ok_item = mock.Mock(status="ok", institution_name="Chase", added=1,
                            reactivated=0, failed=0, error_code=None)
        sync.return_value = mock.Mock(
            items=[ok_item], total_added=1, total_reactivated=0, total_modified=0,
            total_removed=0, total_failed=0, total_superseded=0, dry_run=False,
        )
        assert cli.main([]) == 0


def test_main_returns_nonzero_when_sync_reports_failures():
    """REQ-PT-007: total_failed > 0 (held cursor) → exit code 1 so launchd alerts."""
    with mock.patch.object(cli, "sync_all_active") as sync, \
         mock.patch.object(cli, "make_plaid_client", return_value=mock.Mock()), \
         mock.patch.object(cli, "SessionLocal", return_value=mock.MagicMock()):
        bad_item = mock.Mock(status="error", institution_name="Chase", added=0,
                             reactivated=0, failed=1, error_code=None)
        sync.return_value = mock.Mock(
            items=[bad_item], total_added=0, total_reactivated=0, total_modified=0,
            total_removed=0, total_failed=1, total_superseded=0, dry_run=False,
        )
        assert cli.main([]) == 1


def test_main_returns_nonzero_on_error_status_item():
    """A terminal/retryable item error (failed counter 0) still exits non-zero."""
    with mock.patch.object(cli, "sync_all_active") as sync, \
         mock.patch.object(cli, "make_plaid_client", return_value=mock.Mock()), \
         mock.patch.object(cli, "SessionLocal", return_value=mock.MagicMock()):
        err_item = mock.Mock(status="error", institution_name="Chase", added=0,
                             reactivated=0, failed=0, error_code="ITEM_LOGIN_REQUIRED")
        sync.return_value = mock.Mock(
            items=[err_item], total_added=0, total_reactivated=0, total_modified=0,
            total_removed=0, total_failed=0, total_superseded=0, dry_run=False,
        )
        assert cli.main([]) == 1


def test_main_returns_nonzero_on_institution_down_item():
    """P1-001 / REQ-PT-007: a retryable INSTITUTION_DOWN sets item status
    'institution_down' (NOT 'error') and holds the cursor with failed==0. The
    script must still exit non-zero so launchd surfaces the held-cursor sync —
    a status=='error'-only check would exit 0 and leave ops blind."""
    with mock.patch.object(cli, "sync_all_active") as sync, \
         mock.patch.object(cli, "make_plaid_client", return_value=mock.Mock()), \
         mock.patch.object(cli, "SessionLocal", return_value=mock.MagicMock()):
        down_item = mock.Mock(status="institution_down", institution_name="Chase",
                              added=0, reactivated=0, failed=0,
                              error_code="INSTITUTION_DOWN")
        sync.return_value = mock.Mock(
            items=[down_item], total_added=0, total_reactivated=0, total_modified=0,
            total_removed=0, total_failed=0, total_superseded=0, dry_run=False,
        )
        assert cli.main([]) == 1
