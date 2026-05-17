# Wealth Dashboard Polish — Design Spec

**Date:** 2026-05-13
**Version:** Design v6 — final design freeze (v5 → v6)
**Status:** Design frozen — ready for plan phase
**Requirements:** REQ-WD-001..008 (`requirements/current.md`)
**Implementation repo:** `sparkry-crm` (Cloudflare Pages + Workers + D1)
**Builds on:** REQ-WC-001..019 (wealth-Cloudflare migration), REQ-WC-013a (live-quote refresh)
**Out of scope:** mobile polish (see §4.1 for mobile floor), new Plaid Items (Schwab separate), Fidelity automation.

> **Authority precedence (read first):** The body sections §1–§10 and the §7 test matrix are AUTHORITATIVE. The v1→v6 changelogs below are a historical record of *why* decisions changed; where any changelog entry's section/step number, constant, or value conflicts with the body (e.g. lock-key length, downsample trigger, `§3.3 step` numbering, Phase-A step letters/numbers), **the body wins**. Do not implement from the changelog trail. (Resolves L2-CON-001/003/006/010/013 staleness class.)

---

## Changelog: v1 → v2

| Finding IDs | Change |
|---|---|
| WD-R1-001, WD-R2-009 | Benchmark symbols changed from `^GSPC/^IXIC/^DJI` to `{SPY, QQQ, VTI}` (existing REQ-WC-013 allowlist ETF proxies) throughout §3.1, §4.1, §8 |
| WD-R1-002, WD-R1-003, WD-R2-005 | `historical_price.as_of` → `historical_price.trade_date` everywhere; `repriced-today` writes to `live_quote` (not `historical_price`) |
| WD-R0-003, WD-R2-007 | Budget corrected to 600/day (not 5000); §3.3 step 4 adds `getDailyTwelveDataCount` pre-check; §10 quota math rewritten |
| WD-R2-006, WD-R3-016 | Twelve Data batch-of-8 claim removed; single-symbol fetch reality documented; §3.3 redesigned to refresh ≤N most-stale symbols per invocation (client polls) |
| WD-R2-002 | WEALTH_KV binding gap in Pages `wrangler.toml` documented; action item added in §6.5 |
| WD-R2-003, WD-R3-014 | KV polling loop replaced with 202 in-progress pattern; client polls a lightweight status endpoint |
| WD-R2-004 | `audit_events.entity_id` NOT NULL convention defined (per-run UUID, `entity_type='repriced_today_run'`) |
| WD-R2-001, WD-R0-012 | Auth contradiction resolved: `repriced-today` is cookie-only; REQ-WD-008 WEALTH_INTERNAL_KEY clause corrected (see requirements note in §6.2) |
| WD-R2-008, WD-R3-006 | Rate-limit key changed from `session-id` (does not exist) to `repriced:lock:<sha256(email)[0..16]>` |
| WD-R0-007, WD-R2-012, WD-R3-005 | Chart library resolved: hand-rolled SVG confirmed; secondary y-axis geometry specified in §4.1; §8.1 closed |
| WD-R0-004 | Link audit now explicitly asserts `/wealth/realized` (not old `?view=realized-gl`) |
| WD-R1-004 | Decimal aggregation path: fetch rows, aggregate via Decimal.js in TypeScript (not raw SQL SUM on TEXT) |
| WD-R1-005 | `term_breakdown.unknown` bucket added for NULL-term lots |
| WD-R1-006 | `wash_sale_count` + `total_disallowed_loss` added to realized-gains response |
| WD-R1-007 | `prior_close = 0` guard added (→ null) |
| WD-R1-008 | Benchmark pct_change formula + anchor-selection rule specified |
| WD-R1-009 | Cash-only account augmentation clarified |
| WD-R4-001 | `aria-live='polite'`/`role='status'` on RefreshingBanner; `role='alert'` on error state |
| WD-R4-002 | SortableTable `keydown` (Enter+Space) handler + dynamic `aria-label` specified |
| WD-R4-003 | Day Δ% shape affordance (▲/▼) added alongside color |
| WD-R4-004 | Coverage warning surfaced on home-page realized cards, not only on detail page |
| WD-R3-001, WD-R3-002 | e2e tests replaced with vitest+Miniflare; TDD ordering rewritten in §9 |
| WD-R3-003 | Per-symbol D1 error isolation specified in §3.3 |
| WD-R3-007 | `luxon` reference removed; `isMarketOpen` reuse from existing quotes route specified |
| WD-R3-012 | Store moved to `src/lib/wealth-chart.svelte.ts` (flat `src/lib/` pattern) |
| WD-R3-013 | `range` default changed to `'all'` for backward compat; breaking-change note added |
| WD-R0-019 | Auth §1 description corrected: Google OAuth + HMAC cookie, no CF Access |
| WD-R0-005 | Legend-as-visibility-toggle design decision added in §4.1 |
| WD-R0-006 | CPU benchmark test requirement added for 16-account case |
| WD-R0-008, WD-R1-016 | Forward-fill decision closed (§8.5 → closed, now §3.1 note) |
| WD-R0-009 | Link audit scope extended to include programmatic navigations |
| WD-R0-010 | Coverage warning endpoint + UI specified in §3.2 and §4.2 |
| WD-R0-011 | Zero-symbols short-circuit specified in §3.3 |
| WD-R0-016 | `IngestionLog` row specified in §3.3 step 8 |
| WD-R0-017 | Error fallback for today-point specified in §4.4 |
| WD-R0-018 | SSR hydration strategy for sessionStorage specified in §4.1 |
| WD-R1-010 | Canonical dedup function reference added in §2.1 |
| WD-R1-011, WD-R0-011 | Decimal-aware sort comparator + per-route column allowlists specified in §4.5 and §3.4 |
| WD-R1-012 | Market Value Decimal.js multiplication path specified in §3.5 |
| WD-R1-013 | pct_change `toFixed(4)` serialization rule specified in §3.1 |
| WD-R1-014 | Twelve Data `/quote` field mapping specified in §3.3 |
| WD-R1-015 | Zero-cost-basis coverage warning added in §3.2 |
| WD-R2-010 | D1 query budget analysis added in §6.3 |
| WD-R2-011 | Per-route sort column allowlists specified in §3.4 and §6.5 |
| WD-R2-013 | Budget pre-check pattern documented in §3.3 step 4 |
| WD-R2-014 | RefreshingBanner event coupling mechanism (callback prop) specified in §4.4 |
| WD-R2-015 | HTTP 200 for quota exhaustion justified in §3.3 (matches REQ-WC-013a precedent) |
| WD-R2-016 | Payload cap decision: weekly aggregation when projected > 2 MB (§6.3) |
| WD-R3-008 | Named constants file `src/lib/server/wealth/wd-constants.ts` specified in §6.7 |
| WD-R3-009 | Test matrix test cases enumerated in §7 |
| WD-R3-010 | Coverage split: ≥90% pure TS, ≥60% Svelte files; chart math extracted to pure module |
| WD-R3-011 | Store test file `tests/unit/wealth-chart-store.test.ts` added to §7 |
| WD-R3-015 | `src/lib/components/wealth/` new directory creation noted |
| WD-R3-017 | Structured log format specified in §6.6 |
| WD-R3-018 | `net` computation path (Decimal.js application-layer) specified in §3.2 |
| WD-R3-019 | RefreshingBanner staleness check: POSTs to server (authoritative), no client-side threshold |
| WD-R0-013 | `ytd` field in realized-gains response noted as additive convenience field |
| WD-R0-014 | Section list of div→table conversions added to §5 |
| WD-R0-015 | Link audit moved to integration test (Miniflare), not pure unit test |
| WD-R4-005 | OKLCH palette color-blindness verification step added to §4.1 |
| WD-R4-006 | Chart skeleton loading state specified in §4.4 |
| WD-R4-007 | Range selector visual grouping specified in §4.1 |
| WD-R4-008 | Max-visible series UI limit (8) specified in §4.1 |
| WD-R4-009 | Tooltip timezone: local time + "last updated N min ago" resolved in §4.1 |
| WD-R4-010 | Negative realized G&L: explicit minus sign added alongside parenthetical |
| WD-R4-011 | Custom date picker: native `<input type='date'>` specified in §4.1 |
| WD-R4-012 | SymbolOverlayPicker full combobox interaction specified in §4.1 |
| WD-R4-013 | Mobile floor defined (≤767px collapse behavior) |
| WD-R4-014, WD-R0-020 | Wash-sale flag UX decided: amber 'W' badge + tooltip in §3.4 and §4.5 |
| WD-R4-015 | Today's chart-point labeled with today's date, tooltip shows "Prices as of HH:MM local time" |

## Changelog: v5 → v6 (final design freeze)

| Finding IDs | Change |
|---|---|
| P1-R1RD5001 | §2.1 Phase D credit/loan discriminator corrected: use `plaid_account_balance_snapshot.plaid_account_type IN PLAID_LIABILITY_TYPES` (canonical Set from `plaid-routes.ts:737`), NOT `account.account_type`. §3.1 today-point sign note updated to match. §7 WD-003 test assertion added for depository vs. credit vs. mortgage sign behavior. |
| P1-R1RD5002 | §3.1 response field name: production `networth-history/+server.ts` emits `balance_total` (confirmed at line 222). v5 spec used `net_worth` in the aggregate shape — this is a breaking rename. Decision (a) chosen: preserve `balance_total` as the field name; v6 spec updated. Decision documented in §3.1 with field-name stability note. Callers that already read `balance_total` (Top Holdings page, `/wealth/+page.svelte`) require no change. |
| P1-R1RD5003 | §3.2 `coverage_warnings` type extended: trigger 3 (mixed NULL/non-NULL `lt_gain_loss`) is year-wide, not account-specific — it must not reference `account_id`/`broker`. TypeScript union type added to §3.2; third trigger shape changed to `{scope: 'year', message: '...'}`. §7 WD-004 test assertion updated. |
| P1-R1RD5004 | §2.1 Phase D obligation extended: Plaid balance scale is 4 decimals (`plaid_account_balance_snapshot.current_balance` Numeric 18,4) while `account_balance_snapshot.balance` is scale 2. Merge MUST quantize Plaid balances to scale 2 via `new Decimal(plaidBalance).toDecimalPlaces(2, ROUND_HALF_UP)` before inserting into the series. §7 WD-003 test assertion added. |
| P1-R4RD5004 | §4.4 `<RefreshingBanner>`: promoted always-present/empty-content pattern as the canonical implementation; `visibility:hidden` alternative demoted to footnote. NVDA+Chrome reliability concern with visibility transitions documented. §7 WD-008 test assertions updated to match always-present DOM shape. |
| §9 STATE OF PLAY | Added "STATE OF PLAY AT DESIGN FREEZE" pre-Phase-A checklist enumerating files that do not yet exist, ESLint rules not yet added, constants still duplicated, dead link in +page.svelte:1196, and parseFloat violations — making it explicit which items are by-design execute-phase work vs. design defects. |

## Changelog: v4 → v5

| Finding IDs | Change |
|---|---|
| P1-R0RD4001, P1-R3RD4001 | §2.1 + §3.1: `loadHistoryState` does NOT yet query `plaid_account_balance_snapshot` — false claim corrected; Phase D step added to extend the function with the two-tier merge; column mapping and Plaid-wins dedup rule specified; §7 WD-003 test case added |
| P1-R0RD4002, P1-R1RD4002 | `requirements/current.md` REQ-WD-008: "8 symbols" → `REPRICED_TODAY_BATCH_SIZE (3)` |
| P1-R0RD4003, P1-R4RD4001 | `requirements/current.md` REQ-WD-007: `role="button"` removed; correct pattern documented (tabindex='0' on implicit columnheader) |
| P1-R0RD4004, P1-R1RD4001 (IngestionLog), P1-R2RD4002 (idempotency), P2-R1RD4002, P2-R2RD4004 | §3.3: explicit early-exit after step 3 when `staleSymbols` is empty; IngestionLog write moved to AFTER budget check (step 4b); §7 WD-008 idempotency test assertion updated to remove ambiguous OR |
| P1-R0RD4005, P2-R0RD4009 | §9 Phase C: extract new top-holdings pricing logic to `top-holdings-pricing.ts` (pure TS) and add to coverage include. §9 Phase D: extract networth-history extension logic to `networth-history-extensions.ts` (pure TS) and add to coverage include |
| P1-R1RD4003 | §3.2: second D1 query added (`SELECT DISTINCT account_id FROM position_snapshot`) for first coverage_warnings trigger; account JOIN to populate broker field documented |
| P1-R1RD4004 | §3.2: mixed NULL/non-NULL `lt_gain_loss` third coverage_warnings trigger added to normative prose |
| P1-R2RD4001, P1-R3RD4006 | §3.3 step 6 + §6.3: exponential backoff reduced to 2 attempts at 200ms/600ms; wall-clock worst-case recalculated; hard 25s wall-clock cap added; §6.3 estimate updated |
| P1-R2RD4003 | §9 Phase A: TDD exemption for extraction steps (b)–(d) explicitly scoped as pure-refactor; clarifying note added |
| P1-R2RD4004 | `package.json` (sparkry-crm): `"lint": "eslint src/"` added to scripts |
| P1-R3RD4005 | §9 Phase C: `parseFloat` violation in top-holdings route flagged; fix specified (replace with Decimal.js) |
| P1-R3RD4007 | §3.1: `accounts` param documented as comma-separated UUID strings (account_id values); disambiguation from existing `account_ids` param added |
| P1-R4RD4002 | §4.4: `role='alert'` element MUST NOT use `display:none`; spec updated to use `visibility:hidden` (or always-present empty-content pattern) |
| P1-R4RD4003 | §4.1: AccountMultiSelect Clear button spec added (aria-label, keyboard trigger, post-activation focus); §7 WD-002 test cases added |
| P1-R4RD4004 | §4.1: AccountMultiSelect desktop ARIA pattern specified (role='listbox', aria-multiselectable='true', role='option', aria-selected, keyboard nav) |
| P1-R4RD4005 | §4.4: chart skeleton retry `<button>` spec added (aria-label, disabled-state during retry); banner error retry spec added |
| P2-R0RD4006 | §7: `symbol-helpers.test.ts` test case row added to test matrix |
| P2-R0RD4007 | `requirements/current.md` REQ-WD-004: full response shape added (term_breakdown.unknown, wash_sale_count, total_disallowed_loss, ytd, coverage_warnings[]) |
| P2-R0RD4008 | `requirements/current.md` REQ-WD-005: cross-reference corrected from "REQ-WD-007" to "design spec §3.4" |
| P2-R0RD4010 | §3.3: `Idempotency` labeled subsection added with deterministic early-exit definition |
| P2-R1RD4001 | Changelog R1RD2004 entry corrected (CAST removed; Decimal.js isZero() is canonical) |
| P2-R1RD4002 | Covered by P1-R0RD4004 early-exit fix |
| P2-R2RD4001 | §3.3 step 8: KV lock delete standardized to `kvAvailable` guard (not optional chaining); consistent with step 4 |
| P2-R2RD4003 | §9 Phase A: ESLint `no-restricted-syntax` rule for local `DAILY_BUDGET` constant outside `wd-constants.ts` added as enforcement mechanism |
| P2-R2RD4004 | Covered by P1-R0RD4004 early-exit fix |
| P2-R4RD4006 | §4.1 RangeSelector + BenchmarkToggleGroup: roving tabindex keyboard pattern specified (Arrow Left/Right within group; Tab exits group) |
| P2-R4RD4008 | §4.1: WCAG 1.4.3 contrast ratio requirement added (4.5:1 normal text, 3:1 large/non-text); Phase E verification step added |
| P2-R4RD4009 | §4.2 + §4.5: tooltip ARIA pattern specified (role='tooltip', aria-describedby on trigger) |
| P2-R4RD4010 | §3.4 + §4.5: empty /wealth/realized state: `<tbody>` with single `<tr><td colspan=9>` "No realized transactions in {year}" |
| P2-R4RD4011 | §4.1 NetWorthChart: `<svg role='img' aria-label='Net worth chart: {range}'>` + sr-only summary table specified |
| P2-R4RD4012 | §4.4: banner polling max-retry cap added (6 × 5s = 30s); MAX_RETRIES added to `wealth-constants.ts`; error state message specified |
| P3-R0RD4011 | `requirements/current.md` REQ-WD-001/-003: sessionStorage keys `wd:nw:benchmarks` and `wd:nw:overlays` added |
| P3-R2RD4001 | §6.7: `$lib/server/` SvelteKit enforcement note added |
| P3-R2RD4002 | §10: WEALTH_KV binding gap risk entry updated (RESOLVED — wrangler.toml already patched) |
| P3-R3RD4001 | §3.3 step 5: comment added clarifying `run_at` = start time (preserved intentionally by `updateIngestionLog`) |
| P3-R3RD4002 | §9 Phase A step labels renumbered for clarity (Setup → TDD order) |
| P3-R3RD4003 | §9 Phase A: note added that coverage include for non-existent files is silently skipped; stub-creation step added |
| P3-R4RD4013 | §4.2: RealizedGLCard click target specified as `<a href='/wealth/realized?year={year}'>` wrapper |
| P3-R4RD4014 | §4.1: inline SVG chart labels specified as non-interactive (aria-hidden='true'); all deselection via AccountMultiSelect |
| P3-R4RD4015 | §3.1: `downsampled_before` display changed from "may use" to "MUST display"; format and test case specified |
| P1-R3RD4002, P1-R3RD4003, P1-R3RD4004 | Execute-phase obligations (Phase A): test files, module extractions, constant migration — correctly deferred to TDD implementation phase; §9 notes updated |

## Changelog: v3 → v4

| Finding IDs | Change |
|---|---|
| P0-R1RD3001, P0-R3RD3001, P1-R2RD3001 | `getDailyTwelveDataCount` corrected from `COUNT(*)` to `COALESCE(SUM(records_processed), 0)` in `db-helpers.ts` and confirmed in §3.3 step 8, §8.9 |
| P0-R3RD3002, P1-R1RD3001, P1-R2RD3002 | `writeIngestionLog` status union extended to include `'in_progress'`; new `updateIngestionLog` helper added to `db-helpers.ts`; §3.3 step 5 and step 8 updated for consistency |
| P0-R3RD3003, P1-R1RD3002, P1-R2RD3003 | `REPRICED_TODAY_BATCH_SIZE` canonicalized to `3` everywhere; stale `=4` prose and contradictory stream-of-consciousness paragraph removed from §3.3 step 4 and step 6; changelog entry corrected |
| P0-R0RD3002 (effectively), P1-R0RD3002, P1-R2RD3004, P1-R3RD3010 | HTTP 202 response body standardized to `{status: 'in_progress'}` (distinct shape from all 200 responses); propagated to §3.3 step 4a code snippet, failure-modes, §6.4, §4.4 banner state machine, §7 WD-008 test matrix |
| P1-R0RD3003, P1-R4RD3001, P2-R4RD3010 | Three-state sort cycle (none→asc→desc→none) added to §4.5 per REQ-WD-007; none-state aria-label wording defined; test matrix updated |
| P1-R0RD3004 | `/wealth/realized` page clarified as SSR-only (`+page.server.ts` load function); `GET /wealth/api/brokerage/realized-detail` removed from section title; SSR load contract documented |
| P2-R0RD3005 | `requirements/current.md` REQ-WD-006: `as_of` → `trade_date` |
| P2-R0RD3006, P1-R3RD3007 | Coverage `include` in `vite.config.ts` scoped to new REQ-WD-001..008 modules only; existing routes excluded until their test files exist; `branches: 80` added to thresholds; ≥60% Svelte target documented as manual-review only |
| P2-R0RD3007 | Idempotency definition added to §3.3 and test case added to §7 WD-008 |
| P3-R0RD3009, P2-R2RD3006 | §3.3 step 2: explicit `staleSymbols` filter snippet added; staleness sort order (oldest first) specified before slice |
| P1-R1RD3003 | §3.2 SELECT clause extended with `account_id` and `unadjusted_cost_basis` columns |
| P3-R0RD3010 | §7 WD-004: mixed NULL/non-NULL `lt_gain_loss`/`st_gain_loss` test case added |
| P1-R2RD3005, P2-R2RD3009 | `wrangler.toml`: `preview_id` corrected to staging KV ID `592d46c8...`; `[[env.preview.kv_namespaces]]` added for WEALTH_KV |
| P3-R2RD3010, P3-R2RD3011 | `wrangler.toml` secrets comment extended with all REQ-WC-019 wealth-specific secrets |
| P2-R2RD3007 | §3.3 step 2 updated to use `db.batch()` for staleness reads |
| P2-R2RD3008 | §3.1 `overlay_symbols` regex note: `^` allowed for parity but non-held symbols return empty series without 400 |
| P2-R2RD3009 | §3.1: accounts with zero balance rows return `series: []` (not omitted) |
| P3-R2RD3012 | §6.4: budget exhaustion = graceful exit → DELETE KV lock; clarified alongside error case |
| P3-R2RD3011 | §3.3 step 1: KV cache write-on-failure behavior documented (do NOT write on error) |
| P2-R1RD3002 | Credit/loan sign negation note moved from §3.3 step 3 to §3.1 (networth-history today-point) |
| P1-R3RD3004 | `@vitest/coverage-v8` added to `package.json` devDependencies |
| P1-R3RD3005 | §2.1: `loadHistoryState` location corrected to `db-helpers.ts` (not `networth-history.ts`) |
| P1-R3RD3006 | §9 Phase A: ordering tightened — refactor extractions happen BEFORE writing tests for their consumers |
| P1-R3RD3008 | `CHART_LOADING_TIMEOUT_MS` moved from `wd-constants.ts` (server-only) to `wealth-constants.ts` (shared) |
| P1-R3RD3009 | §9 Setup: `tests/integration/` directory creation added as explicit step |
| P2-R3RD3012 | §6.7: `REQUEST_INTERVAL_MS` derivation comment added |
| P2-R3RD3013 | §9 Phase A: `isCashOrSuspect` also extracted alongside `getSymbolsToFetch` |
| P2-R3RD3014 | `vite.config.ts`: `branches: 80` threshold added |
| P2-R3RD3015 | §3.3 step 6: fragile line-number citations replaced with grep-able function anchors |
| P2-R3RD3016 | §6.3 / §2.1: clarified that `loadHistoryState` does a full table scan (no per-account D1 queries); D1 budget analysis updated to match |
| P3-R3RD3018 | §9 Phase A step (d): `REQUEST_INTERVAL_MS` added to constants migration list |
| P1-R4RD3002 | §4.2: coverage warning `⚠` span: `tabindex="0"` added for keyboard focusability |
| P1-R4RD3003 | §4.1 mobile drawer: `aria-modal="true"` added |
| P1-R4RD3004 | §4.1 CustomDateRange: `<label>` requirement for start/end inputs specified |
| P1-R4RD3005 | §4.3: zero Δ% case (dayDeltaPct="0.0000") specified — no glyph, neutral color, no sr-only direction text |
| P2-R4RD3007 | §7 WD-006: negative Δ% test assertions added (▼ + sr-only "decreased") |
| P2-R4RD3008 | §7: `prefers-reduced-motion` shimmer test case added |
| P2-R4RD3009 | §7 WD-003: tooltip "no Live" assertion moved from prose to test matrix |
| P2-R4RD3011 | §4.1 SymbolOverlayPicker: `aria-selected="false"` (not omitted) on unselected options required |
| P3-R4RD3012 | §4.4: initial idle state sr-only text specified (empty — no AT announcement on page load) |
| P3-R4RD3013 | §4.1: BenchmarkToggleGroup ARIA specification added |

