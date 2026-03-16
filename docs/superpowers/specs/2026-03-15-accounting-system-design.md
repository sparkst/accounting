# Accounting System Design Spec

> Personal + multi-entity accounting system for Travis Sparks. Ingests email receipts, Stripe, Shopify, brokerage/bank CSVs, photo receipts, and deduction emails. Classifies, deduplicates, and produces tax-ready output.

## Entities

| Entity | Legal Structure | Tax Filing | B&O Frequency |
|---|---|---|---|
| Sparkry AI LLC | Single-member LLC | Schedule C (1040) | Monthly |
| BlackLine MTB LLC | Multi-member LLC (partnership) | Form 1065 + K-1 | Quarterly |
| Personal | N/A | 1040 (Schedule A, D, etc.) | N/A |

### BlackLine MTB Ownership (2025)

- Travis Sparks: 100% (Emerson in Year 1 cliff, 0% vested)
- Incorporated June 2025, WA state
- Filed as base LLC — defaults to partnership treatment (Form 1065)
- Had a net loss in 2025
- Emerson Sparks is a dependent on Travis's personal return

### Tax Software Stack

- **TaxAct Business** (~$60) — BlackLine MTB Form 1065
- **FreeTaxUSA** ($0 federal + $15 state) — Travis's 1040
- **FreeTaxUSA** ($0 federal + $15 state) — Emerson's 1040

### Personal Tax Complexity

- ~$500k W-2 salary + RSU vesting (E*Trade, Schwab)
- Short-term and long-term capital gains/losses (E*Trade, Schwab, Vanguard)
- ~$90k charitable giving
- 3 bank accounts
- Sparkry Schedule C income/expenses
- BlackLine K-1 pass-through loss

---

## Architecture

```
DATA SOURCES
├── Gmail/n8n (existing pipeline) → JSON + PDF/image attachments
├── Gmail deductions (new label/pipeline) → charitable, mortgage, medical
├── Stripe API → Sparkry income + BlackLine payments
├── Shopify API → BlackLine sales, fees, payouts
├── Brokerage CSVs → E*Trade, Schwab, Vanguard (manual periodic import)
├── Bank CSVs → 3 banks (manual periodic import)
└── Photo receipts → JPG/PNG with meaningful filenames

    ↓

INGESTION LAYER (Python adapters)
├── Source-specific adapters (one per source type)
├── Normalize to common Transaction schema
├── Deduplication (SHA256 hash + fuzzy matching)
├── Validation + error logging
└── Attachment linking

    ↓

CLASSIFICATION ENGINE (3-tier)
├── Tier 1: Vendor Rules (deterministic, from Account Memory)
├── Tier 2: Pattern Matching (structural rules per source)
└── Tier 3: LLM Classification (Claude API for ambiguous items)
    └── Line-item splitting (hotels, mixed receipts)

    ↓

REGISTER (SQLite)
├── Single source of truth
├── Full audit trail
├── Account Memory (vendor rules, learned patterns)
├── Failure/ingestion logs
└── Reimbursable expense tracking

    ↓

OUTPUTS
├── Dashboard (SvelteKit, localhost:5173)
├── Tax Export (FreeTaxUSA CSV, TaxAct CSV)
├── B&O Tax Reports (monthly Sparkry, quarterly BlackLine)
└── Reimbursement invoice tracker
```

### Key Decisions

- **SQLite** — Portable, no server, stored in `data/` (gitignored). Backed up via SGDrive folder sync, NOT git. Schema migrations via Alembic with upgrade/downgrade support.
- **Python** — All processing, adapters, API server (FastAPI)
- **SvelteKit** — Dashboard, lightweight, existing toolchain
- **Claude API** — LLM classification for ambiguous items, photo receipt OCR
- **No cloud DB** — Financial data stays local
- **No Plaid** — Bank CSVs imported manually, avoids over-engineering

---

## Data Model

### Transaction

