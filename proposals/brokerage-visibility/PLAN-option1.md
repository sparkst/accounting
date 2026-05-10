# PLAN — Option 1: CLI Brokerage Summary Report (v3 — post round-2)

**Project:** 004-brokerage-summary
**Source:** `mempalace drawer_accounting_decisions_5369d59408ad6bf84046973c`
**Strategic decision (user-confirmed, option C):** Fix Phase 1 adapter defects discovered during review AND keep defensive filters in the report. Both changes are required.

**Round 1 review:** `REVIEW-option1-round1.md` (resolved in v2).
**Round 2 review:** 3 reviewers re-ran clean-context. R3 queried live DB and found 3 NEW P0 ingestion defects. R1 + R2 found tactical plan-correctness issues. v3 incorporates all P0/P1/meaningful-P2 from rounds 1 + 2.

---

## Strategic findings from review (NEW — added in v3)

Round-2 R3 surfaced these by querying `data/accounting.db`:

| Defect | Magnitude | Location | Status |
|---|---|---|---|
| E*TRADE "TOTAL" summary row in `position_snapshot` | $3.9M overstatement | etrade_csv.py | Filter exists in current code (line 367) — stale DB row; cleanup + re-ingest |
| E*TRADE "Generated at May 4 2026..." metadata rows | NULL market_value (cosmetic but garbage symbol) | etrade_csv.py | NO filter exists — needs symbol-shape validation |
| Vanguard 65344815 duplicate (account, symbol, as_of) rows | $176k overstatement | vanguard_csv.py | Needs root-cause investigation + fix |
| 3 NULL-symbol Vanguard funds collapsed by `(account_id, symbol)` group-by | $165k inflation | report subquery | Plan defect — `COALESCE(symbol, description)` |
| FDRXX**, FCASH** (`**` suffix) | $6.5k miscategorized as equity | report cash-sleeve list | LIKE pattern instead of exact match |
| SWTXX, VMSXX (real money market) missing from cash list | $275k miscategorized | report cash-sleeve list | Expand list |

---

## File layout

```
src/reports/
  __init__.py
  brokerage_summary.py          # pure functions + render_report
  test_brokerage_summary.py     # co-located, picked up by `pytest src/`
src/adapters/
  etrade_csv.py                 # PATCH: skip metadata rows (Generated at..., non-ticker symbols)
  vanguard_csv.py               # PATCH: dedupe within file
  test_etrade_csv.py            # NEW failing tests for "Generated at" + TOTAL skipping
  test_vanguard_csv.py          # NEW failing tests for within-file dedup
scripts/
  brokerage_summary.py          # CLI shim with PROJECT_ROOT sys.path setup
  cleanup-stale-position-snapshots.py  # one-shot DELETE for invalid rows; re-ingest after
```

`scripts/brokerage_summary.py` shim follows `scripts/weekly-pl-report.py` pattern (PROJECT_ROOT on sys.path) so direct invocation works.

---

## Acceptance criteria

1. `python scripts/brokerage_summary.py` runs against `data/accounting.db` and prints a structured report in under 2 seconds. Exit 0 on success.
2. Report sections, in order:
   - **Net worth summary**: total + per-broker subtotal. Excludes `is_plan_wrapper=True` accounts. Shows `as_of` date range across snapshots. Footer note: number of plan-wrapper accounts excluded.
   - **Accounts**: one row per account with broker, masked account number, name, type, `tax_sheltered` flag, latest `as_of`, market value. Sorted by market_value desc. **Plan-wrapper accounts visible but flagged (e.g. `[wrapper]`) and not in the running total.** Accounts with no snapshots appear in a separate "Awaiting snapshot data" sub-section.
   - **Top holdings (default 10)**: aggregated using `_latest_position_snapshots` helper that partitions by `(account_id, COALESCE(symbol, description))` so NULL-symbol funds with different descriptions are distinct. Filters `market_value > 0`. Cash sleeves (CASH, plus LIKE patterns: `SPAXX%`, `FDRXX%`, `FCASH%`, `VMFXX%`, `VMSXX%`, `SWVXX%`, `SWLXX%`, `SWTXX%`, `MMDA1%` — case insensitive) folded into one "Cash" row. Folding happens BEFORE top-N truncation. NULL-symbol non-cash positions stay in table labeled by description.
   - **Recent transactions (default 14 days)**: filtered `status != REJECTED`, `is_synthetic=False`, suppressing reinvest partner where `paired_transaction_id IS NOT NULL AND canonical_action = CanonicalAction.REINVEST.value`. Sorted by trade_date desc.
   - **Realized G/L by year**: see TASK-07 priority chain. `gain_loss` is broker-pre-adjusted; used directly for steps 2-4. Step 1 (`lt_gain_loss`/`st_gain_loss` non-NULL) uses those columns.
   - **Footer — Wash sales**: if `wash_sale_lots > 0`: "N lots flagged wash_sale, total disallowed_loss: $X.XX". Else: "No wash sales in ingested data (1099-B substantiation not yet ingested)" — distinguishes data absence from confirmed zero.
   - **Footer — Data integrity**: total accounts, transactions, position_snapshots, realized_gain_loss; orphan counts; **suspect-symbol count** (rows with symbol matching summary patterns like 'TOTAL' or starting with 'Generated' — should be zero post-cleanup).
