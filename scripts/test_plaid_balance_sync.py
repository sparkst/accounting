"""Tests for scripts/plaid_balance_sync.py (REQ-FIX-PLD-002).

Exit-code policy mirrors plaid_transactions_sync.py: any accounts_failed>0 OR
any Item not in a clean 'ok' state is a failure. The old policy (only
terminal, non-retryable item errors) silently exited 0 on a retryable
INSTITUTION_DOWN or a partial per-account failure — hiding both from the
OnFailure alert.
"""

import unittest.mock as mock
from pathlib import Path

import pytest

from scripts import plaid_balance_sync as cli


@pytest.fixture(autouse=True)
def _sentinel_tmpdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALERT_SENTINEL_DIR", str(tmp_path))


def test_main_dry_run_default_does_not_apply() -> None:
    with mock.patch.object(cli, "sync_all_active") as sync, \
         mock.patch.object(cli, "make_plaid_client", return_value=mock.Mock()), \
         mock.patch.object(cli, "SessionLocal", return_value=mock.MagicMock()):
        sync.return_value = mock.Mock(items=[], total_processed=0, total_failed=0, dry_run=True)
        cli.main([])
        _, kwargs = sync.call_args
        assert kwargs["dry_run"] is True


def test_main_apply_flag_writes() -> None:
    with mock.patch.object(cli, "sync_all_active") as sync, \
         mock.patch.object(cli, "make_plaid_client", return_value=mock.Mock()), \
         mock.patch.object(cli, "SessionLocal", return_value=mock.MagicMock()):
        sync.return_value = mock.Mock(items=[], total_processed=0, total_failed=0, dry_run=False)
        cli.main(["--apply"])
        _, kwargs = sync.call_args
        assert kwargs["dry_run"] is False


def test_main_returns_zero_on_clean_sync() -> None:
    with mock.patch.object(cli, "sync_all_active") as sync, \
         mock.patch.object(cli, "make_plaid_client", return_value=mock.Mock()), \
         mock.patch.object(cli, "SessionLocal", return_value=mock.MagicMock()):
        ok_item = mock.Mock(
            status="ok", institution_name="Chase", accounts_processed=1,
            accounts_failed=0, accounts_skipped_unmapped=0, accounts_skipped_non_usd=0,
            error_code=None, scope="register", fresh_balances=[],
        )
        sync.return_value = mock.Mock(items=[ok_item], total_processed=1, total_failed=0, dry_run=False)
        assert cli.main([]) == 0


def test_main_returns_zero_on_idempotent_double_run() -> None:
    """IntegrityError collisions count as accounts_processed, not accounts_failed —
    a second same-day run stays exit-0."""
    with mock.patch.object(cli, "sync_all_active") as sync, \
         mock.patch.object(cli, "make_plaid_client", return_value=mock.Mock()), \
         mock.patch.object(cli, "SessionLocal", return_value=mock.MagicMock()):
        ok_item = mock.Mock(
            status="ok", institution_name="Chase", accounts_processed=3,
            accounts_failed=0, accounts_skipped_unmapped=0, accounts_skipped_non_usd=0,
            error_code=None, scope="register", fresh_balances=[],
        )
        sync.return_value = mock.Mock(items=[ok_item], total_processed=3, total_failed=0, dry_run=False)
        assert cli.main(["--apply"]) == 0


def test_main_returns_nonzero_on_accounts_failed() -> None:
    """REQ-FIX-PLD-002: accounts_failed > 0 must exit non-zero — the old
    policy silently swallowed this (only terminal item.status=='error' AND
    not retryable tripped a failure)."""
    with mock.patch.object(cli, "sync_all_active") as sync, \
         mock.patch.object(cli, "make_plaid_client", return_value=mock.Mock()), \
         mock.patch.object(cli, "SessionLocal", return_value=mock.MagicMock()):
        partial_item = mock.Mock(
            status="ok", institution_name="Chase", accounts_processed=2,
            accounts_failed=1, accounts_skipped_unmapped=0, accounts_skipped_non_usd=0,
            error_code=None, scope="register", fresh_balances=[],
        )
        sync.return_value = mock.Mock(items=[partial_item], total_processed=2, total_failed=1, dry_run=False)
        assert cli.main([]) == 1


