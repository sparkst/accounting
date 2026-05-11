"""Tests for src/adapters/plaid_client.py — REQ-026 error classification + retry."""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
from plaid.exceptions import ApiException

from src.adapters.plaid_client import (
    RETRY_BACKOFF_SECONDS,
    RetryablePlaidError,
    TerminalPlaidError,
    UnknownPlaidError,
    _resolve_environment,
    call_with_retry,
    classify_plaid_error,
    make_plaid_client,
)


def _api_exc(error_code: str | None, status: int = 400) -> ApiException:
    """Build a mock ApiException with a Plaid-shaped error body."""
    exc = ApiException(status=status, reason="Bad Request")
    body: dict[str, str] = {"error_message": f"mock {error_code}"}
    if error_code is not None:
        body["error_code"] = error_code
    exc.body = json.dumps(body)
    return exc


# ── classify_plaid_error ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "code",
    [
        "RATE_LIMIT_EXCEEDED",
        "INTERNAL_SERVER_ERROR",
        "PLANNED_MAINTENANCE",
        "INSTITUTION_DOWN",
        "INSTITUTION_NOT_RESPONDING",
        "PRODUCT_NOT_READY",
    ],
)
def test_classify_retryable(code: str) -> None:
    typed = classify_plaid_error(_api_exc(code))
    assert isinstance(typed, RetryablePlaidError)
    assert typed.error_code == code


@pytest.mark.parametrize(
    "code",
    [
        "ITEM_LOGIN_REQUIRED",
        "INVALID_CREDENTIALS",
        "ITEM_LOCKED",
        "INVALID_ACCESS_TOKEN",
        "ACCESS_NOT_GRANTED",
        "ITEM_NOT_FOUND",
    ],
)
def test_classify_terminal(code: str) -> None:
    typed = classify_plaid_error(_api_exc(code))
    assert isinstance(typed, TerminalPlaidError)
    assert not isinstance(typed, UnknownPlaidError)
    assert typed.error_code == code


def test_classify_unknown_is_terminal_failsafe() -> None:
    typed = classify_plaid_error(_api_exc("SOMETHING_NEW_PLAID_INVENTED"))
    assert isinstance(typed, UnknownPlaidError)
    # UnknownPlaidError IS a TerminalPlaidError (so it doesn't retry forever).
    assert isinstance(typed, TerminalPlaidError)


def test_classify_missing_body_returns_unknown() -> None:
    exc = ApiException(status=500, reason="?")
    exc.body = None
    typed = classify_plaid_error(exc)
    assert isinstance(typed, UnknownPlaidError)
    assert typed.error_code == "UNKNOWN"


def test_classify_bytes_body_decodes() -> None:
    exc = ApiException(status=400, reason="?")
    exc.body = b'{"error_code":"RATE_LIMIT_EXCEEDED"}'
    typed = classify_plaid_error(exc)
    assert isinstance(typed, RetryablePlaidError)


def test_classify_garbage_body_returns_unknown() -> None:
    exc = ApiException(status=400, reason="?")
    exc.body = "<html>not json</html>"
    typed = classify_plaid_error(exc)
    assert isinstance(typed, UnknownPlaidError)


# ── call_with_retry ───────────────────────────────────────────────────────────


def test_retry_succeeds_first_try() -> None:
    fn = Mock(return_value="ok")
    sleeps: list[float] = []
    result = call_with_retry(fn, sleep=sleeps.append)
    assert result == "ok"
    assert fn.call_count == 1
    assert sleeps == []  # no sleep on first-try success


def test_retry_succeeds_after_two_retryable_errors() -> None:
    """First two calls hit RATE_LIMIT, third succeeds. Sleep called twice."""
    side_effects: list[object] = [
        _api_exc("RATE_LIMIT_EXCEEDED"),
        _api_exc("RATE_LIMIT_EXCEEDED"),
        "ok",
    ]
    fn = Mock(side_effect=side_effects)
    sleeps: list[float] = []
    result = call_with_retry(fn, backoff=(0.01, 0.02, 0.03), sleep=sleeps.append)
    assert result == "ok"
    assert fn.call_count == 3
    assert sleeps == [0.01, 0.02]