3. Tie-out: `compute_net_worth().total == sum(account_summary.market_value WHERE as_of IS NOT NULL AND NOT is_plan_wrapper)`. Tested explicitly in TASK-04b.
4. Account numbers masked via single helper `_mask_account_number(s)`. `Z23257759` → `****7759`.
5. Empty-DB case: prints "No brokerage data ingested yet". Every pure function returns empty container, no crash, no division by zero.
6. Read-only: tests assert `not session.dirty and not session.new and not session.deleted` after every pure-function call.
7. `--db /tmp/no-such-dir/x.db` exits non-zero with stderr error.

---

## Adapter fix scope (NEW in v3)

### A. etrade_csv.py — symbol-shape validation
- After existing TOTAL filter, add: skip rows where `symbol` matches any of: `^Generated `, contains spaces, exceeds 12 chars (longest legit ticker is ~8 chars including class suffix), is exclusively non-alphanumeric. This is conservative — known false-positives are zero in current data.
- Test: feed CSV containing `Generated at May 4 2026 02:47 PM ET` row; assert it is NOT stored.

### B. vanguard_csv.py — within-file dedup
- Investigation step: query DB to identify exact duplicate pattern (same `(account_id, symbol, as_of)`, different `source_row_hash`). Likely cause: file contains both consolidated-position rows AND lot-level rows for the same holding.
- Fix: detect via per-file in-memory map keyed by `(account_id, symbol_or_description, as_of)`; skip second occurrence.
- Alternative fix if root cause is broker-emits-two-rows-per-position: dedupe by description-prefix.
- Test: feed CSV with two rows representing the same position; assert one snapshot row stored, not two.

### C. cleanup-stale-position-snapshots.py
- One-shot script. DRY-RUN by default; `--apply` to commit. Deletes:
  - `symbol = 'TOTAL'`
  - `symbol LIKE 'Generated %'`
  - Vanguard duplicates: keeps min(`id`) per `(account_id, symbol_or_description, as_of)`; deletes the rest.
- Logs deleted row counts and source_files. Travis re-runs `python scripts/ingest-brokerage.py data/brokerage/` after.
- Test: seed bad rows in fixture DB; run cleanup; assert correct rows deleted, others preserved.

### D. Defensive belt-and-suspenders in report layer
Even after adapters are fixed, the report's `_latest_position_snapshots` subquery applies these guards (so any future bad-row leakage doesn't silently skew totals):
- `symbol NOT IN ('TOTAL')`
- `symbol NOT LIKE 'Generated %'`
- `market_value IS NOT NULL`
- Partition by `(account_id, COALESCE(symbol, description))` with tie-breaker `MIN(id)` to handle accidental same-`as_of` dupes deterministically.

---

## Tasks (TDD — RED phase confirmed before any implementation)

**TDD protocol per task:** write test → run → confirm `ImportError`/`AttributeError`/assertion failure → implement → green. Each TASK includes "Confirm RED:".

### TASK-01 — Test fixture builder
**File:** `src/reports/test_brokerage_summary.py`
**SP:** 3

In-memory SQLite. Snapshot `as_of` values pinned for determinism (relative to `datetime(2026,5,6,12,0,0)` injected via fixture).

