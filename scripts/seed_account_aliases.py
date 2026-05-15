#!/usr/bin/env python3
"""Seed account_alias rows for the wealth dashboard (F2 Phase B).

Walks the operator through each distinct unmatched ``raw_account_name`` from
``account_balance_snapshot`` in the Cloudflare D1 wealth database, suggests
modern ``account_id`` matches via three heuristics, and (with ``--apply``)
writes confirmed mappings to the ``account_alias`` table.

Heuristics (priority order, all run; CLI shows whichever fired):
    1. institution-token + last-4 digits
    2. balance proximity at the cutover boundary
    3. beneficiary / owner name token match

Auth: shells out to ``wrangler d1 execute sparkry-crm-prod --remote ...`` so it
uses the operator's Wrangler OAuth login. No internal-key needed for now; this
pattern matches the M0c/M0j operator-script convention.

Per CLAUDE.md "DRY-RUN default for scripts" — defaults to read-only. Pass
``--apply`` to actually write rows.

Usage:
    python -m scripts.seed_account_aliases                # dry-run
    python -m scripts.seed_account_aliases --apply        # commit
    python -m scripts.seed_account_aliases --resume       # skip already-aliased
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

WRANGLER_CMD = ["npx", "wrangler", "d1", "execute", "sparkry-crm-prod", "--remote"]
SPARKRY_CRM_DIR = "/Users/travis/SGDrive/dev/sparkry-crm"

# ── Wrangler helpers ─────────────────────────────────────────────────────────


def wrangler_query(sql: str) -> list[dict[str, Any]]:
    """Run a read-only query via wrangler and return the result rows."""
    result = subprocess.run(
        [*WRANGLER_CMD, "--command", sql, "--json"],
        capture_output=True,
        text=True,
        cwd=SPARKRY_CRM_DIR,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"wrangler failed: {result.stderr[:500]}")
    data = json.loads(result.stdout)
    return data[0]["results"] or []


def wrangler_write(sql: str) -> dict[str, Any]:
    """Run a write query via wrangler. Returns the meta block."""
    result = subprocess.run(
        [*WRANGLER_CMD, "--command", sql, "--json"],
        capture_output=True,
        text=True,
        cwd=SPARKRY_CRM_DIR,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"wrangler write failed: {result.stderr[:500]}")
    data = json.loads(result.stdout)
    return data[0]["meta"]


def sql_escape(s: str) -> str:
    """Single-quote escape for SQL string literals. Wrangler --command does
    not support parameter binding so we have to inline. Validate input shape
    upstream so this is safe."""
    return s.replace("'", "''")


# ── Data classes ──────────────────────────────────────────────────────────────


@dataclass
class Unmatched:
    raw_account_name: str
    first_seen: str
    last_seen: str
    latest_balance: str | None
    latest_source: str | None
    snapshot_count: int


@dataclass
class Candidate:
    account_id: str
    broker: str
    account_number: str
    account_name: str | None
    account_type: str
    beneficiary: str | None
    is_plan_wrapper: int
    first_position_date: str | None
    first_position_total: float | None

    @property
    def masked(self) -> str:
        if not self.account_number:
            return "?"
        n = self.account_number
        if len(n) <= 4:
            return n
        return "*" * (len(n) - 4) + n[-4:]

    @property
    def last4(self) -> str:
        digits = re.sub(r"\D", "", self.account_number or "")
        return digits[-4:] if len(digits) >= 4 else ""


@dataclass
class Suggestion:
    candidate: Candidate
    confidence: str   # 'auto-institution-last4' | 'auto-balance-match' | 'auto-name-match' | 'manual'
    score: int        # higher = better
    reason: str


# ── Heuristics ────────────────────────────────────────────────────────────────

BROKER_TOKENS = {
    "vanguard": "vanguard",
    "schwab": "schwab",
    "td": "schwab",        # Charles Schwab acquired TD Ameritrade
    "fidelity": "fidelity",
    "etrade": "etrade",
    "sharebuilder": "etrade",   # ShareBuilder → ETrade
    "templeton": "ft",          # Franklin Templeton
    "ft": "ft",
}


def institution_token(raw_name: str) -> str | None:
    """Parse raw_name for a broker token; return canonical broker string."""
    lower = raw_name.lower()
    for token, canonical in BROKER_TOKENS.items():
        if token in lower:
            return canonical
    return None


def heuristic_institution_last4(unm: Unmatched, candidates: list[Candidate]) -> list[Suggestion]:
    """Match on institution token + last-4 digits (highest confidence)."""
    inst = institution_token(unm.raw_account_name)
    # Extract last-4 from raw_name if present
    digits = re.findall(r"\d{4,}", unm.raw_account_name)
    last4 = digits[-1][-4:] if digits else None
    if not inst and not last4:
        return []
    out: list[Suggestion] = []
    for c in candidates:
        score = 0
        reasons = []
        if inst and inst.lower() == c.broker.lower():
            score += 10
            reasons.append(f"broker={c.broker}")
        if last4 and last4 == c.last4:
            score += 50
            reasons.append(f"last4={last4}")
        if score > 0:
            out.append(Suggestion(c, "auto-institution-last4", score, " + ".join(reasons)))
    return out


def heuristic_balance_proximity(unm: Unmatched, candidates: list[Candidate]) -> list[Suggestion]:
    """Match on the last unmatched balance vs earliest position_snapshot total."""
    if not unm.latest_balance:
        return []
    try:
        unm_bal = Decimal(unm.latest_balance)
    except InvalidOperation:
        return []
    if unm_bal <= 0:
        return []
    out: list[Suggestion] = []
    for c in candidates:
        if c.first_position_total is None or c.first_position_total <= 0:
            continue
        cand_bal = Decimal(str(c.first_position_total))
        # Within 10%? Score by closeness.
        diff = abs(cand_bal - unm_bal)
        ratio = diff / unm_bal if unm_bal > 0 else Decimal("1")
        if ratio < Decimal("0.10"):
            score = int(100 - float(ratio) * 1000)   # ratio 0.0 → 100; 0.1 → 0
            reason = f"unmatched={unm_bal:.0f} vs first_pos={cand_bal:.0f} (Δ{float(ratio)*100:.1f}%)"
            out.append(Suggestion(c, "auto-balance-match", max(score, 5), reason))
    return out


NAME_TOKENS = {
    # owner / beneficiary tokens
    "travis": ["travis"],
    "amy": ["amy"],
    "aiden": ["aiden"],
    "emerson": ["emerson"],
    # account-type tokens
    "ira": ["ira"],
    "roth": ["roth"],
    "529": ["529"],
    "coverdale": ["coverdell", "education"],
    "hsa": ["hsa", "health savings"],
    "401k": ["401k", "401(k)"],
    "pension": ["pension"],
    "stock": ["stock", "stocks", "individual"],
    "amzn": ["amzn", "amazon"],
    "amazon": ["amzn", "amazon"],
    "msft": ["msft", "microsoft"],
    "microsoft": ["msft", "microsoft"],
}


def heuristic_name_match(unm: Unmatched, candidates: list[Candidate]) -> list[Suggestion]:
    """Token-overlap match on legacy name vs (account_name, beneficiary)."""
    legacy_tokens = set()
    legacy_lower = unm.raw_account_name.lower()
    for key, aliases in NAME_TOKENS.items():
        if key in legacy_lower or any(a in legacy_lower for a in aliases):
            legacy_tokens.add(key)
    if not legacy_tokens:
        return []
    out: list[Suggestion] = []
    for c in candidates:
        cand_text = " ".join(filter(None, [c.account_name or "", c.beneficiary or "", c.account_type or ""])).lower()
        if not cand_text.strip():
            continue
        matched: list[str] = []
        for tok in legacy_tokens:
            if tok in cand_text or any(a in cand_text for a in NAME_TOKENS[tok]):
                matched.append(tok)
        if matched:
            score = len(matched) * 10
            reason = f"tokens={','.join(matched)}"
            out.append(Suggestion(c, "auto-name-match", score, reason))
    return out


def aggregate_suggestions(unm: Unmatched, candidates: list[Candidate]) -> list[Suggestion]:
    """Combine all three heuristics; dedupe by candidate, keep highest-scoring confidence."""
    by_id: dict[str, Suggestion] = {}
    for s in (
        *heuristic_institution_last4(unm, candidates),
        *heuristic_balance_proximity(unm, candidates),
        *heuristic_name_match(unm, candidates),
    ):
        existing = by_id.get(s.candidate.account_id)
        # Combine scores from multiple heuristics; keep the strongest confidence
        if existing is None:
            by_id[s.candidate.account_id] = s
        else:
            confidence_strength = {
                "auto-institution-last4": 3,
                "auto-balance-match": 2,
                "auto-name-match": 1,
            }
            best_conf = max(
                existing.confidence, s.confidence,
                key=lambda c: confidence_strength.get(c, 0),
            )
            existing.score += s.score
            existing.reason = f"{existing.reason} | {s.reason}"
            existing.confidence = best_conf
    return sorted(by_id.values(), key=lambda s: s.score, reverse=True)


# ── Data fetchers ────────────────────────────────────────────────────────────


def fetch_unmatched() -> list[Unmatched]:
    sql = """
        SELECT
            raw_account_name,
            MIN(substr(as_of, 1, 10)) AS first_seen,
            MAX(substr(as_of, 1, 10)) AS last_seen,
            (SELECT balance FROM account_balance_snapshot abs2
              WHERE abs2.raw_account_name = abs.raw_account_name AND abs2.account_id IS NULL
              ORDER BY abs2.as_of DESC LIMIT 1) AS latest_balance,
            (SELECT source FROM account_balance_snapshot abs3
              WHERE abs3.raw_account_name = abs.raw_account_name AND abs3.account_id IS NULL
              ORDER BY abs3.as_of DESC LIMIT 1) AS latest_source,
            COUNT(*) AS snapshot_count
        FROM account_balance_snapshot abs
        WHERE account_id IS NULL
        GROUP BY raw_account_name
        ORDER BY raw_account_name
    """
    rows = wrangler_query(sql)
    return [Unmatched(**{k: r.get(k) for k in Unmatched.__annotations__}) for r in rows]


def fetch_candidates() -> list[Candidate]:
    sql = """
        WITH first_pos_date AS (
            SELECT account_id, MIN(substr(as_of, 1, 10)) AS first_date
            FROM position_snapshot
            GROUP BY account_id
        ),
        first_pos_total AS (
            SELECT ps.account_id, fpd.first_date,
                   SUM(CAST(ps.market_value AS REAL)) AS first_total
            FROM position_snapshot ps
            JOIN first_pos_date fpd
              ON fpd.account_id = ps.account_id
             AND substr(ps.as_of, 1, 10) = fpd.first_date
            GROUP BY ps.account_id, fpd.first_date
        )
        SELECT
            a.id AS account_id,
            a.broker,
            a.account_number,
            a.account_name,
            a.account_type,
            a.beneficiary,
            a.is_plan_wrapper,
            fpt.first_date AS first_position_date,
            fpt.first_total AS first_position_total
        FROM account a
        JOIN first_pos_total fpt ON fpt.account_id = a.id
        WHERE a.is_plan_wrapper = 0
        ORDER BY a.broker, a.account_number
    """
    rows = wrangler_query(sql)
    return [Candidate(**{k: r.get(k) for k in Candidate.__annotations__}) for r in rows]


def fetch_existing_aliases() -> set[str]:
    rows = wrangler_query("SELECT raw_account_name FROM account_alias")
    return {r["raw_account_name"] for r in rows}


def insert_alias(raw_name: str, account_id: str, confidence: str, approved_by: str, notes: str | None) -> None:
    notes_sql = f"'{sql_escape(notes)}'" if notes else "NULL"
    sql = f"""
        INSERT INTO account_alias (raw_account_name, account_id, confidence, approved_by, notes)
        VALUES ('{sql_escape(raw_name)}', '{sql_escape(account_id)}', '{confidence}',
                '{sql_escape(approved_by)}', {notes_sql})
        ON CONFLICT(raw_account_name) DO NOTHING
    """
    wrangler_write(sql)


# ── CLI ───────────────────────────────────────────────────────────────────────


def fmt_money(s: str | None) -> str:
    if not s:
        return "—"
    try:
        return f"${Decimal(s):,.2f}"
    except InvalidOperation:
        return s or "?"


def print_unmatched_header(idx: int, total: int, unm: Unmatched) -> None:
    print()
    print(f"━━━ [{idx}/{total}] {unm.raw_account_name!r} ━━━")
    print(f"    Seen: {unm.first_seen} → {unm.last_seen}  ({unm.snapshot_count} snapshots, source={unm.latest_source})")
    print(f"    Latest balance: {fmt_money(unm.latest_balance)}")


def print_suggestions(suggestions: list[Suggestion]) -> None:
    if not suggestions:
        print("    (no auto-suggest matches — manual lookup needed)")
        return
    for i, s in enumerate(suggestions[:5], 1):
        c = s.candidate
        marker = "★" if i == 1 else " "
        print(f"    {marker} {i}. {c.broker:9} {c.masked:18} name={c.account_name!r:35} ben={c.beneficiary or '-':15}")
        print(f"           confidence={s.confidence}  score={s.score}  ({s.reason})")


def print_all_candidates(candidates: list[Candidate]) -> None:
    print()
    print("  Available active accounts:")
    for i, c in enumerate(candidates, 1):
        print(f"    {i:2}. {c.broker:9} {c.masked:18} name={c.account_name!r:35} ben={c.beneficiary or '-':15}")
    print()


def prompt(unm: Unmatched, suggestions: list[Suggestion], candidates: list[Candidate]) -> tuple[Suggestion | None, str] | None:
    """Return (suggestion, notes) on confirm; None on skip/quit.

    The second element of the tuple is the operator's freeform note (may be empty).
    """
    while True:
        if suggestions:
            top = suggestions[0]
            choice = input("    Confirm top suggestion? [y]es / [n]ext / [l]ist all / [#] pick by number / [s]kip-no-alias / [q]uit > ").strip().lower()
        else:
            choice = input("    No auto-suggestions. [l]ist all / [#] pick by number / [s]kip-no-alias / [q]uit > ").strip().lower()
        if choice in ("q", "quit", "exit"):
            return None
        if choice in ("s", "skip"):
            print(f"    ↪ skipped {unm.raw_account_name!r} (will stay in unmatched permanently)")
            return None
        if choice == "l":
            print_all_candidates(candidates)
            continue
        if choice in ("y", "yes", "") and suggestions:
            notes = input("    Notes (optional, press enter to skip): ").strip()
            return suggestions[0], notes
        if choice in ("n", "next") and len(suggestions) > 1:
            # rotate top suggestion off; let them see the next
            suggestions.append(suggestions.pop(0))
            print(f"    ↪ now showing #{1} (was #{len(suggestions)})")
            print_suggestions(suggestions)
            continue
        # Try parsing as number
        try:
            num = int(choice)
            if 1 <= num <= len(candidates):
                picked = candidates[num - 1]
                notes = input("    Notes (optional, press enter to skip): ").strip()
                return (
                    Suggestion(picked, "manual", 0, "operator-selected from full list"),
                    notes,
                )
        except ValueError:
            pass
        print(f"    ? unrecognized: {choice!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Commit confirmed mappings (default: dry-run).")
    parser.add_argument("--resume", action="store_true", help="Skip raw_account_names already in account_alias.")
    parser.add_argument(
        "--approved-by",
        default="travis@sparkry.com",
        help="Value for account_alias.approved_by (default: travis@sparkry.com).",
    )
    args = parser.parse_args(argv)

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"━━━ F2 account_alias seeder — mode={mode} ━━━")
    print("Loading data from prod D1 via wrangler...")

    try:
        unmatched = fetch_unmatched()
        candidates = fetch_candidates()
        existing = fetch_existing_aliases() if args.resume else set()
    except RuntimeError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1

    if existing:
        unmatched = [u for u in unmatched if u.raw_account_name not in existing]
        print(f"  {len(existing)} raw_account_names already aliased — skipped (use without --resume to revisit).")

    print(f"  {len(unmatched)} unmatched raw_account_names to process.")
    print(f"  {len(candidates)} active accounts available as match candidates.")
    if not unmatched:
        print("Nothing to do.")
        return 0

    decisions: list[dict[str, str]] = []   # for end-of-run summary

    for idx, unm in enumerate(unmatched, 1):
        suggestions = aggregate_suggestions(unm, candidates)
        print_unmatched_header(idx, len(unmatched), unm)
        if suggestions:
            print("    Auto-suggestions (best first):")
            print_suggestions(suggestions)
        result = prompt(unm, suggestions, candidates)
        if result is None:
            decisions.append({"raw_name": unm.raw_account_name, "action": "skip", "account_id": "-", "confidence": "-"})
            continue
        suggestion, notes = result
        decisions.append({
            "raw_name": unm.raw_account_name,
            "action": "confirm",
            "account_id": suggestion.candidate.account_id,
            "confidence": suggestion.confidence,
            "notes": notes,
        })
        if args.apply:
            try:
                insert_alias(
                    unm.raw_account_name,
                    suggestion.candidate.account_id,
                    suggestion.confidence,
                    args.approved_by,
                    notes or None,
                )
                print(f"    ✓ aliased '{unm.raw_account_name}' → {suggestion.candidate.account_id[:8]}.. ({suggestion.confidence})")
            except RuntimeError as exc:
                print(f"    ✗ write failed: {exc}", file=sys.stderr)
                decisions[-1]["action"] = "error"
        else:
            print(f"    [DRY-RUN] would alias '{unm.raw_account_name}' → {suggestion.candidate.account_id[:8]}.. ({suggestion.confidence})")

    # End-of-run summary
    print()
    print("━━━ SUMMARY ━━━")
    confirms = [d for d in decisions if d["action"] == "confirm"]
    skips = [d for d in decisions if d["action"] == "skip"]
    errors = [d for d in decisions if d["action"] == "error"]
    print(f"  Confirmed: {len(confirms)}")
    print(f"  Skipped:   {len(skips)}")
    if errors:
        print(f"  Errors:    {len(errors)}")
    print()
    if not args.apply:
        print("DRY-RUN complete — no rows written. Re-run with --apply to commit.")
    else:
        print(f"APPLIED — {len(confirms)} rows inserted into account_alias.")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
