# V2 Improvements — Requirements

> Findings from 4 persona reviews (UX, Tax/CPA, Travis/Owner, Systems Architect) of the live accounting dashboard, organized by priority.

---

## CRITICAL BUGS (P0)

### REQ-IMP-001: Register Balance Column Shows $NaN
- **Priority**: P0 (critical bug)
- **Source**: UX, Travis
- **Problem**: The register page running balance column displays `$NaN` for rows where `tx.amount` is returned as a string from the API instead of a number. `TransactionOut.coerce_amount()` serializes Decimal to string (line 104-110 of `src/api/routes/transactions.py`), but the Svelte frontend does arithmetic on it as a number. The `runningTotals` derived computation on line 51 of `dashboard/src/routes/register/+page.svelte` does `prev + (tx.amount || 0)` which produces NaN when amount is a string like `"-42.50"`.
- **Acceptance Criteria**:
  - Register balance column never shows NaN for any transaction
  - Amount values are parsed to numbers on the frontend before arithmetic
  - Running total accumulates correctly across all rows
  - Test: load register with 50+ mixed transactions, verify all balance values are valid currency
- **Files to Change**: `dashboard/src/routes/register/+page.svelte` (parseFloat on amount), `dashboard/src/lib/types.ts` (ensure amount type is `number | null` and the fetch layer parses it)
- **Edge Cases**: Amount is null (unknown), amount is zero, amount is a string like `"33000.00"` with no sign
- **Security/Reliability**: No security concern; purely display bug

### REQ-IMP-002: Income Amounts Displayed Without Sign Context
- **Priority**: P0 (critical bug)
- **Source**: Tax/CPA, Travis
- **Problem**: Two distinct issues: (A) Frontend: Income transactions (positive amounts) may not display correctly when the API returns amounts as strings and the Svelte code does arithmetic without parseFloat. The `formatCurrency` function relies on numeric comparison but receives strings. (B) Backend: The tax summary `abs()` call in `src/api/routes/tax_export.py` line 203 takes absolute value of all amounts regardless of direction, which means income categories (which are stored as positive) get the same treatment as expenses (stored as negative). This works today only because income is already positive, but is semantically wrong and will break if data conventions change.
- **Acceptance Criteria**:
  - Income amounts display as positive (green) in register and review queue
  - Expense amounts display as negative (red) in register and review queue
  - Summary cards show correct aggregate totals (income_total is positive, expense_total is negative)
  - Tax summary income line items sum positive amounts directly (not via abs)
  - Tax summary expense line items use abs() on negative amounts
  - Test: create one $100 income and one -$50 expense, verify summary shows $100 income, -$50 expense, $50 net
- **Files to Change**: `dashboard/src/routes/register/+page.svelte` (parseFloat amounts), `dashboard/src/lib/types.ts` (type definitions), `dashboard/src/lib/api.ts` (parse amounts on fetch), `src/api/routes/tax_export.py` (separate income vs expense aggregation logic at line 196-204)
- **Edge Cases**: Zero-amount transactions, reimbursable expenses, split children, Decimal("0") is falsy in Python
- **Security/Reliability**: Incorrect amounts could cause incorrect tax filing

### REQ-IMP-003: Future paid_date on Seed Invoice CH20260131
- **Priority**: P0 (critical bug)
- **Source**: Systems Architect
- **Problem**: `src/invoicing/seed_customers.py` line 252 sets `paid_date="2026-05-06"` (90 days after submission) for invoice CH20260131. Since today is 2026-03-18, this is a future date. A paid invoice with a future paid_date is logically invalid — the invoice cannot have been paid on a date that hasn't occurred yet.
- **Acceptance Criteria**:
  - Seed invoice CH20260131 has a realistic paid_date (e.g., 2026-04-28 — 82 days, or `None` if not yet paid)
  - Alternatively, change status to `sent` if payment hasn't actually been received
  - Test: after seeding, query CH20260131 and verify paid_date <= today or paid_date is None
- **Files to Change**: `src/invoicing/seed_customers.py` (line 252)
- **Edge Cases**: Re-seeding should update the paid_date if it was previously wrong
- **Security/Reliability**: Incorrect financial dates create audit trail issues

### REQ-IMP-004: Wrong Month in Invoice Generate Button
- **Priority**: P0 (critical bug)
- **Source**: UX, Travis
- **Problem**: The `nextMonth()` function in `dashboard/src/routes/invoices/+page.svelte` (lines 96-100) calculates the next month for the "Generate" button label. For March 2026, it shows "Generate 2026-04" but the user may want to generate for March (the current month). The function uses `now.getMonth() + 2` which skips the current month entirely.
- **Acceptance Criteria**:
  - "Generate Next Invoice" button shows the correct next uninvoiced month for each customer
  - If March invoice exists, button shows April; if March doesn't exist, shows March
  - Label uses human-readable format: "Generate March 2026" not "Generate 2026-03"
  - Test: with Feb invoice existing and no March invoice, button says "Generate March 2026"