## Changelog: v2 → v3

| Finding IDs | Change |
|---|---|
| R0RD2001, R2RD2001 | getDailyTwelveDataCount changed to SUM(records_processed) — §3.3 step 4, §3.3 step 8, §6.7, §10 updated |
| R2RD2002, R2RD2017 | WEALTH_KV: quotes route does NOT use WEALTH_KV (corrected false claim); `[[kv_namespaces]]` added to wrangler.toml; null-guard for platform.env.WEALTH_KV added to §3.3 step 7 and §6.5 |
| R2RD2003, R1RD2003 | 202 polling deadlock fixed: KV lock deleted on successful completion (not relying on TTL); lock acquisition moved before budget pre-check; §3.3 step 7, §6.4 updated |
| R2RD2004 | overlay_symbols validated against `/^[A-Z0-9.^]{1,12}$/` regex — §3.1, §6.5 |
| R2RD2005 | accounts param values validated as UUID format — §3.1 |
| R2RD2006 | `?dir=` validated as `'asc'\|'desc'` enum; fallback to 'asc' — §3.4 |
| R1RD2002 | Twelve Data field mapping corrected to `close ?? price` — §3.3 step 5 |
| R1RD2008, R2RD2015, R3RD2010 | Parallel fetch rate-limit risk acknowledged; reduced to REPRICED_TODAY_BATCH_SIZE=3 with 7500ms sequential gaps (matching EOD cron); §3.3 step 5, §6.3 updated |
| R3RD2002 | wd-constants.ts migration: existing DAILY_BUDGET and CACHE_TTL_MS local copies in three files REMOVED and replaced with import from wd-constants; §9 Phase A |
| R3RD2001 | getSymbolsToFetch extracted to `src/lib/server/wealth/symbol-helpers.ts`; twelve-data-ingest.ts imports from there; §3.3 step 1 |
| R3RD2003 | isMarketOpen extracted to `src/lib/server/wealth/market-hours.ts`; quotes/+server.ts imports from there; tests/unit/market-hours.test.ts added to §7 |
| R2RD2008, R3RD2004 | db.batch() scope clarified: permitted for read-only routes, prohibited for Plaid sync writes; §6.3 |
| R1RD2001, R3RD2005 | realized-gains term breakdown decision: use DB-stored lt_gain_loss/st_gain_loss columns when non-NULL, fall back to proceeds-cost_basis only when NULL; §3.2 |
| R3RD2012 | /realized-gl migration decision: keep with @deprecated comment in Phase B; flag parseFloat violation for fix in same phase; §3.2, §9 |
| R0RD2003, R1RD2014 | BND contradiction resolved: BND remains in EOD allowlist (REQ-WC-010/REQ-WC-013) but is NOT a UI benchmark toggle option; benchmarks API validates against {SPY,QQQ,VTI} only; BND → 400; §3.1 |
| R0RD2002, R2RD2014 | quota_exhausted response shape normalized: all 200 responses include {refreshed,skipped,errors,latest_as_of,stale_symbols,error_code?}; §3.3 step 4, failure-modes |
| R0RD2004 | range-helpers.test.ts added to §7 with all 11 slugs + edge cases |
| R0RD2005 | Link audit negative assertion (old URL absent) already in §5 — test matrix strengthened |
| R0RD2006, R3RD2015 | Weekly downsample test cases added; downsampled_before field added to response schema §3.1; aggregation method specified (last trading day of each week = Friday close) |
| R3RD2006 | Coverage enforcement: vitest.config.ts coverage block with v8 provider and thresholds; @vitest/coverage-v8 in devDependencies; §9 Phase A |
| R3RD2007 | async crypto.subtle snippet for KV lock key — §3.3 step 7 |
| R4RD2001 | Space keydown MUST call event.preventDefault() — §4.5 |
| R4RD2002 | role='button' on th removed; use implicit columnheader role + aria-sort — §4.5 |
| R4RD2003 | Color-blindness verification extended to tritanopia — §4.1 |
| R4RD2004 | ▲/▼ triangles: aria-hidden='true' on glyph + sr-only text alternative — §4.3, §4.5 |
| R4RD2005 | Wash-sale badge: role='img' aria-label='Wash sale' + focus-accessible tooltip — §3.4, §4.5 |
| R4RD2006 | Coverage warning icon: aria-label on element — §4.2 |
| R4RD2007 | RangeSelector aria-pressed='true' on selected button — §4.1 |
| R4RD2008 | CustomDateRange max= attribute on date inputs — §4.1 |
| R4RD2009 | Chip removal focus management specified — §4.1 |
| R4RD2010 | Mobile drawer: role='dialog' + focus trap + Escape — §4.1 |
| R4RD2011 | Realized G&L negative affordance: ▼ is mandatory DOM element, not 'or color' — §4.2 |
| R4RD2012 | isMarketOpen DST note: must use Intl.DateTimeFormat with America/New_York — §3.3 step 3 |
| R4RD2013 | Max-series message in aria-live='polite' region — §4.1 |
| R4RD2014 | RefreshingBanner: two separate DOM elements (role='status' + role='alert'), not dynamic role swap — §4.4 |
| R4RD2015 | SymbolOverlayPicker: Tab closes listbox; Shift+Tab closes and goes to previous — §4.1 |
| R3RD2017 | CPU benchmark test: timing assertion replaced with correctness; note added that 250ms enforced by staging soak — §7 |
| R3RD2018 | CLIENT_RETRY_MS moved to `src/lib/wealth-constants.ts` (shared, not server-only) — §6.7 |
| R2RD2017 | False WEALTH_KV-in-quotes claim corrected in §6.5 |
| R2RD2018 | Year-boundary date format documented: closed_date stored as YYYY-MM-DD; boundary test added — §3.2 |
| R1RD2004 | cost_basis zero comparison corrected to `Decimal.js isZero()` in TypeScript aggregation loop (NOT SQL CAST) — §3.2. Do NOT use `CAST(cost_basis AS REAL) = 0.0` — §3.2 normative text is authoritative. |
| R1RD2005 | pct_change anchor zero-guard: if close[anchor] is zero, return empty series — §3.1 |
| R1RD2006 | NULL quantity guard in top-holdings endpoint — §3.5 |
| R1RD2012 | Credit/loan Plaid balance sign negation in today-point — §3.3 step 3 |
| R2RD2007 | WEALTH_KV symbol-cache KV failure: fallback to fresh D1 query — §3.3 step 1 |
| R2RD2009 | KV lock key uses full SHA256 (64 hex chars) — §3.3 step 7 |
| R2RD2010 | IngestionLog written before Twelve Data calls (in_progress status); updated after — §3.3 step 8 |
| R2RD2011 | Downsample threshold: always downsample series older than 3 years when range=all AND accounts>5 — §6.3 |
| R2RD2013 | DST transition test added to market-hours.test.ts — §7 |
| R3RD2013 | Invalid ?sort= falls back to default silently (no 400); invalid ?dir= falls back to 'asc' — §3.4 |
| R3RD2014 | chart-geometry.test.ts added to §7 |
| R3RD2016 | SortableTable integer comparator changed to parseInt with isNaN guard — §4.5 |
| R1RD2007, R2RD2007 | WEALTH_KV symbol cache KV failure documented as non-fatal — §3.3 |
| R0RD2007 | Budget day is UTC — documented explicitly in §3.3 step 4 |
| R0RD2008 | Accounts table row-click routes to /wealth/accounts/<id>; header-click routes to /wealth/accounts?sort=<col> — §5 |
| R0RD2009 | Migration: realized-gains replaces realized-gl on home page in Phase B; fetchRealizedGL → deprecated — §9 |
| R0RD2012 | Coverage warnings displayed per-card independently; test case distinguishes year N-1 warning — §4.2 |
| R1RD2013 | year upper bound changed to current calendar year (not current+1) — §3.2 |
| R3RD2008 | wealth-link-audit.test.ts moved to tests/integration/ — §5 |
| R3RD2009 | ESLint no-restricted-paths decision: $lib/components/wealth/ is shared wealth UI; CRM routes may import shared primitives; add explicit rule comment — §4.1 |
| R4RD2016 | Custom date range error: role='alert' + aria-invalid + aria-describedby — §4.1 |
| R4RD2017 | Shimmer animation respects prefers-reduced-motion — §4.4 |
| R4RD2018 | Tooltip relative time calculated at tooltip-open time; not in aria-live region — §4.1 |
| R4RD2019 | Custom button placed in its own role='group' aria-label='Custom range' — §4.1 |
| R4RD2020 | aria-label revised to describe next action: "Sort by {col} — click to sort {next dir}" — §4.5 |
| R0RD2010 | Symbol cell in TopHoldingsTable: <a href> inside <td>; row click goes to symbol page — §4.3 |
| R0RD2011 | ESLint verification step added to §9 Phase A |
| R0RD2016 | Recent Activity <thead> column list referenced in §9 Phase E |
| R1RD2011 | Weekly downsample aggregation method: last trading day of week (Friday) balance — §6.3 |
| R2RD2012 | D1 budget analysis updated to include per-account queries; db.batch() for account series — §6.3 |

---

## 1. Context and constraints

Six Plaid Items are linked in production (BofA, Chase, E\*TRADE, PenFed, Vanguard×2; Schwab pending). The `plaid_account_balance_snapshot` table in D1 has been accumulating daily rows since the Workers cron at `7 10 * * *` UTC went live. The `historical_price` table is fed by the Twelve Data EOD cron at `30 7 * * *` UTC for the symbol allowlist defined in REQ-WC-013.

This round upgrades the `/wealth` dashboard from "static last-month snapshot" to "live, interactive, signal-rich" without rebuilding the data plumbing. Almost all required data already exists in D1.

**Auth model (corrected from v1):** Google OAuth + HMAC-signed session cookie guard (`requireWealthAccess` in `hooks.server.ts`) at `/wealth/*`. CF Access is NOT in place — it is deferred to TF-002 per the parent migration spec §A4 (live inspection during M0h confirmed ZERO CF Access applications configured). Do not expect `Cf-Access-Authenticated-User-Email` headers. Browser routes use the cookie guard only; internal-key routes (`X-Internal-Key`) are for Python/script callers only and are never mixed with browser-facing endpoints.

Decimal precision rule from REQ-WC-004 holds throughout: every monetary value crosses the wire as a string (`"1234.56"`), never as a JavaScript `number`. Components parse to `Decimal.js` (or display-format via `Intl.NumberFormat`) but never coerce to `Number`.

---

## 2. Data plane

### 2.1 Tables consumed (all exist in D1; no schema changes)

| Table | Used for |
|---|---|
| `account` | Account list for per-account toggle (REQ-WD-002), Top Holdings grouping (REQ-WD-006) |
| `account_balance_snapshot` | Historical balance seed (REQ-WD-003); canonical dedup via `loadHistoryState()` in `src/lib/server/wealth/db-helpers.ts` |
| `plaid_account_balance_snapshot` | Daily Plaid balances (REQ-WD-003); same dedup function |
| `position_snapshot` | Latest holdings → symbols list (REQ-WD-006, REQ-WD-008) |
| `historical_price` | Benchmark + holding overlay price history; prior_close for Day Δ% (REQ-WD-003, REQ-WD-006). **Column name: `trade_date` (not `as_of`). Composite PK: `(symbol, trade_date)`.** |
| `live_quote` | Intraday quote cache; written by `repriced-today`; read-first before `historical_price` for current day (REQ-WD-006, REQ-WD-008). Columns: `symbol PK, price TEXT, currency TEXT, fetched_at INTEGER epoch-ms, source TEXT, is_stale INTEGER 0\|1`. |
| `realized_gain_loss` | Realized cards (REQ-WD-004), `/wealth/realized` table (REQ-WD-005) |
| `cost_basis_lot` | `/wealth/realized` detail rows |
| `ingestion_log` | Shared Twelve Data daily budget counter (`getDailyTwelveDataCount`) |
| `audit_events` | Audit trail for `repriced-today` bulk runs |

**Canonical two-tier dedup — Phase D obligation:** The current `loadHistoryState()` in `src/lib/server/wealth/db-helpers.ts` only queries `account_balance_snapshot`. As of v5, it does NOT yet query `plaid_account_balance_snapshot`. Phase D (see §9) MUST extend `loadHistoryState` to also SELECT from `plaid_account_balance_snapshot` and merge the rows into `balancesByAccount` using the following column mapping and dedup rule:

- Column mapping: `plaid_account_balance_snapshot.current_balance` → `balance`; `plaid_account_balance_snapshot.snapshot_date` → `as_of`
- Dedup rule: when an account has BOTH an `account_balance_snapshot` row AND a `plaid_account_balance_snapshot` row for the same `account_id` and same date, **the Plaid row wins** (overrides the XLSX-seed row). Plaid is the higher-fidelity source for recent dates.
- **Scale normalization (P1-R1RD5004):** `plaid_account_balance_snapshot.current_balance` is stored at scale 4 (Numeric 18,4 per `schema-wealth.ts`), while `account_balance_snapshot.balance` is scale 2 (Numeric 14,2). When merging Plaid rows into the series, MUST quantize to scale 2: `new Decimal(plaidBalance).toDecimalPlaces(2, Decimal.ROUND_HALF_UP)`. Never insert a scale-4 string directly — this would cause the net-worth aggregate to mix precision levels.
- **Credit/loan sign — canonical discriminator (P1-R1RD5001):** Use `plaid_account_balance_snapshot.plaid_account_type` to determine sign negation — NOT `account.account_type`. The canonical discriminator is the `PLAID_LIABILITY_TYPES` Set defined at `src/lib/server/wealth/plaid-routes.ts:737`: `new Set(['credit', 'loan'])`. When `plaid_account_type` is in this set, `current_balance` is stored as a positive amount owed — negate before adding to net-worth aggregate. The `account.account_type` enum does NOT contain 'credit' or 'loan' — those are Plaid-product-type concepts, not local account classifications. Using `account.account_type` for this check would silently miss all credit/loan accounts.
- **Quantize + negate (order is immaterial under ROUND_HALF_UP — L2-FIN-001 resolved):** `Decimal.ROUND_HALF_UP` rounds half **away from zero** (it is sign-symmetric), so `-1234.5650` → `-1234.57` whether you negate-then-quantize or quantize-then-negate — the two orders give identical results at every `.005` boundary. There is therefore NO mandatory ordering and no 1-cent hazard here (an earlier draft incorrectly claimed ROUND_HALF_UP rounds toward +∞; that is `ROUND_HALF_CEIL`, which the codebase does not use). The only mandatory rule is: quantize the positive scale-4 balance to scale 2, and negate iff it is a liability. Canonical snippet (either order is correct):
  ```ts
  const quantized = new Decimal(plaidBalance).toDecimalPlaces(2, Decimal.ROUND_HALF_UP);
  const signed = PLAID_LIABILITY_TYPES.has(plaidAccountType) ? quantized.negated() : quantized;
  ```
  §7 WD-003 keeps only the scale-normalization assertion (`current_balance='1234.5678'` → `'1234.57'`); it does NOT assert an order-dependent value, since no such dependency exists under ROUND_HALF_UP.
- **Investment-account today-point exclusion (mandatory, L2-TRC-001 / L2-FIN-003):** for any account that has ≥1 `position_snapshot` row (an "investment account"), exclude its **today-dated** `plaid_account_balance_snapshot` row from the merge entirely — the today-point for these accounts comes EXCLUSIVELY from repriced positions × price (see today-point assembly below). This applies regardless of `plaid_account_type` (E*TRADE/Vanguard/Schwab arrive as `plaid_account_type='investment'` AND have `position_snapshot` rows). The Plaid-wins rule above applies only to dates strictly before today for investment accounts; for non-investment accounts (no `position_snapshot` rows — cash/checking/credit/loan) Plaid-wins applies to all dates including today. Without this exclusion the investment account is double-counted on the current day (Plaid intraday snapshot + repriced market value).
- **Today-point assembly (Phase D, L2-TRC-005):** after the snapshot merge, for each investment account compute today's value as `Σ(position_snapshot.quantity × price)` over that account's most-recent `position_snapshot` rows, where `price` follows the §3.5 source order (`live_quote.price` if `fetched_at` within `CACHE_TTL_MS`, else `historical_price.close` for the latest `trade_date`). This replaces the excluded today-dated Plaid row. Non-investment accounts use their most-recent `plaid_account_balance_snapshot` row (sign-adjusted per the discriminator rule).

Until Phase D is complete, `loadHistoryState` only produces XLSX-seed history with no daily Plaid augmentation. The §7 WD-003 test "plaid merge" asserts the extended behavior. Any test that validates the extended endpoint should spy on `loadHistoryState` to confirm it is invoked.

**`loadHistoryState` query strategy:** The existing function does a full table scan of `account_balance_snapshot` and `plaid_account_balance_snapshot` and groups rows in TypeScript (no per-account D1 queries). When the extended `networth-history` endpoint adds `accounts` param support, per-account filtering happens in TypeScript after the full scan — it does NOT switch to per-account D1 queries with `db.batch()`. The D1 budget analysis in §6.3 for per-account series therefore applies to benchmark/overlay queries only (up to 8 queries batched), not to balance-snapshot queries. The full-scan approach is acceptable given the bounded D1 corpus (≤10 years × 16 accounts = ≤58,000 rows for balance snapshots).

### 2.2 New tables: **none.**

### 2.3 New indexes

- No new index for `realized_gain_loss` — use bounded-range predicate `closed_date >= ? AND closed_date < ?` so the existing `ix_realized_gl_closed_date` index is hit. **No new index needed.**
- `historical_price` already has `ix_historical_price_date` on `(trade_date)`. For the per-symbol prior_close lookup, queries will use `WHERE symbol = ? ORDER BY trade_date DESC LIMIT 1` — this will do an index scan on `trade_date`, then filter by symbol. A covering index `idx_hp_symbol_trade_date_desc` on `(symbol, trade_date DESC)` would improve this to an index seek; propose adding it if the top-holdings query shows > 10ms D1 latency in soak testing. **Decision: do not add pre-emptively; add after measuring.**
- `live_quote` PK on `symbol` is already a covering index for single-symbol lookups.

