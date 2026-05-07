# Brokerage Phase 3 — IDEATION

> Pipeline: `005-page` (thorough preset). Predecessor: drawer_accounting_decisions_b5766d3971305830dcbd390c (Phase 2 session-end).

## What the user asked for

1. Tables with full sorting, filtering, and type-ahead search.
2. Balance summary that updates based on the visible/filtered accounts.
3. Visibility into accounts present in the user's external aggregator (Empower-style list, ~18 accounts pasted) but missing from our DB.
4. Historical performance of individual holdings.
5. Performance vs benchmarks (S&P 500, others).
6. A source of historical stock prices (daily resolution, not real-time).
7. Pull historical info from `~/Downloads/Savings & Retirement Plan.xlsx` — 13 sheets, with the heavy hitters being:
   - `Account Summary`: 36 historical balance snapshots (2017-08-08 → 2024-12-11) × ~30 accounts. Quasi-quarterly cadence.
   - `Historical Prices`: ~1,000 symbols × 4 timepoints (yesterday / 30d / 90d / 1y).
   - `TD GainLoss Raw` + `SB Raw`: ~2,000 lot-level cost-basis rows from TD Ameritrade & Sharebuilder.
   - Plus modeling sheets (Retirement Calculator, Savings Model, College Planning) — likely out of scope for a net-worth dashboard.

(Out of scope per user: portfolio improvement suggestions.)

---

## Existing surface (Phase 2)

- API: `/api/brokerage/{networth, accounts, top-holdings, recent-transactions, realized-gl, data-integrity}`.
- Dashboard: `dashboard/src/routes/brokerage/+page.svelte` (674 lines) — three tables (Accounts, Top Holdings, Recent Transactions) + integrity warnings + net-worth headline.
- Models: `Position`, `PositionSnapshot`, `BrokerageTransaction` already exist.
- Live total: $6.82M across 18 visible accounts, computed identically by CLI / API / dashboard.

---

## Feature analysis

### F1 — Sort / filter / type-ahead search (frontend)

**Tables**: Accounts (~18 rows), Top Holdings (50 rows), Recent Transactions (100 rows). All three.

Options:
- **A — Svelte 5 runes hand-roll.** `$state` for query/sort, `$derived` for filtered rows. ~80 lines per table. No new deps. Fits the existing 674-line page.
- **B — Library.** `@tanstack/svelte-table` or `svelte-headless-table`. Powerful but ~30KB and overkill at this scale.

Recommend **A**.

### F2 — Filtered total

Aggregate the visible (post-filter) accounts into the headline. Falls out naturally from `$derived` if F1 uses runes. Show both totals when a filter is active:

```
Visible: $4,432,118  (3 of 18 accounts)
All accounts: $6,820,304
```

Recommend keeping the headline as **two-line** when filtered, single-line when not. Pure F1 work.

### F3 — Missing accounts

Need an "expected accounts" source of truth. Empower has no public API. Plaid is overkill for a personal tool.

Options:
- **A — Static table.** New `expected_account` table (institution, name, last_4, status). Manual add/remove. Seed once from Excel `Account Summary` rows + the user's pasted list.
- **B — Pull from XLSX every run.** XLSX becomes the source of truth. Brittle.
- **C — Plaid integration.** Out of scope; multi-week.

Recommend **A**. Tiny CRUD UI on `/brokerage` (or a settings page) to maintain the list. "Missing" = expected but no `BrokerageAccount` row in last 60 days.

### F4 — Per-holding historical performance

Needs **(a)** quantity history (already have `PositionSnapshot`), **(b)** price history (F6), **(c)** lot cost basis (already in `Position.cost_basis`).

Once F6 lands, computing a per-symbol value series is `position_snapshot.qty × historical_price.close` joined by date. UI: a per-holding detail page (`/brokerage/holdings/[symbol]`) with 3M / 6M / YTD / 1Y / All toggles, a value line, and per-period $/% return. Reuse the toggle styling already on the page.

### F5 — Benchmark comparison

Once F6 is in for benchmark symbols (SPY, VTI, QQQ), compute a "what if I'd held SPY instead" curve:

- Take portfolio value at window start as principal.
- Buy SPY at that day's close, hold to window end.
- Plot both lines normalized to start = 100.
- Headline: "Portfolio +X% vs S&P 500 +Y% over 1Y."

Start with one-line single benchmark. Multi-benchmark dropdown is a follow-up.

### F6 — Historical price data source

Options (free / paid):

