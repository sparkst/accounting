"""systemd OnFailure handler → one Resend email naming the failed unit.

REQ-HM-014. Hourly per-unit dedup via a travis-owned sentinel dir (data/.alerts,
not world-writable /tmp); exits non-zero only on a real send failure. This unit
has NO OnFailure= itself (breaks alert recursion).

REQ-FIX-ALR-006: the email body is enriched with (a) the failing unit's last
~15 journal lines (best-effort — a journalctl failure never blocks the basic
alert email, exit code unchanged) and (b) for dispatcher units
(accounting-balance-alerts, accounting-ea-alerts) the subjects of
`alert_dispatch` rows with status='failed' from the last 2 days (read-only
SELECT — this script never writes to the accounting DB).

Ops prerequisite (deploy step, not yet applied): `travis` must join the
`systemd-journal` group on the box (`usermod -aG systemd-journal travis`) for
`journalctl -u <unit>` to read other units' logs without sudo — otherwise (a)
above silently degrades to "no journal tail" (still a valid, sent email).

Security invariant (this forwards a unit's raw journal output to Resend +
Gmail, a broader exposure surface than the box journal itself): the journal
tail is passed through `_redact()` before it ever enters the email body, which
masks (i) verbatim values of known-secret env vars present in *this* process's
environment and (ii) `key=`/`token=`/`secret=`/`password=`-style query-string
params. That is best-effort, not a guarantee — it cannot mask a secret this
process doesn't itself hold (e.g. a value baked into another unit's own env).
The safety of this feature ultimately also rests on: no unit under
`OnFailure=accounting-alert@%n` may log a secret value to its own journal, and
none may run with a locals-printing traceback formatter (plain Python
tracebacks do not print locals by default — keep it that way).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

_TO = "sparkst@gmail.com"
_FROM = "alerts@sparkry.ai"

_JOURNAL_LINES = 15
_JOURNAL_TIMEOUT_SECONDS = 10

# Units whose OnFailure= body also gets the recent failed-alert-ledger digest.
_DISPATCHER_UNITS = frozenset(
    {"accounting-balance-alerts.service", "accounting-ea-alerts.service"}
)

# REQ-FIX-ALR-006 hardening (P3-001): env vars whose verbatim values must
# never leak into the journal tail forwarded to the OnFailure alert email.
# Best-effort — masks whichever of these this process actually has set.
_SECRET_ENV_VARS = (
    "RESEND_API_KEY",
    "STRIPE_API_KEY",
    "STRIPE_RESTRICTED_KEY",
    "SHOPIFY_API_KEY",
    "N8N_WEBHOOK_SECRET",
    "N8N_ALERTS_WEBHOOK_URL",
    "N8N_ALERTS_WEBHOOK_SECRET",
    "N8N_SEVERITY_WEBHOOK_URL",
    "N8N_SEVERITY_WEBHOOK_SECRET",
    "API_KEY",
    "INGEST_API_KEY",
    "PLAID_CLIENT_ID",
    "PLAID_SANDBOX_SECRET",
    "PLAID_PRODUCTION_SECRET",
    "PLAID_FERNET_KEY",
    "PLAID_TOKEN_ENC_KEY",
    "WEALTH_INTERNAL_KEY",
)

# Common key=value secret patterns in URLs/log lines (webhook query strings,
# "Authorization: <scheme> <token>", etc.) — masked regardless of whether the
# value matches a known env var, since a rotated/derived secret wouldn't.
_QUERY_SECRET_RE = re.compile(
    r"(?i)\b([\w-]*(?:key|token|secret|password))\s*[=:]\s*(\S+)"
)
# "Authorization: Bearer <token>" / "Authorization: Basic <creds>" — the
# secret is the token *after* the auth scheme, not the scheme word itself.
_AUTH_HEADER_RE = re.compile(r"(?i)\b(authorization\s*:\s*\S+\s+)(\S+)")


def _redact(text: str) -> str:
    """Best-effort scrub of secret values before the journal tail enters the
    alert email body (REQ-FIX-ALR-006 / P3-001). Masks (a) verbatim values of
    `_SECRET_ENV_VARS` present in this process's own environment and (b)
    key=/token=/secret=/password=/Authorization-header-style parameters. Not
    a substitute for the invariant documented in the module docstring — only
    a defense-in-depth layer on top of it."""
    redacted = text
    for name in _SECRET_ENV_VARS:
        value = os.environ.get(name)
        if value and len(value) >= 6 and value in redacted:
            redacted = redacted.replace(value, "***REDACTED***")
    redacted = _AUTH_HEADER_RE.sub(r"\1***REDACTED***", redacted)
    return _QUERY_SECRET_RE.sub(r"\1=***REDACTED***", redacted)


class _Client:
    """Thin wrapper so tests can monkeypatch _resend_client() uniformly."""

    class _EmailsProxy:
        @staticmethod
        def send(payload: dict[str, object]) -> dict[str, object]:
            import resend

            result = resend.Emails.send(payload)  # type: ignore[arg-type]
            return dict(result)

    emails: _EmailsProxy = _EmailsProxy()


def _resend_client() -> _Client:
    import resend

    resend.api_key = os.environ["RESEND_API_KEY"]
    return _Client()


def _sentinel_dir() -> Path:
    override = os.environ.get("ALERT_SENTINEL_DIR")
    if override:
        return Path(override)
    # Persistent, travis-owned dedup dir — NOT world-writable /tmp. /tmp would
    # (a) let the collab sandbox (separate untrusted OS user) pre-create
    # alert-<unit>-<hour>.sent to SUPPRESS a real failure email, and (b) be
    # wiped per-invocation under PrivateTmp=yes. data/.alerts (mode 700) avoids both.
    return Path(__file__).resolve().parents[1] / "data" / ".alerts"


def _hour() -> str:
    return os.environ.get("ALERT_HOUR_OVERRIDE") or datetime.now(UTC).strftime("%Y%m%d%H")


def _journal_tail(unit: str) -> str | None:
    """Best-effort: the failing unit's last ~15 journal lines, plain text
    (``-o cat`` strips the syslog prefix). Returns None on any failure
    (missing binary, permission denied, timeout, ...) — the caller must still
    send the basic alert email."""
    try:
        result = subprocess.run(  # noqa: S603 — fixed args, no shell, unit is systemd-controlled
            ["journalctl", "-u", unit, "-n", str(_JOURNAL_LINES), "--no-pager", "-o", "cat"],
            capture_output=True,
            text=True,
            timeout=_JOURNAL_TIMEOUT_SECONDS,
            check=False,
        )
        output = result.stdout.strip()
        return _redact(output) or None
    except Exception:  # noqa: BLE001 — best-effort only, never blocks the alert
        return None


def _failed_alert_subjects(days: int = 2) -> list[str]:
    """Read-only SELECT: subjects of `alert_dispatch` rows with
    status='failed' from the last `days` days. Best-effort — any DB error
    (e.g. the accounting DB isn't reachable from this context) returns []."""
    try:
        from src.alerts.models import AlertDispatch
        from src.db.connection import SessionLocal, init_db

        init_db()
        cutoff = (datetime.now(UTC).date() - timedelta(days=days)).isoformat()
        with SessionLocal() as session:
            rows = (
                session.query(AlertDispatch)
                .filter(
                    AlertDispatch.status == "failed",
                    AlertDispatch.occurrence_date >= cutoff,
                )
                .all()
            )
            return [row.subject for row in rows]
    except Exception:  # noqa: BLE001 — best-effort only, never blocks the alert
        return []


def _build_body(unit: str) -> str:
    lines = [f"systemd reported a failure for {unit} on the Hetzner box at {_hour()} UTC."]

    journal = _journal_tail(unit)
    if journal:
        lines.append("")
        lines.append(f"Last {_JOURNAL_LINES} journal lines:")
        lines.append(journal)

    if unit in _DISPATCHER_UNITS:
        subjects = _failed_alert_subjects()
        if subjects:
            lines.append("")
            lines.append("Failed alert_dispatch rows (last 2 days):")
            lines.extend(f"  - {subject}" for subject in subjects)

    return "\n".join(lines)


def send_alert(unit: str) -> int:
    sdir = _sentinel_dir()
    sdir.mkdir(parents=True, exist_ok=True)
    sentinel = sdir / f"alert-{unit}-{_hour()}.sent"
    if sentinel.exists():
        print(f"[alert] already sent for {unit} this hour — skipping")
        return 0
    try:
        client = _resend_client()
        client.emails.send(
            {
                "from": _FROM,
                "to": _TO,
                "subject": f"[accounting/hetzner] unit failed: {unit}",
                "text": _build_body(unit),
            }
        )
    except Exception as exc:  # noqa: BLE001 — surface send failure to systemd
        print(f"[alert] send failed for {unit}: {exc}", file=sys.stderr)
        return 1
    sentinel.touch()
    print(f"[alert] sent for {unit}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: alert.py <unit-name>", file=sys.stderr)
        return 2
    return send_alert(argv[0])


if __name__ == "__main__":
    sys.exit(main())