---

## 3. API surface (new + extended)

All new routes live under `src/routes/(wealth)/wealth/api/brokerage/` in `sparkry-crm`. Auth: in-app cookie guard (`hooks.server.ts`) + `requireWealthAccess`.

### 3.1 `GET /wealth/api/brokerage/networth-history` — extended (REQ-WD-001, WD-002, WD-003)

Query params (additive — **backward compat note:** default is `'all'` to preserve existing behavior for callers that pass no `range` param; existing callers already get full history):

| Param | Type | Default | Notes |
|---|---|---|---|
| `range` | enum | `'all'` | One of `1w, 2w, 1mo, 3mo, ytd, 1y, 3y, 5y, 10y, all, custom` |
| `start` | `YYYY-MM-DD` | — | Required if `range=custom` |
| `end` | `YYYY-MM-DD` | — | Required if `range=custom`; must be `>= start` and `<= today`; `end > today` is clamped to today |
| `accounts` | comma-list of account_ids | — | If present, also returns per-account series. **Param name: `accounts`; value type: comma-separated UUID strings, each being an `account_id` from the `account` table.** Example: `accounts=uuid1,uuid2`. Each value validated as UUID: `/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i`. Non-matching → 400 `{error_code:'invalid_account_id'}`. Max 16. **Disambiguation:** The existing route also reads an `account_ids` param (underscore, no validation) for tag-based filtering. The new `accounts` param (no underscore, UUID-validated) is for per-account chart series. Both may coexist; they serve different purposes. In Phase D, evaluate whether `account_ids` (tag filter) should be deprecated in favour of a unified param. |
| `include_unmatched` | bool | `false` | Preserved for migration parity |
| `benchmarks` | comma-list ⊆ `{SPY,QQQ,VTI}` | — | ETF proxies for S&P 500, NASDAQ, Total Market (existing REQ-WC-013 allowlist); returns extra series rescaled to `pct_change`. BND is in the EOD data allowlist (REQ-WC-010) but is NOT a valid benchmark toggle option — `benchmarks=BND` → 400 `{error_code:'invalid_benchmark'}`. |
| `overlay_symbols` | comma-list of held symbols | — | Same `pct_change` rescale as benchmarks. Each symbol validated against `/^[A-Z0-9.^]{1,12}$/`. Invalid symbol → 400 `{error_code:'invalid_symbol'}`. Max 5. |
| `dir` | `asc\|desc` | `asc` | Sort direction for series data. Any value other than `'asc'` or `'desc'` silently falls back to `'asc'`. |

**BND clarification (R0RD2003):** BND has `historical_price` rows (fed by the EOD cron) and is valid data in the DB. It is NOT exposed in the benchmark toggle UI because REQ-WD-003 specifies three toggle options: S&P 500 (SPY), NASDAQ (QQQ), Total Market (VTI). BND remains in the EOD cron allowlist so its historical data is maintained, but the `/networth-history` benchmarks param explicitly rejects BND with 400. This is not a contradiction — the EOD allowlist and the UI benchmark set are different scopes.

**`overlay_symbols` `^` character note:** The validation regex `/^[A-Z0-9.^]{1,12}$/` allows the `^` character (for parity with the EOD cron allowlist syntax). However, raw index tickers like `^GSPC`, `^IXIC`, and `^DJI` are NOT held positions and are NOT in the EOD cron allowlist — they have zero `historical_price` rows. A request with `overlay_symbols=^GSPC` will pass regex validation, query `historical_price` for `'^GSPC'`, find no rows, and return `series: []` (HTTP 200) without a 400 error. This is acceptable and documented — the UI type-ahead only offers held symbols, so raw index tickers would only appear via direct API calls. The API does not cross-reference against held symbols.

**Per-account zero-balance series:** When an `account_id` in the `accounts` param is valid (UUID format, passes validation) but has zero balance rows in `account_balance_snapshot` or `plaid_account_balance_snapshot`, the endpoint returns the account series with `series: []` (empty array) — it is NOT omitted from the `accounts` array. The frontend should render these as a flat line at zero or a "no data" legend entry.

**Credit/loan today-point sign (P1-R1RD5001 — canonical discriminator):** Accounts with no `position_snapshot` (cash, credit, checking) contribute their most-recent `plaid_account_balance_snapshot.current_balance` as the today-point addition. Per `schema-wealth.ts` sign-convention: credit/loan balances are stored as positive (amount owed) in D1. When assembling the today-point net-worth total, the endpoint **MUST negate credit/loan balances** (credit/loan account types reduce net worth). **Investment accounts (≥1 `position_snapshot` row) do NOT use this path** — their today-dated Plaid row is excluded and the today-point comes from the positions×price assembly; see §2.1 "Investment-account today-point exclusion" and "Today-point assembly". This prevents the investment-account double-count.

**Canonical sign discriminator:** Use `plaid_account_balance_snapshot.plaid_account_type` with `PLAID_LIABILITY_TYPES = new Set(['credit', 'loan'])` from `src/lib/server/wealth/plaid-routes.ts:737`. Do NOT use `account.account_type` — that enum does not contain 'credit' or 'loan' values and would silently fail to negate any Plaid liability balance. The `plaid_account_type` column comes from the Plaid API response and contains Plaid product-type strings ('depository', 'credit', 'loan', 'investment', etc.). Test: a Plaid checking account (`plaid_account_type='depository'`, `current_balance='10000.00'`) → today-point is INCREASED by 10,000; a Plaid credit card (`plaid_account_type='credit'`, `current_balance='5000.00'`) → today-point is REDUCED by 5,000; a Plaid mortgage (`plaid_account_type='loan'`, `current_balance='200000.00'`) → today-point is REDUCED by 200,000.

**Benchmark symbols (corrected from v1):** Use `{SPY, QQQ, VTI}` — these are in the existing REQ-WC-013 EOD allowlist and have `historical_price` rows. Raw index tickers `^GSPC/^IXIC/^DJI` are NOT held positions, are NOT in the EOD cron allowlist, and have zero `historical_price` rows — using them would produce empty series. The UI labels them: `SPY → "S&P 500 (SPY)"`, `QQQ → "NASDAQ (QQQ)"`, `VTI → "Total Market (VTI)"`.

**pct_change formula (Decimal.js, explicit):**

```ts
// anchor = the first date in the returned window (index 0). Forward-fill (see
//   "Non-trading-day forward-fill" below) is applied BEFORE anchor computation, so
//   every date — including a weekend/holiday start — has a non-null close. Therefore
//   the anchor is ALWAYS index 0 and pct_change[0] === "0.0000" is guaranteed.
//   ("first date with non-null close" ≡ index 0 once forward-fill is applied; do not
//    implement an anchor-search loop that skips nulls and then forget forward-fill.)
// Each benchmark/overlay series anchors independently at its own index 0.
// ZERO-ANCHOR GUARD: if close[0] is null, empty, or new Decimal(close[0]).isZero(), return empty series
// (do not divide by zero — return [] for that series rather than HTTP 500)
pct_change[t] = new Decimal(close[t])
  .minus(close[anchor])
  .div(close[anchor])
  .mul(100)
  .toFixed(4);  // always 4 decimal places; e.g. "0.0000", "12.3457", "-3.2100"
// pct_change[anchor] is always "0.0000"
```

`toFixed(4)` is the serialization method — never `toString()` or `toDecimalPlaces(4)`. Confirmed: `pct_change[0]` for every benchmark/overlay series is always `"0.0000"`. A range change requires a new API request — clients must not reuse a cached series across range changes.

**Non-trading-day forward-fill (closed decision):** When no `historical_price` row exists for a date in the requested range (weekend, holiday), the API returns a row for that date using the most recent prior close (forward-fill). This applies to both benchmark and overlay series. Every date in the requested range appears in the response, with no gaps. Test case: a range spanning a weekend must return Saturday and Sunday benchmark values equal to Friday's value. Test case (added v3): a range spanning a US holiday must return the holiday date with the prior trading day's close forward-filled. Test case: range starting on Saturday — the anchor date is Saturday, but pct_change is `"0.0000"` because the Saturday value equals the most recent prior close (which is also the anchor close).

**Validation:** invalid `range` → HTTP 400 `{error_code: 'invalid_range'}`. `start > end` → 400 `{error_code: 'invalid_range'}`. `end > today` → clamp to today. `benchmarks=BND` (or any symbol not in `{SPY,QQQ,VTI}`) → 400 `{error_code: 'invalid_benchmark'}`. `overlay_symbols` containing an invalid symbol pattern → 400 `{error_code: 'invalid_symbol'}`. `accounts` containing a non-UUID value → 400 `{error_code: 'invalid_account_id'}`. Range alias mapping is centralized in `src/lib/server/wealth/range-helpers.ts` and unit-tested per slug (see §7).

**Downsampled response field:** When the server applies weekly downsampling (see §6.3), the response includes:

```json
"downsampled_before": "2023-05-13"
```

This field is `null` when no downsampling is applied. Clients **MUST display** a "Weekly data before {date}" note in the chart's legend area or subtitle when `downsampled_before` is non-null — this ensures users understand the data density change. The note is a visible text element (not aria-hidden). Test: when `downsampled_before` is non-null, the chart legend contains the text "Weekly data before". If the field is absent (backward compat with callers that don't send it), assume no downsampling.

**Response field name stability (P1-R1RD5002):** The existing production `networth-history/+server.ts` (line 222) emits the aggregate net-worth value as **`balance_total`** — not `net_worth`. Renaming this field would be a breaking change for all callers (Top Holdings page, `/wealth/+page.svelte`, any external script). Decision: **preserve `balance_total` as the field name in the extended response**. The new `accounts`, `benchmarks`, and `overlays` arrays are additive; the existing `balance_total` per-row field is unchanged. If a rename to `net_worth` is desired for clarity it must be done as a separate versioned migration with a deprecation period — it is NOT part of this design.

Response (additive — `balance_total` field preserved from production):
```json
{
  "as_of": "2026-05-13T16:00:00Z",
  "range": "1y",
  "start": "2025-05-13",
  "end":   "2026-05-13",
  "downsampled_before": null,
  "aggregate": [{"date": "2025-05-13", "balance_total": "1234567.89"}, ...],
  "accounts": [
    {"account_id": "uuid", "broker": "vanguard", "label": "Travis Roth IRA",
     "series": [{"date": "...", "balance": "..."}, ...]}
  ],
  "benchmarks": [
    {"symbol": "SPY", "label": "S&P 500 (SPY)",
     "series": [{"date": "...", "pct_change": "0.0000"}, ...]}
  ],
  "overlays": [
    {"symbol": "AAPL", "label": "AAPL",
     "series": [{"date": "...", "pct_change": "0.0000"}, ...]}
  ]
}
```

### 3.2 `GET /wealth/api/brokerage/realized-gains` — new (REQ-WD-004)

Query: `year=<YYYY>` (required, `2000 ≤ year ≤ current calendar year`). Future years → 400 `{error_code:'invalid_year'}`. `current+1` is rejected — the home-page shows YTD + prior 2 full years, none of which are future years.

**Date boundary format:** `closed_date` is stored as `YYYY-MM-DD` TEXT. Filter: `closed_date >= '<year>-01-01' AND closed_date < '<year+1>-01-01'`. Lexicographic ISO-8601 comparison is correct for TEXT date columns in SQLite. Boundary test: `closed_date='2026-12-31'` appears in `year=2026`; `closed_date='2027-01-01'` does not.

Response:
```json
{
  "year": 2026,
  "ytd": true,
  "proceeds":  "12345.67",
  "cost":      "10000.00",
  "net":       "2345.67",
  "lot_count": 17,
  "term_breakdown": {
    "st":      "500.00",
    "lt":      "1845.67",
    "unknown": "0.00"
  },
  "wash_sale_count": 2,
  "total_disallowed_loss": "150.00",
  "coverage_warnings": []
}
```

**`ytd` field:** convenience field (`true` when `year == current calendar year`). Additive to the REQ-WD-004 spec; noted here for clarity.

**Decimal aggregation (REQ-WC-004 compliant):** Do NOT `SUM()` TEXT columns in D1 — SQLite silently casts to IEEE 754 double, causing precision loss. Instead:

1. Fetch all matching rows: `SELECT account_id, proceeds, cost_basis, gain_loss, lt_gain_loss, st_gain_loss, unadjusted_cost_basis, term, wash_sale, disallowed_loss FROM realized_gain_loss WHERE closed_date >= ? AND closed_date < ?`
2. Aggregate in TypeScript using Decimal.js:

```ts
import Decimal from 'decimal.js';

let proceeds = new Decimal(0);
let cost = new Decimal(0);
let st = new Decimal(0);
let lt = new Decimal(0);
let unknown = new Decimal(0);
let washSaleCount = 0;
let totalDisallowedLoss = new Decimal(0);

for (const row of rows) {
  const p = new Decimal(row.proceeds);
  const c = new Decimal(row.cost_basis);
  proceeds = proceeds.plus(p);
  cost = cost.plus(c);

  // Term breakdown: prefer broker-stored lt_gain_loss/st_gain_loss columns when non-NULL.
  // These are broker-computed values that may include wash-sale cost-basis adjustments.
  // Fall back to recomputed proceeds-cost_basis ONLY when the column is NULL.
  // This means st+lt+unknown == SUM(broker gain_loss) when all rows have term columns populated,
  // and st+lt+unknown == proceeds-cost when they are NULL. Document the source in coverage_warnings
  // if a mix of NULL and non-NULL term columns is detected.
  if (row.term === 'st') {
    st = st.plus(row.st_gain_loss != null ? new Decimal(row.st_gain_loss) : p.minus(c));
  } else if (row.term === 'lt') {
    lt = lt.plus(row.lt_gain_loss != null ? new Decimal(row.lt_gain_loss) : p.minus(c));
  } else {
    unknown = unknown.plus(p.minus(c));  // NULL or unrecognized term → unknown bucket
  }

  if (row.wash_sale) {
    washSaleCount++;
    totalDisallowedLoss = totalDisallowedLoss.plus(
      new Decimal(row.disallowed_loss ?? '0')
    );
  }
}

const net = proceeds.minus(cost);
// Note: net = SUM(proceeds) - SUM(cost_basis) (recomputed).
// This may differ from SUM(gain_loss) by rounding epsilon if brokers rounded per-lot.
// The 'net' field is always the recomputed value; broker-stored gain_loss is not surfaced
// in the response to avoid confusion with two different totals.
```

**`net` computation:** `Decimal(proceeds_str).minus(Decimal(cost_str))`, not a third SQL SUM. Serialized as `net.toFixed(2)`.

**`unknown` bucket:** `st + lt + unknown` equals `proceeds.minus(cost)` when all broker-stored columns are NULL (pure recompute path). When broker-stored columns are used, `st + lt + unknown` equals `SUM(lt_gain_loss) + SUM(st_gain_loss) + SUM(proceeds-cost for unknown-term rows)`. Either way, the invariant is internally consistent per the chosen code path. Frontend shows "ST: X · LT: Y · Unknown: Z" when `unknown != "0.00"`, with a link to `/wealth/realized?filter=unknown_term`.

**`wash_sale_count` + `total_disallowed_loss`:** Required for 1099-B reconciliation. The spec uses `cost_basis` (adjusted basis) for `net` — adapters that write wash-sale-adjusted cost_basis ensure net is correct; `disallowed_loss` is surfaced additionally for auditability.

**Coverage warnings:** The response includes `coverage_warnings: []` by default. Three triggers append warning entries:

1. **Missing realized rows for held accounts:** If any `account_id` has rows in `position_snapshot` but zero rows in `realized_gain_loss` for the requested year. This trigger requires a second D1 query (or `db.batch()` call): `SELECT DISTINCT account_id FROM position_snapshot WHERE account_id IS NOT NULL`. Cross-reference with the `realized_gain_loss` account_ids seen in the aggregation loop. To populate the `broker` field in the warning entry, JOIN or lookup the `account` table: `SELECT id, broker FROM account WHERE id IN (...)` for the missing account_ids.

2. **Zero-cost-basis lots with available unadjusted basis:** If any `realized_gain_loss` row has `new Decimal(row.cost_basis).isZero() === true` AND `unadjusted_cost_basis IS NOT NULL`.

3. **Mixed NULL/non-NULL term breakdown columns (P1-R1RD5003):** If, within the aggregation loop, some rows have `lt_gain_loss` or `st_gain_loss` non-NULL while other rows for the same year have those columns NULL. Detection: track `hasNonNullTerm = false` and `hasNullTerm = false` as booleans through the loop; set each when the corresponding condition is seen. If both are `true` at the end of the loop, append a **year-scoped** warning entry with `scope: 'year'` and `message: "Mixed term data sources: some lots use broker-stored gain/loss columns, others use recomputed values. Review lot data for consistency."` This is a year-wide condition — it has no specific `account_id` or `broker` and MUST NOT fabricate those fields.

**`coverage_warnings` array type — TypeScript union (P1-R1RD5003):**

Triggers 1 and 2 produce account-scoped warnings; trigger 3 produces a year-scoped warning. The array is typed as a discriminated union:

```ts
type CoverageWarning =
  | { account_id: string; broker: string; message: string }    // triggers 1 + 2
  | { scope: 'year'; message: string };                         // trigger 3 only
```

Consumers MUST check for the `scope` key (or absence of `account_id`) to distinguish the two shapes. The `account_id` and `broker` fields are NEVER present on a year-scoped warning.

Warning entry shapes:
```json
"coverage_warnings": [
  {"account_id": "uuid", "broker": "vanguard", "message": "Realized G/L may be incomplete — no transaction history imported for this account."},
  {"scope": "year", "message": "Mixed term data sources: some lots use broker-stored gain/loss columns, others use recomputed values. Review lot data for consistency."}
]
```

Note: the zero-check (trigger 2) runs in the TypeScript aggregation loop using `new Decimal(row.cost_basis).isZero()` — Decimal.js normalizes all string variants (`'0'`, `'0.0'`, `'0.00'`, `'0.00000000'`) to zero equivalently. Do NOT use SQL `CAST(cost_basis AS REAL) = 0.0` — the check must be in TypeScript since `cost_basis` is a TEXT column and SQLite CAST behavior on TEXT columns is unreliable for precision values.

This warning is surfaced on home-page cards (see §4.2) and on the `/wealth/realized` detail page. Coverage warnings are evaluated and displayed **per-year independently** — a warning on the N-1 card does not imply any warning on the current year or N-2 cards.

Empty-year case: returns all zeros with `lot_count: 0`, `coverage_warnings: []`. No NaN possible.

**Migration from `/realized-gl`:** The existing `GET /wealth/api/brokerage/realized-gl` endpoint (returns `{by_year, wash_sales}` with `parseFloat()` — a REQ-WC-004 violation) is kept in Phase B with an `@deprecated` comment added to the route handler. In Phase B, `+page.svelte` is updated to call `realized-gains?year=<YYYY>` three times (current year, N-1, N-2) and the `fetchRealizedGL()` function in `wealth-api.ts` is deprecated (marked `@deprecated`). The float-returning `realized-gl` endpoint is NOT removed in Phase B — it is flagged for removal in a subsequent cleanup sprint after all callers migrate.

### 3.3 `POST /wealth/api/brokerage/repriced-today` — new (REQ-WD-008, supports WD-003/WD-006)

**Auth:** cookie guard only (`requireWealthAccess`). This is a browser-facing endpoint — `X-Internal-Key` is NOT required and NOT checked. (See §6.2 for resolution of REQ-WD-008 requirement text contradiction.)

**HTTP 200 for quota exhaustion:** intentional, matching REQ-WC-013a precedent in the quotes route (`is_stale: true, HTTP 200`). The operation is not a client error; degraded stale prices are still usable. Documented in route comment.

**Canonical response shape (all 200 responses):** Every HTTP 200 response from `repriced-today` — whether successful, quota-exhausted, or partially-degraded — uses the same shape:

```json
{
  "refreshed":    12,
  "skipped":      3,
  "errors":       0,
  "latest_as_of": "2026-05-13T20:31:00Z",
  "stale_symbols": [],
  "error_code":   null
}
```

`latest_as_of` is `null` when `refreshed === 0`. `error_code` is `null` when no error; set to `'quota_exhausted'`, `'db_error'`, or other string on degraded paths. `stale_symbols` is always present — an array of symbol strings that still need refreshing. The client uses `stale_symbols.length === 0` as the polling termination condition.

