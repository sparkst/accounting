# Invoice Send Flow — Stripe Payment Link + Resend Email

## Overview
When a user finishes generating an invoice, they should be able to send it to the customer with one click. The system creates a Stripe payment link, generates the PDF, and emails both via Resend.

## Credentials
- **Stripe**: `STRIPE_RESTRICTED_KEY` from Doppler (`claude-code` project, `dev` config). Key prefix: `rk_live_`. Has permissions for Products, Prices, Payment Links (write) and Charges, Refunds, Payouts, Balance Transaction Source, Payment Intents, Customers (read). Platform account is `acct_1RnR1rA6Im2mQkXF` (Sparkry). No connected accounts needed — payment links go directly on the Sparkry account.
- **Resend**: `RESEND_API_KEY` from Doppler (`claude-code` project, `dev` config). Key prefix: `re_`. Sender domain: sparkry.com (travis@sparkry.com).

Add both keys to `.env` (gitignored) and `.env.example`.

## Backend

### 1. New module: `src/invoicing/payment_link.py`
Create a Stripe payment link for an invoice:
- Create a one-time Stripe Product named `"Sparkry Invoice {invoice_number}"`
- Create a Price for the invoice total (in cents, USD)
- Create a Payment Link from that price (quantity=1)
- Return the payment link URL

### 2. New module: `src/invoicing/email_sender.py`
Send invoice email via Resend:
- To: customer's `contact_email` (Customer model already has this column)
- From: `travis@sparkry.com`
- Subject: `"Invoice {invoice_number} from Sparkry LLC"`
- Body: professional HTML email with payment link button and invoice summary (amount, service period, due date)
- Attachment: invoice PDF (generated via existing `render_pdf()`)
- Use `resend` Python package (`pip install resend`)

### 3. New endpoint: `POST /api/invoices/{id}/send`
In `src/api/routes/invoices.py`:
- Accepts optional `to_email` override (defaults to customer.contact_email)
- Creates Stripe payment link (via payment_link.py)
- Generates PDF (via existing pdf_renderer.py)
- Sends email (via email_sender.py)
- Stores `payment_link_url` on the invoice (new column — see migration below)
- Transitions invoice status from `draft` → `submitted` (or allow sending from draft without transition — user's choice)
- Returns the updated invoice with payment_link_url

### 4. Migration: add columns to Invoice
- `payment_link_url: VARCHAR(512), nullable` — Stripe payment link URL
- `payment_link_id: VARCHAR(255), nullable` — Stripe payment link ID
- `sent_at: DATETIME, nullable` — when the email was sent
- `sent_to: VARCHAR(255), nullable` — email address it was sent to

### 5. Update Customer record
- Set `contact_email = 'ben@benthole.com'` for How To Fascinate customer (`4e7df1ee-c1c3-5182-be3a-f54be5588211`)

## Frontend

### Generated invoice view (`dashboard/src/routes/invoices/new/+page.svelte`)
Add to the generated invoice card (after the date pickers):
- **Email field**: text input, pre-filled with customer.contact_email, editable
- **"Send Invoice" button**: calls `POST /api/invoices/{id}/send` with the email
- **Loading state**: spinner while creating payment link + sending
- **Success state**: show "Sent to {email}" with payment link URL displayed

### API client (`dashboard/src/lib/api.ts`)
- Add `sendInvoice(invoiceId: string, toEmail?: string)` function

### Types (`dashboard/src/lib/types.ts`)
- Add `payment_link_url`, `sent_at`, `sent_to` to Invoice interface

## Existing code reference
- PDF rendering: `src/invoicing/pdf_renderer.py` — `render_pdf(invoice, line_items, customer) -> bytes`
- HTML rendering: `src/invoicing/pdf_renderer.py` — `render_html(invoice, line_items, customer) -> str`
- Invoice PATCH: `PATCH /api/invoices/{id}` already supports updating scalar fields
- Status transitions: `PATCH /api/invoices/{id}/status` with `{"status": "submitted"}`
- Invoice model: `src/models/invoice.py` — Invoice, Customer, InvoiceLineItem
- DB connection: `src/db/connection.py` — `SessionLocal()`, `get_db()`

## Testing
- Test payment_link.py with a mock Stripe client
- Test email_sender.py with a mock Resend client
- Test the `/send` endpoint end-to-end with mocked external services
