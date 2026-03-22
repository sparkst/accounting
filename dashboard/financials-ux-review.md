# Financials Page UX Review

**Page:** `/routes/financials/+page.svelte`
**Date:** 2026-03-21
**Tools:** Nielsen heuristic review, WCAG AA accessibility audit, information architecture analysis

---

## Findings

### P0 — Critical

| # | Area | Finding | Recommendation |
|---|------|---------|----------------|
| 1 | Accessibility | **Toggle buttons lack `aria-expanded` and `aria-controls`** — The `section-toggle` (Profitability) and `inline-toggle` (Revenue, Operating Expenses) buttons use plain `<button>` with no ARIA state. Screen readers cannot determine whether a section is expanded or collapsed. WCAG 4.1.2 Name, Role, Value. | Add `aria-expanded={showMargins}` / `aria-expanded={showExpenseDetail}` to each toggle button and `aria-controls` pointing to the collapsible region's `id`. |
| 2 | Accessibility | **Margin and expense bar charts have no text alternative** — The `margin-fill` and `expense-bar-fill` divs communicate data visually via width percentage but have no `role`, `aria-label`, or `aria-valuenow`. WCAG 1.1.1 Non-text Content. | Add `role="meter"` with `aria-valuenow`, `aria-valuemin="0"`, `aria-valuemax="100"`, and `aria-label` (e.g., "Gross margin 65%") to each bar fill element. |
| 3 | Accessibility | **Color-only profit/loss distinction** — `amountClass()` applies green/red CSS classes (`amt-positive`/`amt-negative`) as the only indicator of positive vs negative amounts. WCAG 1.4.1 Use of Color. | Ensure `formatAmount()` always includes a sign character (+ or -) or prefix ("Loss:"), or add an icon/shape indicator alongside color. Verify the current format includes a minus sign for negatives. |

### P1 — High

| # | Area | Finding | Recommendation |
|---|------|---------|----------------|
| 4 | IA / Completeness | **Comparison mode drops critical sections** — When `compareMode` is enabled, Expense Breakdown bars, Revenue Sources table, Concentration Warnings, and the Profitability margin section all disappear. A tax preparer comparing entities loses the revenue concentration warning exactly when they need it most. | Show per-entity or combined versions of Expense Breakdown, Revenue Sources, and Concentration Warnings in comparison mode. At minimum, retain the warnings. |
| 5 | Usability | **No print or export affordance** — Tax preparers and accountants need to produce paper/PDF artifacts of the income statement. There is no print stylesheet, export button, or "Copy to clipboard" option. | Add a "Print / Export" button in the page header. Consider a `@media print` stylesheet that hides controls and optimizes the income statement table layout. |
| 6 | Usability | **No keyboard shortcuts** — Other dashboard pages support `j/k/y/e/s/d` keyboard navigation. This page has no documented shortcuts. For the ADHD/autism user who prefers keyboard-first interaction, this is a gap. | Add at minimum: `p` to toggle Profitability, `r` to toggle Revenue detail, `x` to toggle Expense detail, `c` to toggle Compare mode. Show shortcut hints in a help tooltip or `?` overlay. |
| 7 | Usability | **Collapse state resets on filter change** — Changing entity or year triggers `load()` which re-renders the page. The `showExpenseDetail` / `showIncomeDetail` / `showMargins` states survive (they are not reset in `load()`), but the skeleton flash and re-render can feel like a reset. | Consider preserving scroll position and adding a subtle transition instead of the full skeleton when data is already loaded (only show skeleton on first load, use inline loading indicator for subsequent loads). |
| 8 | Usability | **MoM percentage confusing for yearly view** — The BLUF cards show "vs prior month" change percentages, but the page title says "YTD" and the year selector implies annual scope. This creates a mismatch in mental model. | Either (a) label the period explicitly ("Mar vs Feb 2026"), (b) show YTD-vs-prior-year when viewing annual data, or (c) add a period toggle (Monthly / Quarterly / Annual). |

### P2 — Medium

