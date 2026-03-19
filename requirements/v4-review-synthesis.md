# Plan Review Synthesis — All Three Reviewers

**Date**: 2026-03-19
**Reviewers**: Accounting (code-level), Tax Compliance, UX/Usability
**Plan reviewed**: `v4-consolidated-plan.md`

---

## FIRE ALARMS (Act Today)

### ALARM 1: BlackLine Form 1065 Deadline Was March 15 — 4 Days Ago
**Source**: Tax Reviewer Finding 7.2
**Severity**: URGENT

BlackLine MTB LLC Form 1065 (partnership return) was due **March 15, 2026**. That was 4 days ago. If Form 7004 (automatic extension) was not filed, late-filing penalties are $220/partner/month.

**Immediate action**: Travis must verify whether a Form 7004 extension was filed. If not, file one TODAY — even late, it reduces penalties. The extension pushes the deadline to September 15, 2026.

### ALARM 2: Travis's 2025 Form 1040 — File Extension by April 15
**Source**: Tax Reviewer Finding 7.3

Given that data cleanup (Phases 0-3) won't be complete before April 15, Travis almost certainly needs to file **Form 4868** (automatic extension to October 15). Note: the extension extends *filing* only — estimated tax payment is still due April 15.

### ALARM 3: 1099-NEC Issuance — Deadline Was January 31
**Source**: Tax Reviewer Finding 7.1

If Sparkry or BlackLine paid any contractor $600+ in 2025 (Fiverr designers, Gaby Photography, etc.), 1099-NEC forms were due January 31, 2026. The plan never mentions this.

**Immediate action**: Audit all CONTRACT_LABOR payments for 2025. If any need 1099-NECs, file late immediately.

---

## PLAN ERRORS (Must Fix Before Execution)

### Error 1: B&O Rate — 3x Understatement
**Source**: Accounting Reviewer V5, Tax Reviewer 3.1
**Confidence**: 100% — verified in code

The plan cites 0.471% for Sparkry's B&O. The correct rate is **1.5%** (Service & Other Activities). The code in `bno_tax.py` has the correct rate. At $33k/month, the difference is:
- Plan says: ~$155/month
- Reality: ~$495/month
- **Quarterly understatement: ~$1,020**

### Error 2: Home Office Task Would Double-Count $180
**Source**: Accounting Reviewer V3, ISSUE-2
**Confidence**: 100% — verified in code

Task 5.5 says "Add as Sparkry expense, tax_category: HOME_OFFICE." But the tax export code (`tax_export.py` lines 778-781) **already automatically adds $180** to Sparkry's expenses without needing a transaction. Adding a transaction would count it twice ($360 instead of $180).

**Fix**: Remove Task 5.5 entirely, or change it to "Verify the $180 auto-deduction appears in Tax Summary."

### Error 3: PLATFORM_FEES Category Doesn't Exist
**Source**: Accounting Reviewer V6, Tax Reviewer 4.2
**Confidence**: 100% — verified in enums

Task 0.1 says change Shopify Billing to `tax_category: PLATFORM_FEES`. This value doesn't exist in `TaxCategory`. Using it would either fail validation or silently drop from Schedule C output.

**Fix**: Use `tax_category: SUPPLIES` with `tax_subcategory: ECOMMERCE_PLATFORM`.

### Error 4: OWNER_EQUITY Category Doesn't Exist
**Source**: Accounting Reviewer V2
**Confidence**: 100% — verified in enums

Task 0.2 option B says `tax_category: OWNER_EQUITY`. The correct existing value is `CAPITAL_CONTRIBUTION`.

### Error 5: Task 0.5 Is Already Done
**Source**: Accounting Reviewer V1
**Confidence**: 100% — verified in enums

`Direction.TRANSFER = "transfer"` already exists in `enums.py` line 23. The plan estimates "1-2 hours for schema change, migration, UI update, test updates." The schema work is done — only the UI dropdown needs updating (one line in `TransactionCard.svelte`).

### Error 6: Flat 22% Tax Bracket Understates Liability
**Source**: Accounting Reviewer ISSUE-1

The estimated tax calculator uses a flat 22% income tax rate. At $396k projected annual income, the marginal rate reaches 32%. The understatement is ~$25k annually. The plan references the calculator output without flagging this limitation.

**Fix**: Add caveat that the built-in calculator is approximate. For actual 1040-ES filing, use the IRS worksheet or CPA review.

---

## NEW FINDINGS NOT IN THE PLAN

### Finding A: BlackLine Q1 B&O Filing Missing from Phase 5
**Source**: Accounting Reviewer ISSUE-3, Tax Reviewer 3.2

Phase 5 covers Sparkry B&O but completely omits BlackLine's Q1 B&O filing (also due ~April 25). BlackLine's B&O uses Retailing rate (0.471%) for direct sales and Wholesaling (0.484%) for wholesale.

### Finding B: Health Insurance Conflated with Business Insurance
**Source**: Tax Reviewer 4.3

The plan lists both "Hiscox" (business liability, Schedule C Line 15) and "health insurance" under the same INSURANCE category. Self-employed health insurance is deductible on **Form 1040 Line 17**, NOT Schedule C Line 15. These must be separated or the export will misplace the deduction.

### Finding C: BlackLine K-1 SE Tax Treatment Unclear
**Source**: Accounting Reviewer ISSUE-6, Tax Reviewer 2.1

