# Phase 4 — Per-Institution Adapters: PLAN

Numbered tasks. Dependencies are explicit. Story points (1, 2, 3, 5) reflect implementation
effort + test surface, NOT calendar time. All tests are TDD: write a failing test first, then
implement until green; then run full suite (`pytest`, `ruff`, `mypy`) before moving to next task.

---

### T0 — Migration: extend Broker + AccountType enums  (SP 2)

**Path:** `src/db/alembic/versions/<rev>_phase4_extend_enums.py`, `src/models/enums.py`.

**Tests first:** `src/db/test_phase4_migration.py` — load fixture data with the new enum values
(`fg_annuity`, `nw_mutual`, `franklin_templeton`, `gsk_pension` brokers; `other` account_type)
and assert `account` table accepts them. Existing four-broker rows untouched.

**Impl:** Add the 4 enum values to `Broker`, add `OTHER = "other"` to `AccountType`. Alembic
revision: `op.execute("PRAGMA legacy_alter_table=ON")` then drop+recreate the two CHECK
constraints with the extended VALUES list. Downgrade: reverse (assert no rows use the new
values before downgrading). Reuse the canonical CHECK-constraint-recreation pattern from prior
migrations (look at the most recent one that touched a CHECK).

**Dependencies:** None. **Blocks:** T2–T6 (every adapter creates Account rows).

---

### T1 — Shared helpers: money + pdf  (SP 1)

**Path:** `src/adapters/_shared/__init__.py`, `_shared/money.py`, `_shared/pdf.py`,
`_shared/test_money.py`, `_shared/test_pdf.py`.

**Tests first:**
- `parse_currency`: handles `"$1,234.56"`, `"1234.56"`, `1234.56`, `"-$0.04"`, `"$ 660,218.55"`, raises on `"abc"`, treats `""` and `None` as `Decimal("0")`.
- `quantize_balance` / `quantize_shares`: precision asserted.
- `pdftotext_layout`: golden-file test on a tiny fixture PDF (commit a 1-page test PDF under
  `_shared/fixtures/`). Asserts non-empty extraction and `RuntimeError` when binary missing.

**Impl:** Pure functions. Subprocess wrapper with `subprocess.run([pdftotext, "-layout", str(path), "-"], ...)`.

**Dependencies:** None. **Blocks:** T2–T6.

---

### T2 — Vanguard CSV adapter  (SP 5)

**Path:** `src/adapters/vanguard_csv.py`, `src/adapters/test_vanguard_csv.py`.

**Tests first:**
- `detect_csv_flavor("Account Number,Investment Name,Symbol,...") == "brokerage"`
- `detect_csv_flavor("Fund Account Number,Fund Name,Price,...") == "529"`
- `split_blocks(file_bytes)` returns `[(positions_header, [rows]), (transactions_header, [rows])]`.
- `import_positions(path, dry_run=True)` returns `ImportResult` with `inserted=0`, `parsed=N` for
  the bundled `OfxDownload.csv` fixture (sanitized — 2 accounts, ~6 holdings).
- `import_positions(..., dry_run=False, session=db, as_of=date(2026,5,1))` writes
  `PositionSnapshot` rows; second call is a no-op (dedup hash collision).
- Per-row error isolation: feed a row with malformed shares; assert 1 error appended,
  remaining rows still inserted.
- Unmapped account_number: assert error appended, no rows for that account inserted, but other
  accounts still process.
- 529 file: 5-col flavor parses, writes `PositionSnapshot` with `symbol=None`, `description=Fund Name`.

**Impl:** Follow `xlsx_savings_plan.py` line-for-line for `ImportResult`, savepoint pattern,
`IngestionLog` writer. CLI subcommands: `import-positions --file <csv> [--apply] [--as-of YYYY-MM-DD]`.
The transactions block: parse, count, but do not write — log `transactions_seen` count to
IngestionLog `error_detail` summary JSON.

**Dependencies:** T0, T1. **Blocks:** —.

---

### T3 — F&G annuity PDF adapter  (SP 3)

**Path:** `src/adapters/fg_pdf.py`, `src/adapters/test_fg_pdf.py`.

**Tests first:**
- `extract_annual_statement(text)` returns `(contract='MZ152585', as_of=date(2026,5,1), balance=Decimal('660218.55'))` from a 200-line text fixture.
- `extract_portal_screen(text)` returns the same triple.
- `import_pdf(path, dry_run=True)` for both fixtures.
- `--apply` writes one `AccountBalanceSnapshot`; second apply is no-op.
- Unmapped contract `MZ999999` → error appended, no rows.

**Impl:** Two regexes (annual / portal flavor — tested individually). Adapter detects which
template fired and routes accordingly. Writes only `AccountBalanceSnapshot`. CLI: `import --file <pdf> [--apply]`.

**Dependencies:** T0, T1. **Blocks:** —.

---

