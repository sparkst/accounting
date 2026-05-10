# Brokerage CSV Ingest — Implementation Plan

**Parent REQ:** REQ-005 (to be amended)
**Date:** 2026-05-06
**Source:** IDEATION.md (gate confirmed 2026-05-06)
**Total estimate:** ~37 SP (revised after PLAN review — Vanguard split into 9a/9b)

## REQ amendments and new sub-REQs

### Amend REQ-005

```
## REQ-005: Brokerage CSV Import
- Acceptance: System imports CSV exports from E*Trade, Schwab, Vanguard, and Fidelity
  with correct column mapping. Tracks holdings, transactions, and realized lots in
  isolated brokerage tables (NOT the existing Transaction table). Goal: net-worth
  and performance tracking.
- Non-Goals: Automatic brokerage API connection (Plaid). Tax-form ingestion (1099-B/DIV/INT
  CSVs deferred to Phase 2). Flowing brokerage activity into existing P&L (Phase 2).
```

### New sub-REQ-IDs (for traceability in tests)

- **REQ-005a:** Account registry with parent/child relationship for plan-wrappers (MS 401k → BrokerageLink).
- **REQ-005b:** Brokerage transaction storage with `paired_transaction_id` linking dividend ↔ reinvestment.
- **REQ-005c:** Position snapshots, dedup via `source_row_hash` (not symbol UNIQUE).
- **REQ-005d:** Realized gain/loss with wash-sale and term split.
- **REQ-005e:** Idempotent re-ingest via length-framed `source_row_hash`.
- **REQ-005f:** Per-broker CSV parsers handle BOM, CRLF, multi-section files, currency formatting (`$`, `,`), `"as of"` dates, 2-digit years, broker-specific column quirks.
- **REQ-005g:** Adapters inherit `BaseAdapter`, return `AdapterResult`, write `IngestionLog`.

The existing `ADAPTER-BROK-001..007` REQ-IDs in `src/adapters/brokerage_csv.py` remain owned by that file (deferred to Phase 2 for 1099-B → P&L flow).

---

## Task list (ordered, with dependencies)

Story points use the project's Fibonacci scale.

### TASK-01 — Amend `requirements/current.md` (1 SP)

- Edit REQ-005 acceptance per above. Add stub entries for REQ-005a..g.
- No code changes.
- **Blocks:** TASK-03, TASK-04 (downstream tests reference REQ-IDs).

### TASK-02 — Add StrEnums in `src/models/enums.py` (1 SP)

Add to existing enums file. **Follow the existing UPPER_CASE convention for member names** (per `enums.py` module docstring); string values are the lowercase forms below.

- `Broker`: `ETRADE="etrade"`, `SCHWAB="schwab"`, `VANGUARD="vanguard"`, `FIDELITY="fidelity"`
- `AccountType`: `TAXABLE="taxable"`, `JOINT="joint"`, `ROTH_IRA="roth_ira"`, `TRAD_IRA="trad_ira"`, `K401="401k"`, `K403B="403b"`, `HSA="hsa"`, `K529="529"`, `TOD="tod"`, `BROKERAGELINK="brokeragelink"`, `RSU="rsu"`
  - Member names can't start with a digit. `K401`/`K403B`/`K529` are the names; **DB values are `"401k"`/`"403b"`/`"529"`** (stored as-is, not `t401k` etc.). CHECK constraints in TASK-04 must use the VALUES, not the member names.
- `CanonicalAction`: `BUY="buy"`, `SELL="sell"`, `DIVIDEND_QUALIFIED="dividend_qualified"`, `DIVIDEND_ORDINARY="dividend_ordinary"`, `INTEREST="interest"`, `REINVEST="reinvest"`, `CAPITAL_GAIN_LT="capital_gain_lt"`, `CAPITAL_GAIN_ST="capital_gain_st"`, `RSU_VEST="rsu_vest"`, `STOCK_SPLIT="stock_split"`, `CASH_IN_LIEU="cash_in_lieu"`, `SWEEP="sweep"`, `EXCHANGE="exchange"`, `TRANSFER="transfer"`, `CONTRIBUTION="contribution"`, `DISTRIBUTION="distribution"`, `FEE="fee"`, `JOURNAL="journal"`, `VALUATION_ADJUSTMENT="valuation_adjustment"`, `OTHER="other"`
- `GainLossTerm`: `SHORT="short"`, `LONG="long"`
- `BrokerageTxStatus`: `IMPORTED="imported"`, `CONFIRMED="confirmed"`, `REJECTED="rejected"`

