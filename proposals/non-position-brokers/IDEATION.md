# Surface non-PositionSnapshot brokers in net-worth + accounts views

## Symptom

After Phase 4 landed, the four new brokers (`fg_annuity`, `gsk_pension`,
`nw_mutual`, `franklin_templeton`) hold ~$711k of real value but are **invisible** in:

1. **Live net-worth headline** (`/api/brokerage/networth`) — sums to $7,281,199 (only the original 4 brokers); ignores the new ones.
2. **Accounts table** on the dashboard — 7 accounts (4 NW Mutual + 1 each FG/GSK/FT) hidden because the dashboard filters `as_of !== null` and these accounts have no `PositionSnapshot.as_of`.

## Root cause

`src/reports/brokerage_summary.py` has two functions that drive the dashboard:

- `compute_net_worth(session)` — line 289
- `get_account_summary(session)` — line 342

Both call `_latest_snapshot_rows(session)` which queries **only** `PositionSnapshot`. The Phase 4 adapters write `AccountBalanceSnapshot` (because their source data is statement-level totals, not per-position holdings — there's no per-symbol breakdown in a GSK pension PDF).

Result: any account whose data lives only in `account_balance_snapshot` is silently dropped from both functions.

## Options

| Option | Approach | Tradeoffs |
|---|---|---|
| **A (recommended)** | Add a sibling `_latest_balance_snapshot_per_account(session)` returning the latest `AccountBalanceSnapshot` per account_id. Merge with PositionSnapshot data: for each account, prefer PositionSnapshot if present, else fall back to AccountBalanceSnapshot. `compute_net_worth` and `get_account_summary` consume the merged view. | One new helper, two call sites updated. Each account contributes once. Plan-wrapper exclusion + zero-snapshot count semantics preserved. Per-symbol breakdowns (`top-holdings`) untouched — those still need PositionSnapshot. |
| **B** | Backfill PositionSnapshot rows from AccountBalanceSnapshot in the Phase 4 adapters (synthesize one fake "BALANCE" position per account). | Hides the architectural distinction between holdings-level data and balance-only data. Pollutes `top-holdings` with fake "BALANCE" rows. Worse audit trail. |
| **C** | Two separate endpoints: `/networth` (PositionSnapshot only) and `/networth-with-balances` (merged). Dashboard chooses. | Permanent forking. Two truths to reconcile. The headline tile would still pick the wrong one. |

**Pick A.** It's the smallest change that gives the dashboard a single coherent net-worth picture and respects what each adapter naturally produces.

## Detailed design (Option A)

New helper in `src/reports/brokerage_summary.py`:

```python
def _latest_balance_snapshot_per_account(
    session: Session,
) -> dict[str, AccountBalanceSnapshot]:
    """Return {account_id: latest AccountBalanceSnapshot row} for accounts that
    have any AccountBalanceSnapshot. Used to surface balance-only brokers
    (FG, GSK, NW Mutual, FT) whose data lives in account_balance_snapshot
    rather than position_snapshot."""
```

Merge function, also new:

```python
def _per_account_value(session) -> dict[str, dict]:
    """Returns {account_id: {market_value, as_of, source}} where source is
    'position' or 'balance'. PositionSnapshot wins when both exist (an account
    that's been actively imported via per-position data is more current than
    a statement total)."""
```

Both `compute_net_worth` and `get_account_summary` consume this merged view. Plan-wrapper exclusion still applies (the `is_plan_wrapper` flag is on `Account`, independent of which snapshot type wrote the value).

`zero_snapshot_account_count` redefined as: accounts with NEITHER a PositionSnapshot NOR an AccountBalanceSnapshot. After Phase 4 this should be near-zero (everything's covered).

## Affected callers (verify they still work)

- `compute_net_worth` — used by `/api/brokerage/networth`, `/api/brokerage/top-holdings` (passes `nw["total"]` as denominator for portfolio-pct), `weekly P&L report`.
- `get_account_summary` — used by `/api/brokerage/accounts` (which feeds the dashboard table).

Both `top-holdings` itself and any per-symbol view stays on PositionSnapshot — those are inherently per-position queries.

## Test scenarios (TDD)

1. **`compute_net_worth` includes balance-only accounts.** Seed Account(broker='fg_annuity') with one AccountBalanceSnapshot $660k. Assert `total` includes the $660k and `by_broker['fg_annuity']` == $660k.
2. **PositionSnapshot wins over AccountBalanceSnapshot when both exist.** Seed Account with both: PositionSnapshot.market_value $100k from yesterday and AccountBalanceSnapshot.balance $90k from last week. Assert account contributes $100k (the live position).
3. **`get_account_summary` exposes balance-only accounts with non-null `as_of`.** Seed NW Mutual account with one AccountBalanceSnapshot at date X. Assert the row appears in the returned list with `as_of = X` and `market_value = balance`.
4. **Plan-wrapper exclusion still fires.** Account flagged `is_plan_wrapper=True` with an AccountBalanceSnapshot must NOT contribute to net worth.
5. **`zero_snapshot_account_count` reflects merged sources.** Account with no PositionSnapshot and no AccountBalanceSnapshot → counted. Account with only an AccountBalanceSnapshot → NOT counted.

## Out of scope (deferred)

- The `networth-history` chart endpoint already works through manual SQL rollups for now — fixing it to forward-fill is a larger architectural change tracked separately.
- The E*TRADE adapter writing duplicate position rows is a separate bug worth its own fix.
