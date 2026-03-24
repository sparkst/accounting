# Comprehensive UX Review: Accounting Dashboard

**Date:** 2026-03-24
**Reviewer:** Claude (automated UX audit via ux-knowledge + ui-expert tools)
**Personas:** CFO/Owner (ADHD/autism, BLUF-first), Tax Preparer, Small Business Accountant

---

## Severity Guide

- **P0** - Blocks a critical workflow or causes data loss
- **P1** - Significant usability issue; workaround exists but painful
- **P2** - Moderate friction; impacts efficiency
- **P3** - Minor polish or nice-to-have

---

## 1. Per-Page Findings

### 1.1 Dashboard (`/`, `+page.svelte`)

**What works well:**
- BLUF "next action" card is excellent for the ADHD user -- surfaces the single most important thing
- Progressive disclosure: recent activity capped to 5, deadlines to 3
- Time-based greeting personalizes the experience
- Source health collapsed to a single-line summary with link to full page

**Issues:**

| # | Sev | Finding | Detail |
|---|-----|---------|--------|
| D-1 | P1 | `greeting` uses `$derived(() => ...)` returning a function, then called as `{greeting()}` on line 168 | This is a Svelte 5 anti-pattern. `$derived` should return the value directly, not a thunk. It works but is confusing and may cause unexpected reactivity issues. |
| D-2 | P2 | No entity filter on dashboard summary | "This Month" shows "All Entities" but there's no way to filter to just Sparkry or BlackLine. CFO persona needs per-entity P&L at a glance. |
| D-3 | P2 | Outstanding section mixes invoices, review items, and deadlines without visual grouping | Three different data types in one list. Add subtle dividers or group headers. |
| D-4 | P2 | Import button has no progress indicator | `triggerIngest()` could take 10+ seconds for multiple sources. Only shows "Importing..." text. |
| D-5 | P3 | No keyboard shortcut to trigger import | Dashboard has no keyboard shortcuts at all, unlike Register/Review. |
| D-6 | P3 | Skeleton loading doesn't match actual content structure | Skeleton shows 2 cards + 1 wide card; actual layout has BLUF + quick actions + grid. |

### 1.2 Register (`/register/+page.svelte`)

**What works well:**
- Inline editing with Tab-to-next-field is power-user gold
- Keyboard hints bar clearly documents j/k/y/e shortcuts
- Summary cards are clickable to open InsightPanel
- Running balance column
- Date presets with grouped optgroup
- Undo toast after edits

**Issues:**

| # | Sev | Finding | Detail |
|---|-----|---------|--------|
| R-1 | P0 | Inline edit `onblur` fires `commitEdit` which races with dropdown `onchange` | When selecting a category from the dropdown and clicking away, `onblur` fires before the value is set, potentially saving empty/wrong value. Lines 719, 720. |
| R-2 | P1 | No visual affordance that cells are editable | Cells only show dashed underline on hover (line 1029). Users with touchscreens or keyboard-only never discover editability. Need a subtle pencil icon or background hint. |
| R-3 | P1 | URL params use `dateFrom`/`dateTo` (camelCase) but API uses `date_from`/`date_to` (snake_case) | Line 85-87 reads `dateFrom` from URL, but links from Dashboard (line 245) use `date_from`. Mismatch means deep links from Dashboard won't populate filters. |
| R-4 | P1 | Running total is computed from visible page only | Line 72-78: `runningTotals` computes from `items` which is the current page. This is misleading -- page 2's first row starts from 0, not from the end of page 1. |
| R-5 | P2 | 7+ filter controls in one row overwhelm on smaller screens | Search, entity, status, date preset, date from, date separator, date to, show rejected checkbox, clear button. Needs collapsible "More filters" on mobile. |
| R-6 | P2 | Expanded row TransactionCard has full edit UI including confirm/reject | Confirming a transaction from the register expanded row refreshes the whole list (line 249). User loses their scroll position and context. |
| R-7 | P2 | CSV export only exports current page | Line 416-444: `exportCsv()` uses `items` which is the current page slice. No option to export all matching transactions. |
| R-8 | P3 | `focusedRow` keyboard focus and `expandedId` click-to-expand are independent | Pressing j/k doesn't auto-collapse the previously expanded row. User can have focused row != expanded row, which is confusing. |