def test_main_returns_nonzero_on_retryable_institution_down() -> None:
    """REQ-FIX-PLD-002: a retryable INSTITUTION_DOWN item (status !=
    'ok', accounts_failed==0) must exit non-zero — this is the exact
    silent-exit-0 bug the fix closes."""
    with mock.patch.object(cli, "sync_all_active") as sync, \
         mock.patch.object(cli, "make_plaid_client", return_value=mock.Mock()), \
         mock.patch.object(cli, "SessionLocal", return_value=mock.MagicMock()):
        down_item = mock.Mock(
            status="institution_down", institution_name="Chase", accounts_processed=0,
            accounts_failed=0, accounts_skipped_unmapped=0, accounts_skipped_non_usd=0,
            error_code="INSTITUTION_DOWN", retryable=True, scope="register",
            fresh_balances=[],
        )
        sync.return_value = mock.Mock(items=[down_item], total_processed=0, total_failed=0, dry_run=False)
        assert cli.main([]) == 1


def test_main_returns_zero_on_reauth_item_and_routes_sev3() -> None:
    """REQ-FIX-ALR-009: ITEM_LOGIN_REQUIRED is a human re-link, not an infra
    failure — exit 0; the failure routes to the once-per-state sev3 webhook
    alert (with the re-connect link) instead of tripping OnFailure daily."""
    captured = {}
    real_route = cli.route_batch

    def _spy(items, **kwargs):
        captured["routing"] = real_route(items, **kwargs)
        return captured["routing"]

    with mock.patch.object(cli, "sync_all_active") as sync, \
         mock.patch.object(cli, "route_batch", side_effect=_spy), \
         mock.patch.object(cli, "make_plaid_client", return_value=mock.Mock()), \
         mock.patch.object(cli, "SessionLocal", return_value=mock.MagicMock()):
        err_item = mock.Mock(
            status="error", institution_name="PenFed Credit Union", item_id="item-penfed",
            accounts_processed=0,
            accounts_failed=0, accounts_skipped_unmapped=0, accounts_skipped_non_usd=0,
            error_code="ITEM_LOGIN_REQUIRED", retryable=False, scope="register",
            fresh_balances=[],
        )
        sync.return_value = mock.Mock(items=[err_item], total_processed=0, total_failed=0, dry_run=False)
        assert cli.main([]) == 0
    routing = captured["routing"]
    assert [f.error_code for f in routing.reauth] == ["ITEM_LOGIN_REQUIRED"]
    assert routing.infra == []


def test_main_returns_nonzero_on_non_reauth_terminal_error_item() -> None:
    with mock.patch.object(cli, "sync_all_active") as sync, \
         mock.patch.object(cli, "make_plaid_client", return_value=mock.Mock()), \
         mock.patch.object(cli, "SessionLocal", return_value=mock.MagicMock()):
        err_item = mock.Mock(
            status="error", institution_name="Chase", item_id="item-chase",
            accounts_processed=0,
            accounts_failed=0, accounts_skipped_unmapped=0, accounts_skipped_non_usd=0,
            error_code="UNEXPECTED", retryable=False, scope="register",
            fresh_balances=[],
        )
        sync.return_value = mock.Mock(items=[err_item], total_processed=0, total_failed=0, dry_run=False)
        assert cli.main([]) == 1


# ── REQ-PC-B2: D1 push wiring ────────────────────────────────────────────────


def _ok_item(fresh: list | None = None, *, scope: str = "wealth") -> mock.Mock:
    """P0-r3a: only wealth-scope Items carry fresh_balances, so the push-wiring
    tests below default to that scope."""
    return mock.Mock(
        status="ok", institution_name="Schwab", accounts_processed=1,
        accounts_failed=0, accounts_skipped_unmapped=0, accounts_skipped_non_usd=0,
        error_code=None, scope=scope, fresh_balances=fresh or [],
    )