**Success-path `stale_symbols` value (normative, L2-FIN-002 / L2-FIN-009):** on a successful HTTP 200 (`error_code === null`), `stale_symbols` MUST be the subset of this invocation's originally-stale list that was NOT successfully refreshed this call: `stale_symbols = staleSymbols.filter(s => !successfullyRefreshed.has(s))`. Because at most `REPRICED_TODAY_BATCH_SIZE` (3) symbols are processed per call, when more than 3 symbols were stale this array is non-empty and the client MUST keep polling (it is not done). It is `[]` only when every originally-stale symbol is now fresh. (`quota_exhausted` → `stale_symbols = staleSymbols`; `db_error` → `stale_symbols = remaining`; both already specified in failure-modes.)

**Zero-symbols short-circuit:** If `position_snapshot` is empty or no active accounts have symbols, return `{refreshed:0, skipped:0, errors:0, latest_as_of:null, stale_symbols:[], error_code:null}` immediately without calling Twelve Data.

**Idempotency:** When `position_snapshot` contains symbols but all are fresh (non-zero symbols, zero stale after step 3), the endpoint exits early BEFORE acquiring the KV lock (step 4), BEFORE writing IngestionLog (step 5), and BEFORE calling Twelve Data (step 6). Return `{refreshed:0, skipped:allSymbols.length, errors:0, latest_as_of:null, stale_symbols:[], error_code:null}` immediately. No KV lock is acquired, no IngestionLog row is written, no audit_event is written. This ensures a second call immediately after a successful first call has zero side-effects. Test: see §7 WD-008 idempotency case.

**Logic:**

1. **Enumerate symbols:** Distinct symbols across all `position_snapshot` rows for active accounts. Extracted function `getSymbolsToFetch(db)` lives in `src/lib/server/wealth/symbol-helpers.ts` (extracted from `twelve-data-ingest.ts` — see §9 Phase A). Cache result 60s in WEALTH_KV to avoid repeated D1 scans. **KV failure is non-fatal:** if WEALTH_KV is unavailable or the cache read throws, silently fall back to a fresh D1 query and do NOT attempt to write a new cache entry.

2. **Load staleness timestamps (batched):** Use `db.batch()` for all symbol staleness reads — this avoids N individual D1 round-trips. Batch all `live_quote` reads in one call, then batch `historical_price` fallback reads for symbols that had no `live_quote` row:

   ```ts
   // Batch all live_quote reads
   const liveResults = await db.batch(
     allSymbols.map(sym => db.prepare('SELECT price, fetched_at, is_stale FROM live_quote WHERE symbol = ?').bind(sym))
   );
   // For symbols with no live_quote row, batch historical_price fallback reads
   const missingSymbols = allSymbols.filter((_, i) => !liveResults[i].results[0]);
   const histResults = missingSymbols.length > 0
     ? await db.batch(missingSymbols.map(sym =>
         db.prepare('SELECT close, trade_date FROM historical_price WHERE symbol = ? ORDER BY trade_date DESC LIMIT 1').bind(sym)
       ))
     : [];
   ```

   Use `live_quote.fetched_at` (epoch ms) if present; otherwise derive epoch ms from `historical_price.trade_date` (assume EOD = 21:00 UTC of that date). `db.batch()` is safe here because these are read-only, idempotent queries (REQ-WC-006 prohibition applies only to Plaid sync write paths).

3. **Determine staleness — DST-aware ET clock:** Reuse `isMarketOpen(nowMs: number): boolean` from `src/lib/server/wealth/market-hours.ts` (extracted from `src/routes/(wealth)/wealth/api/brokerage/quotes/+server.ts` in Phase A). This function **MUST use `Intl.DateTimeFormat` with `timeZone: 'America/New_York'`** — do NOT use a fixed UTC offset (-5 or -4). The existing implementation in quotes/+server.ts already does this correctly (verified in the `isMarketOpen` function body). In-window (Mon-Fri 09:30–16:00 ET): stale if `> STALENESS_MARKET_OPEN_MS` (default 5 min = 300_000ms). Outside window: stale if `> STALENESS_OFF_HOURS_MS` (default 24h = 86_400_000ms). Both are named constants in `src/lib/server/wealth/wd-constants.ts`.

   After determining staleness for each symbol, build the stale list sorted oldest-first (most stale first), then slice:

   ```ts
   const staleSymbols = allSymbols
     .filter(sym => isStale(sym, stalenessMap[sym], isMarketOpen(Date.now())))
     .sort((a, b) => (stalenessMap[a] ?? 0) - (stalenessMap[b] ?? 0))  // oldest fetched_at first
     .slice(0, /* capped in step 4b */);
   ```

   The sort ensures the "≤N most-stale symbols per invocation" claim is accurate.

4. **Acquire KV lock FIRST (before budget pre-check):** This ordering ensures no race window between reading the budget and holding the lock.

   ```ts
   // Step 4a: Acquire lock before reading budget
   const hashBuf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(email));
   const hex = Array.from(new Uint8Array(hashBuf)).map(b => b.toString(16).padStart(2, '0')).join('');
   const lockKey = 'repriced:lock:' + hex;   // full 64 hex chars of SHA256 — no truncation
   
   let kvAvailable = false;
   try {
     if (platform?.env?.WEALTH_KV) {
       const existing = await platform.env.WEALTH_KV.get(lockKey);
       if (existing) {
         // Application-layer 30s business rule: parse timestamp stored in lock value.
         // If acquired < 30s ago → 202 in_progress. If ≥ 30s → stale, overwrite below.
         const elapsed = Date.now() - parseInt(existing, 10);
         if (isNaN(elapsed) || elapsed < 30_000) {
           // 202 body is distinct from all 200 shapes — client checks response.status === 202
           return json({ status: 'in_progress' }, { status: 202 });
         }
         // Stale lock — fall through to overwrite below.
       }
       // expirationTtl must be ≥ 60 (Cloudflare KV minimum). Value is acquisition
       // timestamp so application layer can compute elapsed time without relying
       // solely on KV TTL (which is eventually consistent). §6.4 step 2.
       await platform.env.WEALTH_KV.put(lockKey, String(Date.now()), { expirationTtl: 60 });
       kvAvailable = true;
     } else {
       console.warn(JSON.stringify({ endpoint: 'repriced-today', step: 'kv-unavailable',
         message: 'WEALTH_KV binding not present — proceeding without rate-limit lock' }));
     }
   } catch (kvErr) {
     console.warn(JSON.stringify({ endpoint: 'repriced-today', step: 'kv-lock-error',
       error: String(kvErr) }));
     // Fail-open: proceed without rate limiting rather than crash
   }
   
   // Step 4b: Budget pre-check
   // Budget day is UTC (new Date().toISOString().substring(0,10)).
   // The budget counter resets at UTC midnight, which may differ from ET midnight by up to 5 hours.
   // This is intentional — 600 calls per UTC day. Documented here so implementors do not
   // 'fix' the UTC usage assuming it is a bug.
   const todayDate = new Date().toISOString().substring(0, 10);
   const dailyUsed = await getDailyTwelveDataCount(db, todayDate);
   const budgetRemaining = Math.max(0, DAILY_BUDGET - dailyUsed);
   if (budgetRemaining === 0) {
     if (kvAvailable) await platform.env.WEALTH_KV.delete(lockKey);  // release lock
     return json({
       refreshed: 0, skipped: staleSymbols.length, errors: 0,
       latest_as_of: null, stale_symbols: staleSymbols, error_code: 'quota_exhausted'
     });
   }
   // Refresh at most `budgetRemaining` symbols (and at most REPRICED_TODAY_BATCH_SIZE per invocation)
   const toRefresh = staleSymbols.slice(0, Math.min(budgetRemaining, REPRICED_TODAY_BATCH_SIZE));
   ```

   `REPRICED_TODAY_BATCH_SIZE = 3` (named constant — see §6.3 and §8.11 for rationale). The client calls the endpoint repeatedly (see polling model below) until `stale_symbols` is empty or quota is exhausted.

5. **Write IngestionLog AFTER budget check passes (in_progress status):**

   IngestionLog is written AFTER step 4b (budget check) confirms `budgetRemaining > 0` AND AFTER the `toRefresh` slice has been computed. This ensures that `quota_exhausted` early returns (step 4b) do NOT write any IngestionLog row — only runs that will actually call Twelve Data get an IngestionLog entry.

   ```ts
   // Executed only after step 4b confirms budget > 0 and toRefresh is non-empty
   const runId = crypto.randomUUID();
   await writeIngestionLog(db, {
     id: runId, source: 'twelve_data', status: 'in_progress',
     recordsProcessed: 0, recordsFailed: 0
   });
   // Then proceed with Twelve Data fetches.
   // After all fetches complete, UPDATE the ingestion_log row with final counts.
   // This ensures the budget counter (getDailyTwelveDataCount via SUM(records_processed))
   // is eventually consistent even if the Worker crashes after fetches but before the update.
   ```

   **Why after budget check:** Writing before budget check would cause quota_exhausted returns to always create a spurious `in_progress` IngestionLog row. The in_progress row contributes 0 to SUM(records_processed) — so it does not inflate the budget counter, but it does create audit noise on every exhausted call. Writing after the budget check avoids this. The trade-off: if the Worker crashes after Twelve Data fetches but before the IngestionLog write, those calls are not tracked. This is acceptable given the bounded 3-call batch size.

6. **Fetch from Twelve Data — sequential with gaps to avoid rate limits:**

   Twelve Data free tier enforces 8 requests/minute on a **sliding window** (not a fixed bucket). Any burst of calls in under 1 second would consume multiple slots simultaneously, triggering 429 on subsequent calls. The existing EOD cron enforces `REQUEST_INTERVAL_MS = 7500ms` between calls (= Math.ceil(60_000 / 8)) for exactly this reason.

   `REPRICED_TODAY_BATCH_SIZE = 3` with 7500ms sequential gaps. Wall-clock per invocation: 3 calls × ~500ms fetch + 2 × 7500ms gaps ≈ 16.5s — well within the 30s Workers wall-clock limit. Budget cost: 3 API calls per invocation. Client polls every 5s after the invocation completes; second call proceeds with the next 3 stale symbols.

   ```ts
   const results: Array<{symbol: string; ok: boolean; price?: string}> = [];
   for (let i = 0; i < toRefresh.length; i++) {
     if (i > 0) await sleep(REQUEST_INTERVAL_MS); // 7500ms between calls
     const sym = toRefresh[i];
     try {
       const resp = await fetch(`https://api.twelvedata.com/quote?symbol=${sym}&apikey=${apiKey}`);
       const data = await resp.json() as { close?: string|number; price?: string|number; status?: string };
       // Field mapping: use 'close ?? price' — Twelve Data /quote returns 'close' as the
       // last trade price (confirmed by the `close ?? price` comment in the `sanitizeUrl`
       // function of quotes/+server.ts and in twelve-data-ingest.ts fetch loop).
       // Do NOT use 'price' alone — it may be undefined in the actual API response.
       const rawPrice = data.close ?? data.price;
       if (data.status === 'error' || rawPrice == null) {
         results.push({ symbol: sym, ok: false });
       } else {
         results.push({ symbol: sym, ok: true, price: String(rawPrice) });
       }
     } catch (err) {
       results.push({ symbol: sym, ok: false });
     }
   }
   ```

   **Per-symbol error isolation (CLAUDE.md critical pattern):** Each result is independent. A failure for one symbol increments `errors`; the loop continues. Only when ALL symbols fail does the endpoint return a degraded response.

7. **Upsert into `live_quote` (not `historical_price`):**
   ```sql
   INSERT INTO live_quote (symbol, price, currency, fetched_at, source, is_stale)
   VALUES (?, ?, 'USD', ?, 'twelve_data', 0)
   ON CONFLICT(symbol) DO UPDATE SET
     price = excluded.price,
     fetched_at = excluded.fetched_at,
     is_stale = 0,
     source = 'twelve_data';
   ```
   `historical_price` is NOT touched by `repriced-today`. The prior_close for Day Δ% uses `historical_price` for the most recent `trade_date` strictly before today (see §3.5). `live_quote` is a freshness cache — UPDATE/DELETE are permitted per schema-wealth.ts comment.

8. **Audit + IngestionLog finalization:**
   - Call `updateIngestionLog(db, runId, { status: errors === 0 ? 'success' : 'partial', recordsProcessed: refreshed, recordsFailed: errors })`. This UPDATE targets the `ingestion_log` row written in step 5, replacing the `in_progress` / `records_processed=0` placeholder with final counts. `updateIngestionLog` is defined in `db-helpers.ts` alongside `writeIngestionLog`. **Note on `run_at`:** `updateIngestionLog` does NOT update `run_at` — it is intentionally preserved as the start time (set at INSERT by `writeIngestionLog` via `datetime('now')`). `run_at` = "when did this run begin", not "when did it complete". This is correct behavior for budget accounting (e.g., a run starting at 23:59 UTC is counted on that UTC day).
   - Write `audit_events` row: `entity_id = runId`, `entity_type = 'repriced_today_run'`, `field_changed = 'bulk_refresh'`, `new_value = JSON.stringify({refreshed, skipped, errors})`, `changed_by = 'browser:repriced-today'`. This satisfies the `entity_id NOT NULL` constraint.
   - **Delete KV lock on successful completion** (do not rely on TTL): `if (kvAvailable) await platform.env.WEALTH_KV.delete(lockKey)`. Use the `kvAvailable` boolean guard (same pattern as step 4) — NOT optional chaining (`?.`). Using `kvAvailable` is explicit and consistent; if KV was unavailable at lock-acquisition time, `kvAvailable` is `false` and no delete is attempted (correct). This ensures the client's 5s re-poll succeeds immediately rather than waiting for the 30s TTL to expire.

   **Budget counter correctness (R0RD2001/R2RD2001):** `getDailyTwelveDataCount` uses `SELECT COALESCE(SUM(records_processed), 0) AS cnt FROM ingestion_log WHERE source = 'twelve_data' AND run_at LIKE ?` **bound as `todayDate + '%'`** (e.g. `'2026-05-13%'`) — the trailing `%` is mandatory: `run_at` is written by SQLite `datetime('now')` as `'YYYY-MM-DD HH:MM:SS'`, so a bare `'2026-05-13'` LIKE parameter matches zero rows, the counter reads 0, and the 600/day quota check is silently bypassed (L2-FIN-010 / L2-CON-014). It sums `records_processed`, NOT `COUNT(*)`. This means one repriced-today run refreshing 3 symbols increments the counter by 3, not by 1. The EOD cron writes ONE row with `records_processed = <number_of_symbols_fetched>` — so it also counts correctly. The quotes route writes ONE row per HTTP request with `records_processed = <symbols_fetched_from_twelve_data>`. All three consumers write to the same table with the same semantics.

**Failure modes:**
- Twelve Data quota exhausted → HTTP 200 `{refreshed:0, skipped:staleSymbols.length, errors:0, latest_as_of:null, stale_symbols:staleSymbols, error_code:'quota_exhausted'}` (canonical 200 shape). No IngestionLog row written on this path (IngestionLog is written only after budget check passes — see step 5).
- Twelve Data 5xx → retry with capped exponential backoff (**2 attempts** at 200ms/600ms — NOT 3 attempts). After 2 retries, increment `errors` for that symbol and continue batch. **Rationale for 2 attempts at 200ms/600ms:** worst-case per symbol with retries = 200ms + 600ms + ~500ms fetch ≈ 1.3s. Total for 3 symbols: 3 × 1.3s + 2 × 7500ms = 18.9s — well within 30s limit. The old 3-attempt 500ms/1s/2s scheme produced worst-case ≈30s at the limit boundary.
- **Hard 25s wall-clock cap:** After `Date.now() - startMs > 25_000`, abort any remaining symbols in `toRefresh` (mark them as skipped), finalize IngestionLog with current counts, **DELETE the KV lock** (the 25s cap is a graceful-exit path — same lock-disposal class as successful completion per §6.4 step 3/4, NOT the §6.4-step-5 error path — so the client's next poll can immediately fetch the next batch instead of stalling 30s on a stale lock), and return the canonical 200 shape with `stale_symbols` = the not-yet-refreshed subset. This prevents Workers termination from leaving `in_progress` IngestionLog rows. `startMs` is captured at the top of the handler before any async work. §7 WD-008 adds: `25s cap → KV lock deleted (not left for TTL)`.
- D1 write failure for one symbol → increment `errors`, continue (per-record isolation). If ALL symbols fail D1 writes → HTTP 200 `{errors: N, refreshed:0, skipped:remaining.length, latest_as_of:null, stale_symbols:remaining, error_code:'db_error'}` (canonical 200 shape).
- Already-locked (concurrent call) → HTTP 202 `{status: 'in_progress'}`. **This is a distinct shape from all 200 responses.** The client detects it by checking `response.status === 202`, not by inspecting a JSON field. There is no `refreshed`, `stale_symbols`, or `error_code` on the 202 body.
- WEALTH_KV unavailable → log warning, proceed without lock (fail-open).

### 3.4 `/wealth/realized` page — SSR-only, new (REQ-WD-005, WD-007)

Page renders a sortable table of `realized_gain_loss` rows via a SvelteKit `+page.server.ts` `load()` function — there is **no separate `GET /wealth/api/brokerage/realized-detail` API endpoint**. The data is loaded server-side on page render, with `?year=`, `?sort=`, and `?dir=` query params read in the `load()` function and used to build a D1 query. Client-side sort uses `<SortableTable>` (§4.5) with the already-loaded row set.

**SSR load function contract (`+page.server.ts`):**
```ts
export const load: PageServerLoad = async ({ url, platform }) => {
  const year = parseInt(url.searchParams.get('year') ?? String(new Date().getFullYear()), 10);
  const sort = url.searchParams.get('sort') ?? 'closed_date';
  const dir  = url.searchParams.get('dir')  ?? 'desc';
  // Validate year, sort, dir; build and execute D1 query; return rows
  return { rows, year };
};
```

Columns: Symbol, Opened, Closed, Term, Shares, Proceeds, Cost Basis, Net G/L, Wash Sale. Default sort `closed_date DESC`.

**Wash sale flag UX (closed decision):** A non-sortable "W" badge column. `wash_sale = true` → amber badge with `role='img' aria-label='Wash sale: loss disallowed'` and a focus-accessible tooltip (visible on both hover and keyboard focus) reading "Wash sale: loss disallowed. See IRS Pub 550." — do NOT rely on `title=` attribute alone (not exposed by most AT and not visible to keyboard-only users). `wash_sale = false` → em-dash. The column header is "W-Sale" with a `sortable: false` flag in the `Column` definition (see §4.5); no `aria-sort` on non-sortable columns.

**Query params:** `?year=`, `?sort=`, `?dir=`. SSR pre-sorts; client-side sort uses Decimal.js comparators (see §4.5).

**Sort column allowlist (SQL injection guard):** The `?sort=` param is validated against this per-route allowlist before use in any D1 query. **Invalid sort column → silently fall back to default sort (no 400 — avoids enumerating valid column names).** **Invalid `?dir=` → silently fall back to `'asc'` (no 400).** No string interpolation — use a `Map<string, string>` from allowed param name to SQL column name.

| Route | Allowed sort columns |
|---|---|
| `/wealth/realized` | `symbol`, `opened_date`, `closed_date`, `term`, `shares` (quantity), `proceeds`, `cost_basis`, `gain_loss` |
| `/wealth/holdings` | `symbol`, `description`, `shares` (quantity), `price`, `market_value`, `day_delta_pct` |
| `/wealth/accounts` | `broker`, `account_name`, `account_type`, `balance` |
| `/wealth/transactions` | `trade_date`, `symbol`, `action`, `quantity`, `amount` |

**`?dir=` validation:** `dir` must be one of `'asc'` or `'desc'`. Any other value (including `'; DROP TABLE--'`) silently falls back to `'asc'`. Never interpolate `dir` directly into SQL — use a ternary: `const safeDir = dir === 'desc' ? 'DESC' : 'ASC'`.

Test: `?sort='; DROP TABLE realized_gain_loss;--` → HTTP 200 with default sort. `?dir='; DROP TABLE--` → HTTP 200, sort direction = ASC.

### 3.5 `GET /wealth/api/brokerage/top-holdings` — extended (REQ-WD-006)

The existing endpoint is extended to return `price`, `prior_close`, `day_delta_pct`, and recomputed `market_value`.

**Extended response fields per symbol:**
```json
{
  "symbol": "AAPL",
  "description": "Apple Inc.",
  "quantity": "10.00000000",
  "price": "189.45000000",
  "prior_close": "187.20000000",
  "day_delta_pct": "1.2019",
  "market_value": "1894.50",
  "account_count": 2
}
```

**`price` source:** `live_quote.price` if `fetched_at > (now - CACHE_TTL_MS)`; else `historical_price.close` for the latest `trade_date`.

**NULL quantity guard:** `position_snapshot.quantity` is nullable TEXT in the schema. Before any Decimal operation:
```ts
if (!quantity || !price) {
  // Return null for computed fields; frontend renders em-dash
  return { ...row, price: null, prior_close: null, day_delta_pct: null, market_value: null };
}
```
`new Decimal(null)` throws `[DecimalError] Invalid argument: null` — never pass null to the Decimal constructor.

**`prior_close` query — ET date boundary:** Use the current US/Eastern date (via `Intl.DateTimeFormat` with `America/New_York`) for the `trade_date < ?` boundary, consistent with how `isMarketOpen` uses ET. This ensures prior_close correctly identifies the most recent trading day before the current US session regardless of UTC midnight crossings.

```sql
SELECT close FROM historical_price
WHERE symbol = ?
  AND trade_date < ?  -- US/Eastern date in YYYY-MM-DD (NOT UTC date)