- **Files to Change**: `dashboard/src/routes/invoices/+page.svelte` (nextMonth function), ideally query the API for the customer's last invoiced month
- **Edge Cases**: December → January year rollover, customer with no prior invoices, customer with gap months
- **Security/Reliability**: No security concern; UX confusion could cause skipped invoices

### REQ-IMP-005: B&O Revenue Subtotals Show All Zeros
- **Priority**: P0 (critical bug)
- **Source**: Tax/CPA, Travis
- **Problem**: The tax page `dashboard/src/routes/tax/+page.svelte` lines 75-84 hardcode `monthlyIncome` and `quarterlyIncome` to `Array(12).fill(0)` and `Array(4).fill(0)` with a comment saying "The tax summary doesn't return per-month breakdown." The B&O table shows 12 months of dashes/zeros even when income exists. The backend `/api/tax-summary` endpoint does not return per-month breakdown data.
- **Acceptance Criteria**:
  - B&O section shows actual monthly income for Sparkry (12 months)
  - B&O section shows actual quarterly income for BlackLine (4 quarters)
  - Backend `/api/tax-summary` response includes `monthly_income` (array of 12 numbers) and `quarterly_income` (array of 4 numbers)
  - If no income for a period, show $0.00 (not a dash)
  - Test: with $10k income in January and $5k in February, B&O table shows $10,000 for Jan, $5,000 for Feb, $0 for Mar-Dec
- **Files to Change**: `src/api/routes/tax_export.py` (add per-month aggregation to tax-summary endpoint), `dashboard/src/routes/tax/+page.svelte` (consume new data instead of hardcoded zeros)
- **Edge Cases**: Transactions spanning midnight at month boundary, partial months, transactions with null dates
- **Security/Reliability**: Incorrect B&O figures could cause incorrect state tax filing

### REQ-IMP-006: SAP Checklist State Not Persisted on Reload
- **Priority**: P0 (critical bug)
- **Source**: UX, Travis
- **Problem**: The SAP Ariba checklist in `dashboard/src/routes/invoices/+page.svelte` uses local `sapChecklist` state (line 224) that resets to all-unchecked when the page is reloaded. The Invoice model has a `sap_checklist_state` JSON field, but the frontend never reads from it or writes to it. The `initSapChecklist()` function (line 226) always starts fresh.
- **Acceptance Criteria**:
  - Checking a SAP step immediately saves to the invoice's `sap_checklist_state` field via PATCH API
  - Reloading the page restores all previously checked steps
  - When all steps are checked, the "Mark as Submitted" action transitions the invoice to sent status
  - Test: check 3 of 8 steps, reload page, verify those 3 are still checked
- **Files to Change**: `dashboard/src/routes/invoices/+page.svelte` (read/write sap_checklist_state from expandedInvoice), `src/api/routes/invoices.py` (ensure PATCH accepts sap_checklist_state)
- **Edge Cases**: Concurrent edits (two tabs), network failure during save, invoice already in sent status
- **Security/Reliability**: Lost checklist state means Travis has to re-check steps, wasting time and risking missed SAP submission steps

---

## DATA PIPELINE GAPS (P1)

### REQ-IMP-007: Invoice-to-Income Transaction Automation
- **Priority**: P1 (blocks key workflow)
- **Source**: Tax/CPA, Systems Architect
- **Problem**: When an invoice is marked as paid, no corresponding income transaction is automatically created in the register. The invoice system and transaction register are disconnected — invoice payments don't flow into the tax summary or P&L.
- **Acceptance Criteria**:
  - When an invoice is marked paid, an income transaction is automatically created in the register with: source=`invoice` (new Source enum value), entity=`sparkry`, direction=`income`, tax_category=`CONSULTING_INCOME`, amount=invoice total, date=paid_date
  - The invoice's `payment_transaction_id` is set to the new transaction's ID
  - Voiding a paid invoice also rejects the linked income transaction
  - The income transaction description includes the invoice number and customer name (e.g., "Invoice CH20260131 — Cardinal Health, Inc.")
  - Idempotency: if the mark-paid API is called twice for the same invoice, the second call must not create a duplicate income transaction (check payment_transaction_id before creating)
  - Test: mark invoice paid, verify new transaction appears in register with correct amount and category
  - Test: call mark-paid twice, verify only one income transaction exists
- **Files to Change**: `src/api/routes/invoices.py` (status transition to paid), `src/models/enums.py` (add `INVOICE = "invoice"` to Source enum), `src/models/transaction.py`
- **Edge Cases**: Invoice voided after paid (transaction must be rejected), partial payment (not supported in v1 — warn user), duplicate paid transition (idempotent), mark-paid during network interruption (frontend retries)
- **Security/Reliability**: Without this, consulting income is not tracked for taxes

