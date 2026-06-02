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


def require_ingest_api_key(
    header_key: str | None = Security(_API_KEY_HEADER),
) -> None:
    """Enforce INGEST_API_KEY (machine-to-machine) for /api/ingest/* ONLY.

    Checks INGEST_API_KEY exclusively — it does NOT also accept API_KEY, so a
    dashboard user who extracts the browser-baked VITE_API_KEY cannot drive
    ingest. Non-empty/strength is guaranteed by the lifespan() boot assertion;
    this dependency just compares.
    """
    expected = os.environ.get("INGEST_API_KEY")
    if not expected:
        return

    if not header_key or not hmac.compare_digest(header_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
