# Quark's Financial Operations Plan — v4 (Consolidated)

> "Every once in a while, declare peace. It confuses the hell out of your enemies." — Rule of Acquisition #76
> In this case, we're declaring war on bad data before making peace with the tax man.

**Date**: 2026-03-19
**Author**: Quark (CFO Agent)
**Version**: 2.0 — incorporates all self-review findings
**Scope**: Data corrections + rejected transaction triage + v3 remaining work + tax deadline readiness

---

## Executive Summary

We have 337 transactions: 168 auto_classified (50%), 39 confirmed (12%), 130 rejected (38%).

**Critical discovery**: The self-review found **4 P0 data integrity issues** that would corrupt tax filings:
1. 12 transactions with wrong sign conventions ($31k phantom expenses + misclassified Shopify fees)
2. ~$99k+ of missing Cardinal Health consulting income (entire Q1 2026)
3. $22k unidentified "Mspbna" transaction needing Travis's input
4. Estimated tax calculation impossible until income data is complete

The rejected transactions are mostly correct (87 of 130), but the **confirmed and auto_classified data has errors that must be fixed first**.

**Deadline pressure**: Q1 2026 estimated taxes due **April 15** (27 days). March B&O filing for Sparkry due ~April 25.

---

## Phase 0: Critical Data Corrections (Day 0 — BEFORE anything else)

> This phase is non-negotiable. Skipping it means filing taxes with $31k in phantom expenses and $99k in missing income.

### Task 0.1: Fix Shopify Billing Misclassification (7 items)
- **What**: 7 "Shopify Billing" charges ($1.11 to $68.87) are tagged `direction: income`, `tax_category: SALES_INCOME`
- **Reality**: These are Shopify platform subscription fees — they are **expenses**, not income
- **Fix**: Change `direction` → `expense`, `tax_category` → `PLATFORM_FEES` or `SUPPLIES`
- **IDs**: f17796ae, e9fdb9be, 1c11c469 (auto_classified); 9fcc1d81, 9e505166, bcf79823, cf1fa2ec (confirmed)
- **Impact if unfixed**: BlackLine 1065 would understate income and misreport expense categories
- **Effort**: 15 minutes (SQL update + audit trail entry)

### Task 0.2: Fix Inter-Account Transfer Misclassification (5 items)
- **What**: 5 "Online Transfer from CHK ...3894" entries ($26.42 to $11,000) are tagged `direction: expense` with **positive** amounts and no tax_category
- **Reality**: These are inter-account transfers / owner capital contributions — NOT business expenses
- **Fix**: Either (a) change `direction` → `transfer` and add `tax_category` → `OWNER_EQUITY`, or (b) change `status` → `rejected` with reason "Inter-account transfer — not a P&L item"
- **IDs**: 30a867c4 ($10k), 206cdc3d ($1,401), 8a4787e1 ($11k) — BlackLine; 32875de0 ($10k), beefec90 ($26) — Sparkry. All confirmed.
- **Impact if unfixed**: $31,426.42 in phantom expenses inflating Schedule C / 1065 deductions. **Audit risk.**
- **Design decision needed**: Does the system support `direction: transfer`? If not, should we add it, or use `rejected` with a clear reason?
- **Effort**: 30 minutes (may require schema/UI discussion)

### Task 0.3: Identify "Mspbna" $22,000 Transaction (NEEDS TRAVIS)
- **What**: Single transaction dated 2025-11-26, $22,000, classified as BlackLine `SALES_INCOME`
- **"Mspbna"** appears to be a truncated ACH originator name from bank CSV import
- **Questions for Travis**:
  - Is this a wholesale MTB apparel order? A Shopify payout aggregate? Something else entirely?
  - What vendor/customer does this correspond to?
  - Should it remain as BlackLine SALES_INCOME?
- **Impact if wrong**: $22k error on Form 1065
- **Effort**: 5 minutes once Travis identifies it

### Task 0.4: Locate Cardinal Health Consulting Income (NEEDS TRAVIS)
- **What**: Cardinal Health pays $33k/month flat rate consulting. The register contains **zero** Cardinal Health consulting payments.
- **Current CH entries**: 1 rejected item (no amount), 2 confirmed reimbursable café expenses ($8.49 each)
- **Q1 2026 gap**: ~$99,000 missing Sparkry income
- **Questions for Travis**:
  - How do Cardinal Health payments arrive? (ACH direct deposit? Check? Wire?)
  - Which bank account do they deposit to?
  - Has Q1 been invoiced/paid? (Invoicing system shows generation capability but unclear if used)
  - Could the payments be in the bank CSV under a different name? (Like the "Mspbna" situation)
