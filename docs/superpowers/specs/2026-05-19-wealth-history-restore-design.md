# Wealth History Restore & Gap-Fill — Design Spec

**Date:** 2026-05-19
**Status:** Design — feeds the TDD implementation phase
**Requirements:** REQ-WD-009..011 (`requirements/current.md`)
**Implementation repo:** `sparkry-crm` (Cloudflare Pages app + D1)
**Builds on:** REQ-WD-001 (range parsing), REQ-WD-003 (two-tier merge), REQ-WC-013 (`historical_price` EOD), and the 2026-05-18 `unmatched-dedup.ts` double-count fix.

## 1. Root cause (verified read-only against prod D1 + code)

The `/wealth` "All" chart calls the **extended envelope** (`networth-history` with `range`). Window is entirely `parseRange`-driven: `targetDates = generateTargetDates(range.start, range.end)`; the aggregate loop only emits points inside that window.

`networth-history-extensions.ts:42` `const ALL_RANGE_START_YEARS = 50;` and `:114` `case 'all': default: start = addDays(today, -365 * ALL_RANGE_START_YEARS)`. So `range.start('all') = 2026-05-19 − 365·50 = 1976-05-31` — a **fixed 50-year lookback, not data-driven**.

Consequences (one constant, both reported symptoms):
- **"1976-05 point off the scale":** the first target date is `1976-05-31`; 1976→2017 has no contributing series (no matched `state`, unmatched forward-fill `lastVal` stays `null`) → `balance_total="0.00"`. The left-most plotted point is that $0 @ 1976-05.
- **"only back to ~2022":** ~41 years of `$0.00` then real data from `2017-08`. With `range=all` + >5 accounts the series is weekly-downsampled before 3y-ago; the long zero tail + a y-axis dominated by the ~$8.11M recent value visually crushes 2017–2021 so it reads as "starts ~2022". The 2017–2021 data **is** contributing (verified: per-legacy-name `tier2_cut`≈`2026-05-xx`, so `unmatchedActiveAt` returns `true` for 2017–2025).

Verified prod facts (read-only `wrangler d1 execute sparkry-crm-prod --remote`):
- `account_balance_snapshot` has 2017–2021 legacy rows (`account_id IS NULL`): 2017:22, 2018:123, 2019:84, 2020:71, 2021:71; ~15 distinct `raw_account_name`. **Data already in prod — no re-migration (REQ-WD-009 Non-Goal confirmed).**
- No row with date `< 2012` in any table → `1976-05` is purely the `parseRange` constant, not corrupt data (REQ-WD-010).
- Per-legacy-name `tier1_cut` is `null` for all; `tier2_cut` is `2026-05-xx` for all **except** `Templeton`=`2020-12-31` and `Bitcoin`/`Emerson Coverdale`=`null`.
- `position_snapshot.as_of` is stored as microsecond datetime `"2026-05-04 00:00:00.000000"` (the malformed-date hazard REQ-WD-010 normalizes).

## 2. REQ-WD-010 — data-driven "All" start (kills 1976-05 + zero tail)

`parseRange` is pure (no DB). Keep it pure; **override the start for `range==='all'` in the endpoint** after data load, mirroring the legacy parity path which already does `start = candidateDates.reduce(min)`.

### 2.1 `earliestRealSnapshotDate(state, unmatchedByRawName)` (new helper, `networth-history-extensions.ts`)
Returns the min **well-formed** `YYYY-MM-DD` across:
- every matched series date in `state` (the loaded history state used by `perAccountValueAt`),
- every `[d]` in `unmatchedByRawName` values.

Normalization (REQ-WD-010 / P2-A): `const k = raw.substring(0,10); if (!/^\d{4}-\d{2}-\d{2}$/.test(k)) { /* log once, skip */ continue; }`. The regex test wraps the **reduce itself**, not only per-series values. Empty input → `null`.

### 2.2 Endpoint wiring (`handleExtendedEnvelope`) — sequencing (P1-2 review-corrected)
`priceMinDate` gates the `loadHistoryState` call inside the existing `Promise.all`, and `state`/`unmatchedData` must resolve **before** `dataStart` is computable. Therefore `priceMinDate` CANNOT be re-derived from the clamped `startKey` without a two-phase reload (rejected — impractical). Explicit decision for `range==='all'`:
- `loadHistoryState` keeps the **pre-clamp** `priceMinDate` (derived from the unclamped `range.start`). This means the `historical_price` scan stays as wide as today's 50y behavior — **no performance regression vs. status quo, zero correctness impact** (extra rows are simply never referenced once the window is clamped).
- After `state`/`unmatchedData` resolve, and **only when `range.range === 'all'`**:
```
const rawStartKey = range.start.toISOString().substring(0,10);
const dataStart = earliestRealSnapshotDate(state, unmatchedByRawName); // date-only, well-formed or null
const startKey = (dataStart && dataStart > rawStartKey) ? dataStart : rawStartKey;
```
clamp the 50y floor *up* to the first real datum (ternary correct in all cases: `dataStart`>floor → clamp up; `dataStart` null → keep floor; `dataStart`<floor impossible here but → keep floor). `dataStart===null` (empty fixture) → `startKey=rawStartKey` (1976) producing an all-$0 series — acceptable; test R10-a/d fixtures MUST load data so this path isn't silently exercised.
- The clamped `startKey` MUST drive: `generateTargetDates(startKey, endKey, …)`, the response `start` field, AND the benchmark/overlay `loadPctChangeSeries` calls — pass `new Date(startKey + 'T00:00:00Z')` as the range-start argument, **not** `range.start` (the unclamped object; lines ~372/380 currently pass `range.start` and MUST be changed). `endKey`/`range.end` unchanged.
- The legacy parity path already excludes malformed dates implicitly via string-min but MUST adopt the same `^\d{4}-\d{2}-\d{2}$` guard for consistency (shared helper).

