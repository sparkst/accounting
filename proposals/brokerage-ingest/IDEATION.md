# Brokerage CSV Ingest — Ideation

**Parent REQ:** REQ-005 (Brokerage CSV Import)
**Date:** 2026-05-06
**Status:** Awaiting user gate

## Goal

Ingest CSV exports from Fidelity, E*TRADE, Schwab, Vanguard into a separate
brokerage data layer. No coupling to existing Transaction table or P&L flow
(per user decision). One-time backfill + monthly drop workflow.

## Source files observed at `/Users/travis/Downloads/accounts/`

| Broker | File kinds present | Accounts seen |
|---|---|---|
| Fidelity | 4× `Accounts_History*.csv`, 1× `Portfolio_Positions_*.csv` | TOD Z23257759, BrokerageLink 653373015, MS 401k 89766, +1 in 4th history |
| E*TRADE | `DownloadTxnHistory.csv` (with metadata header), `PortfolioDownload.csv`, `tradesdownload.csv`, `MS_2025_1099-CONS_*.pdf` | Cap 1(-6084) -6354 |
| Schwab | per-account `*_Transactions_*.csv`, `* -Positions-*.csv`, `*_GainLoss_Realized_Details_*.csv`, two `XXXX-X724*.CSV` (1099 detail) | Joint Tenant ...724, AMZN RSU ...144 |
| Vanguard | `OfxDownload*.csv` (positions, brokerage), `ofxdownload_05042026*.csv` (positions + transactions, 529) | Brokerage 65344815, Brokerage 70862729, 529 208182839-01 |

## Cross-cutting concerns

1. **Header sniffing** — files vary by broker AND by report type. Need a
   classifier that reads first 10 lines and decides: transactions /
   positions / realized-G/L / 1099-tax-detail / unknown.
2. **BOM markers** present on Fidelity & Vanguard. Use `utf-8-sig`.
3. **Metadata rows** before headers: E*TRADE txn (4–5 rows), Schwab positions
   (1 row of "Positions for account ... as of ..."), Schwab realized-G/L (1
   row), Vanguard 529 file (mixed positions + txns in one file with blank
   line separator).
4. **Duplicate dividend rows** — Schwab/Fidelity issue the dividend and the
   reinvestment as 2 rows; E*TRADE issues 1 combined row. Adapter must
   normalize to a canonical "dividend + buy" pair OR a single combined row,
   pick one and stick with it.
5. **Tax-shelter classification** must come from the Account record, not the
   transaction. 401k/Roth/Trad IRA/HSA/529 → no P&L flow ever; Taxable/Joint
   → can flow when Phase 2/3 enables P&L integration.
6. **Idempotent reload** — user will drop the same file again next month
   with overlapping date ranges. Need a deterministic dedup key.

## Concept options

### Option A — One adapter per broker, stateful classifier

Four adapters, each with `ingest_folder(path)`. Adapter scans folder, detects
file kind by sniffing first lines, dispatches to the right loader. Account
records auto-created from account numbers found in files (with a manual
metadata enrichment step the user runs once).

- **Pros:** Clean separation, mirrors existing `gmail_n8n.py` /
  `bank_csv.py` / `stripe_adapter.py` pattern. Easy to test with file
  fixtures. New broker = one new file.
- **Cons:** Some duplication in CSV-skipping/dedup logic across adapters.
- **Effort:** ~3–5 SP per adapter + 3 SP shared infra = ~15–23 SP.

### Option B — Single generic CSV adapter, broker-specific schema files

One ingest engine driven by per-broker schema configs (YAML/Python dicts)
declaring header sniff patterns, column mappings, action-name mappings.

- **Pros:** Less code duplication. Adding a broker = adding a config, no new
  Python file.
- **Cons:** Fragile when files are weird (E*TRADE metadata, Vanguard 529
  mixed file). Schema configs become a 2nd DSL to maintain. Existing project
  pattern is "one adapter per source" — Option A is more consistent.
- **Effort:** Higher up-front (~10 SP for engine), then ~1–2 SP per broker.

### Option C — Hybrid: small shared helper + per-broker adapters

Per-broker adapters (Option A) but with a shared `csv_helpers.py` for: BOM
handling, metadata-row skipping, dedup key generation, account-record
upsert. Each adapter ~30% smaller.

- **Pros:** Best of both. Aligns with existing pattern. Shared logic in one
  place.
