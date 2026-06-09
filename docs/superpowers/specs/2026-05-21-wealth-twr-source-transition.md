# Wealth TWR engine — source-transition smoothing + overlay empty-state UI

**Status:** Spec, ready to build. Follow-up to `2026-05-20-wealth-twr-account-balance-fallback.md`.
**Repo:** `sparkry-crm`. Branch: `feat/wealth-twr-source-transition`. Base: `main`.

---

## 1. Problem

After deploying the `account_balance_snapshot` fallback, the wealth dashboard's TWR · 3Y card shows `+69.60%` (a believable number; the user's net worth is up ~$4M / +102% over the tracked era), but the chart's daily-chain TWR line spikes to **+406%** at the right edge — about 6× the headline value. The chart and the KPI should agree within 0.001 per the contract test.

### Root cause

Two data sources contribute MV depending on date and account:
- `account_balance_snapshot`: account-level totals, populated by XLSX seed (~$7.99M at 2026-05-07 across 18 accounts).
- `position_snapshot`: per-position `qty × price`, populated by Plaid daily sync + broker CSV imports (started arriving 2026-05-03; only covers 6–18 specific accounts per day, varying).

When account A transitions from account_balance to position_snapshot between day D−1 and day D, the two sources can disagree on A's MV by a large amount (different pricing methods, different valuation date, partial-position capture). The engine's daily formula `r = (V(d) − V(d−1) − CF(d)) / V(d−1)` then reports the source-disagreement as a phantom market return.

Prod data shows the transition window 2026-05-03 → 2026-05-07 has multiple accounts switching sources daily, compounding into a ~6× TWR-index spike. The chart's right-edge value is mathematically what the formula produces but is operationally meaningless — it reflects bookkeeping, not market behavior.

### Secondary issue

When a user adds an overlay symbol with no `historical_price` data on D1 (e.g., AMD), the chart silently renders no line and emits no UI signal. The chip appears in the toolbar but the user has no way to know why nothing rendered.

## 2. Proposed solution

### 2.1 Source-transition smoothing (synthetic non-market CF)

For each in-scope account, find `positionEraStart[account] = min(isoDate(asOf))` across `position_snapshot` rows. On that account's transition day, the engine emits a **synthetic non-market CF** equal to `(position_value − account_balance_value)` for that account. The engine's existing CF-subtraction in the daily-r numerator cancels the source-change effect, so the day's portfolio-level `r_d` reflects only intra-source market returns.

This is purely additive math — no per-account daily r needed, no architecture change to the aggregated-portfolio formula. Implementation is ~30 lines in `dailyTwrSeries`.

```
for each account A in scope:
    posDates = sorted asOf dates for A in position_snapshot
    if posDates is empty: skip (no transition)
    transitionDay = posDates[0]
    if transitionDay outside [start, end]: skip
    balanceValueDayBefore = account_balance_snapshot lookup for A on transitionDay - 1
    if balanceValueDayBefore is None: skip   # ← see "null-case" note below
    posValueOnTransition = position_snapshot MV for A on transitionDay
    syntheticCf[transitionDay] += posValueOnTransition - balanceValueDayBefore
```

**Null-case handling (load-bearing):** when account A has NO
`account_balance_snapshot` row on/before `transitionDay - 1`, the lookup
returns `null` (not `0`) and the synthetic CF for that account is **skipped
entirely**. Treating the missing row as a zero balance would fabricate a
synthetic CF equal to the full position MV on day 1, which the engine's
CF-subtraction would then use to zero out A's legitimate first-day market
return. The null-skip is the correct architecture; the only side effect of
not adding a synthetic CF is that the account's first-day MV jumps from
`$0` (V_prev) to `posValueOnTransition` (V_curr) — but the engine's
existing `if (!vPrev.isZero())` guard already handles that case by
emitting `r=0` for the day (no return computable from a zero base).

Then in the daily loop: `cf_total = (real CF) + (synthetic CF)`.