- Co-locate with existing enums; tests in `src/models/test_enums.py` (one assertion per enum that values are stable strings).
- **Blocks:** TASK-03, TASK-05.

### TASK-03 — SQLAlchemy models in `src/models/brokerage.py` (3 SP)

Four models matching the 4 tables in IDEATION's "Final scope":

```python
class Account(Base):
    __tablename__ = "account"
    # ... see IDEATION
    # CHECK constraints from StrEnums
    # UNIQUE (broker, account_number)

class BrokerageTransaction(Base):
    __tablename__ = "brokerage_transaction"
    # quantity Numeric(18,8), amount Numeric(12,2), price Numeric(18,8)
    # paired_transaction_id self-FK, nullable
    # is_synthetic Boolean default False (True for synthesized partner rows)
    # status default='imported'
    # UNIQUE (account_id, source_row_hash)

class PositionSnapshot(Base):
    # UNIQUE (account_id, source_row_hash)

class RealizedGainLoss(Base):
    # UNIQUE (account_id, source_row_hash)
```

Co-locate `test_brokerage_models.py` covering: enum CHECK constraints, FK cascade behavior, `parent_account_id` self-reference, dedup constraint, decimal precision round-trip.

- **Blocks:** TASK-04, TASK-05, TASK-06, TASK-07, TASK-08, TASK-09.

### TASK-04 — Alembic migration (2 SP)

**Approach: autogenerate, then adjust** — matches the project's existing `tax_documents` migration pattern. Pure-hand-written would be acceptable but autogenerate is faster and the existing pattern works.

Steps:
1. Add `from src.models.brokerage import Account, BrokerageTransaction, PositionSnapshot, RealizedGainLoss  # noqa: F401` to `src/db/alembic/env.py` (matches the existing tax_document import).
2. Run `alembic revision --autogenerate -m "add brokerage tables"`.
3. Review the generated `XXXX_add_brokerage_tables.py`. Hand-edit:
   - **CHECK constraint values** must use enum VALUES (`'401k'`, `'529'`, `'taxable'`), not member names (`K401`, `K529`, `TAXABLE`). The existing pattern in `a2ad1082b755_add_check_constraints_on_enum_columns.py` joins `enum.value` strings — follow it.
   - SQLite `UNIQUE (account_id, source_row_hash)` works; nothing special needed.
4. Verify against a temp DB (NOT production data):
   ```bash
   DATABASE_PATH=/tmp/test_brok.db alembic upgrade head
   DATABASE_PATH=/tmp/test_brok.db alembic downgrade -1
   DATABASE_PATH=/tmp/test_brok.db alembic upgrade head
   ```
5. Run `alembic check` against an empty DB after upgrade — must report "No new upgrade operations detected".

Down-migration drops in reverse FK order.

- **Blocks:** All adapter integration tests.
- **Blocked by:** TASK-03.

### TASK-05 — `src/adapters/brokerage_csv_helpers.py` (3 SP)

Universal helpers needed by every adapter (per IDEATION, only what is provably shared):

- `parse_currency(s: str) -> Decimal | None` — strips `$`, `,`, normalizes `$-X.XX` → `-X.XX`, returns `None` for empty.
- `parse_quantity(s: str) -> Decimal | None` — strips commas, handles negative quantities.
- `parse_date_with_as_of(s: str) -> tuple[date | None, date | None]` — Schwab/Fidelity `"04/22/2026 as of 04/20/2026"` → `(settlement, trade)`.
- `parse_etrade_date(s: str) -> date` — tries `%m/%d/%Y` then `%m/%d/%y`.
- `read_csv_tolerant(path: Path) -> Iterator[list[str]]` — opens with `newline=''` and `encoding='utf-8-sig'`, yields rows, strips Windows CR remnants.
- `compute_brokerage_row_hash(*, broker, account_number, source_file, row_index, trade_date, action, symbol, quantity, amount, synthetic_suffix: str = "") -> str` — wrapper around `compute_source_hash()` (length-framed). Normalizes Decimals to fixed precision before hashing. Includes `source_file` and `row_index` (int) to disambiguate within-file duplicates per the AMZN RSU vesting case. For synthesized partner rows, pass `synthetic_suffix="div_partner"` instead of a float offset — keeps `row_index` an int, mypy-clean.
- `find_header_row(rows: Iterable[list[str]], required_cols: set[str]) -> int` — scans for the row containing all required columns; used by every adapter to skip variable metadata blocks.

