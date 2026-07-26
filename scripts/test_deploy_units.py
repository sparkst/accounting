"""Deploy-unit-file assertions — REQ-FIX-ALR-007 + REQ-PC-B3/B6.

`accounting-balance-alerts.service` must order `After=` on
`plaid-balance-sync.service` (a Persistent=true boot catch-up needs the
04:00 sync to have run before the 14:00 dispatcher evaluates) WITHOUT a
`Wants=`/`Requires=` on it — a sync failure must never block alerting.

REQ-PC-B6: `plaid-transactions-sync.{service,timer}` are versioned in git
with a Description that matches what the unit actually does (the box's
hand-installed copy carried a stale/wrong one). REQ-PC-B3 adds
`plaid-investments-sync.{service,timer}`.
"""

from __future__ import annotations

from pathlib import Path

_DEPLOY_DIR = Path(__file__).resolve().parents[1] / "deploy"
_UNIT_PATH = _DEPLOY_DIR / "accounting-balance-alerts.service"


def _read_unit() -> str:
    return _UNIT_PATH.read_text()


def test_after_plaid_balance_sync_present() -> None:
    text = _read_unit()
    after_lines = [line for line in text.splitlines() if line.startswith("After=")]
    assert any("plaid-balance-sync.service" in line for line in after_lines)


def test_no_wants_or_requires_on_plaid_balance_sync() -> None:
    text = _read_unit()
    for line in text.splitlines():
        if line.startswith(("Wants=", "Requires=")):
            assert "plaid-balance-sync.service" not in line


# ── REQ-PC-B6: plaid-transactions-sync units versioned with correct metadata ──


def test_plaid_transactions_sync_units_exist_in_git() -> None:
    assert (_DEPLOY_DIR / "plaid-transactions-sync.service").is_file()
    assert (_DEPLOY_DIR / "plaid-transactions-sync.timer").is_file()


def test_plaid_transactions_sync_service_description_and_wiring() -> None:
    text = (_DEPLOY_DIR / "plaid-transactions-sync.service").read_text()
    desc = next(line for line in text.splitlines() if line.startswith("Description="))
    # The corrected Description names TRANSACTIONS (not balances) and the sync mode.
    assert "transactions sync" in desc
    assert "balance" not in desc.lower()
    assert "OnFailure=accounting-alert@%p.service" in text
    assert "scripts.plaid_transactions_sync --apply" in text
    assert "env -u DOPPLER_TOKEN" in text


def test_plaid_transactions_sync_timer_daily_0500() -> None:
    text = (_DEPLOY_DIR / "plaid-transactions-sync.timer").read_text()
    assert "OnCalendar=*-*-* 05:00:00 UTC" in text
    assert "Persistent=true" in text


# ── REQ-PC-B3: plaid-investments-sync units ──────────────────────────────────


def test_plaid_investments_sync_units_exist_in_git() -> None:
    assert (_DEPLOY_DIR / "plaid-investments-sync.service").is_file()
    assert (_DEPLOY_DIR / "plaid-investments-sync.timer").is_file()


def test_plaid_investments_sync_service_wiring() -> None:
    text = (_DEPLOY_DIR / "plaid-investments-sync.service").read_text()
    assert "OnFailure=accounting-alert@%p.service" in text
    assert "scripts.plaid_investments_sync --apply" in text
    assert "env -u DOPPLER_TOKEN" in text
    # Ordered after the balance sync, but never Wants=/Requires= on it.
    after_lines = [line for line in text.splitlines() if line.startswith("After=")]
    assert any("plaid-balance-sync.service" in line for line in after_lines)
    for line in text.splitlines():
        if line.startswith(("Wants=", "Requires=")):
            assert "plaid-balance-sync.service" not in line


def test_plaid_investments_sync_timer_daily_0420() -> None:
    text = (_DEPLOY_DIR / "plaid-investments-sync.timer").read_text()
    assert "OnCalendar=*-*-* 04:20:00 UTC" in text
    assert "Persistent=true" in text
