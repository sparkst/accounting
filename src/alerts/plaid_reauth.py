"""Plaid re-auth-aware failure routing for the daily sync scripts (REQ-FIX-ALR-009).

A Plaid Item stuck in a human-action error state (``ITEM_LOGIN_REQUIRED``,
consent revocation, pending expiration, …) used to hard-fail all three sync
units every day until the Item was re-linked, tripping the generic
unit-failure alert each time (§8 of
``docs/superpowers/plans/2026-08-02-alerting-consolidation.md``). No retry or
code path fixes those states — only a human re-linking at
``https://books.sparkry.ai/admin/connections`` does. This module routes them
accordingly:

- ``route_batch`` (the single entry point for all three sync CLIs) partitions
  item-level failures into ``reauth`` (human action required) vs ``infra``
  (everything else — institution_down, D1-push failures, unexpected
  exceptions). Callers exit non-zero for ``infra`` AND for ``post_failed``
  (a re-auth alert we could not deliver — silence is never acceptable).
- Each reauth failure posts ONE sev3 severity-webhook alert per
  ``(item_id, error_code)`` state across ALL sync sources, carrying the
  re-connect link. Sentinel files in ``data/.alerts`` are **per-source**
  (``plaid-reauth-<source>--<item>--<code>.state``) so that recovery is
  judged per product: the balance sync going clean clears only balance-owned
  sentinels and can never wipe the record of an investments-only consent
  error (which would re-alert daily — the exact spam this module removes).
  Posting dedup checks the ``(item, code)`` pair across ALL sources, so a
  whole-Item error like ITEM_LOGIN_REQUIRED still alerts once total, not
  once per sync script.
- When a source posts (or dedup-skips) a NEW code for an Item, its own stale
  other-code sentinels for that Item are dropped, so a code transition
  (PENDING_EXPIRATION → ITEM_LOGIN_REQUIRED) re-alerts and never leaves the
  old state file behind forever.
- When an Item syncs clean again its (per-source) sentinels are cleared, so
  a fresh breakage re-alerts.

Exit-0 on reauth-only failures never hides a dead Item: the freshness
sentinel (REQ-SEN-002) independently asserts Item sync recency every day.

The re-link code set builds on the canonical
``src.adapters.plaid_client.TERMINAL_ERROR_CODES`` — a new terminal code
added there propagates here automatically.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from src.adapters.plaid_client import TERMINAL_ERROR_CODES
from src.balance_alerts.webhook import build_payload_dict, post_payload

logger = logging.getLogger(__name__)

#: Where a human re-links a broken Item (Plaid Link update mode).
RECONNECT_URL = "https://books.sparkry.ai/admin/connections"

#: Plaid error codes that mean "a human must re-link/re-consent this Item".
#: TERMINAL_ERROR_CODES (plaid_client canon: ITEM_LOGIN_REQUIRED,
#: INVALID_CREDENTIALS, ITEM_LOCKED, INVALID_ACCESS_TOKEN, ACCESS_NOT_GRANTED,
#: ITEM_NOT_FOUND) plus the pending/consent states Plaid signals ahead of or
#: alongside a required re-link. Everything else (INSTITUTION_DOWN,
#: D1_PUSH:*, UNEXPECTED, …) is infra.
REAUTH_ERROR_CODES = TERMINAL_ERROR_CODES | frozenset(
    {
        "PENDING_EXPIRATION",
        "PENDING_DISCONNECT",
        "USER_SETUP_REQUIRED",
        "ADDITIONAL_CONSENT_REQUIRED",
        # 2026-08-08 live (BofA, investments): a consent-shaped sibling of
        # ADDITIONAL_CONSENT_REQUIRED. Structural absence is already skipped
        # at the adapter layer; when it reaches routing it means an item with
        # delivered-holdings history regressed — a human re-link, not infra.
        "PRODUCTS_NOT_SUPPORTED",
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
    #: reauth failures whose sev3 POST failed — callers must exit non-zero so
    #: OnFailure pages instead of the breakage passing silently.
    post_failed: list[ItemFailure] = field(default_factory=list)
    alerts_sent: int = 0

    @property
    def exit_failures(self) -> bool:
        """True when the caller must exit non-zero."""
        return bool(self.infra or self.post_failed)


class _ItemResult(Protocol):
    item_id: str
    institution_name: str
    status: str
    error_code: str | None


def _sentinel_dir() -> Path:
    """Same contract as ``scripts/alert.py``: env override for tests, else the
    travis-owned ``data/.alerts`` (never world-writable /tmp)."""
    override = os.environ.get("ALERT_SENTINEL_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "data" / ".alerts"


def _slug(value: str | None) -> str:
    """Alnum+underscore only — structural ``--`` separators stay unambiguous."""
    return re.sub(r"[^A-Za-z0-9_]+", "_", str(value or "none"))


def _sentinel_path(sdir: Path, source: str, item_id: str, code: str | None) -> Path:
    return sdir / f"plaid-reauth-{_slug(source)}--{_slug(item_id)}--{_slug(code)}.state"


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
    source: str,
) -> ReauthRouting:
    """Partition failures, alert-once on new reauth states, clear recovered.

    ``source`` names the calling sync product (``balance`` / ``investments``
    / ``transactions``): sentinels are owned per-source so one product's
    recovery never erases another product's alert state, while POSTING dedup
    spans all sources so a whole-Item error alerts once total.

    DRY-RUN (``apply=False``) still partitions (so exit codes are testable)
    but never POSTs and never touches sentinel files.
    """
    routing = ReauthRouting()
    for f in failures:
        (routing.reauth if is_reauth(f.error_code) else routing.infra).append(f)

    sdir = _sentinel_dir()
    if apply:
        sdir.mkdir(parents=True, exist_ok=True)
        # Recovery: clear only THIS source's sentinels for Items it saw clean.
        for item_id in ok_item_ids:
            for stale in sdir.glob(f"plaid-reauth-{_slug(source)}--{_slug(item_id)}--*.state"):
                stale.unlink(missing_ok=True)
                logger.info("plaid reauth recovered: cleared %s", stale.name)

    for f in routing.reauth:
        own = _sentinel_path(sdir, source, f.item_id, f.error_code)
        if apply:
            # Code transition: drop this source's stale other-code sentinels
            # for the Item so the old state never lingers.
            for stale in sdir.glob(
                f"plaid-reauth-{_slug(source)}--{_slug(f.item_id)}--*.state"
            ):
                if stale != own:
                    stale.unlink(missing_ok=True)
            # Cross-source posting dedup: if ANY source already alerted this
            # (item, code) state, record our own view and skip the POST.
            already = list(
                sdir.glob(f"plaid-reauth-*--{_slug(f.item_id)}--{_slug(f.error_code)}.state")
            )
            if already:
                own.touch()
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
            own.touch()
            routing.alerts_sent += 1
        elif apply:
            routing.post_failed.append(f)
            logger.error(
                "plaid reauth alert POST FAILED for %s (%s) — exiting non-zero "
                "so OnFailure pages; will retry next run",
                f.institution_name,
                f.error_code,
            )
    return routing


def route_batch(
    items: Iterable[_ItemResult],
    *,
    apply: bool,
    source: str,
    clean_statuses: Sequence[str] = ("ok",),
    log: logging.Logger | None = None,
) -> ReauthRouting:
    """The single per-CLI entry point: build failures/ok lists from adapter
    item results, route them, and emit the standard operator log lines."""
    log = log or logger
    items = list(items)
    routing = route_item_failures(
        [
            ItemFailure(r.item_id, r.institution_name, r.error_code)
            for r in items
            if r.status not in clean_statuses
        ],
        [r.item_id for r in items if r.status == "ok"],
        apply=apply,
        source=source,
    )
    if routing.reauth:
        log.warning(
            "re-connect needed (sev3 webhook, not a unit failure): %s",
            ", ".join(f"{f.institution_name}({f.error_code})" for f in routing.reauth),
        )
    if routing.post_failed:
        log.error(
            "re-auth webhook POST failed for: %s — unit will exit non-zero",
            ", ".join(f.institution_name for f in routing.post_failed),
        )
    return routing
