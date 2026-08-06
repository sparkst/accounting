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
    assert "OnFailure=accounting-alert-webhook@%p.service" in text
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
    assert "OnFailure=accounting-alert-webhook@%p.service" in text
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


# ── REQ-SEN-008: freshness-sentinel units ────────────────────────────────────


def test_freshness_sentinel_units_exist_in_git() -> None:
    assert (_DEPLOY_DIR / "accounting-freshness-sentinel.service").is_file()
    assert (_DEPLOY_DIR / "accounting-freshness-sentinel.timer").is_file()


def test_freshness_sentinel_service_wiring() -> None:
    text = (_DEPLOY_DIR / "accounting-freshness-sentinel.service").read_text()
    assert "OnFailure=accounting-alert-webhook@%p.service" in text
    assert "scripts.freshness_sentinel --apply" in text
    assert "env -u DOPPLER_TOKEN" in text


def test_freshness_sentinel_orders_after_producers_without_wants() -> None:
    """Ordering only — the sentinel must still run (and page) when a producer
    unit is broken or missing; that is exactly its job."""
    text = (_DEPLOY_DIR / "accounting-freshness-sentinel.service").read_text()
    after_lines = [line for line in text.splitlines() if line.startswith("After=")]
    joined = " ".join(after_lines)
    for producer in (
        "plaid-balance-sync.service",
        "plaid-transactions-sync.service",
        "accounting-stripe-sync.service",
        "accounting-shopify-sync.service",
    ):
        assert producer in joined
    for line in text.splitlines():
        if line.startswith(("Wants=", "Requires=")):
            assert ".service" not in line or "network-online" in line


def test_freshness_sentinel_timer_daily_1345_persistent() -> None:
    """13:45 UTC — after every overnight sync, ahead of the 14:00 alert window."""
    text = (_DEPLOY_DIR / "accounting-freshness-sentinel.timer").read_text()
    assert "OnCalendar=*-*-* 13:45:00 UTC" in text
    assert "Persistent=true" in text


# ── REQ-FIX-ALR-010: OnFailure cutover to the n8n severity webhook ───────────


def test_no_unit_references_the_email_alert_template() -> None:
    """Alerting consolidation §5: every unit's OnFailure= targets
    accounting-alert-webhook@ (n8n severity webhook → Telegram); the Resend
    email template must be unreferenced. The webhook template itself is
    exempt (it documents the old template in comments and has no OnFailure=)."""
    for unit in _DEPLOY_DIR.glob("*.service"):
        if unit.name == "accounting-alert-webhook@.service":
            continue
        for line in unit.read_text().splitlines():
            if line.startswith("OnFailure="):
                assert line == "OnFailure=accounting-alert-webhook@%p.service", (
                    f"{unit.name}: {line}"
                )