Other ranges (`1y`, `ytd`, …) are unchanged — they are intentionally fixed-width windows.

Result: "All" x-axis = `2017-08` → today; no `1976` point; no 41-year zero tail; 2017–2021 visible and correctly scaled.

## 3. REQ-WD-009 — dedup relaxation correctness (no double-count regression)

`unmatchedActiveAt` already implements "active strictly before effective cutoff, inactive on/after" (Tier-1 `firstMatched && firstMatched <= target → false`; Tier-2 `aliasCutoff && target >= aliasCutoff → false`; absent map entry ⇒ +∞ for that tier). Once §2 widens the window, pre-cutoff legacy history flows through **with no logic change to the cut decision**. Two real correctness fixes remain:

### 3.1 P1-B casing contract (correctness bug, can bleed → double-count)
Today: `matchedNameFirstDate` keys are `.toLowerCase()` (build line 52) and looked up `.toLowerCase()` (line 132) — consistent. But `aliasCutoffByRawName` is keyed by the **exact** `account_alias.raw_account_name` (build line 94) and looked up by **exact** `rawName` (`unmatchedActiveAt` line 136). If `account_alias.raw_account_name` casing ≠ `account_balance_snapshot.raw_account_name` casing, Tier-2 silently misses → legacy series bleeds on/after its real cutoff → **re-introduces the double-count REQ-WD-009 must not regress**.
**Fix (BOTH sides — review-corrected; lookup-only is NOT sufficient):** mirror the Tier-1 pattern exactly:
- **Build** (`buildUnmatchedSeries` line 94): `aliasCutoffByRawName.set(row.raw_account_name.toLowerCase(), row.first_matched.substring(0,10));`
- **Lookup** (`unmatchedActiveAt` line 136): `aliasCutoffByRawName.get(rawName.toLowerCase());`
Both are required — lowercasing only the `.get()` (as an earlier draft of this spec wrongly said) leaves the map keyed `"Amy IRA"` while the lookup asks `"amy ira"` → still a miss → still bleeds. Edge note: `unmatchedByRawName` is keyed on the exact stored snapshot name and its key flows to `unmatchedActiveAt` as `rawName`; if `account_balance_snapshot` ever holds intra-table mixed casing for one logical account, also lowercase the `unmatchedByRawName` build key (line 111) to avoid split series — prod currently has consistent per-name casing so this is a defensive note, not a required change. Test: alias `"amy ira"` vs snapshot `"Amy IRA"` must still cut at the alias date.

### 3.2 Templeton early-cutoff — VERIFIED clean (P1-3 resolved read-only)
`Templeton` `tier2_cut=2020-12-31`; its `account_alias` maps to FT account `4ea987d2-ft-8291`. Verified read-only against prod D1: that account has `account_balance_snapshot` matched rows spanning **2020-12-31 .. 2026-03-31 (7 rows)** (no `position_snapshot`, no `plaid_account_balance_snapshot`). So the carry-forward anchor **exists** — there is NO silent $0 gap: legacy `Templeton` contributes `< 2020-12-31`, matched FT contributes `≥ 2020-12-31` with REQ-WD-011 carry-forward step-holding between its 7 sparse annual points. Boundary is clean (no gap, no overlap): exactly-one-series invariant holds at 2020-12-31 (legacy inactive ≥cutoff, matched active from its first row = the cutoff date). This is the *intended* REQ-WD-009 behavior, not a bug — do NOT relax the cutoff (relaxing would double-count Templeton 2021+). L3 still adds test R9-e asserting the no-gap/no-overlap boundary against a fixture mirroring this shape (7 matched FT rows 2020-12-31..2026-03-31 + legacy Templeton 2017..2020).

### 3.3 Invariant test (REQ-WD-009 financial-correctness)
Miniflare fixture with: one legacy name + its aliased matched account spanning the cutoff. Assert (a) pre-cutoff dates: only legacy contributes; (b) cutoff date and after: only matched contributes; (c) **exactly one** series per economic account per target (never summed); (d) present-day aggregate equals a baseline captured from the pre-change code path on the same fixture (P2-C — compare-to-baseline, not a hardcoded $). Plus the carry-forward-no-bleed test: legacy last value must NOT carry into ≥cutoff dates (gated by `unmatchedActiveAt`).