### 1.3 Review Queue (`/review/+page.svelte`)

**What works well:**
- Priority sorting (missing amount > duplicates > low confidence > first-time vendors)
- Batch selection with Shift-click support
- Optimistic reject with 5-second undo window
- Shortcut overlay (?) for keyboard help
- Client-side filtering appropriate for <200 items

**Issues:**

| # | Sev | Finding | Detail |
|---|-----|---------|--------|
| RV-1 | P1 | Batch confirm has no final confirmation dialog | `bulkConfirmTransactions` is called directly. If user selects 50 items and clicks confirm, there's no "Are you sure?" and no undo. |
| RV-2 | P1 | `fetchReviewQueue` returns all items without pagination | If queue grows beyond 200 items (e.g., after a large import), performance degrades since all filtering is client-side. |
| RV-3 | P2 | Status filter reloads entire queue from server | Changing from "Needs Review" to "Needs Review + Auto" (line 141) triggers full refetch. No caching or incremental load. |
| RV-4 | P2 | No progress indicator for batch operations | `batchSaving` state exists but during a 50-item batch confirm, no progress bar or count. |
| RV-5 | P2 | Card refs use `Record<string, TransactionCard>` but cleanup on item removal is missing | When items are filtered/confirmed, stale refs remain. Line 44. |
| RV-6 | P3 | Direction filter label "Other" is vague | Line 71-73: Filtering `direction !== 'income' && direction !== 'expense'` is labeled "Other" but actually means transfer/reimbursable. |

### 1.4 Tax Summary (`/tax/+page.svelte`)

**What works well:**
- Tax readiness percentage with progress bar
- Year-over-year comparison toggle with delta formatting
- Dismissible tax optimization tips with localStorage persistence
- B&O monthly/quarterly breakdown per entity type
- Multiple export formats (FreeTaxUSA, TaxAct, B&O)

**Issues:**

| # | Sev | Finding | Detail |
|---|-----|---------|--------|
| T-1 | P1 | Tax tips filter uses `isTipDismissed()` which reads localStorage synchronously in `$derived` | Line 96-97: This won't be reactive when `dismissTip` is called because `$derived` doesn't track localStorage reads. The workaround `summary = summary` (line 91) is a hack. |
| T-2 | P2 | Entity tabs don't indicate which entities have data | All three tabs are always shown. Tax preparer might click "Personal" and get empty state with no indication beforehand. |
| T-3 | P2 | No print-friendly view despite `no-print` classes | Classes like `no-print` (line 263) exist but no `@media print` styles are defined. Tax preparer needs to print summaries. |
| T-4 | P2 | B&O section uses `(summary as any)?.bno_monthly` unsafe type assertion | Lines 105, 128: Type safety issue. If API shape changes, this silently returns undefined. |
| T-5 | P3 | Download error banner has no auto-dismiss | `downloadError` persists until manually dismissed, even after a successful subsequent download. |

### 1.5 Invoices (`/invoices/+page.svelte`)

**What works well:**
- SAP Ariba checklist with persisted state is brilliant for the Cardinal Health workflow
- Status transition confirmations (Mark Sent, Mark Paid, Void)
- Generate Next Invoice section with customer billing model context
- AR summary at top with outstanding count/amount/age

**Issues:**

| # | Sev | Finding | Detail |
|---|-----|---------|--------|
| I-1 | P1 | `nextMonth()` has an off-by-one bug | Line 122: `nextM + 1` is wrong. `last.getMonth()` returns 0-indexed, `+1` gets next month (0-indexed), then `+1` again for display makes it skip a month. E.g., if last invoiced in January (month 0), `nextM = 1`, then `1 + 1 = 2` = March, skipping February. |
| I-2 | P1 | Invoice table rows are not keyboard accessible | No `role="row"`, `tabindex`, or keyboard handlers on `<tr class="inv-row">` (line 384). Can't navigate or expand invoices with keyboard. |
| I-3 | P2 | No status filter on invoice list | Only customer filter exists (line 353). Tax preparer looking for all unpaid invoices must scroll through all. |
| I-4 | P2 | Void confirmation and Mark Sent confirmation share `.void-confirm` class | Line 517, 533: Reusing the red-styled void-confirm class for "Mark as sent?" is semantically wrong and visually misleading. |
| I-5 | P3 | Toast stacking has no limit | `toasts` array grows unbounded (line 30). 10 rapid status changes = 10 stacked toasts. |

