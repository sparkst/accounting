"""Deploy-unit-file assertions — REQ-FIX-ALR-007.

`accounting-balance-alerts.service` must order `After=` on
`plaid-balance-sync.service` (a Persistent=true boot catch-up needs the
04:00 sync to have run before the 14:00 dispatcher evaluates) WITHOUT a
`Wants=`/`Requires=` on it — a sync failure must never block alerting.
"""

from __future__ import annotations

from pathlib import Path

_UNIT_PATH = (
    Path(__file__).resolve().parents[1] / "deploy" / "accounting-balance-alerts.service"
)


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
