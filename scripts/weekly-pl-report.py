#!/usr/bin/env python3
"""Weekly P&L Report — 5-line Monday morning financial summary.

Generates a phone-readable financial pulse for Travis:
  Line 1: Revenue this week | Expenses this week
  Line 2: AR Outstanding
  Line 3: Next tax deadline
  Line 4: Flag (one item needing attention or 'all clear')
  Line 5: Date range

Output: writes to reports/weekly-pl-latest.txt (overwritten each run)
        and reports/weekly-pl-{date}.txt (archived)

Designed to run via systemd timer (weekly-pl-report.timer) on the Hetzner box,
Mon 06:00 UTC. stdout is captured by journald (journalctl -u weekly-pl-report).
Output path is read by Jarvis/C3PO for Telegram delivery.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from sqlalchemy import func

from src.db.connection import SessionLocal, init_db
from src.models.enums import TransactionStatus
from src.models.transaction import Transaction
from src.reports.pl_engine import compute_entity_pl, week_window


def generate_report() -> str:
    """Generate the 5-line weekly P&L summary.

    REQ-FIX-API-003: revenue/expenses come from ``compute_entity_pl`` (all
    entities combined, ``entity=None``) — the single source of truth shared
    with the WBR/sellability/tax-forecast reports — over the exact
    half-open ``[week_start, this_monday)`` 7-day window, regardless of
    which day of the week this script runs on. Reimbursable pairs are
    netted out of both sides; rejected/split_parent rows are excluded.
    """
    init_db()
    session = SessionLocal()

    try:
        now = datetime.now()
        week_start, week_end = week_window(now.date())

        # --- Revenue & Expenses (all entities, this week) ---
        pl = compute_entity_pl(session, week_start, week_end, entity=None)
        income = pl.revenue
        expenses = pl.expenses

        # --- AR Outstanding (sent/overdue invoices) ---
        try:
            from src.models.invoice import Invoice
            ar_total = session.query(func.sum(Invoice.total)).filter(
                Invoice.status.in_(["sent", "overdue"]),
            ).scalar() or Decimal("0")
        except Exception:
            ar_total = Decimal("0")

        # --- Next Tax Deadline ---
        try:
            from src.api.routes.health import _build_tax_deadlines
            deadlines = _build_tax_deadlines()
            if deadlines:
                dl = deadlines[0]  # already sorted by due date
                deadline_line = f"{dl['label']} — {dl['due_date']} ({dl['days_until_due']}d)"
            else:
                deadline_line = "No upcoming deadlines"
        except Exception as exc:
            deadline_line = f"Unable to load deadlines: {exc}"

        # --- Expense anomaly detection ---
        # Compare each vendor's this-month spend vs their 3-month average
        anomalies: list[str] = []
        try:
            current_month = now.strftime("%Y-%m")
            three_months_ago = (now - timedelta(days=90)).strftime("%Y-%m-%d")

            # Per-vendor average over last 3 months (excluding current month)
            vendor_avgs = session.query(
                Transaction.description,
                func.avg(func.abs(Transaction.amount)).label("avg_amt"),
                func.count().label("txn_count"),
            ).filter(
                Transaction.status != TransactionStatus.REJECTED.value,
                Transaction.status != TransactionStatus.SPLIT_PARENT.value,
                Transaction.direction == "expense",
                Transaction.date >= three_months_ago,
                ~Transaction.date.like(f"{current_month}%"),
            ).group_by(Transaction.description).having(func.count() >= 2).all()

            avg_map = {v.description: float(v.avg_amt) for v in vendor_avgs}

            # This month's spend per vendor
            month_start = now.replace(day=1).strftime("%Y-%m-%d")
            vendor_this_month = session.query(
                Transaction.description,
                func.sum(func.abs(Transaction.amount)).label("total"),
            ).filter(
                Transaction.status != TransactionStatus.REJECTED.value,
                Transaction.status != TransactionStatus.SPLIT_PARENT.value,
                Transaction.direction == "expense",
                Transaction.date >= month_start,
            ).group_by(Transaction.description).all()

            for v in vendor_this_month:
                avg = avg_map.get(v.description)
                if avg and avg > 0 and float(v.total) >= avg * 2 and float(v.total) > 50:
                    anomalies.append(
                        f"🔴 {v.description[:25]}: ${float(v.total):,.0f} this month (avg ${avg:,.0f})"
                    )
        except Exception:
            pass  # anomaly detection is best-effort

        # --- Flag: one item needing attention ---
        needs_review = session.query(func.count()).filter(
            Transaction.status == TransactionStatus.NEEDS_REVIEW.value,
        ).scalar() or 0

        auto_classified = session.query(func.count()).filter(
            Transaction.status == TransactionStatus.AUTO_CLASSIFIED.value,
        ).scalar() or 0

        if anomalies:
            flag = anomalies[0]  # most important anomaly
        elif needs_review > 0:
            flag = f"⚠️ {needs_review} transactions need review"
        elif auto_classified > 10:
            flag = f"📋 {auto_classified} auto-classified transactions awaiting confirmation"
        elif float(ar_total) > 30000:
            flag = f"💰 AR outstanding: ${float(ar_total):,.0f}"
        else:
            flag = "✅ All clear"

        # --- Build the report ---
        lines = [
            f"Revenue: ${float(income):,.2f} | Expenses: ${float(expenses):,.2f}",
            f"AR Outstanding: ${float(ar_total):,.2f}",
            f"Next deadline: {deadline_line}",
            f"Flag: {flag}",
            # REQ-FIX-API-003: both bounds of the exact half-open window,
            # explicit about end-exclusivity so the printed range is never
            # ambiguous about whether week_end's date is included.
            f"Week of {week_start} to {week_end} (exclusive)",
        ]

        # Append additional anomalies (beyond the first which is in the flag)
        if len(anomalies) > 1:
            lines.append("Alerts: " + " | ".join(anomalies[1:3]))

        return "\n".join(lines)

    finally:
        session.close()


def main() -> None:
    report = generate_report()

    # Write to latest (overwritten each run)
    reports_dir = PROJECT_ROOT / "reports"
    reports_dir.mkdir(exist_ok=True)

    latest = reports_dir / "weekly-pl-latest.txt"
    latest.write_text(report + "\n", encoding="utf-8")

    # Archive with date
    date_str = datetime.now().strftime("%Y-%m-%d")
    archive = reports_dir / f"weekly-pl-{date_str}.txt"
    archive.write_text(report + "\n", encoding="utf-8")

    # Print to stdout (captured by journald: journalctl -u weekly-pl-report.service)
    print(report)
    print(f"\nSaved to: {latest}")


if __name__ == "__main__":
    main()
