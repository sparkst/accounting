#!/usr/bin/env python3
"""Daily data-level freshness/invariant sentinel (REQ-SEN-001..008).

DRY-RUN by default — runs every check and prints violations, no network.
Pass --apply to POST the digest to the n8n severity webhook.

Process monitoring (OnFailure) tells us when a unit *dies*; this sentinel tells
us when the *data* stopped moving even though every unit exited 0 — the failure
mode behind the 30-day frozen wealth balances and the six silent weeks of
Stripe/Shopify ingest.

Exit codes:
  0 — checks ran; violations (if any) were reported/printed
  1 — sentinel infrastructure failure (DB unreachable, webhook send failed) —
      trips the systemd OnFailure alert so the sentinel can't die silently.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.db.connection import SessionLocal, init_db  # noqa: E402
from src.monitoring.sentinel import dispatch_sentinel  # noqa: E402

DEFAULT_REPORT = PROJECT_ROOT / "reports" / "weekly-pl-latest.txt"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Assert data-level freshness invariants.")
    p.add_argument(
        "--apply",
        action="store_true",
        help="POST the violation digest to the n8n severity webhook (default: DRY-RUN).",
    )
    p.add_argument(
        "--now",
        type=datetime.fromisoformat,
        default=None,
        help="Override the reference time (ISO datetime) for testing.",
    )
    p.add_argument(
        "--report-path",
        type=Path,
        default=DEFAULT_REPORT,
        help=f"Weekly P&L artifact to freshness-check (default: {DEFAULT_REPORT}).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    # UTC-naive to match every DB timestamp the checks compare against (repo
    # convention: _now() = datetime.now(UTC).replace(tzinfo=None)). A local
    # now() would shift the whole staleness axis by the box's TZ offset.
    now = args.now or datetime.now(UTC).replace(tzinfo=None)
    # Normalize tz-aware --now to UTC-naive to avoid comparison errors with DB timestamps.
    if now.tzinfo is not None:
        now = now.astimezone(UTC).replace(tzinfo=None)
    try:
        init_db()
        session = SessionLocal()
    except Exception as exc:  # noqa: BLE001 — infra failure must exit 1 loudly
        print(f"sentinel: infrastructure failure: {type(exc).__name__}", file=sys.stderr)
        return 1
    try:
        violations, result = dispatch_sentinel(
            session, now, report_path=args.report_path, apply=args.apply
        )
    finally:
        session.close()

    mode = "APPLIED" if args.apply else "DRY-RUN"
    if not violations:
        print(f"sentinel {mode}: all checks clean")
        return 0
    print(f"sentinel {mode}: {len(violations)} violation(s)")
    for v in violations:
        print(f"  [{v.severity}] {v.check}: {v.subject} — {v.detail}")
    if args.apply and (result is None or result.status != "sent"):
        detail = result.error if result is not None else "no result"
        print(f"sentinel: webhook send FAILED ({detail})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
