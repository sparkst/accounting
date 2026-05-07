# Brokerage Phase 3 — Implementation Plan

> Pipeline `005-page`. Decisions locked in IDEATION.md. Five sub-phases, ~5 working days. TDD throughout.

## Schema additions

Single Alembic migration adds four tables. Decimal scales mirror the existing `position_snapshot` precision.

```
historical_price
  symbol            String(32)   PK part   index
  trade_date        Date         PK part   index
  close             Numeric(18,8) NOT NULL
  open / high / low / volume   nullable (we won't ingest from yfinance daily, but reserve)
  source            String(16)   default 'yfinance'
  ingested_at       DateTime     default now
  PRIMARY KEY (symbol, trade_date)

account_balance_snapshot
  id                String(36)   PK uuid
  account_id        FK -> account.id  nullable  (nullable so XLSX rows that don't match yet still load)
  raw_account_name  String(255)  NOT NULL  (XLSX label, kept for audit)
  as_of             Date         NOT NULL  index
  balance           Numeric(14,2) NOT NULL
  source            String(32)   NOT NULL  ('xlsx_2024', 'computed', etc.)
  source_row_hash   String(64)   index     (dedup)
  created_at        DateTime
  UNIQUE (account_id, as_of, source)  -- when account_id is set
  UNIQUE (raw_account_name, as_of, source)  -- fallback

expected_account
  id                String(36)   PK uuid
  institution       String(64)   NOT NULL
  account_name      String(255)  NOT NULL
  last_4            String(8)    nullable
  status            String(16)   NOT NULL   ('active','closed','unconfirmed') default 'unconfirmed'
  source            String(32)   NOT NULL   ('xlsx','credit_karma','manual')
  notes             Text         nullable
  resolved_account_id  FK -> account.id  nullable  (links expected -> live account when matched)
  created_at / updated_at

cost_basis_lot
  id                String(36)   PK uuid
  account_id        FK -> account.id  nullable  (nullable for unmatched-on-import)
  raw_account_name  String(64)   NOT NULL  ('TD Ameritrade','Sharebuilder')
  symbol            String(32)   NOT NULL  index
  security_name     String(255)  nullable
  open_date         Date         NOT NULL
  quantity          Numeric(18,8) NOT NULL
  cost_per_share    Numeric(18,8) NOT NULL
  cost_total        Numeric(14,2) NOT NULL
  wash_sale_adj     Numeric(14,2) nullable
  source            String(32)   NOT NULL   ('xlsx_td_gainloss','xlsx_sb_raw')
  source_row_hash   String(64)   UNIQUE   (dedup)
  created_at        DateTime
```

Migration name: `<rev>_phase3_history_tables.py`. Down-migration drops in reverse FK order.

---

## Tasks

Tasks are numbered for subagent dispatch. Story-point scale: 1 = trivial (<30 min), 2 = small (~1 hr), 3 = medium (half-day), 5 = large (full day).

### Phase 3a — Frontend table UX

**T1. `accounts-filter` reactive store and table refactor (3 SP)** [F1, F2]
- File: `dashboard/src/routes/brokerage/+page.svelte`.
- Extract the three tables (Accounts, Top Holdings, Recent Transactions) into per-table `$state` for sortKey/sortDir/query and `$derived` filtered arrays.
- Type-ahead search: case-insensitive substring match across visible columns. Debounce 100ms.
- Sort: click column header, three-state toggle (asc / desc / none).
- Filter chips: institution multi-select for Accounts, symbol multi-select for Holdings.
- Update headline: when accounts table is filtered, show `Visible: $X.XX (n of N accounts) · All accounts: $Y.YY`.
- Tests: `dashboard/src/routes/brokerage/+page.test.ts` (vitest) — render, type query, assert filtered count and headline.
- **Independent — can ship as standalone PR.**

### Phase 3b — XLSX import + historical balance backbone

**T2. Alembic migration for the 4 new tables (2 SP)**
- `alembic revision --autogenerate -m "phase3 history tables"`. Hand-fix CHECK constraints, FK order in down-migration. Verify `alembic upgrade head` clean on a copy DB.
- Test: `src/db/test_phase3_migration.py` — apply head, assert tables exist, downgrade, assert removed.

**T3. SQLAlchemy models for the 4 new tables (1 SP)**
- Add to `src/models/brokerage.py` (or new `src/models/history.py` if file size warrants). Mirror existing patterns: `_new_uuid`, `_now`, `Mapped[...]`. Include relationships.
- Test: `src/models/test_history_models.py` — round-trip insert/query each table.