- **Cons:** Slight risk of helper becoming a kitchen sink — keep it small.
- **Effort:** ~12–18 SP total.

## Schema decision

Three new tables, all isolated from the existing Transaction table:

```
account
  id (PK)
  broker (fidelity|etrade|schwab|vanguard)
  account_number (text, broker-scoped)
  account_name (text, free-form from CSV)
  account_type (taxable|joint|roth_ira|trad_ira|401k|hsa|529|tod)
  entity (sparkry|blackline|personal)  -- always personal for now
  tax_sheltered (bool)
  created_at, updated_at
  UNIQUE (broker, account_number)

brokerage_transaction
  id (PK)
  account_id (FK -> account.id)
  trade_date (date)
  settlement_date (date, nullable)
  action (text — broker-native, plus normalized canonical_action)
  canonical_action (buy|sell|dividend|interest|reinvest|capital_gain|fee|transfer|contribution|distribution|other)
  symbol (text, nullable)
  description (text)
  quantity (decimal, nullable)
  price (decimal, nullable)
  amount (decimal — signed, +in/-out, matches existing convention)
  fees (decimal, nullable)
  source_file (text — basename for traceability)
  source_row_hash (text — SHA256 for dedup)
  raw_data (JSON — original row preserved per CLAUDE.md rule)
  created_at
  UNIQUE (account_id, source_row_hash)

position_snapshot
  id (PK)
  account_id (FK)
  as_of (datetime)
  symbol (text, nullable for cash)
  description (text)
  quantity (decimal)
  price (decimal, nullable)
  market_value (decimal)
  cost_basis (decimal, nullable)
  unrealized_gain (decimal, nullable)
  source_file (text)
  raw_data (JSON)
  created_at
  UNIQUE (account_id, as_of, symbol)
```

`realized_gain_loss` (for Schedule D source) — defer to Phase 2 unless
needed now. Lots can be reconstructed from `brokerage_transaction` for
backfill, and Schwab/E*TRADE/Vanguard all give a separate G/L CSV at tax
time. **Recommendation: include a fourth table now**, since the source
files already contain it and storing it once is cheaper than re-deriving:

```
realized_gain_loss
  id (PK)
  account_id (FK)
  symbol, description
  opened_date, closed_date
  quantity
  proceeds, cost_basis, gain_loss
  term (short|long)
  wash_sale (bool)
  disallowed_loss (decimal, nullable)
  source_file, source_row_hash, raw_data
  UNIQUE (account_id, source_row_hash)
```

## Dedup strategy

`source_row_hash = sha256(broker || account_number || trade_date ||
canonical_action || symbol || quantity || amount)`

Why these fields and not the full row: column order varies, whitespace and
formatting differ across exports of the same data. The semantic tuple is
stable. Duplicate downloads of overlapping ranges produce identical hashes
and the UNIQUE constraint silently skips them.

## Recommendation

**Option C (hybrid)** with all four tables (`account`,
`brokerage_transaction`, `position_snapshot`, `realized_gain_loss`).

Why:
- Matches the existing per-adapter pattern in `src/adapters/`.
- Keeps duplication low via a small `brokerage_csv_helpers.py`.
- Including `realized_gain_loss` now is cheap (one more loader per broker)
  and avoids painful re-derivation at tax time.
- Defers Phase 2 (P&L flow) cleanly — the new tables are isolated.

## Open questions for user gate

1. **Hybrid vs pure-per-adapter:** OK with a small shared helpers module, or
   prefer pure isolation per CLAUDE.md "minimal abstraction" rule?
2. **Realized G/L as 4th table:** include now, or defer?
3. **MS 401k account in Fidelity files:** the Portfolio_Positions row shows
   `89766 / MICROSOFT 401K PLAN / BROKERAGELINK / 149770.04` — is this the
   parent 401k holding the BrokerageLink sub-account, or a separate plan?
   Needs your confirmation before I model it.
4. **Schwab AMZN RSU account:** RSUs that have vested = your shares,
   straightforward. Unvested RSUs (if any are tracked) — exclude from
   positions? They're not yours yet. Best guess: Schwab CSV only contains
   vested holdings. Confirm.
5. **Joint Tenant + Amy:** confirmed Personal entity per your direction. No
   action needed; noting for the record.

## What gets built (assuming recommendation accepted)