### 1.6 Health Dashboard (`/health/+page.svelte`)

**What works well:**
- Source freshness sorted worst-first (never > red > amber > green)
- Per-source Re-sync buttons with source config awareness
- Classification accuracy progress bar with legend
- Claude API usage with estimated cost
- Failure log with expandable error details
- "Last refreshed Xs ago" counter

**Issues:**

| # | Sev | Finding | Detail |
|---|-----|---------|--------|
| H-1 | P2 | No auto-refresh | Data goes stale but only manual refresh is available. For a health dashboard, 30-second auto-refresh would be expected. |
| H-2 | P2 | Sync button disables ALL sync buttons when one is syncing | Line 243: `disabled={syncingSource !== null}` disables every Re-sync button. User should be able to queue multiple syncs. |
| H-3 | P2 | Classification accuracy bar math may be wrong | Line 326: Auto-classified width = `auto_confirmed_pct - edited_pct`. If `edited_pct` represents confirmed-by-human percentage, subtracting it from auto-confirmed makes the auto bar too narrow. The naming is confusing. |
| H-4 | P3 | `fmtDatetime` appends 'Z' assuming naive UTC | Line 59: If the API ever returns timezone-aware timestamps, this will double-offset. |

### 1.7 Accounts & Memory (`/accounts/+page.svelte`)

**What works well:**
- Inline editing with cell-level undo
- Delete confirmation with focus trap and focus return
- Source badge (human vs learned) on vendor patterns
- Entity configuration cards with visual entity colors
- Pagination with range display

**Issues:**

| # | Sev | Finding | Detail |
|---|-----|---------|--------|
| A-1 | P1 | TAX_CATEGORIES constant on line 17-24 is a stale copy | Does not include HEALTH_INSURANCE, WHOLESALE_INCOME, CAPITAL_CONTRIBUTION, OTHER_EXPENSE that exist in `categories.ts`. New rules can't use these categories. |
| A-2 | P2 | Entity config cards are read-only with no edit capability | Line 604-638: Shows Tax Form, B&O Filing, Structure but can't be edited. If a future entity is added, code must be changed. |
| A-3 | P2 | No sort on vendor rules table | Headers are not sortable. User can't sort by confidence, match count, or last match date to find stale rules. |
| A-4 | P3 | Delete button uses text "x" character instead of proper icon | Line 575: `>✕</button>` is a Unicode character, not an SVG icon. Inconsistent with the rest of the design system. |

### 1.8 Reconciliation (`/reconciliation/+page.svelte`)

**What works well:**
- Manual match UX with A+B selection is intuitive
- Confidence badges on matched pairs
- Monthly totals with flagged months
- Unlink with inline confirmation
- Tab-based views (Matched/Unmatched/Monthly)

**Issues:**

| # | Sev | Finding | Detail |
|---|-----|---------|--------|
| RC-1 | P1 | Uses inline `apiGet`/`apiPost` functions instead of shared `$lib/api.ts` | Lines 91-111: Duplicates the API request pattern. If auth is added to the shared module, this page breaks. |
| RC-2 | P1 | Initial `load()` calls `apiPost('/reconcile/run')` which triggers reconciliation on every page visit | Line 119-123: Just viewing the page causes a POST that modifies data. Should be GET for viewing, POST only for explicit re-run. |
| RC-3 | P2 | Unmatched row selection is radio-style but uses checkboxes visually | Lines 501-505: Only one item can be selected per side, but visual checkbox implies multi-select. Use radio buttons or a selected-row highlight. |
| RC-4 | P2 | No date range filter on reconciliation data | Can't filter to a specific month. Must scroll through all-time data. |
| RC-5 | P3 | Emoji used in empty state icon | Line 608: `<p class="icon">📅</p>` -- inconsistent with the geometric/SVG icon style elsewhere. |