### REQ-IMP-008: Stripe/Shopify Onboarding Flow with Key Validation
- **Priority**: P1 (blocks key workflow)
- **Source**: Systems Architect, Travis
- **Problem**: The health page shows Stripe and Shopify as "Never synced" but provides no guidance on how to configure API keys or trigger a first sync. There is no UI to enter API keys, validate them, or understand what data will be pulled.
- **Acceptance Criteria**:
  - Health page source cards for unconfigured sources show a "Configure" link with setup instructions
  - Setup instructions explain: where to find the API key, what format it should be in, where to add it (.env file)
  - A "Test Connection" button validates the key without running a full sync
  - After successful test, a "Run First Sync" button triggers the adapter
  - Configured sources show last sync time, record count, and next expected sync
  - Test: with no STRIPE_API_KEY_SPARKRY set, health page shows Stripe card with "Not configured — Add STRIPE_API_KEY_SPARKRY to .env"
- **Files to Change**: `dashboard/src/routes/health/+page.svelte`, `src/api/routes/health.py` (add key-status check endpoint), `src/api/routes/ingest.py`
- **Edge Cases**: Invalid key format, key with insufficient permissions, rate limiting during test
- **Security/Reliability**: API keys should never be displayed in the UI or logged; only show configured/not-configured status

### REQ-IMP-009: Bank CSV Import UI on Reconciliation Page
- **Priority**: P1 (blocks key workflow)
- **Source**: Travis, UX
- **Problem**: The reconciliation page exists but there is no way to upload a bank CSV from the dashboard. Users must use the API directly. The reconciliation workflow requires bank data to match against Stripe/Shopify payouts.
- **Acceptance Criteria**:
  - Reconciliation page has an "Import Bank CSV" button that opens a file upload dialog
  - After upload, shows a preview of the first 5 rows with detected column mapping
  - User can adjust column mapping (date, description, amount, reference)
  - User confirms to import; progress indicator shows during processing
  - After import, reconciliation automatically runs to match new bank records
  - Test: upload a Chase bank CSV, verify 5-row preview shows, confirm import, verify transactions appear in register
- **Files to Change**: `dashboard/src/routes/reconciliation/+page.svelte`, `src/api/routes/csv_import.py` (ensure preview endpoint exists)
- **Edge Cases**: Wrong file format, CSV with no header row, encoding issues (Latin-1), duplicate imports of same CSV
- **Security/Reliability**: File size limit (50MB), validate file is actually CSV before processing

---

## TAX COMPLIANCE (P1)

### REQ-IMP-010: Missing Tax Categories (OTHER_EXPENSE, HOME_OFFICE, UTILITIES)
- **Priority**: P1 (blocks key workflow)
- **Source**: Tax/CPA
- **Problem**: The `TaxCategory` enum in `src/models/enums.py` is missing several common Schedule C categories: OTHER_EXPENSE (Line 27), HOME_OFFICE (Line 30/Form 8829), UTILITIES (Line 25), RENT (Line 20b), DEPRECIATION (Line 13), REPAIRS (Line 21), INTEREST_BUSINESS (Line 16). Travis likely has home office and utility expenses that cannot be properly categorized.
- **Acceptance Criteria**:
  - TaxCategory enum includes: OTHER_EXPENSE, HOME_OFFICE, UTILITIES, RENT, DEPRECIATION, REPAIRS, INTEREST_BUSINESS
  - IRS_LINE_MAPPING in `tax_export.py` maps them to correct Schedule C lines
  - Dashboard category dropdowns include the new categories
  - Existing transactions are not affected (new categories are additive)
  - Test: create a transaction with HOME_OFFICE category, verify it appears under Line 30 in tax summary
- **Files to Change**: `src/models/enums.py`, `src/api/routes/tax_export.py` (IRS_LINE_MAPPING), `dashboard/src/routes/register/+page.svelte` (ALL_CATEGORIES), `dashboard/src/lib/components/TransactionCard.svelte`
- **Edge Cases**: Vendor rules referencing old categories should still work, migration for existing data
- **Security/Reliability**: Missing categories means expenses are miscategorized, affecting tax filing accuracy

### REQ-IMP-011: 1099 Income Tracking and Reporting
- **Priority**: P1 (blocks key workflow)
- **Source**: Tax/CPA
- **Problem**: The system tracks income but does not identify which income sources will generate 1099s. For Schedule C, the IRS requires reporting each 1099 source separately. Travis receives 1099-NEC from Cardinal Health and potentially 1099-K from Stripe/Shopify.
- **Acceptance Criteria**:
  - Transactions can be tagged with a 1099 source (payer name, EIN if known)
  - Tax summary shows a 1099 income breakdown section: which payers, how much per payer
  - FreeTaxUSA export includes 1099-NEC and 1099-K data
  - Warning if total income exceeds sum of 1099s (indicates unreported income sources)
  - Test: tag $33k Cardinal Health income as 1099-NEC, verify tax summary shows 1099 breakdown
- **Files to Change**: `src/models/transaction.py` (add 1099_source field or use raw_data), `src/api/routes/tax_export.py`, `src/export/freetaxusa.py`, `dashboard/src/routes/tax/+page.svelte`
- **Edge Cases**: Multiple 1099s from same payer in different amounts, corrected 1099s, income below $600 threshold
- **Security/Reliability**: EIN storage should be treated as sensitive data