```
src/adapters/brokerage/
  __init__.py
  csv_helpers.py            -- BOM, metadata-skip, sniff, dedup, account upsert
  fidelity_csv.py           -- ~150 LoC
  etrade_csv.py             -- ~180 LoC (metadata header skip)
  schwab_csv.py             -- ~200 LoC (per-account files, tax CSV detection)
  vanguard_csv.py           -- ~220 LoC (529 mixed file is tricky)
  test_fidelity_csv.py      -- fixtures from real files (sanitized)
  test_etrade_csv.py
  test_schwab_csv.py
  test_vanguard_csv.py
  test_csv_helpers.py
src/models/brokerage.py     -- Account, BrokerageTransaction, PositionSnapshot, RealizedGainLoss
src/db/alembic/versions/
  XXXX_add_brokerage_tables.py
src/api/routes/brokerage.py -- minimal read-only endpoints (deferred to Phase 2 if you'd rather)
scripts/ingest-brokerage.py -- CLI: python scripts/ingest-brokerage.py <folder>
```

## Out of scope (per user)

- Plaid/aggregator integration
- Dashboard UI for brokerage data
- Folder-watch automation
- Flowing dividends/interest/realized gains into existing Transaction table / P&L

---

## Review synthesis (3 reviewers, ~40 findings, deduped)

### P0 (must fix before plan)

1. **Existing `src/adapters/brokerage_csv.py` (635 LoC, REQ-IDs ADAPTER-BROK-001..007) was missed.** It writes to the existing Transaction table — directly contradicts the user's "keep separate" directive. Verified: 0 rows in DB with `source=BROKERAGE_CSV`, never used in production.
   → **Resolution: deprecate `brokerage_csv.py`** (rename to `brokerage_csv.py.deprecated` or remove), reuse its REQ-IDs, build new isolated tables per user direction.
2. **Currency formatting:** `$`, comma thousands, and `$-X.XX` (dollar before minus) appear in Schwab and Vanguard 529 amounts/prices/quantities. Naive `float()`/`Decimal()` will raise. → Add `parse_currency()` helper.
3. **Fidelity 401k rows have 15 columns, header has 14** — every account-89766 row will silently misalign. → Detect column count per row, branch on account_number for the +1 shift.
4. **Quantity comma formatting:** Schwab AMZN RSU `"1,471"` raises ValueError on `float()`. → Use `parse_quantity()` that strips commas.
5. **Schwab `XXXX-X724` files are 1099 forms, not transactions or G/L.** Three sections (1099-DIV / 1099-INT / 1099-B) with different schemas. → New 5th table `tax_form_detail`, dedicated section-aware loader. The two `XXXX-X724` files are also identical (same MD5) so dedup by file hash before loading.

### P1 (must address in plan)

