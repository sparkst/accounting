#!/usr/bin/env python3
"""Quark register digest — runs ON the Hetzner box against the live prod DB,
READ-ONLY, and prints a compact JSON digest (a few KB) so Quark never pulls the
31 MB snapshot just to read the register.

Pure stdlib (sqlite3 + datetime) so it runs under the box's plain python3 with no
venv / repo imports. Invoked over SSH via stdin:

    ssh travis@ubuntu 'python3 - /home/travis/accounting/data/accounting.db' \
        < scripts/quark_register_digest.py

Read-only: opens the DB with mode=ro (WAL-safe concurrent read; never writes).
Covers the pulse (week revenue/expenses, AR, next tax deadline), per-entity 90d
P&L, status hygiene, and the unassigned-entity needs-review count.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, datetime, timedelta


def main() -> int:
    db_path = sys.argv[1] if len(sys.argv) > 1 else "data/accounting.db"
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    today = date.today()

    # --- This-week window (Monday-anchored, mirrors weekly-pl-report.py) ---
    if today.weekday() == 0:
        week_start = today - timedelta(days=7)
    else:
        week_start = today - timedelta(days=today.weekday() + 7)
    ws, we = week_start.isoformat(), today.isoformat()

    def scalar(sql: str, params: tuple = ()) -> float:
        row = cur.execute(sql, params).fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0

    week_income = scalar(
        "SELECT SUM(abs(amount)) FROM transactions WHERE status!='rejected' "
        "AND direction='income' AND date>=? AND date<=?", (ws, we))
    week_expense = scalar(
        "SELECT SUM(abs(amount)) FROM transactions WHERE status!='rejected' "
        "AND direction='expense' AND date>=? AND date<=?", (ws, we))

    # --- AR (unpaid invoices) ---
    ar = cur.execute(
        "SELECT COUNT(*), COALESCE(SUM(total),0) FROM invoices "
        "WHERE status IN ('sent','overdue')").fetchone()

    # --- Per-entity trailing-90d P&L ---
    per_entity = [
        {
            "entity": r[0] or "(unassigned)",
            "income": round(float(r[1]), 2),
            "expense": round(float(r[2]), 2),
            "net": round(float(r[3]), 2),
        }
        for r in cur.execute(
            "SELECT entity, "
            "SUM(CASE WHEN direction='income' THEN abs(amount) ELSE 0 END), "
            "SUM(CASE WHEN direction='expense' THEN abs(amount) ELSE 0 END), "
            "SUM(CASE WHEN direction='income' THEN abs(amount) "
            "WHEN direction='expense' THEN -abs(amount) ELSE 0 END) "
            "FROM transactions WHERE status!='rejected' "
            "AND date>=date('now','-90 day') GROUP BY entity ORDER BY 4 DESC")
    ]

    # --- Hygiene ---
    status_counts = {r[0]: int(r[1]) for r in cur.execute(
        "SELECT status, COUNT(*) FROM transactions GROUP BY status")}
    nr_no_entity = int(scalar(
        "SELECT COUNT(*) FROM transactions WHERE status='needs_review' "
        "AND (entity IS NULL OR entity='')"))

    max_txn_date = cur.execute(
        "SELECT MAX(date) FROM transactions WHERE status!='rejected'").fetchone()[0]

    # Plaid connections needing re-auth — only feeds a human must actually fix.
    # Excluded zombies: 'abandoned'/'pending_oauth' rows (never-completed Link
    # attempts) and superseded 'disconnected' rows (no mapped accounts AND the
    # institution has another active item covering it).
    try:
        plaid_reauth = int(scalar(
            "SELECT COUNT(*) FROM plaid_item pi "
            "WHERE (pi.status='active' AND pi.last_sync_status='error') "
            "OR (pi.status='disconnected' AND ("
            "  EXISTS (SELECT 1 FROM account a WHERE a.plaid_item_id=pi.id) "
            "  OR NOT EXISTS (SELECT 1 FROM plaid_item pj "
            "     WHERE pj.institution_id=pi.institution_id "
            "     AND pj.status='active')))"))
    except sqlite3.Error:
        plaid_reauth = None

    con.close()

    # --- Next tax deadline (calendar; mirrors health._build_tax_deadlines) ---
    y = today.year
    cands: list[tuple[str, date]] = []
    # Sparkry monthly B&O: due the 25th of each month.
    for m in (today.month, today.month % 12 + 1):
        yy = y if m >= today.month else y + 1
        cands.append(("WA B&O (Sparkry, Monthly)", date(yy, m, 25)))
    # BlackLine quarterly B&O.
    for label, mmdd in (("Q1", (4, 30)), ("Q2", (7, 31)), ("Q3", (10, 31))):
        cands.append((f"WA B&O (BlackLine, {label})", date(y, *mmdd)))
    cands.append(("WA B&O (BlackLine, Q4)", date(y + 1, 1, 31)))
    # Quarterly estimated tax.
    for mmdd in ((4, 15), (6, 15), (9, 15)):
        cands.append(("Federal estimated tax", date(y, *mmdd)))
    cands.append(("Federal estimated tax", date(y + 1, 1, 15)))

    upcoming = sorted((d, lbl) for lbl, d in cands if d >= today)
    next_deadline = None
    if upcoming:
        d, lbl = upcoming[0]
        next_deadline = {"label": lbl, "due": d.isoformat(), "days": (d - today).days}

    print(json.dumps({
        "register_asof": max_txn_date,
        "week": {"start": ws, "end": we,
                 "revenue": round(week_income, 2), "expenses": round(week_expense, 2)},
        "ar": {"count": int(ar[0]), "total": round(float(ar[1]), 2)},
        "per_entity_90d": per_entity,
        "status_counts": status_counts,
        "needs_review_no_entity": nr_no_entity,
        "plaid_reauth": plaid_reauth,
        "next_deadline": next_deadline,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
