"""Tests for src/adapters/_shared/wealth_client.py (IC-T01).

REQ-WC-012: cloud-mode POSTs use X-Internal-Key header; non-2xx → typed error;
missing env → clear error.

We use pytest-monkeypatch + httpx's own transport mock (``httpx.MockTransport``
/ ``respx``) — but since ``respx`` is not in the project deps, we patch
``httpx.post`` directly, which is simpler and avoids adding a new dependency.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.adapters._shared.wealth_client import (
    WealthClientError,
    WealthConfigError,
    WealthHTTPError,
    WealthProtocolError,
    WealthRateLimitError,
    WealthServerError,
    WealthTransportError,
    WealthUnauthorizedError,
    post_to_wealth,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GOOD_ENV = {
    "WEALTH_API_BASE": "https://internal.sparkry.ai",
    "WEALTH_INTERNAL_KEY": "test-key-abc123",
}


def _mock_response(
    status_code: int,
    json_body: Any = None,
    text: str = "",
    headers: dict[str, str] | None = None,
) -> MagicMock:
    """Return a mock that mimics an httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.is_success = 200 <= status_code < 300
    resp.text = text if text else str(json_body or "")
    resp.json.return_value = json_body or {}
    resp.headers = headers if headers is not None else {}
    return resp


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_post_to_wealth_happy_path_returns_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """Successful POST returns parsed JSON body."""
    monkeypatch.setenv("WEALTH_API_BASE", "https://internal.sparkry.ai")
    monkeypatch.setenv("WEALTH_INTERNAL_KEY", "test-key-abc123")

    expected_response = {"inserted": 3, "errors": []}
    with patch("src.adapters._shared.wealth_client.httpx.post") as mock_post:
        mock_post.return_value = _mock_response(200, json_body=expected_response)
        result = post_to_wealth({"rows": ["a", "b", "c"]}, "brokerage-csv")

    assert result == expected_response
    mock_post.assert_called_once()


def test_post_to_wealth_correct_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """URL is assembled as WEALTH_API_BASE + /wealth/api/internal/ingest/{source}."""
    monkeypatch.setenv("WEALTH_API_BASE", "https://internal.sparkry.ai")
    monkeypatch.setenv("WEALTH_INTERNAL_KEY", "test-key-abc123")

    with patch("src.adapters._shared.wealth_client.httpx.post") as mock_post:
        mock_post.return_value = _mock_response(200, json_body={})
        post_to_wealth({"rows": []}, "xlsx-snapshot")

    call_url = mock_post.call_args[0][0]
    assert call_url == "https://internal.sparkry.ai/wealth/api/internal/ingest/xlsx-snapshot"


def test_post_to_wealth_strips_trailing_slash_from_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trailing slash on WEALTH_API_BASE is stripped before joining."""
    monkeypatch.setenv("WEALTH_API_BASE", "https://internal.sparkry.ai/")
    monkeypatch.setenv("WEALTH_INTERNAL_KEY", "test-key-abc123")

    with patch("src.adapters._shared.wealth_client.httpx.post") as mock_post:
        mock_post.return_value = _mock_response(200, json_body={})
        post_to_wealth({"rows": []}, "brokerage-csv")

    call_url = mock_post.call_args[0][0]
    assert call_url == "https://internal.sparkry.ai/wealth/api/internal/ingest/brokerage-csv"


def test_post_to_wealth_x_internal_key_header_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """X-Internal-Key header carries the WEALTH_INTERNAL_KEY value."""
    monkeypatch.setenv("WEALTH_API_BASE", "https://internal.sparkry.ai")
    monkeypatch.setenv("WEALTH_INTERNAL_KEY", "super-secret-key-xyz")

    with patch("src.adapters._shared.wealth_client.httpx.post") as mock_post:
        mock_post.return_value = _mock_response(200, json_body={})
        post_to_wealth({"rows": []}, "brokerage-csv")

    _, call_kwargs = mock_post.call_args
    headers = call_kwargs["headers"]
    assert headers["X-Internal-Key"] == "super-secret-key-xyz"


def test_post_to_wealth_payload_passed_as_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """payload dict is forwarded as the json= argument to httpx.post."""
    monkeypatch.setenv("WEALTH_API_BASE", "https://internal.sparkry.ai")
    monkeypatch.setenv("WEALTH_INTERNAL_KEY", "k")

    my_payload = {"rows": [{"symbol": "VTI", "price": "1.23"}], "meta": "test"}
    with patch("src.adapters._shared.wealth_client.httpx.post") as mock_post:
        mock_post.return_value = _mock_response(200, json_body={})
        post_to_wealth(my_payload, "brokerage-csv")

    _, call_kwargs = mock_post.call_args
    assert call_kwargs["json"] == my_payload


def test_post_to_wealth_timeout_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Custom timeout is forwarded to httpx.post."""
    monkeypatch.setenv("WEALTH_API_BASE", "https://internal.sparkry.ai")
    monkeypatch.setenv("WEALTH_INTERNAL_KEY", "k")

    with patch("src.adapters._shared.wealth_client.httpx.post") as mock_post:
        mock_post.return_value = _mock_response(200, json_body={})
        post_to_wealth({"rows": []}, "brokerage-csv", timeout=60.0)

    _, call_kwargs = mock_post.call_args
    assert call_kwargs["timeout"] == 60.0


