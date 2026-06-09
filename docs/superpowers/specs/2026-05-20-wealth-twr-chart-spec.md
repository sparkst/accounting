# Wealth Net-Worth Chart — Daily TWR-Indexed Line (REQ-PERF-016 second cut)

**Status:** Spec, ready to build. Replaces the failed first cut (commit `eb36fd4` on `feat/wealth-performance` in `sparkry-crm`, since reverted in `c12b400`).
**Depends on:** REQ-PERF-001..015 already shipped on Cloudflare (`/wealth/api/brokerage/performance/*` endpoints, `cash_flow_type` column populated in D1, `src/lib/server/wealth/performance.ts` engine).
**Scope:** Cloudflare deployment at `internal.sparkry.ai/wealth` only.
**Out of scope:** Local accounting dashboard chart, principal-vs-growth stacked-area chart on holding pages (REQ-PERF-018), period table (REQ-PERF-016 second half).

---

## 1. The problem the first cut failed to solve

The chart at `/wealth` renders the net-worth aggregate line on a **dollar axis** (left) while SPY/QQQ overlays render on a **percent axis** (right) — two incompatible scales on one chart. "Did my portfolio beat SPY?" is unanswerable from the visual.

The first cut tried to fix this by rebasing the existing `networth-history` aggregate to `(balance_total(d) − balance_total(t0)) / balance_total(t0)` (naive cumulative return). The deployed result showed the net-worth line stuck at exactly `+0.0%` across the entire 5-year window while QQQ climbed to +110%. Either `balance_total` was constant across that fetch (some history-restore behaviour the rebase didn't anticipate), or the `filterLowCoverageRows` / today-point-append logic broke the indexing — root cause not fully diagnosed before revert.

Two structural problems with the naive rebase even when it works:

1. It still includes contributions/withdrawals (a $1M deposit shows as a step up, not as zero growth) — so the rebased line is **not** comparable to SPY's buy-and-hold return.
2. It reads from the existing `networth-history` aggregate, which has cutoff/coverage logic geared toward "show me what I had" not "show me my investment return."

Both go away if the chart line comes from the same engine that computes the TWR headline number.

---

## 2. The proposed solution

Add a **daily TWR-indexed series** to the `/wealth/api/brokerage/performance/portfolio` response (or a sibling endpoint), and render that series as the chart's net-worth line when any benchmark overlay (SPY/QQQ) is toggled on. The line:

- Starts at `0%` at the window's first date.
- Climbs or falls based on market returns only — contributions and withdrawals are stripped.
- Lands at exactly the same TWR number shown in the KPI strip above the chart (modulo rounding).
- Shares the same `%` y-axis as SPY/QQQ — one coherent scale, no dual-axis.

When no overlays are visible, the chart falls back to the existing dollar net-worth line, unchanged.

---

## 3. API: new field on the existing portfolio endpoint

Extend `GET /wealth/api/brokerage/performance/portfolio` to return an additional `twrSeries` field alongside the existing `series` (which has principal/growth in dollars):

```jsonc
{
  "scope": "portfolio",
  "scopeId": null,
  "view": "outside_money",
  "series": [/* existing dollar series — unchanged */],
  "twrSeries": [
    {"date": "2025-05-20", "twrIndex": "1.000000", "twrPct": "0.000000"},
    {"date": "2025-05-21", "twrIndex": "1.002314", "twrPct": "0.002314"},
    // ...
    {"date": "2026-05-20", "twrIndex": "1.241742", "twrPct": "0.241742"}
  ],
  "summary": {/* unchanged */}
}
```

Field shapes:
- `twrIndex`: Decimal string, scale 6. The cumulative growth factor (1.0 at start). Useful when you want to chain or rebase.
- `twrPct`: Decimal string, scale 6. `twrIndex − 1`. What the chart actually plots.

Same field also appears on `/account/[id]` and `/holding/[symbol]` for consistency (so per-account / per-symbol drilldown can use the same line shape later).

**Why a new field, not a new endpoint:** the chart already needs to fetch the portfolio summary on mount for the KPI strip. Bundling `twrSeries` into the same response means one HTTP round-trip serves both, the front-end always has TWR numbers and the series in sync, and no second mount-effect to plumb.

**Why not extend `networth-history` instead:** that endpoint is the legacy parity surface and has its own history-restore / coverage-filter contracts. Layering TWR on top of it risks the same coupling problems the first cut hit. Keeping TWR in the new `/performance/*` namespace cleanly separates "investment return" from "what I had on my balance sheet."

---

## 4. Math: chained-daily Modified Dietz

For each scope (portfolio / account / holding), produce a daily TWR-indexed series over `[start, end]`:

```
For each day d in [start, end] ordered ascending:
    V(d)       = market value at end-of-day d (from snapshots × prices, carry-forward; same _mv_at() the engine already has)
    CF(d)      = sum of external_in/out tx amounts dated on d, in brokerage sign (positive = inflow)
    V_prev     = V(d − 1)                     // for d = start, see "start handling" below

    if V_prev > 0:
        // EOD cash-flow convention: subtracting CF(d) from the day's gain isolates the
        // market-driven return component. The deposit's effect is already in V(d) (the
        // deposit became cash, possibly converted to shares), so we strip it from the numerator.
        r_d = (V(d) − V_prev − CF(d)) / V_prev
    else:
        // No invested capital yet (pre-funding window): no return to record.
        r_d = 0

    twr_index(d)  = twr_index(d − 1) × (1 + r_d)       // twr_index(start) = 1
    twr_pct(d)    = twr_index(d) − 1
```

**Start handling.** On the very first day (`d == start`):
- `V_prev = V(start)` (i.e., the start-of-day value, before any same-day CF). In practice, treat `V_prev` as `V(start − 1)` — the previous day's EOD value. If `start` is also the first day the portfolio has any value, `V_prev = 0` → `r_start = 0` → `twr_index(start) = 1` → `twr_pct(start) = 0`. This is the intended "everyone starts at 0%" anchor.

**Rounding.** Use `D` (decimal.js HALF_EVEN, scale 6) for `twr_index` and `twr_pct`. Drop precision to scale 4 (4 decimal places) for SVG path construction — the chart pixel resolution can't distinguish below that.

**Why daily Modified Dietz is OK (not overkill).** The full GIPS Modified-Dietz formula weights each cash flow by `(period_end − cf_date) / total_period_days`. With a one-day period and EOD convention, every cash flow is at the period end → weight 0. The denominator collapses to `V_prev`, and the numerator is `V(d) − V_prev − CF(d)`. That's exactly what's above.

**Why this matches the KPI's headline number.** The KPI runs monthly Modified Dietz over the same window. Chaining 30 daily 1-day returns produces the same answer as one 30-day Modified-Dietz step **as long as cash flows are EOD** — which they are by convention. End-of-window `twr_pct` should equal `summary.twr` to ~5 decimals (small drift from float arithmetic in Brent's method elsewhere is acceptable; the comparison test below allows 0.001 tolerance).