def test_dry_run_never_pushes_to_wealth() -> None:
    with mock.patch.object(cli, "sync_all_active") as sync, \
         mock.patch.object(cli, "push_fresh_balances") as push, \
         mock.patch.object(cli, "make_plaid_client", return_value=mock.Mock()), \
         mock.patch.object(cli, "SessionLocal", return_value=mock.MagicMock()):
        sync.return_value = mock.Mock(
            items=[_ok_item([{"plaid_account_id": "a"}])],
            total_processed=1, total_failed=0, dry_run=True,
        )
        assert cli.main([]) == 0
        push.assert_not_called()


def test_apply_pushes_after_sync() -> None:
    with mock.patch.object(cli, "sync_all_active") as sync, \
         mock.patch.object(cli, "push_fresh_balances") as push, \
         mock.patch.object(cli, "make_plaid_client", return_value=mock.Mock()), \
         mock.patch.object(cli, "SessionLocal", return_value=mock.MagicMock()):
        batch = mock.Mock(
            items=[_ok_item([{"plaid_account_id": "a"}])],
            total_processed=1, total_failed=0, dry_run=False,
        )
        sync.return_value = batch
        push.return_value = mock.Mock(total_pushed=1, failed=False, items=[])
        assert cli.main(["--apply"]) == 0
        push.assert_called_once()
        assert push.call_args[0][0] is batch


def test_apply_exits_nonzero_on_push_failure() -> None:
    """REQ-PC-B2: a failed D1 push must exit non-zero even when the Plaid sync
    itself was clean — the OnFailure alert IS the balance-staleness alert."""
    with mock.patch.object(cli, "sync_all_active") as sync, \
         mock.patch.object(cli, "push_fresh_balances") as push, \
         mock.patch.object(cli, "make_plaid_client", return_value=mock.Mock()), \
         mock.patch.object(cli, "SessionLocal", return_value=mock.MagicMock()):
        sync.return_value = mock.Mock(
            items=[_ok_item([{"plaid_account_id": "a"}])],
            total_processed=1, total_failed=0, dry_run=False,
        )
        push.return_value = mock.Mock(
            total_pushed=0, failed=True,
            items=[mock.Mock(institution_name="Schwab", error="WealthHTTPError: 500")],
        )
        assert cli.main(["--apply"]) == 1


def test_apply_with_only_register_items_exits_zero_and_pushes_nothing() -> None:
    """P0-r3a: a register-only run never contacts the wealth Worker. The real
    push helper is used here (not a mock) so the scope filter itself is under
    test end-to-end from the CLI."""
    real_push = cli.push_fresh_balances
    results: list = []

    def _never(payload: dict, source: str) -> dict:
        raise AssertionError("register balances must never be POSTed to D1")

    def _push_with_exploding_post(batch, **kwargs):  # type: ignore[no-untyped-def]
        result = real_push(batch, session=kwargs.get("session"), post=_never)
        results.append(result)
        return result

    with mock.patch.object(cli, "sync_all_active") as sync, \
         mock.patch.object(cli, "make_plaid_client", return_value=mock.Mock()), \
         mock.patch.object(cli, "SessionLocal", return_value=mock.MagicMock()), \
         mock.patch.object(cli, "push_fresh_balances", _push_with_exploding_post):
        sync.return_value = mock.Mock(
            items=[
                _ok_item([{"plaid_account_id": "a"}], scope="register"),
                _ok_item([{"plaid_account_id": "b"}], scope="register"),
            ],
            total_processed=2, total_failed=0, dry_run=False,
        )
        assert cli.main(["--apply"]) == 0

    # The REAL helper ran (with a post() that explodes if called) and produced
    # no per-Item push results at all.
    assert len(results) == 1
    assert results[0].items == []
    assert results[0].failed is False