| Field | Type | Description |
|---|---|---|
| id | UUID | Primary key |
| source | str | gmail_n8n, stripe, shopify, brokerage_csv, bank_csv, photo_receipt, deduction_email |
| source_id | str | Original ID from source |
| source_hash | str | SHA256(source, date, amount, description_normalized) — dedup key |
| date | date | Transaction date |
| description | str | Vendor/payee/payer |
| amount | decimal | Positive = income, negative = expense |
| currency | str | USD default |
| entity | str | sparkry, blackline, personal, null |
| direction | str | income, expense, transfer, reimbursable |
| tax_category | str | IRS category code |
| tax_subcategory | str | For line-item splits |
| deductible_pct | float | 1.0 default, 0.5 meals, 0.0 personal |
| status | str | auto_classified, needs_review, confirmed, split_parent, rejected |
| confidence | float | 0.0-1.0 |
| review_reason | str | Why needs review (nullable) |
| parent_id | UUID | For split line items |
| reimbursement_link | UUID | Links expense to reimbursement (nullable) |
| attachments | json | File paths to PDFs, images, JSON source files |
| raw_data | json | Original source record verbatim |
| created_at | datetime | |
| updated_at | datetime | |
| confirmed_by | str | auto, human |
| notes | str | Human notes |

### VendorRule (Account Memory)

A single vendor can have multiple rules for different entities (e.g., FedEx used by both Sparkry and BlackLine). Rules are scoped by `(vendor_pattern, entity)` pair. When a vendor matches multiple rules, the classification engine presents all options ranked by `examples` count and `confidence`, and the human picks. Once a pattern emerges (e.g., FedEx + Shopify order reference → BlackLine), a more specific rule is created.

| Field | Type | Description |
|---|---|---|
| id | UUID | Primary key |
| vendor_pattern | str | Regex or exact match on sender/description |
| entity | str | Default entity (one rule per entity per vendor) |
| tax_category | str | Default category |
| tax_subcategory | str | Default subcategory (e.g., lodging, meals) |
| direction | str | income, expense, reimbursable |
| deductible_pct | float | Default deductible percentage |
| confidence | float | Rule confidence |
| source | str | human, learned |
| examples | int | Match count |
| last_matched | datetime | |
| created_at | datetime | |

### IngestedFile (replaces processed_files.json)

Tracks which source files have been processed. Stored in SQLite, not a sidecar JSON file.

| Field | Type | Description |
|---|---|---|
| id | UUID | Primary key |
| file_path | str | Absolute path to source file |
| file_hash | str | SHA256 of file contents |
| adapter | str | Which adapter processed it |
| processed_at | datetime | |
| status | str | success, failed, skipped |
| transaction_ids | json | List of transaction UUIDs created from this file |

### AuditEvent (edit history)

Every human action is recorded for undo/audit capability.

| Field | Type | Description |
|---|---|---|
| id | UUID | Primary key |
| transaction_id | UUID | Which transaction was modified |
| field_changed | str | Which field (entity, tax_category, status, etc.) |
| old_value | str | Previous value |
| new_value | str | New value |
| changed_by | str | human, auto |
| changed_at | datetime | |

### IngestionLog

| Field | Type | Description |
|---|---|---|
| id | UUID | Primary key |
| source | str | Which adapter |
| run_at | datetime | |
| status | str | success, partial_failure, failure |
| records_processed | int | |
| records_failed | int | |
| error_detail | str | Stack trace or error |
| retryable | bool | |
| retried_at | datetime | Null until retried |
| resolved_at | datetime | Null until resolved |

### Tax Categories

**Business (Schedule C / 1065):**

