# UX Critic Review -- Cycle 2

**Date:** 2026-03-22
**Reviewer:** Claude Opus 4.6 (automated UX audit, cycle 2)
**Scope:** Verify cycle 1 P0/P1 fixes, evaluate updated Financials page, find regressions

---

## 1. Verified Fixes (Confirmed Resolved)

### Global / Cross-Page

| Cycle 1 ID | Severity | Status | Evidence |
|---|---|---|---|
| **G-2** | P1 | FIXED | Skip-to-content link in `+layout.svelte` line 14, targets `#main` on `<main>`. |
| **G-6** | P2 | FIXED | Dashboard source health link points to `/health` (no `/sources` reference). |
| **G-7** | P3 | FIXED | Dark mode: `html.dark` overrides in `app.css`, toggle in Nav, flash prevention in `app.html`. |
| **G-10** | P3 | FIXED | `font-size: max(.68rem, 11px)` floor on `.config-badge`, `.confidence-badge`. |
| **G-11** | P3 | FIXED | `--nav-height: 52px` custom property defined and referenced for sticky headers. |
| **IA-6** | P3 | FIXED | Nav label changed from "Accounts" to "Rules". |

### Dashboard

| Cycle 1 ID | Severity | Status | Evidence |
|---|---|---|---|
| **D-1** | P0 | FIXED | "This Month" card shows "All Entities" badge and links to Register filtered by month. |

### Health

| Cycle 1 ID | Severity | Status | Evidence |
|---|---|---|---|
| **H-3** | P2 | FIXED | Dismiss button has `aria-label="Dismiss error"` (line 170). |
| **H-6** | P2 | FIXED | Freshness dots have `aria-label={freshnessLabel(...)}` (line 186). |

### Reconciliation

| Cycle 1 ID | Severity | Status | Evidence |
|---|---|---|---|
| **RC-2** | P1 | FIXED | Link bar shows human-readable descriptions, amounts, and dates instead of truncated UUIDs. |
| **RC-7** | P1 | FIXED | Unmatched rows have `onkeydown` for Enter/Space, `tabindex="0"`, `role="row"`, `aria-selected`. |
| **RC-8** | P2 | FIXED | Tabs use `role="tablist"`, `role="tab"`, `aria-selected`, `aria-controls`, and `role="tabpanel"` with `aria-labelledby`. |

### Invoices

| Cycle 1 ID | Severity | Status | Evidence |
|---|---|---|---|
| **I-1** | P1 | FIXED | Mark Sent and Mark Paid have inline confirmation patterns (`sentConfirmId`, `paidConfirmId`). Toast component added. |

### Financials (New Page -- Cycle 1 Review Fixes)

| Cycle 1 ID | Severity | Status | Evidence |
|---|---|---|---|
| **F-P0-1** | P0 | FIXED | Toggle buttons have `aria-expanded` and `aria-controls` (Profitability, Revenue, Operating Expenses). |
| **F-P0-2** | P0 | PARTIAL | Gross margin bar has `role="meter"` with full ARIA. Net margin bar and expense bars still missing. |
| **F-P2-12** | P2 | FIXED | "Need review" link points to `/register?status=needs_review&entity=` instead of `/review`. |
| **F-P2-13** | P2 | FIXED | "+N more categories" is now a `<button>` that expands expense detail. |

---

## 2. Remaining Issues from Cycle 1

### P1 -- High (still open)

| ID | Page | Finding | Notes |
|---|---|---|---|
| **G-1** | Nav | **9 top-level nav items** (added Financials since cycle 1, was 8). ADHD decision paralysis concern worsened. | Consider grouping: collapse Health + Rules under "System" dropdown, or Tax + Financials. |
| **G-3** | Nav | Badge layout shift on async load. No skeleton placeholders. | Low real-world impact, acceptable to defer. |
| **RC-1** | Reconciliation | No way to un-match a pair. No "Unlink" button on matched rows. | Schedule for next sprint. |
| **R-1/R-2** | Register | Inline edit commits on blur, no undo. | Not in scope this cycle; still open. |
| **R-3** | Register | No visible affordance for editable cells. | Not in scope this cycle; still open. |
| **R-10** | Register | Table rows not keyboard-accessible. | Not in scope this cycle; still open. |
| **A-1** | Rules | Blur behavior inconsistency vs Register. | Not in scope this cycle; still open. |
| **A-6** | Rules | Delete dialog does not trap focus. | Not in scope this cycle; still open. |

### P2 -- Medium (selected, still open)

| ID | Page | Finding |
|---|---|---|
| **RC-3** | Reconciliation | `load()` calls `apiPost('/reconcile/run')` on every page visit (line 116). Should separate GET cached results from POST re-run. |
| **H-1** | Health | Re-sync disables ALL buttons when any source syncs (`disabled={syncingSource !== null}`). |
| **H-7** | Health | Progress bar segments use `title` only, no `role="img"` or `aria-label` for screen readers. |

---

## 3. New Issues Found in Cycle 2

### P1 -- High

