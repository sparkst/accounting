# CFO Review — Accounting System Gap Analysis

**Reviewer:** Quark (acting as CFO for Sparkry AI LLC + BlackLine MTB LLC)
**Date:** 2026-03-24
**Scope:** Full system review — what does the CFO need to run the business?

---

## What We Have (Inventory)

### Pages (12)
1. Dashboard — BLUF next action, monthly summary, deadlines
2. Register — Transaction list with inline editing, filters, search
3. Review — Queue for needs_review items
4. Financials — Income Statement / P&L with entity comparison, monthly drill-down
5. Cash Flow — Operating/Investing/Financing statement
6. Tax — IRS line items, readiness, Y-o-Y comparison, export
7. Invoices — Generation, tracking, SAP Ariba checklist
8. AR Aging — Receivables aging buckets, per-customer breakdown
9. Health — Source freshness, classification stats, Claude API usage
10. Rules — Vendor classification rules (auto-learning)
11. Reconciliation — Stripe/Shopify payout ↔ bank deposit matching
12. Monthly Close — 5-step guided checklist

### Backend
- FastAPI with ~15 endpoints
- SQLite with SQLAlchemy ORM
- 6 data adapters (Stripe, Shopify, Gmail/n8n, Bank CSV, Brokerage CSV, Photo Receipt)
- 3-tier classification (rules → patterns → Claude API)
- Tax export (FreeTaxUSA, TaxAct, WA B&O DOR)
- Reconciliation engine with manual match + auto-match
- Invoice generation (flat rate + calendar-based)

---

## CFO Priority Gaps (What I Need to Run the Business)

### P0 — Can't Operate Without These

**1. No authentication whatsoever**
The API binds to localhost:8000 with zero auth. Anyone on the local network can read/modify all financial data. For a system handling tax records and bank data, this is unacceptable even for a solo operation.
- Need: At minimum, API key auth. Ideally, session-based login.

**2. No backup/restore mechanism**
SQLite is the single source of truth. If the disk fails or the file corrupts, all financial data is lost. The SGDrive sync is not a proper backup.
- Need: Scheduled SQLite backup (daily `.backup` command), backup verification, restore procedure.

**3. No bank account tracking**
We have transactions but no concept of bank accounts or credit card accounts. The reconciliation matches Stripe payouts to bank deposits, but there's no running bank balance, no account-level view.
- Need: Account model (checking, savings, credit card) with running balances and statement reconciliation.

### P1 — Significant Gaps for Tax Season

**4. No 1099 tracking dashboard**
The Transaction model has `payer_1099` and `payer_1099_type` fields, but there's no UI to view/manage 1099 data. During tax season, the CFO needs to know: which payers issued 1099s, do the amounts match, are any missing?
- Need: 1099 tracking page showing expected vs received 1099s by payer.

**5. No WA B&O filing workflow**
We calculate B&O amounts but there's no guided filing workflow. The monthly B&O table exists on the Tax page but doesn't connect to the DOR upload format or filing process.
- Need: B&O filing wizard — calculate, review, export DOR format, mark as filed.

**6. No estimated tax payment tracking (beyond projection)**
The Tax page projects estimated quarterly payments but doesn't track actual payments made. No way to record "I paid $X to IRS on date Y."
- Need: Estimated tax payment log — record payments, compare projected vs actual, track remaining liability.

**7. No expense receipt attachment**
Transactions can have `attachments` and `raw_data` but there's no UI to attach a receipt photo to a specific transaction. The photo_receipt adapter exists but is import-only — no way to associate a photo with an existing transaction.
- Need: Receipt attachment on TransactionCard — upload/capture photo, link to transaction, OCR extraction.

**8. No year-end close / annual summary**
Monthly close exists but no annual close workflow. Tax season needs: all transactions categorized, all 1099s reconciled, all deductions documented, all exports generated.
- Need: Annual close checklist building on monthly close, with tax-specific steps.

### P2 — Would Significantly Improve Financial Management