| Code | IRS Line | Description |
|---|---|---|
| ADVERTISING | L8 | Pinterest, marketing |
| CAR_AND_TRUCK | L9 | Mileage, vehicle expenses |
| CONTRACT_LABOR | L11 | Freelancers (Fiverr, Gaby Photography) |
| INSURANCE | L15 | Hiscox (Sparkry), Hartford (BlackLine) |
| LEGAL_AND_PROFESSIONAL | L17 | Northwest Registered Agent, CPA |
| OFFICE_EXPENSE | L18 | Office supplies |
| SUPPLIES | L22 | SaaS tools, dev infrastructure |
| TAXES_AND_LICENSES | L23 | B&O tax, business licenses |
| TRAVEL | L24a | Flights, hotels (room+tax), wifi, ground transport |
| MEALS | L24b | 50% deductible |
| COGS | Part III | Inventory (Leeline, Brist Mfg) |
| CONSULTING_INCOME | Income | Cardinal Health, Fascinate OS |
| SUBSCRIPTION_INCOME | Income | Substack/Stripe |
| SALES_INCOME | Income | Shopify sales |
| REIMBURSABLE | N/A | Pass-through, nets to zero |

**Personal (Schedule A / Other):**

| Code | Description |
|---|---|
| CHARITABLE_CASH | Cash donations |
| CHARITABLE_STOCK | Appreciated stock donations (FMV) |
| MEDICAL | Medical expenses |
| STATE_LOCAL_TAX | SALT ($10k cap) |
| MORTGAGE_INTEREST | Home mortgage |
| INVESTMENT_INCOME | Schedule D / 8949 |
| PERSONAL_NON_DEDUCTIBLE | Not reported |

---

## Deduplication Strategy

**Two-pass approach:**

### Pass 1: Same-source dedup (hash-based, fast)
1. Compute `source_hash` = SHA256(source_type, source_id)
2. If exact match exists in IngestedFile table → skip entirely (same file/record processed twice)

### Pass 2: Cross-source dedup (fuzzy, catches real-world duplicates)
1. For each new transaction, search existing register for matches within: date ± 3 days AND abs(amount) match AND normalized_vendor similarity > 0.7
2. If match found from a different source → flag `needs_review` with reason "possible duplicate across sources" and link both records
3. Example: Anthropic $16.90 charge appears as Gmail receipt (Feb 26) AND bank CSV line item (Feb 26) — flagged for human to pick which to keep
4. Stripe payout appearing in both Stripe API and bank CSV — auto-link as reconciliation match (not duplicate)

### Reconciliation vs. Dedup
- Stripe/Shopify payouts matching bank deposits are **reconciliation pairs**, not duplicates. They confirm each other.
- A charge from the same vendor on the same date for the same amount from two different sources (email + bank CSV) is a **duplicate** — keep one.

---

## Reimbursable Expense Tracking

Cardinal Health expenses that Travis pays out of pocket and invoices for reimbursement:

1. Expense arrives (email receipt, photo receipt)
2. Classified as `direction: reimbursable`, `entity: sparkry`
3. Shows in dashboard with "Awaiting Reimbursement" status
4. When reimbursement payment arrives (SAP/Ariba or bank deposit):
   - Link via `reimbursement_link` to original expense
   - Both net to zero on P&L
5. Dashboard shows unlinked reimbursables as action items
6. If reimbursement doesn't arrive within configurable window (30 days), flag as overdue

---

## Classification Engine

### Tier 1: Vendor Rules (deterministic)

Lookup vendor in VendorRule table. If match found with confidence > 0.8, auto-classify. Pre-seeded with ~40 known vendors from existing data.

### Tier 2: Pattern Matching

Structural rules per source:
- All Shopify transactions → BlackLine
- Stripe with substack metadata → Sparkry subscription income
- Self-forwarded emails (from: travis@sparkry.com) → route to LLM
- Photo receipts → always route to LLM
- SAP/Ariba notifications → Sparkry consulting income

### Tier 3: LLM Classification

Claude API call with:
- Transaction details (date, amount, vendor, subject, body excerpt)
- Current account memory (recent vendor rules)
- Entity context (what each business does)
- Returns: entity, tax_category, direction, confidence, reasoning

### Line-Item Splitting

