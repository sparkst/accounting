# Final UX Review -- Sprint 3

**Reviewed:** 2026-03-24
**Personas:** CFO/owner (ADHD, BLUF), Tax Preparer, Accountant
**Scope:** 8 new/changed pages from Sprint 3

---

## P0 -- Must Fix Before Ship

### 1. Monthly Close hardcodes entity to 'sparkry'

**File:** `src/routes/monthly-close/+page.svelte` line 174, 183
**Finding:** `fetchTaxSummary('sparkry', year)` is called with a hardcoded entity string. The page has no entity selector, so BlackLine and Personal data never appear in the "Verify P&L" step description.
**Impact:** Tax preparer sees wrong net profit figure; could file based on incomplete data.
**Fix:** Add an entity selector (tabs or dropdown) and pass `selectedEntity` to both `fetchTaxSummary` calls. Persist the choice in localStorage alongside the checklist state.

### 2. B&O Wizard shows full-year data for a monthly/quarterly period

**File:** `src/routes/bno-filing/+page.svelte` line 162
**Finding:** `fetchTaxSummary(selectedEntity, selectedYear)` fetches the entire year. When the user selects "March 2026" for Sparkry's monthly B&O, the gross receipts and tax-due figure reflect the full year, not the selected month.
**Impact:** Filing the wrong amount with WA DOR. This is a correctness bug, not just UX.
**Fix:** Pass month/quarter parameters to the API (or filter the response client-side by date range) so the summary card reflects only the selected period.

### 3. Nav: Annual Close link does not close mobile menu

**File:** `src/lib/components/Nav.svelte` line 439
**Finding:** The Annual Close `<a>` uses `onclick={closeGroup}` while every other dropdown link uses `onclick={closeMobileMenu}`. On mobile, clicking "Annual Close" navigates but the slide-out panel stays open, obscuring the page.
**Fix:** Change `onclick={closeGroup}` to `onclick={closeMobileMenu}` on the Annual Close link (line 439).

---

## P1 -- High Priority

### 4. Annual Close / Monthly Close Reset has no confirmation

**File:** `src/routes/annual-close/+page.svelte` line 223; `src/routes/monthly-close/+page.svelte` line 287
**Finding:** The Reset button immediately wipes all checked steps for the selected entity+year. One accidental click (easy for an ADHD user scanning quickly) destroys progress with no undo path.
**Fix:** Add a `confirm('Reset all steps for 2025 Sparkry AI?')` guard, or implement undo (save prior state and offer a 5-second "Undo" toast).

### 5. Search auto-fires navigation at 2 characters after 300ms

**File:** `src/lib/components/Nav.svelte` lines 41-48
**Finding:** Typing "Ad" pauses 300ms and auto-navigates to `/register?search=Ad`. For an ADHD user who pauses mid-thought, this triggers unwanted page loads and context switches.
**Fix:** Increase the minimum character threshold to 3 and the debounce to 500ms. Or remove auto-commit entirely and only fire on Enter.

### 6. Import page: brokerage CSV has no preview step

**File:** `src/routes/import/+page.svelte` lines 318-331
**Finding:** Bank CSVs get a preview table before committing, but brokerage CSVs go straight to import with a single button. A bad file or wrong format silently ingests garbage.
**Fix:** Add a preview step for brokerage CSVs (even if just showing row count and first 5 rows) before the commit button, consistent with the bank flow.

### 7. Tax page cognitive overload

**File:** `src/routes/tax/+page.svelte` (~800+ lines of template)
**Finding:** Readiness, Warnings, Tax Tips, IRS Breakdown, Estimated Tax + Payment Log, Home Office Calculator, B&O Grid, 1099 Tracking, and Export buttons all render on one scrolling page. For the ADHD persona, the key number (net profit / tax due) gets buried.
**Fix:** Add a BLUF summary card at the top showing: Net Profit, Total Tax Due (est), Readiness %. Keep the rest collapsed by default. Consider moving Home Office and Payment Log to dedicated sub-pages or a tab within Tax.

### 8. B&O and estimated tax filing history stored only in localStorage

**Files:** `src/routes/bno-filing/+page.svelte` lines 96-106; `src/routes/tax/+page.svelte` lines 64-81
**Finding:** "Mark as Filed" timestamps and estimated tax payment records live only in localStorage. Clearing browser data or switching devices loses the filing audit trail.
**Impact:** Accountant cannot rely on filing history for compliance documentation.
**Fix:** Persist filing records and estimated payments to the backend API (POST to a `/filings` or `/payments` endpoint). Keep localStorage as a write-through cache for offline resilience.

### 9. Receipt upload has no client-side validation

**File:** `src/lib/components/TransactionCard.svelte` lines 42-73
**Finding:** The file input uses `accept="image/*,.pdf"` but there is no size limit check. A user could accidentally select a 500MB video or multi-hundred-page PDF scan. The upload will either timeout or consume server resources.
**Fix:** Validate file size client-side (e.g., `if (file.size > 10 * 1024 * 1024) { uploadError = 'File must be under 10 MB'; return; }`) before calling `uploadReceipt`.

---

## P2 -- Significant

### 10. Financials page omits Personal entity

**File:** `src/routes/financials/+page.svelte` lines 8-11
**Finding:** The entity selector only includes Sparkry AI and BlackLine MTB. The Tax page includes Personal. An accountant reviewing personal deductions (Schedule A) cannot do so from Financials.
**Fix:** Add `{ value: 'personal', label: 'Personal' }` to the ENTITIES array if the Financials page should support it, or add a note explaining why it is excluded.

### 11. B&O wizard step indicators lack ARIA semantics

**File:** `src/routes/bno-filing/+page.svelte` lines 240-255
**Finding:** The 3-step progress indicator uses plain `<div>` elements with no `role`, `aria-current`, or `aria-label`. Screen reader users cannot determine which step they are on or how many steps remain.
**Fix:** Add `role="list"` on the steps-bar, `role="listitem"` on each step, and `aria-current="step"` on the active step. Or use `role="progressbar"` with appropriate values.

### 12. Hidden file input for receipt upload has no accessible label

**File:** `src/lib/components/TransactionCard.svelte` (around line 680)
**Finding:** The `<input type="file" class="sr-only">` bound to `uploadInput` has no `aria-label` or associated `<label>`. Screen readers announce it as an unlabeled file input.
**Fix:** Add `aria-label="Upload receipt image or PDF"` to the hidden file input.

### 13. Tax page empty state uses emoji as icon

**File:** `src/routes/tax/+page.svelte` line 506
**Finding:** `<span class="icon">` uses the clipboard emoji character. Emoji rendering varies across platforms and is not reliably announced by screen readers.
**Fix:** Replace with an inline SVG icon (consistent with the rest of the dashboard) and add `aria-hidden="true"`.

### 14. Budget data stored only in localStorage

**File:** `src/routes/financials/+page.svelte` lines 52-98
**Finding:** Budget amounts per category per entity per year are stored only in localStorage. Similar to the filing history issue, this is fragile for a system that serves as the financial source of truth.
**Fix:** Persist budget data to the backend API. This is lower priority than filing history (P1-8) since budgets are planning data, not compliance data.
