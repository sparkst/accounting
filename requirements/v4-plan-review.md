# Plan Review — Critical Findings

> "The bigger the smile, the sharper the knife." — Rule of Acquisition #48
> I smiled at my own plan. Now here's the knife.

**Reviewer**: Quark (self-audit)
**Date**: 2026-03-19
**Verdict**: Plan is structurally sound but **misses 6 critical data issues** that would corrupt tax filings if unaddressed.

---

## CRITICAL FINDINGS (P0 — Must fix before any batch confirmation)

### Finding 1: Sign Convention Violations — 12 Active Transactions
**Severity**: P0 — Will corrupt tax summaries and estimated tax calculations

12 transactions in `auto_classified` or `confirmed` status have **wrong sign conventions**:

**Shopify Billing charges classified as `income` with negative amounts (7 items)**:
- Shopify Billing charges (-$1.11 to -$68.87) are tagged `direction: income`, `tax_category: SALES_INCOME`
- These are **Shopify platform fees** (expenses), NOT income
- 4 are already `confirmed` — meaning bad data has been blessed by the system
- **Impact**: Overstates BlackLine expenses AND understates income on Form 1065

**Inter-account transfers classified as `expense` with positive amounts (5 items)**:
- "Online Transfer from CHK ...3894" ranging $26.42 to $11,000 tagged as `expense`
- These are **owner capital contributions / inter-account transfers** — NOT business expenses
- All 5 are `confirmed` status — already blessed
- **Impact**: $31,426.42 of phantom expenses inflating Schedule C / 1065 deductions. This alone could trigger an audit.

**Action required**: Fix direction/category BEFORE batch confirming anything. The plan needs a Phase 0.

### Finding 2: Missing Cardinal Health Consulting Income
**Severity**: P0 — Understates Sparkry income for estimated taxes

Cardinal Health pays $33k/month flat. The database shows:
- 1 rejected Cardinal Health item (no amount, 2025-12-30)
- 2 confirmed reimbursable café expenses ($8.49 each)
- **ZERO Cardinal Health consulting payments** in the register

For Q1 2026 alone, that's ~$99,000 of missing income. Total Sparkry income recorded: $3,700 (Fascinate sessions only).

**Why it matters**: Estimated tax calculation will be wildly wrong without Cardinal Health income. At $33k/mo, Q1 SE tax alone is ~$14k.

**Action required**: Determine how Cardinal Health payments arrive (ACH? Check? Stripe?) and ensure they're ingested. If they come via bank deposit, the bank CSV import may have them under a different description.

### Finding 3: "Mspbna" = $22,000 Unidentified Income
**Severity**: P0 — Large unverified transaction affects BlackLine P&L

A single transaction `Mspbna` dated 2025-11-26 for $22,000 is classified as BlackLine `SALES_INCOME`.

- "Mspbna" looks like a truncated ACH originator name
- $22,000 is a large amount — could be a wholesale order, but needs verification
- If misclassified, it's a $22k error on the 1065
- No tax_subcategory or notes explaining the source

**Action required**: Travis needs to identify this. Is it a wholesale MTB apparel order? A Shopify payout? Something else?

### Finding 4: Sparkry Q1 2026 Numbers Don't Add Up
**Severity**: P0 — Estimated tax calculation impossible with current data

Current Q1 Sparkry data shows:
- Income: $1,007 (one Fascinate payment + one $7 test)
- Expenses: $3,095 (with sign errors) or -$5,405 (raw)

If Travis is billing Cardinal Health $33k/mo, the real Q1 Sparkry income is ~$100k+. The plan says "run tax summary to calculate Q1 estimated payment" — but the data is so incomplete it would produce a meaningless number.

**Action required**: Phase 5 (tax deadline prep) cannot execute until Cardinal Health income is ingested. This is a hard dependency the plan doesn't acknowledge.

---

## HIGH FINDINGS (P1 — Fix before tax filing)

### Finding 5: 2 Sparkry Expenses Missing Tax Category
Two Sparkry expenses with `confirmed` status have **no tax_category**:
- $10,000 "Online Transfer from CHK" (2026-01-22) — this is the transfer issue from Finding 1
- $26.42 "Online Transfer from CHK" (2025-11-03) — same