- **Impact if unresolved**: Q1 estimated tax calculation is impossible. At $33k/mo + $2,700 Fascinate, Q1 SE tax alone is ~$15k. Filing $0 estimated taxes when you owe $15k = underpayment penalty.
- **Effort**: Variable — depends on payment mechanism

### Task 0.5: Add `TRANSFER` / `OWNER_EQUITY` Category Support
- **What**: The system currently has no clean way to categorize inter-entity transfers
- **Options**:
  - A) Add `direction: transfer` to the schema + UI dropdowns
  - B) Add `tax_category: OWNER_EQUITY` and keep direction as `expense`
  - C) Reject transfers with a clear reason and exclude from all tax summaries
- **Recommendation**: Option A is cleanest — transfers are neither income nor expense. They should appear in the register for audit trail but be excluded from P&L and tax summaries.
- **Effort**: 1-2 hours (schema change, migration, UI update, test updates)

### Task 0.6: Verify Sign Convention System-Wide
- **What**: Run validation query to catch any remaining sign violations:
  - Expenses should have negative amounts
  - Income should have positive amounts
  - Transfers should be excluded from P&L calculations
- **Query**: `SELECT * FROM transactions WHERE status IN ('auto_classified','confirmed') AND ((direction='expense' AND amount > 0) OR (direction='income' AND amount < 0))`
- **Effort**: 15 minutes

---

## Phase 1: Fix Infrastructure (Day 1)

### Task 1.1: Install Missing Dependencies
- **What**: `pip install sqlalchemy aiosqlite` in the venv
- **Why**: 31 test collection errors, all from missing sqlalchemy
- **Effort**: 5 minutes
- **Acceptance**: `pytest` collects all tests without errors

### Task 1.2: Fix the 13 LLM Classification Failures
- **What**: 13 transactions rejected with reason "Low confidence (0.00) from Tier 3 LLM: Unexpected error...Could not resolve authentication method"
- **Root cause**: ANTHROPIC_API_KEY missing or misconfigured when those transactions were ingested
- **Fix**: Re-run classification on these 13 transactions with a valid API key, or manually classify them
- **Items**: Mostly AmEx notifications and personal emails — many may be correctly rejectable after review
- **Effort**: 30 minutes

### Task 1.3: Run Full Test Suite
- **What**: `pytest && ruff check src/ && mypy src/`
- **Acceptance**: All 1,025+ tests pass, no lint/type errors
- **Effort**: 15 minutes (fix any failures)
- **Note**: Phase 0 schema changes (Task 0.5) may require new tests

---

## Phase 2: Rejected Transaction Audit (Day 1-2)

The 130 rejected transactions break down into **7 categories**. Most are correctly rejected.

### Category A: Cross-Source Duplicates — 16 items ✅ CORRECT
- **Review reason**: "Duplicate of gmail_n8n transaction — cross-source confirmed"
- **Action**: Verify each pair, then leave as rejected. Working as designed (REQ-008).

### Category B: Shopify Notifications — 24 items ✅ CORRECT
- **Review reasons**: "Shopify order notification email" (19), "Shopify notification — sales from Shopify API adapter" (4), other (1)
- **Action**: Leave rejected. Notification emails, not financial transactions.

### Category C: Stripe Email Receipts — 17 items ✅ CORRECT
- **Review reasons**: "Stripe email receipt — charges captured by Stripe adapter" (11), "Stripe payout — charges captured" (6)
- **Action**: Leave rejected. Stripe adapter is the authoritative source.

### Category D: Credit Card Payments — 12 items ✅ CORRECT
- **Review reasons**: "AmEx notification" (10), "CC payment/autopay" (2)
- **Action**: Leave rejected. Balance transfers, not expenses — individual charges already captured.

### Category E: Non-Transaction Emails — 18 items ✅ CORRECT
- **Review reasons**: Outgoing invoices (6), Cardinal Health correspondence (6), marketing/quotes (6)
- **Action**: Leave rejected. Correspondence, not financial transactions.

### Category F: LLM Failures — 13 items ⚠️ NEEDS FIX
- **Review reason**: "Low confidence (0.00) from Tier 3 LLM: Unexpected error"
- **Action**: Re-classify with valid API key (Task 1.2). Many may turn out to be correctly rejectable.

