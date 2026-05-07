# Phase 4 — Per-Institution Adapters: IDEATION

**Goal.** Clear the 11 entries in `/api/brokerage/missing-accounts` by ingesting the source files
already provided in `/Users/travis/Downloads/accounts/` (Vanguard CSV, F&G PDF, NW Mutual XLSX,
GSK PDF, Franklin Templeton CSV+PDF). At convergence the panel returns ≤2 entries (Vanguard 8019
& 9844 are already linked but their snapshots may not match exactly until matching tightens).

Canonical reference pattern: `src/adapters/xlsx_savings_plan.py`. Every adapter must follow it
verbatim — `ImportResult` dataclass, `dry_run=True` default, per-row `session.begin_nested()`,
`Decimal(str(value))` at the boundary, `source_row_hash` with quantized numeric components,
`IngestionLog` row on apply, `--apply` opt-in CLI.

---

## Cross-cutting design decision: How do non-broker institutions get an `Account` row?

The `account` table requires `broker IN ('etrade','schwab','vanguard','fidelity')` (CHECK
constraint). FT, NW Mutual, F&G, GSK are not brokers. The missing-accounts panel only clears
when `expected_account.resolved_account_id` is non-null AND fresh snapshots exist for that
`account_id`. So the four non-broker institutions need an `Account` row to link to.

| Option | Approach | Tradeoffs |
|---|---|---|
| **A (recommended)** | Extend `Broker` enum: add `franklin_templeton`, `nw_mutual`, `fg_annuity`, `gsk_pension`. Add `AccountType.OTHER` (or `LIFE_INSURANCE`/`ANNUITY`/`PENSION`). One migration alters both CHECK constraints. | Single small migration. All Phase-3 invariants (missing-accounts, account tags, brokerage page UI, AccountBalanceSnapshot FK) keep working unchanged. The semantic is honest — these are the institutions Travis tracks alongside true brokers. |
| **B** | Loosen `broker` CHECK to allow any text. Use a separate `institution_type` column for richer semantics. | Bigger schema surface. Existing four-broker invariants (per-broker visualizations, dropdowns) need rework. |
| **C** | Skip `Account` entirely. Make the missing-accounts query also accept `raw_account_name` matches against `AccountBalanceSnapshot`. | Avoids a migration but makes the panel logic dual-mode and harder to reason about. Tags/per-account history pages would never work for these institutions. |

**Pick A.** Migration adds ~6 enum values across two CHECK constraints, no row rewrites.

---

## Adapter 1 — Vanguard CSV (handles brokerage 6-col + 529 5-col)

**Source files** (3 — same flavor split across):
- `OfxDownload.csv` — Amy's accounts: 65344815 (Rollover IRA), 70862729 (Roth IRA)
- `OfxDownload-travis.csv` — Travis: 32628019 (Trad IRA), 37737894 (drained, ignore), 59309844 (Roth IRA)
- `ofxdownload_05042026.csv` — 529: 208182839-01 (Aiden Coverdale per snapshot, but DB rows say it's Aiden's 529 — needs reconciliation during execute)

**File structure.** Each CSV has TWO blocks separated by blank lines:
1. **Positions block** — 6-col header `Account Number,Investment Name,Symbol,Shares,Share Price,Total Value` (brokerage) OR 5-col `Fund Account Number,Fund Name,Price,Shares,Total Value` (529). Multiple `Account Number` values appear in the same block.
2. **Transactions block** — wider header (`Trade Date,Settlement Date,Transaction Type,...`).

**Phase 4 scope:** Positions block → `PositionSnapshot`. Transactions block parsed but skipped for v1
(adapter logs `transactions_seen` count; future PR ingests them as `BrokerageTransaction`).

**Account-id mapping.** Lookup by `(broker='vanguard', account_number=<csv-value>)`. 7 of 8 distinct
accounts already exist in the `account` table. Adapter must NOT auto-create — instead, return an
error per unmapped account_number so the user sees it and decides. (One unmapped account is the
known Emerson Coverdale — operator will pre-seed via `seed_expected_accounts`.)

**`as_of` date.** No statement date in the CSV. Use the file's filesystem `mtime` truncated to
date, OR allow `--as-of YYYY-MM-DD` override (recommended). Parameter to `import_positions(...)`.

**Decimal handling.** Strip `$` and `,` from values. `Decimal(str(value))` after strip.

**Dedup hash inputs.** `(account_number, symbol, shares.quantize(0.00000001), price.quantize(0.00000001), market_value.quantize(0.01), as_of.isoformat())`.

**Source tag.** `source_file = "<basename>.csv"`, conceptual source key `vanguard_csv`.

---

## Adapter 2 — F&G annuity PDF

**Source files:**
- `FG/Annual Statement.pdf` — 2025–2026 annual statement. Issued for contract MZ152585. Total Account Value as of 05/01/2026: $660,218.55.
- `FG/MZ152585.pdf` — current online portal screen-grab. Same value.

**Extract.** Use `pdftotext -layout` (Poppler — already installed at `/opt/homebrew/bin/pdftotext`).
Two regex passes:
1. Contract # pattern: `Contract #:\s*([A-Z0-9]+)` (annual stmt) or `Policy number\s+([A-Z0-9]+)` (portal).
2. Total Account Value: `Total Account Value as of\s+(\d{2}/\d{2}/\d{4})\s+\$\s*([\d,]+\.\d\d)` (annual) or `Total account value\s+\$([\d,]+\.\d\d)` (portal — date implicit, use file mtime or --as-of).