| # | Broker | acct # | type (enum) | entity | tax_sheltered | is_plan_wrapper | parent | snapshots (as_of, market_value) |
|---|---|---|---|---|---|---|---|---|
| A1 | Fidelity | Z23257759 | tod | personal | F | F | — | (today, AAPL=$10000), (today, FDRXX**=$200) |
| A2 | Schwab | 12345678 | trad_ira | sparkry | T | F | — | (today, VTI=$5000), (today, VWO=$3000), (today, VOO=$2500), (today, BND=$1000), (today, SWTXX=$300), (today, SPAXX=$200) |
| A3 | E*TRADE | 87654321 | taxable | blackline | F | F | — | (today, MSFT=$4000), (today, GOOGL=$3500), (today, TSLA=$2000), (today, VMFXX=$100), (today, VMSXX=$50), (today, [NULL-symbol description="Vanguard 500 Index Portfolio"]=$500), (today, OLDPOS qty=0 mkt=0), (today-30, MSFT=$3500), (today-30, GOOGL=$3000) |
| A4 | Fidelity | 89766 | 401k | personal | T | **T** | — | (today, [NULL-symbol description="BROKERAGELINK"]=$50000) |
| A5 | Fidelity | 653373015 | brokeragelink | personal | T | F | A4.id | (today, VTSAX=$40000), (today, VTIAX=$10000) |
| A6 | Vanguard | 32628019 | trad_ira | personal | T | F | — | none |
| A7 | Schwab | 99999999 | taxable | personal | F | F | — | (today-10, JNK=$500) — **stale snapshot** |
| A8 | Vanguard | DUPTEST | taxable | personal | F | F | — | seeded with TWO rows for (MGK, today, $63166.16) — different source_row_hash. Tests dedup-on-query. |
| A9 | Vanguard | NULLDUP | taxable | personal | F | F | — | THREE rows with NULL symbol, different descriptions, same as_of: ("Fund A"=$30k), ("Fund B"=$20k), ("Fund C"=$10k). Tests COALESCE-based partitioning. |
| A10 | E*TRADE | BADSYM | taxable | personal | F | F | — | one row with `symbol='TOTAL'`, market_value=$3000000 — tests defensive filter |

Transactions:
- T1: A1 BUY AAPL trade_date=today-1, canonical_action=CanonicalAction.BUY.value, status=IMPORTED
- T2: A1 SELL AAPL trade_date=today-2, canonical_action=CanonicalAction.SELL.value, status=IMPORTED
- T3: A2 dividend trade_date=today-3, canonical_action=CanonicalAction.DIVIDEND_ORDINARY.value, status=IMPORTED, paired_id=T4.id
- T4: A2 reinvest trade_date=today-3, canonical_action=**CanonicalAction.REINVEST.value**, status=IMPORTED, paired_id=T3.id, is_synthetic=False
- T5: A2 BUY ZZZ trade_date=today-30, status=IMPORTED — out of 14-day window
- T6: A3 dividend trade_date=today-1, canonical_action=CanonicalAction.DIVIDEND_ORDINARY.value, status=**REJECTED** — must NOT appear
- T7: A3 SELL OLDPOS trade_date=today-1, status=IMPORTED — recent activity but qty-0 latest snap

Realized G/L (5 rows on A1+A2+A3):
- A1: AAPL closed 2024-06-15, opened 2024-01-15, gain_loss=+$500, lt_gain_loss=NULL, st_gain_loss=$500, term='short', wash_sale=False
- A2: VTI closed 2024-12-20, opened 2023-01-15, gain_loss=+$1200, lt_gain_loss=$1200, st_gain_loss=NULL, term='long'
- A2: SPLIT_LOT closed 2024-08-01, opened 2024-02-01 (mixed lot), gain_loss=$900, lt_gain_loss=$700, st_gain_loss=$200, term='long' — exercises step 1 with both non-NULL
- A2: WASH closed 2025-01-10, opened 2024-11-15, gain_loss=-$100 (broker pre-adjusted), disallowed_loss=$50, wash_sale=True, term='short'
- A3: ANCIENT closed 2025-03-05, opened=NULL, term=NULL, lt_gain_loss=NULL, st_gain_loss=NULL, gain_loss=$50 → "unknown" bucket

**Asserts:** all rows commit; session clean; counts: 10 accounts, 7 transactions, 28 position_snapshots (incl. duplicates, the TOTAL row, the Generated-at row, and extra NULL/NULL + UNIQUE_X fixture rows added in P1-B/H), 6 realized lots (incl. INFDATE date-inference lot added in P1-F).

**Confirm RED:** test imports fixture builder → ImportError before file exists.

