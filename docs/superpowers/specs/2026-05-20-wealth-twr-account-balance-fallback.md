# Wealth TWR engine — account_balance_snapshot fallback (REQ-PERF-016 follow-up)

**Status:** Spec, ready to build. Follows the 2026-05-20 TWR-chart ship.
**Repo:** `sparkry-crm`. Branch: `feat/wealth-twr-account-balance-fallback`.
**Scope:** Cloudflare deployment at `internal.sparkry.ai/wealth` only.

---

## 1. Problem

After deploying REQ-PERF-016 (daily TWR-indexed chart line) to prod, the wealth dashboard's "TWR · 1Y" KPI shows `+0.00%` while S&P 500 shows `+28.36%` and net worth is up `+8.5%` YTD. The chart's aggregate line shows the dollar net-worth crossing from $7.43M → $8.11M only at the right edge.

**Diagnosed root cause:** the `dailyTwrSeries` engine reads market values from `position_snapshot` (per-position `qty × price`). On prod-D1:

| Table | Rows | Coverage |
|---|---|---|
| `brokerage_transaction` | 1,978 | 2022-05 → 2026-05 (4 years of CFs ✓) |
| `account_balance_snapshot` | 530 | 2017-08 → 2026-05 (9 years, 21 accounts ✓) |
| `position_snapshot` | 67 | **2026-05 only** (broker CSV + Plaid era, 14 accounts) |

The XLSX historical seed populated `account_balance_snapshot` (account-level MV) but never `position_snapshot` (per-symbol qty), because the XLSX never had per-position breakdowns. Per-position data only began arriving in May 2026 with broker CSV ingest + Plaid.

Result: `mvAt(any date before 2026-05-01)` returns 0 → `V_prev` stays 0 → daily return is 0 every day → twrPct stays at 0%. The engine is doing exactly what it was specified to do; the data behind it just doesn't cover the window the chart defaults to.

## 2. Proposed solution

Extend the engine's `mvAt` lookup to a **two-tier fallback**:

1. For each in-scope account: if `position_snapshot` has a row `≤ targetDate` → use `Σ qty × price` (current behavior; preserves per-symbol accuracy where we have it).
2. Otherwise → fall back to `account_balance_snapshot.market_value` for that account on `≤ targetDate`.

Position-scope (per-symbol drilldown) gets no fallback — `account_balance_snapshot` has no symbol breakdown. Position-scope on a pre-2026-05 date legitimately returns 0; that's correct.

This is purely additive: dates where `position_snapshot` already covers the account see no behavior change. The TWR contract test still passes because the fixture uses `position_snapshot` data exclusively.

## 3. Engine implementation

`src/lib/server/wealth/performance.ts`:

```ts
export interface PerfAccountBalanceSnapshotRow {
  accountId: string;
  asOf: string;        // ISO YYYY-MM-DD
  marketValue: string; // TEXT scale 2
}

type AccountBalanceIndex = Map<
  string,
  { dates: string[]; marketValues: string[] }
>;

function buildAccountBalanceIndex(
  rows: PerfAccountBalanceSnapshotRow[]
): AccountBalanceIndex { ... }

function mvAt(
  snapIdx: SnapIndex,
  priceIdx: PriceIndex,
  accountBalanceIdx: AccountBalanceIndex,
  scope: Scope,
  targetDate: string,
): D {
  let total = new D('0');
  const accountsCoveredByPositionData = new Set<string>();

  // Tier 1: position-level (qty × price or marketValue fallback).
  for (const [key, slot] of snapIdx) {
    const i = findLe(slot.dates, targetDate);
    if (i < 0) continue;
    const accountId = key.substring(0, key.indexOf('|'));
    accountsCoveredByPositionData.add(accountId);
    // ... existing logic ...
  }

  // Tier 2: account-level fallback. Skipped at position-scope (no symbol
  // breakdown to fall back to).
  if (scope.kind !== 'position') {
    for (const [accountId, slot] of accountBalanceIdx) {
      if (scope.kind === 'account' && scope.accountId !== accountId) continue;
      if (accountsCoveredByPositionData.has(accountId)) continue;
      const i = findLe(slot.dates, targetDate);
      if (i < 0) continue;
      total = total.plus(new D(slot.marketValues[i]));
    }
  }

  return total;
}
```

All three call sites of `mvAt` thread the new `accountBalanceIdx` and `scope`:
- `principalGrowthSeries`
- `dailyTwrSeries`
- (no other callers — `timeWeightedReturnBreakdown` reads from precomputed series)

`buildPerformanceResponse` in `performance-routes.ts`:
- New `loadAccountBalanceSnapshotsForScope(db, scope)` helper (mirrors `loadSnapshotsForScope`).
- Pass loaded rows into both engine functions.

## 4. Acceptance criteria

The build is done when **every** item is true:

### Backend
- [ ] `mvAt` accepts and consults `accountBalanceIdx`.
- [ ] When an account has `position_snapshot` data for a date, the account_balance fallback is NOT applied for that account on that date (no double-count).
- [ ] When an account has NO `position_snapshot` data for a date but has `account_balance_snapshot`, `mvAt` returns the account_balance MV.
- [ ] Position-scope queries do NOT apply the account-level fallback (would be wrong-shape data).
- [ ] `principalGrowthSeries` produces correct historical principal/growth for the seed-era window (2017-2025) using the fallback.
- [ ] `dailyTwrSeries` produces a non-zero TWR series matching expected hand-computed values when only account_balance data exists.
- [ ] The 7 existing `dailyTwrSeries` tests still pass unchanged (additive, no regression).

### API
- [ ] `loadAccountBalanceSnapshotsForScope` queries the `account_balance_snapshot` table with the same scope filter shape as `loadSnapshotsForScope`.
- [ ] `buildPerformanceResponse` loads + threads the new rows.

### Tests
- [ ] At least 4 new vitest cases:
  1. Account with position_snapshot AND account_balance_snapshot → position wins (no fallback applied for covered accounts).
  2. Account with only account_balance_snapshot → fallback used.
  3. Mixed portfolio: some accounts position-covered, some balance-only → both contribute correctly.
  4. Position-scope query with no position_snapshot → returns 0 (fallback intentionally NOT applied).
- [ ] A backfill-shape test: feed in a fixture with `account_balance_snapshot` going back 5 years and assert the `twrSeries` produces non-zero monotonic-ish growth across that range.

### Quality gates
- [ ] `pnpm exec tsc --noEmit` clean
- [ ] `pnpm exec svelte-check` 0 errors (warnings unchanged)
- [ ] `pnpm exec vitest run` all green
- [ ] `pnpm build` clean

### Live smoke
- [ ] After deploy, visit https://internal.sparkry.ai/wealth and confirm TWR · 1Y shows a non-zero value comparable to net-worth growth ($634k / $7.43M ≈ 8.5%, modulo CFs).

## 5. Out of scope

- Re-deriving per-position breakdowns from XLSX historical balances (impossible — data doesn't exist).
- Backfilling `position_snapshot` from anything (the fallback removes the need).
- Updating the local Python engine — separate codebase, separate concern.
- Per-holding drilldown for pre-2026-05 dates — that data simply doesn't exist, position-scope correctly returns 0.

## 6. Anchor refs

- Original TWR-chart spec: `2026-05-20-wealth-twr-chart-spec.md`
- Engine source: `sparkry-crm/src/lib/server/wealth/performance.ts`
- Routes source: `sparkry-crm/src/lib/server/wealth/performance-routes.ts`
- Local Python mirror (for reference, currently has the same gap): `accounting/src/analytics/performance.py::_mv_at`