### 1.9 Cash Flow (`/cashflow/+page.svelte`)

**What works well:**
- Three-section breakdown (Operating/Investing/Financing) follows GAAP structure
- "All Entities" combined view
- Collapsible sections for progressive disclosure

**Issues:**

| # | Sev | Finding | Detail |
|---|-----|---------|--------|
| CF-1 | P2 | `$effect` triggers load on mount + whenever `selectedEntity` or `selectedYear` changes | Lines 152-156: This causes a double-fetch on mount because `$effect` runs after `onMount` equivalent. Use `onMount` + explicit change handlers instead. |
| CF-2 | P2 | Investing/Financing categories are hardcoded heuristics, not actual data | Lines 21-31: CAR_AND_TRUCK is classified as "Investing" but most car expenses are Operating. CAPITAL_CONTRIBUTION appears in both Investing and Financing sets. |
| CF-3 | P3 | Personal entity shows Operating/Investing/Financing structure | Personal finances don't have operating activities. The three-section layout is misleading for personal. |

### 1.10 Financials / P&L (`/financials/+page.svelte`)

**What works well:**
- Entity comparison mode showing Sparkry vs BlackLine side-by-side
- Gross margin and net margin calculations
- Monthly drill-down with category breakdown
- Expense bar charts scaled to max
- MoM change indicators with explicit month labels

**Issues:**

| # | Sev | Finding | Detail |
|---|-----|---------|--------|
| F-1 | P2 | Personal entity excluded from financials | Line 8-9: Only Sparkry and BlackLine in ENTITIES. Personal P&L (investment income, charitable deductions) has no financial view. |
| F-2 | P2 | Compare mode loads 4 API calls simultaneously | Lines 170-179: Two tax summaries + two aggregations. No loading priority or staggered display. |
| F-3 | P3 | COGS separated from other expenses but not clearly labeled as "Cost of Goods Sold" | The abbreviation COGS may not be understood by all users. |

### 1.11 AR Aging (`/ar-aging/+page.svelte`)

**What works well:**
- Standard aging buckets (Current, 1-30, 31-60, 61-90, 90+)
- Color-coded distribution bar
- Per-customer drill-down with individual invoices
- Days overdue calculation with "Due in Xd" for current items

**Issues:**

| # | Sev | Finding | Detail |
|---|-----|---------|--------|
| AR-1 | P2 | `TODAY` is a module-level constant set on first load | Line 52-53: If the user keeps the tab open past midnight, aging calculations will be wrong until refresh. |
| AR-2 | P2 | No action buttons on individual invoices in the drill-down | Can see the invoice but must navigate to /invoices to take action (mark paid, void). |
| AR-3 | P3 | No export of AR aging report | Accountant/tax preparer may need to share AR aging with a partner or attach to filings. |

### 1.12 Monthly Close (`/monthly-close/+page.svelte`)

**What works well:**
- Simple, focused 5-step checklist
- Month navigator with "Current" badge
- Progress percentage and bar
- Each step links to the relevant page

**Issues:**

| # | Sev | Finding | Detail |
|---|-----|---------|--------|
| MC-1 | P1 | Checklist state is only in localStorage, not synced with actual data | Checking "Review Uncategorized" doesn't verify that needs_review_count is actually 0. User can check it off without doing the work. |
| MC-2 | P2 | No "mark all complete" or "reset" confirmation | Reset button (line 161) clears all checks with no confirmation. |
| MC-3 | P2 | Steps don't show real-time status | "Check Data Freshness" could show green/amber/red from health API. "Review Uncategorized" could show pending count. |
| MC-4 | P3 | Can't add custom steps | Some months may have special tasks (e.g., annual filing, quarterly B&O). No way to add ad-hoc items. |

### 1.13 Navigation (`Nav.svelte`)

**What works well:**
- Sticky header stays visible
- Dropdown keyboard navigation (ArrowDown/Up/Escape/Tab)
- Badge counts for review items and overdue invoices
- Click-outside-to-close on dropdowns
- Dark mode toggle with localStorage persistence
- Active state highlighting for current page AND parent group

**Issues:**