Test file `test_brokerage_csv_helpers.py` covers each helper with edge cases from real source files (e.g., `"$-3.92"`, `"1,471"`, `"01/16/2025 as of 01/15/2025"`, `"05/01/26"`).

- **Blocks:** TASK-06, TASK-07, TASK-08, TASK-09.
- **Blocked by:** TASK-02 only. (`compute_source_hash` already exists at `src/utils/dedup.py` with stable signature; TASK-05 does not need models.)

### TASK-06 — Fidelity adapter `src/adapters/fidelity_csv.py` (5 SP)

Most complex broker; implementing first surfaces helper needs.

Capabilities:
- **Account discovery:** every row's `Account Number` + `Account` name → upsert into `account` table. Hard-coded `account_type` per number (TOD, BrokerageLink, HSA, MS 401k); user-confirmable later.
- **Multi-account history files:** ingest all 4 `Accounts_History*.csv` files, tagging rows by `Account Number`.
- **Skip leading blank rows** + scan for header line.
- **Skip trailing disclaimer + "Date downloaded" footer** by truncating when row count drops below header column count.
- **Account 89766 (MS 401k plan wrapper):** auto-set `is_plan_wrapper=True`, `parent_account_id=NULL` initially. Account 653373015 (BrokerageLink): set `parent_account_id` → 89766 after both accounts exist.
- **15-column row handling:** if `account_number == '89766'` and row has 15 cols, shift columns from index 7 onward by 1.
- **Sells with negative quantity:** normalize to positive; canonical_action='sell'.
- **REINVESTMENT + DIVIDEND RECEIVED rows:** persist both, link via `paired_transaction_id` (dividend → reinvest, inserted in order).
- **Action-text mapping** (regex on `Action` field, case-insensitive prefix match):
  - `^YOU BOUGHT` → `buy`
  - `^YOU SOLD` → `sell`
  - `^DIVIDEND RECEIVED` → `dividend_ordinary` (Fidelity doesn't tag qualified)
  - `^REINVESTMENT` → `reinvest`
  - `^INTEREST` → `interest`
  - `^Exchange In` / `Exchange Out` → `exchange`
  - `^TRANSFERRED FROM` / `^JOURNAL` → `transfer`
  - `^Electronic Funds Transfer (Paid|Received)` → `transfer`
  - `^DEPOSIT` / `^CONTRIBUTION` → `contribution`
  - `^WITHDRAWAL` / `^DISTRIBUTION` → `distribution`
  - `^Change on Market Value` → `valuation_adjustment` (paper-only valuation change on 401k wrapper; not a real trade)
  - else → `other`
- **Distinct-action audit:** before marking TASK-06 done, run `awk '{print $4}' Accounts_History*.csv | sort -u` against fixtures and confirm every distinct action either maps explicitly or to `other` deliberately. Same audit-step for TASK-07/08/09.
- **Positions file:** load `Portfolio_Positions_*.csv` into `position_snapshot`; `as_of` from filename (`May-04-2026` → 2026-05-04).
- Inherits `BaseAdapter`, returns `AdapterResult`, writes `IngestionLog`.

Test fixtures: sanitize 5-10 real rows from each Fidelity file; co-located `test_fidelity_csv.py`.

- **Blocked by:** TASK-03, TASK-04, TASK-05.

### TASK-07 — Schwab adapter `src/adapters/schwab_csv.py` (5 SP)

Capabilities:
- **Per-account file naming:** `<AccountName>_*_Transactions_*.csv`, `<AccountName>-Positions-*.csv`, `<AccountName>_GainLoss_Realized_Details_*.csv`. Account name in filename → upsert account.
- **`"04/22/2026 as of 04/20/2026"` date split:** trade_date = "as of" date, settlement_date = leading date.
- **Currency `$` and `,` stripping** via `parse_currency` for Amount, Price, Mkt Val, Cost Basis, Proceeds.
- **Quantity comma stripping** (`"1,471"`).
- **Positions file:** skip first 2 rows (title + blank), parse remaining; filter trailing rows where `Symbol in ('Cash & Cash Investments', 'Positions Total', '--')`.
- **G/L file:** skip first row (title), use second as header. Map `Long Term Gain/Loss` and `Short Term Gain/Loss` to `lt_gain_loss`/`st_gain_loss`. Map `Unadjusted Cost Basis` to `unadjusted_cost_basis`. Wash sale = "Yes"/"No" → bool.
- **Action mapping** (Schwab issues paired rows natively — DON'T synthesize):
  - `Buy` → `buy`, `Sell` → `sell`, `Sell to Close` → `sell`, `Buy to Open` → `buy`
  - `Reinvest Dividend` → `dividend_ordinary` (cash side; positive amount)
  - `Reinvest Shares` → `reinvest` (buy side; negative amount, has quantity). Pair via `paired_transaction_id` to the immediately-preceding `Reinvest Dividend` row for the same symbol.
  - `Qual Div Reinvest` → `dividend_qualified`. Pair to immediately-following `Reinvest Shares` row.
  - `Pr Yr Div Reinvest` → `dividend_ordinary` (paired with following `Reinvest Shares`)
  - `Long Term Cap Gain Reinvest` / `Short Term Cap Gain Reinvest` → `capital_gain_lt` / `capital_gain_st` (cash side, paired with `Reinvest Shares`)
  - `Cash Dividend` → `dividend_ordinary`
  - `Special Dividend` / `Non-Qualified Div` → `dividend_ordinary`
  - `Bank Interest` / `Bond Interest` / `CD Interest` / `Credit Interest` → `interest`
  - `CD Deposit Adj` / `CD Deposit Funds` → `contribution`
  - `Stock Split` → `stock_split`
  - `Cash In Lieu` → `cash_in_lieu`
  - `Journaled Shares` → `rsu_vest` if account_type is `rsu`, else `transfer` (Joint Tenant uses `Journaled Shares` for cash transfers)
  - `Long Term Cap Gain` / `Short Term Cap Gain` → `capital_gain_lt` / `capital_gain_st`
  - `Internal Transfer` / `Security Transfer` / `MoneyLink Transfer` → `transfer`
  - `Journal` (without "Shares") → `journal`
  - else → `other`
- No `is_synthetic=True` rows for Schwab — every needed row exists natively in the source files.
- **`XXXX-X724*.CSV` files:** detected and skipped with a logged INFO message ("1099 form file deferred to Phase 2").
- Inherits `BaseAdapter`.

Test fixtures from sanitized real rows; co-located `test_schwab_csv.py`.

- **Blocked by:** TASK-03, TASK-04, TASK-05.

### TASK-08 — E*TRADE adapter `src/adapters/etrade_csv.py` (3 SP)

Capabilities:
- **Process `DownloadTxnHistory.csv`** for transactions. **Skip `tradesdownload.csv`** entirely (sign convention is opposite, data is duplicate of DownloadTxnHistory).
- **Parse `PortfolioDownload.csv`** for current positions — net-worth tracking needs it. Skip the summary header rows (Account / Net Account Value); detect the column-header row by scanning for the row containing all expected position columns.
- **6-row metadata header skip:** use `find_header_row` looking for `Activity/Trade Date` column.
- **Date parsing:** `parse_etrade_date` handles 2-digit years.
- **Account discovery:** parse `Cap 1(-6084) -6354` from line 3 → account_number `6354`.
- **Action mapping** (E*TRADE Activity Type, case-sensitive exact match — E*TRADE strings are stable):
  - `Bought` → `buy`, `Sold` → `sell`
  - `Dividend` → `dividend_ordinary`
  - `Qualified Dividend` → `dividend_qualified`
  - `Dividend Reinvestment` → `reinvest` (buy side). E*TRADE issues this as a single row with the buy details. **Synthesize** a paired dividend row using same date/symbol/amount=+abs(net), `is_synthetic=True`, `paired_transaction_id` linking to this real row. Pass `synthetic_suffix="div_partner"` to `compute_brokerage_row_hash` for stable distinct hash.
  - `Interest` / `Interest Income` → `interest`
  - `Stock Split` → `stock_split`
  - `Transfer` / `Wire` / `Direct Debit` / `Online Transfer` → `transfer`
  - `Adjustment` / `Reorganization` → `other`
  - else → `other`
- **CUSIP** mapped from `Cusip` column.
- Idempotency test must include the synthesis case: ingest a `Dividend Reinvestment` fixture twice and assert exactly 2 rows (real + synthetic), not 4.
- Inherits `BaseAdapter`.

Test fixtures co-located.

- **Blocked by:** TASK-03, TASK-04, TASK-05.

### TASK-09a — Vanguard section-state-machine parser (3 SP)

Standalone module `src/adapters/vanguard_section_parser.py` (or as nested helper inside `vanguard_csv.py` if size allows):
- Reads the file line-by-line.
- Detects section transitions by exact column-name match against a registered set of header signatures.
- Emits `(section_kind, header, row)` tuples for the consumer.
- Resets on blank line; advances on next header detection.
- **Fails loudly** on unknown header — does NOT silently skip.

Standalone tests `test_vanguard_section_parser.py` cover: brokerage positions header, brokerage transactions header, 529 positions header, 529 transactions header, empty 3rd-section detection, blank-line section reset, unknown-header → raise.

- **Blocked by:** TASK-05.

### TASK-09b — Vanguard adapter `src/adapters/vanguard_csv.py` (5 SP)

Capabilities:
- **Uses TASK-09a section-state-machine parser** (don't reimplement).
- Brokerage `OfxDownload.csv` transaction header: `Account Number,Trade Date,Settlement Date,Transaction Type,Transaction Description,Investment Name,Symbol,Shares,Share Price,Principal Amount,Commissions and Fees,Net Amount,Accrued Interest,Account Type` (note: `Commissions and Fees`, not `Commission Fees`).
- Brokerage positions header: `Account Number,Investment Name,Symbol,Shares,Share Price,Total Value`.
- 529 positions header: `Fund Account Number,Fund Name,Price,Shares,Total Value`.
- 529 transactions header: `Account Number,Trade Date,Process Date,Transaction Type,Transaction Description,Investment Name,Share Price,Shares,Gross Amount,Net Amount`.
- **Currency parsing:** `$`, `$-`, comma — apply `parse_currency` to all monetary fields (esp. 529 has `$` everywhere).
- **Multi-row same-symbol positions:** account 65344815 has 2× VMFXX rows — sum quantities + market_value, weighted-average price, store as one snapshot.
- **Account discovery:** brokerage accounts (65344815, 70862729) get `account_type=taxable`; 529 accounts (208182839-01, 252341309-01) get `account_type="529"`, `tax_sheltered=True`, beneficiary populated (Aiden / Emerson per user direction).
- **Historic accounts in `OfxDownload copy.csv`** (37737894, 32628019, 59309844): create with `account_type=taxable`, `notes='historic — verify with user'`. User can manually amend later.
- **Action mapping** (case-insensitive — Vanguard uses inconsistent case like `'Stock split'`):
  - `Buy` / `Buy (Initial)` → `buy`
  - `Sell` → `sell`
  - `Sweep in` / `Sweep out` → `sweep`
  - `Dividend` → `dividend_ordinary`
  - `Reinvestment` → `reinvest`
  - `Reinvestment (LT gain)` / `Reinvestment (ST gain)` → `reinvest` (cap-gain reinvest; flag via raw_data for later)
  - `Capital gain (LT)` / `Capital gain (ST)` → `capital_gain_lt` / `capital_gain_st`
  - `Stock split` → `stock_split`
  - `Conversion (incoming)` / `Conversion (outgoing)` → `exchange`
  - `Exchange In` / `Exchange Out` → `exchange`
  - `Rollover (incoming)` → `transfer`
  - `Funds Received` → `transfer`
  - `Withdrawal` → `transfer`
  - `Contribution` → `contribution`
  - `Contribution AIP` → `contribution`
  - `Distribution` → `distribution`
  - 529-specific:
    - `Electronic Payment via 529 ePay Fee` → `fee`
    - `Qual w/d to Edu Inst - Electronic` → `distribution`
    - `Qualified w/d Educational Institution` → `distribution`
    - `Qualified w/d Acct Owner` → `distribution`
  - else → `other`
- Inherits `BaseAdapter`.
- **Multi-row same-symbol position handling:** keep both rows in `position_snapshot`. Compute `source_row_hash` per source row (include `row_index` to disambiguate). Aggregation for display is a downstream consumer concern — don't pre-aggregate at ingest. This avoids hash ambiguity and preserves audit fidelity.

Test fixtures co-located.

- **Blocked by:** TASK-03, TASK-04, TASK-05, TASK-09a.

### TASK-10 — Ingestion CLI `scripts/ingest-brokerage.py` (2 SP)

```
python scripts/ingest-brokerage.py /path/to/accounts/
```

- Iterates subfolders by name → dispatches to corresponding adapter.
- Calls `adapter.run(session)` per broker.
- Aggregates `AdapterResult`s, prints summary table (rows added, rows skipped (dup), rows errored).
- Exits non-zero on any errors.
- Idempotent: re-running with same files produces 0 new rows.
- Co-locate `test_ingest_brokerage.py` for the dispatcher logic (parametrized over a folder fixture mirroring the user's downloads structure).

- **Blocked by:** TASK-06, TASK-07, TASK-08, TASK-09.

### TASK-11 — Account metadata seed/enrichment (1 SP)

`scripts/seed-brokerage-accounts.py` — one-time script run after first ingest:

- Sets `account_type` and `beneficiary` for all known accounts.
- Sets `parent_account_id` for BrokerageLink → MS 401k.
- Idempotent (safe to re-run).
- Provides a dry-run mode that prints what would change.

Hard-coded mapping (from IDEATION + user decisions):

| account_number | broker | account_type | tax_sheltered | parent | beneficiary |
|---|---|---|---|---|---|
| Z23257759 | fidelity | tod | false | — | Travis |
| 653373015 | fidelity | brokeragelink | true | 89766 | Travis |
| 89766 | fidelity | t401k | true (wrapper) | — | Travis |
| 241527012 | fidelity | hsa | true | — | Travis |
| ...724 | schwab | joint | false | — | Travis (joint w/ Amy) |
| ...144 | schwab | rsu | false | — | Travis |
| 6354 | etrade | taxable | false | — | Travis |
| 65344815 | vanguard | taxable | false | — | Travis |
| 70862729 | vanguard | taxable | false | — | Travis |
| 208182839-01 | vanguard | t529 | true | — | Aiden |
| 252341309-01 | vanguard | t529 | true | — | Emerson |
| 37737894 | vanguard | taxable | false | — | (historic — verify) |
| 32628019 | vanguard | taxable | false | — | (historic — verify) |
| 59309844 | vanguard | taxable | false | — | (historic — verify) |

- **Blocked by:** TASK-06, TASK-07, TASK-08, TASK-09b. (Operates on DB directly via SQLAlchemy; doesn't need the CLI.)

### TASK-12 — Quality gates pass (2 SP)

- `pytest src/adapters/test_*.py src/models/test_*.py` — all green
- `ruff check src/`
- `mypy src/`
- `pytest --cov=src/adapters` — co-located test coverage ≥ 85% on new adapters
- Fix any drift; do not skip warnings.

- **Blocked by:** all adapter tasks complete.

### TASK-13 — Park `brokerage_csv.py` and clean up empty subdirectory (1 SP)

a. Add a brief docstring note at the top of `src/adapters/brokerage_csv.py`:

```python
"""... existing docstring ...

NOTE (2026-05-06): Targets 1099-B annual tax CSV format → Transaction table. Preserved
for Phase 2 (taxable-event flow into P&L). Phase 1 net-worth/performance ingest uses
src/adapters/{fidelity,schwab,etrade,vanguard}_csv.py → isolated brokerage_* tables.
"""
```

b. Add a deprecation comment to `src/api/routes/ingest.py` on the `/api/import/brokerage-csv` route handler explaining it targets 1099-B → P&L (distinct from the new isolated-table ingest). Do not remove the route.

c. Remove the empty `src/adapters/brokerage/` directory left over from ideation exploration: `git rm -r src/adapters/brokerage/` (if it's still empty at this point).

- **Blocks:** none. Run any time; safest at end of feature merge.

---

## Dependency graph

```
TASK-01 ──┐
TASK-02 ──┼──> TASK-03 ──> TASK-04 ──┐
          │                          │
          └──> TASK-05 ──────────────┼──> TASK-06 ─┐
                                     ├──> TASK-07 ─┤
                                     ├──> TASK-08 ─┤
                                     └──> TASK-09a ─> TASK-09b ─┴──> TASK-10 ─> TASK-12
                                                                       │
                                                                       └──> TASK-11 ─┘
                                                                  TASK-13 (parallel, any time)
```

TASK-06 / TASK-07 / TASK-08 / TASK-09a are **independent** once TASK-04+TASK-05 are done — can be parallelized via subagents. TASK-09b depends on 09a.

## Test strategy (TDD per CLAUDE.md)

- For each REQ-005a..g, write failing tests first that reference the REQ-ID in a docstring.
- **Fixture style: inline byte-string constants** in the test file (matches existing `test_bank_csv.py` pattern with `CHASE_CSV = b"..."`). No separate fixtures directory.
- For larger fixtures (e.g., 50-row Schwab transaction sample), use a multi-line string constant; sanitize by truncating account numbers but preserve full structure.
- **Special-case fixture for plan-wrapper detection:** account 89766 is 5 digits; do NOT redact for tests of TASK-06's 15-column edge case. Use the real number `89766` in those test fixtures (the number is not sensitive on its own — it's a Microsoft 401k plan ID, the same for every plan participant).
- Each adapter test file covers: header sniffing, currency parsing, date parsing, action mapping (assert exhaustive coverage of distinct actions in the fixture), dedup idempotency, account upsert, paired-transaction linking, mapped-vs-other ratio.
- **RSU collision test (P1):** synthetic fixture with 2 rows for the same date/action/symbol/quantity/amount; assert both persisted via distinct hashes.
- **Synthesis test (E*TRADE):** ingest `Dividend Reinvestment` fixture twice; assert exactly 2 rows (real + synthetic, both with stable hashes), not 4.
- **Idempotency test:** ingest each broker's fixture twice, assert row count unchanged after 2nd run.
- **Migration test:** spin up a tmp-file SQLite, run `alembic upgrade head` via `alembic.config.main(['upgrade', 'head'])`, assert 4 tables exist with expected columns + CHECK constraints (insert a row with bad enum value, assert IntegrityError).

## Risks and mitigations

1. **Real account numbers in fixtures** — risk of leaking a sensitive number. Mitigation: replace last 4 of each account_number with `XXXX` in fixtures, but keep the account-number length and prefix structure intact.
2. **Vanguard format may shift** between OFX downloads. Mitigation: section-state-machine parser detects headers by content, not position. If a new section header appears, fail loudly with the unknown header logged so we can extend the parser.
3. **Reinvest pairing race condition** — synthesizing the dividend partner row then linking the buy via FK requires both rows in the same transaction. Mitigation: insert dividend first, get its ID, set `paired_transaction_id` on the reinvest row, commit together.
4. **Fidelity 401k 15-column edge case** — narrow detection: only when account_number=='89766' AND len(row)==15. Add explicit test.
5. **Decimal precision drift** — round all imports to fixed scale in `parse_currency`/`parse_quantity` BEFORE hashing. Test that re-imports of the same row produce the same hash.

## Out of scope (still)

- 1099-B/DIV/INT CSV ingestion (Schwab `XXXX-X724`, E*TRADE 1099 PDFs, Fidelity tax CSVs).
- API integrations (Plaid, Schwab Trader API, E*TRADE API).
- Dashboard UI for brokerage data.
- Folder-watch automation.
- Flowing brokerage activity into the existing `Transaction` table or P&L.
- Net-worth aggregation queries / performance calculations (separate proposal).
