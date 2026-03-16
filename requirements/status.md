# Requirements Status

> Updated: 2026-03-16 after initial build session

## Status Legend
- **Done** — Implemented, tested, working in dashboard
- **Partial** — Core functionality works, gaps noted
- **Not Started** — No code written yet

---

## Original Requirements (REQ-001 through REQ-024)

| REQ | Description | Status | Notes |
|-----|-------------|--------|-------|
| REQ-001 | Gmail/n8n Ingestion | **Done** | 131 records ingested from keep/. Reads JSON, extracts vendor/date/amount (body + subject + HTML fallback), links attachments, dedup, payment method extraction. Skips Shopify order notifications. |
| REQ-002 | Deduction Email Ingestion | **Not Started** | `deductions/` folder doesn't exist in n8n yet. Need n8n workflow update first. |
| REQ-003 | Stripe Integration | **Not Started** | Research needed on API scopes. Stripe MCP plugin connected but adapter not built. |
| REQ-004 | Shopify Integration | **Not Started** | Research needed on Admin API endpoints. Shopify order notification emails are now filtered out (sales data will come from this integration). |
| REQ-005 | Brokerage CSV Import | **Not Started** | Need CSV format samples from E*Trade, Schwab, Vanguard. |
| REQ-006 | Bank CSV Import | **Not Started** | Needed for credit card statement reconciliation. `payment_method` field (card last-4) is ready for cross-referencing. |
| REQ-007 | Photo Receipt Processing | **Partial** | Claude CLI OCR extraction works via "Extract" button on review cards + auto-extract during ingestion. Not using Vision API directly (uses CLI subprocess). HEIC conversion not implemented. |
| REQ-008 | Deduplication | **Done** | Two-pass: file-level SHA256 via IngestedFile, record-level via Transaction.source_hash. Verified: re-running adapter creates 0 duplicates. Cross-source fuzzy matching not yet implemented. |
| REQ-009 | Three-Tier Classification | **Done** | 30+ vendor rules (Tier 1), source-based patterns (Tier 2), Claude API fallback (Tier 3, requires API key). 122/131 auto-classified on first run. |
| REQ-010 | Learning Loop | **Done** | Every confirm creates/updates VendorRule. Confidence and examples count tracked. Corrections update rules. |
| REQ-011 | Line-Item Splitting | **Not Started** | Model supports parent_id FK. Splitter module not built. Hotel splitting map in design spec. |
| REQ-012 | Reimbursable Expense Tracking | **Partial** | `direction: reimbursable` exists. Cardinal Health cafe receipts classified as REIMBURSABLE. Linking expense-to-reimbursement not implemented. 30-day overdue flag not implemented. |
| REQ-013 | Dashboard — Review Queue | **Done** | Full implementation: pre-filled entity/category/subcategory dropdowns, confidence tooltip, keyboard shortcuts (y=confirm, j/k=navigate), inline email viewer with HTML rendering, image preview, PDF viewer (click-to-load), editable amounts, notes field, Extract button (Claude CLI OCR), filters (entity, category, amount, date presets), "Book with Ben" exclusion pattern for calendar customers. |
| REQ-014 | Dashboard — Register View | **Done** | Sortable columns (all 6), expandable rows (same TransactionCard component), summary cards (income/expenses/net across all filtered results), page size selector (25/50/100/200), date range presets, filters. |
| REQ-015 | Dashboard — Health | **Not Started** | Placeholder page. GET /api/health endpoint exists with source freshness and classification stats. Need dashboard UI. |
| REQ-016 | Dashboard — Tax Summary | **Not Started** | Placeholder page. Need per-entity IRS line-item breakdown, B&O subtotals, export buttons. |
| REQ-017 | Dashboard — Accounts & Memory | **Not Started** | Placeholder page. Need vendor rules table (CRUD), entity config, tax deadlines. |
| REQ-018 | Tax Export — FreeTaxUSA | **Not Started** | Research needed on CSV import format. |
| REQ-019 | Tax Export — TaxAct | **Not Started** | Research needed on 1065 import format. |
| REQ-020 | B&O Tax Reports | **Not Started** | Date presets for B&O periods exist in dashboard. Need revenue classification and report generation. |
| REQ-021 | Error Handling | **Partial** | Per-record error isolation works in adapter. IngestionLog model exists. Retry with backoff not implemented. Auth failure halting not implemented. Staleness detection not implemented. |
| REQ-022 | Reconciliation | **Not Started** | `payment_method` field (card last-4) ready for matching. Need bank CSV import first. |
| REQ-023 | GAAP Cash-Basis | **Partial** | Cash-basis recording works (date = when paid). Full audit trail via AuditEvent. Reimbursable netting not fully implemented. |
| REQ-024 | Dashboard UX | **Done** | Column sorting, date presets (16 options across 3 groups), search, stackable filters, Apple design. |

