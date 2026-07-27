# Reliability audit — Plaid sync & register (2026-07-27)

**Trigger:** ~4th–5th failure of routine Plaid syncing and ~10th register correctness bug
this month. This session (a) independently re-verified the 2026-07-27 re-link repairs and
(b) audited *why this class of failure keeps recurring*, then implemented the top
structural fixes.

## Part A — Independent verification of the 2026-07-27 claims (all re-derived)

| Claim | Verdict |
|---|---|
| Schwab re-link repair (scope flip + 2 shells unmapped + AuditEvents) | ✅ Verified — box `plaid_item` 194e6fb1 `scope=wealth`, shells `3e3d66ed`/`c923a39e` NULLed, full `remediation:schwab-scope-fix-2026-07-27` trail |
| Vanguard re-link repair (scope flip + 3 shells + D1 relink×3) | ✅ Verified — item 137cbef9, shells + audit trail; D1 dossiers carry the exact box plaid ids |
| The "4th Vanguard account" (processed=4, mapped=3) | ✅ Identified — `Travis D. Sparks - Brokerage Account ****7894` (settlement), balance **$0.04**, unmapped in D1. Recommend leaving unmapped. |
| Second active Vanguard item is NOT a mirror | ✅ It is **Amy's login** (Amy Rollover IRA / Amy Roth / Emerson 529) — all three mapped + fresh in D1 |
| Fresh D1 data 2026-07-27 | ✅ 17 named accounts incl. Schwab Stocks $2,082,694, Schwab AMZN $312,653.12, Travis IRA $400,186.13, Roth $45,707.81, Aiden 529 $93,185.06; holdings pushed (Schwab 14/15, Vanguard 15/13) |
| PR #27 live (wealth default UI) | ✅ Merged, deployed — served bundle `4.BOfrtviM.js` contains the new markup |
| PR #28 live (429 retry) | ✅ Merged; box file byte-identical to main; retry **fired and recovered in both overnight runs** (03:50 manual + 04:00 scheduled) |
| Overnight timers clean | ✅ balance 04:00 / investments 04:20 / transactions 05:00 / stripe 05:20 / shopify 05:30 all exit 0; all 10 items `last_sync_status=ok`; **Amex PARTIAL_FAILURE resolved** |
| Leftovers | 5 shells exist (keep + mark; audit rows reference them); **5** stray pre-fix snapshots (not 2), written 03:25/03:32 during the mislink window — accurate balances, harmless; the `'pending'` placeholder item **does not exist** (already cleaned) |

