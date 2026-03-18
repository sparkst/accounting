# V2 Improvements — Use Cases

> Practical use cases for Travis Sparks. Every use case minimizes effort — Travis doesn't like doing a lot of work.

---

## UC-001: Monday Morning Transaction Review

**Actor**: Travis
**Goal**: Clear the review queue in under 15 minutes
**Frequency**: Weekly (Monday morning)
**Precondition**: n8n has been ingesting Gmail receipts throughout the week

**Steps**:
1. Open dashboard (localhost:5173) — review queue is the landing page
2. Glance at the AR aging card at top: "1 invoice outstanding: $33,000 (45 days)" — no action needed yet
3. Toggle to Compact mode (if not already set)
4. See priority grouping headers: "Amount Errors (1)", "Low Confidence (3)", "New Vendors (2)", "Pending (18)"
5. For the Amount Error item: click the OCR button to extract receipt data, then confirm
6. For Low Confidence items: review pre-filled entity/category, adjust if wrong, press `y` to confirm
7. For a batch of 5 AWS charges all going to Sparkry/OFFICE_EXPENSE: Shift+click to select all 5, click "Batch Confirm", set entity=Sparkry, category=Office Expense, confirm
8. For new vendors: set entity and category, press `y` — the learning loop creates a VendorRule so next time it auto-classifies
9. Press `?` to check keyboard shortcuts if needed
10. Review queue is empty: "All caught up" message appears

**Expected Result**: 20-30 items reviewed and confirmed in 10-15 minutes. New vendor rules created automatically for future transactions.
**Time**: ~15 minutes for 20-30 items (target: <1 min per item average)

---

## UC-002: Monthly Cardinal Health Invoice Generation

**Actor**: Travis
**Goal**: Generate the monthly $33,000 invoice for Cardinal Health and submit via SAP Ariba
**Frequency**: Monthly (first week of month)
**Precondition**: Prior month's invoice exists and has been paid or is in sent status

**Steps**:
1. Navigate to Invoices page
2. In the "Generate Next Invoice" section, find the Cardinal Health card showing "Flat rate - $33,000.00/mo"
3. Verify the button shows the correct month (e.g., "Generate March 2026")
4. Click "Generate March 2026" — invoice is created and detail panel expands immediately
5. Review auto-filled fields: invoice number (CH20260331), service period (Mar 2-31), amount ($33,000), description ("AI Product Engineering Coaching Month 3")
6. SAP Ariba Instructions panel appears below the invoice detail
7. Open SAP Ariba in another tab
8. Follow the checklist, checking each step as completed:
   - Log into SAP Ariba (check)
   - Open existing order PO# 4700158965 (check)
   - Find the most recent invoice and copy it (check)
   - Update service period (Mar 2, 2026 - Mar 31, 2026) (check)
   - Update description ("AI Product Engineering Coaching Month 3") (check)
   - Enter Sparkry invoice number (CH20260331) (check)
   - Verify amount ($33,000.00) (check)
   - Submit (check)
9. All checkboxes checked — "All steps completed" banner appears
10. Click "Mark Sent" — invoice status changes to "sent"
11. Download PDF for records (optional)

**Expected Result**: Invoice generated, submitted in SAP, marked as sent. Checklist state persists if Travis needs to come back later. Expected payment in ~90 days.
**Time**: ~10 minutes (most time is in SAP Ariba, not the dashboard)

---

## UC-003: Monthly Fascinate Invoice Generation

**Actor**: Travis
**Goal**: Generate an hourly invoice for How To Fascinate based on calendar meetings
**Frequency**: Monthly (end of month)
**Precondition**: Google Calendar has meetings with Ben during the billing period

**Steps**:
1. Navigate to Invoices page
2. Click "Create Invoice" next to How To Fascinate
3. On the new invoice page, upload the iCal (.ics) export from Google Calendar
4. System parses the file and shows filtered billable sessions:
   - Sessions matching "Ben / Travis", "Fascinate OS", "Fascinate" are shown
   - "Book with Ben" entries are excluded
   - Each session shows: date, time, duration (1 hour), description
5. Review sessions — uncheck any that shouldn't be billed (e.g., a social call)
6. Note: any sessions already billed on a prior invoice are shown dimmed with "(Already on invoice 202602-001)"
7. Verify the subtotal (e.g., 8 sessions x $100/hr = $800)
8. Click "Generate Invoice"
9. Invoice created with number 202603-001, line items for each session
10. Download PDF and send to Ben

**Expected Result**: Calendar-based invoice generated with correct sessions, no double-billing. PDF matches Sparkry template.
**Time**: ~5 minutes

---

## UC-004: Monthly Sparkry B&O Filing

**Actor**: Travis
**Goal**: File monthly Washington State B&O tax for Sparkry AI LLC
**Frequency**: Monthly (due 25th of following month)
**Precondition**: All Sparkry income for the month is confirmed in the register

