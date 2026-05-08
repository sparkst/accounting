# Pipeline 003 — PLAN

5 tasks. ~10 SP total. T1 first (live repro), T2-T4 parallelizable, T5 docs.

---

### T1 — Repro & fix tag-chip click bug  (SP 2)

**Path:** `dashboard/src/routes/brokerage/+page.svelte` (lines 322-339 cycleTag, 673-688 chip markup, 770-790 per-row pill ×).

**Repro.** Open `https://macbook.ancon-cliff.ts.net/brokerage`, locate tag chip strip, click one chip 3 times. Note exact behavior. Try the per-row pill `×` button too.

**Likely fixes:**
- If filter-strip cycle is broken: rewrite `cycleTag` to use direct `Set.add()/Set.delete()` on the `$state` Set (Svelte 5 tracks Set/Map mutations directly without need for reassignment).
- If per-row pill `×` is broken: ensure the optimistic `a.tags = next` mutation goes through a `$state` proxy — may need to reload accounts after the API call.

**Tests first:** Playwright e2e in `dashboard/e2e/brokerage-tags.spec.ts` (or vitest equivalent if the project has one):
1. Click chip → asserts `class="chip tag-chip include"` on the button.
2. Click again → `class="chip tag-chip exclude"`.
3. Click again → `class="chip tag-chip"` (neutral, no include/exclude class).
4. Per-row pill: click `×` → assert pill disappears from the DOM.

If no e2e harness exists, document the manual repro steps and add the test scaffolding as a follow-up task.

---

### T2 — PATCH endpoint for account metadata  (SP 2)

**Paths:**
- `src/api/routes/brokerage.py` — add `PATCH /brokerage/accounts/{account_id}` endpoint
- `src/api/test_brokerage_routes.py` — co-located tests

**Pydantic model:**
```python
class AccountPatchRequest(BaseModel):
    account_name: str | None = Field(default=None, max_length=128)
    beneficiary: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None)
```

Use `model_fields_set` (Pydantic v2) to detect which fields the caller actually sent, so passing `null` clears the field but omitting it leaves the existing value alone. This is the standard PATCH semantic.

**Tests (TDD red first):**
1. PATCH unknown account → 404.
2. PATCH `{account_name: "Travis Trad IRA"}` updates ONLY that field; beneficiary + notes unchanged.
3. PATCH `{account_name: null}` clears the field (sets DB to NULL).
4. PATCH with empty body returns 200 + current row, no change.
5. PATCH `{account_name: "x" * 200}` → 422 (max_length validation).
6. PATCH `{beneficiary: "Travis"}` updates beneficiary, others untouched.
7. updated_at bumps on every successful PATCH.

---

### T3 — Account detail GET endpoint  (SP 2)

**Paths:**
- `src/api/routes/brokerage.py` — add `GET /brokerage/accounts/{account_id}/detail`
- `src/api/test_brokerage_routes.py` — co-located tests

**Response model (Pydantic):** `AccountDetailResponse` with sub-models:
- `account`: full Account fields + `tags: list[str]`
- `latest_position_snapshots: list[PositionSnapshotRow]` (top 10 by as_of desc)
- `latest_balance_snapshots: list[BalanceSnapshotRow]` (top 10 by as_of desc)
- `transaction_count_by_action: dict[str, int]` (canonical_action → count)
- `realized_gl_summary: {short_term: Decimal, long_term: Decimal, total: Decimal, lots: int}` (lifetime totals across all years for this account)
- `ingestion_log_recent: list[IngestionLogRow]` (top 5 by run_at desc — filtered to runs whose source touched this broker; approximate filter is "log.source contains the broker name")

**Tests:**
1. GET unknown account → 404.
2. GET account with no positions → returns empty arrays, not nulls.
3. GET account with mixed positions/balances → both arrays populated, sorted desc.
4. Transaction count grouping is correct (counts by canonical_action, not raw action).
5. Realized G/L summary sums short/long/total correctly across multiple years.
6. Account row carries tags array.

---

### T4 — Frontend: edit-name affordance + account detail page  (SP 3)

**Paths:**
- `dashboard/src/routes/brokerage/+page.svelte` — add edit-name pencil icon in the account row; click → small inline form OR navigate to detail page.
- `dashboard/src/routes/brokerage/accounts/[account_id]/+page.svelte` (NEW) — detail page.
- `dashboard/src/lib/api.ts` — add `patchBrokerageAccount(id, patch)` and `fetchBrokerageAccountDetail(id)`.

**v1 UX:**
- Account row: clicking the broker/account-name text navigates to the detail page (cursor: pointer). No inline-edit pencil; the edit form lives on the detail page.
- Detail page sections (top to bottom): Metadata (with edit form), Latest Positions, Latest Balances, Transaction Summary, Ingestion History.
- Edit form: 3 fields (account_name, beneficiary, notes), Save + Cancel buttons. Save calls PATCH and reloads detail.

**Tests:** Manual verification via the live URL since the dashboard doesn't have a frontend test harness (per Phase 3 deferred items). Document the manual test plan in the demo phase.

---

### T5 — CLAUDE.md docs  (SP 1)

Append to the Brokerage section:
- The new PATCH endpoint (with example body).
- The new detail endpoint (with response shape).
- The detail page URL (`/brokerage/accounts/<id>`).

---

## Build sequence

```
T1 (independent)
T2 ──┐
T3 ──┼── T4 (depends on T2 PATCH + T3 detail)
     ┘
T5 (docs, after T2+T3+T4)
```

T1 can run in parallel with T2 and T3. T4 starts after T2+T3. T5 last.

## DoD

- [ ] All 5 tasks landed.
- [ ] `pytest src/api/test_brokerage_routes.py` green; new tests for both endpoints.
- [ ] Tag-chip bug fixed (or root-cause documented + ticket filed if it's deeper than the planned fix).
- [ ] PATCH endpoint live; manual smoke: rename one account through the UI, confirm DB persists.
- [ ] Detail page live; manual smoke: navigate to one account, confirm data loads.
- [ ] CLAUDE.md updated.
- [ ] Review-loop converges to 0 P0+P1 across all 4 lenses.
- [ ] Fresh-context verifier returns PASS.