Triggered for: hotels, mixed receipts, AmEx statements
- LLM extracts line items from receipt/statement
- Creates child transactions linked to parent via `parent_id`
- Each child gets its own tax category and deductible_pct
- Validates: children sum to parent total (flag if not)

Hotel splitting map:
| Line Item | Category | Deductible |
|---|---|---|
| Room rate | TRAVEL | 100% |
| Lodging/occupancy tax | TRAVEL | 100% |
| State/local hotel tax | TRAVEL | 100% |
| Meals/restaurant | MEALS | 50% |
| Parking | TRAVEL | 100% |
| Minibar/personal | PERSONAL_NON_DEDUCTIBLE | 0% |
| Internet/wifi | TRAVEL | 100% |

### Learning Loop

Every human interaction feeds back into the system:
- **First-time assignment**: New VendorRule created with `source: learned`
- **Confirmation of suggestion**: VendorRule `examples` incremented, confidence boosted
- **Correction**: VendorRule updated with new mapping, old confidence lowered
- **Entity assignment**: Learned for future transactions from same vendor
- **Category assignment**: Learned for future transactions from same vendor
- **Any change**: Captured as learning event, rule updated or created

The system suggests aggressively and learns from every interaction. Goal: 95%+ auto-classification within 3 months of use.

---

## Dashboard Design

**Tech:** SvelteKit + FastAPI (Python) + SQLite

**Design Principles:**
- Apple design: minimal chrome, generous whitespace, progressive disclosure
- Zero friction: one click/keystroke to confirm
- Keyboard-first: y=confirm, e=edit, s=split, d=duplicate, arrows=navigate
- Pre-fill everything with best guess
- Dropdowns not free text for entity/category
- Batch operations where sensible

### Global UX Requirements

All list/table views:
- Click column headers to sort (toggle asc/desc, visual indicator)
- Date picker with presets: Today, This Week, This Month, This Quarter, YTD, Last Year, Custom Range
- Type-ahead search across all visible text fields
- Filters stack (entity + status + date range + search all active simultaneously)
- Persistent filter state within session
- Responsive: works on laptop screens (1280px minimum)

### View 1: Review Queue (Primary workspace)

Items needing human attention, ordered by priority:
1. Failures and errors (ingestion problems)
2. Possible duplicates
3. Low confidence classifications
4. First-time vendors
5. Reimbursables awaiting linking

Each card shows:
- Date, vendor, amount (large, scannable)
- Pre-filled entity and category dropdowns
- Confidence score and reasoning (small, secondary)
- Attachment thumbnails (click to preview)
- Primary action: Confirm (one click)
- Secondary: Edit & Confirm, Split, Reject
- For duplicates: Keep First, Keep Second, Keep Both

### View 2: Register

Full transaction list with:
- Sortable columns: Date, Vendor, Category, Amount, Entity, Status
- Inline editing (click any cell to edit with pre-filled dropdowns)
- Row color coding: confirmed (none), needs_review (amber), rejected (strikethrough)
- Running totals in footer: Income, Expenses, Net
- Export: CSV, FreeTaxUSA, TaxAct

### View 3: Health Dashboard

- Source status cards (last sync, record count, green/amber/red)
- Staleness warnings (no new data within expected window)
- Recent failure log with retry buttons
- Classification stats (auto vs human vs pending %)
- Upcoming deadlines (B&O dates, tax filing dates)
- Account memory stats (vendor rules count, last learned)

### View 4: Tax Summary

- Entity selector tabs: Sparkry, BlackLine, Personal
- IRS line-item breakdown with amounts
- Warning banner if unconfirmed transactions affect totals
- B&O revenue subtotals (monthly for Sparkry, quarterly for BlackLine)
- Export buttons per entity per format
- Print-friendly layout

### View 5: Accounts & Memory

- Vendor rules table (editable)
- Add/edit/delete rules manually
- See match history per rule
- Entity configuration
- Tax deadline calendar

