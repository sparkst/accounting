# Invoicing System PRD

> Sparkry LLC invoice generation, tracking, and reconciliation. Supports multiple customer types with different billing workflows.

## Overview

Sparkry LLC needs to generate, track, and reconcile invoices for consulting clients. Two active customers today with different billing models — the system should be extensible to future customers.

**Entity:** Sparkry AI LLC (all invoicing is Sparkry)
**Dashboard location:** New "Invoices" tab in the navigation

---

## Customers

### Customer 1: How To Fascinate (Ben)

| Field | Value |
|---|---|
| Customer name | How To Fascinate |
| Contact | Ben |
| Project | Fascinate OS |
| Billing model | Hourly ($100/hr introductory rate) |
| Billing unit | 1 calendar meeting = 1 hour |
| Payment terms | Net 14 |
| Invoice # format | YYYYMM-NNN (e.g., 202508-001) |
| Late fee | 10% if paid after due date |
| Data source | iCal export from Google Calendar |
| Meeting patterns | "Ben / Travis", "Fascinate OS", "Fascinate" in subject |
| Exclusions | "Book with Ben" entries (different customer) |

**Billing workflow:**
1. Import iCal file (`.ics` export from Google Calendar)
2. System parses and filters meetings matching the customer's patterns
3. Dashboard shows billable sessions with date, duration, description
4. User selects date range for the invoice period
5. User can adjust: remove sessions, edit descriptions, change rate
6. Generate invoice → PDF matching Sparkry LLC template
7. Mark as sent, track payment status

### Customer 2: Cardinal Health

| Field | Value |
|---|---|
| Customer name | Cardinal Health, Inc. |
| Contact | Charelle Lewis (charelle.lewis@cardinalhealth.com) |
| Ship-to contact | Adeola Ogundipe (adeola.ogundipe@cardinalhealth.com) |
| Project | AI Product Engineering Coaching |
| Billing model | Flat rate ($33,000/month) |
| Payment terms | Net 90 |
| Payment method | ACH |
| Invoice # format | CH + YYYYMMDD (e.g., CH20260131) |
| PO number | 4700158965 |
| Tax ID | 39-4105886 |
| Classification | 111811-L3 (custom domain) |
| Data source | Manual (monthly recurring) |