**Steps**:
1. Navigate to Tax Summary page
2. Select entity: Sparkry AI, year: 2026
3. Check readiness: should be 90%+ for the filing month
4. If unconfirmed items exist, click "Review N unconfirmed items" to clear them first
5. Scroll to B&O Revenue Subtotals — see monthly breakdown with actual income figures
6. Note the income for the filing month (e.g., February 2026: $43,000)
7. Click "Download B&O Report" — CSV downloads with all months
8. Open the WA Department of Revenue site, enter the monthly income figure
9. File the return

**Expected Result**: Accurate monthly income figure available for B&O filing without manual calculation.
**Time**: ~5 minutes (plus time on DOR website)

---

## UC-005: Quarterly BlackLine B&O Filing

**Actor**: Travis
**Goal**: File quarterly Washington State B&O tax for BlackLine MTB LLC
**Frequency**: Quarterly (due end of month following quarter end)
**Precondition**: All BlackLine income for the quarter is confirmed

**Steps**:
1. Navigate to Tax Summary page
2. Select entity: BlackLine MTB, year: 2026
3. B&O section shows quarterly breakdown (Q1, Q2, Q3, Q4)
4. Note the quarterly income (e.g., Q1 2026: $12,500 from Shopify)
5. Download B&O Report CSV for reference
6. File on WA DOR website

**Expected Result**: Accurate quarterly income figure for BlackLine B&O filing.
**Time**: ~5 minutes

---

## UC-006: Annual Tax Preparation

**Actor**: Travis
**Goal**: Prepare data for annual tax filing (Schedule C, 1065, Schedule A, Schedule D)
**Frequency**: Annually (January - April)
**Precondition**: All transactions for the tax year are confirmed

**Steps**:
1. Navigate to Tax Summary page, verify year defaults to prior year (e.g., 2025 during Jan-Apr 2026)
2. For Sparkry AI (Schedule C):
   a. Check readiness: must be 100% or close
   b. Review IRS line-item breakdown — verify income and expense categories look reasonable
   c. Check 1099 summary: Cardinal Health $99k (NEC), Stripe $21k (K)
   d. Note net profit for Schedule SE calculation
   e. Optionally toggle "Compare with 2024" to spot anomalies
   f. Download FreeTaxUSA export
3. For BlackLine MTB (Form 1065):
   a. Switch entity tab
   b. Review income (Shopify sales) and expenses (COGS, shipping, events)
   c. Download TaxAct export
4. For Personal (Schedule A/D):
   a. Switch entity tab
   b. Review charitable contributions, mortgage interest, state/local tax
   c. Review investment income / capital gains
   d. Download FreeTaxUSA export
5. After filing, lock the tax year: go to Accounts page, click "Lock 2025" for each entity

**Expected Result**: All tax data organized by entity with IRS line numbers. Exports match filing software import formats. Tax year locked after filing.
**Time**: ~30 minutes for review and export (actual filing takes longer)

---

## UC-007: Bank Statement Reconciliation

**Actor**: Travis
**Goal**: Verify that all Stripe/Shopify payouts match bank deposits
**Frequency**: Monthly
**Precondition**: Stripe/Shopify data ingested, bank statement CSV available

**Steps**:
1. Navigate to Reconciliation page
2. Click "Import Bank Statement" button
3. Upload Chase bank CSV
4. Preview shows first 5 rows with auto-detected columns (date, description, amount)
5. Adjust column mapping if needed, click "Confirm Import"
6. System imports transactions and automatically runs reconciliation
7. Results appear:
   - Green: Matched pairs (Stripe payout $1,245 matched to Chase deposit $1,245 on same date)
   - Amber: Unmatched payouts (no corresponding bank deposit yet)
   - Amber: Unmatched deposits (bank deposit with no known source)
8. For unmatched items: click to manually match if it's a timing difference
9. Check monthly totals at bottom: Stripe payouts $4,500, Bank deposits $4,500 — balanced

**Expected Result**: All payouts matched to bank deposits. Discrepancies identified and resolved.
**Time**: ~10 minutes

---

## UC-008: New Vendor Classification

**Actor**: Travis
**Goal**: Classify a transaction from a vendor never seen before
**Frequency**: As needed (typically during Monday review)
**Precondition**: Transaction appears in review queue with status needs_review

**Steps**:
1. In review queue, see a new vendor: "Minuteman Press - $145.00"
2. Travis knows this is a BlackLine printing expense
3. Set entity dropdown to "BlackLine" (or press `2`)
4. Set category to "Advertising" (or press `c` and type "adv")
5. Press `y` to confirm
6. System creates a VendorRule: pattern="Minuteman Press", entity=blackline, category=ADVERTISING, confidence=0.80
7. Next time a Minuteman Press charge appears, it auto-classifies (no review needed)

**Expected Result**: Transaction confirmed, vendor rule created automatically. Future transactions from this vendor will be auto-classified.
**Time**: ~10 seconds per vendor

---

## UC-009: Expense Reimbursement Linking

**Actor**: Travis
**Goal**: Link a Cardinal Health reimbursable expense to its reimbursement payment
**Frequency**: As needed (when reimbursement arrives)
**Precondition**: Reimbursable expense exists in register, reimbursement payment received