---

## Ingestion Adapters

### Gmail/n8n Adapter

- Reads from: `SGDrive/LIVE_SYSTEM/accounting/{keep,for-review,manual,deductions}/`
- Tracks processed files via `IngestedFile` table in SQLite (not a sidecar JSON)
- Links JSON → attachments by hex ID prefix
- Extracts structured data from body_text (regex per known vendor format)
- Falls back to LLM for unrecognized formats

### Deduction Email Adapter

New pipeline for non-receipt deduction documentation:
- Charitable donation confirmations
- Mortgage interest statements (Form 1098)
- Property tax notices
- Medical bills
- Education expenses
- n8n addition: new label `accounting-deductions` with keyword detection
- Keywords: "donation receipt", "1098", "tax-deductible", "contribution confirmation", "property tax", "medical statement"

### Stripe Adapter

- Connects to Stripe API (separate API keys for Sparkry and BlackLine if separate accounts, or filter by metadata)
- Pulls: charges, payouts, invoices, refunds
- Maps Substack subscription payments → Sparkry income
- Maps BlackLine payment processing → BlackLine income/fees

### Shopify Adapter

- Connects to Shopify Admin API (BlackLine store)
- Pulls: orders (sales income), refunds, shipping costs, Shopify fees, payouts
- All transactions auto-tagged as BlackLine entity
- Reconciles Shopify payouts with bank deposits

### Brokerage CSV Adapter

- Supports: E*Trade, Schwab, Vanguard CSV formats
- Imports: trades (buy/sell with cost basis), dividends, interest
- Maps to Schedule D / Form 8949 categories
- Tracks: short-term vs long-term holding periods
- Wash sale detection: OUT OF SCOPE for v1. Import raw trades as-is; brokerages report wash sales on 1099-B. System imports the brokerage's wash sale adjustments rather than computing them independently.
- Manual trigger (not scheduled)

### Bank CSV Adapter

- Generic CSV parser with column mapping config per bank
- Imports: all transactions
- Primary use: cross-reference against other sources for completeness
- Flags transactions not matched by any other source
- Manual trigger

### Photo Receipt Adapter

- Watches for new images in accounting folders
- Sends to Claude Vision API for extraction
- Prompt: "Extract vendor, date, line items with amounts, tax, total, payment method"
- Auto-flags needs_review if confidence < 0.8 or amounts don't reconcile
- Supports: JPG, PNG, HEIC (HEIC converted to PNG via `pillow-heif` before sending to Claude Vision API, which does not natively accept HEIC)

---

## Error Handling

### Per-Adapter Error Handling

Every adapter wraps each record in try/catch:
- Normalize failure → log, skip record, continue
- Validation failure → log with details, skip, continue
- API timeout → retry with exponential backoff (3 attempts)
- Auth failure → log, halt adapter, surface on dashboard
- Partial success is OK — process what you can, log what you can't

### IngestionLog

Every adapter run produces an IngestionLog entry:
- Success: records_processed count, zero failures
- Partial failure: some records processed, failures logged with details
- Failure: adapter couldn't run at all (auth, connectivity)
- Dashboard shows all non-success logs with retry/acknowledge actions

### Staleness Detection

| Source | Expected Frequency | Warning Threshold |
|---|---|---|
| Gmail/n8n | Daily | 48 hours |
| Stripe | Weekly | 14 days |
| Shopify | Weekly | 14 days |
| Bank CSVs | Monthly | 45 days |
| Brokerage | Quarterly | 120 days |

### Reconciliation Checks

Periodic automated checks:
- Stripe payouts should appear in bank statements
- Shopify payouts should appear in bank statements
- Reimbursable expenses should have matching reimbursement within 30 days
- Monthly totals sanity check (flag if any month has 3x deviation from average)

---

## GAAP Considerations

This is a cash-basis accounting system (appropriate for small LLCs and personal taxes). Key principles applied:

