# PLAN — surface non-PositionSnapshot brokers

3 tasks. Total ~3 SP. All co-located TDD; full quality gate before commit.

### T1 — `_per_account_value` merger helper  (SP 1)

**Path:** `src/reports/brokerage_summary.py` (extend), `src/reports/test_brokerage_summary.py` (extend or create).

**Tests first** (5 scenarios from IDEATION):
1. PositionSnapshot-only account → contributes its market_value, source='position'.
2. AccountBalanceSnapshot-only account → contributes balance, source='balance'.
3. Account with both → PositionSnapshot wins, source='position'.
4. Account with neither → not in returned dict.
5. Most-recent date wins for both snapshot types when there are multiple.

**Impl:** New `_latest_balance_snapshot_per_account(session) -> dict[str, AccountBalanceSnapshot]` (correlated subquery on max(as_of) per account_id, account_id NOT NULL). New `_per_account_value(session) -> dict[str, dict[str, Any]]` returning `{account_id: {market_value, as_of, source, broker, entity, is_plan_wrapper}}`. Internally calls `_latest_snapshot_rows` and the new balance helper, merges with PositionSnapshot precedence.

### T2 — Update `compute_net_worth` to use the merger  (SP 1)

**Tests first**:
1. `total` includes balance-only brokers (seed `fg_annuity` with one ABS $660k, assert total includes $660k and `by_broker['fg_annuity'] == 660000`).
2. PositionSnapshot still wins when account has both.
3. Plan-wrapper exclusion still fires when the value comes from ABS.
4. `zero_snapshot_account_count` excludes accounts that have an ABS but no PositionSnapshot (it should count truly-empty accounts only).

**Impl:** Replace `_latest_snapshot_rows` call with `_per_account_value`. Iterate the merged dict instead of raw PositionSnapshots. Same total/by_broker/by_entity aggregation. `as_of_dates` collected from the merged source dates.

### T3 — Update `get_account_summary` to use the merger  (SP 1)

**Tests first**:
1. Account with only an ABS → returned with `as_of` set (not None) and `market_value` = balance.
2. Account with both → returned with PositionSnapshot value and as_of.
3. Plan-wrapper still flagged.
4. Sort order (market_value desc) correct across mixed sources.

**Impl:** Replace `_latest_snapshot_rows` consumption with `_per_account_value`. Per-account row construction unchanged.

### Build sequence

`T1 → [T2, T3 in any order]`.

### DoD

- [ ] All 3 tasks landed.
- [ ] `pytest src/reports/ src/api/ -q` green.
- [ ] `ruff check src/reports/` clean.
- [ ] `compute_net_worth` total now includes the 4 Phase-4 brokers.
- [ ] `/api/brokerage/accounts` returns NW Mutual policies with non-null `as_of`.
- [ ] Dashboard "Accounts" table now shows the NW Mutual / GSK / FG / FT rows.
- [ ] Review-loop converges to zero P0+P1 across all 4 lenses.
- [ ] Fresh-context verifier returns PASS.