| # | Sev | Finding | Detail |
|---|-----|---------|--------|
| N-1 | P1 | No mobile responsive design | Nav has no hamburger menu, no collapse, no media queries. On mobile, nav items will overflow or wrap unpredictably. |
| N-2 | P1 | No skip-to-content link | Keyboard users must tab through all nav items before reaching page content. |
| N-3 | P2 | Badge counts fetch on every mount including page transitions | Lines 22-41: `fetchHealth()` and `fetchInvoices()` fire on every component mount. SvelteKit page transitions re-mount the layout, causing unnecessary API calls. |
| N-4 | P2 | "Rules" label in System dropdown links to `/accounts` | Mismatch between nav label ("Rules") and page title ("Accounts & Memory"). Confusing wayfinding. |
| N-5 | P3 | No visual indicator of dropdown group while menu is open | When dropdown is open, the trigger gets a background but no arrow/triangle pointing to the dropdown menu. |

### 1.14 TransactionCard (`TransactionCard.svelte`)

**What works well:**
- Progressive disclosure: fields visible, reasoning collapsible, email/attachments behind toggle
- Inline amount editing with sign toggle (+/-)
- Hotel split suggestion (80% room / 20% meals) is domain-smart
- Receipt extraction via Claude AI
- Email HTML/text toggle with sandboxed iframe
- Copy-to-clipboard for attachment paths

**Issues:**

| # | Sev | Finding | Detail |
|---|-----|---------|--------|
| TC-1 | P1 | `handleCardKeydown` captures 'y' globally via `svelte:window` | Line 438: Every TransactionCard instance adds a window-level keydown listener. With 50 cards rendered, 50 listeners fire on every keypress. Only the `focused` card should respond. |
| TC-2 | P2 | Split panel has no keyboard shortcut hint | 's' shortcut exists at the Review page level but the card doesn't show it. |
| TC-3 | P2 | Confirm button disabled state only checks `saving` | If entity and category are both unassigned, confirm still works. Should warn or require at minimum an entity assignment. |
| TC-4 | P2 | Email iframe with `sandbox="allow-same-origin"` is a security concern | Line 887: `allow-same-origin` in combination with user-generated HTML content could allow XSS. Should use `sandbox=""` (no permissions) or add CSP. |
| TC-5 | P3 | `a11y_click_events_have_key_events` and `a11y_no_static_element_interactions` suppressed | Lines 440-441: Two accessibility warnings suppressed rather than fixed. |

### 1.15 Design System (`app.css`)

**What works well:**
- Clean CSS custom property system with semantic tokens
- Full dark mode with color-scheme swapping
- Consistent status pills, entity badges, confidence badges
- Data table styles with sticky headers and hover states
- Focus-visible outline system
- `.sr-only` utility class for screen readers

**Issues:**

| # | Sev | Finding | Detail |
|---|-----|---------|--------|
| CSS-1 | P2 | No `@media print` styles | Tax/Financial pages need print support. Multiple pages use `no-print` class but no print stylesheet exists. |
| CSS-2 | P2 | No responsive breakpoint utilities | Only individual pages define their own `@media` queries inconsistently. Need shared breakpoints. |
| CSS-3 | P2 | `--blue-400` referenced in component styles but not defined | E.g., Register line 903 uses `var(--blue-400)` but only `--blue-500` and `--blue-600` exist in app.css. |
| CSS-4 | P3 | Font size minimum enforcement only on two classes | Line 412: `font-size: max(.68rem, 11px)` only on `.config-badge, .confidence-badge`. Other small text (`.source-badge` at 0.65rem = ~9px) may fail WCAG. |
| CSS-5 | P3 | `--red-200` used in `.btn-danger` border but not defined in dark mode | Line 196-197: `border-color: var(--red-200, #fecaca)` has a fallback but dark mode only defines red-100 through red-700. |

### 1.16 API Layer (`api.ts`, `types.ts`, `categories.ts`)

**Issues:**

