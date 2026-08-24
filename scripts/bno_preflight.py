#!/usr/bin/env python3
"""Deterministic B&O pre-filing checklist (REQ-BNO-CHK-001..006).

Run BEFORE filing the monthly Sparkry (or quarterly BlackLine) WA B&O return.
Strictly read-only: the SQLite register is opened with
``file:...?immutable=1&mode=ro`` and nothing is ever written. No network.

Usage:
    python scripts/bno_preflight.py --entity sparkry   --period 2026-07
    python scripts/bno_preflight.py --entity blackline --period 2026-Q2

Prints one PASS/FAIL line per check plus detail rows for failures; exits
non-zero if ANY check fails.

Checks:
  REQ-BNO-CHK-001  sign-vs-direction integrity in the period
  REQ-BNO-CHK-002  stale unlinked reimbursables with an in-period deposit
                   from the same counterparty
  REQ-BNO-CHK-003  confirmed-only gate on in-period income rows
  REQ-BNO-CHK-004  refund/chargeback sweep — the P3-302 manual
                   returns-and-allowances deduction (informational)
  REQ-BNO-CHK-005  prior-calendar-year ServiceOther gross < $1M (ESSB 2081
                   rate tier / DOR code 40 assumption)
  REQ-BNO-CHK-006  every in-period WA retail row's locality resolves in
                   WA_LOCATION_CODES (catches the REQ-FIX-TAX-007 hard-fail
                   before upload generation)
"""

from __future__ import annotations

import argparse
import calendar
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.export.basis import (  # noqa: E402
    RETAIL_CATEGORIES,
    UNKNOWN_WA_LOCATION,
    pretax_abs_amount,
    retail_facts,
)
from src.export.bno_tax import BO_CLASSIFICATION, INCOME_CATEGORIES  # noqa: E402

DEFAULT_DB = PROJECT_ROOT / "data" / "accounting.db"

# ESSB 2081: Service & Other Activities stays at 1.5% / DOR code 40 only while
# prior-calendar-year taxable ServiceOther gross receipts are under $1M.
SERVICE_TIER_THRESHOLD = Decimal("1000000")

REIMBURSABLE_STALE_DAYS = 30

REFUND_PATTERN = re.compile(r"refund|chargeback|dispute|returned?\b", re.IGNORECASE)


@dataclass
class Period:
    """A single B&O filing period: one month or one quarter."""

    year: int
    months: list[int]
    label: str

    @property
    def start(self) -> str:
        return f"{self.year}-{self.months[0]:02d}-01"

    @property
    def end(self) -> str:
        last_month = self.months[-1]
        last_day = calendar.monthrange(self.year, last_month)[1]
        return f"{self.year}-{last_month:02d}-{last_day:02d}"

    def contains(self, date_str: str) -> bool:
        return self.start <= str(date_str or "")[:10] <= self.end


def parse_period(raw: str) -> Period:
    """Parse ``YYYY-MM`` (monthly) or ``YYYY-QN`` (quarterly)."""
    m = re.fullmatch(r"(\d{4})-(\d{2})", raw)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        if not 1 <= month <= 12:
            raise ValueError(f"Invalid month in period: {raw!r}")
        return Period(year, [month], raw)
    q = re.fullmatch(r"(\d{4})-[Qq]([1-4])", raw)
    if q:
        year, quarter = int(q.group(1)), int(q.group(2))
        months = [3 * (quarter - 1) + i for i in (1, 2, 3)]
        return Period(year, months, raw.upper())
    raise ValueError(f"Period must be YYYY-MM or YYYY-QN, got {raw!r}")


@dataclass
class CheckResult:
    req_id: str
    name: str
    passed: bool
    details: list[str] = field(default_factory=list)


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


# ── REQ-BNO-CHK-001 ────────────────────────────────────────────────────────


def check_sign_vs_direction(txs: list[dict[str, Any]], period: Period) -> CheckResult:
    """No in-period expense stored positive nor income stored negative."""
    bad: list[str] = []
    for tx in txs:
        if not period.contains(tx.get("date", "")):
            continue
        amt = _dec(tx.get("amount"))
        direction = tx.get("direction")
        if (direction == "expense" and amt > 0) or (direction == "income" and amt < 0):
            bad.append(
                f"  id={tx.get('id')} date={tx.get('date')} direction={direction}"
                f" amount={amt} desc={str(tx.get('description'))[:60]!r}"
            )
    return CheckResult(
        "REQ-BNO-CHK-001",
        "sign-vs-direction integrity",
        passed=not bad,
        details=bad,
    )


# ── REQ-BNO-CHK-002 ────────────────────────────────────────────────────────