# ---------------------------------------------------------------------------
# Missing env var → WealthConfigError
# ---------------------------------------------------------------------------


def test_missing_wealth_api_base_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WealthConfigError raised when WEALTH_API_BASE is absent."""
    monkeypatch.delenv("WEALTH_API_BASE", raising=False)
    monkeypatch.setenv("WEALTH_INTERNAL_KEY", "k")

    with pytest.raises(WealthConfigError, match="WEALTH_API_BASE"):
        post_to_wealth({}, "brokerage-csv")


def test_empty_wealth_api_base_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WealthConfigError raised when WEALTH_API_BASE is set but empty."""
    monkeypatch.setenv("WEALTH_API_BASE", "")
    monkeypatch.setenv("WEALTH_INTERNAL_KEY", "k")

    with pytest.raises(WealthConfigError, match="WEALTH_API_BASE"):
        post_to_wealth({}, "brokerage-csv")


def test_missing_wealth_internal_key_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WealthConfigError raised when WEALTH_INTERNAL_KEY is absent."""
    monkeypatch.setenv("WEALTH_API_BASE", "https://internal.sparkry.ai")
    monkeypatch.delenv("WEALTH_INTERNAL_KEY", raising=False)

    with pytest.raises(WealthConfigError, match="WEALTH_INTERNAL_KEY"):
        post_to_wealth({}, "brokerage-csv")


def test_empty_wealth_internal_key_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WealthConfigError raised when WEALTH_INTERNAL_KEY is set but empty."""
    monkeypatch.setenv("WEALTH_API_BASE", "https://internal.sparkry.ai")
    monkeypatch.setenv("WEALTH_INTERNAL_KEY", "")

    with pytest.raises(WealthConfigError, match="WEALTH_INTERNAL_KEY"):
        post_to_wealth({}, "brokerage-csv")


# ---------------------------------------------------------------------------
# Non-2xx responses → typed exceptions
# ---------------------------------------------------------------------------


def test_401_raises_wealth_unauthorized_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP 401 raises WealthUnauthorizedError, a subtype of WealthHTTPError."""
    monkeypatch.setenv("WEALTH_API_BASE", "https://internal.sparkry.ai")
    monkeypatch.setenv("WEALTH_INTERNAL_KEY", "k")

    with patch("src.adapters._shared.wealth_client.httpx.post") as mock_post:
        mock_post.return_value = _mock_response(401, text="Unauthorized")
        with pytest.raises(WealthUnauthorizedError) as exc_info:
            post_to_wealth({}, "brokerage-csv")

    assert isinstance(exc_info.value, WealthHTTPError)
    assert exc_info.value.status_code == 401


def test_429_raises_wealth_rate_limit_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REQ-FIX-WLT-008: persistent HTTP 429 raises WealthRateLimitError after
    the bounded retries are exhausted."""
    monkeypatch.setenv("WEALTH_API_BASE", "https://internal.sparkry.ai")
    monkeypatch.setenv("WEALTH_INTERNAL_KEY", "k")

    with (
        patch("src.adapters._shared.wealth_client.httpx.post") as mock_post,
        patch("src.adapters._shared.wealth_client.time.sleep") as mock_sleep,
    ):
        mock_post.return_value = _mock_response(429, text="Too Many Requests")
        with pytest.raises(WealthRateLimitError) as exc_info:
            post_to_wealth({}, "brokerage-csv")

    assert exc_info.value.status_code == 429
    # 1 initial attempt + 2 retries, sleeping before each retry
    assert mock_post.call_count == 3
    assert mock_sleep.call_count == 2


