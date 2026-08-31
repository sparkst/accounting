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

Issue #53 (`classify`): every body carries the unit's systemd ``Result=``;
accounting-uptime-check is further split by what happened — Result=timeout
with an ``uptime ok`` probe line → info (host starvation, endpoint up),
timeout with no probe verdict → sev3, ``uptime FAIL`` → sev2 as before.
"""

from __future__ import annotations

import subprocess
import sys

from scripts.alert import _build_body, _hour, _redact, _sentinel_dir
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


#: The 5-min serving-stack probe (scripts/uptime_check.sh). Issue #53: its
#: OnFailure can fire because systemd hit TimeoutStartSec on a starved host
#: while the probe itself returned 200 — that is host starvation, not an
#: outage, and must not page sev2.
PROBE_UNIT = "accounting-uptime-check.service"
_SYSTEMCTL_TIMEOUT_SECONDS = 10
_PROBE_PREFIXES = ("uptime ok:", "uptime FAIL:")


def _severity(unit: str) -> str:
    return "sev2" if unit in SEV2_UNITS else "sev3"


def _systemctl_show(unit: str, prop: str) -> str | None:
    """Best-effort ``systemctl show -p <prop> --value <unit>`` (no privileges
    needed for system units). None on any failure."""
    try:
        result = subprocess.run(  # noqa: S603 — fixed args, unit is systemd-controlled
            ["systemctl", "show", "-p", prop, "--value", unit],
            capture_output=True,
            text=True,
            timeout=_SYSTEMCTL_TIMEOUT_SECONDS,
            check=False,
        )
    except Exception:  # noqa: BLE001 — enrichment only, never blocks the alert
        return None
    value = result.stdout.strip()
    return value or None


def _unit_result(unit: str) -> str | None:
    """systemd's ``Result=`` for the failed unit: ``timeout`` (start timeout
    hit), ``exit-code`` (the process itself failed), ``signal``, ..."""
    return _systemctl_show(unit, "Result")


def _invocation_journal(unit: str) -> str | None:
    """Journal lines of the unit's LAST invocation only (not the previous
    5-min runs, whose ``uptime ok`` would masquerade as this run's result)."""
    invocation = _systemctl_show(unit, "InvocationID")
    if not invocation:
        return None
    try:
        result = subprocess.run(  # noqa: S603 — fixed args, no shell
            [
                "journalctl",
                f"_SYSTEMD_INVOCATION_ID={invocation}",
                "--no-pager",
                "-o",
                "cat",
            ],
            capture_output=True,
            text=True,
            timeout=_SYSTEMCTL_TIMEOUT_SECONDS,
            check=False,
        )
    except Exception:  # noqa: BLE001
        return None
    return _redact(result.stdout.strip()) or None


def _probe_line(unit: str) -> str | None:
    """The last ``uptime ok:`` / ``uptime FAIL:`` line the failed invocation
    logged, or None if the probe never got that far."""
    journal = _invocation_journal(unit)
    if not journal:
        return None
    for line in reversed(journal.splitlines()):
        stripped = line.strip()
        if stripped.startswith(_PROBE_PREFIXES):
            return stripped
    return None


def _is_genuine_failure(result: str | None) -> bool:
    """Whether an OnFailure= trigger reflects a real failure the wrapper should
    page on, given the unit's systemd ``Result=`` at handler-run time.

    Issue #79 (08-27, journal ids 17223, 17227): the OnFailure handler ran, but
    by the time it queried state the 5-min timer had already re-activated the
    probe to a successful run, so ``Result`` read ``success`` — the failing
    invocation had recovered. The handler paged "unit failed" anyway because the
    page/no-page decision never consulted Result. Page only when Result names a
    real failure (``exit-code``/``timeout``/``signal``/``core-dump``/…). A
    positive ``success`` is the one shape that is suppressed; ``None`` (state
    unreadable — no privileges) fails OPEN and still pages, so a genuine outage
    is never silenced by an inability to read Result."""
    return result != "success"


def classify(unit: str, result: str | None) -> tuple[str, str, list[str]]:
    """(severity, title, extra body lines) for the failed unit, given its
    systemd ``Result=`` (fetched once by the caller so the page/no-page guard
    and the body see the same value — Issue #79).

    Every unit gets its ``Result=`` in the body. The probe unit is additionally
    classified by what actually happened:
      * Result=timeout + probe logged ``uptime ok``  → info  (host starvation,
        NOT an outage — the endpoint answered 200);
      * Result=timeout + no probe line               → sev3  (the probe could
        not run inside the start window — starvation, endpoint state unknown);
      * probe logged ``uptime FAIL`` (any Result)     → sev2  (endpoint down).
    """
    severity = _severity(unit)
    title = f"[accounting/hetzner] unit failed: {unit}"
    extra: list[str] = []
    if result:
        extra.append(f"systemd Result: {result}")
    if unit != PROBE_UNIT:
        return severity, title, extra

    probe = _probe_line(unit)
    extra.append(f"Probe result: {probe or '(none — the probe never reached its verdict)'}")
    probe_ok = probe is not None and probe.startswith("uptime ok:")
    probe_fail = probe is not None and probe.startswith("uptime FAIL:")
    if result == "timeout" and probe_ok:
        severity = "info"
        title = (
            "[accounting/hetzner] accounting-uptime-check slow to start "
            "(host starvation) — probe OK"
        )
        extra.append(
            "systemd hit TimeoutStartSec before the unit finished, but the probe "
            "answered 200 — this is host starvation (CPU/memory), NOT a serving-stack "
            "outage. Check load/swap on the box; books.sparkry.ai is up."
        )
    elif result == "timeout" and not probe_fail:
        severity = "sev3"
        title = (
            "[accounting/hetzner] accounting-uptime-check timed out before the "
            "probe completed (host starvation?)"
        )
        extra.append(
            "systemd hit TimeoutStartSec and the probe never logged a verdict — "
            "the box was too starved to run it. Endpoint state UNKNOWN: check "
            "load/swap, then curl -H 'Host: books.sparkry.ai' 127.0.0.1:9000/api/health/ping."
        )
    return severity, title, extra


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
    # Issue #79: derive the page/no-page decision from the unit's ACTUAL
    # failure state, not from the mere fact that OnFailure= fired. If the
    # failing invocation has already recovered (Result=success — the 08-27
    # false-fire shape), there is nothing to page. Read Result once and thread
    # it into classify() so the guard and the body agree.
    unit_result = _unit_result(unit)
    if not _is_genuine_failure(unit_result):
        print(
            f"[alert-webhook] {unit}: systemd Result={unit_result} — OnFailure fired "
            "but the unit is not in a failure state (stale/re-run trigger); no page"
        )
        return 0

    sdir = _sentinel_dir()
    sdir.mkdir(parents=True, exist_ok=True)
    hour = _hour()
    sentinel = sdir / f"alert-webhook-{unit}-{hour}.sent"
    if sentinel.exists():
        print(f"[alert-webhook] already sent for {unit} this hour — skipping")
        return 0

    severity, title, extra = classify(unit, unit_result)
    body = _build_body(unit)
    if extra:
        body = "\n".join([*extra, "", body])
    payload = build_payload_dict(
        severity=severity,
        title=title,
        message=body,
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