| # | Sev | Finding | Detail |
|---|-----|---------|--------|
| API-1 | P1 | No request cancellation / abort controller | Long-running requests (e.g., fetchTransactions during rapid filter changes) pile up. Previous requests should be aborted. |
| API-2 | P1 | `categories.ts` BUSINESS_CATEGORIES includes HEALTH_INSURANCE, WHOLESALE_INCOME, etc. but `types.ts` TaxCategory type does not | Lines categories.ts:14,25 vs types.ts:7-31. TypeScript will complain when these values are used. |
| API-3 | P2 | No retry logic for transient failures | If the API returns 503, the user sees an error with only "Try again" button. No automatic retry with backoff. |
| API-4 | P2 | `fetchAggregations` in `api.ts` doesn't use the shared `request<T>` helper | Line 626: Uses raw `fetch` instead of the shared function. Inconsistent error handling. |
| API-5 | P3 | Invoice `total`, `subtotal`, `adjustments` are strings, not numbers | `types.ts` lines 205-208: These should be numbers for calculations. Multiple pages parse them with `parseFloat()`. |

---

## 2. Cross-Cutting Concerns

### 2.1 Entity Context Inconsistency (P1)

Every page that filters by entity has its own selector with its own state. There is no global entity context. The user must re-select the entity on every page:
- Register: `entityFilter` dropdown
- Tax: `selectedEntity` tabs
- Financials: `selectedEntity` tabs (only Sparkry/BlackLine)
- Cash Flow: `selectedEntity` dropdown (includes "All" and "Personal")
- Accounts: `entityFilter` dropdown

**Recommendation:** Add a global entity context (e.g., in a Svelte store or URL param) that persists across page navigation.

### 2.2 Inconsistent Amount Formatting (P2)

- Dashboard: `formatAmount()` from categories.ts (parenthetical negatives)
- Invoices: `fmtCurrencyAmount()` local function wrapping `formatAmount()`
- Tax: `fmtCurrency()` local function using `toLocaleString()`
- TransactionCard: `formatCurrency()` local Intl.NumberFormat
- Cash Flow: imports `formatAmount` from categories.ts

Some show `($1,234.56)` and others show `-$1,234.56`. Should use `formatAmount()` everywhere.

### 2.3 Date Formatting Inconsistency (P2)

At least 5 different `formatDate`/`fmtDate` functions defined across pages:
- Dashboard line 100: Custom with "Today"/"Yesterday" relative dates
- Register line 140: `Intl.DateTimeFormat` with year
- Invoices line 89: Manual with 'T00:00:00' suffix
- Health line 57: With 'Z' suffix for UTC
- Reconciliation line 210: Manual with 'T00:00:00' suffix

**Recommendation:** Create a single `$lib/formatters.ts` module with all date/currency/entity formatting functions.

### 2.4 Loading/Error State Inconsistency (P2)

Each page implements its own loading/error pattern:
- Some use skeleton screens (Dashboard, Health, Register)
- Some use text "Loading..." (Register table)
- Error states vary: some have "Try again" buttons, some don't
- No shared `ErrorCard` or `LoadingState` component

### 2.5 Missing `@media print` Styles (P2)

Tax Summary, Financials, AR Aging, and Invoices all have data that users would want to print. The `no-print` class is used in multiple places but no `@media print` block exists in `app.css`.

### 2.6 No Mobile Navigation (P1)

The navigation bar has no responsive breakpoint. On screens below ~900px, the three dropdown groups plus brand plus dark toggle will overflow. No hamburger menu, no bottom nav, no mobile adaptation.

---

## 3. Missing Features for a Real Small Business Accounting System

### 3.1 Critical Missing (would expect in any accounting tool)

| Feature | Priority | Notes |
|---------|----------|-------|
| **Search across all pages** | P1 | No global search. Can't search for a vendor across register, invoices, rules from one place. |
| **Audit log viewer** | P1 | System tracks audit events but no UI to view them. Critical for tax disputes. |
| **Duplicate detection UI** | P1 | Review queue sorts duplicates high but no side-by-side comparison view. |
| **Multi-currency support** | P2 | No indication of currency anywhere. Assumes USD. |
| **Attachment upload** | P1 | Can view attachments but no UI to upload receipts/documents to a transaction. |
| **Bank statement import UI** | P1 | Health page shows bank_csv source but no upload UI for CSV files. |

### 3.2 Important Missing (expected for tax/accounting)