| Source | Free tier | Reliability | Daily EOD | Notes |
|---|---|---|---|---|
| yfinance | unlimited (unofficial) | breaks ~yearly | ✅ | Python lib, no key, popular |
| Stooq CSV | unlimited | high | ✅ | One CSV per symbol; no key |
| Alpha Vantage | 25/day | high | ✅ | Too restrictive |
| Tiingo | 50 symbols free, $10/mo unlimited | high | ✅ | Clean API |
| Polygon | 5/min free, $29/mo | high | ✅ | Overkill for daily |

Recommend **yfinance** for MVP, behind a small adapter so swapping to Tiingo (paid) is trivial if yfinance flakes. Daily cron via launchd at ~5pm Pacific to fetch yesterday's closes for held symbols + benchmarks (~150 symbols, fits any free tier).

New table: `historical_price (symbol, date, close, source, ingested_at)`. Backfill 10 years on first run; daily incremental after.

### F7 — XLSX import

Two clear wins, one maybe:

- **Win A — `Account Summary` → `account_balance_snapshot`.** 36 dates × ~30 accounts ≈ 1,000 rows. Gives us a 7-year net-worth history immediately, without waiting for new live snapshots to accrue. Mapping account names from XLSX → modern `BrokerageAccount` rows is the only friction (some have changed institutions / numbers). Plan: build name-mapping table in code, surface unmatched in the integrity panel.
- **Win B — `Historical Prices` → `historical_price` seed.** 1,000 symbols × 4 dates. Bootstrap data while F6 backfill is running. Optional once F6 is solid.
- **Maybe — `TD GainLoss Raw` / `SB Raw` → cost-basis backfill.** Rich (lot-level dates from 2009!) but reconciling with current `Position.cost_basis` is tricky. Defer.

---

## Recommended scope split

**Phase 3a — Frontend wins (1 day)**
F1 + F2. Pure frontend; no DB/migration risk. Ship first, get the daily-driver UX improvements immediately.

**Phase 3b — Historical balance backbone (1 day)**
F7 Win A only. New `account_balance_snapshot` table + Alembic migration. Importer + name-mapping. Net-worth-over-time chart on the dashboard.

**Phase 3c — Price pipeline + benchmarks (1.5 days)**
F6 + F5. yfinance adapter, `historical_price` table, daily launchd job, "vs S&P 500" overlay on the historical chart.

**Phase 3d — Per-holding pages (1 day)**
F4. New `/brokerage/holdings/[symbol]` route. Time-series value, period returns, cost-basis comparison.

**Phase 3e — Account coverage (0.5 day)**
F3. `expected_account` table, tiny admin UI, "missing accounts" panel.

Total ≈ 5 working days. Each sub-phase ships independently.

---

## Decisions (locked 2026-05-07)

1. **Scope** — All five sub-phases (3a → 3e) in this pipeline.
2. **Price source** — yfinance, behind a swappable adapter.
3. **XLSX scope** — Account Summary (balances) + Historical Prices (seed) + TD GainLoss Raw + SB Raw (lot-level cost basis). Modeling sheets out of scope.
4. **Expected-accounts seed** — Union of XLSX `Account Summary` account names AND the user's pasted Credit Karma list (~18 accounts). During execute, walk through each candidate with the user to mark **active vs closed**; only "active" entries become the source of truth for the missing-accounts panel.

## Implications of the decisions

- **GainLoss reconciliation is now in scope.** Treat it as a "Phase 3b extension": load lots into a new `cost_basis_lot` table keyed on (symbol, account, open_date, qty). Don't try to overwrite live `Position.cost_basis` from XLSX — instead expose the historical lots as a per-symbol view that supplements current data. This avoids the "live data is wrong because of stale XLSX" trap.
- **Credit Karma list is the user's current external aggregator** (not Empower as I'd assumed). Affects only documentation; logic is unchanged.
- **Interactive account-classification step** lives in the execute phase, not the plan. Plan just notes that the importer will produce a candidate list and stop for confirmation before activating any rows.

---

## Dependencies / sequencing

```
F1 ───────────────────────►
F2 (depends on F1's filter state) ──►
F7 (XLSX import: balances) ──► historical net-worth chart
F6 (price source) ──► F5 (benchmarks) ──► F4 (per-holding history)
F3 (expected accounts; can run anytime, but seeded from F7 data is cleanest)
```

F1+F2 are independent. F4 depends on F6. F7 unblocks the chart F4 will live alongside.