_WORD = re.compile(r"[a-z]{4,}")
_STOPWORDS = {
    "payment", "transfer", "deposit", "expense", "invoice", "online",
    "travel", "hotel", "flight", "reimbursement",
}


def _counterparty_keys(description: str) -> set[str]:
    """Deterministic counterparty tokens: lowercase words of >=4 letters."""
    return {w for w in _WORD.findall(str(description).lower()) if w not in _STOPWORDS}


def check_unlinked_reimbursables(txs: list[dict[str, Any]], period: Period) -> CheckResult:
    """Stale unlinked reimbursables that look paired with an in-period deposit.

    A reimbursable row with ``reimbursement_link IS NULL`` older than 30 days
    (relative to period end) combined with any in-period deposit sharing a
    counterparty token in its description is a likely un-linked reimbursement —
    it would inflate income (the deposit) without the offsetting link.
    """
    end = date.fromisoformat(period.end)
    stale_cutoff = (end - timedelta(days=REIMBURSABLE_STALE_DAYS)).isoformat()

    stale = [
        tx
        for tx in txs
        if tx.get("direction") == "reimbursable"
        and tx.get("reimbursement_link") is None
        and str(tx.get("date", ""))[:10] <= stale_cutoff
    ]
    deposits = [
        tx for tx in txs if period.contains(tx.get("date", "")) and _dec(tx.get("amount")) > 0
    ]

    pairs: list[str] = []
    for r in stale:
        r_keys = _counterparty_keys(str(r.get("description", "")))
        for d in deposits:
            if r_keys & _counterparty_keys(str(d.get("description", ""))):
                pairs.append(
                    f"  reimbursable id={r.get('id')} date={r.get('date')}"
                    f" amount={_dec(r.get('amount'))} desc={str(r.get('description'))[:40]!r}"
                    f" <-> deposit id={d.get('id')} date={d.get('date')}"
                    f" amount={_dec(d.get('amount'))} desc={str(d.get('description'))[:40]!r}"
                )
    return CheckResult(
        "REQ-BNO-CHK-002",
        "unlinked reimbursables vs in-period deposits",
        passed=not pairs,
        details=pairs,
    )


# ── REQ-BNO-CHK-003 ────────────────────────────────────────────────────────


def check_confirmed_only(txs: list[dict[str, Any]], period: Period) -> CheckResult:
    """Every in-period income-category row must be human/auto CONFIRMED."""
    bad: list[str] = []
    for tx in txs:
        if not period.contains(tx.get("date", "")):
            continue
        if tx.get("tax_category") not in INCOME_CATEGORIES:
            continue
        if tx.get("status") in ("auto_classified", "needs_review"):
            bad.append(
                f"  id={tx.get('id')} date={tx.get('date')} amount={_dec(tx.get('amount'))}"
                f" category={tx.get('tax_category')} status={tx.get('status')}"
                f" confidence={tx.get('confidence')}"
            )
    return CheckResult(
        "REQ-BNO-CHK-003",
        "confirmed-only gate on income rows",
        passed=not bad,
        details=bad,
    )


# ── REQ-BNO-CHK-004 ────────────────────────────────────────────────────────


def check_refund_sweep(txs: list[dict[str, Any]], period: Period) -> CheckResult:
    """Total the period's refund/chargeback outflows (always printed).

    This is the P3-302 manual returns-and-allowances deduction: refunds are
    booked as OTHER_EXPENSE and are NOT netted against Retailing gross in the
    B&O export — apply this total manually when filing with DOR. Informational:
    PASS regardless of the total.
    """
    total = Decimal("0")
    rows: list[str] = []
    for tx in txs:
        if not period.contains(tx.get("date", "")):
            continue
        amt = _dec(tx.get("amount"))
        if amt >= 0:
            continue
        if not REFUND_PATTERN.search(str(tx.get("description", ""))):
            continue
        total += abs(amt)
        rows.append(
            f"  id={tx.get('id')} date={tx.get('date')} amount={amt}"
            f" source={tx.get('source')} desc={str(tx.get('description'))[:60]!r}"
        )
    header = (
        f"  P3-302 manual returns-and-allowances deduction for {period.label}:"
        f" ${total:.2f} ({len(rows)} row(s))"
    )
    return CheckResult(
        "REQ-BNO-CHK-004",
        "refund sweep (informational)",
        passed=True,
        details=[header, *rows],
    )


# ── REQ-BNO-CHK-005 ────────────────────────────────────────────────────────


