"""Tests for scripts/plaid_investments_sync.py — REQ-PC-B3 exit-code policy.

'ok' and 'skipped_invalid_product' are clean; anything else (Plaid error,
failed D1 push) exits non-zero so the systemd OnFailure alert fires.
"""

import unittest.mock as mock

from scripts import plaid_investments_sync as cli


def _item(status: str = "ok") -> mock.Mock:
    return mock.Mock(
        status=status, institution_name="ETRADE", securities=1, holdings=2,
        pushed=(status == "ok"), error_code=None if status == "ok" else "X",
    )


def _batch(items: list[mock.Mock], failed: int) -> mock.Mock:
    return mock.Mock(
        items=items, total_holdings=sum(i.holdings for i in items),
        total_failed_items=failed, dry_run=False,
    )


def test_main_dry_run_default() -> None:
    with mock.patch.object(cli, "sync_all_wealth") as sync, \
         mock.patch.object(cli, "make_plaid_client", return_value=mock.Mock()), \
         mock.patch.object(cli, "SessionLocal", return_value=mock.MagicMock()):
        sync.return_value = _batch([], 0)
        cli.main([])
        _, kwargs = sync.call_args
        assert kwargs["dry_run"] is True


def test_main_apply_flag() -> None:
    with mock.patch.object(cli, "sync_all_wealth") as sync, \
         mock.patch.object(cli, "make_plaid_client", return_value=mock.Mock()), \
         mock.patch.object(cli, "SessionLocal", return_value=mock.MagicMock()):
        sync.return_value = _batch([], 0)
        cli.main(["--apply"])
        _, kwargs = sync.call_args
        assert kwargs["dry_run"] is False


def test_exit_zero_on_ok_and_invalid_product_skip() -> None:
    with mock.patch.object(cli, "sync_all_wealth") as sync, \
         mock.patch.object(cli, "make_plaid_client", return_value=mock.Mock()), \
         mock.patch.object(cli, "SessionLocal", return_value=mock.MagicMock()):
        sync.return_value = _batch(
            [_item("ok"), _item("skipped_invalid_product")], failed=0
        )
        assert cli.main(["--apply"]) == 0


def test_exit_nonzero_on_error_item() -> None:
    with mock.patch.object(cli, "sync_all_wealth") as sync, \
         mock.patch.object(cli, "make_plaid_client", return_value=mock.Mock()), \
         mock.patch.object(cli, "SessionLocal", return_value=mock.MagicMock()):
        sync.return_value = _batch([_item("ok"), _item("error")], failed=1)
        assert cli.main(["--apply"]) == 1
