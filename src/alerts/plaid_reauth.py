"""Plaid re-auth-aware failure routing for the daily sync scripts (REQ-FIX-ALR-009).

A Plaid Item stuck in a human-action error state (``ITEM_LOGIN_REQUIRED``,
consent revocation, pending expiration, …) used to hard-fail all three sync
units every day until the Item was re-linked, tripping the generic
unit-failure alert each time (§8 of
``docs/superpowers/plans/2026-08-02-alerting-consolidation.md``). No retry or
code path fixes those states — only a human re-linking at
``https://books.sparkry.ai/admin/connections`` does. This module routes them
accordingly:

- ``route_item_failures`` partitions item-level failures into ``reauth``
  (human action required) vs ``infra`` (everything else — institution_down,
  D1-push failures, unexpected exceptions). Callers exit non-zero only for
  ``infra``.
- Each reauth failure posts ONE sev3 severity-webhook alert per
  ``(item_id, error_code)`` state, carrying the re-connect link. Dedup is a
  sentinel file in ``data/.alerts`` shared by all three sync scripts
  (balance 04:00 → investments 04:20 → transactions 05:00 UTC), so a broken
  Item alerts once total, not three times daily.
- When an Item syncs clean again its sentinels are cleared, so a fresh
  breakage re-alerts.

Exit-0 on reauth-only failures never hides a dead Item: the freshness
sentinel (REQ-SEN-002) independently asserts Item sync recency every day.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from src.balance_alerts.webhook import build_payload_dict, post_payload

logger = logging.getLogger(__name__)

#: Where a human re-links a broken Item (Plaid Link update mode).
RECONNECT_URL = "https://books.sparkry.ai/admin/connections"

#: Plaid error codes that mean "a human must re-link/re-consent this Item".
#: Everything else (INSTITUTION_DOWN, D1_PUSH:*, UNEXPECTED, …) is infra.
REAUTH_ERROR_CODES = frozenset(
    {
        "ITEM_LOGIN_REQUIRED",
        "PENDING_EXPIRATION",
        "PENDING_DISCONNECT",
        "ITEM_LOCKED",
        "USER_SETUP_REQUIRED",
        "ACCESS_NOT_GRANTED",
        "ADDITIONAL_CONSENT_REQUIRED",
    }
)


@dataclass(frozen=True)
class ItemFailure:
    item_id: str
    institution_name: str
    error_code: str | None


@dataclass
class ReauthRouting:
    reauth: list[ItemFailure] = field(default_factory=list)
    infra: list[ItemFailure] = field(default_factory=list)
    alerts_sent: int = 0


def _sentinel_dir() -> Path:
    """Same contract as ``scripts/alert.py``: env override for tests, else the
    travis-owned ``data/.alerts`` (never world-writable /tmp)."""
    override = os.environ.get("ALERT_SENTINEL_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "data" / ".alerts"


def _slug(value: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", str(value or "none"))


def _sentinel_path(sdir: Path, item_id: str, error_code: str | None) -> Path:
    return sdir / f"plaid-reauth-{_slug(item_id)}-{_slug(error_code)}.state"


def is_reauth(error_code: str | None) -> bool:
    return error_code in REAUTH_ERROR_CODES


def _post_reauth_alert(failure: ItemFailure, *, apply: bool) -> bool:
    """POST one sev3 alert for a newly-broken Item. Returns True iff sent."""
    payload = build_payload_dict(
        severity="sev3",
        title=f"[accounting] Plaid re-connect needed: {failure.institution_name}",
        message=(
            f"{failure.institution_name} Plaid connection is in "
            f"{failure.error_code} — a human re-link is required; retries "
            f"cannot fix it.\n"
            f"Fix: {RECONNECT_URL}\n"
            f"This alert fires once per new error state. The daily freshness "
            f"sentinel keeps reporting the Item as stale until re-linked."
        ),
        alert_key=f"plaid-reauth:{failure.item_id}:{failure.error_code}",
        account=failure.institution_name,
    )
    result = post_payload(
        payload, key=f"plaid-reauth:{failure.item_id}", apply=apply
    )
    return result.status == "sent"


def route_item_failures(
    failures: Sequence[ItemFailure],
    ok_item_ids: Iterable[str],
    *,
    apply: bool,
) -> ReauthRouting:
    """Partition failures, alert-once on new reauth states, clear recovered.

    DRY-RUN (``apply=False``) still partitions (so exit codes are testable)
    but never POSTs and never touches sentinel files.
    """
    routing = ReauthRouting()
    for f in failures:
        (routing.reauth if is_reauth(f.error_code) else routing.infra).append(f)

    sdir = _sentinel_dir()
    if apply:
        sdir.mkdir(parents=True, exist_ok=True)
        # Clear sentinels for Items that sync clean again — next breakage re-alerts.
        for item_id in ok_item_ids:
            for stale in sdir.glob(f"plaid-reauth-{_slug(item_id)}-*.state"):
                stale.unlink(missing_ok=True)
                logger.info("plaid reauth recovered: cleared %s", stale.name)

    for f in routing.reauth:
        sentinel = _sentinel_path(sdir, f.item_id, f.error_code)
        if apply and sentinel.exists():
            logger.info(
                "plaid reauth already alerted for %s (%s) — skipping",
                f.institution_name,
                f.error_code,
            )
            continue
        sent = _post_reauth_alert(f, apply=apply)
        if sent:
            # Touch only on confirmed delivery: a failed POST retries on the
            # next daily run instead of being silently swallowed.
            sentinel.touch()
            routing.alerts_sent += 1
        elif apply:
            logger.warning(
                "plaid reauth alert POST failed for %s (%s) — will retry next run",
                f.institution_name,
                f.error_code,
            )
    return routing
