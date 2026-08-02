"""Alerting-consolidation (plan 2026-08-02): systemd OnFailure → n8n severity
webhook, replacing the scripts/alert.py Resend email path.

Same contract as scripts/alert.py: one alert per failed unit per hour (dedup
sentinel), exit non-zero only on a real send failure, no recursion. Channel is
the `WH-Severity / Send Alert` webhook (N8N_SEVERITY_WEBHOOK_URL/SECRET) via
the shared `post_payload` client.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from scripts import alert_webhook as aw
from src.alerts.webhook import WebhookResult


@pytest.fixture()
def sent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[dict[str, Any]]:
    """Capture posted payloads; isolate the sentinel dir; freeze the hour."""
    captured: list[dict[str, Any]] = []

    def _fake_post_payload(
        payload: dict[str, Any], *, key: str, apply: bool, timeout: float = 10.0
    ) -> WebhookResult:
        captured.append(payload)
        return WebhookResult("sent", 200, None)

    monkeypatch.setattr(aw, "post_payload", _fake_post_payload)
    monkeypatch.setattr(aw, "_sentinel_dir", lambda: tmp_path)
    monkeypatch.setattr(aw, "_build_body", lambda unit: f"body for {unit}")
    monkeypatch.setenv("ALERT_HOUR_OVERRIDE", "2026080214")
    return captured


def test_sends_once_then_dedups(sent: list[dict[str, Any]]) -> None:
    rc1 = aw.send_alert("plaid-balance-sync.service")
    rc2 = aw.send_alert("plaid-balance-sync.service")  # same unit+hour → skip
    assert rc1 == 0 and rc2 == 0
    assert len(sent) == 1


def test_distinct_units_both_send(sent: list[dict[str, Any]]) -> None:
    aw.send_alert("plaid-balance-sync.service")
    aw.send_alert("caddy.service")
    assert len(sent) == 2


def test_serving_stack_unit_maps_to_sev2(sent: list[dict[str, Any]]) -> None:
    aw.send_alert("caddy.service")
    assert sent[0]["type"] == "sev2"
    assert "caddy.service" in sent[0]["title"]


def test_batch_job_unit_maps_to_sev3(sent: list[dict[str, Any]]) -> None:
    aw.send_alert("plaid-transactions-sync.service")
    assert sent[0]["type"] == "sev3"


def test_body_and_alert_key_ride_the_payload(sent: list[dict[str, Any]]) -> None:
    aw.send_alert("weekly-pl-report.service")
    payload = sent[0]
    assert payload["message"] == "body for weekly-pl-report.service"
    assert payload["alert_key"] == "unit:weekly-pl-report.service:2026080214"


def test_send_failure_returns_nonzero_and_writes_no_sentinel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _failing_post(
        payload: dict[str, Any], *, key: str, apply: bool, timeout: float = 10.0
    ) -> WebhookResult:
        return WebhookResult("failed", 502, "non-2xx: 502")

    monkeypatch.setattr(aw, "post_payload", _failing_post)
    monkeypatch.setattr(aw, "_sentinel_dir", lambda: tmp_path)
    monkeypatch.setattr(aw, "_build_body", lambda unit: "body")
    monkeypatch.setenv("ALERT_HOUR_OVERRIDE", "2026080214")

    rc = aw.send_alert("accounting-api.service")
    assert rc == 1
    # a failed send must NOT write a dedup sentinel (so a retry can still alert)
    assert list(tmp_path.iterdir()) == []


def test_raising_post_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _boom(
        payload: dict[str, Any], *, key: str, apply: bool, timeout: float = 10.0
    ) -> WebhookResult:
        raise RuntimeError("boom")

    monkeypatch.setattr(aw, "post_payload", _boom)
    monkeypatch.setattr(aw, "_sentinel_dir", lambda: tmp_path)
    monkeypatch.setattr(aw, "_build_body", lambda unit: "body")
    monkeypatch.setenv("ALERT_HOUR_OVERRIDE", "2026080214")

    rc = aw.send_alert("accounting-api.service")
    assert rc == 1
    assert list(tmp_path.iterdir()) == []


def test_webhook_sentinels_use_distinct_namespace(
    sent: list[dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The webhook path dedups independently of the email path — its sentinel
    filename must not collide with scripts/alert.py's `alert-<unit>-<hour>.sent`
    (during a transition window each channel keeps its own hourly budget)."""
    aw.send_alert("caddy.service")
    names = [p.name for p in tmp_path.iterdir()]
    assert names == ["alert-webhook-caddy.service-2026080214.sent"]


def test_main_requires_unit_arg() -> None:
    assert aw.main([]) == 2