### T4 — NW Mutual XLSX adapter  (SP 3)

**Path:** `src/adapters/nw_mutual_xlsx.py`, `src/adapters/test_nw_mutual_xlsx.py`.

**Tests first:**
- `parse_workbook(path)` returns 4 dicts with `policy_number`, `insured`, `net_accum_value`, `net_death_benefit`. The `N/A` row yields `net_accum_value=None`.
- `import_balances(..., dry_run=True)` reports 3 inserts + 1 skip for the `N/A` row.
- `--apply` writes 3 `AccountBalanceSnapshot` rows. Second run no-op.
- Unmapped policy_number → error appended.

**Impl:** openpyxl, `data_only=True`. Strip `$` and `,` via `_shared.money.parse_currency`.
CLI: `import --file <xlsx> [--apply] [--as-of YYYY-MM-DD]`.

**Dependencies:** T0, T1. **Blocks:** —.

---

### T5 — GSK pension PDF adapter  (SP 2)

**Path:** `src/adapters/gsk_pdf.py`, `src/adapters/test_gsk_pdf.py`.

**Tests first:**
- `extract_closing_balance(text)` returns `(date(2026,5,7), Decimal('31405.55'))` from a single-page fixture.
- Missing `GSK_PENSION` Account row → error appended.
- `--apply` writes one `AccountBalanceSnapshot`. Second apply no-op.

**Impl:** Single regex. CLI: `import --file <pdf> [--apply]`.

**Dependencies:** T0, T1. **Blocks:** —.

---

### T6 — Franklin Templeton PDF adapter (statements only)  (SP 3)

**Path:** `src/adapters/ft_pdf.py`, `src/adapters/test_ft_pdf.py`.

**Tests first:**
- `parse_statement_filename("2024-12-31.pdf")` returns `date(2024,12,31)`.
- `extract_portfolio_overview(text)` returns `Decimal('16406.38')` from a statement text fixture.
- `import_statements(directory, dry_run=True)` walks `*.pdf` in the given dir, parses each,
  reports per-file successes / errors.
- `--apply` writes one `AccountBalanceSnapshot` per parsed PDF.
- Unparseable file → error appended, batch continues.
- Account `franklin_templeton/8291` not seeded → all rows error, no insertions.
- Companion stub: `count_csv_transactions(path)` returns the row count from `accounthistory.csv`
  for IngestionLog summary (no DB writes — that's Phase 5).

**Impl:** Walk dir for `*.pdf`, regex on each. CLI: `import-statements --dir <path> [--apply]`.

**Dependencies:** T0, T1. **Blocks:** —.

---

### T7 — Wire seed_expected_accounts confirm flow for new institutions  (SP 1)

**Path:** `scripts/seed_expected_accounts.py` (extend, do not rewrite).

**Tests first:** Update existing `test_seed_expected_accounts.py` with a fixture row whose
institution is `Franklin Templeton` and assert the confirm walkthrough offers to create an
`Account` row with `broker='franklin_templeton'`. Same for `nw_mutual`, `fg_annuity`, `gsk_pension`.

**Impl:** Add the 4 institution → broker mappings to the existing dispatch dict.

**Dependencies:** T0. **Blocks:** post-Phase-4 operator workflow (does not block adapters).

---

### T8 — Smoke commands documentation  (SP 1)

**Path:** `CLAUDE.md` (Brokerage section), no new file.

**Impl:** Append the new CLI commands to the importer block (vanguard, fg, nw_mutual, gsk, ft).
Mention `--apply` opt-in pattern. Note the migration must run first.

**Dependencies:** T2–T6 (all adapters present). **Blocks:** —.

---

## Build sequence (gates the review-loop)

```
[T0 + T1]  →  [T2, T3, T4, T5, T6 in any order]  →  [T7, T8]
```

T0 and T1 can run in parallel (independent paths). T2–T6 can run in parallel after T0+T1 land.
T7 and T8 are pure cleanup.

## Total: 8 tasks, 21 SP

Per-task SP roll-up:
- T0 migration: 2
- T1 helpers: 1
- T2 vanguard: 5
- T3 fg: 3
- T4 nw_mutual: 3
- T5 gsk: 2
- T6 ft: 3
- T7 seed: 1
- T8 docs: 1

## Definition of Done

- [ ] All 8 tasks landed.
- [ ] `pytest && ruff check src/ && mypy src/` green.
- [ ] Migration applied to dev DB.
- [ ] `IngestionLog` rows present for every `--apply` run.
- [ ] Review-loop converges to zero P0/P1 across all 4 lenses.
- [ ] Fresh-context verifier returns PASS on `proposals/brokerage-phase4/IDEATION.md` requirements.
- [ ] Demo to user shows: dry-run output for one adapter, sample IngestionLog row, updated
  `/missing-accounts` count after the user runs `--apply` against the live DB (out-of-scope for
  CI; demoed manually).