## 4. REQ-WD-011 — gap-fill: carry-forward + reprice where shares known

### 4.1 Carry-forward (already correct, lock with a test)
The `for (const [d,val] of series) { if (d>target) break; lastVal=val }` loop already step-holds the last value (matched path via `perAccountValueAt` slots; unmatched via the loop). No change; add an explicit step-semantics test (sparse snapshots → flat plateaus, never interpolated).

### 4.2 Reprice refinement (additive, share-known only)
Priority ladder **per account+date** (REQ-WD-011): real snapshot for that exact date `>` reprice `>` carry-forward. Matched accounts with `position_snapshot` history already get positions×price via `perAccountValueAt` — unchanged. The **only new code** is legacy single-symbol names:
- **"shares known" (P2-B, structured only):** a legacy `raw_account_name` qualifies iff a declared 1:1 mapping to one ticker exists — reuse `account_alias` extended with an optional `symbol` column **if already present**; if no such structured column exists, introduce a tiny seed map `LEGACY_NAME_SYMBOL: Record<string,string>` (lowercased name → ticker) in `unmatched-dedup.ts`, seeded ONLY with operator-confirmed single-symbol names (candidates from data: `bitcoin`→(no equity ticker — EXCLUDE, no `historical_price`), `amazon stock`→`AMZN`). Names not in the map stay carry-forward. **No ticker inference from free text.**
- For a qualifying legacy name at `target` with known share count `q` (from the legacy row's balance ÷ … — NO: shares are NOT in `account_balance_snapshot`, only aggregate `balance`). **Therefore reprice for legacy names is only possible where a share quantity source exists** (`position_snapshot`/`CostBasisLot` for that economic account). If the only datum is an aggregate `balance`, there are no shares → **carry-forward** (this is the realistic outcome for almost all legacy names; reprice mainly benefits matched accounts which already have it). The spec keeps the reprice hook for legacy names that DO map to a lot-bearing account post-alias, valued `Σ shares × historical_price.close` (forward-fill non-trading days; Decimal.js ROUND_HALF_UP scale 2; REQ-WC-004). Missing shares or price → carry-forward, never $0, never fabricate.
- **Today-point (P1-D):** legacy single-symbol reprice for `target===todayKey` uses `historical_price.close` (latest EOD, forward-filled), NOT `live_quote`; the canonical present-day total constraint applies to the matched-account aggregate only (legacy names have no matched account / live-quote path).

### 4.3 No double-count
Reprice/carry-forward of a legacy series is still wrapped by `unmatchedActiveAt(rawName,target,…)` → contributes `$0` on/after the effective cutoff regardless. The priority ladder operates strictly *within* the REQ-WD-009 legacy-vs-matched partition.

## 5. Test matrix (L3 must implement, behavioral, Miniflare D1)

- **R10-a** `parseRange('all')` start clamps to earliest real date (fixture earliest 2017-08 → startKey 2017-08, not 1976).
- **R10-b** malformed `as_of` (`"2026-05-04 00:00:00.000000"`, `""`, `"1976-05"`-style) excluded from `earliestRealSnapshotDate`; start = earliest *well-formed* real date; no 1976 point in output.
- **R10-c** non-`all` ranges unchanged (1y/ytd fixed width).
- **R9-a** pre-cutoff legacy contributes; on/after cutoff only matched; exactly-one-series invariant.
- **R9-b** carry-forward no-bleed past cutoff.
- **R9-c** casing: mixed-case alias vs snapshot still cuts (P1-B).
- **R9-d** present-day aggregate == pre-change baseline on same fixture (no double-count; P2-C/P2-4): baseline = the output of the *pre-change* aggregate code path run on the **identical Miniflare D1 fixture**; capture that fixture-derived value and assert equality after the change (NOT a prod golden, NOT a hardcoded prod $). R11-c needs a Miniflare `historical_price` AMZN fixture.
- **R9-e** Templeton boundary: no gap/overlap at 2020-12-31 (or documents a flagged coverage gap).
- **R11-a** sparse snapshots → step plateaus, never interpolated.
- **R11-b** legacy name with no share source → carry-forward (not $0, not reprice).
- **R11-c** reprice path (where shares exist) = Σ shares×EOD close, forward-filled, Decimal scale 2; today uses EOD not live_quote for legacy.

## 6. Non-goals / no schema change
No migration (prod D1 already has the rows; `LEGACY_NAME_SYMBOL` is a code seed, not a table). No Plaid historical backfill (infeasible — point-in-time API). No interpolation. No change to `≥cutoff` matched behavior or the 2026-05-18 fix's post-cutoff semantics. `position_snapshot` rows are normalized at read, never mutated.