---

## 5. Implementation: backend

Add to `sparkry-crm/src/lib/server/wealth/performance.ts`:

```ts
export interface TwrPoint {
    date: string;        // ISO YYYY-MM-DD
    twrIndex: string;    // Decimal scale 6, 1.0 at start
    twrPct: string;      // Decimal scale 6, twrIndex - 1
}

export function dailyTwrSeries(
    txs: PerfTransactionRow[],
    snaps: PerfPositionSnapshotRow[],
    prices: PerfHistoricalPriceRow[],
    scope: Scope,
    start: string,
    end: string,
): TwrPoint[] {
    // 1. Filter out REJECTED txs (engine convention).
    // 2. Build snap_idx + price_idx (reuse private helpers from this module).
    // 3. Pre-aggregate external CF per date for the scope, using
    //    buildBrokerageCashFlows(txs, scope, start, end) and grouping by date.
    // 4. Iterate dates from (start - 1 day) → end, maintaining V_prev and twr_index.
    //    Emit a TwrPoint for each date >= start.
}
```

The function is pure (no DB, no D1) so it gets straightforward vitest coverage in `performance.test.ts`.

Wire it into `performance-routes.ts::buildPerformanceResponse`:

```ts
const twrSeries = dailyTwrSeries(txs, snaps, prices, scope, start, end);
return { scope, scopeId, view, series, twrSeries, summary };
```

Add `twrSeries: TwrPoint[]` to the response type interface.

**Performance note.** For a 5-year window, `dailyTwrSeries` is O(N_days × N_positions) for the daily `_mv_at` call. ~1825 days × 100 positions = 182,500 bisect operations per request. On a Cloudflare Worker that's <50ms; fine. If it becomes a bottleneck later, materialise `DailyPositionValue` (see anchor design spec §4.2 deferred item).

---

## 6. Implementation: frontend

Two changes in `sparkry-crm/src/lib/components/wealth/NetWorthChart.svelte`:

**Change 1:** Accept a new optional prop `twrSeries: TwrPoint[] | null` and pass it down from `+page.svelte` (which already loads the portfolio response for `PerformanceKpis`).

```ts
interface Props {
    // ...existing...
    twrSeries?: TwrPoint[] | null;
}
```

