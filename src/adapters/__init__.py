"""Adapter registry.

REQ-ID: ADAPTER-REG-001  Lazy factory maps Source enum values to adapter classes.
REQ-ID: ADAPTER-REG-002  Missing API keys return None with a warning, never raise.
REQ-ID: ADAPTER-REG-003  Adapters are constructed only when get_adapter() is called.

Usage::

    from src.adapters import get_adapter
    from src.models.enums import Source

    adapter = get_adapter(Source.GMAIL_N8N)
    if adapter is None:
        # missing keys — source was skipped, warning already logged
        ...
"""

from __future__ import annotations

import logging
import os

from src.adapters.base import BaseAdapter
from src.models.enums import Source

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Required environment variables per source
# ---------------------------------------------------------------------------

_REQUIRED_ENV: dict[Source, list[str]] = {
    Source.STRIPE: ["STRIPE_API_KEY"],
    Source.SHOPIFY: ["SHOPIFY_API_KEY", "SHOPIFY_STORE_URL"],
    # File-based adapters need no API keys
    Source.GMAIL_N8N: [],
    Source.DEDUCTION_EMAIL: [],
    # Upload-only adapters — not part of the automated ingest loop
    Source.BROKERAGE_CSV: [],
    Source.BANK_CSV: [],
    Source.PHOTO_RECEIPT: [],
}


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def get_adapter(source: Source) -> BaseAdapter | None:
    """Return a freshly constructed adapter for *source*, or None if unavailable.

    Construction is deferred to call time (lazy) so imports never raise even
    when optional environment variables are absent.

    Missing required environment variables produce a WARNING log and return
    ``None`` rather than raising, so the caller (e.g. the ingest endpoint) can
    skip that source gracefully and include a warning in the response.

    Args:
        source: A :class:`~src.models.enums.Source` enum value.

    Returns:
        A :class:`~src.adapters.base.BaseAdapter` instance, or ``None`` if the
        source is not registered or its required API keys are missing.
    """
    # Check required env vars first so we never even import the adapter module
    # unnecessarily when keys are absent.
    required = _REQUIRED_ENV.get(source, [])
    missing = [var for var in required if not os.environ.get(var)]
    if missing:
        logger.warning(
            "Skipping adapter %r — missing environment variables: %s",
            source.value,
            ", ".join(missing),
        )
        return None

    try:
        if source == Source.GMAIL_N8N:
            from src.adapters.gmail_n8n import GmailN8nAdapter
            return GmailN8nAdapter()

        if source == Source.DEDUCTION_EMAIL:
            from src.adapters.deduction_email import DeductionEmailAdapter
            return DeductionEmailAdapter()

        if source == Source.STRIPE:
            from src.adapters.stripe_adapter import StripeAdapter
            return StripeAdapter()

        if source == Source.SHOPIFY:
            from src.adapters.shopify_adapter import ShopifyAdapter
            return ShopifyAdapter()

        # BROKERAGE_CSV and BANK_CSV are upload-driven — not part of the
        # automated ingest loop.  PHOTO_RECEIPT is not yet implemented.
        logger.warning(
            "Source %r is not registered in the automated ingest loop.",
            source.value,
        )
        return None

    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Failed to construct adapter for %r: %s",
            source.value,
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# Registered sources (used by the ingest endpoint for "run all")
# ---------------------------------------------------------------------------

#: Sources included in a full (no source filter) ingest pass, in run order.
INGEST_SOURCES: list[Source] = [
    Source.GMAIL_N8N,
    Source.DEDUCTION_EMAIL,
    Source.STRIPE,
    Source.SHOPIFY,
]