ORDER BY trade_date DESC
LIMIT 1;
```

**`day_delta_pct` guard (L2-TRC-002 / L2-FIN-007):** return `null` if `prior_close` is NULL, empty string, or numerically zero — test as `!priorClose || new Decimal(priorClose).isZero()`, NOT a string-equality check against `'0.00000000'` (the TEXT column may store `'0'`, `'0.0'`, `'0.00'`, etc.; an exact-string compare would miss those and divide by zero). Likewise return `null` if `price` is NULL/empty. Frontend renders em-dash for null. Never divide by zero, never `new Decimal('')` (throws).

**Day Δ% formula (Decimal.js, server-side):**
```ts
if (price && priorClose && new Decimal(priorClose).gt(0)) {
  dayDeltaPct = new Decimal(price).minus(priorClose).div(priorClose).mul(100).toFixed(4);
}
```

**`market_value` computation (Decimal.js, server-side):**
```ts
// quantity and price are TEXT; multiply via Decimal.js, round to scale 2
const mv = new Decimal(quantity).mul(new Decimal(price)).toDecimalPlaces(2, Decimal.ROUND_HALF_UP);
market_value = mv.toFixed(2);
```
Never use `Number()` or `parseFloat()` on quantity or price before multiplication.

---

## 4. Frontend components (Svelte 5 runes throughout — REQ-WC-011)

### 4.1 `<NetWorthChart>` — replaces the existing chart on `/wealth/+page.svelte`

**Files (new directory `src/lib/components/wealth/` to be created):**
```
src/lib/components/wealth/          # NEW directory
  NetWorthChart.svelte
  RangeSelector.svelte              # 1w/2w/1mo/.../all/custom
  CustomDateRange.svelte            # native <input type='date'> pair (see below)
  AccountMultiSelect.svelte         # multi-select grouped by broker
  BenchmarkToggleGroup.svelte       # SPY / QQQ / VTI
  SymbolOverlayPicker.svelte        # combobox (see below)
  RefreshingBanner.svelte           # REQ-WD-008
  account-colors.ts                 # 16-color OKLCH palette
  chart-geometry.ts                 # PURE TS: scale computation, path generation, secondary-axis math
```

**ESLint isolation decision (R3RD2009):** `$lib/components/wealth/` contains shared wealth-UI primitives (SortableTable, etc.). CRM routes MAY import from `$lib/components/wealth/` — the existing `eslint.config.js` `no-restricted-paths` rule only blocks imports between `(crm)` and `(wealth)` route groups (not `$lib`). This is intentional: `$lib` is the shared surface. Add a comment in `src/lib/components/wealth/index.ts`: `// Wealth UI primitives — importable by both (wealth) and (crm) route groups per ESLint config.` Do NOT add an additional restrictive ESLint rule for this directory. Verify in Phase A: run `pnpm run lint` and confirm no new violations.

**Store (corrected naming — flat `src/lib/` pattern matching `wealth-quotes.svelte.ts`):**
`src/lib/wealth-chart.svelte.ts` (`.svelte.ts` suffix for rune-bearing modules, consistent with `wealth-quotes.svelte.ts`).

```ts
type Range = '1w'|'2w'|'1mo'|'3mo'|'ytd'|'1y'|'3y'|'5y'|'10y'|'all'|'custom';
type ChartState = {
  range: Range; customStart?: string; customEnd?: string;
  selectedAccounts: string[];    // account_ids
  benchmarks: Set<'SPY'|'QQQ'|'VTI'>;   // ETF proxies only
  overlaySymbols: string[];
};
```

**Persistence (SSR-safe):** sessionStorage keys `wd:nw:range`, `wd:nw:accounts`, `wd:nw:benchmarks`, `wd:nw:overlays`. Read in `onMount` only (not during SSR). Default on first visit: `range='all'`, no accounts, no benchmarks, no overlays. The chart renders with default state on first SSR pass and updates client-side in `onMount` — no layout flash because the chart data fetch is also triggered on mount. `RangeSelector` renders the selected button client-only after mount (using `{#if mounted}`). Test: navigate to /wealth with sessionStorage `wd:nw:range = '3y'`, reload, assert 3y button is selected and chart shows 3y data without 1y flicker.

**Chart library (resolved — §8.1 closed):** The existing `/wealth` chart is hand-rolled SVG (confirmed in `+page.svelte:731-859` — raw `<svg>` paths, no chart library import). This approach is retained. Secondary y-axis is implemented in `chart-geometry.ts` as a pure TypeScript module:

- Primary y-axis: dollar scale, `yFor(balance: number): number` function
- Secondary y-axis: percent-change scale, `yForPct(pct: number): number` function, separate tick range computed from `[minPct, maxPct]` across all benchmark/overlay series
- Two y-axis label columns rendered in SVG: dollars on left, percent on right (when benchmarks/overlays active)
- A visual separator line at x=CHART_PAD_X_LEFT renders the left axis; right axis labels at x=CHART_W-CHART_PAD_X_RIGHT

Extracting geometry to `chart-geometry.ts` (pure TS) enables ≥90% unit-test coverage without a browser environment.

**Legend as visibility toggle (REQ-WD-002):** The chart legend IS the multi-select control — selecting an account in `AccountMultiSelect` adds its series to the chart (visible); deselecting removes it. There is no separate legend component. Within the chart itself, each rendered line has a labeled dot or inline label at the right edge; clicking that label is equivalent to deselecting in `AccountMultiSelect`. No separate hidden/shown state layer — the `selectedAccounts` set in `ChartState` is the single source of truth for visibility.

**Max-visible series limit:** When the user attempts to add a 9th total series (accounts + benchmarks + overlays combined), an inline message is shown: "Maximum 8 series visible at once — remove one to add another." This message MUST be rendered in an `aria-live='polite'` region so AT users are notified when the limit is hit (the message appears dynamically; without a live region, keyboard/screen-reader users hear nothing). The server-side 400 limit (16 accounts, 3 benchmarks, 5 overlays) prevents API abuse but the UI imposes a readability limit of 8 total.

**Range selector visual grouping:** The 11 options are grouped in the UI with a subtle visual divider:
- Short: `1W · 2W · 1Mo · 3Mo`
- Long: `YTD · 1Y · 3Y · 5Y · 10Y · All`
- `[Custom...]` button separately

Rendered as **three** `role='group'` elements:
- `<div role='group' aria-label='Short ranges'>` containing 1W, 2W, 1Mo, 3Mo buttons
- `<div role='group' aria-label='Long ranges'>` containing YTD, 1Y, 3Y, 5Y, 10Y, All buttons
- `<div role='group' aria-label='Custom range'>` containing the Custom button

This ensures AT announces the group label for each section, including Custom. Each range button uses `aria-pressed='true'` when it is the currently-selected range, `aria-pressed='false'` otherwise.

**Color palette:** Deterministic by `hash(account_id) % 16`. The 16-color OKLCH palette must be verified under **protanopia, deuteranopia, and tritanopia** before implementation freeze. Use a color-blindness simulator (Coblis or Adobe Color accessibility tool) to verify adjacent palette colors remain distinguishable under all three types. If colors collapse under any simulation, add line-dash style variation as a secondary discriminator (e.g., solid, dashed, dotted). Document verification result for all three types in `account-colors.ts` file header. Red and green are reserved for Day Δ% in REQ-WD-006 and must not appear in the 16-account palette.

**Today's chart-point label (resolved):** The rightmost point is labeled with today's date (matching all other x-axis points). The tooltip shows: `"Prices as of HH:MM [local time] (intra-day snapshot)"`. Do NOT use "Live" — it implies streaming which is not implemented. Test: render NetWorthChart with a today-point active; assert the tooltip does NOT contain the string "Live".

**Tooltip timezone:** Use `Intl.DateTimeFormat` with the user's local timezone (no dependency on luxon — confirmed absent from `sparkry-crm/package.json`). Show timestamp as local time: `"Prices as of 3:47 PM"`. The relative time suffix `"(last updated N min ago)"` is optional cosmetic detail calculated **at tooltip-open time** (not continuously updated via interval). It is NOT placed in an `aria-live` region — AT does not announce it on appearance. The primary timestamp `"Prices as of HH:MM"` is the accessible value.

**CustomDateRange.svelte:** Use native `<input type='date'>` for both start and end fields. This provides full keyboard support and AT compatibility without custom ARIA scaffolding. Accept platform-variable appearance as the correct tradeoff. `start` must be `<= end`; both must be `<= today`.

- The start date input **MUST** have an associated `<label>` (e.g. `<label for="custom-start">Start date</label>`) or `aria-label="Start date"`. The end date input **MUST** have an associated `<label>` or `aria-label="End date"`. Without an accessible name, AT announces only "date input" without indicating which field is Start vs. End (WCAG 1.3.1, 4.1.2).
- The end date input **MUST** have `max={today}` attribute (today in `YYYY-MM-DD` format, dynamically set).
- The start date input **MUST** have `max={endValue ?? today}` attribute.
- Validation errors are displayed in a `<p role='alert'>` immediately below the date inputs. Each input with an error receives `aria-invalid='true'` and `aria-describedby` pointing to the error paragraph id.
- Invalid pair does not trigger an API call.

**SymbolOverlayPicker.svelte (combobox spec):** Full ARIA combobox pattern:
- `<input role='combobox' aria-expanded={open} aria-controls='overlay-listbox' aria-activedescendant={activeId}>` with 200ms debounce
- Empty input → shows all held symbols (≤80) in a scrollable list
- `<ul role='listbox' id='overlay-listbox'>` with `<li role='option' aria-selected={selected ? 'true' : 'false'}>` per symbol. **Every `<li role='option'>` MUST have `aria-selected` present as either `"true"` or `"false"` — never omit the attribute on unselected options.** Per ARIA spec, `role='option'` requires `aria-selected` to be explicitly set on every option in a listbox; omitting it causes inconsistent AT behavior.
- Arrow Up/Down navigates options; Enter selects focused option; Escape closes dropdown
- **Tab key while listbox is open:** close the listbox without selection and move focus to the next interactive element in the page tab order (per ARIA APG combobox pattern §3.1.3). Do NOT trap Tab inside the listbox.
- **Shift+Tab:** close listbox and move focus to the previous interactive element.
- Selected symbols rendered as removable chips above the input: `<button aria-label='Remove {SYMBOL}'>×</button>`
- **Chip removal focus management:** After a chip is removed via keyboard activation, focus MUST be programmatically moved to: the next chip in the list (if one exists), or the preceding chip (if the removed chip was last), or the combobox input (if no chips remain). Do not leave focus on `document.body`.

**BenchmarkToggleGroup.svelte ARIA spec:** SPY/QQQ/VTI are multi-select toggle buttons (any combination can be active simultaneously — not a radio group). Wrap in `<div role='group' aria-label='Benchmark overlays'>`. Each button uses `aria-pressed='true'` when active, `aria-pressed='false'` otherwise. **Keyboard (roving tabindex):** Only one button in the group is in the tab order at a time (`tabindex='0'` on the currently-active or most-recently-focused button; `tabindex='-1'` on all others). Arrow Left/Right moves focus between buttons within the group; Tab exits the group entirely to the next interactive element on the page. Space/Enter toggles the focused button. This is the ARIA APG toolbar/radio-group roving-tabindex pattern — it prevents 3 unnecessary tab stops for 3 buttons.

**RangeSelector keyboard (roving tabindex):** The 11 range buttons are organized into 3 groups (Short, Long, Custom per §4.1). Within each group, use the same roving tabindex pattern: Arrow Left/Right moves between buttons in the group; Tab exits the group. Across groups, Tab moves between the three `role='group'` containers. This reduces from 11 tab stops to 3 (one per group), which is far more keyboard-friendly.

**AccountMultiSelect desktop ARIA pattern:** The desktop dropdown (above the net-worth chart) uses a **listbox** pattern:
- Trigger: `<button aria-haspopup='listbox' aria-expanded={open} aria-label='Select accounts'>` — shows current selection count when collapsed (e.g., "3 accounts selected").
- Dropdown container: `<ul role='listbox' aria-multiselectable='true' aria-label='Accounts'>` — appears below the trigger when open.
- Each option: `<li role='option' aria-selected='true'|'false'>` — every option has `aria-selected` explicitly set (never omitted).
- Keyboard navigation within listbox: Arrow Up/Down moves between options; Space toggles `aria-selected` on focused option; Enter closes the listbox; Escape closes without changing selection.
- Focus management: when the listbox closes (Escape or Enter), focus returns to the trigger button.
- **"Clear" button:** Rendered as `<button type='button' aria-label='Clear all account overlays'>Clear</button>` (or visible text "Clear" without aria-label if text is sufficient). After keyboard activation (Enter or Space), focus moves to the AccountMultiSelect trigger button. After activation, `selectedAccounts` is reset to empty and `sessionStorage` key `wd:nw:accounts` is cleared. Test assertions: see §7 WD-002 Clear test cases.

**SVG chart accessibility:** The `<NetWorthChart>` SVG element MUST have `role='img'` and `aria-label='Net worth chart: {range}'` (e.g., `aria-label='Net worth chart: 1 year'`). Additionally, include a visually-hidden summary below the SVG:
```html
<p class="sr-only">Net worth chart. Range: {rangeLabel}. Current value: {currentNetWorth}. Start of period: {startNetWorth}. Change: {changeFormatted}.</p>
```
This provides AT users meaningful information without duplicating the full data table. Test: the SVG in NetWorthChart has an `aria-label` attribute containing "Net worth".

**Inline SVG chart labels (deselection path):** Each rendered line's inline label at the right edge is `aria-hidden='true'` and NOT independently keyboard-focusable. All deselection routing goes through `AccountMultiSelect` (the single source of truth). This simplifies the AT model: users interact with the listbox, not SVG elements. The click handler on the inline label still fires for mouse users for convenience, but keyboard users use AccountMultiSelect.

**WCAG 1.4.3 contrast ratios:** All text/foreground colors in this design MUST meet WCAG 1.4.3 AA contrast ratio:
- Normal text (< 18pt or < 14pt bold): minimum 4.5:1 against background
- Large text (≥ 18pt or ≥ 14pt bold) and non-text elements: minimum 3:1 against background
- Specifically verify: green Δ% on white/card background; red Δ% on white/card background; amber badge text on amber background; negative net (red) on card background; neutral zero Δ% color on white/card background.
- Verification is required in Phase E using a contrast checker (e.g., WebAIM Contrast Checker or browser DevTools accessibility panel). If any color fails, adjust the OKLCH palette value to meet threshold before implementation freeze.
- **The neutral color for zero Δ% (§4.3)** must be a mid-tone gray or dark text that meets 4.5:1 on white — do NOT use a light gray that fails contrast.

**Mobile floor (≤767px):** Though mobile polish is out of scope, the dashboard must not be actively broken on narrow screens:
- `AccountMultiSelect` collapses to a count badge "N accounts selected" with a bottom-sheet/drawer on tap. The drawer **MUST** be implemented with `role='dialog'` `aria-modal='true'` `aria-label='Select accounts'`, focus trapped inside while open (Tab/Shift+Tab cycle within the dialog), Escape key closes and returns focus to the trigger badge element. `aria-modal='true'` is required so AT does not allow virtual cursor navigation outside the dialog boundary (VoiceOver/JAWS behavior without it).
- `BenchmarkToggleGroup` and `SymbolOverlayPicker` collapse behind a single "Overlays ▾" disclosure button
- Max visible chart series on mobile: 3 (excess hidden with "N more — view on desktop" note)
- Chart SVG is `viewBox`-based and scales to viewport width automatically

### 4.2 `<RealizedGLCards>` — new on `/wealth/+page.svelte`

Three cards in a horizontal flex row for current year YTD + prior 2 full years. Each card shows: year label, big net number, sub-line "Proceeds X · Cost Y", term breakdown "ST: A · LT: B · Unknown: C" (unknown omitted if zero). **Click target: wrap the card content in `<a href='/wealth/realized?year={year}' aria-label='View {year} realized gains and losses'>`. Using an `<a>` ensures native keyboard Enter activation, correct link semantics for AT, and correct right-click/open-in-new-tab browser behavior.** Do NOT use a `<div>` with a click handler — that is not keyboard-accessible.

**Negative net display:** `"–$1,234.56"` — explicit minus sign before the dollar sign, in red. Also rendered as parenthetical for accounting convention familiarity: `"(–$1,234.56)"`. Include a ▼ shape affordance in the DOM (aria-hidden="true") for additional visual reinforcement — the ▼ is **NOT optional** when the card's red color is the only other cue. This is NOT an "or color" alternative — the minus sign + parenthetical satisfy WCAG 1.4.1; the ▼ is an additional visual aid. Do not rely on color alone per WCAG 1.4.1.

**Coverage warning on home-page cards — per-year independent:** Each card independently evaluates its own `coverage_warnings[]` from the `realized-gains?year=<YYYY>` response. If `coverage_warnings.length > 0` for a specific year, render a small warning icon on that card: `<span role='img' aria-label='Data coverage warning: some accounts may have incomplete data' tabindex='0'>⚠</span>`. The `tabindex='0'` is required for keyboard focusability — a bare `<span>` with `role='img'` has no native focus behavior, so keyboard-only users could not reach the tooltip without it. The ⚠ glyph itself is inside the aria-labeled span, so AT reads the meaningful label rather than "warning sign". Pair with a tooltip triggered on both hover and keyboard focus: "Some accounts may have incomplete data — see details."

A warning on the N-1 card does NOT imply warnings on the current-year or N-2 cards. Test: mock `realized-gains` to return `coverage_warnings` only for year N-1; assert ⚠ appears on the N-1 card only.

### 4.3 `<TopHoldingsTable>` — replaces existing on `/wealth/+page.svelte`

Columns per REQ-WD-006: Symbol | Description | Shares | Price | Market Value | Day Δ% | Account Count. Uses `<SortableTable>` primitive (§4.5). **Symbol cell links to `/wealth/holdings/<symbol>` via `<a href='/wealth/holdings/{symbol}'>` inside the `<td>` — a standard nested anchor inside a table cell (valid HTML).** Other cells in the row are not independently clickable; clicking anywhere in a non-symbol cell has no navigation effect.

**Day Δ% display:** Value shown with explicit sign and shape affordance:
```html
<span aria-hidden="true">▲</span>
<span class="sr-only">increased</span>
+1.20%
```
or
```html
<span aria-hidden="true">▼</span>
<span class="sr-only">decreased</span>
–0.45%
```
**Zero delta case** (`dayDeltaPct = "0.0000"`): omit both the ▲/▼ glyph and the sr-only direction text entirely. Render `+0.00%` with a neutral color (neither green nor red). No sr-only "increased" or "decreased" text — announcing "increased +0.00%" is misleading. Test: `dayDeltaPct='0.0000'` → no `▲` or `▼` in DOM, no "increased"/"decreased" sr-only text.

The ▲/▼ glyph is `aria-hidden="true"` (AT would read "black up-pointing triangle" which is meaningless); the `sr-only` span provides the meaningful text alternative ("increased" / "decreased"). Color (green/red) is additional, not sole, discriminator. This satisfies WCAG 1.4.1.

### 4.4 `<RefreshingBanner>` — new

**States:** `idle | loading | refreshing | error` (loading = initial chart data fetch; refreshing = repriced-today in flight).

**ARIA — two separate DOM elements (not dynamic role swap):** Dynamically changing `role` on an existing DOM element is not reliably supported by AT (NVDA/JAWS/VoiceOver may cache the original role at mount time). Use two permanently-present elements.

**Canonical implementation (P1-R4RD5004 — always-present/empty-content pattern):**

Both `role='status'` and `role='alert'` elements are ALWAYS present in the DOM. In the non-active state they have empty `textContent` — no content is set until the element needs to announce. This is the most reliable NVDA+Chrome pattern: the AT registers the live region on mount and watches it for mutations. When content is later inserted into the already-present element, the announcement fires reliably.

```html
<!-- Both elements always present; AT registers them on mount -->
<div role="status" aria-live="polite" aria-atomic="true">
  {#if state !== 'error'}
    <span class="sr-only">{normalStateText}</span>
    {visibleContent}
  {/if}
</div>
<div role="alert" aria-live="assertive" aria-atomic="true">
  {#if state === 'error'}
    <span class="sr-only">Error: {errorText}</span>
    {errorContent}
  {/if}
</div>
```

