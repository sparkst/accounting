"""Shared HTTP client for posting normalized rows to the Wealth Workers API.

REQ-WC-012: local Python importers POST to Workers when ``--target cloud``.

Auth: every request carries ``X-Internal-Key`` from ``WEALTH_INTERNAL_KEY``
env var.  Target URL is ``WEALTH_API_BASE/wealth/api/internal/ingest/{source}``.

Usage::

    from src.adapters._shared.wealth_client import post_to_wealth, WealthClientError

    response = post_to_wealth(payload={"rows": [...]}, source="xlsx-snapshot")

Raises :class:`WealthClientError` (or a subclass) on non-2xx responses or
missing env vars.  The caller is responsible for per-row error isolation.
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Typed exception hierarchy
# ---------------------------------------------------------------------------


class WealthClientError(Exception):
    """Base class for all wealth-client errors."""


class WealthConfigError(WealthClientError):
    """A required environment variable is missing or empty."""


class WealthHTTPError(WealthClientError):
    """The Workers endpoint returned a non-2xx status.

    Attributes
    ----------
    status_code : int
        The HTTP status code returned by the server.
    body : str
        The response body (truncated if very large).
    """

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"Workers ingest returned HTTP {status_code}: {body[:400]}")


class WealthUnauthorizedError(WealthHTTPError):
    """401 response — key is wrong or rotated."""


class WealthRateLimitError(WealthHTTPError):
    """429 response — Cloudflare WAF rate limit hit."""


class WealthServerError(WealthHTTPError):
    """5xx response from the Workers endpoint."""


class WealthTransportError(WealthClientError):
    """A transport-layer failure reaching the Workers endpoint.

    Wraps :class:`httpx.HTTPError` (DNS resolution, TLS handshake, connect,
    read/write timeout) so importer callers that ``except WealthClientError``
    don't crash mid-batch on a network blip (REQ-FIX-WLT-007).
    """


class WealthProtocolError(WealthClientError):
    """A 2xx response whose body was not valid JSON.

    Attributes
    ----------
    status_code : int
        The (successful) HTTP status code returned by the server.
    body : str
        The response body (truncated to 500 chars).
    """

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(
            f"Workers ingest returned HTTP {status_code} with a non-JSON body: "
            f"{body[:400]}"
        )


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


def post_to_wealth(
    payload: dict,  # type: ignore[type-arg]
    source: str,
    *,
    timeout: float = 30.0,
) -> dict:  # type: ignore[type-arg]
    """POST *payload* to ``WEALTH_API_BASE/wealth/api/internal/ingest/{source}``.

    Parameters
    ----------
    payload:
        JSON-serialisable dict, typically ``{"rows": [...], "dry_run": False}``.
    source:
        The ingest slug (e.g. ``"brokerage-csv"``, ``"xlsx-snapshot"``).
    timeout:
        Total request timeout in seconds. Default 30 s.

    Returns
    -------
    dict
        The parsed JSON response body.

    Raises
    ------
    WealthConfigError
        If ``WEALTH_API_BASE`` or ``WEALTH_INTERNAL_KEY`` are missing.
    WealthUnauthorizedError
        On HTTP 401 (wrong or rotated key).
    WealthRateLimitError
        On HTTP 429 (WAF rate limit).
    WealthServerError
        On HTTP 5xx.
    WealthHTTPError
        On any other non-2xx status.
    """
    base_url = os.environ.get("WEALTH_API_BASE", "").rstrip("/")
    if not base_url:
        raise WealthConfigError(
            "WEALTH_API_BASE is not set. "
            "Run: doppler run --project accounting --config dev -- <importer>"
        )

    internal_key = os.environ.get("WEALTH_INTERNAL_KEY", "")
    if not internal_key:
        raise WealthConfigError(
            "WEALTH_INTERNAL_KEY is not set. "
            "Run: doppler run --project accounting --config dev -- <importer>"
        )

    url = f"{base_url}/wealth/api/internal/ingest/{source}"

    # SECURITY: never log the X-Internal-Key value.
    logger.debug("POST %s (source=%s, payload_keys=%s)", url, source, list(payload.keys()))

    headers = {
        "X-Internal-Key": internal_key,
        "Content-Type": "application/json",
    }

    try:
        response = httpx.post(url, json=payload, headers=headers, timeout=timeout)
    except httpx.HTTPError as exc:
        # DNS / TLS / connect / timeout — keep it inside the WealthClientError
        # hierarchy so batch callers don't crash on a transient network fault.
        logger.error(
            "wealth_client: transport error POSTing %s (source=%s): %s",
            url,
            source,
            type(exc).__name__,
        )
        raise WealthTransportError(str(exc)) from exc

    if response.is_success:
        try:
            return response.json()  # type: ignore[no-any-return]
        except ValueError as exc:
            # 2xx with a non-JSON body (e.g. an HTML error page served with a
            # 200 by an intermediary) — surface as a typed protocol error.
            raise WealthProtocolError(
                response.status_code, response.text[:500]
            ) from exc

    status = response.status_code
    body = response.text

    # SECURITY: log only the status, never the key or raw body (may contain
    # internal error details, but the URL itself never carries the key).
    logger.error(
        "wealth_client: POST %s returned %s (source=%s)",
        url,
        status,
        source,
    )

    if status == 401:
        raise WealthUnauthorizedError(status, body)
    if status == 429:
        raise WealthRateLimitError(status, body)
    if status >= 500:
        raise WealthServerError(status, body)
    raise WealthHTTPError(status, body)


__all__ = [
    "WealthClientError",
    "WealthConfigError",
    "WealthHTTPError",
    "WealthProtocolError",
    "WealthRateLimitError",
    "WealthServerError",
    "WealthTransportError",
    "WealthUnauthorizedError",
    "post_to_wealth",
]