### REQ-IMP-012: Quarterly Estimated Tax Payment Tracking
- **Priority**: P1 (blocks key workflow)
- **Source**: Tax/CPA, Travis
- **Problem**: Self-employed individuals (Travis via Sparkry) must make quarterly estimated tax payments (Form 1040-ES). The system has no way to track these payments or remind Travis of upcoming deadlines.
- **Acceptance Criteria**:
  - Tax page shows quarterly estimated tax section with: Q1 (Apr 15), Q2 (Jun 15), Q3 (Sep 15), Q4 (Jan 15 next year)
  - Each quarter shows: estimated tax due based on YTD income, amount already paid, remaining balance
  - Health page tax deadline calendar includes estimated tax due dates
  - Estimated tax payments are categorized as TAXES_AND_LICENSES with a subcategory of `estimated_tax`
  - Test: with $100k YTD income, quarterly estimated tax section shows approximate amount due
- **Files to Change**: `dashboard/src/routes/tax/+page.svelte`, `src/api/routes/tax_export.py`, `src/models/enums.py` (add estimated_tax subcategory)
- **Edge Cases**: Travis may use prior year safe harbor method, state estimated taxes (WA has no income tax but has B&O)
- **Security/Reliability**: Late estimated tax payments incur penalties; reminders are important

### REQ-IMP-013: B&O Per-Month API Breakdown
- **Priority**: P1 (blocks key workflow)
- **Source**: Tax/CPA, Travis
- **Problem**: The `/api/tax-summary` endpoint returns only aggregate totals, not per-month or per-quarter breakdown needed for B&O filing. The B&O export CSV does have month-by-month data, but the dashboard cannot display it inline without a separate API call.
- **Acceptance Criteria**:
  - `/api/tax-summary` response includes `bno_breakdown` array with one entry per period (12 for Sparkry monthly, 4 for BlackLine quarterly)
  - Each entry has: period label, gross income, taxable amount, B&O classification code
  - Dashboard B&O section consumes this data instead of hardcoded zeros
  - Test: with income in Jan and Feb, API returns 12-element array with values for Jan/Feb and zeros for Mar-Dec
- **Files to Change**: `src/api/routes/tax_export.py` (add bno_breakdown to tax-summary response), `dashboard/src/routes/tax/+page.svelte` (consume bno_breakdown)
- **Edge Cases**: No income in any month, income only in Q4, transactions with null entity
- **Security/Reliability**: Incorrect B&O amounts = incorrect state tax filing

---

## UX IMPROVEMENTS (P1/P2)

### REQ-IMP-014: Review Queue Compact/Density Mode
- **Priority**: P1 (blocks key workflow — Travis wants to clear queue in 15 min)
- **Source**: UX, Travis
- **Problem**: Each review queue card takes significant vertical space. With 20-30 items to review weekly, Travis must scroll extensively. A compact mode showing more items per screen would improve throughput.
- **Acceptance Criteria**:
  - Toggle between "Comfortable" (current) and "Compact" mode
  - Compact mode shows: date, vendor, amount, entity dropdown, category dropdown, confirm button — all on one line
  - Compact mode fits 15+ items per viewport (vs ~5 in comfortable)
  - User preference persisted in localStorage
  - Keyboard shortcuts still work in compact mode
  - Test: toggle to compact mode, verify 15+ items visible without scrolling on 1080p display
- **Files to Change**: `dashboard/src/routes/+page.svelte`, `dashboard/src/lib/components/TransactionCard.svelte`
- **Edge Cases**: Very long vendor names (truncate with tooltip), narrow viewport, items with attachments
- **Security/Reliability**: No concern

### REQ-IMP-015: Review Queue Priority Grouping Headers
- **Priority**: P2 (polish)
- **Source**: UX
- **Problem**: The review queue sorts by priority (errors, duplicates, low confidence, first-time vendors) but has no visual grouping headers to tell Travis which section he is in.
- **Acceptance Criteria**:
  - Visual section headers appear between priority groups: "Amount Errors", "Duplicate Suspects", "Low Confidence", "New Vendors", "Other"
  - Headers show count per group (e.g., "Duplicate Suspects (3)")
  - Collapsed groups can be expanded/collapsed
  - Test: with mixed priority items, verify headers appear at group boundaries
- **Files to Change**: `dashboard/src/routes/+page.svelte`
- **Edge Cases**: Only one priority group, empty groups (hide header), all items in same group
- **Security/Reliability**: No concern

### REQ-IMP-016: Health Page Source Configuration Guidance
- **Priority**: P1 (blocks key workflow)
- **Source**: UX, Travis
- **Problem**: The health page shows source freshness cards but unconfigured sources (Stripe, Shopify, Brokerage) show as "Never synced" with no guidance on what to do next. Travis needs to know: is this expected? How do I set it up?
- **Acceptance Criteria**:
  - Unconfigured sources show a "Setup Required" badge instead of "Never synced"
  - Clicking the badge shows inline instructions (API key location, .env format, sample value)
  - Configured but never-synced sources show "Ready — Run first sync" with a sync button
  - Sources that require external files (bank CSV, brokerage CSV) show "Import via Reconciliation page" with a link
  - Test: with no Stripe keys configured, health page shows "Setup Required" with instructions