- **Revenue recognition**: Recorded when payment received (not when invoiced)
- **Expense recognition**: Recorded when payment made
- **Matching principle**: Reimbursable expenses tracked and netted
- **Consistency**: Same classification rules applied across all periods
- **Audit trail**: Every transaction has full provenance (source, raw data, classification history)
- **Double-entry not required**: Cash basis with single-entry register is sufficient for Schedule C and simple 1065

If entities grow in complexity (e.g., accrual basis needed, or S-Corp election), the schema supports migration to double-entry by adding debit/credit fields.

---

## Project Structure

```
/Users/travis/SGDrive/dev/accounting/
├── CLAUDE.md                    # Project instructions for Claude Code
├── requirements/
│   └── current.md               # PRD requirements with REQ-IDs
├── src/
│   ├── adapters/                # One file per source
│   │   ├── base.py
│   │   ├── gmail_n8n.py
│   │   ├── stripe_adapter.py
│   │   ├── shopify_adapter.py
│   │   ├── brokerage_csv.py
│   │   ├── bank_csv.py
│   │   ├── photo_receipt.py
│   │   └── deduction_email.py
│   ├── classification/
│   │   ├── engine.py            # 3-tier classification orchestrator
│   │   ├── rules.py             # Tier 1: vendor rules
│   │   ├── patterns.py          # Tier 2: structural patterns
│   │   ├── llm_classifier.py    # Tier 3: Claude API
│   │   └── splitter.py          # Line-item splitting
│   ├── models/
│   │   ├── transaction.py
│   │   ├── vendor_rule.py
│   │   └── ingestion_log.py
│   ├── db/
│   │   ├── schema.sql
│   │   ├── migrations/
│   │   └── connection.py
│   ├── api/                     # FastAPI backend for dashboard
│   │   ├── main.py
│   │   ├── routes/
│   │   └── middleware/
│   ├── export/
│   │   ├── freetaxusa.py
│   │   ├── taxact.py
│   │   ├── bno_tax.py
│   │   └── csv_export.py
│   └── utils/
│       ├── dedup.py
│       ├── reconciliation.py
│       └── staleness.py
├── dashboard/                    # SvelteKit frontend
│   ├── src/
│   │   ├── routes/
│   │   │   ├── +page.svelte     # Review Queue (home)
│   │   │   ├── register/
│   │   │   ├── health/
│   │   │   ├── tax/
│   │   │   └── accounts/
│   │   ├── lib/
│   │   │   ├── components/      # Shared components
│   │   │   ├── stores/
│   │   │   └── api.ts           # FastAPI client
│   │   └── app.html
│   └── package.json
├── data/                        # GITIGNORED — backed up via SGDrive sync
│   ├── accounting.db            # SQLite register
│   └── imports/                 # Drop zone for CSV imports
├── tests/                       # Co-located with source (test_*.py alongside .py)
└── docs/
    └── superpowers/specs/
        └── 2026-03-15-accounting-system-design.md
```

---

## Schema Migrations

- **Tool:** Alembic (SQLAlchemy-based)
- **Location:** `src/db/migrations/`
- **Policy:** Every schema change gets a migration with both `upgrade()` and `downgrade()`. Migrations are tested against a copy of the production DB before applying to the real one.
- **Backup before migrate:** The `INGEST` skill always backs up `accounting.db` to `accounting.db.bak` before running any pending migrations.
- **Future path:** If double-entry is ever needed (S-Corp election, accrual basis), add `debit_account` and `credit_account` fields to Transaction via migration — existing data gets a default mapping.

---

## Security

- **Localhost-only binding:** FastAPI binds to `127.0.0.1:8000`, not `0.0.0.0`. Dashboard only accessible from the local machine.
- **API keys in `.env`:** Stripe, Shopify, Anthropic keys stored in `.env` (gitignored). Never logged, never in error output.
- **Read-only API scopes:** Stripe and Shopify connections use read-only scopes/permissions where available.
- **No PII in logs:** IngestionLog captures error details but redacts amounts, account numbers, and email bodies. Only vendor names and transaction IDs appear in logs.
- **SQLite in gitignored `data/`:** Financial data never enters git history. Backed up via SGDrive folder sync.

