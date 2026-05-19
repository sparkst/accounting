# Plaid Sync Reliability & Intra-day Tiering — Design Spec

**Date:** 2026-05-18 (rev 2 — Layer-2 qloop convergence)
**Status:** Design — feeds the TDD implementation phase
**Requirements:** REQ-PS-001..003 (`requirements/current.md`)
**Implementation repo:** `sparkry-crm` (Cloudflare Pages app + separate `sparkry-crm-cron` Worker + D1)
**Builds on:** REQ-WC-006 (Plaid daily cron), REQ-WD-003/008 (today-point), REQ-028/REQ-WC-008 (reconciliation), REQ-WC-012, REQ-021, REQ-WC-017 (≤30s Plaid-sync cron budget).

## 1. Context

Root cause (verified): the cron Worker (`wrangler.worker.toml` → `sparkry-crm-cron`, `src/worker.ts scheduled()`) is correct and declares `"7 10 * * *"`, but `ci.yml` only runs `wrangler pages deploy` — the cron Worker is never CI-deployed, so the nightly Plaid sync runs only when hand-deployed → stale/gappy net worth. Freshness tiers: **A** reliable nightly Plaid baseline (or matched `account_balance_snapshot` for manual accounts) → **B** on-login async Plaid intra-day refresh for stale Items → **C** positions×live_quote today-point (existing REQ-WD-008). **A′** baseline-drift alerting is cross-cutting (runs after every A and B write).

Schema/codebase facts this design is pinned to (verified against sparkry-crm): `audit_events` columns = `(id, entity_id, entity_type, field_changed, old_value, new_value, changed_by, changed_at, cf_scheduled_time)` — there is **no** `action`/`changed_fields` column; the existing convention (`plaid-balance-sync.ts`) is `field_changed`, `new_value=JSON`. `plaid_account_balance_snapshot` columns include `id, account_id, snapshot_date, plaid_account_type, plaid_account_subtype, current_balance, available_balance, iso_currency_code, pulled_at, raw_data` with `UNIQUE(account_id, snapshot_date)` and a DELETE-only trigger (UPDATE is allowed). Server code reaches D1 via the `WealthEnv & { WEALTH_DB?: D1Database }` pattern (`plaid-balance-sync.ts:496`): `const db = (env.WEALTH_DB ?? env.DB)`. The chart re-fetch mechanism is `chartStore.bumpRefreshCount()` (`wealth-chart.svelte.ts`) — there is no `refreshCount` component prop and no `TopHoldingsTable` component (top holdings via `fetchTopHoldings()`).

## 2. Tier A — CI deploys the cron Worker (REQ-PS-001)

### 2.1 Deploy ordering (explicit)
The existing `deploy` job order is: **(1) Apply D1 migrations** (existing step, runs first) → **(2) Deploy cron Worker** (NEW, inserted here) → **(3) Deploy Pages** (existing step). Migrations therefore always apply before the new cron-Worker code that references the new columns is live, and the cron Worker is never older than the Pages frontend. Insert, immediately before the existing `Deploy to Cloudflare Pages` step:

```yaml
      - name: Deploy cron Worker
        run: pnpm wrangler deploy --config wrangler.worker.toml
        env:
          CLOUDFLARE_API_TOKEN: ${{ secrets.CLOUDFLARE_API_TOKEN }}
          CLOUDFLARE_ACCOUNT_ID: ${{ secrets.CLOUDFLARE_ACCOUNT_ID }}
```

`run:` failure aborts the job (shell `set -e`) → Pages deploy never runs (fail-fast; prod stays fully on the prior version — intentionally non-atomic, pre-customer). `sparkry-crm-cron` secrets are provisioned out-of-band via `wrangler secret put --name sparkry-crm-cron`; CI needs `CLOUDFLARE_API_TOKEN` (already a repo secret) and `CLOUDFLARE_ACCOUNT_ID` (Cloudflare account id — confirm it is a repo secret; the Pages deploy also needs it).