**T4. XLSX importer skeleton + Account Summary sheet (3 SP)** [F7]
- New module `src/adapters/xlsx_savings_plan.py`.
- Read `Account Summary` sheet. Detect header row, parse 36 date columns. Emit one `account_balance_snapshot` per (account_name × date) where balance is non-null.
- **Account name mapping**: build a lookup table inline mapping XLSX names → live `Account.id` where possible (e.g. `'Travis IRA Vanguard'` → existing Account row by broker+account_number). Unmatched rows get `account_id=NULL` and surface in a report.
- CLI: `python -m src.adapters.xlsx_savings_plan --file <path> --sheet account_summary`. Print summary: imported / matched / unmatched / dup-skipped.
- Tests: `src/adapters/test_xlsx_savings_plan.py` — fixtures with two-account, three-date mini-sheet; assert dedup by source_row_hash; assert unmatched account_name is preserved.

**T5. Account-name match-table review tool (1 SP)**
- After T4 lands, generate `proposals/brokerage-phase3/xlsx-account-matches.md` listing every XLSX account name with the proposed match (or "UNMATCHED"). User reviews, edits, we re-run import.

**T6. API endpoint: `/api/brokerage/networth-history` (2 SP)**
- Returns array of `{as_of, balance_total}` aggregated across active accounts at each date. Joins `account_balance_snapshot` to its `account_id` and excludes `expected_account.status='closed'` linked accounts.
- Tests: fixtures + assertions.

**T7. Dashboard: net-worth-over-time line chart (2 SP)**
- New section above the accounts table. Chart.js or hand-rolled SVG (we already use Chart.js elsewhere — reuse). Hover shows date + total.

### Phase 3c — Price pipeline + benchmarks

**T8. yfinance adapter (3 SP)** [F6]
- New module `src/adapters/yfinance_prices.py`. Single function `fetch_eod(symbols: list[str], start: date, end: date) -> list[HistoricalPrice]`.
- Wrap `yfinance.download(symbols, start, end, progress=False, auto_adjust=False)`. Handle multi-index columns. Skip rows where Close is NaN.
- Add `yfinance` to `pyproject.toml`.
- Tests: mock `yfinance.download` to return a known DataFrame; assert HistoricalPrice rows have correct shape. **Do not hit live API in tests.**

**T9. Backfill script (2 SP)**
- `scripts/backfill_historical_prices.py`. Discover all symbols from `position_snapshot` + `brokerage_transaction` + benchmark list (`SPY`, `VTI`, `QQQ`, `BND`). Fetch 10-year history. Upsert into `historical_price`.
- DRY-RUN by default. `--apply` to commit. Logs to IngestionLog.

**T10. Daily incremental cron via launchd (1 SP)**
- `com.sparkry.accounting-prices-daily.plist` → runs at 17:30 PT weekdays. `python -m scripts.fetch_daily_prices` fetches yesterday's close for all symbols already in `historical_price`. Restart-on-failure off (cron-style).

**T11. Seed Historical Prices from XLSX (1 SP)** [F7]
- Add `--sheet historical_prices` to `xlsx_savings_plan.py`. Each of the 4 timepoint columns (yesterday, 30d, 90d, 1y) → `historical_price` row at the corresponding `Original Date` row. Source = `'xlsx_2024'`.
- Conflicts with yfinance data: keep the yfinance row, log XLSX overlap.

**T12. API endpoint: `/api/brokerage/networth-history?benchmark=SPY` (2 SP)** [F5]
- Optional `benchmark` query param. Computes a parallel "if you'd held SPY since the first snapshot" series:
  - At each `as_of` boundary: if balance increased, treat the delta as a contribution and "buy SPY at that day's close." If decreased, treat as a withdrawal proportional to current SPY position.
  - Simpler MVP: ignore deltas, take initial balance only and compute SPY-equivalent end value.
- Tests: golden-data fixture with two snapshots + known SPY prices, assert benchmark series matches expected within 1¢.

**T13. Dashboard: benchmark overlay on history chart (2 SP)** [F5]
- Toggle: "Compare to S&P 500." When on, draw the benchmark line on the same chart, normalized to start = portfolio start. Headline: `Portfolio +X% · S&P 500 +Y%`.

### Phase 3d — Per-holding pages