**Billing workflow:**
1. Select month → system auto-generates invoice with standard fields
2. Dashboard shows SAP Ariba submission instructions:
   - Open existing order (PO# 4700158965)
   - Find the most recent invoice and copy it
   - Update the service period dates (start/end of month)
   - Update the description (e.g., "AI Product Engineering Coaching Month 3")
   - Add the Sparkry invoice number (CH + YYYYMMDD)
   - Submit
3. Generate matching Sparkry-side invoice for records
4. Track: submitted date, expected payment date (90 days), paid date

**Existing invoices:**
- CH20260131: $33,000, service period Jan 5–30 2026, submitted Feb 5 2026
- CH20260228: $33,000, service period Feb 2–27 2026, submitted Mar 2 2026

---

## Data Model

### Invoice

| Field | Type | Description |
|---|---|---|
| id | UUID | Primary key |
| invoice_number | str | Human-readable (YYYYMM-NNN or CH + YYYYMMDD) |
| customer_id | UUID | FK to Customer |
| entity | str | Always "sparkry" for now |
| project | str | Project name (e.g., "Fascinate OS") |
| submitted_date | date | When invoice was created/sent |
| due_date | date | Payment due date |
| service_period_start | date | Start of billing period |
| service_period_end | date | End of billing period |
| subtotal | decimal | Sum of line items |
| adjustments | decimal | Discounts, credits |
| total | decimal | Final amount due |
| tax | decimal | Tax amount (usually $0) |
| status | str | draft, sent, paid, overdue, void |
| paid_date | date | When payment was received (nullable) |
| notes | str | Free-form notes (e.g., "Introductory Rate: $100/hr") |
| late_fee_pct | float | Late fee percentage (e.g., 0.10) |
| payment_terms | str | "Net 14", "Net 90" |
| payment_method | str | "ACH", "Check", etc. |
| po_number | str | Customer PO (nullable) |
| sap_instructions | json | SAP-specific submission steps (nullable) |
| pdf_path | str | Path to generated PDF (nullable) |
| created_at | datetime | |
| updated_at | datetime | |

### InvoiceLineItem

| Field | Type | Description |
|---|---|---|
| id | UUID | Primary key |
| invoice_id | UUID | FK to Invoice |
| description | str | Line item description (e.g., "Nov 4" or "AI Product Engineering Coaching Month 1") |
| quantity | decimal | Units (hours, months) |
| unit_price | decimal | Rate per unit |
| total_price | decimal | qty * unit_price |
| date | date | Session date (for calendar-based items) |
| sort_order | int | Display order on invoice |

### Customer

| Field | Type | Description |
|---|---|---|
| id | UUID | Primary key |
| name | str | Customer name |
| contact_name | str | Primary contact |
| contact_email | str | Email |
| billing_model | str | "hourly", "flat_rate", "project" |
| default_rate | decimal | Default hourly/monthly rate |
| payment_terms | str | "Net 14", "Net 90" |
| invoice_prefix | str | Prefix for invoice numbers (e.g., "" for YYYYMM-NNN, "CH" for CH+date) |
| late_fee_pct | float | Default late fee |
| po_number | str | Standing PO number (nullable) |
| sap_config | json | SAP submission details (nullable) |
| calendar_patterns | json | Patterns to match in iCal subjects (nullable) |
| calendar_exclusions | json | Patterns to exclude (nullable) |
| address | json | Mailing address |
| notes | str | |
| active | bool | |
| created_at | datetime | |

---

## REQ-INV-001: iCal Import & Parsing

**Acceptance:** System reads an `.ics` file, parses VEVENT entries, filters by customer's `calendar_patterns` (excluding `calendar_exclusions`), and returns a list of billable sessions with date, time, duration, and description. Handles recurring events, timezone conversion (UTC → Pacific), and deduplicates by date+time.

**Non-Goals:** Direct Google Calendar API integration (future — use iCal export for now).

## REQ-INV-002: Invoice Generation — Calendar-Based

**Acceptance:** User selects a customer and date range. System shows filtered calendar sessions. User can adjust (remove items, edit descriptions, change rate). Clicking "Generate" creates an Invoice with InvoiceLineItems, one per session. Invoice number auto-incremented per customer's format.

## REQ-INV-003: Invoice Generation — Flat Rate

**Acceptance:** User selects a customer and month. System auto-creates an invoice with one line item for the flat monthly amount. Service period is first-to-last business day of the month. Description auto-generated (e.g., "AI Product Engineering Coaching Month 3").

## REQ-INV-004: PDF Export — Sparkry Template

**Acceptance:** System generates a print-ready PDF matching the existing Sparkry LLC invoice template:
- Orange header bar
- Sparkry LLC address block (24517 SE 43rd Pl, Sammamish, WA 98029, (919) 491-3894)
- Invoice metadata: Submitted on, Invoice for, Payable to, Invoice #, Project, Due date
- Line items table: Description, Qty, Unit price, Total price
- Section header grouping (e.g., "Fascinate OS Project - 10 Hours")
- Notes field
- Subtotal, Adjustments, Total (large, magenta/pink)
- Late fee notice
- Clean, professional, one-page-when-possible

**Implementation:** HTML template rendered to PDF (use browser print or a library like weasyprint/puppeteer).

## REQ-INV-005: SAP Ariba Instructions Panel

**Acceptance:** For Cardinal Health invoices, the dashboard shows a step-by-step SAP Ariba submission guide:
1. Log into SAP Ariba
2. Open existing order (PO# shown)
3. Find the most recent invoice and copy it
4. Update service period dates to match this invoice
5. Update description (shown with month number)
6. Enter Sparkry invoice number (shown)
7. Verify amount ($33,000.00)
8. Submit

Each step has a checkbox. When all checked, mark as "Submitted in SAP."

## REQ-INV-006: Invoice Dashboard Page

**Acceptance:** New "Invoices" page in the dashboard navigation with:
- Customer selector (dropdown or tabs)
- Invoice history table: Invoice #, Date, Customer, Amount, Status, Actions
- Status pills: draft (gray), sent (blue), paid (green), overdue (red)
- Click row to expand → full invoice detail with line items
- "New Invoice" button → opens generation flow (calendar import or flat-rate)
- Filter by status, date range

## REQ-INV-007: Invoice Status Tracking

**Acceptance:** Each invoice tracks its lifecycle:
- **Draft** → created but not sent
- **Sent** → submitted to customer (date recorded)
- **Paid** → payment received (date recorded, links to income transaction in register)
- **Overdue** → past due date and not paid (auto-calculated)
- **Void** → cancelled

Status transitions are audit-logged.

## REQ-INV-008: Payment Reconciliation

**Acceptance:** When a payment arrives (via email receipt, bank CSV, or Stripe), the system suggests matching it to an open invoice based on amount and customer. User confirms the match. Both the invoice (marked paid) and the income transaction are linked.

**Non-Goals:** Auto-matching without human confirmation.

## REQ-INV-009: Invoice Number Sequencing

**Acceptance:** Invoice numbers are auto-generated per customer format:
- Calendar customers: YYYYMM-NNN where NNN increments per month (001, 002, ...)
- Cardinal Health: CH + YYYYMMDD based on invoice date
- No gaps in sequence. Voided invoices keep their number.

## REQ-INV-010: Customer Management

**Acceptance:** Customers are configurable via the dashboard (or seeded). Each customer has: name, billing model, default rate, payment terms, invoice prefix, calendar patterns (for iCal customers), SAP config (for SAP customers).

**Initial seed:**
- How To Fascinate: hourly, $100/hr, Net 14, patterns ["Ben / Travis", "Fascinate"]
- Cardinal Health: flat_rate, $33,000/mo, Net 90, PO# 4700158965

---

## Sparkry LLC Invoice Template Reference

```
┌─────────────────────────────────────────────────┐
│ ████████████████████ ORANGE BAR ████████████████ │
│                                                 │
│  Sparkry LLC                                    │
│  24517 SE 43rd Pl                               │
│  Sammamish, WA 98029                            │
│  (919) 491-3894                                 │
│                                                 │
│  Invoice                                        │
│                                                 │
│  Submitted on:  MM/DD/YYYY                      │
│                                                 │
│  Invoice for     Payable to     Invoice #       │
│  [Customer]      Sparkry LLC    YYYYMM-NNN      │
│                                                 │
│                  Project        Due date         │
│                  [Project]      MM/DD/YYYY       │
│                                                 │
│  ─────────────────────────────────────────────── │
│                                                 │
│  Description              Qty  Unit price Total  │
│  ─────────────────────────────────────────────── │
│  [Section Header] - N Hours                     │
│    Date 1                  1   $100.00  $100.00  │
│    Date 2                  1   $100.00  $100.00  │
│    ...                                          │
│                                                 │
│  ─────────────────────────────────────────────── │
│  Notes: [rate info]        Subtotal  $X,XXX.XX  │
│                            Adjustments          │
│                            ───────────────────  │
│                            $X,XXX.XX  (LARGE)   │
│                                                 │
│  * 10% Late Fee if paid after due date.         │
└─────────────────────────────────────────────────┘
```

---

## Implementation Notes

- **PDF generation:** Render an HTML template that matches the Sparkry invoice design, then print to PDF via the browser's print dialog or `weasyprint` on the backend. The HTML version is also useful for preview in the dashboard.
- **iCal parser:** Python `icalendar` library for parsing `.ics` files. Handle VTIMEZONE, RRULE (recurring), and EXDATE (cancelled occurrences).
- **Database:** Add Invoice, InvoiceLineItem, Customer tables to the existing SQLite schema via Alembic migration.
- **API:** New router `src/api/routes/invoices.py` with CRUD + generate + PDF endpoints.
- **Dashboard:** New `dashboard/src/routes/invoices/` with list, detail, and generate views.

---

## Future Enhancements

- Direct Google Calendar API integration (replace iCal import)
- Email invoice directly to customer from the dashboard
- Recurring invoice auto-generation (monthly CH invoices)
- Multi-currency support
- QuickBooks / Xero export
- Stripe invoice integration for payment collection
