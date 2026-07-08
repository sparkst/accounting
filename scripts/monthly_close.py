#!/usr/bin/env python3
"""Monthly close agent CLI (REQ-MCA-001/004, spec §1).

Builds the deterministic close report (reconciliation + anomalies + auto-confirm
summary + data-hygiene callouts), optionally renders the env-gated Gemini
narrative, and emails it via Resend. DRY-RUN default per the CLAUDE.md rule —
``--apply`` sends and records the ``alert_dispatch`` ledger row.

Usage:
    doppler run -- python -m scripts.monthly_close                 # dry-run, prior month
    doppler run -- python -m scripts.monthly_close --month 2026-06 # dry-run a month
    doppler run -- python -m scripts.monthly_close --apply         # send
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# Add project root to path when invoked as a script.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from sqlalchemy.orm import Session  # noqa: E402

from src.alerts.models import AlertDispatch  # noqa: E402
from src.close.email import render_html, send_close_report  # noqa: E402
from src.close.narrative import render_narrative  # noqa: E402
from src.close.report import CloseReport, build_close_report  # noqa: E402
from src.db.connection import SessionLocal, init_db  # noqa: E402

logger = logging.getLogger("monthly_close")


@dataclass
class CloseRun:
    report: CloseReport
    narrative: str | None
    dispatch: AlertDispatch | None
    html: str


def run_close(
    session: Session,
    *,
    month: str | None = None,
    apply: bool = False,
    today: date | None = None,
    to_email: str | None = None,
) -> CloseRun:
    """Build, render, and (on ``apply``) send the monthly close. Caller commits."""
    report = build_close_report(session, month, today=today)
    narrative = render_narrative(report, session=session)
    html = render_html(report, narrative=narrative)
    dispatch = send_close_report(
        session,
        report,
        apply=apply,
        to_email=to_email,
        narrative=narrative,
        today=today,
    )
    return CloseRun(report=report, narrative=narrative, dispatch=dispatch, html=html)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--month", default=None, help="YYYY-MM (default: prior month)")
    parser.add_argument(
        "--apply", action="store_true", help="Send the email + record the ledger row."
    )
    parser.add_argument(
        "--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR")
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    init_db()
    with SessionLocal() as session:
        run = run_close(session, month=args.month, apply=args.apply)
        if args.apply:
            session.commit()

    mode = "APPLIED" if args.apply else "DRY-RUN"
    rep = run.report
    logger.info(
        "monthly close %s: month=%s rows=%d auto_confirmed=%d needs_review=%d "
        "new_vendors=%d outliers=%d missing_recurring=%d",
        mode,
        rep.month,
        rep.rows_ingested,
        rep.autoconfirm.total,
        rep.needs_review_depth,
        len(rep.anomalies.new_vendors),
        len(rep.anomalies.outliers),
        len(rep.anomalies.missing_recurring),
    )
    if run.dispatch is not None:
        logger.info("close email ledger: status=%s", run.dispatch.status)
        if run.dispatch.status == "failed":
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
