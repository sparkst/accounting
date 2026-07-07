"""n8n webhook client for EA alerts.

DRY-RUN by default: `post_alert(alert, apply=False)` builds the payload but makes
no network call.  Only `apply=True` POSTs to the configured n8n webhook.

REQ-FIX-ALR-001: the single `httpx.post` call routes through
`src.alerts.retry.post_with_retry` (3 attempts, exponential backoff + jitter on
connect error/timeout/5xx; 4xx returns immediately).

REQ-FIX-ALR-003: recipient/sender allowlists are env-configured
(`ALERT_ALLOWED_TO` / `ALERT_ALLOWED_FROM`, comma-separated), each defaulting
to the current hardcoded literal. Parsed at call time (not import time) so
tests and Doppler both work.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx

from src.alerts.retry import post_with_retry
from src.alerts.rules import Alert

logger = logging.getLogger(__name__)

DEFAULT_FROM = "Travis@sparkry.com"
DEFAULT_TO = "ea-alerts@sparkry.com"
ALLOWED_TO_ENV = "ALERT_ALLOWED_TO"
ALLOWED_FROM_ENV = "ALERT_ALLOWED_FROM"


@dataclass(frozen=True)
class WebhookResult:
    status: str  # "sent" | "failed" | "dry_run"
    http_status: int | None
    error: str | None


def resolve_from_email() -> str:
    return os.environ.get("ALERT_FROM_EMAIL", DEFAULT_FROM)


def resolve_to_email() -> str:
    return os.environ.get("ALERT_TO_EMAIL", DEFAULT_TO)


def _allowed_to() -> set[str]:
    # Deliberately independent of ALERT_TO_EMAIL: the allowlist is
    # defense-in-depth against a tampered recipient env value. A mismatch is
    # a LOUD failure (failed ledger row + OnFailure email naming both vars),
    # never a silent mute.
    raw = os.environ.get(ALLOWED_TO_ENV, DEFAULT_TO)
    return {e.strip() for e in raw.split(",") if e.strip()}


def _allowed_from() -> set[str]:
    raw = os.environ.get(ALLOWED_FROM_ENV, DEFAULT_FROM)
    return {e.strip() for e in raw.split(",") if e.strip()}


def build_payload(alert: Alert, from_email: str, to_email: str) -> dict[str, str | None]:
    return {
        "from": from_email,
        "to": to_email,
        "subject": alert.subject,
        "body_text": alert.body_text,
        "body_html": alert.body_html,
        "alert_type": alert.alert_type,
        "entity": alert.entity,
        "due_date": alert.due_date,
        "action_url": alert.action_url,
        "alert_key": alert.alert_key,
        "occurrence_date": alert.occurrence_date,
    }


def post_raw_payload(
    payload: dict[str, str | None], *, apply: bool, timeout: float = 10.0
) -> WebhookResult:
    """POST an already-built payload dict, unconditionally re-checking the
    allowlists.

    Used by both `post_alert` (fresh alert) and the failed-row sweep
    (REQ-FIX-ALR-002), which replays a stored `payload_json` byte-for-byte —
    keeping the URL/secret/retry logic in exactly one place.
    """
    to_email = payload.get("to")
    from_email = payload.get("from")
    if to_email not in _allowed_to():
        return WebhookResult(
            "failed",
            None,
            "recipient not allowlisted (ALERT_TO_EMAIL must appear in ALERT_ALLOWED_TO)",
        )
    if from_email not in _allowed_from():
        return WebhookResult(
            "failed",
            None,
            "sender not allowlisted (ALERT_FROM_EMAIL must appear in ALERT_ALLOWED_FROM)",
        )

    if not apply:
        logger.debug("DRY-RUN alert %s", payload.get("alert_key"))
        return WebhookResult("dry_run", None, None)

    url = os.environ.get("N8N_ALERTS_WEBHOOK_URL", "")
    secret = os.environ.get("N8N_ALERTS_WEBHOOK_SECRET", "")
    if not url or not secret:
        logger.warning(
            "post_alert: webhook not configured — N8N_ALERTS_WEBHOOK_URL/SECRET missing; --apply runs will fail"
        )
        return WebhookResult("failed", None, "webhook not configured")
    if not url.startswith("https://"):
        return WebhookResult("failed", None, "webhook url must be https")

    headers = {"X-Webhook-Secret": secret, "Content-Type": "application/json"}
    # SECURITY: never log the secret value.
    logger.debug("POST n8n alerts webhook key=%s", payload.get("alert_key"))
    try:
        resp = post_with_retry(
            lambda: httpx.post(url, json=payload, headers=headers, timeout=timeout)
        )
    except httpx.HTTPError as exc:
        # Static message — never interpolate `exc` (it can carry the URL).
        logger.debug(
            "n8n webhook network error key=%s: %s",
            payload.get("alert_key"),
            type(exc).__name__,
        )
        return WebhookResult("failed", None, "network error")

    if resp.is_success:
        return WebhookResult("sent", resp.status_code, None)
    logger.debug(
        "n8n webhook non-2xx key=%s status=%s", payload.get("alert_key"), resp.status_code
    )
    return WebhookResult("failed", resp.status_code, f"http {resp.status_code}")


def post_alert(alert: Alert, *, apply: bool, timeout: float = 10.0) -> WebhookResult:
    from_email = resolve_from_email()
    to_email = resolve_to_email()
    payload = build_payload(alert, from_email, to_email)
    return post_raw_payload(payload, apply=apply, timeout=timeout)
