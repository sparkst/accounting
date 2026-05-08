# PLAN — Net-worth chart forward-fill + re-pricing

3 tasks. ~5 SP.

### T1 — `_per_account_value_at(session, target_date)` helper  (SP 2)

**Path:** `src/reports/brokerage_summary.py` (extend), `src/reports/test_brokerage_summary.py` (extend).

**Tests first:**
1. `target_date` after the only snapshot date → returns the snapshot value (forward-fill).
2. `target_date` before any snapshot → account omitted.
3. Account with PositionSnapshot at X + HistoricalPrice for that symbol at later date Y → returns shares × close[Y] (re-priced).
4. Account with PositionSnapshot at X but no HistoricalPrice at Y → returns snapshot's stored market_value (no re-pricing).
5. Account with both PositionSnapshot and AccountBalanceSnapshot → PositionSnapshot wins (matching existing precedence).
6. Multiple positions per account: re-priced and stored-fallback values aggregate per account.
7. Pre-loading optimization works: a single bulk query loads all snapshots, then per-date walks are O(accounts).

**Impl:**
- Helper `_load_history_state(session)` returns indexed in-memory structures: `{account_id: [(as_of, [PositionSnapshot])]}`, `{account_id: [(as_of, balance)]}`, `{(symbol, trade_date): close}`.
- `_per_account_value_at(session, target_date, *, history_state=None)`: uses pre-loaded state if provided, else loads it. Walks per account: latest position-snapshot at-or-before target → recompute each held position via HistoricalPrice (with weekend-rollback to nearest earlier weekday) or fallback to stored market_value. If no PositionSnapshot, latest AccountBalanceSnapshot at-or-before target.
- `_per_account_value(session)` becomes thin wrapper: `_per_account_value_at(session, _today())`.

### T2 — Refactor `networth_history` to forward-fill + re-price  (SP 2)

**Path:** `src/api/routes/brokerage.py:779`.

**Tests first:** add to `src/api/test_brokerage_routes.py`:
1. Series start = earliest snapshot date across all sources.
2. Weekly granularity: ~52 points per year of data.
3. Each point uses forward-filled+re-priced values per account.
4. "today" point always last regardless of week alignment.
5. Filter params (tags_include, account_ids) still work; pre-existing tests should still pass.
6. Empty DB → empty series.
7. Closed-expected-account exclusion still fires.
8. New `granularity` query param: daily / weekly / monthly. Default weekly.

**Impl:**
- Pre-load history state once via `_load_history_state`.
- Generate target dates per granularity from earliest snapshot to today.
- For each target date: call `_per_account_value_at(..., history_state=state)`, apply filters, sum.
- Append "today" point unconditionally.

### T3 — Live verify + commit  (SP 1)

- Restart API after T1+T2 land.
- Curl `/api/brokerage/networth-history?include_unmatched=true` and confirm:
  - Series spans 2017→today
  - "Today" point ≈ current $8.0M (not the artificially-low partial sums)
  - Smooth weekly progression (no catastrophic drops)
- Refresh dashboard, eyeball the chart.

## DoD

- [ ] All 3 tasks landed.
- [ ] `pytest src/reports/ src/api/test_brokerage_routes.py` green.
- [ ] Live chart shows smooth weekly series ending at today's full ~$8M.
- [ ] Forward-fill + re-pricing covered by tests.
- [ ] Review-loop converges to 0 P0+P1.
- [ ] Verifier returns PASS.
