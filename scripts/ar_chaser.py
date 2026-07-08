#!/usr/bin/env python3
"""AR chaser CLI (REQ-ARC-001/002).

Subcommands:
    run [--apply] [--date YYYY-MM-DD]  Draft the due reminder ladder (DRY-RUN default).
    list                               List all open (non-terminal) reminders.
    approve <reminder-id>              Approve + send a pending reminder via Resend.
    dismiss <reminder-id>              Dismiss a pending reminder without sending.

The CLI is the local-operator fallback for the Telegram approval flow; approve /
dismiss set ``approved_via="cli"`` and require no token. Intended for a daily
systemd timer (``accounting-ar-chaser.timer``, 14:15 UTC) running ``run --apply``.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.ar.chaser import dismiss_reminder, run  # noqa: E402
from src.ar.send import approve_and_send  # noqa: E402
from src.db.connection import SessionLocal, init_db  # noqa: E402
from src.models.ar_reminder import (  # noqa: E402
    AR_STATUS_DISMISSED,
    AR_STATUS_SENT,
    ArReminder,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="AR chaser — draft-for-approval reminders.")
    sub = p.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Draft the due reminder ladder.")
    run_p.add_argument(
        "--apply",
        action="store_true",
        help="Insert drafts + POST notifications (default: DRY-RUN).",
    )
    run_p.add_argument(
        "--date",
        type=date.fromisoformat,
        default=None,
        help="Override today's date (YYYY-MM-DD) for testing.",
    )

    sub.add_parser("list", help="List open reminders.")

    approve_p = sub.add_parser("approve", help="Approve + send a pending reminder.")
    approve_p.add_argument("reminder_id")

    dismiss_p = sub.add_parser("dismiss", help="Dismiss a pending reminder.")
    dismiss_p.add_argument("reminder_id")

    return p.parse_args(argv)


def _cmd_run(args: argparse.Namespace) -> int:
    today = args.date or date.today()
    session = SessionLocal()
    try:
        summary = run(session, today=today, apply=args.apply)
    finally:
        session.close()
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(
        f"[{mode}] {today.isoformat()} — drafted={summary.drafted} "
        f"dismissed={summary.dismissed} notified={summary.notified} "
        f"notify_failed={summary.notify_failed}"
    )
    return 1 if summary.notify_failed else 0


def _cmd_list() -> int:
    session = SessionLocal()
    try:
        reminders = (
            session.query(ArReminder)
            .filter(ArReminder.status.notin_([AR_STATUS_SENT, AR_STATUS_DISMISSED]))
            .order_by(ArReminder.created_at)
            .all()
        )
        if not reminders:
            print("No open reminders.")
            return 0
        for r in reminders:
            print(
                f"{r.id}  invoice={r.invoice_id}  rung={r.rung}  "
                f"status={r.status}  subject={r.draft_subject!r}"
            )
    finally:
        session.close()
    return 0


def _cmd_approve(reminder_id: str) -> int:
    session = SessionLocal()
    try:
        reminder = session.get(ArReminder, reminder_id)
        if reminder is None:
            print(f"reminder {reminder_id} not found")
            return 1
        result = approve_and_send(session, reminder, approved_via="cli")
    finally:
        session.close()
    if result.sent:
        print(f"sent reminder {reminder_id} (message_id={result.message_id})")
        return 0
    print(f"not sent — {result.status}: {result.error or ''}".rstrip(": "))
    return 1


def _cmd_dismiss(reminder_id: str) -> int:
    session = SessionLocal()
    try:
        reminder = session.get(ArReminder, reminder_id)
        if reminder is None:
            print(f"reminder {reminder_id} not found")
            return 1
        changed = dismiss_reminder(
            session, reminder, changed_by="cli", approved_via="cli"
        )
    finally:
        session.close()
    if changed:
        print(f"dismissed reminder {reminder_id}")
        return 0
    print(f"reminder {reminder_id} already terminal ({reminder.status})")
    return 1


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    init_db()
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "list":
        return _cmd_list()
    if args.command == "approve":
        return _cmd_approve(args.reminder_id)
    if args.command == "dismiss":
        return _cmd_dismiss(args.reminder_id)
    return 2  # pragma: no cover - argparse enforces a valid subcommand


if __name__ == "__main__":
    raise SystemExit(main())