6. **REQ-005 lists only E*TRADE/Schwab/Vanguard.** Adding Fidelity is scope expansion. → Amend REQ-005 in `requirements/current.md` to add Fidelity explicitly.
7. **Adapter pattern fit:** must inherit `BaseAdapter`, return `AdapterResult`, write `IngestionLog` row, define `source` property. Use existing `compute_source_hash()` from `src/utils/dedup.py` (length-framed to avoid `||` collisions) instead of bare `||` concatenation.
8. **Dedup hash needs `account_id`, `source_file`, `row_index`, normalized Decimal precision.** Without these: collisions on (a) two AMZN RSU vesting tranches with identical fields on the same day, (b) corrected statements where amount precision varies, (c) NULL/empty fields normalizing inconsistently.
9. **`canonical_action` enum is missing real values from source files:** `stock_split`, `rsu_vest`, `cash_in_lieu`, `sweep`, `exchange`, `dividend_qualified` (qualified vs ordinary tax treatment differs).
10. **`account_type` enum is missing `brokeragelink`.** Add it; mark `tax_sheltered=True`. Account 89766 `MICROSOFT 401K PLAN` is a plan wrapper containing 653373015 `BrokerageLink` — model with `parent_account_id` FK + `is_plan_wrapper=True` to prevent double-counting. Cross-account `Exchange Out 89766 → TRANSFERRED FROM 653373015` is one event, not two.
11. **`position_snapshot` UNIQUE on (account_id, as_of, symbol) breaks** — Vanguard has the same VMFXX symbol twice in one account (settlement vs reinvest buckets). Cash rows are NULL symbol; SQLite NULL ≠ NULL so cash dedup also fails. → Replace UNIQUE with `source_row_hash` like transactions; aggregate same-symbol multi-rows OR include bucket index.
12. **Reinvested-dividend "pick one" advice is wrong.** Fidelity/Schwab 2-row vs E*TRADE 1-row carry different precision. → Always store 2 rows; synthesize the missing dividend row for E*TRADE; link via `paired_transaction_id` FK.
13. **Schwab `'01/16/2025 as of 01/15/2025'`** date format requires splitting → leading = `settlement_date`, "as of" = `trade_date`.
14. **E*TRADE 2-digit years** (`05/01/26`) in `DownloadTxnHistory.csv`, 4-digit years (`1/2/2025`) in `tradesdownload.csv`. → Try-both date parser.
15. **E*TRADE `tradesdownload.csv` Buy rows have positive Net Amount** (opposite of project sign convention). → Negate on store: `amount = -abs(net_amount)` for Buy rows.
16. **Schwab Positions files have trailing `Cash & Cash Investments` and `Positions Total` rows** that look like data. → Filter by symbol in skip-list.
17. **Vanguard files mix positions and transactions in one file with multiple section headers** (positions, transactions, empty Run-Date section). → Section-state-machine parser, not single header.
18. **Vanguard "copy" files contain different accounts**, not duplicates. Original=208182839-01 (529), copy=252341309-01 (second 529); brokerage `OfxDownload copy.csv` differs from `OfxDownload.csv`. → Don't filter by filename; ingest by content.
19. **Fidelity HSA 241527012** is in every history file but missing from IDEATION's accounts table. Add: `account_type=hsa`, `tax_sheltered=True`. Also explicitly add **529 account 252341309-01**.

### P2 (refine in plan)

20. **Schema additions:** `cusip` (E*TRADE only, useful for 1099-B), `commission` separate from `fees` (different tax treatment), `status` ('imported'|'confirmed'|'rejected' per CLAUDE.md never-delete rule), `updated_at` on all tables, `unadjusted_cost_basis` + `lt_gain_loss` + `st_gain_loss` on `realized_gain_loss` (needed for 8949 wash-sale reporting).
21. **StrEnums** for `broker`, `account_type`, `canonical_action`, `term`, `status` — match existing `Direction`/`TaxCategory`/`Entity` pattern; auto-generate CHECK constraints.
22. **Decimal precision specs:** `Numeric(18, 8)` for `quantity`/`price` (fractional shares can have 8+ places), `Numeric(12, 2)` for currency fields. Without this, SQLAlchemy on SQLite defaults to REAL (float) → corrupts cost-basis math.
23. **Fidelity sells use NEGATIVE quantity** (`-0.836`); Schwab sells use POSITIVE quantity. Pick one canonical sign convention and normalize in adapter.
24. **Quantity-sign canonical decision:** quantity always positive; direction in `canonical_action` and amount sign.
25. **Hand-write the migration** (not autogenerate). Schema has SQLite UNIQUE/NULL gotchas, multi-value CHECK constraints, FKs.
26. **Flat module structure** (`src/adapters/fidelity_csv.py`, etc.) — no `brokerage/` sub-package. Matches every other adapter.
27. **`csv_helpers.py` is premature.** Per CLAUDE.md "three similar lines is better than abstraction." → Build one adapter first, extract on second duplication.
28. **CRLF line endings** in some files — open with `newline=''`.
29. **E*TRADE metadata header is 6 rows (not 4-5)** — detect by scanning for actual header row, not fixed skip count.
30. **Fidelity 2 leading blank rows + 5-line trailing disclaimer** must be stripped.
31. **Vanguard 529 has third empty section header** for `Run Date,Transaction Activity` — skip it.
32. **`canonical_action` should distinguish `dividend_qualified` from `dividend_ordinary`** — E*TRADE & Schwab both flag this and tax treatment differs.

### P3 (nice-to-have / track for Phase 2)

33. `avg_cost_basis` per-share on positions (Fidelity exposes it, others compute).
34. Skip-list for old Vanguard accounts in `OfxDownload copy.csv` (37737894, 32628019, 59309844 — historic/closed).
35. Display-name vs internal `etrade` enum (Morgan Stanley E*TRADE rebrand).
36. `Date downloaded` footer parsing with `dateutil` (multiple formats observed).
37. Vanguard 529 `Gross Amount` vs `Net Amount` divergence warning (currently always equal).

