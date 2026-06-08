"""n8n webhook client for EA alerts.

DRY-RUN by default: `post_alert(alert, apply=False)` builds the payload but makes
no network call.  Only `apply=True` POSTs to the configured n8n webhook.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx

from src.alerts.rules import Alert

logger = logging.getLogger(__name__)

DEFAULT_FROM = "Travis@sparkry.com"
DEFAULT_TO = "ea-alerts@sparkry.com"
ALLOWED_TO = {"ea-alerts@sparkry.com"}  # recipient allowlist — defense-in-depth
ALLOWED_FROM = {"Travis@sparkry.com"}  # sender allowlist — parity with recipient guard


@dataclass(frozen=True)
class WebhookResult:
    status: str  # "sent" | "failed" | "dry_run"
    http_status: int | None
    error: str | None


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


def post_alert(alert: Alert, *, apply: bool, timeout: float = 10.0) -> WebhookResult:
    from_email = os.environ.get("ALERT_FROM_EMAIL", DEFAULT_FROM)
    to_email = os.environ.get("ALERT_TO_EMAIL", DEFAULT_TO)
    if to_email not in ALLOWED_TO:
        return WebhookResult("failed", None, "recipient not allowlisted")
    if from_email not in ALLOWED_FROM:
        return WebhookResult("failed", None, "sender not allowlisted")
    payload = build_payload(alert, from_email, to_email)

    if not apply:
        logger.debug("DRY-RUN alert %s (%s)", alert.alert_key, alert.subject)
        return WebhookResult("dry_run", None, None)

    url = os.environ.get("N8N_ALERTS_WEBHOOK_URL", "")
    secret = os.environ.get("N8N_ALERTS_WEBHOOK_SECRET", "")
    if not url or not secret:
        logger.warning("post_alert: webhook not configured — N8N_ALERTS_WEBHOOK_URL/SECRET missing; --apply runs will fail")
        return WebhookResult("failed", None, "webhook not configured")
    if not url.startswith("https://"):
        return WebhookResult("failed", None, "webhook url must be https")

    headers = {"X-Webhook-Secret": secret, "Content-Type": "application/json"}
    # SECURITY: never log the secret value.
    logger.debug("POST n8n alerts webhook key=%s", alert.alert_key)
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=timeout)
    except httpx.HTTPError as exc:
        logger.debug("n8n webhook network error key=%s: %s", alert.alert_key, exc)
        return WebhookResult("failed", None, "network error")

    if resp.is_success:
        return WebhookResult("sent", resp.status_code, None)
    logger.debug("n8n webhook non-2xx key=%s status=%s", alert.alert_key, resp.status_code)
    return WebhookResult("failed", resp.status_code, f"http {resp.status_code}")