**9. No budget/forecast capability**
We show actuals but can't set budgets or compare against them. A CFO needs to know: are we on track for the year? Are expenses exceeding plan?
- Need: Simple budget model (annual by category), budget vs actual comparison on Financials page.

**10. No profit by client/project**
Revenue comes from multiple sources (Fascinate consulting, Cardinal Health, Substack, product sales) but there's no profit-per-client view. As CFO I need to know: which client is most profitable after expenses?
- Need: Client/project profitability report.

**11. No multi-year trend analysis**
Y-o-Y comparison exists on the Tax page but only compares two years. No 3-5 year trend charts for revenue, expenses, or profit trajectory.
- Need: Multi-year trend dashboard with charts.

**12. No depreciation / asset tracking**
If the business owns equipment (computers, bikes for BlackLine), there's no way to track assets and their depreciation schedule. This affects Schedule C deductions.
- Need: Simple asset register with straight-line depreciation calculation.

**13. No owner's draw / distribution tracking**
For Sparkry (single-member LLC) and BlackLine (partnership), owner draws are tax-relevant. The `transfer` direction exists but there's no dedicated view for tracking draws vs capital contributions.
- Need: Owner's equity view showing contributions, draws, and retained earnings.

**14. No home office deduction calculator**
The Tax tips mention home office but there's no structured calculator. Need: square footage, percentage, associated expenses (rent/mortgage, utilities, internet), simplified method option.
- Need: Home office deduction worksheet integrated into Tax page.

**15. No sales tax tracking**
BlackLine sells physical products. If selling in WA or other states with nexus, sales tax collected/remitted needs tracking.
- Need: Sales tax liability view (if applicable based on nexus).

### P3 — Nice to Have

**16. No mobile-responsive dashboard**
Nav dropdowns help but the data tables and financial statements aren't optimized for phone screens.

**17. No email/notification for deadlines**
Tax deadlines show on the Dashboard but there's no push notification or email reminder.

**18. No document storage / tax folder**
No central place to store tax-related documents (1099s received, filing confirmations, correspondence with CPA).

**19. No CPA collaboration view**
If working with an accountant or CPA, there's no read-only view or export package for them to review.

**20. No chart/graph visualizations on Dashboard**
The Dashboard is text-heavy. A simple revenue/expense trend chart would give instant visual context.

---

## Edge Cases to Address

1. **Split transaction with reimbursement** — Can you split a transaction where one line is reimbursable and the other isn't? Does the reimbursement linking work across splits?
2. **Negative income (refund to customer)** — How are refunds from Stripe handled in the P&L? Are they reducing income or showing as an expense?
3. **Late 1099 corrections** — If a 1099 amount changes after initial filing, can the system track corrections?
4. **Multi-currency** — All amounts assume USD. What if Shopify processes a CAD transaction?
5. **Year boundary transactions** — A Stripe charge on Dec 31 with a bank deposit on Jan 2. Which tax year? The system uses transaction date but the bank CSV shows the deposit date.
6. **Duplicate detection across sources** — A Stripe charge and a Gmail receipt for the same purchase. The dedup uses source_hash (source + source_id) so these would be different hashes. Need cross-source dedup.
7. **Retroactive rule changes** — If a vendor rule is updated, existing transactions classified by the old rule aren't reclassified. Should they be?
8. **Empty states on new pages** — Cash Flow, AR Aging, Monthly Close have empty states but they should guide the user to import data, not just say "no data."

---

## Recommendations (Prioritized)

### Immediate (before tax season)
1. Add backup/restore for SQLite
2. Build 1099 tracking dashboard
3. Add estimated tax payment log
4. Build annual close checklist

### Next quarter
5. Add bank account model with running balances
6. Receipt attachment on transactions
7. B&O filing wizard
8. Budget vs actual on Financials

### Long-term
9. Client profitability reporting
10. Multi-year trends
11. Asset/depreciation tracking
12. Mobile responsiveness