### Category G: Miscellaneous — ~30 items 🔍 NEEDS REVIEW
- **Action**: Manual review in dashboard.
- Key items:
  - "Check deposit — needs category" (1) — assign to correct entity/category
  - "Camp transaction forwarded email" (1) — likely personal expense
  - "Amount is missing" (1) — find original receipt
  - "Emerson Sparks" (3) — BlackLine business partner, check attachments for amounts
  - "Transfer out to personal" (1) — correctly rejected
  - Test charges (4) — correctly rejected

### Phase 2 Summary
| Category | Count | Action | Effort |
|---|---|---|---|
| A: Cross-source dupes | 16 | Verify & leave rejected | 15 min |
| B: Shopify notifications | 24 | Leave rejected | 5 min |
| C: Stripe email receipts | 17 | Leave rejected | 5 min |
| D: CC payments | 12 | Leave rejected | 5 min |
| E: Non-transaction emails | 18 | Leave rejected | 5 min |
| F: LLM failures | 13 | Re-classify | 30 min |
| G: Miscellaneous | ~30 | Manual review | 30 min |
| **Total** | **130** | | **~1.5 hr** |

---

## Phase 3: Confirm Auto-Classified Transactions (Day 2-3)

> **CHANGED from v1**: No batch-confirm by sampling. Review every category group.

168 auto_classified transactions all have entity, category, and amount populated, but Phase 0 proved that even confirmed data has errors. Review must be thorough.

### Task 3.1: Review by Category Group (NOT random sample)
For each entity, review all items grouped by tax_category:

**Sparkry AI** (~80 items):
- SUPPLIES (80): Software subscriptions, API costs — verify each is a real business expense
- TRAVEL (20): Flights, hotels, WiFi — verify business purpose
- INSURANCE (4): Hiscox, health — verify correct categorization
- OFFICE_EXPENSE (3): Office supplies — verify
- MEALS (3): Business meals — verify deductible percentage
- REIMBURSABLE (2): Cardinal Health pass-throughs — verify linked
- LEGAL_AND_PROFESSIONAL (1): Verify
- UNCATEGORIZED (2): **Must assign category before confirming**

**BlackLine MTB** (~23 items):
- COGS (majority): Wholesale inventory, manufacturing — verify vendor/amounts
- SALES_INCOME: Verify direction and sign (after Phase 0 Shopify fixes)
- ADVERTISING: Marketing spend — verify

**Personal** (~1 item):
- Personal expense — verify

### Task 3.2: Reconciliation Check
Before confirming, compare register totals by entity/month against bank statement totals:
- Pull bank statement balances for each month
- Compare to sum of register transactions
- Flag any month where discrepancy > $100
- **This catches missing transactions** (like the Cardinal Health gap)

### Task 3.3: Confirm Valid Items
- Use dashboard review queue with keyboard shortcuts (y=confirm, j/k=navigate)
- Group by category for faster review
- Target: Move all verified auto_classified to confirmed

---

## Phase 4: v3 Remaining Work (Day 3-5)

Priority-ordered by tax deadline impact:

### P1 — Tax-Critical (Before April 15)

| Task | REQ | Status | Effort | Dependencies |
|---|---|---|---|---|
| WooCommerce CSV import | V3-004 | Adapter built | 1 hr | Travis provides CSV |
| Bank CSV cross-source dedup | V3-008 | Manual dedup done | 2-3 hr | None |
| Bank CSV vendor cleanup | V3-009 | Function built | 30 min | None |
| Tax year locking test | V3-005 | UI built | 30 min | None |
| Home office deduction entry | NEW | Not started | 15 min | $180/yr, 6x6 room, prorate Q1 |
| Bank statement gap analysis | NEW | Not started | 1 hr | Identify missing months/accounts |

### P2 — Dashboard Quality (Week 2)

| Task | REQ | Status | Effort |
|---|---|---|---|
| Compact review mode live test | V3-001 | Code built | 30 min |
| Priority grouping headers test | V3-002 | Code built | 30 min |
| Bank CSV import E2E test | V3-003 | UI built | 1 hr |
| Duplicate invoice warning test | V3-006 | Logic built | 30 min |
| Fascinate first invoice | V3-010 | All code built | Blocked |
| Rejected transactions report | NEW | Not started | 2 hr |

### P3 — Blocked/Deferred

| Task | REQ | Blocker |
|---|---|---|
| Shopify API integration | V3-007 | Travis adds API credentials |
| Fascinate invoice | V3-010 | Travis exports Google Calendar .ics |
| Monthly P&L report | V3-011 | Nice-to-have |
| Recurring invoice automation | V3-012 | Nice-to-have |
| Mobile responsive | V3-014 | Nice-to-have |

---

## Phase 5: Tax Deadline Preparation (Ongoing through April 15)