- **Files to Change**: `dashboard/src/routes/health/+page.svelte`, `src/api/routes/health.py` (add config status per source)
- **Edge Cases**: Partially configured (one Stripe key but not the other), key in .env but invalid
- **Security/Reliability**: Never display actual key values

### REQ-IMP-017: Rejected Transactions Hidden by Default
- **Priority**: P2 (polish)
- **Source**: UX, Travis
- **Problem**: Rejected transactions clutter the register view. Most of the time Travis wants to see active transactions only. Rejected items should be hidden by default with a toggle to show them.
- **Acceptance Criteria**:
  - Register defaults to hiding rejected transactions (status filter excludes "rejected")
  - A "Show rejected" toggle in the filter bar reveals them with visual distinction (strikethrough, dimmed)
  - Register count and summary totals exclude rejected by default
  - Test: with 5 rejected and 20 confirmed, register shows 20 by default; toggle shows all 25
- **Files to Change**: `dashboard/src/routes/register/+page.svelte`
- **Edge Cases**: All transactions are rejected (show empty state with hint), filter combination with status=rejected
- **Security/Reliability**: No concern

### REQ-IMP-018: Tax Year Defaults to Filing Year
- **Priority**: P2 (polish)
- **Source**: Tax/CPA, Travis
- **Problem**: The tax page year selector defaults to `new Date().getFullYear()` (2026). During tax season (Jan-Apr), Travis is filing for the prior year (2025). The default should be the likely filing year.
- **Acceptance Criteria**:
  - Between January 1 and April 15, default year is previous year (filing year)
  - After April 15, default year is current year
  - User can still manually select any year
  - Test: on March 18 2026, tax page defaults to 2025; on May 1 2026, defaults to 2026
- **Files to Change**: `dashboard/src/routes/tax/+page.svelte` (line 13, CURRENT_YEAR logic)
- **Edge Cases**: Extension filers (Oct 15 deadline), year selector range should include prior years
- **Security/Reliability**: No concern

---

## MISSING FEATURES (P2)

### REQ-IMP-019: AR Aging Widget on Review Page
- **Priority**: P2 (polish)
- **Source**: Travis
- **Problem**: The review queue page does not show outstanding invoices. Travis wants to see at a glance if any invoices are overdue while doing his weekly review.
- **Acceptance Criteria**:
  - A small AR summary card appears at the top of the review page (above the review queue)
  - Shows: number of outstanding invoices, total amount, oldest days outstanding
  - Clicking the card navigates to the invoices page
  - Only shows when there are outstanding invoices (hidden when all paid)
  - Test: with one $33k invoice 45 days outstanding, card shows "1 invoice outstanding: $33,000 (45 days)"
- **Files to Change**: `dashboard/src/routes/+page.svelte`, `dashboard/src/lib/api.ts` (fetch outstanding invoices count)
- **Edge Cases**: No outstanding invoices (hide card), many outstanding invoices (show summary not list)
- **Security/Reliability**: No concern

### REQ-IMP-020: Duplicate Invoice Protection Warning
- **Priority**: P2 (polish)
- **Source**: Systems Architect
- **Problem**: The flat-rate invoice generator checks for duplicates (UNIQUE on customer_id + service_period_start) but returns a generic 422 error. The UI should show a clear warning before the user even clicks Generate.
- **Acceptance Criteria**:
  - Before generating, the UI checks if an invoice already exists for the selected month
  - If exists, shows: "Invoice CH20260228 already exists for February 2026" with a link to view it
  - Generate button is disabled when duplicate exists
  - For calendar-based invoices, shows which sessions are already billed
  - Test: with March invoice existing, Generate button shows "Already invoiced" and links to existing invoice
- **Files to Change**: `dashboard/src/routes/invoices/+page.svelte`, `src/api/routes/invoices.py` (add check endpoint or return existing in error response)
- **Edge Cases**: Voided invoice for same month (should allow re-generation), invoice in draft status
- **Security/Reliability**: Prevents accidental double-billing

### REQ-IMP-021: Year-Over-Year Comparison on Tax Page
- **Priority**: P2 (polish)
- **Source**: Tax/CPA
- **Problem**: Travis cannot compare this year's income/expenses to last year to spot anomalies. A simple side-by-side comparison would help during tax preparation.
- **Acceptance Criteria**:
  - Tax page has a "Compare with [prior year]" toggle
  - When enabled, each category row shows current year and prior year amounts side by side
  - Delta column shows increase/decrease with color coding (green for reduced expenses, red for increased)
  - Gross income, total expenses, and net profit show year-over-year comparison
  - Test: with 2025 data ($100k income) and 2024 data ($80k income), comparison shows +$20k / +25%
- **Files to Change**: `dashboard/src/routes/tax/+page.svelte`, `src/api/routes/tax_export.py` (allow fetching two years)
- **Edge Cases**: No prior year data (show "N/A"), first year of business, new categories that didn't exist prior year
- **Security/Reliability**: No concern

