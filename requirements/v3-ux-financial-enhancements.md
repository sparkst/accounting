# V3: UX & Financial Enhancements

> Follow-up work from the v2 UX redesign and Financials page. Prioritized by impact.

---

## Priority A: Register Inline Editing UX (P1)

### REQ-V3-001: Inline Edit Undo System
- When a user edits a cell inline (entity, category) and it saves, show a toast: "Category changed to X. Undo?" with a 5-second revert window
- Store previous value in memory; clicking "Undo" reverts the PATCH and restores the cell
- Applies to: Register page and Accounts/Rules page

### REQ-V3-002: Inline Edit Visible Affordance
- Editable cells in the Register table should have a subtle visual indicator (dashed underline or pencil icon on hover)
- User with ADHD needs to discover edit mode without guessing

### REQ-V3-003: Register Keyboard Accessibility
- Table rows must be keyboard-navigable (tabindex="0", Enter/Space to expand)
- Remove suppressed a11y warnings — fix the underlying issue
- Implement full keyboard shortcut set from design spec: y=confirm, e=edit, s=split, d=duplicate, j/k=navigate, Enter=expand, Escape=collapse

### REQ-V3-004: Accounts/Rules Blur Consistency
- Align blur behavior between Register (commit) and Rules (cancel)
- Decision: blur-saves everywhere with undo toast (safer for ADHD — prevents lost work)

### REQ-V3-005: Delete Dialog Focus Trap
- Accounts/Rules delete confirmation dialog must trap focus (keyboard users cannot tab out)
- On dialog open, focus first interactive element; Tab from last cycles to first

---

## Priority B: Navigation Consolidation (P1)

### REQ-V3-006: Nav Grouping
- Reduce 9 top-level items to 5-6
- Proposed: Dashboard, Transactions (Register + Review), Money (Invoices + Financials + Tax), System (Health + Rules + Reconciliation)
- Or: collapse Health + Rules under a "System" dropdown
- Must preserve keyboard accessibility and ADHD-friendly discoverability

---

## Priority C: Financials Page Enhancements

### REQ-V3-007: Compare Mode Completeness
- When entity comparison is enabled, show per-entity expense breakdown bars and concentration warnings
- Currently these sections disappear in compare mode — tax preparer loses critical info

### REQ-V3-008: Monthly Drill-Down
- Add monthly columns to the income statement table (Jan through current month)
- Collapsible: default shows YTD totals only, click to expand monthly breakdown
- Monthly data available via existing /api/tax-summary bno_monthly endpoint

### REQ-V3-009: Keyboard Shortcuts for Financials
- p = toggle Profitability section
- r = toggle Revenue detail
- x = toggle Expense detail
- c = toggle Compare mode
- Show shortcut hints on hover or via ? overlay

### REQ-V3-010: MoM vs YTD Label Fix
- BLUF card shows "vs prior month" but page context is YTD — mental model mismatch
- Fix: label explicitly ("Mar vs Feb 2026") or add period toggle (Monthly/Quarterly/Annual)

---

## Priority D: Reconciliation Enhancement (P1)

### REQ-V3-011: Un-Match Capability
- Add "Unlink" button on matched reconciliation pairs
- Requires new API endpoint: POST /api/reconcile/unlink with pair IDs
- Moves both transactions back to unmatched state
- Critical for fixing incorrect automatic matches

---

## Priority E: New Financial Views

### REQ-V3-012: Cash Flow Statement
- Standard cash flow format: Operating Activities, Investing Activities, Financing Activities
- Data source: Transaction model with direction field (income/expense/transfer)
- Period selector matching Financials page

### REQ-V3-013: AR Aging Report
- Aging buckets: Current, 1-30 days, 31-60 days, 61-90 days, 90+ days
- Data source: Invoice model with due_date and status
- Per-customer breakdown
- Total outstanding with aging distribution bar

### REQ-V3-014: Monthly Close Checklist
- Guided workflow: (1) Check data freshness → (2) Review uncategorized → (3) Reconcile → (4) Verify P&L → (5) Export
- Each step links to relevant page with progress indicator
- Persists completion state in localStorage per month
- For the accountant persona doing monthly close

---

## Estimation

| REQ | Effort | Dependencies |
|-----|--------|-------------|
| V3-001 | M | Toast component (can reuse import-toast pattern) |
| V3-002 | S | CSS only |
| V3-003 | M | Keyboard handler + a11y attributes |
| V3-004 | S | Align one function |
| V3-005 | S | Focus trap utility |
| V3-006 | M | Nav component restructure |
| V3-007 | M | Financials page update |
| V3-008 | L | Monthly data aggregation + table columns |
| V3-009 | S | Keyboard event handlers |
| V3-010 | S | Label change |
| V3-011 | L | New API endpoint + UI |
| V3-012 | L | New page + possible new API |
| V3-013 | M | New page, uses existing Invoice API |
| V3-014 | M | New page/component |