**Found broken during verification (fixed live):** `weekly-pl-report.service` failed Mon
06:00 UTC with `226/NAMESPACE` — the Jul-26 deploy rsync **deleted the runtime `reports/`
dir** (and shipped untracked HEIC photos to prod). Recreated the dir, re-ran clean.
Also: every deploy restart of `accounting-api`/`accounting-dashboard` was paging
(`doppler run` exits 255 / vite exits 143 on SIGTERM) — `SuccessExitStatus` drop-ins
applied live and versioned in `deploy/overrides/` (PR #30).

## Part B — Why it keeps recurring

Root pattern across the month's 7 incidents: **every monitor asserted that processes ran,
none asserted that data moved.** Secondary patterns: silent-success trust boundaries,
human-in-the-loop steps defaulting to the dangerous option, no classification regression
oracle.

### Implemented this session

| PR | Fix | Incidents it addresses |
|---|---|---|
| #29 | **Freshness/invariant sentinel** (REQ-SEN-001..008): daily data assertions — item sync recency, per-source `ingestion_log` success recency (expectations derived from active items), register snapshot recency, the mislink scope-anomaly signature, register txn flow, report artifact freshness. One aggregated severity-webhook digest/day; UTC-discipline; masking-aware ambiguity marker; validated clean against a live-DB snapshot. qreviewed (2 rounds, all P1s fixed). | 30-day frozen balances; 6-week silent Stripe/Shopify; dead tx sync; wrong-scope links; deleted reports/ |
| #30 | **Deterministic deploy** (`scripts/deploy_box.py`, REQ-DEP-001..004): clean-worktree guard, gitignore-driven excludes, protect-filters on runtime dirs, DRY-RUN default + versioned `SuccessExitStatus` drop-ins | reports/-deletion class; HEIC-to-prod; restart-noise paging |
| #31 | **Wealth default at every link-token layer** (api.ts param, API schema default, no-body fallback) — PR #27 only fixed the select | The Schwab/Vanguard mislink class, permanently |
| #32 | **Three silent-failure doors closed**: requested-source adapter-unavailable → error (was "OK — ingested 0" forever); raising alert computation → counted failed → exit 1 (was invisible death of EA B&O + overdraft alerts); sweep `still_failed` → exit code (was backlog aging out silently). Plus `seed-brokerage-accounts.py` flipped to DRY-RUN default. | The credential-rotation re-run of incident 4; alert-delivery death |

### Prioritized follow-ups (verified, not yet implemented)

**P0 (money-wrong on filed returns):**
1. **Split children drop `entity` + `deductible_pct`** — `src/classification/splitter.py:236` builds children with `entity=item.entity` (nullable, default None) and no `deductible_pct` (column default 1.0 fires). A no-entity split vanishes from Schedule C/B&O exports (parent excluded as SPLIT_PARENT, children excluded as entity-NULL); a 50%-meals split becomes 100% deductible. Needs: inherit parent entity/deductible_pct unless per-line override.
2. **Vendor-rule creation defaults** — dashboard pre-fills `entity: sparkry`, `OFFICE_EXPENSE`, `deductible_pct 1.0`, **`confidence 1.0`** (above the 0.95/0.97 seeded ceiling → skips review queue forever). A standing wrong instruction, larger blast radius than a single row. Needs: unassigned defaults + confidence ≤0.95.

**P1:**
3. `plaid_balance.py:241` blanket `except IntegrityError` counts failed writes as processed (FK/CHECK violations masked; only the dup-day UNIQUE is benign).
4. `ADDITIONAL_CONSENT_REQUIRED` treated as "no investments product" (clean skip) — indistinguishable from an expiring consent on Schwab/E*TRADE that should page.
5. Empty-work-set floor: all Plaid syncs report success over zero items (accidental mass-disconnect would look healthy). Sentinel's derived expectations shrink with the item set too — a floor assertion (`≥1 register item`, `≥N wealth items`) belongs in the sentinel.
6. `POST /bank-csv/configs` is destructive overwrite advertised as update; `PATCH /invoices/{id}` rebuilds line items (zeroes amounts on partial patch, deactivates live payment link).
7. **D1-side freshness read endpoint** (sparkry-crm): nothing internal-key-readable exposes per-account `MAX(snapshot_date)` from `plaid_account_balance_snapshot`; D1's own staleness queries only look at `account_balance_snapshot`/`position_snapshot` — the Plaid tables are invisible to them. Add `/wealth/api/internal/freshness` (same query the session-gated reconciliation summary already runs) + sentinel check REQ-SEN-009. Closes the 30-day-freeze class **end-to-end** (box-push-succeeded ≠ D1-data-fresh).
8. Classification regression oracle: golden-fixture replay of a known month through the classifier as a CI gate (no oracle exists today; ~10 misclassification bugs this month were human-spotted).

**P2:** mark the 5 retired shells' `notes` (with AuditEvents); "0 mapped accounts" UI label → "wealth" badge; ingestion_log `source` should carry the item id (removes the ambiguity class); consider `institution_id`-scoped source keys.

### Detection-gap scorecard (would today's monitoring catch each past incident?)

| Incident | OnFailure alone | + Sentinel (#29) |
|---|---|---|
| Chase mirror phantom rows | ❌ | ⚠️ partial (adapter hard-errors now; row-count band is follow-up 8) |
| 30-day frozen wealth balances | ❌ | ✅ item_stale + ingest_stale day 1 (D1-side end-to-end needs follow-up 7) |
| Dead box tx sync | ⚠️ days-to-diagnose | ✅ ingest_stale names the source day 1 |
| Silent Stripe/Shopify 6 weeks | ❌ | ✅ ingest_stale day 1; PR #32 also fails the run at the door |
| WAF 429 self-page | n/a (fixed #28) | ✅ retry + sentinel would catch data gaps |
| Wrong-scope links | ❌ | ✅ scope_anomaly same day; PR #31 removes the cause |
| Misclassification stream | ❌ | ❌ — needs the regression oracle (follow-up 8) |