---

### TASK-02 — Helper: `_latest_position_snapshots(session)` (failing)
**SP:** 3

Returns a SQLAlchemy subquery: per `(account_id, COALESCE(symbol, description))`, the row with `MAX(as_of)` and ties broken by `MIN(id)`. Defensive filters applied:
- `symbol NOT IN ('TOTAL')` (case-insensitive: `func.upper(symbol) != 'TOTAL'`)
- `symbol IS NULL OR symbol NOT LIKE 'Generated %'`
- `market_value IS NOT NULL`

**Asserts:**
- A8 (DUPTEST, MGK with two rows): exactly ONE row returned, total = $63166.16 (not $126,332).
- A9 (NULLDUP, 3 NULL-symbol rows different descriptions): all THREE rows returned (because COALESCE distinguishes them by description).
- A10 (BADSYM, TOTAL row): NOT returned.
- A3's older MSFT snapshot (today-30) NOT returned; latest (today, $4000) returned.
- Read-only invariant.

**Confirm RED:** ImportError.

---

### TASK-03 — Pure-function: `compute_net_worth(session)` (failing)
**SP:** 2

Returns:
```python
{
  "total": Decimal,
  "by_broker": {"fidelity": Decimal, "schwab": Decimal, "etrade": Decimal, "vanguard": Decimal},
  "by_entity": {"personal": Decimal, "sparkry": Decimal, "blackline": Decimal},  # consumed by Option 2/3 renderer
  "as_of_min": date | None,
  "as_of_max": date | None,
  "zero_snapshot_account_count": int,
  "plan_wrapper_excluded_count": int,
}
```
Logic: `_latest_position_snapshots` joined to Account, sum `market_value`, EXCLUDE `is_plan_wrapper=True`.

**Asserts (with pinned numbers based on TASK-01 fixture):**
- A4 wrapper $50,000 NOT in totals.
- A8 DUPTEST contributes $63,166.16 (not $126k).
- A9 NULLDUP contributes $60,000 (sum of three distinct NULL-symbol rows).
- A10 BADSYM TOTAL row NOT in totals.
- `by_broker['fidelity']` = sum(A1 latest) + sum(A5 latest) = $10,200 + $50,000 = $60,200.
- `by_broker['schwab']` = sum(A2 latest) + A7 latest = $12,000 + $500 = $12,500.
- `by_broker['etrade']` = sum(A3 latest where mkt > 0) = $4,000+$3,500+$2,000+$100+$50+$500 = $10,150.
- `by_broker['vanguard']` = $63,166.16 (A8) + $60,000 (A9) = $123,166.16. (A6 has none.)
- `by_entity['personal']` = $60,200 + $500 + $123,166.16 = $183,866.16.
- `by_entity['sparkry']` = $12,000.
- `by_entity['blackline']` = $10,150.
- `total` = $206,016.16. Tests exact value.
- `as_of_max` = today; `as_of_min` = today-10 (A7 stale).
- `zero_snapshot_account_count` = 1 (A6).
- `plan_wrapper_excluded_count` = 1 (A4).
- Empty DB → all zeros, no crash.
- Read-only invariant.

**Confirm RED:** ImportError.

---

### TASK-04 — Pure-function: `get_account_summary(session)` (failing)
**SP:** 2

Returns sorted list (market_value desc; plan-wrappers tagged but NOT excluded so report can show them):
```python
[{"account_id", "broker", "account_number_masked", "account_name", "account_type",
  "entity", "tax_sheltered", "is_plan_wrapper", "as_of", "market_value"}]
```
- `account_number_masked` via shared `_mask_account_number(s)`.
- A6 (no snaps): `as_of=None, market_value=0`.

**Asserts:**
- All 10 accounts present.
- A1 `Z23257759` → `****7759`.
- `_mask_account_number("12")` → `****`.
- A8 market_value = $63,166.16 (deduplicated).
- A6 row has `as_of=None`.
- A4 wrapper has `is_plan_wrapper=True`.
- Read-only invariant.

---

### TASK-04b — Tie-out integration test (failing)
**SP:** 1

Calls both `compute_net_worth` and `get_account_summary` against the fixture; asserts:
```python
assert compute_net_worth(session)["total"] == sum(
    a["market_value"] for a in get_account_summary(session)
    if a["as_of"] is not None and not a["is_plan_wrapper"]
)
```

