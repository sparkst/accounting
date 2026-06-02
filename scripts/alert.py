"""systemd OnFailure handler → one Resend email naming the failed unit.

REQ-HM-014. Hourly per-unit dedup via /tmp sentinel; exits non-zero only on a
real send failure. This unit has NO OnFailure= itself (breaks alert recursion)."""
from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

_TO = "sparkst@gmail.com"
_FROM = "alerts@sparkry.ai"


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
    return Path("/tmp")


def _hour() -> str:
    return os.environ.get("ALERT_HOUR_OVERRIDE") or datetime.now(UTC).strftime("%Y%m%d%H")


def send_alert(unit: str) -> int:
    sentinel = _sentinel_dir() / f"alert-{unit}-{_hour()}.sent"
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
                "text": f"systemd reported a failure for {unit} on the Hetzner box at {_hour()} UTC.",
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
