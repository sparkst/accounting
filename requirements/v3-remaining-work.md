# Remaining Work — v3 Backlog

> Items not yet implemented. All are P2 (polish/improvement) or P3 (nice-to-have).
> The system is production-ready for daily use without these.

---

## P2 — Polish & Improvements

### REQ-V3-001: Live Testing of Compact Review Mode
- **Status**: Code built (T-105), untested with live data
- **What**: Verify compact mode (40px single-row cards) works correctly with 15+ items
- **Acceptance**: Toggle between Comfortable/Compact, keyboard shortcuts work in both modes, 18-20 items visible per viewport in compact
- **Blocked by**: Empty review queue during UAT — test when new transactions arrive

### REQ-V3-002: Live Testing of Priority Grouping Headers
- **Status**: Code built (T-106), untested with live data
- **What**: Verify priority groups (Amount Missing, Low Confidence, New Vendors, etc.) display correctly with sticky headers
- **Acceptance**: Groups render with count badges, collapsible, empty groups hidden
- **Blocked by**: Empty review queue during UAT

### REQ-V3-003: End-to-End Bank CSV Import via Dashboard
- **Status**: UI built (T-111), needs end-to-end testing
- **What**: Drag-and-drop CSV on reconciliation page, preview columns, confirm import, auto-reconcile
- **Acceptance**: User can import a bank CSV without CLI assistance, results show imported/matched/duplicate counts
- **Test with**: New bank statement CSV

### REQ-V3-004: WooCommerce CSV Import with Real Data
- **Status**: Adapter built (T-119), needs Travis's actual CSV
- **What**: Import BlackLine WooCommerce order history from 2025
- **Acceptance**: Orders imported as BlackLine/SALES_INCOME, deduped, per-record error isolation
- **Files**: src/adapters/woocommerce_csv.py, POST /api/import/woocommerce-csv

### REQ-V3-005: Tax Year Locking UI Verification
- **Status**: Backend + UI built (T-117), needs live testing
- **What**: Lock/unlock filed tax years on accounts page, verify mutation guards work
- **Acceptance**: Locking 2025 prevents editing 2025 transactions, unlocking re-enables

### REQ-V3-006: Duplicate Invoice Warning Live Test
- **Status**: Logic built (T-119), needs testing with actual invoice generation
- **What**: Generate button disabled when invoice exists for target month
- **Acceptance**: Shows "Invoice CH20260228 already exists for February 2026" with link

### REQ-V3-007: Shopify API Integration
- **Status**: Adapter built (T-012), never connected to live Shopify
- **What**: Connect Shopify with SHOPIFY_API_KEY + SHOPIFY_STORE_URL, sync BlackLine orders
- **Acceptance**: Orders/refunds/fees/payouts appear in register tagged as BlackLine
- **Blocked by**: Travis adding Shopify API credentials to .env or Doppler

### REQ-V3-008: Cross-Source Dedup on Import
- **Status**: Manual dedup done (16 pairs merged), needs automation
- **What**: When importing bank CSV, automatically detect and merge duplicates with existing Gmail transactions
- **Acceptance**: Import shows "12 matched to existing email receipts" instead of creating duplicates
- **Files**: src/adapters/bank_csv.py, src/utils/dedup.py

### REQ-V3-009: Bank CSV Vendor Name Cleanup
- **Status**: clean_bank_description() built, applied to 17 records
- **What**: Automatically clean raw ACH strings on all future imports (not just backfill)
- **Acceptance**: New bank CSV imports show "Shopify" not "ORIG CO NAME:SHOPIFY..."
- **Files**: src/adapters/bank_csv.py (apply clean_bank_description in _process_row)

### REQ-V3-010: Fascinate First Invoice Generation
- **Status**: All code built (iCal parser, calendar generator, PDF, dashboard wizard)
- **What**: Travis generates first Fascinate invoice from Google Calendar export
- **Acceptance**: Upload .ics → select sessions → generate invoice with correct line items → download PDF
- **Blocked by**: Travis exporting Google Calendar .ics file

---

## P3 — Nice-to-Have / Future

### REQ-V3-011: Monthly P&L Report
- Dashboard widget showing income/expenses/net by month with sparkline trend

### REQ-V3-012: Recurring Invoice Automation
- Auto-generate Cardinal Health invoice on the 1st of each month (currently one-click)

### REQ-V3-013: Google Calendar API Integration
- Replace manual iCal export with direct Google Calendar API for Fascinate session fetching

### REQ-V3-014: Mobile-Responsive Dashboard
- Current dashboard is desktop-first; add responsive breakpoints for tablet/phone

### REQ-V3-015: Data Export/Backup Automation
- Scheduled SQLite backup to SGDrive with integrity verification

### REQ-V3-016: Multi-User Support
- Currently single-user (Travis). Add basic auth if others need access.

### REQ-V3-017: Improved Anomaly Detection
- Use historical patterns to flag unusual spending (currently >2x vendor average)
- Add seasonal adjustments, trend-based detection

### REQ-V3-018: Receipt Image OCR Improvements
- HEIC conversion support, better amount extraction from PDFs
- Claude Vision API for complex receipt layouts

---

## Completed Work Reference

### This Session (6 commits, 1025 tests)
- 24 original QRALPH tasks (full accounting system + invoicing)
- 7 P0 bug fixes + WA DOR B&O upload format
- 21 P1/P2 improvement tasks (insights, tax compliance, UX)
- Currency detection + conversion (Frankfurter API)
- Stripe sync + 4 bank/CC CSV imports
- Auto-classification + cross-source dedup
- Code review (7 issues, 5 fixed) + simplification (182 lines extracted)
- UAT walkthrough with all findings fixed

### Test Coverage: 1,025 tests
### Python Files: 101 | Svelte Files: 16 | Vendor Rules: 59
### Transactions: 335 (205 active, 130 rejected)