**Steps**:
1. In the review queue or register, notice a new income transaction from Cardinal Health for a reimbursable amount
2. Click the income transaction, expand detail
3. Click "Link Reimbursement" — system shows suggested matches based on amount proximity and unlinked reimbursable expenses
4. See suggestions: "FedEx shipping -$45.00 (reimbursable, 22 days ago)" — click to select
5. If no suggestion matches, use the search bar to find the original expense manually
6. System validates: expense is reimbursable, income is income, amounts match (or close)
7. Both transactions are linked bidirectionally
8. P&L impact: both net to zero (expense -$45 + income +$45 = $0)

**Expected Result**: Expense and reimbursement linked, both net to zero on P&L. No tax impact for pass-through expenses.
**Time**: ~2 minutes

**Note**: If a reimbursable expense is older than 30 days with no reimbursement linked, it appears in the "Overdue Reimbursables" section of the review queue (GET /api/transactions?direction=reimbursable&overdue=true).

---

## UC-010: Transaction Splitting (Hotel)

**Actor**: Travis
**Goal**: Split a hotel charge into room (travel deductible) and meals (50% deductible) components
**Frequency**: Occasional (after business travel)
**Precondition**: Hotel transaction exists in register with total amount

**Steps**:
1. In the review queue, see a hotel charge: "Marriott Seattle - $342.80"
2. Press `s` to open the split panel (or click "Split")
3. System detects "Marriott" as a hotel keyword and pre-populates two rows:
   - Room: $274.24 (80% estimate), entity=Sparkry, category=TRAVEL
   - Meals: $68.56 (20% estimate), entity=Sparkry, category=MEALS, deductible_pct=0.50
4. Travis adjusts amounts based on the actual receipt (folio breakdown)
5. Running sum shows green check when amounts match parent total ($342.80)
6. Click "Apply Split"
7. Parent becomes split_parent, two children created with their own classifications
8. Confirm each child transaction

**Expected Result**: Hotel charge split into deductible components. Room is 100% deductible under TRAVEL (Line 24a), meals are 50% deductible under MEALS (Line 24b).
**Time**: ~2 minutes

---

## UC-011: Receiving Payment for an Invoice

**Actor**: Travis
**Goal**: Record that a Cardinal Health invoice has been paid
**Frequency**: Monthly (when ACH arrives, ~90 days after submission)
**Precondition**: Invoice exists in sent or overdue status, payment has arrived in bank account

**Steps**:
1. Navigate to Invoices page
2. See invoice CH20260131 showing as "overdue" (past due date)
3. Click the invoice row to expand detail
4. Click "Mark Paid"
5. System automatically:
   a. Sets invoice status to "paid" with today's date
   b. Creates an income transaction in the register: $33,000, entity=sparkry, category=CONSULTING_INCOME, description="Invoice CH20260131 -- Cardinal Health, Inc."
   c. Links the invoice to the new transaction via payment_transaction_id
6. Toast notification: "Invoice CH20260131 marked paid. Income transaction created."
7. The income transaction appears in the register and will be included in tax summary

**Expected Result**: Invoice marked paid, income transaction auto-created in register. No manual data entry needed.
**Time**: ~30 seconds

**Note**: If Travis accidentally marks the wrong invoice paid, he can void it. Voiding a paid invoice will also reject the auto-created income transaction.

---

## UC-012: Checking Estimated Tax Status (was UC-011)

**Actor**: Travis
**Goal**: See if quarterly estimated tax payments are on track
**Frequency**: Quarterly (before payment deadlines)
**Precondition**: Income transactions are confirmed for the year

**Steps**:
1. Navigate to Tax Summary page, select Sparkry AI
2. Scroll to Estimated Tax section
3. See quarterly breakdown:
   - Q1 (due Apr 15): estimated $4,688 due, $0 paid — **action needed**
   - Q2 (due Jun 15): estimated $4,688 due, upcoming
   - Q3 (due Sep 15): upcoming
   - Q4 (due Jan 15 2027): upcoming
4. Note the Q1 amount, make a payment via IRS Direct Pay
5. Record the payment as a transaction: entity=sparkry, category=TAXES_AND_LICENSES, subcategory=estimated_tax

**Expected Result**: Travis knows how much estimated tax to pay and when. No surprises at year-end.
**Time**: ~2 minutes to check (payment is external)

---

## UC-013: Reviewing System Health

**Actor**: Travis
**Goal**: Check that all data sources are flowing and no errors are accumulating
**Frequency**: Weekly or when something feels off
**Precondition**: System has been running

**Steps**:
1. Navigate to Health page
2. Review source freshness cards:
   - Gmail/n8n: green (2 hours ago) — good
   - Stripe: "Setup Required" — see instructions for adding API key
   - Bank CSV: "Import via Reconciliation page" — expected, manual process
3. Check classification stats: auto-confirmed 68%, pending 8% — reasonable
4. Check Claude API usage: 47 calls this month, $0.05 estimated — very cheap
5. Check tax deadlines: next deadline is April 15 (Q1 estimated tax), 28 days away
6. If any source shows red/error: click "Retry" to re-run the adapter

**Expected Result**: Quick health check confirms all systems operational. Setup guidance for unconfigured sources is clear.
**Time**: ~2 minutes