---

## Claude API Cost Controls

- **Per-run budget:** Each ingestion run has a configurable max LLM calls (default: 50). If exceeded, remaining items queued as `needs_review` without LLM classification.
- **Batch prompts:** Where possible, batch multiple transactions into a single LLM call (up to 10 items per prompt) to reduce API overhead.
- **Cache responses:** If the same vendor+amount pattern was classified recently (within 7 days), reuse the prior classification instead of calling the API again.
- **Dashboard cost tracking:** Health dashboard shows LLM API calls this month and estimated cost.

---

## Tax Subcategory Enum

Defined values for `tax_subcategory` (not free-form):

| Subcategory | Used With | Description |
|---|---|---|
| lodging | TRAVEL | Hotel room rate |
| lodging_tax | TRAVEL | Hotel occupancy/lodging tax |
| airfare | TRAVEL | Flight tickets |
| ground_transport | TRAVEL | Taxi, rideshare, rental car |
| wifi | TRAVEL | In-flight or hotel wifi |
| parking | TRAVEL | Airport/hotel parking |
| business_meals | MEALS | Client meals, travel meals |
| saas | SUPPLIES | Software subscriptions |
| cloud_infra | SUPPLIES | Hosting, compute, API costs |
| shipping | SUPPLIES | FedEx, USPS, etc. |
| packaging | SUPPLIES | EcoEnclose, Sticker Mule |
| raw_materials | COGS | Product sourcing (Leeline, Brist Mfg) |
| inventory | COGS | Finished goods |
| platform_fees | OTHER_EXPENSE | Shopify, Stripe processing fees |
| registered_agent | LEGAL_AND_PROFESSIONAL | Northwest Registered Agent |

New subcategories can be added via the Accounts & Memory dashboard view.

---

## Tax Export Caveats

**FreeTaxUSA:** Supports CSV import for 1099-B (brokerage data). For Schedule C and Schedule A, data entry is manual but the system produces a print-friendly summary aligned to IRS line numbers so entry is fast and error-free. Research needed: verify if any additional import formats are supported.

**TaxAct Business 1065:** Supports data entry with form navigation. The system produces a print-friendly summary aligned to Form 1065 lines. Research needed: verify if TaxAct supports any CSV/data import for the 1065.

**Charitable Stock Donations (FMV):** The system does NOT compute fair market value for stock donations. The brokerage provides FMV on the transfer confirmation. These must be manually entered with FMV from the brokerage statement. The deduction email adapter captures the donation receipt; the human adds FMV during review.

---

## Skills & Agents Needed

Skills to create (via /skill-creator) for the accounting project's CLAUDE.md:

1. **ingest** — Run all adapters, process new records, report results
2. **classify** — Run classification on unclassified transactions
3. **review** — Interactive CLI review of needs_review items (backup to dashboard)
4. **export-tax** — Generate tax-ready exports for specified entity and year
5. **export-bno** — Generate B&O tax report for specified entity and period
6. **reconcile** — Run reconciliation checks, surface discrepancies
7. **import-csv** — Import a brokerage or bank CSV file
8. **health-check** — Check all source freshness, report status

### MCP Servers / Plugins to Connect

- **Stripe MCP** or direct API via Python `stripe` library
- **Shopify Admin API** via Python `shopify` library or MCP
- **Claude API** (already available) for LLM classification and photo OCR
- **Google Drive MCP** (optional, for direct Drive access vs. SGDrive sync)

### n8n Workflow Changes

1. Add `accounting-deductions` Gmail label
2. Add keyword detection for deduction-related emails
3. Route to new `deductions/` folder in Google Drive
4. Consider adding more granular categorization keywords to reduce `for-review` volume