def check_rate_tier(txs: list[dict[str, Any]], period: Period) -> CheckResult:
    """Prior-calendar-year ServiceOther gross receipts must be under $1M.

    Crossing the ESSB 2081 tier makes both the 1.5% rate and DOR line code 40
    wrong — the filing must switch tiers, so fail loudly.
    """
    prior_year = str(period.year - 1)
    total = Decimal("0")
    for tx in txs:
        cat = str(tx.get("tax_category") or "")
        if BO_CLASSIFICATION.get(cat, ("", ""))[0] != "ServiceOther":
            continue
        if not str(tx.get("date", "")).startswith(prior_year):
            continue
        total += pretax_abs_amount(tx)
    passed = total < SERVICE_TIER_THRESHOLD
    details = []
    if not passed:
        details.append(
            f"  {prior_year} ServiceOther gross ${total:.2f} >= $1,000,000 —"
            " the 1.5% rate and DOR code 40 no longer apply (ESSB 2081 tiers);"
            " re-derive the rate/code before filing."
        )
    return CheckResult(
        "REQ-BNO-CHK-005",
        f"rate-tier assert ({prior_year} ServiceOther ${total:.2f} < $1M)",
        passed=passed,
        details=details,
    )


# ── REQ-BNO-CHK-006 ────────────────────────────────────────────────────────


def check_locality_mapping(
    txs: list[dict[str, Any]], period: Period, entity: str
) -> CheckResult:
    """Every in-period WA retail row's locality must resolve in WA_LOCATION_CODES.

    Uses ``retail_facts`` (src/export/basis.py) — the same predicate whose
    unmapped sentinel makes ``generate_dor_upload`` raise the REQ-FIX-TAX-007
    ValueError. Catch it here, before upload generation. Entities with no
    in-period retail rows pass trivially.
    """
    bad: list[str] = []
    for tx in txs:
        if tx.get("tax_category") not in RETAIL_CATEGORIES:
            continue
        if not period.contains(tx.get("date", "")):
            continue
        facts = retail_facts(tx)
        if facts.is_wa and facts.location_code == UNKNOWN_WA_LOCATION[0]:
            bad.append(
                f"  id={tx.get('id')} date={tx.get('date')} amount={_dec(tx.get('amount'))}"
                f" locality={facts.location_name!r} — add to WA_LOCATION_CODES"
                " (src/export/basis.py) before generating the DOR upload"
            )
    return CheckResult(
        "REQ-BNO-CHK-006",
        f"WA locality mapping ({entity})",
        passed=not bad,
        details=bad,
    )


# ── orchestration ──────────────────────────────────────────────────────────


def run_checks(txs: list[dict[str, Any]], period: Period, entity: str) -> list[CheckResult]:
    return [
        check_sign_vs_direction(txs, period),
        check_unlinked_reimbursables(txs, period),
        check_confirmed_only(txs, period),
        check_refund_sweep(txs, period),
        check_rate_tier(txs, period),
        check_locality_mapping(txs, period, entity),
    ]


def load_transactions(db_path: Path, entity: str) -> list[dict[str, Any]]:
    """Load all non-rejected rows for the entity from a READ-ONLY connection."""
    uri = f"file:{db_path}?immutable=1&mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "SELECT id, source, date, description, amount, entity, direction,"
            " tax_category, status, confidence, reimbursement_link, raw_data"
            " FROM transactions WHERE entity = ? AND status != 'rejected'",
            (entity,),
        )
        out: list[dict[str, Any]] = []
        for row in cur:
            d = dict(row)
            raw = d.get("raw_data")
            if isinstance(raw, str):
                try:
                    d["raw_data"] = json.loads(raw)
                except ValueError:
                    d["raw_data"] = {}
            out.append(d)
        return out
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic B&O pre-filing checklist.")
    parser.add_argument("--entity", required=True, choices=["sparkry", "blackline"])
    parser.add_argument("--period", required=True, help="YYYY-MM (monthly) or YYYY-QN (quarterly)")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite register path (read-only)")
    args = parser.parse_args(argv)

    try:
        period = parse_period(args.period)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 2

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: database not found: {db_path}")
        return 2

    try:
        txs = load_transactions(db_path, args.entity)
    except sqlite3.Error as exc:
        print(f"ERROR: cannot read {db_path}: {exc}")
        return 2

    print(
        f"B&O pre-filing checklist — entity={args.entity} period={period.label}"
        f" ({period.start}..{period.end}) rows={len(txs)}"
    )
    results = run_checks(txs, period, args.entity)
    any_fail = False
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        any_fail = any_fail or not r.passed
        print(f"{status}  {r.req_id}  {r.name}")
        for line in r.details:
            print(line)
    print("RESULT: " + ("BLOCKED — do not file yet" if any_fail else "clear to file"))
    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())