### REQ-IMP-022: Tax Year Locking
- **Priority**: P2 (polish)
- **Source**: Tax/CPA
- **Problem**: After filing taxes for a year, transactions in that year should be protected from accidental changes. Currently, any confirmed transaction can be re-edited at any time.
- **Acceptance Criteria**:
  - Accounts page has a "Lock Tax Year" button per entity per year
  - Locked years prevent: editing, reclassifying, splitting, rejecting transactions
  - Locked status shown in register (lock icon on rows)
  - Unlock requires explicit confirmation: "Unlock 2025 for Sparkry? This allows edits to filed tax data."
  - Test: lock 2025 for Sparkry, try to edit a 2025 Sparkry transaction, verify it returns 403
- **Files to Change**: `src/models/` (new TaxYearLock model), `src/api/routes/transactions.py` (check lock before edit), `dashboard/src/routes/accounts/+page.svelte`
- **Edge Cases**: Amended returns (need to unlock), transactions spanning year boundaries (date determines year), split parent in locked year with children in unlocked year
- **Security/Reliability**: Protects filed tax data from accidental modification

### REQ-IMP-023: Claude API Cost Tracking
- **Priority**: P2 (polish)
- **Source**: Travis, Systems Architect
- **Problem**: The health page mentions "Claude API calls this month and estimated cost" in the plan but this is not implemented. Travis wants to know how much the LLM classification is costing.
- **Acceptance Criteria**:
  - Each Claude API call logs: timestamp, model, input/output tokens, cost estimate
  - Health page shows: total calls this month, total tokens, estimated cost
  - Cost estimate uses current Claude pricing (Haiku $0.25/1M input, $1.25/1M output)
  - Monthly trend (last 3 months) shown as a simple bar chart
  - Test: after 10 classification calls, health page shows call count and estimated cost
- **Files to Change**: `src/classification/llm_classifier.py` (log API usage), `src/models/` (new LLMUsageLog model), `src/api/routes/health.py`, `dashboard/src/routes/health/+page.svelte`
- **Edge Cases**: Classification that uses fallback (no cost), retries (count each attempt), rate limiting
- **Security/Reliability**: No concern; cost tracking only

### REQ-IMP-024: Bank CSV Import Button on Reconciliation Page
- **Priority**: P2 (polish — overlaps with REQ-IMP-009)
- **Source**: Travis
- **Problem**: The reconciliation page shows matched and unmatched pairs but provides no way to import new bank data from the page itself. The user must navigate to a separate import flow.
- **Acceptance Criteria**:
  - Reconciliation page has "Import Bank Statement" button in the header
  - Opens the same CSV upload flow as REQ-IMP-009
  - After successful import, reconciliation automatically re-runs
  - Shows import results: N new transactions imported, M matched to payouts
  - Test: from reconciliation page, import a CSV, verify new matches appear
- **Files to Change**: `dashboard/src/routes/reconciliation/+page.svelte`
- **Edge Cases**: Import while reconciliation is running (queue the import)
- **Security/Reliability**: Same as REQ-IMP-009

### REQ-IMP-025: Review Queue Batch Confirm with Entity/Category Preset
- **Priority**: P1 (blocks key workflow)
- **Source**: Travis, UX
- **Problem**: When multiple transactions from the same vendor need the same entity/category, Travis must confirm them one at a time. A batch confirm would let him select several, set entity/category once, and confirm all.
- **Acceptance Criteria**:
  - Checkbox on each review queue card for multi-select
  - Shift+click for range selection
  - "Batch Confirm" button appears when 2+ items selected
  - Batch confirm dialog: set entity, category, direction — applies to all selected
  - API: POST /api/transactions/bulk-confirm accepts list of IDs with entity/category (max 100 per request)
  - Each confirmed transaction triggers the learning loop (creates/updates VendorRule)
  - Progress indicator during batch confirm
  - Test: select 5 transactions, batch confirm as sparkry/OFFICE_EXPENSE, verify all 5 are confirmed and VendorRules created
