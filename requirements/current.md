# Accounting System Requirements

## REQ-001: Gmail/n8n Ingestion
- Acceptance: System reads JSON files from SGDrive/LIVE_SYSTEM/accounting/{keep,for-review,manual,deductions}/, extracts transaction data, links attachments by hex ID, and writes to register
- Non-Goals: Replacing n8n pipeline; system works downstream of it

## REQ-002: Deduction Email Ingestion
- Acceptance: System ingests deduction-related emails (charitable, mortgage, medical, property tax) from a new `deductions/` folder, classifies as personal deductions
- Non-Goals: Automatically categorizing all deduction subtypes without human review

## REQ-003: Stripe Integration
- Acceptance: System connects to Stripe API, pulls charges/payouts/invoices for both Sparkry and BlackLine, maps to correct entity, identifies Substack subscription income
- Non-Goals: Writing to Stripe

## REQ-004: Shopify Integration
- Acceptance: System connects to Shopify Admin API, pulls orders/refunds/fees/payouts for BlackLine MTB, all auto-tagged as BlackLine entity
- Non-Goals: Modifying Shopify data

## REQ-005: Brokerage CSV Import
- Acceptance: System imports CSV exports from E*Trade, Schwab, Vanguard with correct column mapping, tracks cost basis, short/long term classification
- Non-Goals: Automatic brokerage API connection (Plaid)

## REQ-006: Bank CSV Import
- Acceptance: System imports bank statement CSVs with configurable column mapping per bank, cross-references against other sources
- Non-Goals: Real-time bank feeds

## REQ-007: Photo Receipt Processing
- Acceptance: System processes JPG/PNG/HEIC images via Claude Vision API, extracts vendor/date/line items/amounts/total, flags low-confidence extractions for review
- Non-Goals: Perfect OCR — human review expected for low confidence

## REQ-008: Deduplication
- Acceptance: SHA256-based dedup detects same-source duplicates (auto-skip) and cross-source duplicates (flag for review). No duplicate transactions in confirmed register.
- Non-Goals: Fuzzy matching on partial data

## REQ-009: Three-Tier Classification
- Acceptance: Vendor rules → pattern matching → LLM classification. Every transaction gets entity + tax_category + direction. Confidence score attached. Items below 0.7 confidence routed to needs_review.
- Non-Goals: 100% auto-classification from day one

## REQ-010: Learning Loop
- Acceptance: Every human interaction (first-time assignment, confirmation, correction, entity/category/vendor changes) creates or updates a VendorRule. System classification accuracy improves over time.
- Non-Goals: Unsupervised learning without human confirmation

## REQ-011: Line-Item Splitting
- Acceptance: Hotels and mixed receipts split into child transactions with correct tax categories and deductible percentages. Children must sum to parent total (flag if not).
- Non-Goals: Splitting every multi-line invoice

## REQ-012: Reimbursable Expense Tracking
- Acceptance: Cardinal Health pass-through expenses tracked separately. Linked to reimbursement when received. Both net to zero on P&L. Overdue reimbursements flagged after 30 days.
- Non-Goals: Auto-generating invoices to Cardinal Health

## REQ-013: Dashboard — Review Queue
- Acceptance: Web dashboard (localhost:5173) shows items needing review. Pre-filled dropdowns for entity/category. One-click confirm. Keyboard shortcuts. Sortable, filterable, searchable.
- Non-Goals: Mobile app

## REQ-014: Dashboard — Register View
- Acceptance: Full transaction list, sortable by all columns, inline editing, running totals, export buttons
- Non-Goals: Infinite scroll (pagination is fine)

## REQ-015: Dashboard — Health Dashboard
- Acceptance: Source status with staleness warnings, failure log with retry actions, classification stats, upcoming tax deadlines, account memory stats
- Non-Goals: Real-time monitoring

## REQ-016: Dashboard — Tax Summary
- Acceptance: Per-entity IRS line-item breakdown with amounts. Warning if unconfirmed transactions affect totals. B&O subtotals. Export buttons for FreeTaxUSA and TaxAct formats.
- Non-Goals: Filing taxes directly

## REQ-017: Dashboard — Accounts & Memory
- Acceptance: Editable vendor rules table, entity configuration, tax deadline calendar
- Non-Goals: Multi-user access control

## REQ-018: Tax Export — FreeTaxUSA
- Acceptance: CSV export matching FreeTaxUSA import format for Schedule C, K-1 data, Schedule A deductions, 1099-B transactions
- Non-Goals: Direct API integration with FreeTaxUSA

## REQ-019: Tax Export — TaxAct
- Acceptance: CSV export matching TaxAct Business 1065 import format for BlackLine MTB partnership return
- Non-Goals: Direct API integration with TaxAct

## REQ-020: B&O Tax Reports
- Acceptance: Revenue figures for WA B&O tax filing. Monthly for Sparkry, quarterly for BlackLine. Correct revenue classification codes.
- Non-Goals: Auto-filing B&O returns

## REQ-021: Error Handling
- Acceptance: Per-record error isolation (one bad record doesn't halt batch). IngestionLog for every adapter run. Retry with backoff for transient failures. Auth failures halt and surface immediately. Staleness detection per source.
- Non-Goals: Self-healing without human awareness

## REQ-022: Reconciliation
- Acceptance: Automated checks that Stripe/Shopify payouts appear in bank statements. Flag unmatched items. Monthly total sanity checks.
- Non-Goals: Real-time reconciliation

## REQ-023: GAAP Cash-Basis Compliance
- Acceptance: Revenue recorded when received, expenses when paid. Consistent classification. Full audit trail. Reimbursables properly netted.
- Non-Goals: Accrual basis or double-entry bookkeeping

## REQ-024: Dashboard UX — Sorting, Filtering, Search
- Acceptance: All list views support column-header sorting (asc/desc toggle), date picker with presets (Today, This Week, This Month, This Quarter, YTD, Last Year, Custom), type-ahead search, stackable filters. Apple design principles: minimal, zero friction, progressive disclosure.
- Non-Goals: Saved/named filter presets (v1)
