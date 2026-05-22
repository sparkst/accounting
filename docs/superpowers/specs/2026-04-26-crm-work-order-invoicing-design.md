# CRM + Work Order Invoicing System

**Date:** 2026-04-26
**Status:** Design approved + reviewed
**Platform:** Cloudflare Pages + D1 + Workers
**Domain:** internal.sparkry.ai

---

## Purpose

Standalone CRM for managing customers, work orders (discrete projects), and milestone-based invoicing. Replaces the local accounting system's invoicing UI. Designed for a non-technical operator (Amy, EA) to manage the full invoice lifecycle independently.

Primary user: Amy Sparks (amycsparks@gmail.com) — full admin, no prior invoicing experience.
Secondary user: Travis Sparks (travis@sparkry.com) — full admin.

---

## Architecture

**Full-stack SvelteKit on Cloudflare Pages** (single deployment):

- SvelteKit pages with SSR via Cloudflare Workers
- SvelteKit server routes (`+server.ts`) as the API layer with D1 bindings
- Cloudflare D1 (SQLite-compatible) for persistence
- Google OAuth for authentication (allowlist: travis@sparkry.com, amycsparks@gmail.com)
- Cloudflare Access as defense-in-depth layer (all routes except webhook endpoint)
- Resend for email alerts, invoice delivery, and bounce tracking
- Stripe for payment links (CC, ACH, Venmo)
- Cloudflare Cron Trigger (Workers `scheduled()` handler) for daily milestone checks
- Sentry (Cloudflare Workers SDK) for error monitoring
- HTML invoice template with browser print-to-PDF (no server-side PDF generation)

### Key Architectural Decisions

- **Amounts stored as integer cents** in D1 (multiply by 100). Avoids floating-point corruption in financial data. Display layer divides by 100.
- **D1 `batch()` API** for all multi-table writes (atomicity guarantee).
- **Optimistic locking** via `updated_at` on all mutable records — prevents concurrent edit loss.
- **Stripe SDK vs raw fetch:** Evaluate bundle size. If `stripe` SDK exceeds Worker size budget, use raw `fetch()` for the 3 endpoints needed (create payment link, retrieve session, construct webhook event).
- **DB helper pattern:** `getDB(platform)` returns drizzle client. No global singletons (Workers model).

---

## Data Model

### Customers

Migrated from existing system. Fields:

| Field | Type | Purpose |
|-------|------|---------|
| id | UUID | Primary key |
| name | text (unique normalized) | Company name |
| contact_name | text | Primary contact |
| contact_email | text | Invoice recipient (validated RFC 5322) |
| contact_phone | text | Phone number |
| website | text | Company URL |
| billing_model | enum | hourly, flat_rate, project |
| default_rate | integer | Default rate in cents |
| payment_terms | text | "Net 14", "Net 30", etc. |
| invoice_prefix | text | For invoice number generation |
| payment_methods | JSON | Array of accepted methods |
| cc_fee_passthrough | boolean | Pass CC fees to client (default true for >$1k) |
| tax_id | text (nullable) | Client's tax ID / EIN (optional) |
| address | JSON | Mailing address |
| notes | text | Internal notes |
| active | boolean | Soft delete |
| created_at | timestamp | |
| updated_at | timestamp | |

**Duplicate detection:** On customer create, fuzzy-match against existing names and warn if similar customer exists.

### Contacts

| Field | Type | Purpose |
|-------|------|---------|
| id | UUID | Primary key |
| customer_id | FK → customers | |
| name | text | Contact name |
| email | text | Contact email |
| phone | text | Phone |
| role | enum | billing, project, primary | Contact role |
| is_default | boolean | Default invoice recipient |
| created_at | timestamp | |

Customers can have multiple contacts (AP department vs. project contact). The `is_default` contact receives invoices unless overridden.

### Work Orders