In the idle state: `role='status'` has empty textContent (no sr-only text, per changelog finding P3-R4RD3012 — no AT announcement on initial page load). `role='alert'` has empty textContent. When state transitions to 'error', content is inserted into the already-present `role='alert'` element — the assertive announcement fires. When error clears, the `role='alert'` content is removed (empty textContent again).

**Critical: `display:none` MUST NOT be used on `role='alert'` or `role='status'` elements.** Elements hidden with `display:none` are removed from the accessibility tree — when they are later shown with content, NVDA/JAWS may not announce the change. The always-present/empty-content pattern above avoids this entirely by never hiding the elements.

> **Footnote — `visibility:hidden` alternative:** If the always-present/empty-content pattern is impractical (e.g., the visible-content slot cannot be conditionally empty due to layout constraints), `visibility:hidden` (not `display:none`) may be used as a fallback. `visibility:hidden` keeps the element in the accessibility tree while hiding it visually. However, NVDA+Chrome has documented reliability issues with visibility transitions: content inserted at the same tick as a `visibility:hidden`→`visible` transition may not be announced. If using this pattern, insert content in a `requestAnimationFrame` callback AFTER the visibility change to give the AT time to re-register the element. The canonical always-present/empty-content pattern above is preferred precisely because it avoids this timing hazard.

Normal states (idle, loading, refreshing) update the `role='status'` element. Error state populates and shows the `role='alert'` element (assertive announcement fires when content is set and element becomes visible). Each state includes a visually-hidden `<span class='sr-only'>` text node (e.g., "Prices refreshed" for idle after success; "Could not refresh prices — showing last known" for error).

**Skeleton loading state:** While the initial `networth-history` call is in flight, `<NetWorthChart>` renders chart axes and labels immediately with a shimmer overlay on the series area. The shimmer animation **MUST** be disabled when `prefers-reduced-motion: reduce` is set — substitute a static gray fill instead of the moving gradient. After 10s with no response, show a retry affordance:

```html
<button type="button" aria-label="Retry loading chart data" aria-disabled={isRetrying}>
  Could not load chart data — Retry
</button>
```

The button MUST be `<button type='button'>` (not `<a>` or `<div>`). While the retry is in flight, set `aria-disabled='true'` (and add visual disabled styling) to prevent double-submission. On click, re-issue the `networth-history` fetch and reset the 10s timeout.

**Banner error retry button:** The error state in `role='alert'` includes a retry button:
```html
<button type="button" aria-label="Retry refreshing prices" aria-disabled={isRetrying}>
  Retry
</button>
```
Same `aria-disabled` pattern during retry flight.

**Polling max-retry cap:** The client MUST NOT poll indefinitely. After `MAX_RETRIES = 6` polls (6 × `CLIENT_RETRY_MS` = 30s), transition to the error state with message "Could not refresh prices — refresh timed out, showing last known." Add `MAX_RETRIES = 6` to `src/lib/wealth-constants.ts`. The `pollCount` cap applies to BOTH continuation paths below — an HTTP 202 (concurrent lock) AND an HTTP 200 with `stale_symbols.length > 0` (more batches remaining) each increment `pollCount`; reaching `MAX_RETRIES` on either path → error state.

**State machine:**
1. On mount → `loading` state; `role='status'` element has empty sr-only span (no AT announcement on initial render); fetch `networth-history`; on resolve → fetch repriced-today
2. If `networth-history` fetch succeeds and prices are stale → `refreshing`; POST repriced-today
3. If repriced-today returns HTTP 200, `error_code === null`, and `stale_symbols.length === 0` → `idle`; call `onRefreshed` callback (terminal — every originally-stale symbol is now fresh)
3a. If repriced-today returns HTTP 200, `error_code === null`, and `stale_symbols.length > 0` → **stay `refreshing`** (a batch completed but more symbols remain — `REPRICED_TODAY_BATCH_SIZE` is 3); call `onRefreshed` so the chart/holdings re-render the freshly-priced subset, then if `pollCount < MAX_RETRIES` increment `pollCount`, wait `CLIENT_RETRY_MS`, re-POST; else → `error` state. Do NOT go idle while `stale_symbols` is non-empty.
4. If repriced-today returns HTTP 202 → body is `{status: 'in_progress'}`; banner stays `refreshing`; if `pollCount < MAX_RETRIES` increment `pollCount`, wait `CLIENT_RETRY_MS`, re-poll; else → `error`. **Detect by `response.status === 202`** (HTTP status code), not by reading a JSON field — the 202 body shape is distinct from all 200 shapes.
5. If repriced-today returns HTTP 200 with `error_code` non-null → `error` state with retry button
6. If repriced-today returns HTTP 200 with `refreshed === 0` and `stale_symbols.length === 0` (idempotent / zero-symbols / all-fresh) → `idle` immediately

**Today-point fallback:** When repriced-today returns an error or is skipped, the chart's today-point falls back to the current net-worth total from the existing `/wealth/api/brokerage/networth` endpoint (the same fallback as the current implementation using `chartLiveValue`). The fallback point renders with a dashed circle instead of solid, and a tooltip "Last known balance as of <date>."

**Event coupling (Svelte 5 runes):** `<RefreshingBanner>` accepts an `onRefreshed: () => void` callback prop from the parent page. The parent's `onRefreshed` handler increments a reactive `$state` counter (`refreshCount`) that `<NetWorthChart>` and `<TopHoldingsTable>` subscribe to via `$derived` for re-fetch triggering. The staleness check is server-authoritative — the client simply POSTs to `repriced-today` on mount and lets the server decide if a refresh is needed (no client-side threshold duplication from `wealth-quotes.svelte.ts`).

### 4.5 `<SortableTable>` — primitive, used everywhere (REQ-WD-007)

**Location:** `src/lib/components/wealth/SortableTable.svelte` (wealth UI primitive; importable by both wealth and CRM routes per §4.1 ESLint decision).

```ts
type ValueType = 'string' | 'decimal' | 'integer' | 'date';

type Column = {
  key: string;
  label: string;
  valueType: ValueType;
  sortable?: boolean;  // default true; false for icon/badge columns (no aria-sort, no tabindex)
};
```

**Decimal-aware sort comparator:**
- `decimal` columns: `new Decimal(a).comparedTo(new Decimal(b))` — never lexicographic. For performance, pre-map to Decimal instances before sort: `const vals = rows.map(r => ({row: r, val: new Decimal(r[col])}))`.
- `date` columns: `a.localeCompare(b)` (ISO-8601 strings sort correctly lexicographically)
- `string` columns: `a.localeCompare(b)`
- `integer` columns: `parseInt(a, 10) - parseInt(b, 10)` with `isNaN` guard: if either value is `NaN` (from `null`, `''`, or non-numeric), sort that row to the end. **Do NOT use `Number(a) - Number(b)`** — `Number('')` and `Number(null)` both return `0`, silently hiding nulls.

**Keyboard accessibility — WCAG-conformant sortable headers:**
- Each sortable `<th>` uses its **implicit `columnheader` role** — do NOT add `role='button'` on `<th>` (invalid ARIA per ARIA in HTML spec §2.5; `<th>` has implicit role `columnheader` which cannot be overridden with `role='button'`).
- Add `tabindex='0'` to make the `<th>` keyboard-focusable.
- Add `aria-sort='none|ascending|descending'` to reflect current sort state. **All non-active-sort columns start with `aria-sort='none'`.**
- **Three-state sort cycle (REQ-WD-007):** none → ascending → descending → none. A third click on the active column removes the sort (returns to `aria-sort='none'`), restoring the original load order (natural D1 query order). This matches the requirement `asc → desc → none on click`.
- `aria-label` is dynamic and describes the **next action** for all three states:
  - `aria-sort='none'` → `"Sort by {col} ascending"` (no "click to" prefix needed; the label IS the action)
  - `aria-sort='ascending'` → `"Sort by {col} — click to sort descending"`
  - `aria-sort='descending'` → `"Sort by {col} — click to remove sort"`
  This avoids double-announcement of state (AT reads `aria-sort` for current state; `aria-label` gives the action hint). The word "none" is not used in the visible or sr label — "remove sort" is natural English.
- On `keydown` for **both Enter and Space**: trigger sort cycle. **The Space handler MUST call `event.preventDefault()`** to suppress the browser's default page-scroll behavior.
- Non-sortable columns: `sortable: false` → `<th>` has no `tabindex`, no `aria-sort`. Used for the wash-sale badge column.

**Wash-sale 'W' badge accessibility:** The badge is a `<span>` with `role='img' aria-label='Wash sale: loss disallowed'`. The tooltip ("Wash sale: loss disallowed. See IRS Pub 550.") is a custom tooltip component that appears on **both hover and keyboard focus**. **Tooltip ARIA pattern (ARIA APG §3.3.1):** The tooltip element has `role='tooltip'` and a unique `id` (e.g., `id='ws-tooltip-{rowIndex}'`). The trigger element (the 'W' badge span) has `aria-describedby={tooltip-id}`. This ensures AT reads the tooltip content when the trigger is focused, without requiring the tooltip to be open/visible. Test: when the wash-sale badge is focused, the DOM includes an element with `role='tooltip'` and the trigger's `aria-describedby` links to that element's `id`. Do NOT use `title=` attribute as the sole mechanism (not exposed by most AT in default mode; not visible to keyboard-only users who cannot hover).

**Coverage warning icon tooltip ARIA pattern (§4.2):** The ⚠ span has `aria-describedby` pointing to the tooltip element's `id`. The tooltip element has `role='tooltip'`. Same pattern as the wash-sale badge above.

**Empty state for `/wealth/realized`:** When the page has zero rows for the selected year, the `<SortableTable>` MUST render a `<tbody>` with a single row:
```html
<tbody>
  <tr>
    <td colspan="9" style="text-align:center; padding:2rem">No realized transactions in {year}.</td>
  </tr>
</tbody>
```
The `SortableTable` component accepts an optional `emptyMessage: string` prop. When `rows.length === 0` and `emptyMessage` is provided, render this single-row empty state instead of an empty `<tbody>`. An empty `<tbody>` (with only a `<thead>`) is valid HTML but AT may announce "0 rows" without context. The colspan value equals the number of columns (9 for `/wealth/realized`).

Props: `columns: Column[]`, `rows: Row[]`, `defaultSort?: {col: string, dir: 'asc'|'desc'}`, `routeOnClick?: string`. When `defaultSort` is absent, all columns start in the `none` state (original load order). Renders semantic `<table>` with `<thead><tr>` containing `<th>` per column.

**SSR sort column validation:** The server-side handler validates `?sort=` against the per-route allowlist in §3.4 before constructing any D1 query. Allowed column names are stored in a `Map<string, string>` (param → SQL column) and the param is looked up — never interpolated directly. Invalid → fall back to default sort (silent, no 400).

---

## 5. Routing changes

| Existing | New / changed |
|---|---|
| `/wealth/+page.svelte` | Modified — uses new chart + cards + sortable mini-tables |
| `/wealth/holdings/+page.svelte` | Add SSR `?sort=&dir=` |
| `/wealth/accounts/+page.svelte` | Add SSR `?sort=&dir=` |
| `/wealth/transactions/+page.svelte` | Add SSR `?sort=&dir=` |
| `/wealth/realized/+page.svelte` | **New** — REQ-WD-004/005 detail page |
| `/wealth/holdings/[symbol]/+page.svelte` | No change (already exists) |

**Existing link to fix (REQ-WD-005):** The existing link at `/wealth/+page.svelte:1196`:
```svelte
<a href="/wealth/transactions?view=realized-gl" ...>Lots, wash-sale checks, 1099-B</a>
```
MUST be changed to:
```svelte
<a href="/wealth/realized" ...>Lots, wash-sale checks, 1099-B</a>
```

**Div-to-table conversions required by REQ-WD-007** (every `<table>` must have `<th>` headings):
- **Accounts summary section** (`+page.svelte:~1073`): Currently a div-based list (`.accounts-list`, `.a-row`). Convert to `<table>` with `<thead><tr><th>` for columns: Broker, Account Name, Type, Balance. Row-click routes to `/wealth/accounts/<account_id>` (preserves existing per-account navigation). Header-click routes to `/wealth/accounts?sort=<col>&dir=<dir>`. These are two distinct navigation targets — the SortableTable `routeOnClick` prop handles row navigation; header sorting uses the standard column-click handler.
- **Top Holdings section**: Replaced by `<TopHoldingsTable>` which uses `<SortableTable>` — this conversion is covered.
- **Recent Activity section** (`+page.svelte:~1160`): Currently `<table class='data-table compact'>` with `<tbody>` but no `<thead>`. Add `<thead><tr><th>` for columns: **Date, Broker, Action, Symbol, Qty, Amount** (these exact six column headers per §9 Phase E reference). Test: assert every `<table>` in the rendered `/wealth` page has a `<thead>` with at least one `<th>`.

**Link audit (REQ-WD-005 — updated scope):**

The link audit is an integration test (Miniflare-based, not a pure unit test) located in **`tests/integration/wealth-link-audit.test.ts`** (not `tests/unit/` — this test makes HTTP round-trips via Miniflare and belongs in a dedicated integration directory):

1. **Static hrefs:** Iterates every `<a href>` in the rendered HTML of `/wealth/+page.svelte` (with a Miniflare-injected test auth cookie). Asserts each resolves to HTTP 200.

2. **Link target assertion:** Explicitly asserts two things:
   - The "Lots, wash-sale checks, 1099-B" link resolves to `/wealth/realized` (HTTP 200).
   - **The old URL `/wealth/transactions?view=realized-gl` is NOT present anywhere in the rendered HTML.** (Negative assertion — the test FAILS if the old URL string appears in the page source.)

3. **Programmatic navigations:** The test also enumerates these known dynamic/programmatic navigations and asserts HTTP 200 with a test-fixture account ID and symbol:
   - `/wealth/accounts/<fixture_account_id>` (from `href={'/wealth/accounts/${a.account_id}'}`)
   - `/wealth/holdings/<fixture_symbol>` (from symbol cell links)
   - `/wealth/realized?year=2026`

4. **Auth:** Test uses a Miniflare-injected session cookie for `sparkst@gmail.com` (the WEALTH_ALLOWED_EMAILS user). The test must NOT trivially pass as 302 — auth must be real.

---

## 6. Cross-cutting concerns

### 6.1 Decimal precision (REQ-WC-004 enforcement)

Every endpoint serializes monetary values as JSON strings. `quantize_balance` helper (from REQ-WC-004 implementation) is reused. Client never calls `Number()` or `parseFloat()` on monetary values — only `Intl.NumberFormat` with a `Decimal.js`-backed parse for display. Specific serialization methods:
- Monetary (scale 2): `new Decimal(val).toFixed(2)`
- Quantities (scale 8): `new Decimal(val).toFixed(8)`
- Percent change (scale 4): `new Decimal(val).toFixed(4)` — always exactly 4 decimal places

**Phase B migration note:** `wealth-api.ts:fetchRealizedGL()` (calls `/realized-gl`) is marked `@deprecated` in Phase B. The existing `/realized-gl` route uses `parseFloat()` (a REQ-WC-004 violation) — this violation is documented and flagged for fix in the same Phase B sprint. The home-page realized cards are migrated to call `realized-gains?year=<YYYY>` three times instead.

### 6.2 Auth (REQ-WC-002, REQ-WC-019)

All new browser-facing routes use `requireWealthAccess(event)`. `repriced-today` is browser-facing (called by `<RefreshingBanner>` on page load) — it uses the **cookie guard only**. `X-Internal-Key` is NOT checked on `repriced-today`.

**Resolution of REQ-WD-008 requirement text contradiction:** The requirements text at `current.md:264` says "route enforces WEALTH_ALLOWED_EMAILS + reuses WEALTH_INTERNAL_KEY check pattern from existing internal routes." The `WEALTH_INTERNAL_KEY` clause is a copy-paste artifact — browser clients cannot send `X-Internal-Key` without exposing it publicly. The correct auth is WEALTH_ALLOWED_EMAILS cookie guard only. The requirements text is corrected in `requirements/current.md` (see fix applied there). No new secrets required.

### 6.3 CPU budget (REQ-WC-017)

- **`realized-gains?year=`:** Row-fetch + Decimal.js aggregation in TypeScript. Single D1 scan on `closed_date` indexed range. D1 latency ~1-5ms + TS aggregation <2ms for typical corpus (≤5000 rows/year). Total: <10ms. ✅

- **`repriced-today`:** `REPRICED_TODAY_BATCH_SIZE = 3` symbols per invocation, fetched **sequentially** with `REQUEST_INTERVAL_MS = 7500ms` gaps. Wall-clock estimates:
  - **Happy path (no retries):** 3 × ~500ms fetch + 2 × 7500ms gaps ≈ 16.5s. Within 30s Workers wall-clock limit. ✅
  - **Worst-case with retries (2 attempts at 200ms/600ms per symbol):** Per symbol: 200ms + 600ms backoff + ~500ms fetch ≈ 1.3s retry overhead. Total: 3 × 1.3s + 2 × 7500ms = 18.9s. Within 30s limit with margin. ✅
  - **Hard 25s cap:** If `Date.now() - startMs > 25_000`, abort remaining symbols early. This provides a 5s safety margin before the 30s Workers wall-clock termination.
  
  **Rationale for sequential (not parallel):** The existing EOD cron enforces 7500ms between calls because Twelve Data free tier has an 8 req/min **sliding-window** rate limit. A burst of 3-8 parallel calls in under 1 second would trigger 429 on most of them, since the rate limit is per-minute not per-day. Sequential with 7500ms gaps keeps burst rate at 8/min exactly.

- **`networth-history` with overlays — D1 query budget (updated):** Worst case: 1 aggregate series query (full-scan of `account_balance_snapshot` + `plaid_account_balance_snapshot` — grouped in TypeScript; see §2.1) + up to 5 overlay symbol scans of `historical_price` + 3 benchmark symbol scans. Query count: 1 + 5 + 3 = 9 queries for balance/overlay/benchmark. **Note:** per-account balance filtering happens in TypeScript after the full scan — there are NO separate per-account D1 queries for balance data (see §2.1 `loadHistoryState` strategy). Use `db.batch()` for the 8 overlay/benchmark `historical_price` queries (read-only, idempotent). **`db.batch()` is safe for READ-ONLY queries because reads are idempotent — the prohibition in REQ-WC-006 applies ONLY to Plaid sync write paths where per-row error isolation is required (see `plaid-balance-sync.ts:20-21` comment).** D1 per-query latency: ~1-10ms. With batching: ~10-50ms D1 time + 10ms JSON serialization. Total estimated: ≤150ms. Measured against REQ-WC-017 ≤250ms staging threshold. **Action item:** measure actual D1 latency in staging with 10-year range and 16 accounts; if > 150ms, add covering index `idx_hp_symbol_trade_date_desc`.

