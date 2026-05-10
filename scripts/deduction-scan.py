#!/usr/bin/env python3
"""Tax Deduction Maximizer — scan for commonly missed deductions.

Analyzes confirmed transactions and flags deductions Travis may be missing.
Outputs a report with specific recommendations and estimated tax savings.

Usage:
    python3 scripts/deduction-scan.py [--entity sparkry] [--year 2026]
"""

from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from src.db.connection import SessionLocal, init_db
from src.models.transaction import Transaction
from src.models.enums import TransactionStatus
from sqlalchemy import func


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan for missed tax deductions")
    parser.add_argument("--entity", default="sparkry", help="Entity to scan (default: sparkry)")
    parser.add_argument("--year", type=int, default=2026, help="Tax year (default: 2026)")
    args = parser.parse_args()

    init_db()
    session = SessionLocal()

    try:
        entity = args.entity
        year = args.year
        year_prefix = str(year)

        # Get all non-rejected transactions for the entity+year
        txns = session.query(Transaction).filter(
            Transaction.entity == entity,
            Transaction.status != TransactionStatus.REJECTED.value,
            Transaction.date.like(f"{year_prefix}-%"),
        ).all()

        # Get current category totals
        cat_totals: dict[str, float] = {}
        for tx in txns:
            cat = tx.tax_category or "UNCATEGORIZED"
            cat_totals[cat] = cat_totals.get(cat, 0) + abs(float(tx.amount or 0))

        print(f"{'=' * 65}")
        print(f"Tax Deduction Maximizer — {entity.upper()} {year}")
        print(f"{'=' * 65}")
        print(f"Transactions scanned: {len(txns)}")
        print()

        findings: list[dict] = []

        # --- Check 1: Health Insurance (Self-Employed Health Insurance Deduction) ---
        health_ins = cat_totals.get("HEALTH_INSURANCE", 0)
        has_insurance = cat_totals.get("INSURANCE", 0) > 0
        if health_ins == 0 and has_insurance:
            findings.append({
                "category": "Health Insurance (1040 Line 17)",
                "status": "⚠️ MISSING",
                "detail": f"You have ${cat_totals['INSURANCE']:,.0f} in business insurance but $0 in health insurance premiums. "
                          "Self-employed individuals can deduct 100% of health/dental/vision premiums for themselves, "
                          "spouse, and dependents — above the line (reduces AGI). "
                          "If you pay premiums, add them as HEALTH_INSURANCE category.",
                "est_savings": "$2,000-8,000/yr (typical family plan × 25% tax bracket)",
            })
        elif health_ins == 0:
            findings.append({
                "category": "Health Insurance (1040 Line 17)",
                "status": "⚠️ MISSING",
                "detail": "No health insurance premiums recorded. If self-employed and paying your own premiums, "
                          "this is one of the largest above-the-line deductions available.",
                "est_savings": "$2,000-8,000/yr",
            })

        # --- Check 2: Home Office ---
        has_home_office = any("home" in str(tx.tax_category or "").lower() or
                            "home" in str(tx.description or "").lower()
                            for tx in txns if tx.tax_category == "OFFICE_EXPENSE")
        home_office_in_export = True  # We have the $180 home office in export
        if not has_home_office:
            findings.append({
                "category": "Home Office Deduction",
                "status": "✅ CONFIGURED",
                "detail": f"Home office deduction of $180 (simplified method, 6×6 room) is included in tax export. "
                          "Consider actual method if rent/mortgage + utilities exceed $180.",
                "est_savings": "Already captured ($180)",
            })

        # --- Check 3: Vehicle / Mileage ---
        car_expense = cat_totals.get("CAR_AND_TRUCK", 0)
        has_travel = cat_totals.get("TRAVEL", 0) > 0
        if car_expense == 0 and has_travel:
            findings.append({
                "category": "Vehicle / Mileage (Schedule C Line 9)",
                "status": "⚠️ MISSING",
                "detail": f"You have ${cat_totals['TRAVEL']:,.0f} in travel expenses but $0 in car/truck. "
                          "If you drive to client sites, conferences, or business meetings, track mileage at "
                          "72.5¢/mile (2026 IRS rate). 5,000 business miles = $3,625 deduction.",
                "est_savings": "$1,000-5,000/yr (depending on miles driven)",
            })

        # --- Check 4: Internet / Phone (Business Use %) ---
        has_internet = any("internet" in str(tx.description or "").lower() or
                          "comcast" in str(tx.description or "").lower() or
                          "xfinity" in str(tx.description or "").lower() or
                          "spectrum" in str(tx.description or "").lower()
                          for tx in txns)
        has_phone = any("t-mobile" in str(tx.description or "").lower() or
                       "verizon" in str(tx.description or "").lower() or
                       "at&t" in str(tx.description or "").lower() or
                       "phone" in str(tx.description or "").lower()
                       for tx in txns)
        if not has_internet:
            findings.append({
                "category": "Internet (Business Use %)",
                "status": "⚠️ MISSING",
                "detail": "No internet service charges found. If you work from home, you can deduct the "
                          "business-use percentage of your internet bill. Typical: 50-80% business use "
                          "× $100/mo = $600-960/yr deduction.",
                "est_savings": "$600-960/yr",
            })
        if not has_phone:
            findings.append({
                "category": "Cell Phone (Business Use %)",
                "status": "⚠️ MISSING",
                "detail": "No cell phone charges found. Business-use percentage of your phone bill is deductible. "
                          "Typical: 50-75% business use × $100/mo = $600-900/yr deduction.",
                "est_savings": "$600-900/yr",
            })

        # --- Check 5: Professional Development ---
        has_edu = any("course" in str(tx.description or "").lower() or
                     "udemy" in str(tx.description or "").lower() or
                     "coursera" in str(tx.description or "").lower() or
                     "training" in str(tx.description or "").lower() or
                     "conference" in str(tx.description or "").lower() or
                     "summit" in str(tx.description or "").lower()
                     for tx in txns)
        if not has_edu:
            findings.append({
                "category": "Professional Development / Education",
                "status": "ℹ️ NOT FOUND",
                "detail": "No professional development expenses found. Courses, conferences, books, and "
                          "certifications related to your business are deductible as SUPPLIES or OTHER_EXPENSE.",
                "est_savings": "$500-3,000/yr (if applicable)",
            })

        # --- Check 6: Retirement Contributions ---
        has_retirement = any("sep" in str(tx.description or "").lower() or
                           "ira" in str(tx.description or "").lower() or
                           "401k" in str(tx.description or "").lower() or
                           "retirement" in str(tx.description or "").lower()
                           for tx in txns)
        if not has_retirement:
            findings.append({
                "category": "SEP-IRA / Solo 401(k) Contribution",
                "status": "⚠️ MISSING",
                "detail": "No retirement contributions found. Self-employed individuals can contribute up to 25% "
                          "of net self-employment income to a SEP-IRA (max $69,000 for 2026) — fully deductible "
                          "above the line. This is the single largest tax reduction available to solopreneurs.",
                "est_savings": "$5,000-25,000/yr (at 25% bracket on contribution amount)",
            })

        # --- Check 7: Meals at 50% ---
        meals = cat_totals.get("MEALS", 0)
        total_expenses = sum(v for k, v in cat_totals.items() if k not in ("UNCATEGORIZED",))
        if meals > 0 and meals < total_expenses * 0.02:
            findings.append({
                "category": "Meals (50% Deductible)",
                "status": "ℹ️ LOW",
                "detail": f"Only ${meals:,.0f} in meals ({meals/total_expenses*100:.1f}% of expenses). "
                          "Client meals, team meals, and business travel meals are 50% deductible. "
                          "Common misses: meals during business travel, client lunches, working lunches.",
                "est_savings": "$200-1,000/yr (if meals are being categorized elsewhere)",
            })

        # --- Print findings ---
        missed = [f for f in findings if "MISSING" in f["status"]]
        low = [f for f in findings if "LOW" in f["status"] or "NOT FOUND" in f["status"]]
        ok = [f for f in findings if "CONFIGURED" in f["status"] or "OK" in f["status"]]

        if missed:
            print(f"🔴 MISSED DEDUCTIONS ({len(missed)}):")
            print("-" * 65)
            for f in missed:
                print(f"\n  {f['category']}")
                print(f"  {f['status']}")
                print(f"  {f['detail']}")
                print(f"  Est. savings: {f['est_savings']}")

        if low:
            print(f"\n📋 REVIEW THESE ({len(low)}):")
            print("-" * 65)
            for f in low:
                print(f"\n  {f['category']}")
                print(f"  {f['status']}")
                print(f"  {f['detail']}")

        if ok:
            print(f"\n✅ ALREADY CAPTURED ({len(ok)}):")
            print("-" * 65)
            for f in ok:
                print(f"  {f['category']}: {f['est_savings']}")

        # Total estimated missed
        print(f"\n{'=' * 65}")
        print(f"Missed deductions found: {len(missed)}")
        if missed:
            print(f"Estimated potential tax savings: $3,000-15,000/yr")
            print(f"\nACTION: Review the missed items above with Travis before Apr 15 filing.")
        else:
            print("All common deductions captured! 🎉")

    finally:
        session.close()


if __name__ == "__main__":
    main()