---

## Revised recommendation

**Option C (hybrid) with all corrections above.** Specifically:

- **Deprecate `src/adapters/brokerage_csv.py`** (rename to `.deprecated`, keep code visible until Phase 2 confirms no need to revive). Reuse its REQ-IDs.
- **Five tables**, all isolated from `Transaction`:
  - `account` (with `parent_account_id`, `is_plan_wrapper`, StrEnum types)
  - `brokerage_transaction` (with `cusip`, `commission`, `status`, `updated_at`, `paired_transaction_id`, `source_row_hash` from `compute_source_hash()`)
  - `position_snapshot` (with `source_row_hash` instead of UNIQUE-on-symbol)
  - `realized_gain_loss` (with `unadjusted_cost_basis`, `lt_gain_loss`, `st_gain_loss`, `wash_sale`, `disallowed_loss`)
  - `tax_form_detail` (1099-DIV/INT/B box-level data from Schwab `XXXX-X724`)
- **Inherit `BaseAdapter`**, return `AdapterResult`, write `IngestionLog` per run.
- **No `csv_helpers.py` initially** — write Fidelity adapter first (most file kinds), extract shared helpers only after second adapter shows duplication.
- **Always-2-row reinvest model** with `paired_transaction_id` linking dividend ↔ buy.
- **Hand-written Alembic migration**.
- **Amend REQ-005** in `requirements/current.md` to include Fidelity.
- **Defer `tax_form_detail` loader** to Phase 1.5 if needed — it's the most schema-different and the data is already in user's PDF 1099s.

## User decisions (locked at gate, 2026-05-06)

1. **Existing `brokerage_csv.py` parked untouched.** Different purpose (1099-B → existing P&L). New adapters live alongside in isolated tables. One-line docstring note added marking it deferred to Phase 2.
2. **REQ-005 amended** to include Fidelity.
3. **Parent/child 401k model adopted:** `account.parent_account_id`, `account.is_plan_wrapper`. MS 401k 89766 wraps BrokerageLink 653373015; transactions on 89766 marked internal-transfer.
4. **Goal = net worth + performance, NOT tax.** Drops the `tax_form_detail` table and Schwab `XXXX-X724` 1099 form CSVs from Phase 1. Realized G/L still loaded (it serves performance, not just tax) via the dedicated `*_GainLoss_Realized_Details_*.csv` files. **4 tables, not 5.**
5. **Two 529 beneficiaries:** Aiden → 208182839-01, Emerson → 252341309-01. `account` table gets a `beneficiary` field (free-text).

## Final scope — 4 tables

1. `account` — broker, account_number, account_name, account_type (StrEnum), entity, tax_sheltered, parent_account_id (FK self), is_plan_wrapper, beneficiary, created_at, updated_at
2. `brokerage_transaction` — account_id (FK), trade_date, settlement_date, action (broker-native), canonical_action (StrEnum), symbol, cusip, description, quantity (Numeric 18,8, always positive), price (Numeric 18,8), amount (Numeric 12,2, signed), commission, fees, paired_transaction_id (FK self, for div+reinvest pair), status, source_file, source_row_hash, raw_data, created_at, updated_at
3. `position_snapshot` — account_id (FK), as_of, symbol, description, quantity, price, market_value, cost_basis, avg_cost_basis, unrealized_gain, source_file, source_row_hash, raw_data, created_at
4. `realized_gain_loss` — account_id (FK), symbol, description, opened_date, closed_date, quantity, proceeds, cost_basis, unadjusted_cost_basis, gain_loss, lt_gain_loss, st_gain_loss, term (StrEnum), wash_sale, disallowed_loss, source_file, source_row_hash, raw_data, created_at, updated_at

## Files-out-of-scope for Phase 1

- `schwab/XXXX-X724*.CSV` — 1099 form data; deferred. Already in PDFs.
- `etrade/MS_2025_1099-CONS_*.pdf` and `TaxDocuments_*.pdf` — PDF tax docs.
- E*TRADE `tradesdownload.csv` — superset of `DownloadTxnHistory.csv` for trades only; pick `DownloadTxnHistory.csv` as primary, ignore `tradesdownload.csv` (its sign convention is opposite, and `DownloadTxnHistory.csv` already has trades).