**Change 2:** When `hasPctSeries` (a benchmark or overlay is visible) AND `twrSeries` is non-empty, render the aggregate line from `twrSeries.twrPct` against `yPct` instead of from `aggregatePoints.balance_total` against `yDollar`. Use `aggregatePoints` only for the x-axis date keying so the line aligns with the rest of the chart's date positions.

```ts
const useTwrLine = $derived(hasPctSeries && (twrSeries?.length ?? 0) > 0);

const twrByDate = $derived.by(() => {
    const m = new Map<string, number>();
    for (const p of twrSeries ?? []) m.set(p.date, Number(p.twrPct));
    return m;
});

const aggregatePath = $derived.by(() => {
    if (useTwrLine) {
        const points: Array<{ x: number; y: number }> = [];
        aggregatePoints.forEach((ap, i) => {
            const pct = twrByDate.get(ap.as_of);
            if (pct === undefined) return;        // skip dates with no TWR data (pre-window)
            points.push({ x: xAt(i), y: yPct(pct) });
        });
        return pathFromPoints(points);
    }
    // ...existing dollar-mode rendering unchanged...
});
```

Also gate the left-axis tick labels: when `useTwrLine`, render `%` ticks on the left (mirror of right-axis ticks). When not, dollar ticks as before.

**Update the hover tooltip** to show `twrPct` first when `useTwrLine`, with the dollar `balance_total` in parentheses for context.

