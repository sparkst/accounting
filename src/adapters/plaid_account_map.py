"""Plaid consolidation — box-side push of the D1 account↔Plaid mapping.

Fixes P0-001: after cutover, the box owns Link/consent, so it is the only
actor that ever learns a NEW ``plaid_account_id`` exists (at ``/exchange``
time for a wealth-scope Item). Without this push, a freshly re-linked Item's
accounts have no ``account.plaid_account_id`` row in D1 to resolve against,
and A1/A2 per-row skip every balance/holding for that Item forever.

Called from ``exchange_public_token`` in ``src/api/routes/plaid.py`` for
wealth-scope Items only — register-scope Items keep using the local
``/map-accounts`` UI flow (B5), which has nothing to do with D1.

Best-effort: a push failure here does NOT fail the Plaid Link flow itself
(the Item is already connected; the mapping can be repaired by re-running
this push, e.g. on the next relink, or manually). The caller surfaces the
outcome so the operator isn't left silently guessing.
"""

from __future__ import annotations

import logging
from typing import Any

from src.adapters._shared.wealth_client import WealthClientError, post_to_wealth

logger = logging.getLogger(__name__)

#: Ingest slug — POSTs land at WEALTH_API_BASE/wealth/api/internal/ingest/plaid-account-map.
WEALTH_ACCOUNT_MAP_INGEST_SOURCE = "plaid-account-map"


def build_account_map_payload(
    accounts: list[dict[str, Any]], *, institution_name: str
) -> dict[str, Any]:
    """Build the mapping-endpoint POST body from ``/accounts/get``-shaped dicts.

    ``accounts`` is the same JSON-friendly list ``_enumerate_accounts_for_response``
    already builds for ``ExchangeResponse.accounts`` — reused here rather than
    re-fetching from Plaid.
    """
    mappings: list[dict[str, Any]] = []
    for acct in accounts:
        account_id = acct.get("account_id")
        if not account_id:
            continue
        balances = acct.get("balances") or {}
        mappings.append(
            {
                "plaid_account_id": account_id,
                "institution_name": institution_name,
                "account_name": acct.get("name") or acct.get("official_name"),
                "mask": acct.get("mask"),
                "plaid_account_type": str(acct.get("type")) if acct.get("type") else None,
                "plaid_account_subtype": (
                    str(acct.get("subtype")) if acct.get("subtype") else None
                ),
                # unused today but harmless; keeps the payload shape stable if the
                # endpoint later branches by currency.
                "iso_currency_code": balances.get("iso_currency_code"),
            }
        )
    return {"mappings": mappings}


def push_account_map(
    accounts: list[dict[str, Any]],
    *,
    institution_name: str,
    post: Any = post_to_wealth,
) -> dict[str, Any] | None:
    """POST the account map for one Item's accounts. Returns the parsed
    response, or ``None`` on failure (logged, never raised) — best-effort.
    """
    payload = build_account_map_payload(accounts, institution_name=institution_name)
    if not payload["mappings"]:
        return None
    try:
        result: dict[str, Any] = post(payload, WEALTH_ACCOUNT_MAP_INGEST_SOURCE)
        return result
    except WealthClientError as exc:
        logger.error(
            "plaid account-map D1 push failed for %s: %s",
            institution_name,
            exc,
        )
        return None
