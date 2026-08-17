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
    # hermetic: no systemctl/journalctl on the test host
    monkeypatch.setattr(aw, "_unit_result", lambda unit: None)
    monkeypatch.setattr(aw, "_probe_line", lambda unit: None)
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
    assert payload["message"].endswith("body for weekly-pl-report.service")
    assert payload["alert_key"] == "unit:weekly-pl-report.service:2026080214"


def test_send_failure_returns_nonzero_and_writes_no_sentinel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _failing_post(
        payload: dict[str, Any], *, key: str, apply: bool, timeout: float = 10.0
    ) -> WebhookResult:
        return WebhookResult("failed", 502, "non-2xx: 502")

    monkeypatch.setattr(aw, "post_payload", _failing_post)
    monkeypatch.setattr(aw, "_email_fallback", lambda unit: 1)
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
    monkeypatch.setattr(aw, "_email_fallback", lambda unit: 1)
    monkeypatch.setattr(aw, "_sentinel_dir", lambda: tmp_path)
    monkeypatch.setattr(aw, "_build_body", lambda unit: "body")
    monkeypatch.setenv("ALERT_HOUR_OVERRIDE", "2026080214")

    rc = aw.send_alert("accounting-api.service")
    assert rc == 1
    assert list(tmp_path.iterdir()) == []


def test_webhook_failure_falls_back_to_email(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """n8n-outage resilience: a failed webhook POST falls back to the legacy
    Resend email path (its own dedup namespace) and exits 0 when the email
    delivers — an n8n outage must never silence unit-failure alerting."""
    fallback_calls: list[str] = []

    def _failing_post(
        payload: dict[str, Any], *, key: str, apply: bool, timeout: float = 10.0
    ) -> WebhookResult:
        return WebhookResult("failed", None, "network error")

    monkeypatch.setattr(aw, "post_payload", _failing_post)
    monkeypatch.setattr(
        aw, "_email_fallback", lambda unit: (fallback_calls.append(unit), 0)[1]
    )
    monkeypatch.setattr(aw, "_sentinel_dir", lambda: tmp_path)
    monkeypatch.setattr(aw, "_build_body", lambda unit: "body")
    monkeypatch.setenv("ALERT_HOUR_OVERRIDE", "2026080214")

    rc = aw.send_alert("accounting-backup.service")
    assert rc == 0
    assert fallback_calls == ["accounting-backup.service"]
    # webhook sentinel NOT written — a later webhook retry can still alert
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


# ---------------------------------------------------------------------------
# Issue #53: distinguish "unit slow to start" (host starvation) from
# "endpoint down" for accounting-uptime-check. The probe result and the
# systemd Result= belong in the alert body; only a real probe failure pages sev2.
# ---------------------------------------------------------------------------

_UPTIME = "accounting-uptime-check.service"


def _wire_probe(
    monkeypatch: pytest.MonkeyPatch, *, result: str | None, probe: str | None
) -> None:
    monkeypatch.setattr(aw, "_unit_result", lambda unit: result)
    monkeypatch.setattr(aw, "_probe_line", lambda unit: probe)


def test_uptime_timeout_with_probe_ok_downgrades_to_info(
    sent: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _wire_probe(
        monkeypatch,
        result="timeout",
        probe="uptime ok: http://127.0.0.1:9000/api/health/ping -> 200",
    )
    assert aw.send_alert(_UPTIME) == 0
    p = sent[0]
    assert p["type"] == "info"
    assert "slow to start" in p["title"]
    assert "probe OK" in p["title"]
    assert "Probe result: uptime ok" in p["message"]
    assert "systemd Result: timeout" in p["message"]
    assert "NOT a serving-stack outage" in p["message"]


def test_uptime_timeout_without_probe_line_is_sev3(
    sent: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _wire_probe(monkeypatch, result="timeout", probe=None)
    aw.send_alert(_UPTIME)
    p = sent[0]
    assert p["type"] == "sev3"
    assert "before the probe completed" in p["title"]
    assert "Probe result: (none" in p["message"]


def test_uptime_probe_fail_stays_sev2_with_probe_line(
    sent: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = "uptime FAIL: http://127.0.0.1:9000/api/health/ping -> code=502 body=Bad Gateway"
    _wire_probe(monkeypatch, result="exit-code", probe=probe)
    aw.send_alert(_UPTIME)
    p = sent[0]
    assert p["type"] == "sev2"
    assert p["title"] == f"[accounting/hetzner] unit failed: {_UPTIME}"
    assert f"Probe result: {probe}" in p["message"]
    assert "systemd Result: exit-code" in p["message"]


def test_uptime_timeout_but_probe_fail_line_stays_sev2(
    sent: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timeout whose invocation still logged a FAIL line is a real outage."""
    _wire_probe(monkeypatch, result="timeout", probe="uptime FAIL: x -> code=000 body=")
    aw.send_alert(_UPTIME)
    assert sent[0]["type"] == "sev2"


def test_non_uptime_units_carry_result_but_keep_severity(
    sent: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _wire_probe(monkeypatch, result="timeout", probe=None)
    aw.send_alert("plaid-balance-sync.service")
    aw.send_alert("caddy.service")
    assert sent[0]["type"] == "sev3"
    assert sent[1]["type"] == "sev2"
    assert "systemd Result: timeout" in sent[0]["message"]
    assert "Probe result" not in sent[0]["message"]


def test_unknown_result_is_omitted_gracefully(
    sent: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _wire_probe(monkeypatch, result=None, probe=None)
    aw.send_alert("caddy.service")
    assert sent[0]["type"] == "sev2"
    assert "systemd Result" not in sent[0]["message"]


def test_probe_line_picks_last_probe_line_of_journal(monkeypatch: pytest.MonkeyPatch) -> None:
    journal = "uptime ok: a -> 200\nsome noise\nuptime FAIL: b -> code=502 body=x"
    monkeypatch.setattr(aw, "_invocation_journal", lambda unit: journal)
    assert aw._probe_line(_UPTIME) == "uptime FAIL: b -> code=502 body=x"
    monkeypatch.setattr(aw, "_invocation_journal", lambda unit: "no probe line here")
    assert aw._probe_line(_UPTIME) is None
    monkeypatch.setattr(aw, "_invocation_journal", lambda unit: None)
    assert aw._probe_line(_UPTIME) is None
