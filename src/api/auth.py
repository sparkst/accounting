"""API key authentication dependency.

Header-only. The query-parameter form was removed because uvicorn's access
log records full URLs to a persistent file, which would write the secret to
disk in plaintext.
"""

from __future__ import annotations

import hmac
import os

from fastapi import HTTPException, Security, status
from fastapi.security.api_key import APIKeyHeader

_API_KEY_HEADER = APIKeyHeader(name="X-Api-Key", auto_error=False)


def require_api_key(
    header_key: str | None = Security(_API_KEY_HEADER),
) -> None:
    """FastAPI dependency that enforces API key auth when ``API_KEY`` is set.

    When ``API_KEY`` is unset, auth is disabled — the API binds to 127.0.0.1
    only, but be aware that Caddy's reverse proxy fronts these endpoints over
    Tailscale. Set ``API_KEY`` in any non-trivial environment.
    """
    expected = os.environ.get("API_KEY")
    if not expected:
        return

    if not header_key or not hmac.compare_digest(header_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )


def require_api_or_ingest_key(
    header_key: str | None = Security(_API_KEY_HEADER),
) -> None:
    """Accept the browser API_KEY OR the machine INGEST_API_KEY.

    Used for /api/ingest/run, which is called BOTH by the dashboard 'Sync Now'
    button (browser API_KEY) and by the n8n automation (INGEST_API_KEY). It is a
    trigger with no injectable body, so accepting either credential is safe; CF
    Access gates all callers at the edge. Presence/strength/distinctness of the
    keys in production is enforced by the lifespan() boot assertion.
    """
    api_key = os.environ.get("API_KEY")
    ingest_key = os.environ.get("INGEST_API_KEY")
    if not api_key and not ingest_key:
        return  # dev/no-auth mode (matches require_api_key behavior)
    if header_key:
        if api_key and hmac.compare_digest(header_key, api_key):
            return
        if ingest_key and hmac.compare_digest(header_key, ingest_key):
            return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing API key",
        headers={"WWW-Authenticate": "ApiKey"},
    )