def test_429_then_success_retries_and_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REQ-FIX-WLT-008: a transient 429 (WAF rate rule during a multi-item
    push run) is retried after a backoff and the eventual 2xx is returned."""
    monkeypatch.setenv("WEALTH_API_BASE", "https://internal.sparkry.ai")
    monkeypatch.setenv("WEALTH_INTERNAL_KEY", "k")

    expected = {"inserted": 2, "errors": []}
    with (
        patch("src.adapters._shared.wealth_client.httpx.post") as mock_post,
        patch("src.adapters._shared.wealth_client.time.sleep") as mock_sleep,
    ):
        mock_post.side_effect = [
            _mock_response(429, text="Too Many Requests"),
            _mock_response(200, json_body=expected),
        ]
        result = post_to_wealth({}, "plaid-balance")

    assert result == expected
    assert mock_post.call_count == 2
    mock_sleep.assert_called_once()
    # default backoff outlives the WAF window
    assert mock_sleep.call_args[0][0] >= 60.0


def test_429_retry_honors_retry_after_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REQ-FIX-WLT-008: a numeric Retry-After header sets the backoff (capped)."""
    monkeypatch.setenv("WEALTH_API_BASE", "https://internal.sparkry.ai")
    monkeypatch.setenv("WEALTH_INTERNAL_KEY", "k")

    with (
        patch("src.adapters._shared.wealth_client.httpx.post") as mock_post,
        patch("src.adapters._shared.wealth_client.time.sleep") as mock_sleep,
    ):
        mock_post.side_effect = [
            _mock_response(429, text="slow down", headers={"Retry-After": "15"}),
            _mock_response(200, json_body={}),
        ]
        post_to_wealth({}, "plaid-balance")

    mock_sleep.assert_called_once_with(15.0)


def test_429_retry_caps_absurd_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REQ-FIX-WLT-008: an oversized Retry-After is capped so a sync run can
    never hang for hours on a hostile/buggy header."""
    monkeypatch.setenv("WEALTH_API_BASE", "https://internal.sparkry.ai")
    monkeypatch.setenv("WEALTH_INTERNAL_KEY", "k")

    with (
        patch("src.adapters._shared.wealth_client.httpx.post") as mock_post,
        patch("src.adapters._shared.wealth_client.time.sleep") as mock_sleep,
    ):
        mock_post.side_effect = [
            _mock_response(429, text="slow down", headers={"Retry-After": "86400"}),
            _mock_response(200, json_body={}),
        ]
        post_to_wealth({}, "plaid-balance")

    assert mock_sleep.call_args[0][0] <= 120.0


def test_429_retry_defaults_on_malformed_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REQ-FIX-WLT-008: an HTTP-date (or garbage) Retry-After falls back to the
    default backoff rather than crashing."""
    monkeypatch.setenv("WEALTH_API_BASE", "https://internal.sparkry.ai")
    monkeypatch.setenv("WEALTH_INTERNAL_KEY", "k")

    with (
        patch("src.adapters._shared.wealth_client.httpx.post") as mock_post,
        patch("src.adapters._shared.wealth_client.time.sleep") as mock_sleep,
    ):
        mock_post.side_effect = [
            _mock_response(
                429, text="slow down", headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
            ),
            _mock_response(200, json_body={}),
        ]
        post_to_wealth({}, "plaid-balance")

    assert mock_sleep.call_args[0][0] >= 60.0