### 2.2 CI-assertable smoke gate (post-deploy step in the deploy job)
A step calls `GET https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/workers/scripts/sparkry-crm-cron/schedules` (Bearer `$CLOUDFLARE_API_TOKEN`) and fails the job unless the response `result.schedules[].cron` includes `"7 10 * * *"`. Asserts the script exists + the trigger is **registered** — NOT that it has fired (a fresh deploy has no history).

### 2.3 Operational gate (runbook, NOT CI)
≥25h post-deploy, `SELECT MAX(run_at) FROM ingestion_log WHERE source='plaid-balance-sync'` must be within the last 25h (proves the cron actually fires). Runbook step; not a CI gate (inherently async).

### 2.4 Regression guard
`scripts/check-ci-deploys-cron-worker.mjs` (CI `test` job, before deploy): parse `.github/workflows/ci.yml` with a YAML parser. FAIL unless the `deploy` job contains a step whose `run` matches `/wrangler\s+deploy/` AND contains `--config wrangler.worker.toml` AND does NOT contain `pages`, the step has no `if:` key, no `continue-on-error: true`, and no `|| true` in `run`. `tests/unit/ci-cron-deploy-guard.test.ts` feeds synthesized ci.yml variants (missing step / `if: false` / `if: <expr>` / commented / `continue-on-error: true` / `|| true`) and asserts the guard throws for each, and passes for the real fixed ci.yml.

## 3. Tier B — on-login intra-day refresh (REQ-PS-002)

### 3.1 Schema (migration `0007_plaid_snapshot_fetched_at.sql`)
SQLite/D1 forbids a non-constant `DEFAULT` in `ALTER TABLE ADD COLUMN`, so use a constant sentinel:
```sql
ALTER TABLE `plaid_account_balance_snapshot` ADD COLUMN `fetched_at` INTEGER NOT NULL DEFAULT 0;
```
Pre-existing rows read back `fetched_at=0` (sentinel = "infinitely stale") → the first login refresh treats every pre-migration account as stale once, then settles. `snapshot_date` (DATE) is unchanged → REQ-WD-003 per-date net-worth merge unaffected (still one row per account per UTC day; `fetched_at` only disambiguates which intra-day fetch a same-day row holds). All new writes set `fetched_at` explicitly to epoch-ms.

Also migration `0007b_plaid_item_last_attempted.sql`:
```sql
ALTER TABLE `plaid_item` ADD COLUMN `last_attempted_at` INTEGER NOT NULL DEFAULT 0;
```
(used for the retryable-error cooldown, §3.2).

### 3.2 Route `src/routes/(wealth)/wealth/api/brokerage/refresh-plaid-balances/+server.ts`
`POST`, cookie-guarded exactly like `repriced-today` (`isWealthAllowed(locals.user?.email, parseWealthAllowedEmails(env.WEALTH_ALLOWED_EMAILS))`; no WEALTH_INTERNAL_KEY). DB via the existing pattern: `const db = (env.WEALTH_DB ?? env.DB) as D1Database` (the `WealthEnv & {WEALTH_DB?}` type per `plaid-balance-sync.ts:496` — do NOT introduce a bare `env.WEALTH_DB` that breaks typecheck).