| Field | Type | Purpose |
|-------|------|---------|
| id | UUID | Primary key |
| customer_id | FK → customers | |
| title | text | "Website Redesign" |
| description | text | Scope summary |
| total_value | integer | Total contract value in cents |
| currency | text | Default "USD" |
| status | enum | draft, active, completed, cancelled |
| start_date | date | Work begins |
| expected_end_date | date | Target completion |
| cancelled_reason | text (nullable) | Why cancelled (required on cancel) |
| recurring_schedule | JSON (nullable) | For recurring WOs (monthly flat-rate) |
| created_at | timestamp | |
| updated_at | timestamp | |

**Status transitions:** draft → active → completed | cancelled

**Cancellation policy:** On cancel, auto-void all unpaid draft/sent invoices linked to this WO. Paid milestones remain as-is. Requires a reason.

**Validation:** Sum of milestone amounts must equal `total_value`. Soft warning on mismatch during editing, hard constraint on WO activation.

### Milestones

| Field | Type | Purpose |
|-------|------|---------|
| id | UUID | Primary key |
| work_order_id | FK → work_orders | |
| title | text | "Phase 1 delivery" (deliverable description) |
| amount | integer | Invoice amount in cents |
| due_date | date | Expected invoice date (nullable for manual-only) |
| trigger_type | enum | date, manual, both |
| status | enum | pending, ready, invoiced, paid |
| sort_order | int | Display sequence |
| invoice_id | FK → invoices (nullable, unique where non-void) | Links to generated invoice |
| notified_at | timestamp (nullable) | When alert email was sent (idempotency gate) |
| created_at | timestamp | |
| updated_at | timestamp | |

**Status transitions:** pending → ready → invoiced → paid

**Locking:** Once status = `invoiced` or `paid`, `amount` and `title` fields are immutable. To change, void the linked invoice first (resets milestone to `ready`).