**Output.** One `AccountBalanceSnapshot` row per PDF (raw_account_name = "F&G Annuity {contract_#}",
account_id = lookup by `(broker='fg_annuity', account_number=contract_#)`).

**Dedup hash:** `(contract_#, as_of.isoformat(), balance.quantize(0.01))`.

**Source tag.** `fg_pdf`.

---

## Adapter 3 — NW Mutual whole-life XLSX

**Source file:** `nw-mutual/allAccounts.xlsx` — single sheet `Life Insurance`, 4 policy rows
(verified by direct openpyxl read). Columns: `Insured | Account Number | Net Death Benefit |
Annualized Premium | Last Annual Dividend | Loans | Net Accumulated Value`.

**Output.** One `AccountBalanceSnapshot` per policy. `balance = Net Accumulated Value`. (NB: policy
17399232 has `Net Accumulated Value = "N/A"` — adapter must skip-with-warning and not write a row
for that policy. Operator decides whether to seed a manual snapshot later.)

**Account mapping.** `(broker='nw_mutual', account_number=<xlsx-value>)`. Account rows must be
pre-seeded via `seed_expected_accounts confirm` once broker enum extension lands; adapter errors on
unmapped policies.

**`as_of`.** XLSX has no statement date. Use file mtime, override via `--as-of`.

**Dedup hash:** `(policy_number, as_of, net_accum_value.quantize(0.01))`.

**Source tag.** `nw_mutual_xlsx`.

---

## Adapter 4 — GSK pension PDF

**Source file:** `gsk/GSK Cash Balance Account Activity.pdf` — single page. Closing Balance as of
05/07/2026: $31,405.55. Vesting 100%.

**Extract.** Single regex on `pdftotext -layout` output:
`Closing Balance as of\s+([A-Z][a-z]+\s+\d{1,2},\s+\d{4})\s+\$([\d,]+\.\d\d)`.

**Output.** One `AccountBalanceSnapshot`. `account_number = "GSK_PENSION"` (single account, no
natural ID in the document). `raw_account_name = "GSK Cash Balance Pension Plan"`.

**Dedup hash:** `(as_of, balance.quantize(0.01))`.

**Source tag.** `gsk_pdf`.

---

## Adapter 5 — Franklin Templeton (CSV transactions + PDF year-end statements)

**Source files:**
- `FT/accounthistory.csv` — transaction-level DIV REINVEST, LT/ST CAPGN REINV for `********8291`.
- `FT/2020-12-31.pdf` … `FT/2026-03-31.pdf` — 11 year-end (and one quarter-end) statements with
  Portfolio Overview totals (e.g. 2024 = $16,406.38).
- `FT/Portfolio Performance - Online Account Access.pdf` — current portal value $20,699.48 as of 05/01/2026.

**Phase 4 scope:** **statements only**. One `AccountBalanceSnapshot` per statement (year-end value
from `Portfolio Overview` or `Ending Value`). The transactions CSV is parsed by a sibling helper
that returns row counts for the IngestionLog summary, but no `BrokerageTransaction` rows are
written (deferred to Phase 5 — adds reinvestment lots that complicate cost-basis tracking).

**Extract.** Per PDF: `pdftotext -layout`, regex on first page:
- `(\d{4}-\d{2}-\d{2})` filename → `as_of`
- `Beginning Portfolio Value as of \d{2}/\d{2}/\d{4}\s+\$([\d,]+\.\d\d)` → ignored (used for cross-check)
- `PORTFOLIO OVERVIEW\s+\$([\d,]+\.\d\d)` → `balance`

**Account mapping.** `(broker='franklin_templeton', account_number='8291')`. Single account.

**Dedup hash:** `(account_number='8291', as_of.isoformat(), balance.quantize(0.01))`.

**Source tag.** `ft_pdf` (statements), `ft_csv` (transactions placeholder).

---

## Shared helpers (DRY)

A small helper module `src/adapters/_shared/money.py` exports:
- `parse_currency(value: str | float | int) -> Decimal` — strips `$`, `,`, whitespace; `Decimal(str(...))`
- `quantize_balance(d: Decimal) -> Decimal` — `.quantize(Decimal("0.01"))`
- `quantize_shares(d: Decimal) -> Decimal` — `.quantize(Decimal("0.00000001"))`

A helper `src/adapters/_shared/pdf.py` exports:
- `pdftotext_layout(path: Path) -> str` — wraps subprocess, raises if pdftotext missing.

Both helpers get their own `test_*.py`. The Vanguard adapter additionally exposes parsing helpers
(`split_position_block`, `split_transaction_block`, `detect_csv_flavor`) that are independently
testable.

---

## Migration plan (gates Phase 4 execute)

1. New Alembic revision `phase4_extend_broker_enum` — recreates the two CHECK constraints with the
   added enum values. Downgrade is the reverse. Single transaction. No row rewrites.
2. After migration, operator runs `seed_expected_accounts confirm` to interactively walk through
   the still-unresolved expected_accounts and create `Account` rows pointing at the new brokers.

Phase 4 adapters do **not** auto-create `Account` rows. They error per-row when an account is
unmapped, so the operator always sees what's missing before bulk apply.

---

## Recommendation

Build adapters 1–5 in the order listed (Vanguard first — largest blast radius, exercises the
PositionSnapshot path; F&G + GSK next — single-row PDF extraction, fastest wins; NW Mutual XLSX —
straightforward openpyxl; FT last — wraps in the year-end-statement path and parks the transaction
ingester). Migration lands first, before any adapter writes a row. Tests co-located, DRY-RUN by
default, per-record savepoint, IngestionLog on `--apply`. Single review-loop covers all five.
