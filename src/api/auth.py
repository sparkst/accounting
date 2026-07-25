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
) -> str:
    """Accept the browser API_KEY OR the machine INGEST_API_KEY.

    Returns which credential matched — ``"api"``, ``"ingest"``, or ``"none"``
    (dev/no-auth mode, when neither key is configured) — so that callers which
    need to know can distinguish them. Used for two shapes of route:

    - Write-only triggers with no readable body (e.g. ``POST /api/ingest/run``,
      called BOTH by the dashboard 'Sync Now' button and by n8n): accepting
      either credential is safe there regardless of the return value, because
      there is nothing sensitive to read back and CF Access gates all callers
      at the edge.
    - Read routes with a caller-selectable scope (e.g.
      ``GET /api/ingest/wbr/ledger-summary``): the OLD "trigger-only, no
      readable body" rationale does NOT apply — those routes MUST consume the
      returned credential and scope their own results by it. The machine
      INGEST_API_KEY is the weaker credential of the two (it lives in n8n) and
      must never be allowed to read more than its automation needs;
      ``wbr_ledger.py`` is the reference implementation (ingest key forced to
      ``entity=personal``, 403 otherwise).

    Presence/strength/distinctness of the keys in production is enforced by
    the lifespan() boot assertion.
    """
    api_key = os.environ.get("API_KEY")
    ingest_key = os.environ.get("INGEST_API_KEY")
    if not api_key and not ingest_key:
        return "none"  # dev/no-auth mode (matches require_api_key behavior)
    if header_key:
        if api_key and hmac.compare_digest(header_key, api_key):
            return "api"
        if ingest_key and hmac.compare_digest(header_key, ingest_key):
            return "ingest"
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing API key",
        headers={"WWW-Authenticate": "ApiKey"},
    )