**Why this works:** the synthetic CF exactly absorbs the source-disagreement on the transition day. The portfolio's `V_curr` already includes the position value; `V_prev` includes the balance value. `(V_curr − V_prev − syntheticCf)` cancels the discontinuity, leaving the intra-source market return component.

**Caveats:**
- The synthetic CF doesn't represent real money flow. It's a bookkeeping adjustment, distinct from `external_in/out`. The XIRR engine (which uses real money flows only) is NOT affected — the synthetic CF only feeds `dailyTwrSeries`.
- The monthly `timeWeightedReturnBreakdown` is unchanged because its monthly cash-flow weighting already smooths most transitions. The TWR KPI was already approximately right; the chart line catches up to it after this fix.

### 2.2 Overlay empty-state UI

In `NetWorthChart.svelte`, mirror the existing "C5: per-benchmark empty-state notice" block for `overlays`:

```svelte
{#each (data?.overlays ?? []) as o (o.symbol)}
    {#if !hiddenSeries.has(`overlay:${o.symbol}`) && o.series.length === 0}
        <p class="nw-empty-note">{o.symbol}: no historical price data</p>
    {/if}
{/each}
```

This is a 7-line addition. No engine work needed.

## 3. Acceptance criteria

### Engine

- [ ] `dailyTwrSeries` accepts the existing parameters; computes per-account `positionEraStart` from `snaps` internally.
- [ ] On each account's `positionEraStart`, a synthetic CF entry is added to `cfByDate` equal to `(positionMv − balanceMv)`. The original `cfByDate` from real `buildBrokerageCashFlows` is preserved unchanged.
- [ ] Accounts with no `position_snapshot` data ever: no synthetic CF.
- [ ] Accounts whose `positionEraStart` falls outside `[start, end]`: no synthetic CF (the transition isn't in the rendered window).
- [ ] Contract test: with a fixture that has one account transitioning mid-window from account_balance to position_snapshot, `twrSeries[last].twrPct` matches the value computed by the monthly Modified-Dietz (`timeWeightedReturnBreakdown`) within 0.001.
- [ ] Regression: all 14 existing fallback tests still pass (no synthetic CF in fixtures without source transitions).

### UI

- [ ] When a user adds an overlay symbol that has no `historical_price` data, an empty-state notice renders below the chart: `${symbol}: no historical price data`. Notice goes away when the user removes the chip or when data becomes available.

### Tests

- [ ] At least 3 new vitest cases:
  1. Account transitions mid-window: synthetic CF cancels phantom return, twrPct[end] matches monthly KPI within 0.001.
  2. Account transitions before window start: no synthetic CF emitted; engine behaves as if account is always position-only.
  3. Multiple accounts transition on different days: synthetic CFs aggregate correctly.

### Quality gates

- [ ] `pnpm exec tsc --noEmit` clean
- [ ] `pnpm exec svelte-check` 0 errors
- [ ] `pnpm exec vitest run` all green
- [ ] `pnpm build` clean

### Live smoke

- [ ] After deploy, visit https://internal.sparkry.ai/wealth: confirm the TWR chart's right-edge value is within ~5pp of the KPI's TWR · 3Y headline (the +406% spike collapses to roughly +70%).
- [ ] Add overlay symbol "FOOBAR" (or another known-not-in-D1 symbol); confirm the "no historical price data" notice appears.

## 4. Out of scope

- Backfilling AMD or other missing price data into D1 (separate ticket; existing `twelve-data-ingest.ts` can be wired to fetch on demand).
- Per-account daily r calculation (deeper architecture change; current portfolio-aggregate formula plus synthetic CFs achieves the same correctness for the user's stated need).
- The deferred `mvSource` UI flag (still not strictly needed once source-transitions are smoothed).

## 5. Anchor refs

- Daily TWR chart spec: `2026-05-20-wealth-twr-chart-spec.md`
- Account-balance fallback spec: `2026-05-20-wealth-twr-account-balance-fallback.md`
- Engine source: `sparkry-crm/src/lib/server/wealth/performance.ts`
- Chart source: `sparkry-crm/src/lib/components/wealth/NetWorthChart.svelte`