---

## New Requirements (discovered during build session)

### REQ-025: Invoicing System
- **Status:** PRD written (`requirements/invoicing-prd.md`)
- See REQ-INV-001 through REQ-INV-010
- Two customer workflows: calendar-based (Fascinate) and flat-rate (Cardinal Health)

### REQ-026: HTML Body Amount Extraction
- **Status:** Done
- When body_text has no amount, fall back to body_html for extraction
- Catches vendors like Fiverr (HTML-only receipts) and DHL (amounts in HTML tags)

### REQ-027: Forwarded Email Vendor Extraction
- **Status:** Done
- Self-forwarded emails (from Travis Sparks) parse the real vendor from the forwarded `From:` header
- Also checks forwarded `Subject:` for "Payment receipt from X" patterns
- 14 forwarded emails resolved to real vendors (Cloudflare, DHL, Apple, Alaska Airlines, etc.)

### REQ-028: Payment Method Tracking
- **Status:** Done
- `payment_method` field on Transaction (e.g., "VISA ****5482")
- Extracted from body_text and body_html via regex
- 21 existing transactions backfilled
- Purpose: cross-reference with credit card statements for reconciliation (REQ-006, REQ-022)

### REQ-029: Shopify Order Email Filtering
- **Status:** Done
- Gmail adapter skips Shopify order notification emails (subject pattern `[Store] Order #XXXX`)
- Sales data will come from Shopify API integration (REQ-004) instead
- 19 existing Shopify order emails rejected

### REQ-030: Receipt OCR via Claude CLI
- **Status:** Done
- "Extract" button on review cards sends image/PDF to Claude CLI for OCR
- Auto-extract during ingestion when body is empty + image/PDF attachment exists
- Returns: vendor, amount, date, entity_hint
- No API key needed — uses CLI auth

### REQ-031: Missing Amount → Needs Review
- **Status:** Done
- Transactions with NULL amounts always route to needs_review regardless of classification confidence
- Enforced in classification engine's apply_result()

### REQ-032: Subcategory Tracking
- **Status:** Done
- TaxSubcategory enum with 30+ values across events, meals, shipping, manufacturing, marketing, etc.
- Subcategory dropdown in review cards (context-dependent on selected category)
- Saved on confirm, feeds into vendor rule learning loop

### REQ-033: Attachment Inline Viewing
- **Status:** Done
- Images render inline in review cards and register expanded rows
- PDFs load on-demand via "View PDF" button (no auto-download)
- File serving endpoint at `/api/attachments/serve` with path validation

### REQ-034: Date Range Presets
- **Status:** Done
- 16 presets in 3 groups: Standard (today through last quarter), Tax/Fiscal (YTD, tax years), B&O Filing (monthly Sparkry, quarterly BlackLine)
- Available on both review queue and register pages

---

## Priority for Next Session

### High Priority (blocks tax filing)
1. **REQ-016: Tax Summary page** — Need per-entity IRS line-item totals to verify before filing
2. **REQ-020: B&O Tax Reports** — Monthly Sparkry, quarterly BlackLine, due regularly
3. **REQ-018/019: Tax Exports** — FreeTaxUSA + TaxAct CSV formats

### Medium Priority (blocks completeness)
4. **REQ-003: Stripe Integration** — Sparkry subscription income + BlackLine payment processing
5. **REQ-004: Shopify Integration** — BlackLine sales, fees, payouts
6. **REQ-006: Bank CSV Import** — Reconciliation against credit card statements
7. **REQ-025: Invoicing** — PRD written, ready to build

### Lower Priority (quality of life)
8. **REQ-015: Health Dashboard** — API exists, need UI
9. **REQ-017: Accounts & Memory** — Vendor rules CRUD
10. **REQ-002: Deduction Emails** — Depends on n8n workflow update
11. **REQ-011: Line-Item Splitting** — Hotels, mixed receipts
12. **REQ-005: Brokerage CSV** — Periodic, can wait
