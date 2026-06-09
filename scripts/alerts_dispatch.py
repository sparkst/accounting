#!/usr/bin/env python3
"""Daily EA alert dispatch.

DRY-RUN by default — prints what *would* send and makes no network call.
Pass --apply to POST to the n8n webhook and record sends in alert_dispatch.

Invoked by com.sparkry.alerts-dispatch.timer (systemd) on the Hetzner box.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.alerts.dispatcher import dispatch_alerts  # noqa: E402
from src.db.connection import SessionLocal, init_db  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Dispatch EA alerts to the n8n webhook.")
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
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    today = args.date or date.today()
    init_db()
    session = SessionLocal()
    try:
        summary = dispatch_alerts(session, today, apply=args.apply)
    finally:
        session.close()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(
        f"[{mode}] {today.isoformat()} — "
        f"sent={summary.sent} skipped={summary.skipped} "
        f"failed={summary.failed} dry_run={summary.dry_run}"
    )
    return 1 if summary.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
