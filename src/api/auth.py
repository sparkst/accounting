"""API key authentication dependency.

Usage:
    Apply ``Depends(require_api_key)`` to individual routes, or pass it as a
    global dependency to FastAPI() so every route is protected automatically.

Behaviour:
    - If the ``API_KEY`` environment variable is not set, auth is DISABLED and
      all requests pass through.  This preserves the ergonomic local-dev
      experience where the server is bound to 127.0.0.1 anyway.
    - If ``API_KEY`` is set, every request must supply it via the
      ``X-Api-Key`` header OR the ``api_key`` query parameter.
    - Requests that fail authentication receive a 401 response.

The health endpoint is always exempt so monitoring tools can reach it without
credentials.
"""

from __future__ import annotations

import hmac
import os

from fastapi import Depends, HTTPException, Security, status
from fastapi.security.api_key import APIKeyHeader, APIKeyQuery

_API_KEY_HEADER = APIKeyHeader(name="X-Api-Key", auto_error=False)
_API_KEY_QUERY = APIKeyQuery(name="api_key", auto_error=False)


def require_api_key(
    header_key: str | None = Security(_API_KEY_HEADER),
    query_key: str | None = Security(_API_KEY_QUERY),
) -> None:
    """FastAPI dependency that enforces API key auth when ``API_KEY`` is set.

    Accepts the key from the ``X-Api-Key`` request header or the ``api_key``
    query parameter (header takes precedence).

    Raises:
        HTTPException 401 — if ``API_KEY`` is configured and the request does
            not supply a matching key.
    """
    expected = os.environ.get("API_KEY")
    if not expected:
        # Auth disabled — local dev convenience.
        return

    provided = header_key or query_key
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