**Confirm RED:** test exists before TASK-03/TASK-04 are green; fails with ImportError.

---

### TASK-05 — Pure-function: `get_top_holdings(session, net_worth_total: Decimal, n: int | None = 10)` (failing)
**SP:** 3

Returns top-N by market_value desc. Cash sleeves folded BEFORE truncation. NULL-symbol non-cash positions stay labeled by description.

Cash-sleeve identification: case-insensitive ticker match against any of:
```
{"CASH"} ∪ LIKE-prefix patterns: SPAXX, FDRXX, FCASH, VMFXX, VMSXX, SWVXX, SWLXX, SWTXX, MMDA1
```
Implementation: `func.upper(symbol).op('GLOB')` or sequence of `LIKE` clauses.

`pct_of_net_worth = market_value / net_worth_total` (denominator passed in).

**Asserts:**
- AAPL, VTI, MSFT, etc. present individually.
- Cash row = $200 (FDRXX**) + $300 (SWTXX) + $200 (SPAXX) + $100 (VMFXX) + $50 (VMSXX) = $850 — folded BEFORE n=10 truncation.
- A9 NULLDUP rows: 3 separate rows labeled by description (Fund A, B, C), NOT folded into Cash.
- A4 wrapper's NULL-symbol BROKERAGELINK: NOT in results (wrapper excluded).
- A10 TOTAL: NOT in results (defensive filter).
- A8 MGK: deduplicated to $63,166.16.
- OLDPOS (qty 0, mkt 0): absent.
- `n=3`: 3 rows returned; cash still folded (folding is pre-truncation).
- `n=None`: all positions returned; sum(pct) ≈ 1.0 within Decimal('0.01').
- Empty DB / `net_worth_total=Decimal(0)` → `[]`, no ZeroDivisionError.
- Read-only invariant.

---

### TASK-06 — Pure-function: `get_recent_transactions(session, days=14)` (failing)
**SP:** 1

Filters:
- `trade_date >= date.today() - timedelta(days=days)` (local time)
- `status != BrokerageTxStatus.REJECTED.value`
- `is_synthetic = False`
- NOT (`canonical_action = CanonicalAction.REINVEST.value AND paired_transaction_id IS NOT NULL`)
- Excludes plan-wrapper accounts

**Asserts:**
- T1, T2, T3 (dividend) appear.
- T4 (reinvest partner) suppressed.
- T5 (today-30) excluded by 14-day window.
- T6 (REJECTED) excluded.
- `--days 60` → T5 included.
- Read-only invariant.

---

### TASK-07 — Pure-function: `get_realized_gl_summary(session)` (failing)
**SP:** 2

Returns ONE dict (not two — disambiguated):
```python
{
  "by_year": {2024: {"short_term": Decimal, "long_term": Decimal,
                     "unknown": Decimal, "total": Decimal, "lots": int}, ...},
  "wash_sales": {"lots": int, "total_disallowed_loss": Decimal},
}
```

ST/LT priority chain (explicit):
1. If `lt_gain_loss IS NOT NULL` OR `st_gain_loss IS NOT NULL`: use those columns (LT gets `lt_gain_loss or 0`; ST gets `st_gain_loss or 0`). Lot counts in whichever bucket(s) are non-zero.
2. Else if `term = 'long'`: use `gain_loss` as LT amount.
3. Else if `term = 'short'`: use `gain_loss` as ST amount.
4. Else if both `opened_date` and `closed_date` non-NULL: `(closed - opened).days > 365` → LT, else ST. Amount = `gain_loss`.
5. Else: bucket as "unknown". Amount = `gain_loss`.

`gain_loss` is broker-pre-adjusted; `disallowed_loss` reported separately, NOT subtracted from gain_loss.

**Asserts (against fixture):**
- 2024 ST = $500 (AAPL via step 1, st_gain_loss=$500) + $200 (SPLIT_LOT step 1, st_gain_loss=$200) = $700.
- 2024 LT = $1200 (VTI step 1) + $700 (SPLIT_LOT step 1, lt_gain_loss=$700) = $1900.
- 2024 lots = 3 (AAPL, VTI, SPLIT_LOT).
- 2025 ST = -$100 (WASH via step 3, term='short', gain_loss=-$100).
- 2025 unknown = $50 (ANCIENT, all NULL → step 5).
- 2025 lots = 2.
- `wash_sales.lots = 1`, `wash_sales.total_disallowed_loss = Decimal("50")`.
- Empty DB → empty dicts, zero counts.
- Read-only invariant.