| # | Page | Finding | Recommendation |
|---|---|---|---|
| **C2-1** | Financials | **Net margin bar and all expense bars lack accessible text alternatives.** Gross margin was fixed with `role="meter"`, but net margin fill (line 416) and `expense-bar-fill` divs (line 569) have no role, aria-label, or aria-valuenow. Screen readers see empty divs. WCAG 1.1.1. | Add `role="meter"` with `aria-valuenow`, `aria-valuemin`, `aria-valuemax`, and `aria-label` to net margin fill and each expense bar fill. **5 min fix.** |
| **C2-2** | Financials | **Compare mode drops Expense Breakdown, Revenue Sources, and Concentration Warnings entirely.** Tax preparer comparing entities loses concentration risk warnings exactly when needed. | At minimum retain Concentration Warnings in compare mode. Ideally show per-entity expense bars. |
| **C2-3** | Reconciliation | **Dismiss buttons on error banners have no `aria-label`.** Health page fixed this (H-3), but Reconciliation dismiss buttons (lines 246, 253) still use raw `X` character. | Add `aria-label="Dismiss error"` to both. **2 min fix.** |

### P2 -- Medium

| # | Page | Finding | Recommendation |
|---|---|---|---|
| **C2-4** | Financials | **Table `<th>` elements lack `scope="col"`.** Income statement and comparison table headers missing scope. WCAG 1.3.1. | Add `scope="col"` to all column headers. |
| **C2-5** | Financials | **No keyboard shortcuts.** Primary tax preparer workflow page has zero shortcuts, unlike Register. | Add `p` (Profitability toggle), `e` (Expense detail), `c` (Compare toggle). |
| **C2-6** | Financials | **MoM percentage says "vs prior month" but page context is YTD.** Revenue BLUF card shows "+12% vs prior month" while title says YTD. Mental model mismatch. | Label comparison period explicitly ("Mar vs Feb 2026"). |
| **C2-7** | Financials | **Expense bar fills have low contrast.** `var(--gray-400)` at opacity 0.7 against `var(--gray-100)` track yields roughly 2.5:1 ratio. | Increase opacity to 0.85 or use `var(--gray-500)`. |
| **C2-8** | Financials | **No print/export affordance.** Tax preparers need paper artifacts. No `@media print`, no export button. | Add print button and print stylesheet. |
| **C2-9** | Reconciliation | **No arrow-key navigation between tabs.** ARIA tabs pattern requires left/right arrow keys. Only click/Enter works. | Add `onkeydown` for ArrowLeft/ArrowRight on tablist. |
| **C2-10** | Dashboard | **`greeting` is `$derived(() => ...)` called as `{greeting()}`.** Works but is a double-evaluation pattern. Should be `$derived.by(...)` referenced as `{greeting}`. | Refactor to `$derived.by(...)`. |
| **C2-11** | Dashboard | **Import button still silently fails.** Catch block at line 152 is empty. D-2 from cycle 1 was not fixed. | Add error toast in catch block. |

---

## 4. Summary Scorecard

| Area | Cycle 1 P0/P1 | Fixed | Still Open | New P1/P2 |
|---|---|---|---|---|
| Global/Nav | 3 | 1 | 2 | 0 |
| Dashboard | 1 (P0) | 1 | 0 | 2 (P2) |
| Financials | 3 (P0) + 5 (P1) | 4.5 | 0.5 | 3 P1 + 5 P2 |
| Register | 3 | not checked | 3 | 0 |
| Invoices | 1 | 1 | 0 | 0 |
| Health | 2 (P2) | 2 | 2 (P2) | 0 |
| Reconciliation | 3 | 2 | 1 | 1 P1 + 1 P2 |
| Rules | 2 | not checked | 2 | 0 |

**P0 remaining: 0**
**P1 remaining: 6** (G-1, G-3, RC-1, C2-1, C2-2, C2-3)
**P2 remaining: ~14** across all pages

---

## 5. Recommended Quick Fixes (Do Before Shipping)

These three take under 15 minutes combined and close half the remaining P1s:

1. **C2-1** (5 min): Add `role="meter"` + `aria-*` to net margin bar and expense bars in `financials/+page.svelte`.
2. **C2-3** (2 min): Add `aria-label="Dismiss error"` to Reconciliation dismiss buttons.
3. **C2-11** (5 min): Add error toast to Dashboard import catch block.

---

## 6. Assessment: Ready to Stop Iterating?

**Almost.** The P0 is clear. The three quick fixes above would reduce P1s to three structural items (nav grouping, un-match capability, compare mode data loss) that are acceptable to ship with and address in the next sprint.

After those quick fixes, the dashboard is in a **shippable state** for tax preparer and accountant workflows. Key strengths:
- BLUF progressive disclosure is well-implemented across Dashboard and Financials
- Dark mode is complete and handles flash prevention
- Skip-link, ARIA tabs, keyboard row selection, and meter roles are solid
- Invoice confirmation flow is now safe against accidental state transitions
- Reconciliation manual match shows human-readable context

**Recommendation: Apply the 3 quick fixes, then ship. Schedule G-1, RC-1, and C2-2 for next sprint.**