**T14. API endpoint: `/api/brokerage/holdings/{symbol}/history` (3 SP)** [F4]
- Returns per-symbol time series: for each PositionSnapshot of that symbol, return `(as_of, qty, market_value)`. Optionally extrapolate value between snapshots using `historical_price` for the symbol.
- Returns also lots from `cost_basis_lot` matching the symbol.

**T15. Dashboard: `/brokerage/holdings/[symbol]` route (3 SP)** [F4]
- New page. Reuse range toggle (3M/6M/YTD/1Y/All). Chart of value over time. Cost-basis lot table from T14. Headline: current value · unrealized gain · % return.

**T16. TD GainLoss + SB Raw importer (3 SP)** [F7]
- Add `--sheet td_gainloss` and `--sheet sb_raw` to `xlsx_savings_plan.py`. Each row → `cost_basis_lot`. Skip `#N/A` rows. Symbol normalization: `'FB'` → keep as-is (history is what it is).
- Tests: fixture rows for one TD lot + one SB lot.

### Phase 3e — Account coverage panel

**T17. expected_account seed importer (2 SP)** [F3]
- `scripts/seed_expected_accounts.py`. Read XLSX Account Summary names + the hard-coded Credit Karma list (~18 names from the user's session). Insert as `status='unconfirmed'`, `source='xlsx'` or `'credit_karma'`. Dedup by `(institution, last_4 or normalized_name)`.
- After insert, **prompt the user (interactive CLI)** for each row: active / closed / skip. Update status per response. Save resolved_account_id where a live account match exists.

**T18. API endpoint: `/api/brokerage/missing-accounts` (1 SP)**
- Returns list of `expected_account` rows where `status='active'` AND (`resolved_account_id IS NULL` OR latest `account_balance_snapshot` for that account is older than 60 days).

**T19. Dashboard: missing-accounts panel (2 SP)** [F3]
- New section in the integrity area. Lists each missing account: `Institution · Name · Last seen: X days ago / never`. Status pill.

---

## Dependency graph

```
T1 ──── ships independent ──────►
T2 ─► T3 ─┬─► T4 ─┬─► T5 ─► T6 ─► T7
          │       └─► T11 (xlsx prices seed; needs T8 too)
          ├─► T8 ─► T9 ─► T10
          │            └─► T12 ─► T13
          ├─► T14 ─► T15
          │  ▲
          ├──┼─ T16 (lot import)
          │
          └─► T17 ─► T18 ─► T19
```

Critical path: T2 → T3 → T8 → T9 → T12 → T13 (price + benchmark line).

---

## Risk register

- **yfinance API breakage** → mitigated by adapter boundary; can swap to Tiingo/Stooq with a single class change.
- **XLSX account-name matching** → could leave many rows unmatched. T5 review step + raw_account_name preservation lets us iterate without re-importing.
- **Cost basis double-counting** → `cost_basis_lot` is read-only/historical; never overwrites `Position.cost_basis`. UI surfaces "historical lots" separately from current.
- **Snapshot frequency mismatch** for benchmark math → T12 simpler MVP avoids the irregular-cadence trap; document the limitation.
- **Auth still not enforced** (deferred from Phase 2) → out of scope here, but the 4 new endpoints inherit the same risk. Note in CHANGELOG.

---

## Acceptance criteria

- A1. All three tables on `/brokerage` are sortable by every column, filterable by institution/symbol, and have working type-ahead search.
- A2. Filtered headline shows `Visible: $X · All: $Y` when filtered, single value when not.
- A3. Live DB has ≥30 `account_balance_snapshot` rows imported from XLSX, all with non-null `raw_account_name`.
- A4. `/api/brokerage/networth-history` returns ≥36 dated points; first point ≤ 2017-08-31.
- A5. `historical_price` table has ≥250 daily rows for each of {SPY, AMZN, MSFT, GOOG} after backfill.
- A6. `/brokerage/holdings/AMZN` renders a chart with at least 12 monthly data points.
- A7. `expected_account.status` is set to `active` or `closed` for every seeded row (no `unconfirmed` left after the interactive walkthrough).
- A8. Missing-accounts panel renders ≥1 entry if any active account hasn't reported a snapshot in 60+ days.
- A9. Test count ≥ 1500 + 50 net new (currently 1500 baseline). All passing. ruff + mypy clean on new code.
- A10. Net-worth headline matches the sum of `account_balance_snapshot` at the most recent shared `as_of`.
