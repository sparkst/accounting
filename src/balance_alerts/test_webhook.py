"""Tests for the balance-alert n8n webhook client (REQ-BAL-007)."""

from __future__ import annotations

import logging

import httpx
import pytest

from src.balance_alerts import webhook as wh
from src.balance_alerts.rules import BalanceAlert


def _alert(severity: str = "sev3", level: str | None = "1000") -> BalanceAlert:
    return BalanceAlert(
        alert_key="balance:acc:checking:1000",
        occurrence_date="2026-06-14",
        alert_type="balance_milestone",
        severity=severity,
        entity="sparkry",
        account_id="acc",
        account_name="Sparkry checking",
        kind="checking",
        level=level,
        baseline="1500.00",
        new_balance="900.00",
        title="Sparkry checking below $1,000.00",
        message="fell to 900",
    )


def test_payload_shape_and_severity_passthrough() -> None:
    p = wh.build_payload(_alert(severity="sev2"))
    assert p["type"] == "sev2"
    assert p["source"] == "accounting"
    assert p["account"] == "Sparkry checking"
    assert p["balance"] == "900.00"
    assert p["level"] == "1000"
    assert p["alert_key"] == "balance:acc:checking:1000"


def test_balance_alert_payload_has_no_bot_field() -> None:
    """REQ-DGQ-001: non-digest alerts carry no `bot` override — they stay on
    the info bot chosen by the downstream severity map."""
    p = wh.build_payload(_alert())
    assert "bot" not in p


def test_build_payload_dict_omits_bot_when_none() -> None:
    """REQ-DGQ-001: bot=None (the default) leaves the payload byte-identical —
    no `bot` key at all (backward compat)."""
    p = wh.build_payload_dict(severity="info", title="t", message="m", alert_key="k")
    assert "bot" not in p


def test_build_payload_dict_includes_bot_when_set() -> None:
    """REQ-DGQ-001: an explicit bot rides in the payload for allowlisted routing."""
    p = wh.build_payload_dict(
        severity="info", title="t", message="m", alert_key="k", bot="quark"
    )
    assert p["bot"] == "quark"


def test_dry_run_makes_no_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*a: object, **k: object) -> None:  # pragma: no cover
        raise AssertionError("network in dry-run")

    monkeypatch.setattr(httpx, "post", _boom)
    res = wh.post_balance_alert(_alert(), apply=False)
    assert res.status == "dry_run"


def test_https_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(wh.URL_ENV, "http://insecure.example/webhook")
    monkeypatch.setenv(wh.SECRET_ENV, "s3cret")
    res = wh.post_balance_alert(_alert(), apply=True)
    assert res.status == "failed"
    assert "https" in (res.error or "")


def test_missing_config_fails_gracefully(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(wh.URL_ENV, raising=False)
    monkeypatch.delenv(wh.SECRET_ENV, raising=False)
    res = wh.post_balance_alert(_alert(), apply=True)
    assert res.status == "failed"
    assert "not configured" in (res.error or "")


def test_apply_posts_with_secret_header_and_never_logs_secret(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    captured: dict[str, object] = {}

    class _Resp:
        status_code: int = 200

    def _fake_post(
        url: str, json: dict[str, object], headers: dict[str, str], timeout: float
    ) -> _Resp:
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _Resp()

    monkeypatch.setenv(wh.URL_ENV, "https://n8n.example/webhook/alert")
    monkeypatch.setenv(wh.SECRET_ENV, "TOP-SECRET-VALUE")
    monkeypatch.setattr(httpx, "post", _fake_post)
    with caplog.at_level(logging.DEBUG):
        res = wh.post_balance_alert(_alert(severity="sev2"), apply=True)

    assert res.status == "sent"
    assert res.http_status == 200
    assert captured["headers"]["X-Webhook-Secret"] == "TOP-SECRET-VALUE"  # type: ignore[index]
    assert captured["json"]["type"] == "sev2"  # type: ignore[index]
    # SECURITY: the secret value must never appear in logs.
    assert "TOP-SECRET-VALUE" not in caplog.text


def test_non_2xx_is_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        status_code: int = 500

    monkeypatch.setenv(wh.URL_ENV, "https://n8n.example/webhook/alert")
    monkeypatch.setenv(wh.SECRET_ENV, "s")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp())
    res = wh.post_balance_alert(_alert(), apply=True)
    assert res.status == "failed"
    assert res.http_status == 500


def test_network_error_is_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*a: object, **k: object) -> None:
        raise httpx.ConnectError("boom")

    monkeypatch.setenv(wh.URL_ENV, "https://n8n.example/webhook/alert")
    monkeypatch.setenv(wh.SECRET_ENV, "s")
    monkeypatch.setattr(httpx, "post", _raise)
    res = wh.post_balance_alert(_alert(), apply=True)
    assert res.status == "failed"
    assert "network error" in (res.error or "")