### HARD DEPENDENCY: Cardinal Health Income Must Be In Register
Phase 5 **cannot produce accurate results** until Task 0.4 is resolved. At $33k/mo, the missing income dwarfs everything else in the system.

### Task 5.1: Data Validation Checkpoint (BEFORE any export)
Run validation queries to confirm:
- [ ] No sign convention violations remain
- [ ] No active transactions with missing tax_category (except transfers)
- [ ] All inter-account transfers excluded from P&L
- [ ] Cardinal Health income present and categorized
- [ ] Register totals reconcile with bank statements (±$100/month)
- [ ] BlackLine "Mspbna" $22k identified and correctly categorized

### Task 5.2: Q1 2026 Estimated Tax Calculation
- **Due**: April 15, 2026
- **Requires**: Task 5.1 validation pass + Cardinal Health income
- **Calculate**:
  - Sparkry Schedule C net income → SE tax (15.3%) + income tax bracket
  - BlackLine 1065 K-1 pass-through → Travis's personal return
  - Apply safe harbor: 110% of prior year tax liability, or 90% of current year
- **Output**: 1040-ES voucher amount

### Task 5.3: March B&O Tax Prep (Sparkry)
- **Due**: ~April 25
- **Requires**: All March Sparkry revenue confirmed
- **Calculate**: Sparkry March gross revenue × B&O rate (0.471% service & other)
- **WA DOR Account**: 605-965-107

### Task 5.4: Export Validation
- Verify FreeTaxUSA export format (REQ-018) with test data
- Verify TaxAct export format (REQ-019) with test data
- Verify B&O export with WA DOR upload format
- Cross-check export totals against dashboard tax summary

### Task 5.5: Home Office Deduction
- 6x6 room = 36 sq ft, simplified method = $5/sq ft (max 300 sq ft)
- $180/year, prorated Q1 = $45
- Add as Sparkry expense, tax_category: HOME_OFFICE
- Small amount but legitimate — every deduction counts

---

## Execution Order

```
Day 0 (NOW):
  Phase 0 Tasks 0.1-0.2, 0.5-0.6 (data fixes we can do without Travis)
  Phase 0 Tasks 0.3-0.4 (questions for Travis — BLOCKING)
  Phase 1 Task 1.1 (install deps)

Day 1 (after Travis answers):
  Phase 0 Tasks 0.3-0.4 (apply Travis's answers)
  Phase 1 Tasks 1.2-1.3 (LLM reclassify, test suite)
  Phase 2 (rejected audit — ~1.5 hours)

Day 2:
  Phase 3 (category-by-category review + reconciliation)

Day 3-5:
  Phase 4 P1 tasks (tax-critical v3 work)

Ongoing (parallel):
  Phase 5 (tax deadline prep — gated on Cardinal Health resolution)

Week 2:
  Phase 4 P2 tasks (dashboard quality)
```

---

## Risk Register

| # | Risk | Impact | Likelihood | Mitigation |
|---|---|---|---|---|
| R1 | Cardinal Health income never located | Q1 estimated taxes wrong by ~$15k | Low | Travis knows where payments go — just need to ask |
| R2 | "Mspbna" is misclassified | $22k error on 1065 | Medium | Travis identifies before confirmation |
| R3 | ANTHROPIC_API_KEY missing | 13 items stuck | Medium | Manual classify as fallback |
| R4 | WooCommerce CSV not provided | Missing BlackLine sales | Medium | Use Shopify API or manual entry |
| R5 | Bank statements incomplete | Missing months of data | Medium | Gap analysis in Phase 4 catches this |
| R6 | Sign convention violations elsewhere | Tax summary errors | Low | Phase 0 Task 0.6 catches remaining |
| R7 | Transfer category not supported in UI | Can't properly categorize 5 items | Medium | Fall back to `rejected` with clear reason |
| R8 | Q1 data incomplete by April 1 | Underpayment penalty | Low | File conservative estimate, amend later |

---

## Questions for Travis (Blocking)

1. **Cardinal Health payments**: How do they arrive and in which account? This is the #1 blocker.
2. **"Mspbna" $22k**: What is this? Wholesale order? Shopify aggregate payout?
3. **Inter-account transfers**: Should these be `rejected` or do you want a `transfer` direction in the system?
4. **Bank accounts**: How many checking/savings accounts exist? Which entity uses which?
5. **WooCommerce CSV**: Can you export 2025-2026 order history? (REQ-V3-004 needs it)

---

*Rule of Acquisition #3: "Never spend more for an acquisition than you have to." Including the acquisition of an IRS audit. Fix the data first, then file.*