| Feature | Priority | Notes |
|---------|----------|-------|
| **Chart of Accounts** | P2 | Only tax categories exist. No formal chart of accounts for double-entry. |
| **Balance Sheet** | P2 | P&L exists but no balance sheet view (assets, liabilities, equity). |
| **Journal entries** | P2 | No manual journal entry capability for adjustments. |
| **Depreciation tracking** | P2 | CAR_AND_TRUCK and other assets have no depreciation schedule. |
| **1099 tracking** | P2 | Contract labor payments over $600 need 1099 tracking. No UI for this. |
| **Mileage log** | P3 | CAR_AND_TRUCK category exists but no mileage tracking integration. |
| **Document management** | P2 | Receipts and invoices scattered across email attachments. No organized document store. |

### 3.3 Nice-to-Have (competitive differentiator)

| Feature | Priority | Notes |
|---------|----------|-------|
| **Budget vs. Actual** | P3 | No budget setting or variance analysis. |
| **Cash flow forecast** | P3 | Cash flow page shows historical only. No projection. |
| **Recurring transactions** | P3 | No way to mark transactions as recurring for prediction. |
| **Report builder** | P3 | Fixed reports only. No custom report capability. |
| **API webhooks** | P3 | No real-time notification of new transactions. |
| **Multi-user** | P3 | Single-user system. No accountant/preparer collaboration. |

---

## 4. Edge Cases and Error Handling Gaps

### 4.1 Empty States

| Page | Empty State | Quality |
|------|------------|---------|
| Dashboard | Error card with message | OK but no illustration |
| Register | "No transactions match your filters" with clear button | Good |
| Review | Needs review: shows empty state | Good |
| Invoices | "No invoices yet" with CTA | Good |
| Tax | "No transactions found" with hint | Good |
| Health | Falls through to skeleton forever if API is down | **Bad** - no timeout |
| Reconciliation | "No matched pairs yet" | OK |
| AR Aging | No explicit empty state | **Missing** |
| Financials | No explicit empty state | **Missing** |
| Cash Flow | Has skeleton but no empty-after-load state | **Missing** |
| Monthly Close | Always shows checklist (no empty state needed) | N/A |

### 4.2 Error Recovery

| Scenario | Current Behavior | Expected |
|----------|-----------------|----------|
| API down on page load | Error message + "Try again" button | Good, but add auto-retry |
| API error during inline edit | `cancelEdit()` silently | Should show error toast |
| Network timeout during import | "Import failed" toast for 5s | Should persist until dismissed |
| Concurrent edits (two tabs) | Last write wins, no conflict detection | Should show "Data changed" warning |
| API returns 401 | Shows raw "API 401: ..." text | Should redirect to auth or show friendly message |
| Stale data after long idle | No staleness detection | Should show "Data may be stale" banner |

### 4.3 Boundary Conditions

| Condition | Current Handling | Issue |
|-----------|-----------------|-------|
| 10,000+ transactions | Server-side pagination | OK for register; review queue loads all client-side |
| Transaction with null amount | Shows "Amount missing -- click to enter" | Good |
| Transaction with $0.00 amount | Treated same as null (line 510) | **Bug**: $0.00 is a valid amount (refund?) |
| Very long vendor name | `truncate` class with ellipsis | OK |
| Entity with no transactions in tax year | Empty state message | OK |
| Overdue invoice with no due_date | Shows "--" | OK but should flag as data issue |
| Future-dated transaction | No special handling | Should warn or flag |
| Negative invoice total | No validation | Should prevent or flag |

---

## 5. CFO Perspective: What's Missing for Running the Business

### Morning Dashboard Check
The dashboard BLUF card is excellent. What's missing:
1. **Cash position** - "How much money do I have right now?" No bank balance display.
2. **Burn rate** - "At this rate, how many months of runway?" No projection.
3. **Revenue target tracking** - "Am I on track for the quarter?" No goals/targets.
4. **Per-entity P&L summary** - Dashboard shows "All Entities" only. Need Sparkry net and BlackLine net separately.

### Monthly Close Experience
The checklist is a good start but it's disconnected from reality:
1. Steps should auto-complete based on data (e.g., "Review Uncategorized" auto-checks when count = 0).
2. Missing step: "Generate and send invoices" (Fascinate hourly, Cardinal Health flat).
3. Missing step: "Verify cash matches bank balance" (final reconciliation step).
4. Missing step: "File B&O" (for months where B&O is due).

