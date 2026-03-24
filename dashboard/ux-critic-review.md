# UX Critic Review -- Accounting Dashboard

**Date:** 2026-03-21
**Reviewer:** Claude Opus 4.6 (automated UX audit)
**Personas evaluated:**

- **Tax Preparer** -- Preparing Schedule C (Sparkry), Form 1065 (BlackLine), personal 1040. Needs clear categorization, totals by tax category, easy export. Time-pressured during tax season.
- **Small Business Accountant** -- Monthly close, P&L review, expense tracking. Needs at-a-glance financial health, drill-down capability, reconciliation confidence. Works across multiple clients.

**Primary user profile:** ADHD and autism -- prefers BLUF progressive disclosure, minimal noise, clear next-actions, keyboard-first interaction.

---

## Table of Contents

1. [Global / Cross-Page Issues](#global--cross-page-issues)
2. [Dashboard (Home)](#1-dashboard-home----pagesveltesvelte)
3. [Transaction Register](#2-transaction-register----registerpage-svelte)
4. [Tax Summary](#3-tax-summary----taxpagesvelte)
5. [Health Dashboard](#4-health-dashboard----healthpagesvelte)
6. [Accounts & Memory](#5-accounts--memory----accountspagesvelte)
7. [Invoices](#6-invoices----invoicespagesvelte)
8. [Reconciliation](#7-reconciliation----reconciliationpagesvelte)
9. [Severity Legend](#severity-legend)

---

## Severity Legend

| Rating | Meaning |
|--------|---------|
| **P0 -- Critical** | Blocks a core workflow or causes data loss risk |
| **P1 -- High** | Significant friction for one or both personas |
| **P2 -- Medium** | Noticeable quality issue that degrades the experience |
| **P3 -- Low** | Polish item; nice-to-have improvement |

---

## Global / Cross-Page Issues

### Navigation (Nav.svelte)

| # | Severity | Heuristic | Finding | Recommendation |
|---|----------|-----------|---------|----------------|
| G-1 | **P1** | H8 (Minimalist Design) | **8 top-level nav items with no grouping.** For an ADHD user, this is a high cognitive load. The flat list forces scanning all items to find the right one. | Group into 2-3 clusters: "Work" (Dashboard, Review, Register), "Money" (Invoices, Tax, Reconciliation), "System" (Health, Accounts). Or reduce to 5-6 primary items and nest Accounts + Health under a "Settings/System" dropdown. |
| G-2 | **P1** | WCAG 2.1 | **No skip-to-content link.** Keyboard users must tab through all 8 nav links before reaching page content. | Add `<a href="#main" class="sr-only focus:not-sr-only">Skip to content</a>` before the nav, and `id="main"` on the page container. |
| G-3 | **P1** | H1 (System Status) | **Nav badges (review count, overdue count) load asynchronously.** They pop in after the nav renders, causing layout shift. For an autism-spectrum user, unexpected layout changes are jarring. | Render badge placeholders with a fixed minimum width, or use a skeleton dot during load. |
| G-4 | **P2** | Responsive | **No mobile navigation pattern.** The 8-link horizontal nav overflows on screens under ~900px. No hamburger menu, no overflow handling. | Add a hamburger/sheet pattern for mobile. At minimum, add `overflow-x: auto` with scroll indicators. |
| G-5 | **P2** | WCAG 1.4.11 | **Color-only status encoding in multiple places.** Freshness dots, BLUF urgency, deadline urgency, amount coloring all rely on color alone. Users with color vision deficiency lose information. | Always pair color with a text label, icon, or shape. Most places already have text -- audit the few that do not (e.g., source-summary-dot on Dashboard, outstanding-icon dots). |
| G-6 | **P2** | H4 (Consistency) | **Broken cross-link: Dashboard source health points to `/sources` but nav links to `/health`.** | Change the Dashboard link from `/sources` to `/health`. |
| G-7 | **P3** | H7 (Flexibility) | **No dark mode.** Nav hardcodes `background: #fff`. The design system uses CSS custom properties but has no dark theme. | Add a `prefers-color-scheme: dark` media query block in `app.css` that remaps the custom properties. |
| G-8 | **P3** | H10 (Help) | **No global keyboard shortcut reference.** Register has `j/k` hints but there is no discoverable shortcut legend across the app. | Add a `?` keyboard shortcut that opens a shortcut overlay, or link to a help page from the nav. |

### Design System (app.css)

| # | Severity | Finding | Recommendation |
|---|----------|---------|----------------|
| G-9 | **P2** | **No `<caption>` or `aria-describedby` on any `<table>`.** Screen readers cannot determine the table's purpose. | Add a visually-hidden `<caption>` to each data table describing what it contains (e.g., "Transaction register filtered by current criteria"). |
| G-10 | **P3** | **Base font-size is 14px.** While legible, many secondary elements drop to 0.7rem (9.8px), which is below WCAG minimum recommended size. | Set a floor of 11px (0.786rem) for the smallest text. Audit `.dash-card-title`, `.source-badge`, `.config-badge`, `.delta-pct` which all go below 11px. |
| G-11 | **P3** | **Sticky table headers use `top: 52px`** (nav height). If the nav height changes or wraps, headers misalign. | Use a CSS custom property `--nav-height: 52px` and reference it in both places. |

---

## 1. Dashboard (Home) -- `+page.svelte`

### Usability (Nielsen Heuristics)

| # | Severity | Heuristic | Finding | Recommendation |
|---|----------|-----------|---------|----------------|
| D-1 | **P0** | H1 (System Status) | **No entity context on the monthly summary.** "This Month" shows combined Income/Expenses/Net but does not indicate which entity (or all). Tax Preparer needs per-entity P&L; Accountant needs to know which books they are looking at. | Add an entity selector or show the entity breakdown. At minimum, label it "All Entities" and link to Register filtered by current month. |
| D-2 | **P2** | H7 (Flexibility) | **Import button silently fails.** The `handleImport` catch block does nothing -- no toast, no error message. The user has no idea if import succeeded or failed. | Show a toast notification on success ("Import started") and on failure ("Import failed: reason"). |
| D-3 | **P2** | H6 (Recognition) | **"Generate Invoice" button in quick actions is ambiguous.** It links to `/invoices` (the list page), not a creation flow. The label implies it will create an invoice. | Either link to `/invoices/new` or rename to "Invoices" / "View Invoices". |
| D-4 | **P2** | H2 (Real World) | **`greeting()` is called as a function in the template** but `greeting` is defined as `$derived(() => ...)`, making it a derived value that is already a function. This works but is a code smell that could cause confusion during maintenance. | Change to `$derived.by(...)` and reference as `{greeting}` (no parentheses), or keep the getter pattern but be consistent. |
| D-5 | **P3** | H8 (Minimalist) | **Outstanding card mixes 3 unrelated item types** (invoices, review items, deadlines) in one list without visual grouping. For ADHD, ungrouped heterogeneous lists increase scan time. | Add subtle section dividers or group headers within the Outstanding card. |

### Accessibility (WCAG AA)

| # | Severity | Finding | Recommendation |
|---|----------|---------|----------------|
| D-6 | **P2** | The BLUF card uses `aria-label="Next action"` which is good, but the urgency level (red/amber/green) is communicated only via border color and background tint. | Add a visually-hidden span like `<span class="sr-only">Urgent:</span>` before the message when urgency is red. |
| D-7 | **P3** | Outstanding deadline items use `<span>` instead of `<a>` or `<button>`, so they are not focusable via keyboard despite appearing interactive (they have colored dots). | Either make them links to `/tax` or add `role="listitem"` context. |

### Persona-Specific

- **Tax Preparer:** Cannot see per-entity numbers from the dashboard. Must navigate to Tax page to see entity breakdown. Consider adding entity tabs or a mini per-entity row in the "This Month" card.
- **Accountant:** The "This Month" card is useful but lacks comparison to prior month. The BLUF action is excellent for surfacing the most urgent task.

---

## 2. Transaction Register -- `register/+page.svelte`

### Usability (Nielsen Heuristics)

| # | Severity | Heuristic | Finding | Recommendation |
|---|----------|-----------|---------|----------------|
| R-1 | **P1** | H5 (Error Prevention) | **Inline editing commits on blur, even accidental blur.** If the user clicks away accidentally while editing a category, the edit commits with whatever value was in the field. No confirmation, no undo. | Add a brief delay on blur-commit (150ms) and check if the value actually changed. Provide an undo toast: "Category changed to X. Undo?" |
| R-2 | **P1** | H3 (User Control) | **No undo for inline edits.** Once committed, the only way to fix a wrong edit is to click the cell again and change it back. No undo stack, no toast with revert option. | Implement a toast-based undo for the last edit (store previous value, offer "Undo" button for 5 seconds). |
| R-3 | **P1** | H6 (Recognition) | **Inline-editable cells have no visible affordance.** Only on hover does the background change to gray-50. For ADHD users, discoverability of edit mode is critical. | Add a subtle pencil icon or dashed underline to editable cells. Or show a thin bottom-border that distinguishes them from read-only cells. |
| R-4 | **P2** | H1 (System Status) | **No feedback after inline edit succeeds.** The cell just reverts to display mode. User has no confirmation the save worked. | Flash the cell green briefly (200ms) on successful save, or show a micro-toast. |
| R-5 | **P2** | H4 (Consistency) | **Row click expands detail, but cell click starts editing.** These two interactions conflict -- clicking a category cell both triggers the row expansion AND starts the edit (mitigated by `stopPropagation`, but the mental model is confusing). | Add a visual mode indicator. Consider requiring double-click to edit, or show an explicit "Edit" icon in each editable cell. |
| R-6 | **P2** | H7 (Flexibility) | **`j/k` navigation does not support Enter to expand or `e` to edit.** The keyboard hint only mentions j/k but the design spec calls for `y=confirm, e=edit, s=split, d=duplicate`. | Implement the full keyboard shortcut set from the design spec. Add Enter to expand the focused row, Escape to collapse. |
| R-7 | **P2** | H9 (Error Recovery) | **The `a11y_click_events_have_key_events` Svelte warning is suppressed** on the table row. This means keyboard users cannot activate the row expansion via Enter/Space on the `<tr>`. | Add `onkeydown` handler to `<tr>` that triggers `toggleRow` on Enter/Space. Add `tabindex="0"` and `role="row"` with `aria-expanded`. |
| R-8 | **P3** | H8 (Minimalist) | **Running Balance column shows cumulative total within the current page/filter**, which may be misleading. If filters are active, the "balance" is not a true account balance. | Rename to "Page Running Total" when filters are active, or hide the column when filters exclude transactions. |
| R-9 | **P3** | H2 (Real World) | **Tab key during inline edit moves to the next field in the same row, then to the next row.** This is spreadsheet behavior which is good, but Tab's default browser behavior (move to next focusable element) is overridden without notice. | Document this behavior in the keyboard hint bar: "Tab moves to next cell while editing." |

### Accessibility (WCAG AA)

| # | Severity | Finding | Recommendation |
|---|----------|---------|----------------|
| R-10 | **P1** | **Table rows use `onclick` without keyboard equivalent.** The `a11y_click_events_have_key_events` warning is suppressed rather than fixed. Keyboard-only users cannot expand rows. | Add `tabindex="0"` to expandable rows and handle Enter/Space keydown. |
| R-11 | **P2** | **Sortable columns announce sort direction only via arrow character** in the column text. Screen readers read "Date ↓" as meaningless. | Add `aria-sort="descending"` (or `ascending`) attribute to the `<th>` element. |
| R-12 | **P2** | **Autofocus on inline edit fields** triggers `a11y_autofocus` Svelte warning, suppressed with a comment. Autofocus can disorient screen reader users. | Use `requestAnimationFrame(() => element.focus())` instead of the `autofocus` attribute, and announce the edit mode via `aria-live`. |

### Persona-Specific

- **Tax Preparer:** The category filter is missing from the filter bar. A tax preparer reviewing "all MEALS expenses" must use the search box or sort by category. Add a category filter dropdown.
- **Accountant:** CSV export is good. Missing: ability to select multiple rows for bulk operations (bulk confirm, bulk re-categorize). This is especially painful during month-end close with dozens of auto-classified items.

---

## 3. Tax Summary -- `tax/+page.svelte`

### Usability (Nielsen Heuristics)

| # | Severity | Heuristic | Finding | Recommendation |
|---|----------|-----------|---------|----------------|
| T-1 | **P1** | H1 (System Status) | **No confirmation after export download.** The download button shows a spinner while downloading, but after completion there is no success message. The user may not notice the browser's download notification. | Show a brief toast: "FreeTaxUSA export downloaded" with the filename. |
| T-2 | **P2** | H7 (Flexibility) | **No print stylesheet optimization.** The `no-print` class hides buttons during print, which is good, but the IRS breakdown table may split across pages awkwardly. No `@media print` rules in the component styles. | Add `page-break-inside: avoid` on the IRS table and summary sections. Add `@media print` block for optimized layout. |
| T-3 | **P2** | H6 (Recognition) | **The "vs 2025" compare toggle is easy to miss.** It is a small text button in the header actions area, not visually connected to the data it affects. | Move the compare toggle closer to the table (above the IRS breakdown section) or make it a more prominent toggle switch. |
| T-4 | **P2** | H2 (Real World) | **Expense amounts display with inconsistent sign conventions.** In the IRS table, expenses show as negative (red) in the Amount column but the total row shows "(amount)" parenthetical notation. The YoY delta uses plus/minus signs. Three different sign conventions on one page. | Pick one convention and use it everywhere. Recommendation: show expenses as positive numbers in an "Expenses" section (the section header implies the sign), and use +/- only for deltas. |
| T-5 | **P3** | H8 (Minimalist) | **Tax tips section is collapsed by default with a count badge.** Good progressive disclosure. However, dismissed tips persist in localStorage by year+tipId, which means if the same tip appears in a new year, it shows again. This is probably intentional but could surprise the user. | Add a note in the dismissed section: "Tips reset each tax year." |
| T-6 | **P3** | H4 (Consistency) | **Entity tabs use a different visual pattern than the Reconciliation tabs** despite serving the same function (content switching). Tax tabs have bottom-border style; Reconciliation tabs have the same. But Reconciliation tabs show counts in parens while entity tabs do not. | Consider showing transaction counts in entity tabs too: "Sparkry AI (142)". |

### Accessibility (WCAG AA)

| # | Severity | Finding | Recommendation |
|---|----------|---------|----------------|
| T-7 | **P2** | Entity tabs use `role="tablist"` and `role="tab"` correctly with `aria-selected`. However, there are no corresponding `role="tabpanel"` elements, and arrow-key navigation between tabs is not implemented. | Add `role="tabpanel"` to the content area, wire left/right arrow keys to cycle tabs, and manage `tabindex` per WAI-ARIA tab pattern. |
| T-8 | **P3** | The collapsible B&O breakdown uses a `<button>` with `aria-expanded` -- good. But the insights toggle also uses `aria-expanded` on a button that is not semantically connected to its panel via `aria-controls`. | Add `aria-controls="insights-panel"` and `id="insights-panel"` on the tips list container. |

### Persona-Specific

- **Tax Preparer:** This is the most important page for this persona. The IRS line-item breakdown with line references is excellent. Export buttons are well-placed. Missing: a "Print-ready summary" button that generates a clean one-page PDF for filing records. Also missing: a "What's changed since last export" indicator.
- **Accountant:** The readiness percentage is very useful for monthly close confidence. The estimated quarterly tax section is well-structured. Missing: link from each category row to the Register filtered by that category (click "Office Expense" to see all Office Expense transactions).

---

## 4. Health Dashboard -- `health/+page.svelte`

### Usability (Nielsen Heuristics)

| # | Severity | Heuristic | Finding | Recommendation |
|---|----------|-----------|---------|----------------|
| H-1 | **P2** | H1 (System Status) | **Re-sync button disables ALL sync buttons when any source is syncing** (`disabled={syncingSource !== null}`). The user cannot sync two independent sources in parallel. | Only disable the button for the source currently syncing. Other sources should remain actionable. |
| H-2 | **P2** | H5 (Error Prevention) | **No confirmation before re-sync.** Re-sync triggers an API call that may re-ingest data and potentially create duplicates if the adapter is not idempotent. | Add a confirmation tooltip or brief "Are you sure?" inline confirmation for re-sync, especially for sources that are already "green". |
| H-3 | **P2** | H9 (Error Recovery) | **Sync error banner uses a Unicode X character for dismiss** (`dismiss-btn` with `\u2715`). This is not screen-reader friendly and the button has no accessible label. | Add `aria-label="Dismiss error"` to the dismiss button. |
| H-4 | **P3** | H8 (Minimalist) | **Claude API Usage section shows raw token counts.** The user is unlikely to know what "423,891 input tokens" means. The estimated cost is more useful. | Lead with the cost and make token details expandable. Or show a simple budget bar: "$X.XX of $Y.YY monthly budget used." |
| H-5 | **P3** | H6 (Recognition) | **"Last refreshed Xs ago" counter updates every second.** This constant updating is visual noise for an ADHD user and provides minimal value. | Update every 30 seconds, or just show the timestamp and skip the live counter. |

### Accessibility (WCAG AA)

| # | Severity | Finding | Recommendation |
|---|----------|---------|----------------|
| H-6 | **P2** | Freshness dots use `style="background: {color}"` with inline color. Screen readers get no information. The `title` attribute helps on hover but is not accessible to keyboard users. | Add `aria-label` to the freshness dot: `aria-label="Status: Fresh"` (or Stale, Very stale, Never synced). |
| H-7 | **P2** | The progress bar in Classification Accuracy has segments with `title` attributes but no accessible alternative. Screen readers see an empty div structure. | Add `role="img"` and `aria-label="Classification breakdown: 60% confirmed, 25% auto-classified, 10% pending, 5% rejected"` to the progress bar container. |

### Persona-Specific

- **Tax Preparer:** Cares about: "Is all my data imported?" The source freshness grid answers this well. Missing: a single BLUF line at the top: "All sources up to date" or "2 sources need attention -- data may be incomplete."
- **Accountant:** Wants to know classification accuracy and pending review count. The CTA to review pending items is good. Missing: trend data -- is the auto-classification accuracy improving or declining over time?

---

## 5. Accounts & Memory -- `accounts/+page.svelte`

### Usability (Nielsen Heuristics)

| # | Severity | Heuristic | Finding | Recommendation |
|---|----------|-----------|---------|----------------|
| A-1 | **P1** | H3 (User Control) | **Inline edit on vendor rules commits on blur via `cancelEdit`**, but the `onblur` handler calls `cancelEdit` (not `saveEdit`). This means the edit is LOST on blur, not committed. Inconsistent with the Register page where blur commits. | Make behavior consistent: either blur-saves everywhere or blur-cancels everywhere. Given the ADHD user profile, blur-save with undo is safer (prevents lost work from accidental blur). |
| A-2 | **P2** | H5 (Error Prevention) | **Delete rule has no undo.** The confirmation dialog says "This cannot be undone." For vendor rules that the system learned over time, accidental deletion destroys classification memory. | Soft-delete rules (mark as inactive) with an undo option, or add a "recently deleted" recovery list. |
| A-3 | **P2** | H1 (System Status) | **No success feedback after creating, editing, or deleting a rule.** The table just refreshes. | Add toast notifications: "Rule created for Amazon", "Rule deleted", etc. |
| A-4 | **P2** | H6 (Recognition) | **Confidence and Deductible % fields in the Add Rule form accept raw decimals (0.0-1.0)** but most users think in percentages. | Change the input to accept percentages (0-100) and convert internally, or show "%" suffix with the current 0-1 range clearly labeled. |
| A-5 | **P3** | H8 (Minimalist) | **Entity Configuration cards are static/read-only.** They show Tax Form, B&O Filing, and Structure but cannot be edited. Their purpose is unclear -- are they documentation or configuration? | Add a subtle "Reference" or "Info" label to clarify these are informational, not editable. Or remove them if they add no value beyond what is in the nav/entity filter. |

### Accessibility (WCAG AA)

| # | Severity | Finding | Recommendation |
|---|----------|---------|----------------|
| A-6 | **P1** | **Delete confirmation dialog has `role="dialog"` and `aria-modal="true"` but does not trap focus.** Keyboard users can tab out of the dialog into the background page. | Implement a focus trap: on dialog open, focus the first interactive element; on Tab from the last element, cycle to the first. |
| A-7 | **P2** | The search input has no `aria-label`. | Add `aria-label="Search vendor rules"` to the search input. |
| A-8 | **P2** | Inline edit `autofocus` attributes trigger accessibility warnings (same issue as Register). | Use programmatic focus with `requestAnimationFrame`. |

### Persona-Specific

- **Tax Preparer:** Vendor rules directly affect tax categorization. Missing: a way to see which transactions were classified by a specific rule (click a rule to see its matched transactions in the Register).
- **Accountant:** The match count and last match date are useful for identifying stale rules. Missing: a "Coverage" metric -- what percentage of transactions are classified by rules vs. Claude API vs. unclassified?

---

## 6. Invoices -- `invoices/+page.svelte`

### Usability (Nielsen Heuristics)

| # | Severity | Heuristic | Finding | Recommendation |
|---|----------|-----------|---------|----------------|
| I-1 | **P1** | H5 (Error Prevention) | **"Mark Sent" and "Mark Paid" have no confirmation.** These are one-way state transitions that cannot be undone (no "un-send" or "un-pay" action). Accidental clicks on these buttons change invoice status permanently. | Add inline confirmation for Mark Sent/Paid (like the Void button already has). |
| I-2 | **P2** | H1 (System Status) | **SAP Ariba checklist state is persisted to the backend, but there is no loading indicator** while the patch request is in flight. If the network is slow, the checkbox may appear to toggle but the save fails silently. | Show a small saving indicator next to the checklist, or disable checkboxes while saving. |
| I-3 | **P2** | H3 (User Control) | **No way to edit an invoice after creation.** The detail panel shows metadata but has no edit capability for line items, dates, or amounts. The only mutations are status transitions and void. | Add an "Edit" button for draft invoices that navigates to an edit form. |
| I-4 | **P2** | H4 (Consistency) | **The "Generate Next Invoice" section computes the next month based on `last_invoiced_date`**, but the `nextMonth` function has a potential off-by-one error at line 118: `nextM + 1` should just be `nextM` since `getMonth()` is 0-based and `nextM` already adds 1. | Audit and fix the month calculation logic. |
| I-5 | **P3** | H7 (Flexibility) | **No sorting on the invoice table.** The list appears in API return order. Tax Preparer may want to sort by amount or date; Accountant may want to sort by status or days outstanding. | Add sortable columns, at minimum on Date, Amount, and Status. |
| I-6 | **P3** | H8 (Minimalist) | **The SAP Ariba checklist is Cardinal Health-specific** but is embedded directly in the generic Invoice page component. If more customers are added, this becomes clutter. | Extract the SAP checklist into a separate component that is conditionally rendered only for the relevant customer/billing model. |

### Accessibility (WCAG AA)

| # | Severity | Finding | Recommendation |
|---|----------|---------|----------------|
| I-7 | **P2** | Invoice table rows use `onclick` for expand/collapse but have no `role`, `tabindex`, or keyboard handler. Same issue as Register table rows. | Add `tabindex="0"`, `role="row"`, `aria-expanded`, and Enter/Space keyboard handling. |
| I-8 | **P3** | Void confirmation is inline (not a dialog) which is fine, but the "Are you sure?" text and buttons appear without focus management. | Move focus to the "Yes, Void" button when the confirmation appears. |

### Persona-Specific

- **Tax Preparer:** The AR summary card is useful for tracking receivables. Missing: a "Total invoiced this year" or "Total received this year" figure for tax reporting.
- **Accountant:** The SAP Ariba checklist is brilliant for the Cardinal Health workflow -- reduces cognitive load by externalizing process steps. Missing: aging buckets (Current, 30-day, 60-day, 90+ day) in the AR summary.

---

## 7. Reconciliation -- `reconciliation/+page.svelte`

### Usability (Nielsen Heuristics)

| # | Severity | Heuristic | Finding | Recommendation |
|---|----------|-----------|---------|----------------|
| RC-1 | **P1** | H3 (User Control) | **No way to un-match a pair.** Once matched (automatically or manually), there is no UI to break the link. If a match is wrong, the user is stuck. | Add an "Unlink" button on matched pairs (in the matched tab, either per-row or on expand). |
| RC-2 | **P1** | H5 (Error Prevention) | **Manual match shows truncated IDs** (`selectedA.slice(0, 8)`) in the link bar. The user is confirming a link based on 8 characters of a UUID, which is not human-meaningful. | Show the transaction description and amount instead of the ID. E.g., "Link Stripe payout ($1,234.56, Mar 15) with Bank deposit ($1,234.56, Mar 16)?" |
| RC-3 | **P2** | H1 (System Status) | **`load()` calls `apiPost('/reconcile/run')` on mount.** This means every page visit triggers a reconciliation run. If the run is expensive, this could cause performance issues and the user has no control over it. | Separate load (GET cached results) from run (POST trigger re-run). Only run on explicit "Re-run" button click. |
| RC-4 | **P2** | H6 (Recognition) | **The manual match workflow requires selecting from two separate tables** in a side-by-side layout. The user must hold one selection in memory while scrolling the other table. This is high cognitive load for ADHD. | Add a "suggested matches" feature that pre-selects likely pairs based on amount and date proximity. Or show the selected A item's details in a sticky bar while browsing B items. |
| RC-5 | **P2** | H8 (Minimalist) | **Matched pairs table shows all columns even when most pairs are high-confidence same-day matches.** The Date Diff and Card Match columns add noise when values are mostly "same day" and "check". | Default to a compact view showing only exceptions (date diffs > 0, low confidence, no card match). Add a "Show all details" toggle. |
| RC-6 | **P3** | H4 (Consistency) | **Tabs show counts but do not update dynamically.** If the user manually matches a pair, the count in the tab label does not update until the data reloads. | Update tab counts from the reactive state after a successful link operation. |

### Accessibility (WCAG AA)

| # | Severity | Finding | Recommendation |
|---|----------|---------|----------------|
| RC-7 | **P1** | **Unmatched rows use `onclick` on `<tr>` for selection** with no keyboard equivalent. Checkbox inputs inside the rows do have keyboard support, but clicking the row itself (which also triggers selection) is mouse-only. | Add `onkeydown` handler on `<tr>` for Enter/Space to toggle selection. Or rely solely on the checkbox for selection and remove the row-level click handler. |
| RC-8 | **P2** | **Tabs have no ARIA tab pattern.** The tab buttons use plain `<button>` elements with a CSS active class but no `role="tablist"`, `role="tab"`, `role="tabpanel"`, or `aria-selected`. | Add proper ARIA tab roles and arrow-key navigation between tabs. |
| RC-9 | **P2** | **Color-coded rows (green for matched, amber for unmatched, blue/green for selected)** convey meaning through color alone. | Add a small text badge or icon in the first column: a checkmark for matched, a dash for unmatched, "A" / "B" labels for selected items. |

### Persona-Specific

- **Tax Preparer:** Needs to know: "Is everything accounted for?" A single reconciliation confidence score (e.g., "94% reconciled") at the top would be more useful than raw counts.
- **Accountant:** The monthly totals tab with discrepancy detection is excellent. Missing: ability to drill down from a flagged month to see the specific unmatched transactions in that month. Also missing: date range filter to focus on a specific period.

---

## Cross-Page Information Architecture Assessment

### Current Structure
```
/ (Dashboard)
/review (Review Queue)
/register (Transaction Register)
/invoices (Invoices)
/health (Health Dashboard)
/tax (Tax Summary)
/accounts (Accounts & Memory)
/reconciliation (Reconciliation)
```

### Findings

| # | Severity | Finding | Recommendation |
|---|----------|---------|----------------|
| IA-1 | **P1** | **8 top-level items exceeds the 5-7 recommended maximum** for a flat nav. For an ADHD user, this creates decision paralysis when scanning the nav. | Reduce to 5-6 primary items. Suggested grouping: Dashboard, Transactions (Register + Review as sub-tabs), Money (Invoices + Tax + Reconciliation), System (Health + Accounts). |
| IA-2 | **P1** | **Review page is in the nav but no route file was found.** It may be a separate implementation not yet reviewed, or it may be missing entirely. The Dashboard and Health pages both link to `/review`. | Verify the Review page exists. If it is a filtered view of Register, consider making it a Register preset rather than a separate route. |
| IA-3 | **P2** | **No cross-linking between related pages.** Tax category rows do not link to Register filtered by that category. Vendor rules do not link to their matched transactions. Invoice amounts do not link to the reconciled bank deposit. | Add contextual links: click a tax category to see those transactions, click a vendor rule to see matches, click an invoice to see its reconciliation status. |
| IA-4 | **P2** | **No breadcrumbs or "you are here" indicator beyond the nav active state.** For deep interactions (expanding a row, opening an inline edit), the user loses context of where they are in the workflow. | The nav active state is sufficient for page-level orientation. But consider adding a breadcrumb or context header when the user is deep in a sub-workflow (e.g., "Register > Transaction #ABC123 > Editing Category"). |
| IA-5 | **P2** | **Tax Preparer workflow is fragmented.** The typical workflow is: Health (check data completeness) -> Register (review/categorize) -> Tax (verify breakdown) -> Tax (export). This requires visiting 3 different pages. | Add a "Tax Filing Checklist" to the Tax page that walks through the workflow with links: (1) Check data freshness, (2) Review uncategorized items, (3) Verify line items, (4) Export. |
| IA-6 | **P3** | **The "Accounts" label is ambiguous** in an accounting context. It could mean bank accounts, chart of accounts, user accounts, or (as it actually is) vendor classification rules. | Rename to "Rules" or "Classification Rules" to be more specific. |

---

## Summary of Top-Priority Fixes

### P0 (Critical)
1. **D-1:** Dashboard monthly summary lacks entity context -- Tax Preparer cannot tell which entity's numbers they are seeing.

### P1 (High)
2. **G-1:** Nav has 8 ungrouped items -- overwhelming for ADHD user.
3. **G-2:** No skip-to-content link -- keyboard users blocked.
4. **R-1/R-2:** Inline editing has no undo and commits on accidental blur.
5. **R-3:** No visible affordance for editable cells.
6. **R-10:** Table rows not keyboard-accessible (suppressed a11y warning).
7. **A-1:** Accounts page blur behavior (cancel) is inconsistent with Register (commit).
8. **A-6:** Delete dialog does not trap focus.
9. **RC-1:** No way to un-match a reconciliation pair.
10. **RC-2:** Manual match confirmation shows UUIDs instead of human-readable info.
11. **I-1:** Mark Sent/Paid have no confirmation despite being irreversible.
12. **IA-1/IA-2:** Navigation structure needs consolidation and Review page verification.

### Quick Wins (can fix in < 1 hour each)
- G-2: Add skip-to-content link (5 minutes)
- G-6: Fix `/sources` -> `/health` link (2 minutes)
- D-2: Add toast on import success/failure (10 minutes)
- H-3: Add `aria-label` to dismiss buttons (5 minutes)
- A-7: Add `aria-label` to search input (2 minutes)
- R-11: Add `aria-sort` to sortable columns (10 minutes)
- T-8: Add `aria-controls` to collapsible sections (5 minutes)
