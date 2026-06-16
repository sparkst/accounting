#!/usr/bin/env python3
"""Daily balance-milestone alert dispatch (REQ-BAL-001..010).

DRY-RUN by default — computes crossings and prints what *would* send, no network.
Pass --apply to POST to the n8n severity webhook and record sends in alert_dispatch.
Pass --digest to also send the daily account-pulse (REQ-BAL-008).

Intended for a daily systemd timer on the Hetzner box (Track A). Business-account
coverage requires the Chase business Plaid Item re-authed + a daily balance sync
(REQ-BAL-009).
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.balance_alerts.digest import post_pulse  # noqa: E402
from src.balance_alerts.dispatcher import dispatch_balance_alerts  # noqa: E402
from src.db.connection import SessionLocal, init_db  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Dispatch balance-milestone alerts.")
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually POST to n8n and record sends (default: DRY-RUN).",
    )
    p.add_argument(
        "--date",
        type=date.fromisoformat,
        default=None,
        help="Override today's date (YYYY-MM-DD) for testing.",
    )
    p.add_argument(
        "--digest",
        action="store_true",
        help="Also send the daily account-pulse digest (REQ-BAL-008).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    today = args.date or date.today()
    init_db()
    session = SessionLocal()
    try:
        summary = dispatch_balance_alerts(today, session, apply=args.apply)
        pulse_status = None
        if args.digest:
            pulse_status = post_pulse(today, session, apply=args.apply).status
    finally:
        session.close()

    mode = "APPLY" if args.apply else "DRY-RUN"
    line = (
        f"[{mode}] {today.isoformat()} — sent={summary.sent} "
        f"skipped={summary.skipped} failed={summary.failed} dry_run={summary.dry_run}"
    )
    if pulse_status is not None:
        line += f" pulse={pulse_status}"
    print(line)
    return 1 if (summary.failed or pulse_status == "failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