def test_retry_exhausted_raises_retryable() -> None:
    """All attempts fail with retryable → raise RetryablePlaidError after limit."""
    fn = Mock(side_effect=_api_exc("RATE_LIMIT_EXCEEDED"))
    sleeps: list[float] = []
    with pytest.raises(RetryablePlaidError) as exc_info:
        call_with_retry(fn, backoff=(0.01, 0.01, 0.01), sleep=sleeps.append)
    assert exc_info.value.error_code == "RATE_LIMIT_EXCEEDED"
    # 4 attempts total: 1 initial + 3 retries
    assert fn.call_count == 4
    assert sleeps == [0.01, 0.01, 0.01]


def test_retry_terminal_does_not_retry() -> None:
    fn = Mock(side_effect=_api_exc("ITEM_LOGIN_REQUIRED"))
    sleeps: list[float] = []
    with pytest.raises(TerminalPlaidError) as exc_info:
        call_with_retry(fn, sleep=sleeps.append)
    assert exc_info.value.error_code == "ITEM_LOGIN_REQUIRED"
    assert fn.call_count == 1
    assert sleeps == []


def test_retry_unknown_does_not_retry() -> None:
    fn = Mock(side_effect=_api_exc("BRAND_NEW_ERROR"))
    sleeps: list[float] = []
    with pytest.raises(UnknownPlaidError):
        call_with_retry(fn, sleep=sleeps.append)
    assert fn.call_count == 1


def test_retry_passes_through_non_api_exceptions() -> None:
    """Non-Plaid exceptions (e.g. network timeout from httpx) propagate as-is."""
    fn = Mock(side_effect=RuntimeError("network down"))
    with pytest.raises(RuntimeError, match="network down"):
        call_with_retry(fn, sleep=lambda _s: None)


def test_default_backoff_is_spec_table() -> None:
    """Sanity-check the module default is 1s/5s/30s per spec."""
    assert RETRY_BACKOFF_SECONDS == (1.0, 5.0, 30.0)


# ── client factory ────────────────────────────────────────────────────────────


def test_resolve_environment_sandbox() -> None:
    assert _resolve_environment("sandbox") is not None
    assert _resolve_environment("SANDBOX") is not None


def test_resolve_environment_production() -> None:
    assert _resolve_environment("production") is not None
    assert _resolve_environment("prod") is not None


def test_resolve_environment_legacy_development_warns(caplog: pytest.LogCaptureFixture) -> None:
    """The retired 'development' env name maps to Sandbox with a warning."""
    import logging

    caplog.set_level(logging.WARNING, logger="src.adapters.plaid_client")
    _resolve_environment("development")
    assert any("development" in r.message for r in caplog.records)


def test_resolve_environment_invalid_raises() -> None:
    with pytest.raises(ValueError, match="not recognized"):
        _resolve_environment("staging")


def test_make_plaid_client_missing_client_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PLAID_CLIENT_ID", raising=False)
    monkeypatch.setenv("PLAID_ENV", "sandbox")
    monkeypatch.setenv("PLAID_SANDBOX_SECRET", "anything")
    with pytest.raises(RuntimeError, match="PLAID_CLIENT_ID"):
        make_plaid_client()


def test_make_plaid_client_missing_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLAID_CLIENT_ID", "anything")
    monkeypatch.setenv("PLAID_ENV", "sandbox")
    monkeypatch.delenv("PLAID_SANDBOX_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="PLAID_SANDBOX_SECRET"):
        make_plaid_client()


def test_make_plaid_client_production_uses_production_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In production env, the production secret is required (not sandbox secret)."""
    monkeypatch.setenv("PLAID_CLIENT_ID", "anything")
    monkeypatch.setenv("PLAID_ENV", "production")
    monkeypatch.setenv("PLAID_SANDBOX_SECRET", "wrong-secret")
    monkeypatch.delenv("PLAID_PRODUCTION_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="PLAID_PRODUCTION_SECRET"):
        make_plaid_client()


def test_make_plaid_client_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLAID_CLIENT_ID", "test-id")
    monkeypatch.setenv("PLAID_ENV", "sandbox")
    monkeypatch.setenv("PLAID_SANDBOX_SECRET", "test-secret")
    client = make_plaid_client()
    assert client is not None