The code applies SE tax to BlackLine income the same way as Sparkry. But partnership K-1 ordinary income SE tax treatment depends on whether Travis is a general or limited partner. As a general/managing member, it's likely subject to SE tax — but this needs CPA confirmation.

Additionally, filing BlackLine as a partnership (1065) when Travis owns 100% is unusual — a single-member LLC defaults to disregarded entity (Schedule C). If Emerson has 0% vested, the entity classification should be reviewed.

### Finding D: 2025 Data Completeness Not Addressed
**Source**: Accounting Reviewer M7

The plan focuses on Q1 2026 but the 2025 annual return also needs complete data. Missing Cardinal Health income may extend to Q4 2025 as well. The FreeTaxUSA/TaxAct exports need 2025 data for the annual returns.

### Finding E: 1099-NEC Verification for Income Received
**Source**: Accounting Reviewer ISSUE-5

Cardinal Health should issue a 1099-NEC to Sparkry. The plan should include verifying the 1099-NEC amount matches the register total — discrepancies create audit flags.

### Finding F: Cross-Year Reimbursement Pairs
**Source**: Tax Reviewer 5.2

If a Cardinal Health reimbursable expense was paid in Dec 2025 but reimbursed in Jan 2026, under cash-basis they don't net to zero — the expense is in 2025 and the income in 2026. The system should flag cross-year pairs.

---

## UX BLOCKERS (Phase 3 Is Broken As Written)

### Blocker 1: Review Queue Can't Show Auto-Classified Items
**Source**: UX Reviewer

The `/api/transactions/review` endpoint filters for `status == 'needs_review'` only. The 168 auto_classified items **will not appear** in the review queue. The plan's Phase 3 ("Use dashboard review queue with keyboard shortcuts") will show "All caught up!" — an empty queue.

**Fix required**: Either modify the review endpoint to accept `?include_auto_classified=true`, or Travis must use the register page (much slower workflow — no batch operations, no keyboard shortcuts for confirm).

**Impact**: Without this fix, Phase 3 goes from ~45 minutes (batch) to ~2.5 hours (click-expand-confirm per item).

### Blocker 2: No `transfer` in Direction Dropdown
**Source**: UX Reviewer

`TransactionCard.svelte` direction dropdown only offers: income, expense, reimbursable. No `transfer` option despite the backend supporting it.

**Fix**: One-line change — add `<option value="transfer">Transfer</option>`.

### Blocker 3: No review_reason Visibility for Rejected Items
**Source**: UX Reviewer

The register table doesn't show `review_reason`. Phase 2's rejected audit requires expanding every row individually to see why it was rejected. No filtering by review_reason either.

### Blocker 4: Phase 0 Fixes Should Use API, Not SQL
**Source**: UX Reviewer

Direct SQL bypasses the AuditEvent audit trail. The API's PATCH endpoint supports all the needed field changes and creates proper audit records.

---

## REVISED PRIORITY ORDER

```
IMMEDIATE (Today, March 19):
  1. Travis checks: Was Form 7004 filed for BlackLine 2025 1065? If not, FILE NOW.
  2. Travis checks: Were any 1099-NECs needed for 2025? If yes, file late.

Before April 15 (hard deadline):
  3. File Form 4868 (1040 extension) — unless 2025 return will be ready
  4. Calculate and pay 2025 estimated tax balance with extension
  5. Calculate and pay Q1 2026 estimated tax (1040-ES)

Phase 0 (data fixes — prerequisite for everything):
  6. Fix Shopify Billing: direction→expense, category→SUPPLIES/ECOMMERCE_PLATFORM
  7. Fix transfers: direction→transfer, category→CAPITAL_CONTRIBUTION
  8. Identify Mspbna $22k (Travis input)
  9. Locate Cardinal Health income (Travis input)
  10. Add transfer to UI dropdown (1-line fix)
  11. Fix review queue to show auto_classified (code change)

Phase 1-3 (infrastructure, rejected audit, confirmation):
  12. Install deps, run tests
  13. Audit rejected (mostly correct, ~1.5 hrs)
  14. Category-by-category review and batch confirm

Phase 4-5 (v3 features, tax prep):
  15. Remaining v3 work
  16. Tax export validation
  17. B&O filing for both entities
```

---

## PLAN QUALITY RATINGS

| Reviewer | Rating | Key Praise | Key Criticism |
|---|---|---|---|
| Accounting | 3.5/5 | Correctly identifies top 4 data issues; sound Phase 0 sequencing | 3 factual errors that would cause financial harm; missed already-implemented features |
| Tax | -- | Reimbursable treatment correct; home office calc correct | Missed Form 1065 deadline; 1099-NEC obligation; B&O rate wrong; health insurance conflation |
| UX | -- | Plan is well-organized; dashboard has good batch capabilities | Phase 3 is broken as written; audit trail bypassed by SQL approach |

**Overall**: The plan's *structure* is sound — Phase 0 before everything else was the right call. But it contains factual errors in rates and categories, misses time-sensitive deadlines, and proposes a Phase 3 workflow that technically cannot work without a code fix. All fixable, but must be fixed before execution.

---

*Rule of Acquisition #9: "Opportunity plus instinct equals profit." The opportunity here is catching these issues before they became costly. The instinct was to get independent reviewers. The profit is not paying IRS penalties.*