**Unique constraint:** Only one non-void invoice per milestone (`invoice_id` unique where invoice.status != 'void'`).

### Invoices

| Field | Type | Purpose |
|-------|------|---------|
| id | UUID | Primary key |
| invoice_number | text (unique) | Human-readable (e.g., XS-002) |
| customer_id | FK → customers | |
| work_order_id | FK → work_orders (nullable) | For WO-based invoices |
| milestone_id | FK → milestones (nullable, unique where non-void) | Which milestone triggered this |
| entity | text | "sparkry" (always, for now) |
| status | enum | draft, sent, paid, overdue, void |
| submitted_date | date | Invoice date |
| due_date | date | Payment due |
| subtotal | integer | Sum of line items in cents |
| cc_fee_amount | integer | CC surcharge in cents (3.5% if applicable) |
| tax | integer | Tax in cents (0 for now) |
| total | integer | subtotal + cc_fee + tax in cents |
| payment_terms | text | "Net 14" |
| payment_method | enum | stripe_cc, ach, venmo, check |
| payment_link_url | text | Stripe payment link |
| payment_link_id | text | Stripe link ID (idempotency key) |
| stripe_event_id | text (nullable) | Stripe event that marked it paid (dedup) |
| sent_at | timestamp | When email was sent |
| sent_to | text | Recipient email |
| resend_email_id | text (nullable) | Resend scheduled email ID (for undo-send cancellation) |
| delivery_status | enum | pending, scheduled, delivered, bounced, failed | Resend delivery status |
| paid_date | date | When payment received |
| void_reason | text (nullable) | Required when voiding a paid invoice |
| notes | text | Invoice-level notes |
| created_at | timestamp | |
| updated_at | timestamp | |

**Status transitions:** draft → sent | void; sent → paid | void | overdue; paid → void (requires reason + confirmation); overdue → paid | void; void (terminal)

**Invoice numbering:** Per-prefix auto-increment via `SELECT MAX(invoice_number) FROM invoices WHERE invoice_number LIKE '{prefix}-%'` + 1. Safe at this concurrency level.

**Total always server-derived:** On save/send, total is recomputed from line items + cc_fee. Never trust the client-supplied total.

### Invoice Line Items

| Field | Type | Purpose |
|-------|------|---------|
| id | UUID | Primary key |
| invoice_id | FK → invoices | |
| description | text | Line item text |
| quantity | integer | Quantity × 10000 (4 decimal precision) |
| unit_price | integer | Rate per unit in cents |
| total_price | integer | quantity × unit_price / 10000 in cents |
| sort_order | int | Display order |
| created_at | timestamp | |

### Activity Log

| Field | Type | Purpose |
|-------|------|---------|
| id | UUID | Primary key |
| entity_type | enum | customer, work_order, milestone, invoice |
| entity_id | UUID | What was affected |
| action | text | "status_changed", "amount_edited", "sent", "voided", etc. |
| user_email | text | Who did it |
| old_value | text (nullable) | Previous value |
| new_value | text (nullable) | New value |
| metadata | JSON (nullable) | Additional context |
| created_at | timestamp | |

Logs all state transitions, edits to locked fields (after void), send actions, and payment events.

### Credit Notes

| Field | Type | Purpose |
|-------|------|---------|
| id | UUID | Primary key |
| invoice_id | FK → invoices | Original invoice |
| customer_id | FK → customers | |
| amount | integer | Credit amount in cents |
| reason | text | Why issued |
| status | enum | issued, applied | |
| applied_to_invoice_id | FK → invoices (nullable) | If applied to future invoice |
| created_at | timestamp | |

Issued when voiding a paid invoice. Can be applied as a discount on a future invoice.

---

## UI Design

### Navigation

Top tab navigation with dashboard as home:

**Desktop tabs:** Dashboard | Customers | Work Orders | Invoices
**Mobile:** Hamburger menu or bottom bar with icons + short labels (Home, Clients, Projects, Invoices)

User avatar in top-right for account/logout. Content area max-width ~800px, centered.

### Dashboard (Home)

**First-time (empty state):**
Welcome message with 3-step getting-started guide:
1. "Add a customer" — links to Customers → New
2. "Create a project" — links to Work Orders → New
3. "Add milestones — we'll remind you when it's time to send invoices"

Each step links to relevant page. Collapses to normal dashboard after first invoice is sent.

**Normal state:**
Personalized greeting ("Good morning, Amy") with action cards:

- **Invoices ready to send** — count + click to view
- **Paid this month** — dollar amount
- **Outstanding** — dollar amount + invoice count

**"Coming Up" section** — list of milestones approaching their due date with "Review & Send" buttons. This is where email alert links land.

**"Needs Attention" section** — bounced emails, overdue invoices, missed milestones (due_date passed but still pending).

### Customers Page

List of active customers with **typeahead search** on name/contact. Each row shows: name, contact, active WO count, outstanding invoice amount.

Actions: Add Customer, Edit, Deactivate.

**Add/Edit Customer form:**
- Name, contact name, contact email, phone, website
- Billing model (with helper text: "Project — one-time work with fixed milestones", "Hourly — bill by the hour", "Flat rate — same amount each month")
- Default rate, payment terms (dropdown: "Due in 14 days", "Due in 30 days", "Due in 60 days")
- Invoice prefix, payment methods
- CC fee passthrough (label: "Add credit card processing fee to invoices over $1,000 (3.5%) — the client pays the fee instead of us absorbing it")
- Address, tax ID (optional), notes

**Duplicate warning:** On name entry, if similar customer exists, show: "A customer named [X] already exists. Did you mean to edit them?"

### Work Orders Page

List view: customer name, WO title, total value, progress (invoiced/total), status badge.

**Empty state:** "Work orders track the projects you're billing for. Each project has milestones — when a milestone is done, you send an invoice." + "Create Your First Project" button.

**Work Order Detail:**
- Header: title, customer, total value, status
- Progress bar: invoiced amount vs. total
- Milestone sum validation: if milestones don't add up to total, show warning banner
- Milestones list with status indicators:
  - Green check = paid (tooltip: "Paid")
  - Amber dot = ready to invoice (tooltip: "Ready to invoice")
  - Grey circle = pending (tooltip: "Not yet complete")
- Actions per milestone:
  - Pending: "Mark Complete" (with confirmation dialog: "Mark this milestone complete? This will create a draft invoice for $X and notify Travis/Amy." → "Yes, mark complete" | "Cancel")
  - Ready: "Create Invoice" (generates draft, navigates to review). Button disabled + loading after first click.
  - Invoiced: links to invoice
  - Paid: links to invoice (shows paid badge)
- "+ Add Milestone" button
- Existing WO warning: When creating a new WO, show active WOs for the same customer ("Did you mean to add milestones to [existing WO]?")

### Invoice Review/Send Page

Pre-populated from milestone data:

- Bill-to info (from customer default contact)
- Invoice date, due date (auto-calculated from payment terms, displayed as "Due June 14, 2026" not "Net 14")
- Line items table: description, qty, rate, amount, delete (×)
  - Mobile: stacked card layout per item (not a table)
- "+ Add Item" button for additional line items
- Payment method selector (CC, ACH, Venmo, Check)
- CC fee line auto-appears when Credit Card selected + invoice > $1,000 (3.5%)
- Subtotal, fees, total
- Actions: "Save Draft" | "Review & Send →"

**"Review & Send" confirmation:**
- Recipient email displayed prominently with highlight: "This invoice will be emailed to: **tiffany@xternalsource.com**" + "Edit" link to customer record
- First invoice to a customer: extra callout "First invoice to this contact — please double-check the email address"
- Email subject and body preview
- Final "Send Invoice" button

**After successful send:**
- Success toast: "Invoice XS-001 will be sent to tiffany@xternalsource.com"
- **30-second undo window** with countdown timer and prominent "Undo" button
- Status badge shows "Sending..." during the 30-second window
- After 30 seconds: toast updates to "Invoice sent", status badge updates to "Sent"
- "View Invoice" link + "Back to Dashboard" link
- Do not redirect silently

**Undo-send implementation (Resend scheduled emails):**
1. On "Send Invoice", call Resend API with `scheduledAt: now + 30 seconds`
2. Resend returns an `email_id` — store as `resend_email_id` on the invoice
3. Set invoice `delivery_status: scheduled`, `status: sent`, `sentAt: now`
4. UI shows 30-second countdown with "Undo" button
5. **If Undo clicked:** call `POST /emails/{resend_email_id}/cancel`, revert invoice to `draft`, clear `sentAt`
6. **If 30 seconds pass:** Resend delivers automatically, delivery webhook updates `delivery_status: delivered`
7. **Idempotency:** before any send, check if `resend_email_id` is already set and not cancelled

### Invoices List Page

All invoices with filters: status (draft/sent/paid/overdue/void), customer, date range.

Each row: invoice number, customer, amount, status badge, date, delivery status indicator (bounced = warning icon), actions.

**Actions per invoice:**
- Draft: Edit, Send, Cancel (void)
- Sent: Resend, Mark Paid (for offline), Cancel
- Overdue: Resend, Send Reminder, Mark Paid, Cancel
- Paid: View, Cancel (requires reason + extra confirmation: "This invoice has already been paid. Cancelling it means the payment may need to be returned. Are you sure?")
- Void/Cancelled: View only

### Settings Page

- Account info (connected Google account)
- Company details (EIN, business address — shown on invoices)
- Terms & conditions template (footer text for invoices)
- Late fee policy display
- Notification preferences

---

## Payment System

### Stripe Integration

- **Credit Card:** Stripe Payment Link with `payment_method_types: ['card']`. For invoices >$1,000, CC processing fee (3.5%) added to the invoice total.
- **ACH:** Stripe Payment Link with `payment_method_types: ['us_bank_account']`. No fee pass-through.
- **Venmo:** Stripe Payment Link with `payment_method_types: ['venmo']`. No fee pass-through.
- **Idempotency:** Use invoice ID as idempotency key on payment link creation. Store link on first call, return stored link on subsequent calls.

### Offline Check

No Stripe link generated. Invoice email includes mailing address and "Make checks payable to Sparkry LLC." Manual "Mark Paid" action on the invoice.

### CC Fee Logic

```
if payment_method == 'stripe_cc' AND subtotal_cents > 100000:
    cc_fee_cents = round(subtotal_cents * 0.035)
    total_cents = subtotal_cents + cc_fee_cents
else:
    cc_fee_cents = 0
    total_cents = subtotal_cents
```

Fee is shown as a separate line in the invoice totals (not as a line item). Invoice email and payment link reflect the total including fee.

### Webhook

**Endpoint:** Workers `fetch` handler on a dedicated route (not cron). Protected by Stripe signature verification.

**Security:** `stripe.webhooks.constructEvent(body, sig, webhookSecret)` on every request. Reject invalid signatures with 400.

**Idempotency:** Check `stripe_event_id` on the invoice — if already set and matches, return 200 (already processed). Store the event ID atomically with the status update.

**Handler flow:**
1. Verify signature
2. Extract `payment_link` ID from event
3. Find invoice by `payment_link_id`
4. If invoice.status is already `paid`, return 200 (idempotent)
5. D1 `batch()`: update invoice (status=paid, paid_date, stripe_event_id) + update milestone (status=paid)
6. Send payment confirmation email to both users
7. Return 200

**Reconciliation fallback:** Daily cron also checks invoices in `sent` status older than payment_terms + 3 days against Stripe API for matching completions. Alerts if discrepancy found.

### Refunds & Credit Notes

- Voiding a paid invoice requires a reason and triggers credit note creation
- Credit notes can be applied to future invoices (reduces the total)
- Actual Stripe refunds are handled directly in Stripe dashboard — the system tracks the credit note for accounting

---

## Notification System

### Daily Milestone Check (Cron Trigger)

**Implementation:** Cloudflare Workers `scheduled()` handler — NOT an HTTP-routable endpoint. Cannot be triggered externally.

**Schedule:** Runs daily. Cron expression: `0 15 * * *` (15:00 UTC = 8am PT during PDT, 7am during PST. Acceptable drift.)

**Handler flow:**
1. Query milestones where `due_date <= today` AND `status = pending` AND `trigger_type IN ('date', 'both')`
   - Uses `<=` (not `=`) to catch any milestones missed on previous days
2. For each matching milestone (that doesn't already have `notified_at` set today):
   - D1 `batch()`: transition milestone to `ready` + create draft invoice + set `notified_at`
   - Send alert email to both users via Resend
3. **Overdue invoice check:** Query invoices where status = `sent` AND `due_date < today`. Transition to `overdue`.
4. **Payment reminder emails:** For overdue invoices at 3, 7, and 14 days past due, send reminder email to the client (configurable per customer).
5. **Reconciliation check:** Query `sent` invoices older than payment_terms + 3 days, cross-check Stripe API for completed payments. Alert on discrepancy.
6. **Heartbeat:** On success, log to activity table. On failure, send error email to travis@sparkry.com via Resend (separate try/catch).

**Idempotency:** `notified_at` gate prevents duplicate emails if cron fires twice (at-least-once delivery).

### Alert Email

- **To:** travis@sparkry.com, amycsparks@gmail.com
- **Subject:** "Invoice ready: {customer_name} — {milestone_title} (${amount})"
- **Body:** Customer name, milestone description, amount, due date
- **CTA button:** "Review & Send" → links to `internal.sparkry.ai/invoices/{id}`

### Payment Reminder Emails (to client)

- **3 days overdue:** "Friendly reminder: Invoice {number} for ${total} was due on {date}"
- **7 days overdue:** "Second notice: Invoice {number} is now 7 days past due"
- **14 days overdue:** "Final notice: Invoice {number} is 14 days past due. Please remit payment."

Configurable per customer (some clients are always late — don't want to nag). Can be disabled.

### Payment Confirmation Email

Triggered by Stripe webhook on payment:

- **To:** travis@sparkry.com, amycsparks@gmail.com
- **Subject:** "Payment received: {customer_name} — ${total}"
- **Body:** Invoice number, amount, payment method, date

### Manual Trigger

"Mark Complete" on a pending milestone → confirmation dialog → same flow as cron (transition to ready, create draft, send alert email).

### Email Bounce Handling

Register Resend bounce/delivery webhooks. On bounce:
- Update invoice `delivery_status` to `bounced`
- Surface warning badge on invoice in UI: "Email bounced — resend to a different address"
- Include in dashboard "Needs Attention" section

---

## Send Invoice Pipeline

The "Send Invoice" action is an ordered pipeline. Failure at any step aborts subsequent steps and leaves the invoice in the last consistent state.

```
1. Validate: invoice status == draft, line items present, recipient email valid
2. Recompute total: sum(line_items) + cc_fee (never trust stored total)
3. Create Stripe payment link (if not check): idempotent (returns existing if already created)
4. Schedule email via Resend with scheduledAt = now + 30 seconds → get resend_email_id
5. D1 batch(): persist payment_link_url + resend_email_id + flip status to 'sent' + set sent_at + sent_to + delivery_status='scheduled'
6. Return success to UI → show 30-second undo countdown

On "Undo" (within 30s):
  1. Call POST /emails/{resend_email_id}/cancel on Resend
  2. D1 batch(): revert status to 'draft', clear sent_at/sent_to/resend_email_id, delivery_status='pending'
  3. If Resend cancel fails (email already sent): show "Email already delivered, cannot undo"

On failure at step 3: show error toast, remain in draft
On failure at step 4: show error toast, remain in draft (Stripe link orphaned but harmless)
On failure at step 5: show error toast, Resend email scheduled but DB not updated — cron reconciliation catches this
```

---

## Authentication

### Google OAuth + Cloudflare Access (defense-in-depth)

**Layer 1: Cloudflare Access**
- Configured on internal.sparkry.ai for all routes EXCEPT `/api/webhooks/stripe`
- Allowlist: travis@sparkry.com, amycsparks@gmail.com
- Blocks unauthorized access at the edge before reaching the Worker

**Layer 2: Application-level auth (SvelteKit)**
- Google OAuth via `arctic` library
- Session: signed HTTP-only cookie containing user email + expiry timestamp
- 7-day expiry, refreshed on every authenticated request
- Email comparison: lowercase + trim both allowlist and OAuth-returned email
- Session bound to user-agent hash (prevents stolen cookie reuse from different browser)
- Non-allowlisted accounts: "Access denied" page
- "Sign out all sessions" capability (invalidate all cookies by rotating signing secret)
- Return URL preserved through OAuth redirect (so deep links from email alerts work post-auth)

### CSRF Protection

SvelteKit's built-in `kit.csrf.checkOrigin` enabled (default). Webhook endpoint exempted (uses Stripe signature instead).

---

## Invoice Presentation

HTML invoice viewable in-browser (styled page), with "Download PDF" triggering browser print-to-PDF.

**Style:** Consistent with existing Fascinate invoices — clean, professional, Sparkry branding. Orange accent color (#F97316), clear typography, line item table with totals section.

**Invoice includes:**
- Sparkry LLC header + logo
- Business address + EIN
- Bill-to: customer name, contact, address, tax ID (if set)
- Invoice number, date, due date (displayed as date, not "Net 14")
- Line items table: description, qty, rate, amount
- Subtotal, CC fee (if applicable), total
- Payment instructions (Stripe link button or check mailing address)
- Terms & conditions footer (configurable)
- "Thank you for your business" footer

**Print route auth:** The `/invoices/[id]/print` route is protected by the same root layout auth guard. UUID obscurity alone is not sufficient.

---

## Migration

### From Local SQLite → Cloudflare D1

One-time TypeScript migration script (runs via `wrangler d1 execute`):

1. Read local SQLite via `better-sqlite3`
2. Transform Customer records:
   - Drop SAP/calendar-specific fields (sap_config, calendar_patterns, calendar_exclusions)
   - Add new CRM fields (contacts, tax_id)
   - Convert decimal amounts to integer cents
3. Transform Invoice records:
   - Map status values
   - Convert amounts to cents
   - Drop fields not in CRM (pdf_path, sap_instructions, payment_transaction_id)
   - Map `adjustments` → `cc_fee_amount` where applicable
4. Generate Work Order stubs for existing customers:
   - Fascinate: recurring WO with monthly milestones
   - Cardinal Health: flat-rate WO with monthly milestones
5. Insert into D1
6. Validate: row counts + total invoiced amounts must match between systems

**Schema differences handled:**
- `service_period_start/end` → dropped (not needed for WO-based system)
- `pdf_path` → dropped (browser print-to-PDF instead)
- Decimal → integer cents conversion

### Example: Xternal Source (first customer to onboard via UI)

Reference for requirements/testing — not pre-seeded:

- Customer: Xternal Source, Tiffany Broderson (President), 480.382.3076, xternalsource.com
- Work Order: $8,000 total, project-based
- Milestones: $2k upfront (manual), $2k end of month 1 (both), $4k completion (manual)

---

## UX Principles

- **8th grade reading level** — clear, simple language throughout. No jargon.
- **Not condescending** — professional tone, not dumbed down. Assume intelligence, not expertise.
- **Action-oriented dashboard** — show what needs doing, not everything that exists.
- **Pre-populated defaults** — invoices create themselves from milestone data. Amy reviews and sends, not builds from scratch.
- **One-click from email** — alert lands on the exact invoice, ready to send.
- **Visual status** — color-coded badges and milestone indicators (green/amber/grey) with tooltips on hover, text labels on mobile.
- **Confirmation before destructive actions** — always one confirmation step before emails go to clients or invoices are voided.
- **Extra confirmation for dangerous actions** — voiding a paid invoice gets a scarier dialog explaining financial implications.
- **Forgiving** — draft invoices editable, undo-send window, nothing permanently destructive without confirmation.
- **Contextual help** — helper text on form fields (billing model explanations, CC fee label), one-time milestone explanation on empty WO page. Not a full tutorial.
- **Responsive** — works on tablet and phone. Tables become stacked cards on mobile. Nav collapses to hamburger.

### Terminology Decisions

| System term | UI label | Reasoning |
|-------------|----------|-----------|
| Work Order | Project (in nav) / Work Order (in detail) | "Project" is universally understood |
| void | Cancel Invoice | "Void" is accounting jargon |
| Net 14 | Due in 14 days | Shown as dropdown with plain text |
| milestone | Milestone | Acceptable — explain once on empty state |
| overdue | Past due | Slightly more natural |
| cc_fee_passthrough | "Add credit card fee..." | Full sentence label with explanation |

---

## Operational Concerns

### Deployment Pipeline

GitHub repo → Cloudflare Pages Git integration:
- Push to branch → automatic preview deployment (bound to staging D1)
- Merge to `main` → production deployment
- Migrations: GitHub Action runs `drizzle-kit generate` + `wrangler d1 migrations apply --remote`
- Rollback: Cloudflare Pages dashboard (instant code rollback) + manual reverse migration if schema changed

### Error Monitoring

- **Sentry** (Cloudflare Workers SDK) for runtime errors
- **Error email:** Critical failures (webhook processing, cron failure) email travis@sparkry.com via Resend
- **D1 error_log table:** Fallback if Sentry is unreachable

### Backup Strategy

- **D1 Time Travel:** Point-in-time restore up to 30 days (verify enabled)
- **Weekly export:** Scheduled Worker runs `D1 export` to R2 bucket every Sunday
- **Non-negotiable for a financial system**

### Health Check

`/api/health` endpoint: queries D1 (`SELECT 1`), returns 200. External uptime monitor hits every 5 minutes.

### Staging Environment

- `sparkry-crm-staging` D1 database
- Cloudflare Pages preview deployments (auto-created for PRs) bound to staging DB
- `[env.staging]` bindings in `wrangler.toml`

### Cron Monitoring

Cron handler wrapped in try/catch:
- On success: log to activity_log table
- On failure: send error alert email to travis@sparkry.com
- External dead-man's switch (optional): ping Cronitor/Better Uptime on success

### Rate Limiting

Cloudflare WAF rules:
- `/api/webhooks/stripe`: 100 req/min
- OAuth callback: 10 req/min
- All other routes: protected by Cloudflare Access (no public access)

---

## Technical Details

### Cloudflare Configuration

- **Pages project:** `sparkry-crm`
- **Custom domain:** internal.sparkry.ai
- **D1 databases:** `sparkry-crm-prod`, `sparkry-crm-staging`
- **Cron Trigger:** `0 15 * * *` (Workers scheduled handler)
- **R2 bucket:** `sparkry-crm-backups` (weekly D1 exports)
- **Cloudflare Access:** Application on internal.sparkry.ai, Google IdP, allowlist policy
- **WAF rules:** Rate limiting on webhook + auth endpoints
- **Environment variables (secrets):** GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, RESEND_API_KEY, ALLOWED_EMAILS, SENTRY_DSN, SESSION_SIGNING_KEY

### SvelteKit Route Structure

```
src/routes/
  +layout.server.ts     — auth guard (ALL routes)
  +layout.svelte        — top nav, responsive shell
  +page.svelte          — dashboard (empty state + normal)
  customers/
    +page.svelte        — customer list + search
    [id]/+page.svelte   — customer detail/edit + contacts
    new/+page.svelte    — add customer (with dupe detection)
  work-orders/
    +page.svelte        — WO list (empty state)
    [id]/+page.svelte   — WO detail + milestones + progress
    new/+page.svelte    — create WO (with existing WO warning)
  invoices/
    +page.svelte        — invoice list + filters
    [id]/+page.svelte   — invoice detail/review/send
    [id]/print/+page.svelte — printable HTML invoice (auth protected)
  settings/
    +page.svelte        — company details, T&C, notification prefs
  api/
    auth/callback/+server.ts  — Google OAuth callback
    auth/logout/+server.ts    — Session invalidation
    health/+server.ts         — Health check (D1 ping)
    webhooks/stripe/+server.ts — Stripe webhook (signature verified)
    webhooks/resend/+server.ts — Resend delivery/bounce webhook
src/
  worker.ts             — scheduled() handler for cron (not HTTP-routable)
```

### Key Dependencies

- `sveltekit` + `@sveltejs/adapter-cloudflare`
- `arctic` (Google OAuth — lightweight, Workers-compatible)
- `stripe` (evaluate bundle size; fallback to raw fetch if needed)
- `resend` (tiny, fetch-based)
- `drizzle-orm` + `drizzle-kit` (D1 ORM, pinned versions)
- `@sentry/cloudflare` (error monitoring)

### Bundle Size Management

Monitor Worker bundle size during development. If approaching 10MB compressed:
1. Replace `stripe` SDK with raw `fetch()` calls (~1.5MB savings)
2. Ensure tree-shaking is effective (check adapter output)
3. Use `$env/static/private` for secrets (server-only, not bundled to client)

### Migration Tooling

- Drizzle-kit for schema migrations (generate SQL, apply via wrangler)
- Keep migrations small and additive (never drop columns in prod)
- Test against local D1 (`wrangler d1 --local`) before applying to prod
- No automatic rollback — document reverse migrations manually
