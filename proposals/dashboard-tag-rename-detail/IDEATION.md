# Pipeline 003 — IDEATION

Three independent items targeted at the brokerage dashboard, bundled because they share the same accounts table surface and one PR is cheaper to coordinate than three.

---

## Item A — Tag-chip click bug (FIX)

**Symptom:** Clicking a tag chip in the filter strip (`dashboard/src/routes/brokerage/+page.svelte:673-688`) doesn't unclick. The chip stays "on" visually after the user expects it to cycle off.

**Investigation.** The `cycleTag` function at line 322 looks correct on paper:
```js
neutral → include → exclude → neutral (3 clicks back to neutral)
```
And `tagState(tag)` at line 341 reads from the `$state` Sets and returns the right tri-state value. The chip uses `class:include={state === 'include'}` etc. which is reactive.

So either:
- (a) The per-row tag-pill `×` button at line 774 (the `Remove` button inside the inline editor) is what the user means by "tag chip" — this is a different code path that calls the API with a wholesale tag-list replacement.
- (b) Svelte 5 `$state` Set mutation pattern: `next = new Set(acctTagInclude); next.delete(...)` reassigns. There may be a closure-staleness bug if the `state` const inside the `{#each}` loop captures the OLD set reference. Svelte 5 derived state tracking should handle this, but a bug here would explain the symptom.

**Recommendation.** Reproduce live first (open dashboard, click a chip 3× and observe state). Two likely fixes:
1. If filter-strip cycle: simplify the cycleTag function by NOT mutating new Sets and reassigning — use `acctTagInclude.add()` / `.delete()` directly on the `$state` Set (Svelte 5 reactivity should track this without reassignment).
2. If per-row pill `×`: ensure the optimistic local update (`a.tags = next`) is reactive when `accounts` is itself a `$state` array.

Add a test (Playwright or vitest) that:
- Renders the page with seeded accounts and tags.
- Clicks a tag chip 3 times.
- Asserts the chip's class transitions: include → exclude → neutral.

This is small (1-2 SP) but load-bearing for the user's chart filtering experience.

---

## Item B — Human-friendly account names (FEATURE)

**Symptom.** The accounts table currently shows `broker · account_number_masked · account_name?`. For most accounts `account_name` is null because the brokerage adapters don't populate it. The user identifies accounts by their personal mental shorthand ("Travis Trad IRA", "Amy Roth", "Aiden 529", "Joint Tenant", etc.) which doesn't appear anywhere.

**Schema.** `Account.account_name` is already `String(128) nullable=True` (`src/models/brokerage.py:82`). The column exists; we just need a way to write to it from the UI.

**Design.**
- Add inline-edit affordance on the accounts table (similar pattern to the existing tag editing): click the name cell → input field → save → API PATCH → optimistic update.
- New API endpoint: `PATCH /api/brokerage/accounts/{id}` with body `{account_name: string | null}` (allowing null to clear back to the masked-number-only view).
- API endpoint validates: trimmed length 1..128 OR null; no other fields modifiable in this endpoint (keep scope tight).
- DB write is a single UPDATE; `updated_at` auto-bumps via the existing onupdate trigger.

**Notable choice.** Should we ALSO let the user edit `beneficiary` (already exists, line 103)? It would be a natural sibling field. **Recommendation: include `beneficiary` in this endpoint.** Accepting `{account_name?, beneficiary?, notes?}` as a partial update lets the rename flow extend without follow-up endpoints. Keep `notes` editable too — natural place to record "401k plan wrapper, do not double-count" etc.

**Endpoint shape:**
```
PATCH /api/brokerage/accounts/{account_id}
Body: { account_name?: string | null,
        beneficiary?: string | null,
        notes?: string | null }
Returns: { account_id, account_name, beneficiary, notes, updated_at }
```

**UI.** For v1, hover the row → small "edit" pencil → opens a small inline form (3 fields: name, beneficiary, notes). Save / Cancel. ~3 SP.

---

## Item C — Account details view (FEATURE)

**Symptom.** No way to see what data we have for a single account: snapshot history, position list, transactions, raw account metadata (broker / number / type / entity / tax_sheltered / parent / wrapper flag / dates).

**Design.** Two layers:

1. **API endpoint** `GET /api/brokerage/accounts/{id}/detail` returning everything we know:
   ```
   {
     account: {id, broker, account_number, account_name, account_type, entity,
               tax_sheltered, beneficiary, notes, parent_account_id,
               is_plan_wrapper, created_at, updated_at, tags: [...]},
     latest_position_snapshots: [...10 most recent rows],
     latest_balance_snapshots: [...10 most recent rows],
     transaction_count_by_action: {buy: N, sell: N, ...},
     realized_gl_summary: {short_term, long_term, total},
     ingestion_log_recent: [...5 most recent runs that touched this account]
   }
   ```

2. **UI.** Click an account row → expand inline OR navigate to `/brokerage/accounts/[account_id]`. For v1, **navigate** — keeps the table compact and lets the detail page have generous space.
   - Detail page renders the data in 4 sections: metadata, latest positions/balance, transaction summary, ingestion history.
   - "Edit" affordance on metadata uses the PATCH endpoint from Item B.

**Choice.** Inline expansion is more dashboardy but accounts table has 7+ columns and limited horizontal space. Detail page wins on screen real estate. ~5 SP.

---

## Cross-cutting decisions

- **Auth:** all 3 items live behind the same Tailscale-only API; no auth changes.
- **Tests:** API tests via FastAPI TestClient (existing pattern); frontend behavior via vitest if available, otherwise Playwright reaching the live dev server. The tag-chip bug particularly benefits from a frontend-level test.
- **Migrations:** none — all touched columns already exist.
- **Backwards compatibility:** none of these items break existing endpoints. The new PATCH and detail endpoints are additive.

## Total scope

~10 SP across 3 items. Fits one pipeline run.

---

## Recommendation

Build all 3 in parallel after a brief exploratory live-repro of Item A (5 minutes). Single review-loop covers all three. Fresh-context verifier checks each item independently.