def test_429_no_retry_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REQ-FIX-WLT-008: rate_limit_retries=0 preserves the old fail-fast
    behavior for interactive callers."""
    monkeypatch.setenv("WEALTH_API_BASE", "https://internal.sparkry.ai")
    monkeypatch.setenv("WEALTH_INTERNAL_KEY", "k")

    with (
        patch("src.adapters._shared.wealth_client.httpx.post") as mock_post,
        patch("src.adapters._shared.wealth_client.time.sleep") as mock_sleep,
    ):
        mock_post.return_value = _mock_response(429, text="Too Many Requests")
        with pytest.raises(WealthRateLimitError):
            post_to_wealth({}, "brokerage-csv", rate_limit_retries=0)

    assert mock_post.call_count == 1
    mock_sleep.assert_not_called()


def test_500_raises_wealth_server_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP 5xx raises WealthServerError."""
    monkeypatch.setenv("WEALTH_API_BASE", "https://internal.sparkry.ai")
    monkeypatch.setenv("WEALTH_INTERNAL_KEY", "k")

    with patch("src.adapters._shared.wealth_client.httpx.post") as mock_post:
        mock_post.return_value = _mock_response(500, text="Internal Server Error")
        with pytest.raises(WealthServerError) as exc_info:
            post_to_wealth({}, "brokerage-csv")

    assert exc_info.value.status_code == 500
    assert isinstance(exc_info.value, WealthHTTPError)


def test_503_raises_wealth_server_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP 503 raises WealthServerError (generic 5xx bucket)."""
    monkeypatch.setenv("WEALTH_API_BASE", "https://internal.sparkry.ai")
    monkeypatch.setenv("WEALTH_INTERNAL_KEY", "k")

    with patch("src.adapters._shared.wealth_client.httpx.post") as mock_post:
        mock_post.return_value = _mock_response(503, text="Service Unavailable")
        with pytest.raises(WealthServerError):
            post_to_wealth({}, "brokerage-csv")


def test_422_raises_wealth_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP 422 raises generic WealthHTTPError (not a 4xx subtype)."""
    monkeypatch.setenv("WEALTH_API_BASE", "https://internal.sparkry.ai")
    monkeypatch.setenv("WEALTH_INTERNAL_KEY", "k")

    with patch("src.adapters._shared.wealth_client.httpx.post") as mock_post:
        mock_post.return_value = _mock_response(422, text="Unprocessable Entity")
        with pytest.raises(WealthHTTPError) as exc_info:
            post_to_wealth({}, "brokerage-csv")

    assert exc_info.value.status_code == 422
    # Must be the base class, NOT WealthUnauthorizedError/WealthServerError
    assert type(exc_info.value) is WealthHTTPError


def test_413_raises_wealth_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP 413 (payload too large) raises WealthHTTPError."""
    monkeypatch.setenv("WEALTH_API_BASE", "https://internal.sparkry.ai")
    monkeypatch.setenv("WEALTH_INTERNAL_KEY", "k")

    with patch("src.adapters._shared.wealth_client.httpx.post") as mock_post:
        mock_post.return_value = _mock_response(413, text="Payload Too Large")
        with pytest.raises(WealthHTTPError) as exc_info:
            post_to_wealth({}, "brokerage-csv")

    assert exc_info.value.status_code == 413


# ---------------------------------------------------------------------------
# REQ-FIX-WLT-007: transport errors → WealthTransportError
# ---------------------------------------------------------------------------


def test_connect_error_raises_wealth_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """httpx.ConnectError (DNS/TLS/connect) → WealthTransportError, not a crash."""
    monkeypatch.setenv("WEALTH_API_BASE", "https://internal.sparkry.ai")
    monkeypatch.setenv("WEALTH_INTERNAL_KEY", "k")

    with patch("src.adapters._shared.wealth_client.httpx.post") as mock_post:
        mock_post.side_effect = httpx.ConnectError("name resolution failed")
        with pytest.raises(WealthTransportError) as exc_info:
            post_to_wealth({"rows": []}, "brokerage-csv")

    # Stays inside the hierarchy so batch callers catch it.
    assert isinstance(exc_info.value, WealthClientError)


def test_timeout_raises_wealth_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """httpx.ReadTimeout is a subclass of httpx.HTTPError → WealthTransportError."""
    monkeypatch.setenv("WEALTH_API_BASE", "https://internal.sparkry.ai")
    monkeypatch.setenv("WEALTH_INTERNAL_KEY", "k")

    with patch("src.adapters._shared.wealth_client.httpx.post") as mock_post:
        mock_post.side_effect = httpx.ReadTimeout("timed out")
        with pytest.raises(WealthTransportError):
            post_to_wealth({"rows": []}, "brokerage-csv")


# ---------------------------------------------------------------------------
# REQ-FIX-WLT-007: non-JSON 2xx body → WealthProtocolError
# ---------------------------------------------------------------------------


def test_non_json_2xx_body_raises_wealth_protocol_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A text/plain 200 whose body is not JSON → WealthProtocolError."""
    monkeypatch.setenv("WEALTH_API_BASE", "https://internal.sparkry.ai")
    monkeypatch.setenv("WEALTH_INTERNAL_KEY", "k")

    resp = MagicMock()
    resp.status_code = 200
    resp.is_success = True
    resp.text = "<html>not json</html>"
    resp.json.side_effect = ValueError("Expecting value: line 1 column 1")

    with patch("src.adapters._shared.wealth_client.httpx.post") as mock_post:
        mock_post.return_value = resp
        with pytest.raises(WealthProtocolError) as exc_info:
            post_to_wealth({"rows": []}, "brokerage-csv")

    assert isinstance(exc_info.value, WealthClientError)
    assert exc_info.value.status_code == 200
    assert "not json" in exc_info.value.body