- **Files to Change**: `dashboard/src/routes/+page.svelte`, `src/api/routes/transactions.py` (add bulk-confirm endpoint)
- **Edge Cases**: Some items in batch fail validation (partial success with per-item results), items already confirmed (skip, don't error), mixed entities in selection (apply the batch entity to all), batch exceeds 100 items (return 422)
- **Security/Reliability**: Bulk operations should report partial results clearly. Maximum batch size of 100 prevents abuse. Each item validated individually.

### REQ-IMP-026: Dashboard Keyboard Shortcut Help Overlay
- **Priority**: P2 (polish)
- **Source**: UX
- **Problem**: The dashboard has many keyboard shortcuts (y, e, s, d, r, j, k, 1, 2, 3, c, ?) but no way to discover them other than reading documentation. A help overlay would improve discoverability.
- **Acceptance Criteria**:
  - Pressing `?` shows a modal with all keyboard shortcuts organized by context (Review, Register, Navigation)
  - Modal dismissed by pressing `?` again or Escape
  - Shortcuts shown with key symbol and description
  - Footer of the dashboard shows a subtle "Press ? for shortcuts" hint
  - Test: press ? on review page, verify overlay shows with all shortcuts listed
- **Files to Change**: `dashboard/src/routes/+page.svelte`, `dashboard/src/lib/components/KeyboardShortcutHelp.svelte` (new component)
- **Edge Cases**: Overlay should not interfere with shortcut keys when an input is focused
- **Security/Reliability**: No concern

---

## P1: DASHBOARD INSIGHTS (from Travis walkthrough Mar 18, 2026)

### REQ-IMP-030: Summary Card Drill-Down with Graphs
- **Priority**: P1 (key workflow improvement)
- **Source**: Travis
- **Problem**: The register page summary cards (Income, Expenses, Net, Transactions) are static numbers with no ability to explore. Travis wants to click a card and see: a line graph of that metric over time, breakdown by top sources/vendors, and key trends.
- **Acceptance Criteria**:
  - Clicking Income/Expenses/Net card expands to a detail panel below the cards
  - Line graph showing daily/weekly/monthly totals over the selected date range (use SVG or canvas — no heavy charting library)
  - Top 5 sources/vendors breakdown as a simple bar chart or ranked list with amounts
  - Month-over-month change percentage shown
  - Panel collapsible (click card again or X to close)
  - Graph responds to existing date/entity filters
  - Test: filter to Sparkry + 2026, click Income card, verify graph shows Stripe income over months
- **Files to Change**: `dashboard/src/routes/register/+page.svelte` (add expandable panels), `src/api/routes/transactions.py` (add aggregation endpoint if needed)
- **Edge Cases**: No data for selected period (show empty state), single day of data (show single point), very large amounts (auto-scale Y axis)

### REQ-IMP-031: Income Insights Panel
- **Priority**: P1
- **Source**: Travis
- **Problem**: Travis wants to understand his income patterns to make better business decisions — client concentration, growth trends, revenue by source.
- **Acceptance Criteria**:
  - Income card expansion shows: total income, income by client/vendor (pie or bar), income by entity, monthly trend line
  - Client concentration warning: if >80% of income from one client, show amber warning "80% of income from Cardinal Health — diversification risk"
  - Growth rate: "Income up 15% vs last month" or "down 10%"
  - Revenue by source: Stripe charges, bank deposits, invoice payments broken out
  - Test: with Fascinate + Cardinal Health income, verify concentration warning shows

### REQ-IMP-032: Expense Insights Panel
- **Priority**: P1
- **Source**: Travis
- **Problem**: Travis wants to spot expense trends, unusual charges, and opportunities to cut costs.
- **Acceptance Criteria**:
  - Expense card expansion shows: total expenses, top 5 categories with amounts and % of total, monthly trend line
  - Unusual charge detection: flag any single charge >2x the average for that vendor/category with "Unusual: $633 at Residence Inn (avg $8 for Travel)"
  - Month-over-month change: "Expenses up $2,300 vs last month — mainly from Travel (+$1,400)"
  - Largest single expense highlighted
  - Test: with hotel charges and normal SaaS charges, verify unusual detection fires for hotel

### REQ-IMP-033: Tax Optimization Insights
- **Priority**: P1
- **Source**: Travis, CPA review
- **Problem**: Travis is leaving money on the table — missed deductions, unclaimed home office, no estimated tax planning. The system should proactively surface these.
- **Acceptance Criteria**:
  - Tax page (or dedicated insights section) shows actionable tax tips:
  - "Home office deduction: You qualify for $180/year (36 sqft × $5). Not yet claimed."
  - "Estimated tax: Based on $33K/mo income, your Q1 estimated payment should be ~$X"
  - "Reimbursable expenses: $497 in Cardinal Health expenses pending reimbursement (30+ days)"
  - "Unlinked income: 3 Stripe charges ($3,700) not matched to invoices"
  - "Vehicle/mileage: No car expenses recorded — do you drive for business?"
  - Each insight is dismissible (don't show again) or actionable (link to fix)
  - Test: with 36 sqft configured and no home office transactions, verify the tip appears

---

## REVIEW CYCLE SUMMARY

Two review cycles were conducted with 4 personas (Business Analyst, Principal Engineer, Security/Reliability Expert, UX/Usability Expert).

### Cycle 1 Findings (incorporated):
- **P0 (Engineer)**: `_monthly_income` function used `tx.amount or 0` which breaks for `Decimal("0")` (falsy). Fixed to use `is not None` check. Also changed to filter by `INCOME_CATEGORIES` instead of `direction == "income"` since B&O is based on gross receipts by tax category.
- **P0 (Security)**: Invoice mark-paid idempotency gap — calling twice could create duplicate income transactions. Added `payment_transaction_id` guard.
- **P1 (BA)**: REQ-IMP-002 mixed two distinct issues (frontend string parsing + backend abs logic). Clarified as issues A and B with separate acceptance criteria.
- **P1 (Engineer)**: Source enum missing `INVOICE` value for auto-created income transactions. Added to requirements and design.
- **P1 (BA)**: Bulk confirm endpoint missing learning loop requirement. Added VendorRule upsert and max batch size of 100.
- **P1 (UX)**: UC-009 reimbursement linking too manual — added suggested matches feature.
- **P1 (BA)**: Missing use case for invoice payment receipt. Added UC-011.
- **P1 (Engineer)**: Tax year lock performance concern for bulk operations. Added cache-per-request strategy to design.
- **P1 (BA)**: Estimated tax calculation formula unspecified. Added to design doc.

### Cycle 2 Findings (P2 only — convergence reached):
- P2 (BA): No requirement for "all months invoiced" edge case in Generate button
- P2 (Engineer): REQ-IMP-001 and REQ-IMP-002 should be implemented together (noted in design)
- P2 (Security): SAP checklist PATCH should reject updates on voided invoices
- P2 (Security): LLM usage log should not store full prompts (privacy)
- P2 (UX): UC-012 estimated tax payment recording workflow unclear

---

## TRAVIS'S ANSWERS (Mar 18, 2026)

1. **B&O filing**: Confirmed monthly for Sparkry, quarterly for BlackLine. Sparkry due Mar 25 for Feb. Uses WA DOR data upload format (ACCOUNT/TAX/DED tags in CSV). See REQ-IMP-027.
2. **Estimated tax**: Not currently paying but will need to. Income is consistent (~$33K/mo). Will use safe harbor method.
3. **Home office**: Has a 6'x6' corner of living room. Open to simplified method: $5 × 36 sqft = $180/year. See REQ-IMP-028.
4. **1099 sources**: Cardinal Health (1099-NEC), Stripe/Shopify (1099-K), WooCommerce entries from 2025 (downloaded CSV, site not live). See REQ-IMP-029.
5. **Tax year locking**: Important but can be one of the last items implemented.

---

## ADDITIONAL REQUIREMENTS (from Travis's answers)

### REQ-IMP-027: WA DOR B&O Upload Format Export
- **Priority**: P0 (Sparkry B&O due Mar 25 — 7 days away)
- **Source**: Travis (WA DOR specification)
- **Problem**: Current B&O export produces a summary CSV. WA DOR requires a specific upload format with ACCOUNT/TAX/DED tags, line codes, and no dollar signs/commas.
- **Acceptance Criteria**:
  - `GET /api/export/bno?entity=sparkry&year=2026&month=2` returns DOR-formatted .txt file
  - Format: ACCOUNT line with account ID, period (MMYYYY), preparer name, email, phone
  - TAX lines with line code (e.g., 2 for Retailing, service code for consulting), location code 0, amount (no $ or commas)
  - DED lines for any applicable deductions
  - Lines starting with # are comments (not uploaded)
  - File saved as .txt (comma delimited) or .csv
  - Dashboard B&O export button downloads this format directly
  - Test: generate DOR file for Feb 2026 with $33K consulting income, verify line codes and format
- **Files to Change**: `src/export/bno_tax.py` (add DOR format output), `src/api/routes/tax_export.py` (add month param), `dashboard/src/routes/tax/+page.svelte` (month selector for B&O download)
- **Edge Cases**: Rate changes within reporting period (must be entered manually per DOR docs), multiple tax classifications in same period
- **Security/Reliability**: Incorrect format = upload rejection by DOR. Must match exactly.
- **Reference**: WA DOR upload instructions at /Users/travis/Downloads/WADORData_Upload_instr.pdf
- **Needs from Travis**: Sparkry account ID and BlackLine account ID for the ACCOUNT line

### REQ-IMP-028: Home Office Deduction (Simplified Method)
- **Priority**: P2 (small amount but free deduction)
- **Source**: Travis (6'x6' = 36 sqft, simplified method)
- **Problem**: No HOME_OFFICE tax category exists. Travis has a qualifying home office (36 sqft) worth $180/year via simplified method ($5/sqft).
- **Acceptance Criteria**:
  - HOME_OFFICE added to TaxCategory enum
  - Tax summary shows home office deduction as IRS Form 8829 / Simplified Method line
  - FreeTaxUSA export includes home office deduction amount
  - System auto-calculates: 36 sqft × $5 = $180 per year (configurable sqft in entity config)
  - Test: tax summary for Sparkry shows $180 home office deduction
- **Files to Change**: `src/models/enums.py`, `src/export/freetaxusa.py`, `src/api/routes/tax_export.py`, `dashboard/src/routes/accounts/+page.svelte` (sqft config)

### REQ-IMP-029: WooCommerce CSV Import
- **Priority**: P2 (historical 2025 data)
- **Source**: Travis
- **Problem**: Travis has WooCommerce order CSVs from 2025 (downloaded, site no longer live) that need to be imported as BlackLine income.
- **Acceptance Criteria**:
  - Bank CSV adapter extended to handle WooCommerce export format (or new dedicated parser)
  - WooCommerce orders tagged as BlackLine entity, SALES_INCOME category
  - POST /api/import/woocommerce-csv accepts file upload
  - Test: import sample WooCommerce CSV, verify transactions created with correct entity/category
- **Files to Change**: `src/adapters/bank_csv.py` or new `src/adapters/woocommerce_csv.py`, `src/api/routes/csv_import.py`