---

### TASK-08 — Pure-function: `compute_data_integrity(session)` (failing)
**SP:** 2

Returns:
```python
{
  "accounts": int,
  "transactions": int,
  "position_snapshots": int,
  "realized_lots": int,
  "orphan_transactions": int,        # FK to nonexistent account
  "orphan_snapshots": int,
  "stale_snapshot_accounts": int,    # latest as_of < today - 7d
  "suspect_symbols": int,            # symbol IN ('TOTAL') OR LIKE 'Generated %'
  "duplicate_position_groups": int,  # (account_id, COALESCE(symbol,description), as_of) groups with COUNT > 1
}
```
Comparing `as_of` (DateTime) to date threshold uses `func.date(PositionSnapshot.as_of)` to coerce.

**Asserts (fixture):**
- accounts=10, transactions=7, position_snapshots=28, realized_lots=6.
- orphan_transactions=0, orphan_snapshots=0.
- stale_snapshot_accounts=1 (A7 only — A8/A9 latest is today).
- suspect_symbols=2 (A10 TOTAL + A10 Generated-at row).
- duplicate_position_groups=1 (A8 MGK has 2 rows). A9's NULL-symbol rows have DIFFERENT descriptions so don't count as duplicates.
- Read-only invariant.

---

### TASK-09 — Renderer: `render_report(data: dict) -> str` (failing)
**SP:** 2

Pure function. ASCII tables. Stdlib only. `Decimal` quantized to `0.01` with `ROUND_HALF_UP` then formatted `${:,.2f}`.

Section order: Net Worth Summary → Accounts → Awaiting Snapshot Data (only if any) → Top Holdings → Recent Transactions → Realized G/L by Year → Wash Sales footer → Data Integrity footer.

**Asserts:**
- All section headers present.
- Net worth total appears.
- Empty data dict → "No brokerage data ingested yet".
- `Decimal('12345.678')` formatted → `$12,345.68`.
- At least one broker name and its subtotal value appear in Net Worth Summary.
- Wash sales footer when `lots > 0`: includes lot count and disallowed_loss.
- Wash sales footer when `lots == 0`: prints "No wash sales in ingested data (1099-B substantiation not yet ingested)".
- Plan-wrapper exclusion note appears in Net Worth Summary.
- Awaiting-snapshot section absent if no zero-snapshot accounts.
- Suspect-symbols count appears in Data Integrity footer; non-zero → printed as warning line.
- Duplicate-position-groups count: same pattern.

---

### TASK-10 — Wire-up + shim
**SP:** 2

`src/reports/brokerage_summary.py::main()`:
- argparse: `--top N` (default 10), `--days N` (default 14), `--db PATH` (default `data/accounting.db`), `--stale-days N` (default 7).
- Flow: open session → compute_net_worth → get_account_summary → get_top_holdings(net_worth_total=nw['total']) → get_recent_transactions → get_realized_gl_summary → compute_data_integrity → render_report → print.
- On `OperationalError`: print to stderr, exit 1.

`scripts/brokerage_summary.py` (shim):
```python
#!/usr/bin/env python3
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from src.reports.brokerage_summary import main
if __name__ == "__main__":
    main()
```

**Integration asserts:**
- Run against fixture DB → all sections present.
- `--top 3` truncates holdings to 3.
- `--days 1` truncates transactions.
- `--db /tmp/no-such-dir/x.db` → exit code != 0 with stderr error.
- Performance: `time.monotonic()` end-to-end < 2.0s on fixture.
- Direct invocation `python scripts/brokerage_summary.py` works (subprocess).
- Module invocation `python -m scripts.brokerage_summary` works (subprocess).

---

### TASK-11 — Adapter fix: etrade_csv.py "Generated at" filter (failing test → green)
**File:** `src/adapters/test_etrade_csv.py`
**SP:** 2

Add failing test: feed CSV with header + `Generated at May 4 2026 02:47 PM ET` row + valid AAPL row. Assert only AAPL stored.

Then patch `_process_positions`: after existing `TOTAL` skip, add:
```python
# Skip metadata rows: must look like a ticker (alphanumeric + optional ** suffix, ≤ 12 chars).
if not re.match(r'^[A-Z0-9]{1,8}(\*\*)?$', symbol.upper()):
    continue
```