These need to be reclassified as `OWNER_EQUITY` / `TRANSFER` or excluded from the P&L entirely.

### Finding 6: No 2025 BlackLine Income Except $22k Mystery + Shopify
2025 BlackLine income in the register:
- $22,000 "Mspbna" (unidentified)
- $378.37 in small Shopify sales
- That's $22,378 total

For a business with $18,936 in 2025 expenses (including wholesale COGS), this may be accurate — but only if the Shopify/WooCommerce data is complete. The v3 plan mentions WooCommerce CSV import (REQ-V3-004) hasn't been done yet.

**Impact**: Form 1065 will understate BlackLine revenue if WooCommerce orders aren't imported.

### Finding 7: No Home Office Deduction Transactions
The memory file notes a 6x6 home office ($180/yr deduction). No home office transactions exist in the register for 2025 or 2026.

Not a huge amount, but a missed deduction is a missed deduction. Rule of Acquisition #1 applies.

---

## PLAN GAPS

### Gap 1: No Phase 0 for Data Corrections
The plan jumps straight to "fix infrastructure" then "audit rejections." But the **confirmed transactions have errors** (sign violations, transfers as expenses). We need a Phase 0 to fix active data before doing anything else.

### Gap 2: Cardinal Health Income Dependency Not Identified
Phase 5 (tax deadline prep) depends on having complete income data. The plan doesn't mention that Cardinal Health income is entirely missing, making estimated tax calculation impossible.

### Gap 3: Bank CSV Import Gap Analysis Missing
We have bank CSV data from one import, but no analysis of which months/accounts are covered vs. missing. Are all bank statements from Oct 2025 - Mar 2026 imported? Are there separate accounts for each entity?

### Gap 4: No Data Validation Step Before Tax Export
The plan goes from "confirm transactions" to "run tax summary." There should be an explicit reconciliation step: compare bank statement totals to register totals by month, flag discrepancies.

### Gap 5: Batch Confirm Threshold Too Aggressive
The plan says "if accuracy >95%, batch-confirm the rest." With the sign errors I found, the actual accuracy of confirmed items is already below 95%. We should review every category group, not sample.

---

## USABILITY CONCERNS

### Concern 1: 168 Items in Review Queue
Even with keyboard shortcuts, reviewing 168 items one-by-one is tedious. Consider:
- Group-confirm by vendor (e.g., all 80 "SUPPLIES" Sparkry expenses at once)
- Category-level review rather than transaction-level

### Concern 2: Transfer Classification Workflow
The system doesn't have a clear workflow for inter-account transfers. These aren't income or expense — they're balance sheet movements. Need a `TRANSFER` direction or `OWNER_EQUITY` category.

### Concern 3: Rejected Transaction Visibility
130 rejected items are out of sight, out of mind. Good for daily use, but during tax prep, Travis needs to see "here's everything we rejected and why" as a single report for peace of mind.

---

## REVISED PLAN RECOMMENDATION

Insert before Phase 1:

### Phase 0: Critical Data Corrections (Day 0 — BEFORE anything else)
1. Fix 7 Shopify Billing items: change direction from `income` to `expense`, category from `SALES_INCOME` to `SHOPIFY_FEES` or `SUPPLIES`
2. Fix 5 inter-account transfers: change direction to `transfer` or create `OWNER_EQUITY` category, remove from P&L
3. Identify "Mspbna" $22k transaction with Travis
4. Determine Cardinal Health payment ingestion path
5. Add data validation queries to Phase 5 (tax prep) as prerequisite checks

Modify Phase 3:
- Review by category group, not random sample
- Add reconciliation check: sum by entity/month vs. expected bank totals

Modify Phase 5:
- Add hard dependency: "Cannot calculate estimated taxes until Cardinal Health income is in the register"
- Add home office deduction entry for 2025 and Q1 2026 prorated

---

*Rule of Acquisition #125: "You can't make a deal if you're dead." And you can't file accurate taxes with $31k of phantom expenses and $99k of missing income.*