- **Payload cap (updated — simpler invariant):** Always downsample per-account balance series older than 3 years when `range=all` AND the request includes `accounts` with more than 5 account IDs. Aggregation method: **use the last trading day of each calendar week (Friday's balance)** as the weekly data point, with the Friday date as the x-axis label. ISO week — if Friday has no data, use Thursday, then Wednesday (first available day ≤ Friday). This reduces a 10y daily series to ~3y daily + 7y weekly ≈ 1459 points per series. The response includes `"downsampled_before": "<date>"` when downsampling is applied (see §3.1). This is NOT transparent to the client — the field signals the data density change.

### 6.4 Concurrency / coalescing on repriced-today

**Corrected design — lock acquired first, deleted on completion:**

1. On entry: acquire KV lock `repriced:lock:<sha256(email)>` (full 64 hex chars). If lock already set → return HTTP 202 `{status: 'in_progress'}` immediately. No polling inside the Worker.
2. If not locked: set lock with `expirationTtl: 60` (KV enforces a 60-second minimum TTL — see Cloudflare KV docs). The lock VALUE is the epoch-ms acquisition timestamp (e.g. `"1715640000000"`), not `'1'`. This allows the application layer to enforce a 30-second business rule: on lock-read, parse the timestamp, and if `now() - timestamp < 30000ms` treat as in_progress; if elapsed ≥ 30s the lock is considered stale and the new request may overwrite it. KV TTL = 60s serves as safety cleanup only. Proceed with budget pre-check and fetch.
3. **On successful completion:** DELETE the KV lock key immediately (do not wait for TTL). This allows the client's 5s re-poll to succeed: the second call arrives at T+5s; the lock was deleted at T+16.5s (after the Worker completed), so the second call proceeds with the next batch.
4. **Budget exhaustion (graceful exit):** DELETE the KV lock before returning the `quota_exhausted` HTTP 200 response. Budget exhaustion is NOT an error — no cooldown is needed. The client's next poll should be allowed to proceed immediately (it will re-check the budget and return `quota_exhausted` again if still exhausted, or proceed if budget has reset at UTC midnight).
5. **On error** (Twelve Data failure, D1 write failure): Do NOT delete the lock — let the 60s KV TTL serve as a safety cleanup after failures. The application-layer 30s business rule acts as the effective cooldown: any retry arriving within 30s of the failed acquisition timestamp sees 202 in_progress. After 30s, the lock is treated as stale (overwritable). After 60s, KV itself cleans up. This prevents tight retry loops on persistent errors while bounding the maximum stall to 60s.
6. Client banner: on 202 response, wait `CLIENT_RETRY_MS` (5s from `src/lib/wealth-constants.ts`), then re-issue the POST.

Rate-limit clock skew (WEALTH_KV TTLs are eventually consistent): acceptable for a 60s cleanup TTL + 30s application-layer business rule. The timestamp-in-value approach is immune to KV TTL drift. Documented in the route comment.

### 6.5 Security / abuse

- **WEALTH_KV binding in Pages project — corrected (R2RD2017):** The existing `quotes` route (`REQ-WC-013a`) does NOT use WEALTH_KV — its 15-min cache is implemented via `live_quote.fetched_at` in D1 (verified in `quotes/+server.ts`: zero references to `platform.env.WEALTH_KV`). There is currently no Pages route that validates WEALTH_KV availability in production. Therefore, the "working in production" justification from v2 is not valid evidence.

  **Action required before implementation:** The `[[kv_namespaces]]` binding for WEALTH_KV **MUST** be added to `wrangler.toml` for local dev + staging parity. Additionally, verify in the Cloudflare Pages dashboard that the production deployment has a WEALTH_KV binding with ID `a66a7e6988ad45ab84aa1cfc4301587c` (from `wrangler.worker.toml`). The `repriced-today` route includes a null-guard for `platform.env.WEALTH_KV` that fails-open (see §3.3 step 4 — logs warning, proceeds without lock).

  `wrangler.toml` addition — **already applied** (see `wrangler.toml` in repo). The key corrections vs. the original v2 snippet:
  - `preview_id` is the **staging** KV namespace ID (`592d46c8...`), NOT the production ID.
  - An `[[env.preview.kv_namespaces]]` block is added under `[env.preview]` for the staging env.
  ```toml
  [[kv_namespaces]]
  binding = "WEALTH_KV"
  id = "a66a7e6988ad45ab84aa1cfc4301587c"
  preview_id = "592d46c827a94e6199a54a5965ee55b8"

  # Under [env.preview]:
  [[env.preview.kv_namespaces]]
  binding = "WEALTH_KV"
  id = "592d46c827a94e6199a54a5965ee55b8"
  ```

- `repriced-today` is rate-limited 1 invocation per 30s per email-hash (effectively global for this single-user system). Lock key: `repriced:lock:<full-64-char-sha256(email)>` — no truncation, eliminating any hash collision risk.
- All inputs validated against enum/schema allowlist before D1. No string interpolation into SQL.
- `overlay_symbols` validated against `/^[A-Z0-9.^]{1,12}$/` (same regex as `quotes/+server.ts:23`). Invalid symbol → 400 `{error_code:'invalid_symbol'}`.
- `accounts` values validated as UUID format. Invalid → 400 `{error_code:'invalid_account_id'}`.
- `overlay_symbols` and `accounts` query params are bounded: max 16 accounts, max 5 overlay symbols, max 3 benchmarks. Hard 400 rejection above those limits. UI imposes a softer max-8-series readability limit.
- Sort column allowlists enforced per-route (see §3.4). Invalid sort → silent default fallback.
- Audit trail: every `repriced-today` call writes one `audit_events` row and one `ingestion_log` row.

### 6.6 Observability

Structured log format for all new endpoints — emit JSON objects, not bare strings:

```ts
// Use this pattern in all new endpoints
console.log(JSON.stringify({
  endpoint: 'repriced-today',
  step: 'fetch-complete',
  symbol_count: toRefresh.length,
  refreshed,
  skipped,
  errors,
  elapsed_ms: Date.now() - startMs,
}));
console.error(JSON.stringify({
  endpoint: 'repriced-today',
  step: 'symbol-fetch-error',
  symbol: sym,  // safe — symbol is a validated ticker, not PII
  error_code: resp.status,
  elapsed_ms: Date.now() - startMs,
}));
// API key NEVER appears in logs — use sanitizeUrl() pattern from quotes/+server.ts
```

- Sentry breadcrumb per endpoint hit with `range`, `account_count`, `overlay_count`, `refreshed`, `skipped`, `errors`.
- Workers analytics counters: `wd_networth_history_calls`, `wd_realized_calls`, `wd_repriced_today_calls`, `wd_repriced_today_errors`.
- Banner-error path logs `error_code` so frontend telemetry differentiates upstream-unavailable vs. quota-exhausted.

### 6.7 Named constants

**Two constant files — server-only vs. shared:**

`src/lib/server/wealth/wd-constants.ts` — server-only (never imported client-side):
```ts
export const DAILY_BUDGET = 600;                    // Twelve Data calls/day (shared cap)
export const REPRICED_TODAY_BATCH_SIZE = 3;         // Max symbols per repriced-today invocation
export const REQUEST_INTERVAL_MS = 7500;            // Math.ceil(60_000 / 8) — Twelve Data 8 req/min sliding window
export const STALENESS_MARKET_OPEN_MS = 5 * 60_000; // 5 min (in market hours)
export const STALENESS_OFF_HOURS_MS = 24 * 3_600_000; // 24 h (outside market hours)
// Note: RATE_LIMIT_TTL_S removed — KV expirationTtl is hardcoded to 60 (KV
// minimum). The 30s business rule is enforced via the timestamp stored in the
// lock value (application layer), not via KV TTL. See §6.4 step 2.
export const MAX_ACCOUNT_OVERLAYS = 16;
export const MAX_SYMBOL_OVERLAYS = 5;
export const MAX_BENCHMARKS = 3;
export const CACHE_TTL_MS = 15 * 60_000;            // live_quote cache TTL
export const PAYLOAD_THRESHOLD_ACCOUNTS = 5;        // Downsample trigger: accounts > this value
```

`src/lib/wealth-constants.ts` — shared (importable by client and server):
```ts
export const CLIENT_RETRY_MS = 5_000;               // Client retry after 202
export const MAX_VISIBLE_SERIES = 8;                // UI readability limit
export const PAYLOAD_THRESHOLD_BYTES = 2_000_000;   // Informational ONLY — NOT the downsample trigger. The operative trigger is accounts-param length > PAYLOAD_THRESHOLD_ACCOUNTS (see §6.3); this byte figure has no implementation role.
export const CHART_LOADING_TIMEOUT_MS = 10_000;     // Skeleton timeout before error state (client-side)
```

`CHART_LOADING_TIMEOUT_MS` is in the **shared** `wealth-constants.ts` (not server-only `wd-constants.ts`) because it is consumed by the client-side `<NetWorthChart>` / `<RefreshingBanner>` components. **SvelteKit `$lib/server/` enforcement:** `wd-constants.ts` lives at `src/lib/server/wealth/wd-constants.ts`. The `$lib/server/` path IS enforced by SvelteKit at build time — any import from `$lib/server/` in a client component (`.svelte` file or client-side `.ts`) causes a SvelteKit build error (`"Cannot import server-only module"`). This is the actual mechanism preventing accidental client-side import — not just documentation convention. `wealth-constants.ts` (at `src/lib/wealth-constants.ts`) is outside `$lib/server/` and is therefore importable by both client and server code.

**ESLint no-restricted-syntax enforcement for constant migration:** To enforce that local `DAILY_BUDGET` declarations are removed and replaced with imports from `wd-constants.ts`, add this rule to `eslint.config.js`:

```js
// In eslint.config.js, within the rules for src/lib/server/wealth/ and src/routes/
{
  rules: {
    'no-restricted-syntax': [
      'error',
      {
        selector: "VariableDeclaration > VariableDeclarator[id.name='DAILY_BUDGET']",
        message: "Do not declare DAILY_BUDGET locally. Import from $lib/server/wealth/wd-constants.ts"
      }
    ]
  }
}
```

This rule is added in Phase A alongside the constant migration step. Running `pnpm run lint` after Phase A will fail if any file still declares `const DAILY_BUDGET` locally.

**`MAX_RETRIES` constant:** Add to `src/lib/wealth-constants.ts`:
```ts
export const MAX_RETRIES = 6;  // Max banner poll attempts (6 × 5s = 30s)
```

**Migration of existing local constants (R3RD2002):** The following files currently define `DAILY_BUDGET` as local constants — these MUST be removed and replaced with import from `wd-constants.ts` in Phase A:
- `src/lib/server/wealth/twelve-data-ingest.ts` — `const DAILY_BUDGET = 600;` and `const RATE_LIMIT_PER_MINUTE = 8; const REQUEST_INTERVAL_MS = Math.ceil(60_000 / RATE_LIMIT_PER_MINUTE);`
- `src/routes/(wealth)/wealth/api/brokerage/quotes/+server.ts` — `const DAILY_BUDGET = 600;`
- `src/routes/(wealth)/wealth/api/internal/prices/backfill/+server.ts` — `const DAILY_BUDGET = 600;`

Similarly, `CACHE_TTL_MS` in `quotes/+server.ts` and `REQUEST_INTERVAL_MS` in `twelve-data-ingest.ts` are replaced by imports from `wd-constants.ts`. Remove the local `RATE_LIMIT_PER_MINUTE` variable from `twelve-data-ingest.ts` after migration — it is superseded by the named `REQUEST_INTERVAL_MS` constant with an inline derivation comment.

Tests import thresholds from these files — no magic numbers in test files.

---

## 7. Tests (TDD per CLAUDE.md)

**TDD ordering: every numbered step below means (a) write failing tests first, then (b) implement until tests pass. No implementation step is started without a corresponding failing test.**

**e2e approach (corrected):** `sparkry-crm` has no Playwright config or dependency. All tests use **Vitest + Miniflare** (matching the existing test harness in `tests/unit/`). Tests that require HTTP round-trips use Miniflare's `fetch()` against a locally-bound Pages function environment. The `tests/integration/` directory is used for Miniflare round-trip tests; `tests/unit/` is for pure unit tests.

**Coverage targets and enforcement (R3RD2006):** ≥90% line coverage on pure TypeScript modules (endpoints, store, `chart-geometry.ts`, `wd-constants.ts`). ≥60% line coverage on Svelte component files. Add coverage block to `vite.config.ts` (see §9 Phase A). Add `@vitest/coverage-v8` to `package.json` devDependencies.

| REQ | Test type | Location | Key test cases |
|---|---|---|---|
| WD-001 | API unit + component | `tests/unit/networth-history-range.test.ts`, `tests/unit/wealth-chart-store.test.ts`, `tests/unit/range-helpers.test.ts` | `range=1w` → 7 days; `range=custom&start=2025-01-01&end=2025-03-01` → bounded; `range=bad` → 400; `start>end` → 400; `end>today` → clamped; default `range` (no param) → all-time; sessionStorage default on first visit; sessionStorage write on range change; custom range validation (start ≤ end); clear resets to defaults; **range-helpers.test.ts (all 11 slugs):** 1w→7d, 2w→14d, 1mo→~30d, 3mo→~91d, ytd→Jan-1-to-today, 1y→365d, 3y→~1095d, 5y→~1825d, 10y→~3650d, all→full history, custom→bounded by start/end; edge: ytd on Jan 1 (span=1d); edge: all with no data (empty range); edge: custom with start=end (1d); `downsampled_before` field present when >5 accounts + range=all; weekly-spaced dates when downsampled; daily dates within 3y window |
| WD-002 | API + component | `tests/unit/networth-history-per-account.test.ts` | 1 account param → per-account series in response; 16 accounts → all series present; >16 accounts → 400; legend toggle = account deselect; **correctness test** (not timing): 16 accounts × 3650 rows via mock D1 → assert all series returned with correct date range and no missing points; **downsample test:** >5 accounts + range=all → `downsampled_before` populated + series older than 3y are Friday-spaced (not daily); ≤5 accounts + range=all → no downsampling |
| WD-003 | API + component | `tests/unit/networth-history-benchmarks.test.ts`, `tests/unit/networth-history-today-aug.test.ts`, `tests/unit/market-hours.test.ts` | SPY/QQQ/VTI valid benchmarks; `^GSPC` → 400; `benchmarks=BND` → 400 `{error_code:'invalid_benchmark'}`; `pct_change[0] == '0.0000'` for all series; range change → fresh pct_change anchor; zero-anchor guard: anchor close='0.00000000' → empty series returned, no 500; weekend in range → Saturday/Sunday benchmark equals Friday value (forward-fill); **US holiday forward-fill:** range spanning a holiday → holiday row present with prior trading day's close; **weekend anchor:** range starting Saturday → Saturday row present with Friday close; pct_change[Saturday]='0.0000'; cash-only account contributes plaid balance to today-point; **credit/loan sign — canonical discriminator (P1-R1RD5001):** Plaid checking account (`plaid_account_type='depository'`, `current_balance='10000.00'`) → today-point INCREASED by 10,000; Plaid credit card (`plaid_account_type='credit'`, `current_balance='5000.00'`) → today-point REDUCED by 5,000; Plaid mortgage (`plaid_account_type='loan'`, `current_balance='200000.00'`) → today-point REDUCED by 200,000; **assert discriminator uses `plaid_account_type`, not `account.account_type`** (mock an account where account_type is 'investment' but plaid_account_type is 'credit' → balance is still negated); error fallback → today-point still rendered using networth endpoint; **tooltip no-Live:** render NetWorthChart with today-point active → tooltip does NOT contain string "Live"; tooltip contains "Prices as of" with a time value; **account with zero balance rows:** account_id valid UUID but no rows → series:[] in response (not omitted); **Plaid scale normalization (P1-R1RD5004):** `plaid_account_balance_snapshot` row with `current_balance='1234.5678'` → merged series row value is `'1234.57'` (ROUND_HALF_UP to scale 2); XLSX `account_balance_snapshot` row with `balance='1234.57'` on same account+date → Plaid row wins and value is `'1234.57'` (no duplicate); **investment-account today-point exclusion / no double-count (L2-TRC-001/L2-FIN-003):** account with ≥1 `position_snapshot` row AND a today-dated `plaid_account_balance_snapshot` row AND a repriced positions×price value → today-dated Plaid row is EXCLUDED from the merge; today-point = positions×price only; assert today-point net worth equals the repriced value alone (NOT repriced + Plaid snapshot); same account's pre-today Plaid rows still follow Plaid-wins; **market-hours.test.ts:** market-open at 09:31 ET Mon-Fri returns true; pre-open 09:29 returns false; post-close 16:01 returns false; weekend returns false; **DST spring-forward Sunday at 09:31 ET:** returns false (weekend); **DST fall-back Sunday at 09:31 ET:** returns false (weekend); isMarketOpen uses America/New_York (DST-aware, not fixed offset) |
| WD-004 | API + component | `tests/unit/realized-gains.test.ts` | 3 rows with proceeds `10.10` each → net `"30.30"` (not `"30.29..."`); NULL-term lot → `unknown != "0.00"`, `st+lt+unknown == net` (recompute path); wash_sale=true lot → `wash_sale_count=1`, `total_disallowed_loss` correct; zero-cost-basis lot with unadjusted_cost_basis → coverage_warning present (Decimal.js isZero() check); `cost_basis='0'` (not '0.00') also triggers coverage_warning; empty year → all zeros, no coverage_warning; **coverage warning per-year independent:** mock N-1 year with warning, N and N-2 without → assert ⚠ only on N-1 card; **broker-stored lt_gain_loss:** row with lt_gain_loss='100.00' and proceeds-cost='99.99' → lt bucket uses '100.00' (not recomputed); **mixed NULL/non-NULL lt_gain_loss (P1-R1RD5003):** two rows same year — one lt_gain_loss non-NULL, one lt_gain_loss NULL → `coverage_warnings` contains an entry with `scope: 'year'` and message indicating mixed source; assert entry does NOT have `account_id` or `broker` fields; **future year** (year=currentYear+1) → 400 `{error_code:'invalid_year'}`; **year-boundary:** lot with closed_date='2026-12-31' in year=2026; lot with closed_date='2027-01-01' NOT in year=2026; **coverage warning aria-label present** in DOM when warnings>0; **coverage warning icon keyboard-focusable:** tabindex='0' present on ⚠ span |
| WD-005 | Integration (Miniflare) | `tests/integration/wealth-link-audit.test.ts` | All static `<a href>` in /wealth return 200 with test cookie; "Lots, wash-sale checks" link resolves to `/wealth/realized`; **negative assertion: `/wealth/transactions?view=realized-gl` string NOT present anywhere in rendered HTML**; `/wealth/accounts/<fixture_id>` → 200; `/wealth/holdings/<fixture_symbol>` → 200; `/wealth/realized?year=2026` → 200; every `<table>` in /wealth has `<thead>` with ≥1 `<th>` |
| WD-006 | Unit + component | `tests/unit/top-holdings-pricing.test.ts` | `prior_close=null` → `day_delta_pct=null` → em-dash; `prior_close='0.00000000'` → `day_delta_pct=null`; `quantity=null` → `market_value=null`, `day_delta_pct=null` (no Decimal constructor throw); market_value with quantity=`'0.12345678'`, price=`'100.00'` → `'12.35'`; `pct_change` sign correct for up/down days; `▲` present in DOM with aria-hidden='true' for positive Δ%; sr-only 'increased' text present for positive; `▼` present in DOM with aria-hidden='true' for negative Δ%; sr-only 'decreased' text present for negative Δ%; **zero delta:** dayDeltaPct='0.0000' → no `▲` or `▼` in DOM, no 'increased'/'decreased' sr-only text; symbol cell has `<a href='/wealth/holdings/{symbol}'>` inside `<td>` |
| WD-007 | Component | `tests/unit/sortable-table.test.ts` | Decimal column `['100.00','9.00','20.00']` sorts ascending to `['9.00','20.00','100.00']`; `keydown Enter` triggers sort cycle; `keydown Space` triggers sort cycle; **Space keydown calls preventDefault** (no page scroll); `aria-sort` updates to reflect current sort; **three-state cycle:** first click → aria-sort='ascending'; second click → aria-sort='descending'; **third click → aria-sort='none', rows in original load order**; `aria-label` for none-state = "Sort by {col} ascending"; `aria-label` for ascending-state = "Sort by {col} — click to sort descending"; `aria-label` for descending-state = "Sort by {col} — click to remove sort"; **no role='button' on sortable th** (implicit columnheader role only); `sortable:false` column has no tabindex, no aria-sort; `?sort='; DROP TABLE...;--'` → silent default fallback; `?dir='; DROP TABLE--'` → silent 'asc' fallback; integer column null/empty values sort to end; wash-sale badge has role='img' and aria-label containing 'Wash sale'; **REPRICED_TODAY_BATCH_SIZE constant:** imported from wd-constants.ts and equals 3; **empty state:** when rows=[] and emptyMessage provided → single `<td colspan=9>` rendered inside `<tbody>` |
| WD-008 | Endpoint + component | `tests/unit/repriced-today.test.ts` | Empty position_snapshot → `{refreshed:0, skipped:0, errors:0, latest_as_of:null, stale_symbols:[], error_code:null}`; budget pre-check binding-constraint cases (L2-CON-002): SUM(records_processed)=597 (budgetRemaining=3) → exactly 3 symbols fetched; SUM(records_processed)=595 (budgetRemaining=5) → still exactly 3 fetched (`REPRICED_TODAY_BATCH_SIZE`=3 is the binding cap, NOT budgetRemaining); SUM(records_processed)=599 (budgetRemaining=1) → exactly 1 fetched (budget is binding); all using SUM not COUNT(*)=rows; **quota_exhausted full shape:** HTTP 200 `{refreshed:0, skipped:N, errors:0, latest_as_of:null, stale_symbols:[...symbols], error_code:'quota_exhausted'}` — stale_symbols always present; **quota_exhausted → NO IngestionLog row written** (IngestionLog written only after budget check passes); D1 upsert failure for one symbol → errors++, batch continues; **concurrent lock → HTTP 202 `{status: 'in_progress'}`** — assert response.status===202 AND body is exactly `{status:'in_progress'}` (no refreshed/stale_symbols fields); WEALTH_KV unavailable → proceeds without lock, no crash; IngestionLog row written AFTER budget check with status='in_progress'; IngestionLog updated to 'success' after (via updateIngestionLog); audit_events row written with entity_id=UUID, entity_type='repriced_today_run'; `live_quote` updated (not `historical_price`); KV lock deleted on success via kvAvailable guard (not just TTL expiry); **budget exhaustion → KV lock deleted** (graceful exit, not error); close??price field mapping: mock Twelve Data returning only 'close' → price extracted correctly; mock returning only 'price' → also extracted; `prior_close=0` guard; **idempotency:** second call immediately after successful first call → all symbols fresh → `{refreshed:0, skipped:N, errors:0, stale_symbols:[]}` → no Twelve Data calls, **no new IngestionLog row written** (NOT "or row with records_processed=0" — exactly zero rows); **staleness sort:** two stale symbols with different fetched_at → older symbol appears in toRefresh batch first; **25s wall-clock cap:** mock elapsed > 25s → abort remaining symbols, return canonical 200 with counts AND **KV lock deleted** (not left for TTL — L2-R2C-001); **batched-poll continuation / step 3a (L2-FIN-002/L2-R2C-003):** HTTP 200 `error_code:null` with `stale_symbols=['MSFT','TSLA']` (non-empty) → banner stays `refreshing` (NOT idle); `onRefreshed` called once (partial batch re-renders); `pollCount` incremented; waits `CLIENT_RETRY_MS` then re-POSTs; a 7th consecutive non-empty-`stale_symbols`/202 poll (pollCount ≥ MAX_RETRIES=6) → `error` state "refresh timed out"; success-path `stale_symbols` = original stale minus successfully-refreshed; **prefers-reduced-motion:** when matchMedia('prefers-reduced-motion: reduce') returns true → shimmer overlay has animation:none (or static-gray class applied instead of shimmer class); **always-present live-region DOM (P1-R4RD5004):** in idle state — `role='status'` element is present in DOM AND has empty textContent (no sr-only text); `role='alert'` element is present in DOM AND has empty textContent; in error state — `role='alert'` element is present in DOM AND has non-empty textContent (error message); `role='status'` element still present but empty; assert neither live-region element uses `display:none` (only empty-content or conditional children); **kvAvailable=false successful completion → no crash on delete attempt**; **250ms budget:** NOT a timing assertion — documented as staging-soak verified per REQ-WC-017, not unit test |
| WD-002 (Clear) | Component | `tests/unit/wealth-chart-store.test.ts` | **Clear button click → `selectedAccounts` becomes empty, no account series in API request**; **Clear button keyboard Enter → same result**; **sessionStorage `wd:nw:accounts` cleared after Clear activation**; **aggregate "Net Worth" series still present after Clear**; **focus moves to AccountMultiSelect trigger after Clear keyboard activation** |
| WD-003 (Plaid merge) | API unit | `tests/unit/networth-history-today-aug.test.ts` | **plaid merge:** a `plaid_account_balance_snapshot` row for account X on date D is included in the merged net-worth series; when both `account_balance_snapshot` and `plaid_account_balance_snapshot` have rows for the same account+date, the Plaid row balance wins (overrides XLSX seed); **scale normalization (P1-R1RD5004):** Plaid balance `'1234.5678'` → merged series value `'1234.57'` (ROUND_HALF_UP); XLSX balance `'1234.57'` same date → Plaid wins, value `'1234.57'`; **sign discriminator (P1-R1RD5001):** `plaid_account_type='credit'` → balance negated; `plaid_account_type='depository'` → balance added as-is; `plaid_account_type='loan'` → balance negated |
| REQ-WD-008 | Unit | `tests/unit/symbol-helpers.test.ts` | (`symbol-helpers` feeds `repriced-today`, REQ-WD-008 — not the range selector REQ-WD-001; L2-CON-007) `getSymbolsToFetch` returns distinct non-null symbols from position_snapshot; `isCashOrSuspect` correctly classifies 'CASH' (true), 'TOTAL' (true), 'AAPL' (false), '' (true); null symbol rows excluded from result; WEALTH_KV cache hit returns symbols without D1 query; KV read failure falls back to D1 query silently (no throw); `getSymbolsToFetch` result contains no duplicates even when multiple accounts hold the same symbol |
| — | Unit | `tests/unit/chart-geometry.test.ts` | `yFor(balance, range, chartH)` correct for min/max/mid values; `yForPct(pct)` correct; secondary axis min/max computed from series extrema; empty series → no NaN in path output; path output is deterministic given same input |

---

## 8. Resolved decisions (formerly "open questions")

The following were open questions in v1, now resolved:

1. **§8.1 Chart library:** Hand-rolled SVG retained. Secondary y-axis geometry extracted to `chart-geometry.ts` (pure TS, unit-testable). No library migration.

2. **§8.2 Payload cap:** When `range=all` AND `accounts` param contains >5 account IDs, downsample per-account series older than 3 years: use last trading day of each calendar week (Friday, or nearest prior day if no data). Non-downsampled (≤5 accounts, or range≠all) remains daily. `downsampled_before` field in response.

3. **§8.3 Wash-sale flag UX:** Amber "W" badge (`role='img'` `aria-label='Wash sale: loss disallowed'`) in non-sortable column. Focus-accessible tooltip: "Wash sale: loss disallowed. See IRS Pub 550." Em-dash when `wash_sale = false`.

4. **§8.4 Today's chart-point label:** Today's date (matching x-axis format). Tooltip: "Prices as of HH:MM [local time] (intra-day snapshot)." No "Live" label.

5. **§8.5 Benchmark forward-fill:** Forward-fill from most recent prior close for non-trading days (weekends + US holidays). Every date in the requested range has a row. No gaps.

6. **§8.6 `/wealth/realized` empty corpus:** Page shows `$0.00, 0 lots, "No realized transactions in <year>"` — not an error state.

7. **§8.7 Rate-limit clock skew:** Acceptable for 30s backstop TTL. Lock deleted on success. Documented in route comment.

8. **§8.8 Benchmark symbols:** `{SPY, QQQ, VTI}` — existing REQ-WC-013 allowlist ETFs. BND is in the EOD data allowlist but NOT exposed in the benchmark UI toggle (§3.1). Labels: "S&P 500 (SPY)", "NASDAQ (QQQ)", "Total Market (VTI)".

9. **§8.9 getDailyTwelveDataCount semantics:** Uses `SUM(records_processed)` not `COUNT(*)`. Counts actual Twelve Data API calls, not invocation rows. All callers (EOD cron, quotes route, repriced-today) write `records_processed = <number_of_symbols_fetched>`.

10. **§8.10 Twelve Data field mapping:** Use `data.close ?? data.price` — the `/quote` endpoint returns `close` for current price (confirmed by the `close ?? price` comment in the `sanitizeUrl` pattern of `quotes/+server.ts` and in the `twelve-data-ingest.ts` fetch loop). Never use `price` alone.

11. **§8.11 Fetch strategy:** Sequential with 7500ms gaps, REPRICED_TODAY_BATCH_SIZE=3. Rationale: Twelve Data 8 req/min is a sliding window; parallel bursts trigger 429. Sequential matches EOD cron behavior.

12. **§8.12 realized-gains term breakdown:** Use broker-stored `lt_gain_loss`/`st_gain_loss` columns when non-NULL (broker-computed, may include wash-sale adjustments). Fall back to `proceeds - cost_basis` only when NULL. The `net` is always `SUM(proceeds) - SUM(cost_basis)` (recomputed, not from `gain_loss` column).

13. **§8.13 /realized-gl coexistence:** Kept with `@deprecated` comment; `parseFloat` float violation flagged for fix in Phase B sprint. `fetchRealizedGL()` in `wealth-api.ts` marked `@deprecated`.

---

## 9. Rollout (TDD-first order)

For each phase below: **(a) write failing tests → (b) implement until tests pass → (c) run full test suite before proceeding.**

1. **Setup:** Create `src/lib/components/wealth/` directory. Create `src/lib/server/wealth/wd-constants.ts` and `src/lib/wealth-constants.ts`. Create `tests/integration/` directory (add `.gitkeep` or first test stub so the directory is tracked in git). Add `[[kv_namespaces]]` for WEALTH_KV to `wrangler.toml` (already applied — verify in CF Pages dashboard). Add `@vitest/coverage-v8` to `package.json` devDependencies (already present). Add coverage block to `vite.config.ts`. **Coverage include stub note:** `vite.config.ts` coverage `include` lists file paths for modules that do not yet exist. Vitest silently skips include patterns for non-existent files — running coverage before Phase A creates a false-green (0 files covered trivially satisfies any threshold). To prevent false-green: immediately after creating stub files in Phase A, run `pnpm run test --coverage` and verify it fails (0% coverage) before implementing. This confirms the include list is active.

2. **Phase A — Foundation (unblocks everything):**

   **Ordering constraint (TDD exemption for pure refactors):** The extraction steps (1)–(3) below are pure refactors — no new behavior, only file-location changes. The TDD exemption applies: create the new module first (so imports resolve), then write the failing test importing from the new location, then implement/verify, then update old callers. Do NOT write tests against the old location before moving code. This is the only scenario where the CLAUDE.md "failing test first" mandate is suspended. The mandate applies in full to all NEW behavior (steps 4 onward). Add explicit note to any PR: "Steps (1)–(3) are pure refactors; TDD applied from step (4) onward."

   - (1) Extract `getSymbolsToFetch` **and `isCashOrSuspect`** from `twelve-data-ingest.ts` to `src/lib/server/wealth/symbol-helpers.ts`. Both must be exported. When implementing `isCashOrSuspect` in `symbol-helpers.ts`, **reuse `isCashSymbol` and `isSuspectSymbol` from `brokerage-summary.ts`** (import them) rather than re-implementing with a new `Set(['CASH','TOTAL'])`. This fixes the existing duplication and the per-call allocation inefficiency. Update `twelve-data-ingest.ts` to import both from `symbol-helpers.ts`.
   - (2) Extract `isMarketOpen` from `quotes/+server.ts` to `src/lib/server/wealth/market-hours.ts`; update `quotes/+server.ts` to import from there.
   - (3) Migrate `DAILY_BUDGET`, `CACHE_TTL_MS`, `REQUEST_INTERVAL_MS` (and remove local `RATE_LIMIT_PER_MINUTE`) from `twelve-data-ingest.ts`, `quotes/+server.ts`, `backfill/+server.ts` to import from `wd-constants.ts`. Remove local declarations. Extract `sleep()` from `twelve-data-ingest.ts` to a shared utility (e.g., `src/lib/server/wealth/utils.ts`) and export it — `repriced-today` will need it in Phase C. Add `ESLint no-restricted-syntax` rule for `const DAILY_BUDGET` outside `wd-constants.ts` to `eslint.config.js` (see §6.7).
   - (4) Write failing tests (after (1)–(3) create the new modules): `tests/unit/sortable-table.test.ts` (WD-007), `tests/unit/wealth-chart-store.test.ts` (WD-001 store), `tests/unit/chart-geometry.test.ts`, `tests/unit/market-hours.test.ts`, `tests/unit/range-helpers.test.ts`, `tests/unit/symbol-helpers.test.ts`. **Note:** these test files do not exist yet — they are created in this step. The tests import from the new modules created in steps (1)–(3).
   - (5) Implement `<SortableTable>` + `src/lib/wealth-chart.svelte.ts` + `chart-geometry.ts` + `range-helpers.ts` until tests pass.
   - (6) Run `pnpm run lint` — verify no new ESLint violations. The `no-restricted-syntax` rule added in step (3) will fail if any file still declares `const DAILY_BUDGET` locally.
   - (7) Run `pnpm run test --coverage` — verify coverage thresholds pass for the new modules. If coverage is 0% (all include files missing), check that stub files were created — see Setup step note.

3. **Phase B — Realized G&L (small surface, immediate user win):**
   - (a) Write failing tests: `tests/unit/realized-gains.test.ts` (WD-004), `tests/integration/wealth-link-audit.test.ts` (WD-005)
   - (b) Implement `realized-gains` endpoint + `/wealth/realized` page skeleton + fix old realized-gl link
   - (c) Mark `fetchRealizedGL()` in `wealth-api.ts` as `@deprecated`; add `@deprecated` comment to `realized-gl/+server.ts` route; flag `parseFloat` in that route as `// REQ-WC-004 violation: fix in cleanup sprint`
   - (d) Migrate home-page realized cards from `fetchRealizedGL()` to three `realized-gains?year=<YYYY>` calls

4. **Phase C — repriced-today + Top Holdings:**
   - (a) **Coverage extraction (pure TS module):** Extract all new pricing computation logic from the top-holdings extension (price selection, `prior_close` query, `day_delta_pct` computation, `market_value` computation) into `src/lib/server/wealth/top-holdings-pricing.ts` (pure TypeScript, no SvelteKit route dependency). Add this file to the `vite.config.ts` coverage include list. The route handler (`top-holdings/+server.ts`) becomes a thin wrapper calling functions from `top-holdings-pricing.ts`.
   - (b) **parseFloat fix:** In addition to the new extension, replace all `parseFloat()` calls in the existing `top-holdings/+server.ts` (lines 61-64) with `new Decimal(val).toFixed(N)` serialization (or return string values directly from `computeTopHoldings()`). This fixes the REQ-WC-004 violation in the existing production code. Flag with a code comment: `// REQ-WC-004 fix: parseFloat removed in Phase C`.
   - (c) Write failing tests: `tests/unit/repriced-today.test.ts` (WD-008), `tests/unit/top-holdings-pricing.test.ts` (WD-006)
   - (d) Implement `repriced-today` endpoint (writes to `live_quote`) + extended top-holdings endpoint using `top-holdings-pricing.ts`

5. **Phase D — Chart extensions:**
   - (a) **Coverage extraction (pure TS module):** Extract all new param-handling and series-computation logic for `networth-history` (range params, benchmark validation, pct_change computation, overlay params, downsampling, Plaid merge in `loadHistoryState`) into `src/lib/server/wealth/networth-history-extensions.ts` (pure TypeScript). Add this file to the `vite.config.ts` coverage include list. The existing `networth-history/+server.ts` route handler imports from this module.
   - (b) **Plaid balance merge:** Extend `loadHistoryState` in `db-helpers.ts` to also query `plaid_account_balance_snapshot` and merge rows into `balancesByAccount` per the column mapping and Plaid-wins dedup rule specified in §2.1. This is the Phase D obligation for REQ-WD-003.
   - (c) Write failing tests: `tests/unit/networth-history-range.test.ts` (WD-001), `tests/unit/networth-history-benchmarks.test.ts` (WD-003), `tests/unit/networth-history-today-aug.test.ts` (WD-003 — includes Plaid merge test cases), `tests/unit/networth-history-per-account.test.ts` (WD-002)
   - (d) Implement range/account/benchmark/overlay params on `networth-history` endpoint using `networth-history-extensions.ts`; implement `<NetWorthChart>` + all chart sub-components

6. **Phase E — Integration + polish:**
   - (a) Run `tests/integration/wealth-link-audit.test.ts` with auth fixtures
   - (b) Fix any broken links; convert Accounts/Recent-Activity sections to semantic tables with `<th>` (Accounts: Broker, Account Name, Type, Balance; Recent Activity: **Date, Broker, Action, Symbol, Qty, Amount**)
   - (c) Add aria attributes per §4.1–§4.5; verify OKLCH palette under protanopia, deuteranopia, and tritanopia simulation
   - (d) Verify `prefers-reduced-motion` CSS for shimmer animation

7. **Phase F — UAT + deploy:**
   - Preview deploy via CF; UAT on real D1 data; sign-off; production deploy via existing CD pipeline.

### STATE OF PLAY AT DESIGN FREEZE (pre-Phase-A checklist)

The following items are intentional execute-phase work, NOT design defects. They appear as "issues" in static analysis but are correctly deferred to their respective phases. This checklist makes the distinction explicit so reviewers do not flag them as spec gaps.

**Files that do NOT yet exist — created in their respective phases:**
- `src/lib/server/wealth/symbol-helpers.ts` — Phase A step (1)
- `src/lib/server/wealth/market-hours.ts` — Phase A step (2)
- `src/lib/server/wealth/wd-constants.ts` — Phase A Setup
- `src/lib/server/wealth/top-holdings-pricing.ts` — Phase C step (a)
- `src/lib/server/wealth/networth-history-extensions.ts` — Phase D step (a)
- `src/lib/wealth-constants.ts` — Phase A Setup
- `src/routes/(wealth)/wealth/api/brokerage/realized-gains/+server.ts` — Phase B step (b)
- `src/routes/(wealth)/wealth/api/brokerage/repriced-today/+server.ts` — Phase C step (d)
- `tests/integration/wealth-link-audit.test.ts` — Phase B step (a)
- `tests/unit/networth-history-range.test.ts`, `tests/unit/networth-history-benchmarks.test.ts`, `tests/unit/networth-history-per-account.test.ts`, `tests/unit/networth-history-today-aug.test.ts` — Phase D step (c)
- `tests/unit/realized-gains.test.ts`, `tests/unit/repriced-today.test.ts`, `tests/unit/top-holdings-pricing.test.ts`, `tests/unit/symbol-helpers.test.ts`, `tests/unit/chart-geometry.test.ts`, `tests/unit/sortable-table.test.ts`, `tests/unit/wealth-chart-store.test.ts`, `tests/unit/market-hours.test.ts`, `tests/unit/range-helpers.test.ts` — Phase A step (4) + Phase B/C/D

**ESLint rules NOT yet in `eslint.config.js`** — Phase A step (3) will add:
- `no-restricted-syntax` rule for local `DAILY_BUDGET` constant outside `wd-constants.ts`

**Constants still DUPLICATED in 3 places** — Phase A step (3) will consolidate:
- `DAILY_BUDGET = 600` in `twelve-data-ingest.ts`, `quotes/+server.ts`, `backfill/+server.ts`
- `CACHE_TTL_MS` in `quotes/+server.ts`
- `REQUEST_INTERVAL_MS` (via `RATE_LIMIT_PER_MINUTE`) in `twelve-data-ingest.ts`

**Dead link in production** — Phase E step (b) will replace:
- `/wealth/+page.svelte:1196`: `<a href="/wealth/transactions?view=realized-gl">` → will become `<a href="/wealth/realized">`

**`parseFloat` violations** — flagged for fix in their respective phases (NOT pre-Phase-A):
- `networth-history/+server.ts:222` — `parseFloat(balanceTotal.toFixed(2))` → migrate to Decimal.js string output in Phase D
- `top-holdings/+server.ts:61–63` — Phase C step (b)
- `realized-gl/+server.ts` (existing, deprecated) — flagged for cleanup sprint after all callers migrate

---

## 10. Open risks

- **Twelve Data quota during repriced-today:** Shared 600/day budget. EOD cron uses ≤80 calls/day (`REPRICED_TODAY_BATCH_SIZE`=3 symbols × number of invocations). The quotes route uses additional calls. Budget counter is now `SUM(records_processed)` — accurately tracks actual API calls. Realistic budget analysis: 600 - 80 (cron) - ~20 (quotes route typical day) = 500 remaining. At 3 calls per repriced-today invocation, that is ~166 page-load refreshes before quota exhaustion — well over expected daily usage. If quota is exhausted mid-day, the banner shows "Could not refresh prices — showing last known" which is acceptable.

- **Cost-basis lot data completeness:** REQ-WD-004 assumes `realized_gain_loss` is populated. For accounts where we have positions but no historical transactions (XLSX-seeded lots), realized G/L will undercount. Mitigation: `coverage_warnings[]` in the response and ⚠ icon on home-page cards.

- **WEALTH_KV binding gap: RESOLVED** — `[[kv_namespaces]]` and `[[env.preview.kv_namespaces]]` blocks are already present in `wrangler.toml` (see §6.5). Action remaining: verify in the Cloudflare Pages dashboard that the production deployment has the WEALTH_KV binding with ID `a66a7e6988ad45ab84aa1cfc4301587c` before the first repriced-today deploy. The Phase A Setup step to "add `[[kv_namespaces]]`" is superseded — skip it. The null-guard in `repriced-today` ensures the endpoint does not crash if the binding is somehow missing (logs warning, proceeds without lock).

- **OAuth session collisions on /wealth:** Multi-tab refresh-banner races handled by KV lock + HTTP 202 pattern. Each tab independently polls at CLIENT_RETRY_MS intervals; the lock serializes actual Twelve Data calls. Lock deleted on success means the wait is bounded by actual invocation duration (~16.5s), not the 30s TTL backstop.

- **getDailyTwelveDataCount UTC day boundary:** The budget day is UTC (derived from `new Date().toISOString().substring(0,10)`). Between midnight ET and midnight UTC (up to 5 hours depending on DST), the UTC date is already the next day — creating a new budget bucket. This means a user active at 11 PM ET might find a fresh 600-call budget even though the US trading day is ending. This is intentional and tolerable — it slightly inflates available budget near midnight ET, which is acceptable for a 600-call pool. Documented in the route comment so implementors do not "fix" it.
