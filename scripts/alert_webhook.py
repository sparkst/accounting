"""systemd OnFailure handler → n8n severity webhook (Telegram, severity-routed).

Alerting-consolidation (docs/superpowers/plans/2026-08-02-alerting-consolidation.md):
replaces scripts/alert.py's direct Resend email with a POST to the
`WH-Severity / Send Alert` webhook (N8N_SEVERITY_WEBHOOK_URL/SECRET — the same
channel the balance alerts and the freshness sentinel already use), so every
accounting alert rides one severity-routed pipe.

Contract is identical to scripts/alert.py (REQ-HM-014):
  * hourly per-unit dedup via a travis-owned sentinel dir (data/.alerts) —
    the sentinel namespace is `alert-webhook-…` (distinct from the email
    path's `alert-…`) so each channel keeps its own hourly budget during a
    transition window where both handlers are wired;
  * exits non-zero ONLY on a real send failure (systemd surfaces it — but the
    unit template has NO OnFailure= of its own: no alert recursion);
  * body enrichment (journal tail + failed-ledger digest) and secret
    redaction are reused from scripts/alert.py (`_build_body` → `_redact`).

Severity mapping: serving-stack units (api/caddy/cloudflared/dashboard/
uptime-check — books.sparkry.ai is down or unprobeable) → sev2; every other
unit (batch timers: syncs, backups, reports) → sev3. The webhook's own
routing turns sev2/sev3 into the right Telegram channel; unknown types
downgrade to info upstream, never dropped.
"""

from __future__ import annotations

import sys

from scripts.alert import _build_body, _hour, _sentinel_dir
from src.balance_alerts.webhook import build_payload_dict, post_payload

#: Units whose failure means the public serving path (books.sparkry.ai) is
#: down or unprobeable — paged as sev2. Everything else is a batch job → sev3.
SEV2_UNITS = frozenset(
    {
        "accounting-api.service",
        "accounting-dashboard.service",
        "caddy.service",
        "cloudflared.service",
        "accounting-uptime-check.service",
    }
)


def _severity(unit: str) -> str:
    return "sev2" if unit in SEV2_UNITS else "sev3"


def _email_fallback(unit: str) -> int:
    """n8n-independent last resort: if the severity webhook cannot deliver,
    fall back to the legacy Resend email path (scripts/alert.py, its own
    `alert-*` hourly dedup namespace). Without this, an n8n outage would
    silence ALL unit-failure alerting — the webhook template deliberately has
    no OnFailure= of its own, so nothing observes our non-zero exit."""
    try:
        from scripts.alert import send_alert as email_send

        return email_send(unit)
    except Exception as exc:  # noqa: BLE001 — fallback must never raise
        print(
            f"[alert-webhook] email fallback raised for {unit}: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 1


def send_alert(unit: str) -> int:
    sdir = _sentinel_dir()
    sdir.mkdir(parents=True, exist_ok=True)
    hour = _hour()
    sentinel = sdir / f"alert-webhook-{unit}-{hour}.sent"
    if sentinel.exists():
        print(f"[alert-webhook] already sent for {unit} this hour — skipping")
        return 0

    payload = build_payload_dict(
        severity=_severity(unit),
        title=f"[accounting/hetzner] unit failed: {unit}",
        message=_build_body(unit),
        alert_key=f"unit:{unit}:{hour}",
    )
    try:
        result = post_payload(payload, key=f"unit:{unit}", apply=True)
    except Exception as exc:  # noqa: BLE001 — fall back, then surface to systemd
        print(f"[alert-webhook] send raised for {unit}: {type(exc).__name__}", file=sys.stderr)
        return _email_fallback(unit)
    if result.status != "sent":
        # post_payload's error strings are static (never carry URL/secret).
        print(
            f"[alert-webhook] send failed for {unit}: {result.error} — "
            f"falling back to Resend email",
            file=sys.stderr,
        )
        return _email_fallback(unit)
    sentinel.touch()
    print(f"[alert-webhook] sent for {unit} (type={payload['type']})")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: alert_webhook.py <unit-name>", file=sys.stderr)
        return 2
    return send_alert(argv[0])


if __name__ == "__main__":
    sys.exit(main())