**Suppress the area fill** when `useTwrLine` (the "fill down to zero net worth" semantic doesn't translate; spec §1 first-cut explainer).

---

## 7. Acceptance criteria

The build is done when **every** item is true:

### Backend (`performance.ts` + `performance-routes.ts`)

- [ ] `dailyTwrSeries` exists, returns one `TwrPoint` per day in `[start, end]` inclusive.
- [ ] `twrSeries[0].twrIndex === "1.000000"` and `twrSeries[0].twrPct === "0.000000"` always.
- [ ] When a known 1-year fixture (deposit $10,000 on day 1, no cash flow after, $10,823 on last day) is fed in, `twrSeries[365].twrPct ≈ "0.082300"` (within `0.001`).
- [ ] When a known mid-window cash flow fixture is used (deposit $10,000 day 1, $5,000 mid-year, end value matches a hand-computed Modified-Dietz target), `twrSeries[last].twrPct` matches the same value `summary.twr` produces, within `0.001`. This is the contract test that the chart line will agree with the headline KPI.
- [ ] REJECTED rows are excluded (uses the same `notRejected` filter as the rest of the engine).
- [ ] Empty position (sold all shares mid-window): `twrSeries` continues to be emitted for all remaining days; per-day return is `0` once `V_prev` is `0`. No crash, no `NaN`.
- [ ] `buildPerformanceResponse` returns `twrSeries` for portfolio, account, and holding scopes.

### Frontend (`NetWorthChart.svelte`)

- [ ] When no benchmark/overlay is visible, the chart looks **identical** to today (dollar net-worth line on left axis, no behavioural change). This is the safety guarantee.
- [ ] When SPY toggle is on, the aggregate line renders as the TWR-indexed % series on the same axis as the SPY line. The line starts at `0%` on the left edge and lands at exactly the value shown in the "TWR · 1Y" KPI above the chart at the right edge.
- [ ] When both SPY and QQQ toggles are on, all three lines (net-worth TWR, SPY, QQQ) share the same % axis. Each is independently toggleable via the existing BenchmarkToggleGroup.
- [ ] Left-axis tick labels switch from `$` to `%` when in TWR mode.
- [ ] Hover tooltip shows the TWR % at the hovered date, with the dollar `balance_total` in parentheses for context.
- [ ] No area fill under the line in TWR mode.
- [ ] Dates before the first `twrSeries` entry (e.g., pre-tracked-history) are skipped in the line, not coerced to `0%`. The line starts where TWR data starts.

### Tests

- [ ] At least 5 new vitest cases in `performance.test.ts`:
  1. Empty inputs → empty `twrSeries`.
  2. Single deposit, 1-year window, ending value with growth → `twrPct[last]` matches expected to 4 decimals.
  3. Mid-window deposit doesn't show as growth (`twrPct[last]` excludes the deposit value).
  4. Mid-window withdrawal doesn't show as decline.
  5. REJECTED tx excluded — same window with and without a flagged REJECTED row produce identical `twrSeries`.
- [ ] One Svelte-component or page test that asserts: when `twrSeries` is provided and a benchmark is visible, the chart's path includes points at `y = yPct(twrPct)` rather than `y = yDollar(balance_total)`. (Inspect the rendered `<path d="...">`.)

### Quality gates

- [ ] `pnpm exec tsc --noEmit` — clean.
- [ ] `pnpm exec svelte-check` — no new errors (pre-existing CRM warnings unchanged).
- [ ] `pnpm exec vitest run src/lib/server/wealth/ src/lib/components/wealth/` — all green.
- [ ] `pnpm build` — clean.
- [ ] After `wrangler pages deploy ... --branch main`: visit `https://internal.sparkry.ai/wealth` logged in, toggle SPY on, confirm the green line climbs to roughly the same %-value that the KPI strip shows for TWR.

---

## 8. Edge cases the builder must handle

| Case | Expected behaviour |
|---|---|
| `start == end` (single-day window) | `twrSeries` has one point at `0%`. |
| Window predates any snapshot data | `V_prev = 0` for all days → all `twrPct = 0`. Line is flat at `0%`, which is honest (the engine has no information). |
| Cash flow on the first day | EOD convention: `r_start = 0` because `V_prev = 0` (no prior holdings). The deposit shows up in `V(start+1)` and onward. |
| Cash flow on the last day | Counted normally: subtracted from the numerator so the day's return only reflects market. |
| `V_prev > 0` but `V(d) == 0` (sold everything mid-window) | `r_d = (0 − V_prev − CF(d)) / V_prev`. If the user sold to cash and withdrew (`CF(d) < 0` matching the value), `r_d ≈ 0` (no market move). If they sold but kept cash (no CF), `r_d = -1` → `twr_index` collapses. The day after, `V_prev = 0` → `r = 0` and the line stays flat. This is mathematically correct but visually jarring; document it in the API docstring. |
| Daily ratchet from rounding | Decimal scale 6 throughout; chain via Decimal `times`, not float. |
| SPY/QQQ benchmark series missing for a date | The benchmark line skips that date; the TWR line still renders. |

---

## 9. Open decisions for the implementer

These don't block the build — pick reasonable defaults, document the choice:

1. **Account-scope and holding-scope `twrSeries`** — same algorithm, just the scope argument changes. Confirm `dailyTwrSeries` works at all three scopes via vitest before declaring done.
2. **Account-filtered portfolio (`account_ids` query param)** — currently 501s. Implementing the filter for TWR requires summing per-account TWR-indexed series correctly, which is **not** the same as summing dollar series (you can't just add ratios). Easiest first cut: keep the 501. Properly: weight each account's per-day return by its `V_prev` share of the filtered total, then chain. Defer to a separate ticket.
3. **Range selector reactivity** — the chart's `RangeSelector` lets the user pick 1Y / 3Y / 5Y / All. Currently `PerformanceKpis` only loads once with the API default (last 365 days). To make the chart line reflect the selected range, both the KPI fetch and the chart-prop fetch need to pass the range as `start_date`/`end_date` query params. This means moving the fetch up to `+page.svelte` (above both KPIs and chart) and propagating the result down. Worth doing in this same change — otherwise the chart line and the KPIs read different time windows and the user sees a drift.
4. **The SPY-not-visible bug** the user noticed — when the chart was in rebase mode, only QQQ rendered in the legend. Could be unrelated to this change, but worth verifying that with the new TWR mode both SPY and QQQ render when their respective toggles are on. If not, that's a separate fix in BenchmarkToggleGroup / NetWorthChart's series enumeration.

---

## 10. Anchor references

- Anchor design spec: `docs/superpowers/specs/2026-05-11-performance-measurement-design.md`
- Requirements: `requirements/current.md` REQ-PERF-016 (Cloudflare port REQ-PERF-021)
- Engine source (TypeScript, where the new function lives): `sparkry-crm/src/lib/server/wealth/performance.ts`
- Engine source (Python, the original implementation to mirror): `accounting/src/analytics/performance.py::time_weighted_return_breakdown`
- First-cut commit (failed): `eb36fd4` on `feat/wealth-performance` branch in `sparkry-crm`. Revert: `c12b400`. Inspect the diff before implementing — the path-rendering scaffolding is sound; only the *data source* (rebased dollar aggregate → daily TWR series) needs to change.
- KPI consumer that already calls the portfolio endpoint: `sparkry-crm/src/lib/components/wealth/PerformanceKpis.svelte`.

---

## 11. Why this design (one paragraph)

The chart needs to answer "did my portfolio beat SPY?" — that requires the line on the chart to be the **same kind of number** as SPY's line. SPY's line is buy-and-hold cumulative return; the portfolio's equivalent is TWR (because we have cash flows, SPY doesn't). The current chart shows dollars on the left and percent on the right, which is incomparable. The first cut shrank that gap by rebasing dollars to "% from start," but "% from start including contributions" is still a different kind of number than SPY's % return. The right answer is to plot **TWR-indexed cumulative return** as the chart line, drawn from the same analytics engine that computes the headline TWR number above the chart, so the line shape and the headline are guaranteed to agree.
