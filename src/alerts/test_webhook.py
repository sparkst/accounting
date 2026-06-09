"""Tests for the n8n webhook client.

REQ-ID: REQ-ALERT-007 (DRY-RUN default — no network without apply)
REQ-ID: REQ-ALERT-009 (payload shape + secret header)
"""

from typing import cast

import httpx
import pytest

from src.alerts.rules import Alert
from src.alerts.webhook import WebhookResult, build_payload, post_alert

_ALERT = Alert(
    alert_key="tax:sparkry:bo:2026-04",
    occurrence_date="2026-05-10",
    alert_type="tax_bo",
    entity="sparkry",
    subject="WA B&O due — Sparkry LLC",
    body_text="body here",
    due_date="2026-05-25",
    action_url="https://secure.dor.wa.gov/home/Login",
)


def test_build_payload_has_required_fields() -> None:
    payload = build_payload(_ALERT, "Travis@sparkry.com", "ea-alerts@sparkry.com")
    assert payload["from"] == "Travis@sparkry.com"
    assert payload["to"] == "ea-alerts@sparkry.com"
    assert payload["subject"] == "WA B&O due — Sparkry LLC"
    assert payload["body_text"] == "body here"
    assert payload["alert_type"] == "tax_bo"
    assert payload["entity"] == "sparkry"
    assert payload["due_date"] == "2026-05-25"
    assert payload["action_url"] == "https://secure.dor.wa.gov/home/Login"
    assert payload["alert_key"] == "tax:sparkry:bo:2026-04"
    assert payload["occurrence_date"] == "2026-05-10"


def test_dry_run_makes_no_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*a: object, **k: object) -> None:  # pragma: no cover
        raise AssertionError("network call made during DRY-RUN")

    monkeypatch.setattr(httpx, "post", _boom)
    result = post_alert(_ALERT, apply=False)
    assert isinstance(result, WebhookResult)
    assert result.status == "dry_run"
    assert result.http_status is None


def test_apply_posts_with_secret_header(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class _Resp:
        status_code: int = 200
        is_success: bool = True
        text: str = "ok"

    def _fake_post(url: str, json: dict[str, object], headers: dict[str, str], timeout: float) -> _Resp:
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _Resp()

    monkeypatch.setenv("N8N_ALERTS_WEBHOOK_URL", "https://n8n.example/webhook/alerts")
    monkeypatch.setenv("N8N_ALERTS_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setattr(httpx, "post", _fake_post)

    result = post_alert(_ALERT, apply=True)
    assert result.status == "sent"
    assert result.http_status == 200
    assert captured["url"] == "https://n8n.example/webhook/alerts"
    assert cast(dict[str, object], captured["headers"])["X-Webhook-Secret"] == "s3cret"
    assert cast(dict[str, object], captured["json"])["alert_key"] == "tax:sparkry:bo:2026-04"


def test_apply_without_config_returns_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("N8N_ALERTS_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("N8N_ALERTS_WEBHOOK_SECRET", raising=False)
    result = post_alert(_ALERT, apply=True)
    assert result.status == "failed"
    assert result.http_status is None


def test_apply_rejects_non_https_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("N8N_ALERTS_WEBHOOK_URL", "http://n8n.example/webhook/alerts")
    monkeypatch.setenv("N8N_ALERTS_WEBHOOK_SECRET", "s3cret")
    result = post_alert(_ALERT, apply=True)
    assert result.status == "failed"
    assert result.error == "webhook url must be https"


def test_apply_rejects_non_allowlisted_recipient(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALERT_TO_EMAIL", "attacker@evil.com")
    result = post_alert(_ALERT, apply=False)
    assert result.status == "failed"
    assert result.error == "recipient not allowlisted"


def test_apply_rejects_non_allowlisted_sender(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALERT_FROM_EMAIL", "spoofed@evil.com")
    result = post_alert(_ALERT, apply=False)
    assert result.status == "failed"
    assert result.error == "sender not allowlisted"


def test_build_payload_body_html_is_none_by_default() -> None:
    payload = build_payload(_ALERT, "Travis@sparkry.com", "ea-alerts@sparkry.com")
    assert payload["body_html"] is None


def test_build_payload_body_html_passes_through() -> None:
    alert_with_html = Alert(
        alert_key="tax:sparkry:bo:2026-04",
        occurrence_date="2026-05-10",
        alert_type="tax_bo",
        entity="sparkry",
        subject="WA B&O due",
        body_text="plain body",
        due_date="2026-05-25",
        action_url="https://secure.dor.wa.gov/home/Login",
        body_html="<p>html body</p>",
    )
    payload = build_payload(alert_with_html, "Travis@sparkry.com", "ea-alerts@sparkry.com")
    assert payload["body_html"] == "<p>html body</p>"


def test_apply_non_2xx_is_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        status_code: int = 500
        is_success: bool = False
        text: str = "boom"

    monkeypatch.setenv("N8N_ALERTS_WEBHOOK_URL", "https://n8n.example/webhook/alerts")
    monkeypatch.setenv("N8N_ALERTS_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp())
    result = post_alert(_ALERT, apply=True)
    assert result.status == "failed"
    assert result.http_status == 500
