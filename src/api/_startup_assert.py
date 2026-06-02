"""Production boot assertion (§7 Hetzner migration).

Refuses to start in production unless BOTH API_KEY and INGEST_API_KEY are set,
strong (>=32 chars), and DISTINCT. The equality check is a permanent runtime
invariant (not a one-time deploy check) so a future Doppler rotation that
collapses the two keys cannot silently re-merge the dashboard and ingest keys.
"""
from __future__ import annotations

import hmac
import os

_MIN_LEN = 32


def assert_production_secrets() -> None:
    if os.environ.get("PLAID_ENV") != "production":
        return

    api_key = os.environ.get("API_KEY") or ""
    ingest_key = os.environ.get("INGEST_API_KEY") or ""

    for name, value in (("API_KEY", api_key), ("INGEST_API_KEY", ingest_key)):
        if not value:
            raise RuntimeError(f"{name} must be set in production (got empty)")
        if len(value) < _MIN_LEN:
            raise RuntimeError(f"{name} must be >= {_MIN_LEN} chars in production")

    if hmac.compare_digest(api_key, ingest_key):
        raise RuntimeError("API_KEY and INGEST_API_KEY must differ (must differ)")