**Eligibility** (active, not-recently-attempted, stale — intentionally NO `last_sync_status` filter; see SQL comment):
```sql
SELECT i.* FROM plaid_item i
WHERE i.status='active' AND i.access_token_encrypted != 'REVOKED'
  -- NO last_sync_status string filter: the D1 CHECK on plaid_item.last_sync_status
  -- only permits ok|error|pending|institution_down (never raw Plaid codes), so a
  -- `NOT IN ('ITEM_LOGIN_REQUIRED',...)` clause is a dead no-op. The 30-min
  -- cooldown below is the actual runaway-cost bound for ANY persistently
  -- failing Item (terminal or retryable): ≤ ~1 Plaid call / 30 min, self-heals
  -- on re-link. On a Plaid terminal error the code sets last_sync_status='error'
  -- (CHECK-valid, matches plaid-balance-sync.ts) — never the raw Plaid code.
  AND i.last_attempted_at < :nowMs - 1800000        -- 30-min per-Item cooldown
  AND NOT EXISTS (
    SELECT 1 FROM plaid_account_balance_snapshot s
    JOIN account a ON a.id = s.account_id
    WHERE a.plaid_item_id = i.id AND s.fetched_at > :nowMs - 14400000)  -- TTL 4h
ORDER BY i.last_attempted_at ASC;                    -- most-stale first (deterministic)
```
The `last_attempted_at` cooldown bounds Plaid billing for ANY persistently-failing Item — terminal OR retryable: it is re-attempted at most ~once / 30 min regardless of login frequency, and self-heals once re-linked. There is intentionally **no `last_sync_status` exclusion** (it is coarse/CHECK-limited; the cooldown is the bound). Items inside the cooldown are counted `skipped_items`. `ORDER BY last_attempted_at` makes the 25s-cap cut deterministic and starves nobody.

