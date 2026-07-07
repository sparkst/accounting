"""n8n webhook client for balance alerts (REQ-BAL-007).

POSTs a severity-tagged payload to the n8n `UT-Send Alert Message` stack, which
owns Telegram/Gmail routing by `type` (info | sev3 | sev2). No direct email is
sent from here.

DRY-RUN by default: `apply=False` builds the payload but makes no network call.
The single POST path lives in `post_payload` so the HTTPS guard, secret header,
timeout, and error-string discipline exist in exactly one place (reused by the
daily pulse in `digest.py`). Reuses `WebhookResult` from the EA-alerts module.
"""

from __future__ import annotations

import logging
import os

import httpx

from src.alerts.retry import post_with_retry
from src.alerts.webhook import WebhookResult
from src.balance_alerts.rules import SOURCE, BalanceAlert

logger = logging.getLogger(__name__)

URL_ENV = "N8N_SEVERITY_WEBHOOK_URL"
SECRET_ENV = "N8N_SEVERITY_WEBHOOK_SECRET"


def post_payload(
    payload: dict[str, str | None], *, key: str, apply: bool, timeout: float = 10.0
) -> WebhookResult:
    """Build-and-POST the single severity-webhook path. DRY-RUN when ``apply`` is False.

    SECURITY: the secret travels only in the ``X-Webhook-Secret`` header and is
    never logged; the error string is static (no exception interpolation) so no
    URL/credential fragment is ever persisted to ``alert_dispatch.error_detail``.
    """
    if not apply:
        logger.debug("DRY-RUN severity webhook key=%s", key)
        return WebhookResult("dry_run", None, None)

    url = os.environ.get(URL_ENV, "")
    secret = os.environ.get(SECRET_ENV, "")
    if not url or not secret:
        logger.warning(
            "post_payload: webhook not configured — %s/%s missing", URL_ENV, SECRET_ENV
        )
        return WebhookResult("failed", None, "webhook not configured")
    if not url.startswith("https://"):
        return WebhookResult("failed", None, "webhook url must be https")

    headers = {"X-Webhook-Secret": secret, "Content-Type": "application/json"}
    logger.debug("POST n8n severity webhook key=%s type=%s", key, payload.get("type"))
    try:
        resp = post_with_retry(
            lambda: httpx.post(url, json=payload, headers=headers, timeout=timeout)
        )
    except httpx.HTTPError as exc:
        # Static message — never interpolate `exc` (it can carry the URL).
        logger.debug("n8n webhook network error key=%s: %s", key, exc)
        return WebhookResult("failed", None, "network error")
    if resp.status_code // 100 != 2:
        logger.debug("n8n webhook non-2xx key=%s status=%s", key, resp.status_code)
        return WebhookResult("failed", resp.status_code, f"non-2xx: {resp.status_code}")
    return WebhookResult("sent", resp.status_code, None)


def build_payload_dict(
    *,
    severity: str,
    title: str,
    message: str,
    alert_key: str,
    account: str | None = None,
    balance: str | None = None,
    level: str | None = None,
    baseline_gap_days: str | None = None,
) -> dict[str, str | None]:
    """The n8n `UT-Send Alert Message` contract. `type` drives channel routing."""
    return {
        "type": severity,  # info | sev3 | sev2
        "title": title,
        "message": message,
        "source": SOURCE,
        "account": account,
        "balance": balance,
        "level": level,
        "alert_key": alert_key,
        "baseline_gap_days": baseline_gap_days,
    }


def build_payload(alert: BalanceAlert) -> dict[str, str | None]:
    """Payload for one fired BalanceAlert.

    REQ-FIX-PLD-003: `baseline_gap_days` always rides along in the payload
    (1 = normal prior-calendar-day baseline); the message note is appended
    only when it's > 1 (see rules.py `_gap_note`).
    """
    return build_payload_dict(
        severity=alert.severity,
        title=alert.title,
        message=alert.message,
        alert_key=alert.alert_key,
        account=alert.account_name,
        balance=alert.new_balance,
        level=alert.level,
        baseline_gap_days=str(alert.baseline_gap_days),
    )


def post_balance_alert(
    alert: BalanceAlert, *, apply: bool, timeout: float = 10.0
) -> WebhookResult:
    """Build the payload and (when apply) POST it to the n8n severity webhook."""
    return post_payload(
        build_payload(alert), key=alert.alert_key, apply=apply, timeout=timeout
    )