| # | Area | Finding | Recommendation |
|---|------|---------|----------------|
| 9 | Accessibility | **Skeleton loading has no accessible announcement** — The skeleton grid shows visual placeholders but does not use `aria-busy="true"` on the container or announce loading state to screen readers. WCAG 4.1.3 Status Messages. | Add `aria-busy={loading}` to the main container and use `aria-live="polite"` region for the error message. |
| 10 | Accessibility | **Table header `scope` attributes missing** — The `<th>` elements in the income statement and revenue sources tables lack `scope="col"` or `scope="row"`. WCAG 1.3.1 Info and Relationships. | Add `scope="col"` to all `<th>` elements. For the section-header rows that use `colspan`, consider using `<th scope="rowgroup">` instead of `<td>`. |
| 11 | Usability | **Error state is generic** — The error card shows the raw error message string and a "Try again" button. For a user with ADHD, a vague "Failed to load financial data" with no next step beyond retry is frustrating. | Provide specific guidance: "Check that the API server is running on localhost:8000" or "No data found for BlackLine MTB in 2024 — try a different year." Differentiate network errors from empty-data states. |
| 12 | IA | **"Data Confidence" card links to `/review` which may not exist** — The link `href="/review?entity={selectedEntity}"` points to a route not listed in the known sibling routes. | Verify the `/review` route exists. If it is the same as the review queue on the Register page, link to `/register?filter=needs_review&entity={selectedEntity}` instead. |
| 13 | Usability | **"+N more categories" link is not interactive** — When expenses are collapsed, the "+4 more categories" text is a plain `<td>` with `text-muted` styling. It looks like a label, not a button. User expects to click it to expand. | Make "+N more categories" a `<button>` that triggers `showExpenseDetail = true`, matching the expand affordance of the section toggle. |
| 14 | IA | **No breadcrumb or back-navigation context** — The page relies entirely on the global nav. For a tax preparer who navigated here from the Tax page, there is no contextual breadcrumb trail. | Add a lightweight breadcrumb (e.g., "Dashboard / Financials") or at minimum ensure the global nav clearly highlights the current page. |

### P3 — Low

| # | Area | Finding | Recommendation |
|---|------|---------|----------------|
| 15 | Accessibility | **Toggle icon characters are decorative but not hidden** — The `+` / `−` characters in `.toggle-icon` and `.toggle-hint` are read aloud by screen readers ("minus", "plus") adding noise. | Add `aria-hidden="true"` to the toggle icon spans since the `aria-expanded` attribute (once added per P0-1) conveys the state. |
| 16 | Usability | **Reimbursable section has no tooltip/explanation** — "Reimbursable (nets to $0)" may confuse a tax preparer unfamiliar with the Cardinal Health reimbursement flow. | Add a small `(?)` info icon with a tooltip: "Expenses paid on behalf of Cardinal Health, reimbursed monthly. Excluded from P&L totals." |
| 17 | Aesthetics | **Expense bar fill uses red with 0.6 opacity** — The `expense-bar-fill` uses `var(--red-500)` at 60% opacity. Red implies "bad" for all expenses, even normal operating costs. This adds unnecessary anxiety for a neurodivergent user. | Use a neutral color (e.g., `var(--gray-500)` or `var(--blue-400)`) for the expense bars. Reserve red for items exceeding budget or flagged anomalies. |
| 18 | Usability | **Footer "View tax filing details" link is easy to miss** — It is a small, muted-color link at the very bottom of a long page. | Promote this to a more visible call-to-action, or add it as a secondary action in the page header controls area near the entity/year selectors. |
| 19 | Usability | **No empty state for zero-revenue entities** — If an entity has no transactions for the selected year, the page shows BLUF cards with $0 values but no helpful guidance. | Show an empty state message: "No transactions found for {entity} in {year}. Import transactions from the Register page." |
| 20 | IA | **Comparison mode toggle is a checkbox, not a segmented control** — The "Compare entities" checkbox is visually lightweight and easy to overlook. It fundamentally changes the page layout. | Consider a segmented control or toggle switch with clearer visual weight, e.g., `[Single Entity] [Compare]` pill buttons. |

---

## Summary

- **P0 (3 findings):** Accessibility blockers around ARIA states for collapsible sections, missing text alternatives on data visualizations, and color-only data encoding.
- **P1 (5 findings):** Comparison mode data loss, missing print/export, no keyboard shortcuts, collapse/loading UX friction, and confusing MoM-vs-YTD mismatch.
- **P2 (6 findings):** Screen reader loading announcements, table semantics, generic errors, broken review link, non-interactive "more" text, missing breadcrumbs.
- **P3 (6 findings):** Decorative icon screen reader noise, missing reimbursable explanation, anxiety-inducing bar colors, low-visibility footer link, no empty state, weak compare toggle affordance.

**Highest-impact quick wins:** P0-1 (ARIA expanded), P0-3 (sign characters), P1-6 (keyboard shortcuts), P2-13 (clickable "+N more"), P3-17 (neutral bar color).