**Per Plaid account returned for an eligible Item** (per-Item `try/catch`; the REQ-WC-006 three-layer D1 isolation — per-row `db.prepare().bind().run()` in try/catch; NOT a Python savepoint). Set `plaid_item.last_attempted_at = :nowMs` for each Item before its Plaid call. Skip non-USD accounts (`iso_currency_code != 'USD'`) with a warning log (per REQ-WC-006/REQ-026). On a Plaid **terminal** error: `UPDATE plaid_item SET last_sync_status='error' WHERE id=?` (the literal `'error'` — CHECK-valid, matching plaid-balance-sync.ts; NEVER the raw Plaid code, which the D1 CHECK rejects), increment `errors`, continue (REQ-021's halt-immediately applies to script/adapter runs, not this browser endpoint — operator notice is via REQ-WC-007).

1. **Read baseline BEFORE write, excluding today** (resolves the false-positive/false-clear): `SELECT current_balance, plaid_account_type FROM plaid_account_balance_snapshot WHERE account_id=? AND snapshot_date < :todayUTC ORDER BY snapshot_date DESC, fetched_at DESC LIMIT 1` → `baseline` (null = no prior-day history → A′ no-op). Drift therefore always means "change since the prior calendar-day close" on **both** the cron and login paths — a stable, financially meaningful baseline (not an intra-day morning-vs-afternoon delta).
2. **Upsert-if-fresher** — full column fidelity (all snapshot columns in both INSERT and DO UPDATE so a fresh-wins insert never NULLs a column and a stale audit trail never lingers):
```sql
INSERT INTO plaid_account_balance_snapshot
  (id, account_id, snapshot_date, plaid_account_type, plaid_account_subtype,
   current_balance, available_balance, iso_currency_code, pulled_at, fetched_at, raw_data)
VALUES (?,?,:todayUTC,?,?,?,?,?,?,:nowMs,?)
ON CONFLICT(account_id, snapshot_date) DO UPDATE SET
  plaid_account_type=excluded.plaid_account_type,
  plaid_account_subtype=excluded.plaid_account_subtype,
  current_balance=excluded.current_balance,
  available_balance=excluded.available_balance,
  iso_currency_code=excluded.iso_currency_code,
  pulled_at=excluded.pulled_at,
  fetched_at=excluded.fetched_at,
  raw_data=excluded.raw_data
WHERE excluded.fetched_at > plaid_account_balance_snapshot.fetched_at;
```
The nightly cron keeps its insert-or-ignore double-run protection unchanged; only this login path upserts-if-fresher (a plain insert-or-ignore here would silently drop the fresher intra-day value and the feature would deliver nothing — the original P0).
3. **Confirm the write landed:** read D1 `meta.changes` from the upsert result (the established codebase convention — `meta.changes` is SQLite `changes()`, reliably `0` when a conditional `ON CONFLICT DO UPDATE … WHERE` predicate is false; do NOT use `meta.rows_written`, whose behavior on a WHERE-blocked conditional upsert is unspecified by workers-types). If `meta.changes === 0` (no-op: `excluded.fetched_at` was not greater) the DB value is unchanged → **skip the A′ hook** (A′ must never compute drift on a value the DB does not hold). The unchanged-value case (Plaid returns the same balance) is naturally handled: the row IS written (`fetched_at` advances, `changes=1`), drift = 0 → within tolerance → no alert, and a prior flag auto-clears. §5 adds a test asserting `meta.changes===0` on a same-`fetched_at` no-op upsert against Miniflare D1 (pins the D1 behavioral contract).
4. **A′ hook** (§4): only if step 3 confirmed a write — `await checkBalanceDrift(deps, {accountId, plaidAccountType, baseline, newBalance})`.

**Concurrency (best-effort, NOT exclusive — KV has no atomic CAS):** before processing, `kv.put('plaid_refresh:'+hex(sha256(email)), String(nowMs), {expirationTtl:60})`; if `kv.get(key) !== null` on entry, return `202 {status:'in_progress'}` (key present ⇒ a sync started <60s ago; CF KV TTL guarantees absence after 60s even on crash, so no manual age math). **Cloudflare KV enforces a hard minimum `expirationTtl` of 60s** — a 30s TTL is rejected (`400 Invalid expiration_ttl`) in prod and under Miniflare, so 60s is the floor, not a tuning choice. This is acknowledged best-effort de-dup for a single low-frequency operator — a true race (two cold-start requests in the same instant) could double-call Plaid; the 4h stale-gate + 30-min per-Item cooldown are the real cost bounds. Delete the key on completion. TTL 60s > the 25s wall-clock cap, so a crashed run holds the lock at most ~60s before auto-expiry (a subsequent login within that window sees `202` and retries once after `CLIENT_RETRY_MS`).

**Wall-clock cap:** capture `startMs`; before each Item, if `Date.now()-startMs > 25_000` → stop, `aborted=true`, remaining Items → `skipped_items`. The next login resumes (their snapshots are still stale; `ORDER BY last_attempted_at` rotates fairly).

**Idempotent / no-op:** zero eligible Items → `200 {refreshed_items:0,skipped_items:N,errors:0,aborted:false,error_code:null}`; no KV key written; no `ingestion_log` row.

**Observability:** per Item that called Plaid → one `ingestion_log` row (`source='plaid_login_refresh'`, `records_processed`=accounts written, `status`='success' on full/partial success or 'error' on per-Item exception after retry). One `audit_events` row per non-idempotent run: `entity_type='plaid_login_refresh_run'`, `entity_id`=crypto.randomUUID(), `field_changed='run'`, `new_value`=JSON `{refreshed_items,skipped_items,errors,aborted}`, `changed_by='system:plaid_login_refresh'`.

**Response** `{refreshed_items,skipped_items,errors,aborted,error_code}`; `error_code` ∈ {null,'partial_error','db_error','fetch_error'} mirroring REQ-WD-008.

### 3.3 Client (`src/routes/(wealth)/wealth/+page.svelte`)
In the existing `onMount`, after the initial data fetch is kicked off (non-blocking): `fetch('/wealth/api/brokerage/refresh-plaid-balances',{method:'POST'})`.
- On `200` with `refreshed_items>0`: call `chartStore.bumpRefreshCount()` (the existing chart re-fetch trigger via the `chartStoreKey` derived) AND re-run the existing `fetchTopHoldings()` / `fetchNetWorth()` / `fetchAccounts()` calls — exactly what the RefreshingBanner `onRefreshed` callback already does (`+page.svelte` ~L832-840). There is **no** new `refreshCount` `$state` and **no** component prop; reuse the store. (REQ-WD-008's `repriced-today` is a *separate* trigger via RefreshingBanner — the two refresh paths are independent; this one is an onMount fetch.)
- On `200` with `refreshed_items===0 && errors>0` (`error_code` non-null): render a non-blocking advisory ("Plaid refresh incomplete — showing last known"); no manual retry (next login re-attempts).
- On `202`: `setTimeout` retry once after `CLIENT_RETRY_MS` (5000, the REQ-WD-008 banner interval); if still 202, render last-known and stop. Documented UX: a refresh completing after the client stops polling surfaces on the next page load (the KV key is deleted on completion, so the next load is not blocked).
First paint never blocks on Plaid; a Miniflare integration test simulates 5s Plaid latency and asserts the chart renders from last-known data <2s.

## 4. A′ — baseline-drift alerting (REQ-PS-003, cross-cutting)

### 4.1 Shared helper `src/lib/server/wealth/balance-drift.ts`
`export async function checkBalanceDrift(deps, {accountId, plaidAccountType, baseline, newBalance})`. Called from BOTH the cron path (`plaid-balance-sync.ts`, **additive** — does not change the REQ-WC-006 fetch/classify/write algorithm; invoked after a confirmed per-account write, with `baseline` = the prior-calendar-day row read the same way as §3.2 step 1) and the login path (§3.2 step 4). `LIABILITY = new Set(['credit','loan'])`. Decimal.js throughout (ROUND_HALF_UP, scale 4); never `Number`/`parseFloat`.

```
if (baseline == null) return                                  // no prior-day history
const norm = (b) => LIABILITY.has(plaidAccountType) ? new D(b).negated() : new D(b)
const nb = norm(newBalance), bb = norm(baseline)
const delta = nb.minus(bb), absDelta = delta.abs()
if (nb.isZero() && bb.isZero()) return                         // both zero — no alert
const isInv = plaidAccountType === 'investment'
const pctT = isInv ? 15.0 : 2.0, absT = isInv ? 25000 : 100    // single THRESHOLDS table
const zeroBaseline = bb.abs().lt(new D('1.00'))                // |baseline|<$1 ⇒ abs-only (avoids 9900% noise on ~$0 liabilities)
const deltaPct = zeroBaseline ? null : delta.div(bb.abs()).times(100)
const pctExceed = deltaPct !== null && deltaPct.abs().gt(pctT)
const absExceed = absDelta.gt(absT)
const exceeded = pctExceed || absExceed
```
- `exceeded` → `UPDATE account SET drift_flagged_at=:nowMs WHERE id=?` (idempotent). Email (dedup §4.2). One `audit_events` row: `entity_type='account'`, `entity_id=accountId`, `field_changed='balance_drift'`, `old_value=String(baseline)`, `new_value=JSON.stringify({baseline,new_balance,delta,delta_pct: deltaPct?deltaPct.toFixed(4):null,account_type:plaidAccountType,threshold_triggered: pctExceed&&absExceed?'both':pctExceed?'pct':'abs'})`, `changed_by='system:balance_drift'`. Audit rows accumulate (not deduped).
- `!exceeded && account.drift_flagged_at IS NOT NULL` → `UPDATE account SET drift_flagged_at=NULL` (auto-clear; same prior-calendar-day baseline + threshold test as the alert, so a within-tolerance day clears a prior flag — no stored "flag-triggering snapshot" needed).

### 4.2 Email dedup (cross-Worker; cron Worker and Pages Worker share WEALTH_KV)
Key `drift_alert:<account_id>:<YYYY-MM-DD UTC>`. Before send: `if (await kv.get(key)) return;` after send: `await kv.put(key,'1',{expirationTtl:90000})`. Dedup is **per UTC calendar day** (the date is in the key); the TTL is a straddle-midnight safety net, not the primary window — two drift events on consecutive UTC days each send one email (documented, intended). Resend to `travis@sparkry.com`; subject = `{account_name}: {signed delta} ({delta_pct or 'n/a'}%) — review`. For `plaid_account_type='investment'` the body MUST state: "Net-worth display uses repriced positions for this account (REQ-WD-003/008); the Plaid balance here is the drift trigger, not the net-worth input."

**Cron-path budget (REQ-WC-017 ≤30s):** in the cron path the `kv.get` dedup check is awaited (correctness — must precede deciding to send), but the Resend send and the `kv.put` MUST be dispatched via `ctx.waitUntil(...)` (off the per-account critical path); the `drift_flagged_at` UPDATE is a fast local D1 write and stays synchronous. Budget note: per-account added cost ≈ 1 KV get (~1-5ms) + 1 D1 UPDATE (~1-10ms) on-path; Resend/KV-put are off-path. At ≤35 accounts this is well within the 30s budget. The login path may await all (no cron budget).

### 4.3 Schema (migration `0008_account_drift_flag.sql`)
```sql
ALTER TABLE `account` ADD COLUMN `drift_flagged_at` INTEGER;
```
Reconciliation/coverage surface adds a `WHERE drift_flagged_at IS NOT NULL` listing. UPDATE on `account` is permitted (DELETE-guard triggers only block DELETE).

## 5. Tests (TDD — failing first, per REQ-ID)

- **REQ-PS-001:** `tests/unit/ci-cron-deploy-guard.test.ts` — passes on the fixed ci.yml; throws for synthesized variants (missing cron-worker step / `if:` present / `if: false` / commented / `continue-on-error: true` / `|| true` / step contains `pages`).
- **REQ-PS-002** (`tests/unit/refresh-plaid-balances.test.ts`, Miniflare D1+KV): (a) seed today's cron row, refresh with a different balance → row now holds the refreshed balance & advanced fetched_at (NOT dropped); (b) seed today's row with `fetched_at=nowMs` → conditional upsert no-ops (`meta.changes===0`) → A′ NOT called; (c) fresh Item (<4h) skipped, no Plaid call; (d) Item with `last_sync_status='error'` but outside the 30-min cooldown → still attempted (no status-string filter); on a Plaid terminal error → `last_sync_status` set to `'error'` (CHECK-valid, not the raw code) and counted in `errors`; (e) Item attempted <30min ago → skipped (cooldown, counted skipped_items); (f) non-USD account skipped; (g) zero eligible → 200, no KV, no ingestion_log; (h) concurrent (KV key present) → 202; (i) 25s cap → aborted:true; (j) per-Item error isolation; (k) terminal error updates last_sync_status; (l) ingestion_log + run audit written; client runtime test: non-blocking <2s + bumpRefreshCount on refreshed_items>0.
- **REQ-PS-003** (`tests/unit/balance-drift.test.ts`): depository +$150 vs prior-day → alert; investment +3% → NO alert; investment +20% → alert; credit/loan sign-normalized (both sides) correct; baseline read excludes today (intra-day cron→login same-day small delta does NOT clear a real prior-day flag); zero-baseline ($0→$1) → abs-only, no div-by-zero, audit delta_pct=null; |baseline|<$1 → abs-only; both-zero → no alert; first-ever/no-prior-day → no alert; email dedup (two same-UTC-day writes → one Resend) across both paths; auto-clear (flag set, next within-tolerance prior-day write → NULL); investment email contains the net-worth clarification string; cron-path Resend/KV-put are `ctx.waitUntil`-dispatched (not awaited on the per-account path).

## 6. Rollout & smoke

Migrations `0007`, `0007b`, `0008` ship via the existing deploy `wrangler d1 migrations apply --remote` step, which already runs **before** the (new) cron-Worker deploy and the Pages deploy (§2.1) — schema exists before new code. Post-deploy smoke (browser, prod — pre-customer): (1) CF API confirms `sparkry-crm-cron` + `"7 10 * * *"`; (2) load `/wealth` → `refresh-plaid-balances` POST fires, page non-blocked; (3) net worth still reconciles to the canonical headline (no regression of the prior double-count fix); (4) operational: ≥25h later, an `ingestion_log source='plaid-balance-sync'` row exists.

## 7. Non-Goals

Real-time/streaming; refreshing fresh/terminal/recently-attempted Items; blocking page render; automated Pages rollback; manual flag-dismiss UI; changing the REQ-WC-006 per-Item fetch/classify/write algorithm (A′ is an additive downstream call); true exclusive concurrency (KV best-effort accepted for single operator); cross-UTC-day email dedup.
