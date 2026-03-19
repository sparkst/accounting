# Quark's Financial Operations Plan — v4

> "A good plan today is better than a perfect plan tomorrow." — Quark (paraphrasing Rule of Acquisition #62)

**Date**: 2026-03-19
**Author**: Quark (CFO Agent)
**Scope**: Rejected transaction triage + v3 remaining work + tax deadline readiness

---

## Executive Summary

We have 337 transactions: 168 auto_classified (50%), 39 confirmed (12%), 130 rejected (38%). The rejected count looks alarming but is actually **mostly correct**. Analysis shows the rejections fall into well-documented categories — cross-source duplicates, notification emails, and items already captured by other adapters. The real work is: (1) confirming the rejections are truly correct, (2) moving auto_classified items to confirmed, and (3) fixing 13 items that failed LLM classification due to a missing API key.

**Deadline pressure**: Q1 2026 estimated taxes due **April 15** (27 days). March B&O filing for Sparkry due ~April 25.

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
- **Items**: Mostly AmEx notifications and personal emails — likely all re-classifiable
- **Effort**: 30 minutes

### Task 1.3: Run Full Test Suite
- **What**: `pytest && ruff check src/ && mypy src/`
- **Acceptance**: All 1,025+ tests pass, no lint/type errors
- **Effort**: 15 minutes (fix any failures)

---

## Phase 2: Rejected Transaction Audit (Day 1-2)

The 130 rejected transactions break down into **7 categories**. Most are correctly rejected.

### Category A: Cross-Source Duplicates — 16 items ✅ CORRECT
- **Review reason**: "Duplicate of gmail_n8n transaction — cross-source confirmed"
- **What happened**: Bank CSV imports matched existing email receipts
- **Action**: Verify each pair, then leave as rejected. These are working as designed (REQ-008).

### Category B: Shopify Notifications — 24 items ✅ CORRECT
- **Review reasons**: "Shopify order notification email" (19), "Shopify notification — sales from Shopify API adapter" (4), "Shopify subscription notification" (1)
- **What happened**: Gmail ingested Shopify email notifications. The real sales data should come from Shopify API (REQ-004) or WooCommerce CSV (REQ-V3-004)
- **Action**: Leave rejected. These are notification emails, not financial transactions.
- **Note**: 19 "Black Line MTB Apparel" items with negative amounts from gmail_n8n — these are Shopify order notification emails parsed as income. Correctly rejected.

### Category C: Stripe Email Receipts — 17 items ✅ CORRECT
- **Review reasons**: "Stripe email receipt — charges captured by Stripe adapter" (11), "Stripe payout — charges captured by Stripe adapter" (4+6)
- **What happened**: Gmail ingested Stripe notification emails, but actual charges/payouts are captured by the Stripe API adapter
- **Action**: Leave rejected. Stripe adapter is the authoritative source.

### Category D: Credit Card Payments — 12 items ✅ CORRECT
- **Review reasons**: "AmEx notification — charges from bank CSV" (5), "AmEx email notification" (5), "CC payment/autopay" (2)
- **What happened**: AmEx payment confirmations and CC payment transfers. These are balance transfers, not actual expenses — the individual charges are already captured.
- **Action**: Leave rejected. CC payments are not expenses.

### Category E: Non-Transaction Emails — 18 items ✅ CORRECT
- **Review reasons**: "Outgoing invoice/email sent by Travis" (6), "Cardinal Health business correspondence" (6), "Insurance/service quote" (2), various marketing/notification emails (4)
- **What happened**: Gmail n8n pipeline captured non-receipt emails
- **Action**: Leave rejected. These are correspondence, not financial transactions.

### Category F: LLM Failures — 13 items ⚠️ NEEDS FIX
- **Review reason**: "Low confidence (0.00) from Tier 3 LLM: Unexpected error"
- **What happened**: ANTHROPIC_API_KEY missing during ingestion
- **Action**: Re-classify these 13 items. They are real transactions that failed automated processing.

### Category G: Miscellaneous — ~30 items 🔍 NEEDS REVIEW
- Various items: test charges (4), transfers, amounts missing, single one-offs
- **Action**: Manual review in dashboard. Estimate 15 minutes.
- Specific items needing attention:
  - "Check deposit — needs category" (1 item) — assign to correct entity/category
  - "Camp transaction forwarded email — needs manual review" (1 item) — personal expense?
  - "Amount is missing — manual entry required" (1 item) — find original receipt
  - "Emerson Sparks" items (3) — BlackLine business partner, check attachments for amounts
  - "Transfer out to personal" (1) — correctly rejected as non-business
  - Test charges (4) — correctly rejected

### Phase 2 Summary
| Category | Count | Action | Status |
|---|---|---|---|
| A: Cross-source dupes | 16 | Verify & leave rejected | ✅ Correct |
| B: Shopify notifications | 24 | Leave rejected | ✅ Correct |
| C: Stripe email receipts | 17 | Leave rejected | ✅ Correct |
| D: CC payments | 12 | Leave rejected | ✅ Correct |
| E: Non-transaction emails | 18 | Leave rejected | ✅ Correct |
| F: LLM failures | 13 | Re-classify | ⚠️ Fix needed |
| G: Miscellaneous | ~30 | Manual review | 🔍 Review |
| **Total** | **130** | | |

**Bottom line**: ~87 items (67%) are correctly rejected and need no action. 13 need re-classification. ~30 need manual review.

---

## Phase 3: Confirm Auto-Classified Transactions (Day 2-3)

168 auto_classified transactions all have entity, category, and amount populated. They need human confirmation to move to `confirmed` status.

### Task 3.1: Batch Review by Entity
- **Sparkry AI** (est. ~80 items): Consulting income, software expenses
- **BlackLine MTB** (est. ~60 items): Product sales, COGS, shipping
- **Personal** (est. ~28 items): Deductions, non-deductible personal

### Task 3.2: Spot-Check Classification Accuracy
- Sample 10% of auto_classified items and verify entity/category/direction
- If accuracy >95%, batch-confirm the rest
- If accuracy <95%, review each category group individually

### Task 3.3: Confirm Valid Items
- Use dashboard review queue with keyboard shortcuts (y=confirm, j/k=navigate)
- Target: Move all 168 auto_classified to confirmed

---

## Phase 4: v3 Remaining Work (Day 3-5)

Priority-ordered by tax deadline impact:

### P1 — Tax-Critical (Before April 15)

| Task | REQ | Status | Effort |
|---|---|---|---|
| WooCommerce CSV import with real data | V3-004 | Adapter built, needs Travis's CSV | 1 hr |
| Bank CSV cross-source dedup automation | V3-008 | Manual dedup done (16 pairs), needs automation | 2-3 hr |
| Bank CSV vendor name cleanup on import | V3-009 | clean_bank_description() built, needs wiring | 30 min |
| Tax year locking verification | V3-005 | Backend + UI built, needs live test | 30 min |

### P2 — Dashboard Quality (Week 2)

| Task | REQ | Status | Effort |
|---|---|---|---|
| Compact review mode live test | V3-001 | Code built, needs live data | 30 min |
| Priority grouping headers live test | V3-002 | Code built, needs live data | 30 min |
| Bank CSV import E2E via dashboard | V3-003 | UI built, needs E2E test | 1 hr |
| Duplicate invoice warning test | V3-006 | Logic built, needs live test | 30 min |
| Fascinate first invoice generation | V3-010 | All code built, needs .ics file | Blocked |

### P3 — Blocked/Deferred

| Task | REQ | Blocker |
|---|---|---|
| Shopify API integration | V3-007 | Travis needs to add API credentials |
| Fascinate invoice | V3-010 | Travis needs to export Google Calendar .ics |
| Monthly P&L report | V3-011 | Nice-to-have |
| Recurring invoice automation | V3-012 | Nice-to-have |
| Mobile responsive | V3-014 | Nice-to-have |

---

## Phase 5: Tax Deadline Preparation (Ongoing through April 15)

### Task 5.1: Q1 2026 Estimated Tax Calculation
- **Due**: April 15, 2026
- **Requires**: All Q1 income/expenses confirmed
- **Action**: Run tax summary for Sparkry AI (Schedule C) and BlackLine MTB (1065) to calculate Q1 estimated payment

### Task 5.2: March B&O Tax Prep (Sparkry)
- **Due**: ~April 25
- **Requires**: All March Sparkry revenue confirmed
- **Action**: Run B&O export for Sparkry March revenue

### Task 5.3: Export Validation
- Verify FreeTaxUSA export format (REQ-018)
- Verify TaxAct export format (REQ-019)
- Verify B&O export with WA DOR upload format

---

## Execution Order

```
Day 1:  Phase 1 (fix infra) → Phase 2 Categories F+G (fix/review 43 items)
Day 2:  Phase 2 Categories A-E (verify 87 correct rejections) → Phase 3 (batch confirm)
Day 3:  Phase 4 P1 tasks (tax-critical v3 work)
Day 4-5: Phase 4 P2 tasks (dashboard quality)
Ongoing: Phase 5 (tax deadline prep, runs parallel)
```

---

## Risk Register

| Risk | Impact | Mitigation |
|---|---|---|
| ANTHROPIC_API_KEY not in .env | 13 items stuck unclassified | Travis adds key or we manually classify |
| WooCommerce CSV not provided | Missing BlackLine sales data | Ask Travis for CSV export |
| Shopify API credentials missing | No live Shopify sync | Use WooCommerce CSV as interim |
| Auto-classification accuracy <90% | Batch confirm unsafe | Sample review first, category-by-category if needed |
| Q1 data incomplete by April 1 | Estimated tax calculation unreliable | Conservative estimate + amend later |

---

*Rule of Acquisition #75: "Home is where the heart is, but the stars are made of latinum." The home office deduction is nice, but getting those estimated taxes right is where the real savings live.*