# ---------------------------------------------------------------------------
# All errors are WealthClientError (parent class)
# ---------------------------------------------------------------------------


def test_all_errors_are_subclass_of_wealth_client_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WealthHTTPError and WealthConfigError are both WealthClientError subclasses."""
    assert issubclass(WealthHTTPError, WealthClientError)
    assert issubclass(WealthConfigError, WealthClientError)
    assert issubclass(WealthUnauthorizedError, WealthClientError)
    assert issubclass(WealthRateLimitError, WealthClientError)
    assert issubclass(WealthServerError, WealthClientError)
    assert issubclass(WealthTransportError, WealthClientError)
    assert issubclass(WealthProtocolError, WealthClientError)


# ---------------------------------------------------------------------------
# Security: X-Internal-Key not logged
# ---------------------------------------------------------------------------


def test_x_internal_key_not_in_error_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The secret key value must not appear in any raised exception message."""
    secret_key = "SUPER_SECRET_KEY_DO_NOT_LOG"
    monkeypatch.setenv("WEALTH_API_BASE", "https://internal.sparkry.ai")
    monkeypatch.setenv("WEALTH_INTERNAL_KEY", secret_key)

    with patch("src.adapters._shared.wealth_client.httpx.post") as mock_post:
        mock_post.return_value = _mock_response(401, text="Unauthorized")
        try:
            post_to_wealth({}, "brokerage-csv")
        except WealthHTTPError as exc:
            assert secret_key not in str(exc), (
                f"X-Internal-Key value '{secret_key}' leaked into exception message"
            )


# ---------------------------------------------------------------------------
# REQ-WC-019: key-rotation test (xfail — rotation mechanism is Workers-side)
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    reason=(
        "REQ-WC-019: Two-key rotation window test is forward-looking. "
        "The Worker implementation (reading WEALTH_KV for the previous key "
        "during the 5-min overlap window) lives in crm/workers-brokerage. "
        "This test will be un-xfailed when that Worker handler is deployed."
    ),
    strict=False,
)
def test_old_key_returns_401_after_rotation_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate rotated-key window: old key still works for ≤5 min, then 401.

    Per REQ-WC-019: the Worker KV-entry expiry for the previous key is 5 min.
    After 5 min the Worker unconditionally rejects the old key with 401.

    This test simulates the Worker's behaviour by mocking the 401 after the
    window expires.  The actual enforcement lives in workers-brokerage.
    """
    monkeypatch.setenv("WEALTH_API_BASE", "https://internal.sparkry.ai")
    # Old key (before rotation).
    monkeypatch.setenv("WEALTH_INTERNAL_KEY", "old-key-pre-rotation")

    with patch("src.adapters._shared.wealth_client.httpx.post") as mock_post:
        # Simulate: after 5 min the Worker unconditionally rejects the old key.
        mock_post.return_value = _mock_response(401, text="Unauthorized — key rotated")
        with pytest.raises(WealthUnauthorizedError):
            post_to_wealth({"rows": []}, "brokerage-csv")