**Asserts:** new test green; existing etrade tests still green (regression).

---

### TASK-12 — Adapter fix: vanguard_csv.py within-file dedup (failing test → green)
**File:** `src/adapters/test_vanguard_csv.py`
**SP:** 3

Investigation step (one-time, document findings inline in commit message): query DB to identify the exact duplicate pattern. Suspected: brokerage adapter ingests both consolidated-position rows AND lot-level rows from the same Vanguard CSV. Or: file has accidental duplicate rows. Or: the adapter ingests positions twice (once from each section of a multi-section file).

Failing test: feed minimal CSV that reproduces the duplicate pattern. Assert only ONE PositionSnapshot stored per `(account, symbol, as_of)` after ingest.

Fix: per-file in-memory `seen_keys: set[(account_id, symbol_or_description, as_of_iso)]` populated as rows are processed; skip on collision and log warning.

**Asserts:** failing test now green; existing Vanguard tests still green; live re-ingest produces zero new snapshot rows (idempotent + dedup).

---

### TASK-13 — Cleanup script
**File:** `scripts/cleanup-stale-position-snapshots.py`
**SP:** 2

DRY-RUN by default. Logs would-delete rows; `--apply` actually deletes.

Targets:
- `symbol = 'TOTAL'` (case-insensitive)
- `symbol LIKE 'Generated %'`
- Duplicate `(account_id, COALESCE(symbol,description), as_of)` groups: keep `MIN(id)`, delete others.

**Test (in `scripts/test_cleanup_stale_position_snapshots.py`):** seed bad rows; run cleanup; assert correct rows deleted, others preserved; `--dry-run` makes no changes.

---

### TASK-14 — Live execution sequence
**SP:** 2

In order:
1. Quality gates green: `pytest src/`, `ruff check src/ scripts/`, `mypy src/reports/ src/adapters/etrade_csv.py src/adapters/vanguard_csv.py`.
2. Run `python scripts/cleanup-stale-position-snapshots.py` (dry-run, observe output).
3. Run `python scripts/cleanup-stale-position-snapshots.py --apply`.
4. Re-ingest: `python scripts/ingest-brokerage.py data/brokerage/` (idempotent — should add zero new rows on already-clean data, or restore the rows that the cleanup removed if they're legitimate (which they aren't — cleanup only deletes the TOTAL/Generated/dup rows that fixed adapters now skip).
5. Run `python scripts/brokerage_summary.py` against live DB, capture to `reports/brokerage-summary-smoke.txt`.
6. Inspect output:
   - Net worth: in $6.5M–$7M range (was naïvely $14.8M; corrected after all fixes).
   - All 14 accounts visible; 3 Vanguard "awaiting snapshot data".
   - Cash row consolidates SPAXX/FDRXX**/VMFXX/VMSXX/SWTXX.
   - Top holdings show real positions, no TOTAL/Generated.
   - Realized G/L 2024 non-zero.
   - Data integrity: 14 accounts, ~1978 transactions, fewer than 57 snapshots (some deleted), 407 lots; suspect_symbols=0; duplicate_position_groups=0.
7. Run twice; assert idempotent (zero new rows on second run).

---

## Dependencies / order

01 → (02 in parallel with 11, 12, 13) → (03, 04, 05, 06, 07, 08 after 02) → 04b after 03+04 → 09 → 10 → 14.

## SP rollup

Total: 32 SP. (~7-8 hours TDD-style with adapter fixes.)

## Quality gates (before review-loop convergence)

- `pytest src/` — full baseline green.
- `ruff check src/ scripts/` — clean.
- `mypy src/reports/ src/adapters/etrade_csv.py src/adapters/vanguard_csv.py` — clean.
- TASK-14 smoke captured.

## Round-2 P3 nits not folded in

- Pin exact `as_of` datetimes (R2 P2-004) → folded ✓
- Pin exact by_broker / by_entity values (R2 P3-001) → folded ✓
- TASK-11 timing assertion (R2 P3-002) → punt to TASK-14 (smoke is the right place)
- Cash-folding-vs-truncation order (R2 P3-003) → folded ✓
- Generated row symbol-shape (R3 P2-001) → folded as TASK-11
- Stale threshold configurable (R3 P2-002) → folded as `--stale-days`
- E*TRADE timestamp-based dedup (R3 P2-003) → punt to Phase 1 follow-up; current dedup-by-source-row-hash already covers idempotency for same-export
