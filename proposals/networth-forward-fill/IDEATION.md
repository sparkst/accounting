# Net-worth chart: forward-fill + live re-pricing

## Symptom

The Net Worth Over Time chart on `/brokerage` collapses on dates that have no `account_balance_snapshot` rows. The user updates source files only a few times a year — so most days show $0 or near-$0, producing the catastrophic-looking drops we kept hand-patching with SQL.

## Root cause

`GET /api/brokerage/networth-history` (src/api/routes/brokerage.py:779) sums `account_balance_snapshot` rows literally per `as_of` date. There's no forward-fill, no per-account aggregation across dates, no use of `position_snapshot` or `historical_price`.

## Goal

For every date in the historical range, return one row whose `balance_total` equals the sum, across all accounts, of each account's most-defensible value at that date:

| Source available for account at date D | Use |
|---|---|
| Has a PositionSnapshot AND we have HistoricalPrice for every held symbol on date D | Recompute: Σ(shares × close[symbol, D]) — the "live re-priced" view |
| Has a PositionSnapshot but missing prices for some holdings | Use the snapshot's stored `market_value` for those positions, recompute the rest |
| Has only AccountBalanceSnapshot rows | Forward-fill: latest balance at-or-before D |
| Has neither at-or-before D | Contribute 0 (account was not yet seen) |

Plan-wrapper accounts continue to be excluded (`is_plan_wrapper=True`).

## Sampling strategy

The historical series should be one point per **calendar week** (Saturday) plus one for "today" — enough granularity to see trends without exploding the response size. (Current series is already coarse — 37 points over 9 years — so weekly is finer than today.) Configurable via `?granularity=daily|weekly|monthly` query param, default `weekly`.

## Options considered

| Option | Approach | Tradeoffs |
|---|---|---|
| **A (recommended)** | Add merged `_per_account_value_at(session, date)` helper; loop weekly dates and aggregate. Re-uses existing `_per_account_value` (pipeline 002) extended with a date parameter. | Single coherent function. Tests already exist for the merger logic. Adds a date dimension to the existing helper signature. |
| B | Store materialized "daily snapshots" in a new table; recompute nightly via cron. | Faster reads but adds a new table, a launchd job, and a freshness story. Premature optimization for ~9-year × weekly = ~500 points. |
| C | Compute live but cache the response in-memory for 5 minutes. | Correct caching layer but doesn't fix the underlying gap; still need merger logic. |

**Pick A.** Direct extension of the pipeline-002 helper. Defer caching unless query times become a real problem.

## Detailed design

### New helper: `_per_account_value_at(session, target_date)`

Returns `dict[str, _AccountSlot]` of every account's value AS OF `target_date`. Sourcing rules:
1. PositionSnapshot: latest snapshot per account where `as_of <= target_date`. For each held position, look up `HistoricalPrice(symbol, target_date)` (or the next earlier weekday if target_date is weekend / market closed). If found, recompute `market_value = shares × close`. If not, use the snapshot's stored `market_value`.
2. AccountBalanceSnapshot: if no PositionSnapshot exists for the account, latest balance row where `as_of <= target_date`.
3. If neither exists at-or-before target_date → account omitted from result.

The existing `_per_account_value(session)` (no date) becomes `_per_account_value_at(session, _today())` for backward compat.

### Endpoint: `networth_history`

Refactor:
1. Determine series start = earliest `as_of` across all snapshots.
2. Generate weekly target_dates from start through today (Saturdays).
3. For each target_date: call `_per_account_value_at(session, target_date)`, sum across non-wrapper / non-closed-expected-account / filter-passing accounts.
4. Return list of `{as_of, balance_total, account_count}` like before.
5. Always include "today" as the last point regardless of week alignment.

### Performance

~500 weekly dates × ~20 accounts × ~10 positions = ~100k row lookups in the worst case. With proper indexing on `(symbol, trade_date)` (already exists) and `(account_id, as_of)` (already exists on both snapshot tables), each call is a small handful of indexed queries. Pre-load all snapshots once and walk in Python.

Better implementation: load ALL `position_snapshot` and `account_balance_snapshot` rows ordered by `(account_id, as_of)`. For each target date, advance per-account cursors. O(N + dates × accounts) instead of O(dates × accounts × log N).

## Tests

1. Series with no snapshots → empty list (no "today" point manufactured from nothing).
2. Account with one balance snapshot at date X → forward-filled at every date ≥ X.
3. Account with PositionSnapshot at X and HistoricalPrice at later date Y → re-priced at Y.
4. Account with PositionSnapshot at X but no HistoricalPrice at Y → uses snapshot's stored market_value at Y.
5. Plan-wrapper account excluded throughout.
6. Closed expected_account excluded throughout (matches current logic).
7. Filter params (tags_include, tags_exclude, account_ids) still work.
8. "Today" point always present.
9. Granularity param: daily produces ~365× as many points as weekly for a one-year range; monthly produces ~52/4 = 13.

## Out of scope

- Per-day caching (defer until needed).
- Symbol price gap-fill (HistoricalPrice has spotty coverage for many of the user's holdings — VMFXX, VTSAX, MGV, MGK, VTTHX, etc.). For now, accounts with positions whose ticker isn't in HistoricalPrice will fall back to the stored market_value — accurate as of the snapshot, not live. Fixing the price coverage is a separate effort (adapter to backfill more tickers).

## Recommendation

Implement Option A. Default to weekly. Single endpoint refactor. Existing tests for `_per_account_value` carry over. New tests for the date dimension + re-pricing precedence.
