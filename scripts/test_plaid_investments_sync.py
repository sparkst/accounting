"""Tests for scripts/plaid_investments_sync.py — REQ-PC-B3 exit-code policy.

'ok' and 'skipped_invalid_product' are clean; re-auth-class Item errors route
to the sev3 webhook and exit 0 (REQ-FIX-ALR-009); anything else (infra Plaid
error, failed D1 push) exits non-zero so the systemd OnFailure alert fires.
"""

import unittest.mock as mock
from pathlib import Path

import pytest

from scripts import plaid_investments_sync as cli
from src.alerts.webhook import WebhookResult


@pytest.fixture(autouse=True)
def _sentinel_tmpdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALERT_SENTINEL_DIR", str(tmp_path))


def _item(status: str = "ok", error_code: str | None = None) -> mock.Mock:
    return mock.Mock(
        status=status, institution_name="ETRADE", item_id=f"item-{status}",
        securities=1, holdings=2,
        pushed=(status == "ok"),
        error_code=error_code if status != "ok" else None,
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
        sync.return_value = _batch(
            [_item("ok"), _item("error", error_code="D1_PUSH:WealthHTTPError")],
            failed=1,
        )
        assert cli.main(["--apply"]) == 1


def test_exit_zero_on_reauth_item_and_routes_sev3() -> None:
    """REQ-FIX-ALR-009: ITEM_LOGIN_REQUIRED exits 0 and routes to the
    once-per-state sev3 webhook alert instead of tripping OnFailure daily."""
    captured = {}
    real_route = cli.route_batch

    def _spy(items, **kwargs):
        captured["routing"] = real_route(items, **kwargs)
        return captured["routing"]

    with mock.patch.object(cli, "sync_all_wealth") as sync, \
         mock.patch.object(cli, "route_batch", side_effect=_spy), \
         mock.patch(
             "src.alerts.plaid_reauth.post_payload",
             return_value=WebhookResult("sent", 200, None),
         ), \
         mock.patch.object(cli, "make_plaid_client", return_value=mock.Mock()), \
         mock.patch.object(cli, "SessionLocal", return_value=mock.MagicMock()):
        sync.return_value = _batch(
            [_item("ok"), _item("error", error_code="ITEM_LOGIN_REQUIRED")],
            failed=1,
        )
        assert cli.main(["--apply"]) == 0
    routing = captured["routing"]
    assert [f.error_code for f in routing.reauth] == ["ITEM_LOGIN_REQUIRED"]
    assert routing.infra == []