### Tax Preparation Workflow
Tax page is strong. Missing:
1. **Schedule C preview** - Show how data maps to actual form lines.
2. **Estimated tax payment tracking** - Shows projections but no way to record actual payments made.
3. **Home office deduction** - CLAUDE.md mentions 6x6 room, $180/yr but no UI for this.
4. **QBI (Qualified Business Income) deduction** - 20% deduction for pass-through. Not calculated.

### Invoice Workflow
Invoice page is well-designed. Missing:
1. **Payment reminder automation** - No way to send reminders for overdue invoices.
2. **Late fee calculation** - `late_fee_pct` exists in data model but no UI to calculate or apply.
3. **Invoice editing** - Can't edit a draft invoice's line items from the UI.
4. **Recurring invoice scheduling** - Must manually generate each month.

---

## 6. Accessibility Summary (WCAG AA)

### What's Done Well
- `aria-label` on navigation, sections, and form controls
- `role="tablist"` / `role="tab"` on tab controls (Tax, Reconciliation)
- `aria-expanded` on collapsible elements
- `role="dialog"` with `aria-modal` on delete confirmation (Accounts)
- Focus trap in delete dialog
- Focus return to trigger element after dialog close
- `:focus-visible` outline system in app.css
- `aria-current="page"` on active nav links
- `.sr-only` utility class available
- Dark mode respects `prefers-color-scheme`

### Issues

| # | Sev | Finding | WCAG Criterion |
|---|-----|---------|----------------|
| A11Y-1 | P1 | No skip-to-content link | 2.4.1 Bypass Blocks |
| A11Y-2 | P1 | Invoice table rows not keyboard accessible | 2.1.1 Keyboard |
| A11Y-3 | P2 | Sortable column headers use `onclick` without `role="button"` announcement of sort state | 4.1.2 Name, Role, Value |
| A11Y-4 | P2 | Color-only indicators (traffic light dots on health page) | 1.4.1 Use of Color |
| A11Y-5 | P2 | Source badge text at 0.65rem (~9.1px) below 11px minimum | 1.4.4 Resize Text |
| A11Y-6 | P2 | Dialog overlay doesn't prevent background scroll | 2.4.3 Focus Order |
| A11Y-7 | P3 | Inline editable cells have no programmatic indication of editability | 4.1.2 Name, Role, Value |
| A11Y-8 | P3 | Two `a11y` warnings suppressed in TransactionCard | Best practice |

---

## 7. Prioritized Action Items

### Immediate (P0)
1. Fix Register inline edit `onblur` race condition (R-1)
2. Fix Invoice `nextMonth()` off-by-one bug (I-1)
3. Reconciliation page: stop POST on page load, use shared api.ts (RC-1, RC-2)

### High Priority (P1)
4. Add mobile responsive navigation (N-1)
5. Add skip-to-content link (N-2, A11Y-1)
6. Sync TaxCategory type with categories.ts constants (API-2, A-1)
7. Add AbortController to API requests for rapid filter changes (API-1)
8. Add confirmation dialog for batch confirm in Review (RV-1)
9. Fix TransactionCard global keydown listener performance (TC-1)
10. Make invoice table rows keyboard accessible (I-2, A11Y-2)
11. Add global entity context that persists across pages (cross-cutting)
12. Fix URL param mismatch (dateFrom vs date_from) for deep links (R-3)

### Medium Priority (P2)
13. Create shared `$lib/formatters.ts` for date/currency/entity formatting
14. Add `@media print` stylesheet
15. Add visual affordance for editable cells in Register
16. Fix Cash Flow double-fetch from `$effect` (CF-1)
17. Make Monthly Close steps data-aware (show real counts)
18. Add entity indicators on Tax tabs
19. Add global search
20. Add bank/CSV upload UI

### Lower Priority (P3)
21. Add keyboard shortcuts to Dashboard
22. Improve skeleton loading to match content structure
23. Add auto-dismiss on download error after successful download
24. Replace Unicode characters with consistent SVG icons
