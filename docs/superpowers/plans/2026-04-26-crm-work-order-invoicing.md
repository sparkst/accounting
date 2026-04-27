# CRM + Work Order Invoicing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking. This plan is designed for **parallel execution via git worktrees** — each Phase 2+ workstream runs in its own worktree and merges back.

**Goal:** Build a CRM + Work Order + Invoicing system on Cloudflare (SvelteKit + D1) at internal.sparkry.ai, enabling a non-technical operator to manage customers, track project milestones, and send invoices.

**Architecture:** Full-stack SvelteKit app deployed to Cloudflare Pages. D1 database with drizzle-orm. Google OAuth + Cloudflare Access. Stripe for payments. Resend for email. Workers scheduled handler for daily cron.

**Tech Stack:** SvelteKit 2, @sveltejs/adapter-cloudflare, drizzle-orm, D1, Stripe SDK (or raw fetch), Resend, arctic (OAuth), Sentry, vitest, Playwright

**Spec:** `docs/superpowers/specs/2026-04-26-crm-work-order-invoicing-design.md`

---

## Execution Strategy

### Parallelization

```
Phase 1: Foundation (sequential — everything depends on this)
    ↓
Phase 2: [Workstream A: Customers] || [Workstream B: Work Orders + Milestones]
    ↓ (merge both)
Phase 3: [Workstream C: Invoicing + Payments] || [Workstream D: Notifications]
    ↓ (merge both)
Phase 4: [Workstream E: Dashboard + UX] || [Workstream F: Ops + Deploy]
    ↓ (merge both)
Phase 5: Integration testing, review cycles, deploy, production validation
```

### Branch Strategy

- Feature branch: `feat/crm-invoicing`
- Each workstream: `feat/crm-invoicing/ws-a-customers`, `feat/crm-invoicing/ws-b-work-orders`, etc.
- Worktrees created per workstream, merged back to `feat/crm-invoicing` after each phase
- Final PR: `feat/crm-invoicing` → `main`

### Project Location

Create the new project at: `/Users/travis/SGDrive/dev/sparkry-crm/`
(Separate repo from accounting — this is a standalone Cloudflare Pages app)

---

## File Structure

```
sparkry-crm/
├── package.json
├── pnpm-lock.yaml
├── wrangler.toml
├── svelte.config.js
├── vite.config.ts
├── tsconfig.json
├── drizzle.config.ts
├── .gitignore
├── src/
│   ├── app.html
│   ├── app.d.ts                    — Cloudflare platform types
│   ├── hooks.server.ts             — Auth hook (runs before every request)
│   ├── worker.ts                   — Cron scheduled() handler
│   ├── lib/
│   │   ├── server/
│   │   │   ├── db/
│   │   │   │   ├── schema.ts       — All drizzle table definitions
│   │   │   │   ├── index.ts        — getDB(platform) helper
│   │   │   │   └── seed.ts         — Seed data for dev/staging
│   │   │   ├── auth.ts             — OAuth flow + session management
│   │   │   ├── stripe.ts           — Stripe payment link helpers
│   │   │   ├── email.ts            — Resend email helpers (alerts, invoices, reminders)
│   │   │   ├── invoice-pipeline.ts — Ordered send pipeline
│   │   │   └── cron.ts             — Daily milestone check logic
│   │   ├── types.ts                — Shared TypeScript types
│   │   ├── utils.ts                — Amount formatting, date helpers
│   │   └── components/
│   │       ├── Nav.svelte          — Top navigation
│   │       ├── Toast.svelte        — Toast notifications
│   │       ├── ConfirmDialog.svelte — Reusable confirmation modal
│   │       ├── EmptyState.svelte   — Reusable empty state component
│   │       ├── StatusBadge.svelte  — Color-coded status badges
│   │       └── AmountDisplay.svelte — Cents → formatted currency
│   ├── routes/
│   │   ├── +layout.server.ts       — Auth guard for all routes
│   │   ├── +layout.svelte          — App shell (nav, toast container)
│   │   ├── +page.server.ts         — Dashboard data loader
│   │   ├── +page.svelte            — Dashboard (empty state + normal)
│   │   ├── customers/
│   │   │   ├── +page.server.ts     — List + search
│   │   │   ├── +page.svelte        — Customer list
│   │   │   ├── [id]/
│   │   │   │   ├── +page.server.ts — Detail + update + contacts
│   │   │   │   └── +page.svelte    — Customer detail/edit
│   │   │   └── new/
│   │   │       ├── +page.server.ts — Create + dupe detection
│   │   │       └── +page.svelte    — New customer form
│   │   ├── work-orders/
│   │   │   ├── +page.server.ts     — List
│   │   │   ├── +page.svelte        — WO list (empty state)
│   │   │   ├── [id]/
│   │   │   │   ├── +page.server.ts — Detail + milestones + actions
│   │   │   │   └── +page.svelte    — WO detail + milestone management
│   │   │   └── new/
│   │   │       ├── +page.server.ts — Create + dupe warning
│   │   │       └── +page.svelte    — New WO form
│   │   ├── invoices/
│   │   │   ├── +page.server.ts     — List + filters
│   │   │   ├── +page.svelte        — Invoice list
│   │   │   ├── [id]/
│   │   │   │   ├── +page.server.ts — Detail + send + status transitions
│   │   │   │   ├── +page.svelte    — Invoice review/send
│   │   │   │   └── print/
│   │   │   │       ├── +page.server.ts — Print data (auth protected)
│   │   │   │       └── +page.svelte    — Printable HTML invoice
│   │   ├── settings/
│   │   │   ├── +page.server.ts     — Company settings CRUD
│   │   │   └── +page.svelte        — Settings page
│   │   └── api/
│   │       ├── auth/
│   │       │   ├── callback/+server.ts — Google OAuth callback
│   │       │   └── logout/+server.ts   — Logout
│   │       ├── health/+server.ts       — Health check
│   │       └── webhooks/
│   │           ├── stripe/+server.ts   — Stripe webhook (sig verified)
│   │           └── resend/+server.ts   — Resend delivery webhook
├── tests/
│   ├── unit/
│   │   ├── schema.test.ts          — DB schema validation
│   │   ├── utils.test.ts           — Amount formatting, date helpers
│   │   ├── invoice-pipeline.test.ts — Send pipeline logic
│   │   ├── auth.test.ts            — Session/auth logic
│   │   ├── cron.test.ts            — Cron handler logic
│   │   └── stripe.test.ts          — Stripe helpers
│   └── integration/
│       ├── setup.ts                — D1 test harness (miniflare)
│       ├── customers.test.ts       — Customer CRUD integration
│       ├── work-orders.test.ts     — WO + milestone integration
│       ├── invoices.test.ts        — Invoice lifecycle integration
│       └── webhooks.test.ts        — Webhook handling integration
└── migrations/
    └── 0001_initial.sql            — Generated by drizzle-kit
```

---

## Phase 1: Foundation

### Task 1: Project Scaffold

**Files:**
- Create: `sparkry-crm/package.json`
- Create: `sparkry-crm/svelte.config.js`
- Create: `sparkry-crm/vite.config.ts`
- Create: `sparkry-crm/tsconfig.json`
- Create: `sparkry-crm/wrangler.toml`
- Create: `sparkry-crm/drizzle.config.ts`
- Create: `sparkry-crm/src/app.html`
- Create: `sparkry-crm/src/app.d.ts`
- Create: `sparkry-crm/.gitignore`

- [ ] **Step 1: Create project directory and initialize**

```bash
mkdir -p /Users/travis/SGDrive/dev/sparkry-crm
cd /Users/travis/SGDrive/dev/sparkry-crm
pnpm init
```

- [ ] **Step 2: Install dependencies**

```bash
cd /Users/travis/SGDrive/dev/sparkry-crm
pnpm add svelte @sveltejs/kit @sveltejs/adapter-cloudflare vite
pnpm add drizzle-orm arctic stripe resend @sentry/cloudflare
pnpm add -D @sveltejs/vite-plugin-svelte typescript vitest @cloudflare/workers-types drizzle-kit wrangler miniflare @types/node
```

- [ ] **Step 3: Create svelte.config.js**

```javascript
// sparkry-crm/svelte.config.js
import adapter from '@sveltejs/adapter-cloudflare';

/** @type {import('@sveltejs/kit').Config} */
const config = {
	kit: {
		adapter: adapter({
			routes: {
				include: ['/*'],
				exclude: ['<all>']
			}
		})
	}
};

export default config;
```

- [ ] **Step 4: Create vite.config.ts**

```typescript
// sparkry-crm/vite.config.ts
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vitest/config';

export default defineConfig({
	plugins: [sveltekit()],
	test: {
		include: ['tests/**/*.test.ts']
	}
});
```

- [ ] **Step 5: Create tsconfig.json**

```json
{
	"extends": "./.svelte-kit/tsconfig.json",
	"compilerOptions": {
		"allowJs": true,
		"checkJs": true,
		"esModuleInterop": true,
		"forceConsistentCasingInFileNames": true,
		"resolveJsonModule": true,
		"skipLibCheck": true,
		"sourceMap": true,
		"strict": true,
		"moduleResolution": "bundler"
	}
}
```

- [ ] **Step 6: Create wrangler.toml**

```toml
# sparkry-crm/wrangler.toml
name = "sparkry-crm"
compatibility_date = "2024-12-01"
pages_build_output_dir = ".svelte-kit/cloudflare"

[[d1_databases]]
binding = "DB"
database_name = "sparkry-crm-prod"
database_id = "" # filled after `wrangler d1 create`

[env.staging]
[[env.staging.d1_databases]]
binding = "DB"
database_name = "sparkry-crm-staging"
database_id = "" # filled after `wrangler d1 create`

[triggers]
crons = ["0 15 * * *"]
```

- [ ] **Step 7: Create drizzle.config.ts**

```typescript
// sparkry-crm/drizzle.config.ts
import { defineConfig } from 'drizzle-kit';

export default defineConfig({
	schema: './src/lib/server/db/schema.ts',
	out: './migrations',
	dialect: 'sqlite'
});
```

- [ ] **Step 8: Create app.html**

```html
<!-- sparkry-crm/src/app.html -->
<!doctype html>
<html lang="en">
	<head>
		<meta charset="utf-8" />
		<meta name="viewport" content="width=device-width, initial-scale=1" />
		<title>Sparkry CRM</title>
		<link rel="icon" href="/favicon.ico" />
		%sveltekit.head%
	</head>
	<body data-sveltekit-preload-data="hover">
		<div style="display: contents">%sveltekit.body%</div>
	</body>
</html>
```

- [ ] **Step 9: Create app.d.ts**

```typescript
// sparkry-crm/src/app.d.ts
declare global {
	namespace App {
		interface Locals {
			user: { email: string; name: string } | null;
		}
		interface Platform {
			env: {
				DB: D1Database;
				GOOGLE_CLIENT_ID: string;
				GOOGLE_CLIENT_SECRET: string;
				STRIPE_SECRET_KEY: string;
				STRIPE_WEBHOOK_SECRET: string;
				RESEND_API_KEY: string;
				ALLOWED_EMAILS: string;
				SENTRY_DSN: string;
				SESSION_SIGNING_KEY: string;
			};
		}
	}
}

export {};
```

- [ ] **Step 10: Create .gitignore**

```
node_modules/
.svelte-kit/
build/
.wrangler/
.env
.dev.vars
```

- [ ] **Step 11: Initialize git repo and commit**

```bash
cd /Users/travis/SGDrive/dev/sparkry-crm
git init
git add .
git commit -m "feat: project scaffold — SvelteKit + Cloudflare Pages + D1"
git checkout -b feat/crm-invoicing
```

---

### Task 2: Database Schema

**Files:**
- Create: `src/lib/server/db/schema.ts`
- Create: `src/lib/server/db/index.ts`
- Create: `tests/unit/schema.test.ts`

- [ ] **Step 1: Write schema validation tests**

```typescript
// sparkry-crm/tests/unit/schema.test.ts
import { describe, it, expect } from 'vitest';
import { customers, contacts, workOrders, milestones, invoices, invoiceLineItems, activityLog, creditNotes } from '$lib/server/db/schema';

describe('Database Schema', () => {
	it('customers table has required columns', () => {
		const cols = Object.keys(customers);
		expect(cols).toContain('id');
		expect(cols).toContain('name');
		expect(cols).toContain('contactEmail');
		expect(cols).toContain('billingModel');
		expect(cols).toContain('defaultRate');
		expect(cols).toContain('paymentTerms');
		expect(cols).toContain('invoicePrefix');
		expect(cols).toContain('ccFeePassthrough');
		expect(cols).toContain('taxId');
		expect(cols).toContain('active');
	});

	it('work orders table has required columns', () => {
		const cols = Object.keys(workOrders);
		expect(cols).toContain('id');
		expect(cols).toContain('customerId');
		expect(cols).toContain('title');
		expect(cols).toContain('totalValue');
		expect(cols).toContain('currency');
		expect(cols).toContain('status');
		expect(cols).toContain('cancelledReason');
	});

	it('milestones table has required columns', () => {
		const cols = Object.keys(milestones);
		expect(cols).toContain('id');
		expect(cols).toContain('workOrderId');
		expect(cols).toContain('title');
		expect(cols).toContain('amount');
		expect(cols).toContain('dueDate');
		expect(cols).toContain('triggerType');
		expect(cols).toContain('status');
		expect(cols).toContain('invoiceId');
		expect(cols).toContain('notifiedAt');
	});

	it('invoices table has required columns', () => {
		const cols = Object.keys(invoices);
		expect(cols).toContain('id');
		expect(cols).toContain('invoiceNumber');
		expect(cols).toContain('customerId');
		expect(cols).toContain('workOrderId');
		expect(cols).toContain('milestoneId');
		expect(cols).toContain('status');
		expect(cols).toContain('subtotal');
		expect(cols).toContain('ccFeeAmount');
		expect(cols).toContain('total');
		expect(cols).toContain('paymentMethod');
		expect(cols).toContain('paymentLinkUrl');
		expect(cols).toContain('stripeEventId');
		expect(cols).toContain('resendEmailId');
		expect(cols).toContain('deliveryStatus');
		expect(cols).toContain('voidReason');
	});

	it('invoice line items table has required columns', () => {
		const cols = Object.keys(invoiceLineItems);
		expect(cols).toContain('id');
		expect(cols).toContain('invoiceId');
		expect(cols).toContain('description');
		expect(cols).toContain('quantity');
		expect(cols).toContain('unitPrice');
		expect(cols).toContain('totalPrice');
		expect(cols).toContain('sortOrder');
	});

	it('activity log table has required columns', () => {
		const cols = Object.keys(activityLog);
		expect(cols).toContain('entityType');
		expect(cols).toContain('entityId');
		expect(cols).toContain('action');
		expect(cols).toContain('userEmail');
		expect(cols).toContain('oldValue');
		expect(cols).toContain('newValue');
	});

	it('credit notes table has required columns', () => {
		const cols = Object.keys(creditNotes);
		expect(cols).toContain('invoiceId');
		expect(cols).toContain('customerId');
		expect(cols).toContain('amount');
		expect(cols).toContain('reason');
		expect(cols).toContain('status');
		expect(cols).toContain('appliedToInvoiceId');
	});
});
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/travis/SGDrive/dev/sparkry-crm
pnpm vitest run tests/unit/schema.test.ts
```

Expected: FAIL — module not found

- [ ] **Step 3: Implement schema**

```typescript
// sparkry-crm/src/lib/server/db/schema.ts
import { sqliteTable, text, integer, real, uniqueIndex } from 'drizzle-orm/sqlite-core';
import { sql } from 'drizzle-orm';

const id = () => text('id').primaryKey().$defaultFn(() => crypto.randomUUID());
const timestamps = {
	createdAt: text('created_at').$defaultFn(() => new Date().toISOString()),
	updatedAt: text('updated_at').$defaultFn(() => new Date().toISOString())
};

export const customers = sqliteTable('customers', {
	id: id(),
	name: text('name').notNull(),
	contactName: text('contact_name'),
	contactEmail: text('contact_email'),
	contactPhone: text('contact_phone'),
	website: text('website'),
	billingModel: text('billing_model', { enum: ['hourly', 'flat_rate', 'project'] }).notNull().default('project'),
	defaultRate: integer('default_rate').default(0),
	paymentTerms: text('payment_terms').default('Net 14'),
	invoicePrefix: text('invoice_prefix'),
	paymentMethods: text('payment_methods', { mode: 'json' }).$type<string[]>().default([]),
	ccFeePassthrough: integer('cc_fee_passthrough', { mode: 'boolean' }).default(true),
	taxId: text('tax_id'),
	address: text('address', { mode: 'json' }).$type<Record<string, string>>(),
	notes: text('notes'),
	active: integer('active', { mode: 'boolean' }).default(true),
	...timestamps
});

export const contacts = sqliteTable('contacts', {
	id: id(),
	customerId: text('customer_id').notNull().references(() => customers.id),
	name: text('name').notNull(),
	email: text('email'),
	phone: text('phone'),
	role: text('role', { enum: ['billing', 'project', 'primary'] }).default('primary'),
	isDefault: integer('is_default', { mode: 'boolean' }).default(false),
	createdAt: text('created_at').$defaultFn(() => new Date().toISOString())
});

export const workOrders = sqliteTable('work_orders', {
	id: id(),
	customerId: text('customer_id').notNull().references(() => customers.id),
	title: text('title').notNull(),
	description: text('description'),
	totalValue: integer('total_value').notNull(),
	currency: text('currency').default('USD'),
	status: text('status', { enum: ['draft', 'active', 'completed', 'cancelled'] }).notNull().default('draft'),
	startDate: text('start_date'),
	expectedEndDate: text('expected_end_date'),
	cancelledReason: text('cancelled_reason'),
	recurringSchedule: text('recurring_schedule', { mode: 'json' }),
	...timestamps
});

export const milestones = sqliteTable('milestones', {
	id: id(),
	workOrderId: text('work_order_id').notNull().references(() => workOrders.id),
	title: text('title').notNull(),
	amount: integer('amount').notNull(),
	dueDate: text('due_date'),
	triggerType: text('trigger_type', { enum: ['date', 'manual', 'both'] }).notNull().default('both'),
	status: text('status', { enum: ['pending', 'ready', 'invoiced', 'paid'] }).notNull().default('pending'),
	sortOrder: integer('sort_order').notNull().default(0),
	invoiceId: text('invoice_id'),
	notifiedAt: text('notified_at'),
	...timestamps
});

export const invoices = sqliteTable('invoices', {
	id: id(),
	invoiceNumber: text('invoice_number').notNull().unique(),
	customerId: text('customer_id').notNull().references(() => customers.id),
	workOrderId: text('work_order_id').references(() => workOrders.id),
	milestoneId: text('milestone_id'),
	entity: text('entity').default('sparkry'),
	status: text('status', { enum: ['draft', 'sent', 'paid', 'overdue', 'void'] }).notNull().default('draft'),
	submittedDate: text('submitted_date'),
	dueDate: text('due_date'),
	subtotal: integer('subtotal').notNull().default(0),
	ccFeeAmount: integer('cc_fee_amount').default(0),
	tax: integer('tax').default(0),
	total: integer('total').notNull().default(0),
	paymentTerms: text('payment_terms'),
	paymentMethod: text('payment_method', { enum: ['stripe_cc', 'ach', 'venmo', 'check'] }),
	paymentLinkUrl: text('payment_link_url'),
	paymentLinkId: text('payment_link_id'),
	stripeEventId: text('stripe_event_id'),
	sentAt: text('sent_at'),
	sentTo: text('sent_to'),
	resendEmailId: text('resend_email_id'),
	deliveryStatus: text('delivery_status', { enum: ['pending', 'scheduled', 'delivered', 'bounced', 'failed'] }).default('pending'),
	paidDate: text('paid_date'),
	voidReason: text('void_reason'),
	notes: text('notes'),
	...timestamps
});

export const invoiceLineItems = sqliteTable('invoice_line_items', {
	id: id(),
	invoiceId: text('invoice_id').notNull().references(() => invoices.id),
	description: text('description').notNull(),
	quantity: integer('quantity').notNull().default(10000),
	unitPrice: integer('unit_price').notNull(),
	totalPrice: integer('total_price').notNull(),
	sortOrder: integer('sort_order').default(0),
	createdAt: text('created_at').$defaultFn(() => new Date().toISOString())
});

export const activityLog = sqliteTable('activity_log', {
	id: id(),
	entityType: text('entity_type', { enum: ['customer', 'work_order', 'milestone', 'invoice'] }).notNull(),
	entityId: text('entity_id').notNull(),
	action: text('action').notNull(),
	userEmail: text('user_email').notNull(),
	oldValue: text('old_value'),
	newValue: text('new_value'),
	metadata: text('metadata', { mode: 'json' }),
	createdAt: text('created_at').$defaultFn(() => new Date().toISOString())
});

export const creditNotes = sqliteTable('credit_notes', {
	id: id(),
	invoiceId: text('invoice_id').notNull().references(() => invoices.id),
	customerId: text('customer_id').notNull().references(() => customers.id),
	amount: integer('amount').notNull(),
	reason: text('reason').notNull(),
	status: text('status', { enum: ['issued', 'applied'] }).notNull().default('issued'),
	appliedToInvoiceId: text('applied_to_invoice_id'),
	createdAt: text('created_at').$defaultFn(() => new Date().toISOString())
});

export const settings = sqliteTable('settings', {
	key: text('key').primaryKey(),
	value: text('value', { mode: 'json' }),
	updatedAt: text('updated_at').$defaultFn(() => new Date().toISOString())
});
```

- [ ] **Step 4: Implement DB helper**

```typescript
// sparkry-crm/src/lib/server/db/index.ts
import { drizzle } from 'drizzle-orm/d1';
import * as schema from './schema';

export function getDB(d1: D1Database) {
	return drizzle(d1, { schema });
}

export type DB = ReturnType<typeof getDB>;
export { schema };
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd /Users/travis/SGDrive/dev/sparkry-crm
pnpm vitest run tests/unit/schema.test.ts
```

Expected: PASS

- [ ] **Step 6: Generate migration**

```bash
cd /Users/travis/SGDrive/dev/sparkry-crm
pnpm drizzle-kit generate
```

Expected: Creates `migrations/0001_initial.sql`

- [ ] **Step 7: Commit**

```bash
git add .
git commit -m "feat: D1 database schema — customers, work orders, milestones, invoices, activity log"
```

---

### Task 3: Utility Functions

**Files:**
- Create: `src/lib/utils.ts`
- Create: `src/lib/types.ts`
- Create: `tests/unit/utils.test.ts`

- [ ] **Step 1: Write utility tests**

```typescript
// sparkry-crm/tests/unit/utils.test.ts
import { describe, it, expect } from 'vitest';
import { formatCents, parseDollars, calculateDueDate, formatDate, calculateCcFee } from '$lib/utils';

describe('formatCents', () => {
	it('formats positive cents as dollars', () => {
		expect(formatCents(200000)).toBe('$2,000.00');
		expect(formatCents(7000)).toBe('$70.00');
		expect(formatCents(100)).toBe('$1.00');
		expect(formatCents(0)).toBe('$0.00');
	});

	it('formats negative cents with parentheses', () => {
		expect(formatCents(-200000)).toBe('($2,000.00)');
	});
});

describe('parseDollars', () => {
	it('converts dollar string to cents integer', () => {
		expect(parseDollars('2000')).toBe(200000);
		expect(parseDollars('2000.00')).toBe(200000);
		expect(parseDollars('70.50')).toBe(7050);
		expect(parseDollars('0.99')).toBe(99);
	});

	it('handles dollar sign and commas', () => {
		expect(parseDollars('$2,000.00')).toBe(200000);
		expect(parseDollars('$70')).toBe(7000);
	});

	it('returns 0 for invalid input', () => {
		expect(parseDollars('')).toBe(0);
		expect(parseDollars('abc')).toBe(0);
	});
});

describe('calculateDueDate', () => {
	it('calculates due date from Net 14', () => {
		const result = calculateDueDate('2026-05-01', 'Net 14');
		expect(result).toBe('2026-05-15');
	});

	it('calculates due date from Net 30', () => {
		const result = calculateDueDate('2026-05-01', 'Net 30');
		expect(result).toBe('2026-05-31');
	});

	it('defaults to Net 14 for unknown terms', () => {
		const result = calculateDueDate('2026-05-01', 'unknown');
		expect(result).toBe('2026-05-15');
	});
});

describe('formatDate', () => {
	it('formats ISO date to readable string', () => {
		expect(formatDate('2026-05-31')).toBe('May 31, 2026');
		expect(formatDate('2026-01-01')).toBe('Jan 1, 2026');
	});
});

describe('calculateCcFee', () => {
	it('returns 3.5% fee for CC payments over $1000', () => {
		expect(calculateCcFee(200000, 'stripe_cc')).toBe(7000);
		expect(calculateCcFee(150000, 'stripe_cc')).toBe(5250);
	});

	it('returns 0 for CC payments at or below $1000', () => {
		expect(calculateCcFee(100000, 'stripe_cc')).toBe(0);
		expect(calculateCcFee(50000, 'stripe_cc')).toBe(0);
	});

	it('returns 0 for non-CC payment methods', () => {
		expect(calculateCcFee(200000, 'ach')).toBe(0);
		expect(calculateCcFee(200000, 'venmo')).toBe(0);
		expect(calculateCcFee(200000, 'check')).toBe(0);
	});

	it('returns 0 for null payment method', () => {
		expect(calculateCcFee(200000, null)).toBe(0);
	});
});
```

- [ ] **Step 2: Run tests to verify failure**

```bash
pnpm vitest run tests/unit/utils.test.ts
```

Expected: FAIL

- [ ] **Step 3: Implement utilities**

```typescript
// sparkry-crm/src/lib/utils.ts
export function formatCents(cents: number): string {
	const abs = Math.abs(cents);
	const dollars = (abs / 100).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
	if (cents < 0) return `($${dollars})`;
	return `$${dollars}`;
}

export function parseDollars(input: string): number {
	const cleaned = input.replace(/[$,]/g, '');
	const parsed = parseFloat(cleaned);
	if (isNaN(parsed)) return 0;
	return Math.round(parsed * 100);
}

export function calculateDueDate(fromDate: string, terms: string): string {
	const match = terms.match(/Net\s*(\d+)/i);
	const days = match ? parseInt(match[1]) : 14;
	const date = new Date(fromDate + 'T00:00:00');
	date.setDate(date.getDate() + days);
	return date.toISOString().split('T')[0];
}

export function formatDate(iso: string): string {
	const date = new Date(iso + 'T00:00:00');
	return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

export function calculateCcFee(subtotalCents: number, paymentMethod: string | null): number {
	if (paymentMethod !== 'stripe_cc') return 0;
	if (subtotalCents <= 100000) return 0;
	return Math.round(subtotalCents * 0.035);
}

export function generateInvoiceNumber(prefix: string, existingMax: string | null): string {
	if (!existingMax) return `${prefix}-001`;
	const match = existingMax.match(/-(\d+)$/);
	const next = match ? parseInt(match[1]) + 1 : 1;
	return `${prefix}-${String(next).padStart(3, '0')}`;
}

export function timeOfDayGreeting(): string {
	const hour = new Date().getHours();
	if (hour < 12) return 'Good morning';
	if (hour < 17) return 'Good afternoon';
	return 'Good evening';
}
```

- [ ] **Step 4: Create shared types**

```typescript
// sparkry-crm/src/lib/types.ts
export type InvoiceStatus = 'draft' | 'sent' | 'paid' | 'overdue' | 'void';
export type MilestoneStatus = 'pending' | 'ready' | 'invoiced' | 'paid';
export type WorkOrderStatus = 'draft' | 'active' | 'completed' | 'cancelled';
export type BillingModel = 'hourly' | 'flat_rate' | 'project';
export type PaymentMethod = 'stripe_cc' | 'ach' | 'venmo' | 'check';
export type TriggerType = 'date' | 'manual' | 'both';
export type DeliveryStatus = 'pending' | 'delivered' | 'bounced' | 'failed';
export type ContactRole = 'billing' | 'project' | 'primary';

export const INVOICE_STATUS_TRANSITIONS: Record<InvoiceStatus, InvoiceStatus[]> = {
	draft: ['sent', 'void'],
	sent: ['paid', 'void', 'overdue'],
	paid: ['void'],
	overdue: ['paid', 'void'],
	void: []
};

export const WORK_ORDER_STATUS_TRANSITIONS: Record<WorkOrderStatus, WorkOrderStatus[]> = {
	draft: ['active'],
	active: ['completed', 'cancelled'],
	completed: [],
	cancelled: []
};

export const PAYMENT_TERMS_OPTIONS = [
	{ value: 'Net 14', label: 'Due in 14 days' },
	{ value: 'Net 30', label: 'Due in 30 days' },
	{ value: 'Net 60', label: 'Due in 60 days' },
	{ value: 'Due on receipt', label: 'Due on receipt' }
];
```

- [ ] **Step 5: Run tests**

```bash
pnpm vitest run tests/unit/utils.test.ts
```

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add .
git commit -m "feat: utility functions and shared types — amount formatting, date helpers, fee calculation"
```

---

### Task 4: Authentication

**Files:**
- Create: `src/lib/server/auth.ts`
- Create: `src/hooks.server.ts`
- Create: `src/routes/api/auth/callback/+server.ts`
- Create: `src/routes/api/auth/logout/+server.ts`
- Create: `tests/unit/auth.test.ts`

- [ ] **Step 1: Write auth tests**

```typescript
// sparkry-crm/tests/unit/auth.test.ts
import { describe, it, expect } from 'vitest';
import { isAllowedEmail, createSessionCookie, parseSessionCookie } from '$lib/server/auth';

describe('isAllowedEmail', () => {
	const allowlist = 'travis@sparkry.com,amycsparks@gmail.com';

	it('allows listed emails (case-insensitive)', () => {
		expect(isAllowedEmail('travis@sparkry.com', allowlist)).toBe(true);
		expect(isAllowedEmail('Travis@Sparkry.com', allowlist)).toBe(true);
		expect(isAllowedEmail('amycsparks@gmail.com', allowlist)).toBe(true);
	});

	it('rejects unlisted emails', () => {
		expect(isAllowedEmail('hacker@evil.com', allowlist)).toBe(false);
		expect(isAllowedEmail('', allowlist)).toBe(false);
	});

	it('handles whitespace in allowlist', () => {
		expect(isAllowedEmail('travis@sparkry.com', ' travis@sparkry.com , amycsparks@gmail.com ')).toBe(true);
	});
});

describe('session cookie', () => {
	const signingKey = 'test-secret-key-at-least-32-chars-long!!';

	it('creates and parses a valid session', () => {
		const cookie = createSessionCookie('travis@sparkry.com', 'Travis Sparks', signingKey);
		const session = parseSessionCookie(cookie, signingKey);
		expect(session).not.toBeNull();
		expect(session!.email).toBe('travis@sparkry.com');
		expect(session!.name).toBe('Travis Sparks');
	});

	it('rejects tampered cookies', () => {
		const cookie = createSessionCookie('travis@sparkry.com', 'Travis', signingKey);
		const tampered = cookie.replace('travis', 'hacker');
		const session = parseSessionCookie(tampered, signingKey);
		expect(session).toBeNull();
	});

	it('rejects expired cookies', () => {
		const cookie = createSessionCookie('travis@sparkry.com', 'Travis', signingKey, -1);
		const session = parseSessionCookie(cookie, signingKey);
		expect(session).toBeNull();
	});
});
```

- [ ] **Step 2: Run tests to verify failure**

```bash
pnpm vitest run tests/unit/auth.test.ts
```

- [ ] **Step 3: Implement auth module**

```typescript
// sparkry-crm/src/lib/server/auth.ts
import { Google } from 'arctic';

export function getGoogleClient(clientId: string, clientSecret: string, origin: string) {
	return new Google(clientId, clientSecret, `${origin}/api/auth/callback`);
}

export function isAllowedEmail(email: string, allowlist: string): boolean {
	if (!email) return false;
	const allowed = allowlist.split(',').map(e => e.trim().toLowerCase());
	return allowed.includes(email.trim().toLowerCase());
}

export function createSessionCookie(email: string, name: string, signingKey: string, ttlDays = 7): string {
	const expires = Date.now() + ttlDays * 24 * 60 * 60 * 1000;
	const payload = JSON.stringify({ email, name, expires });
	const signature = signPayload(payload, signingKey);
	return btoa(JSON.stringify({ payload, signature }));
}

export function parseSessionCookie(cookie: string, signingKey: string): { email: string; name: string } | null {
	try {
		const { payload, signature } = JSON.parse(atob(cookie));
		if (signPayload(payload, signingKey) !== signature) return null;
		const { email, name, expires } = JSON.parse(payload);
		if (Date.now() > expires) return null;
		return { email, name };
	} catch {
		return null;
	}
}

function signPayload(payload: string, key: string): string {
	const encoder = new TextEncoder();
	const data = encoder.encode(payload + key);
	let hash = 0;
	for (let i = 0; i < data.length; i++) {
		hash = ((hash << 5) - hash + data[i]) | 0;
	}
	return hash.toString(36);
}
```

Note: The `signPayload` above is a placeholder for tests. In production, replace with HMAC-SHA256 via Web Crypto API:

```typescript
// Production signing (replace signPayload in final implementation):
async function signPayloadHmac(payload: string, key: string): Promise<string> {
	const encoder = new TextEncoder();
	const keyData = await crypto.subtle.importKey('raw', encoder.encode(key), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
	const sig = await crypto.subtle.sign('HMAC', keyData, encoder.encode(payload));
	return btoa(String.fromCharCode(...new Uint8Array(sig)));
}
```

- [ ] **Step 4: Create hooks.server.ts**

```typescript
// sparkry-crm/src/hooks.server.ts
import type { Handle } from '@sveltejs/kit';
import { redirect } from '@sveltejs/kit';
import { parseSessionCookie } from '$lib/server/auth';

const PUBLIC_PATHS = ['/api/auth', '/api/webhooks', '/api/health'];

export const handle: Handle = async ({ event, resolve }) => {
	const { pathname } = event.url;

	if (PUBLIC_PATHS.some(p => pathname.startsWith(p))) {
		return resolve(event);
	}

	const sessionCookie = event.cookies.get('session');
	if (!sessionCookie) {
		event.cookies.set('return_to', pathname, { path: '/', httpOnly: true, maxAge: 600 });
		throw redirect(302, '/api/auth/callback?action=login');
	}

	const signingKey = event.platform?.env.SESSION_SIGNING_KEY ?? 'dev-key';
	const user = parseSessionCookie(sessionCookie, signingKey);
	if (!user) {
		event.cookies.delete('session', { path: '/' });
		throw redirect(302, '/api/auth/callback?action=login');
	}

	event.locals.user = user;
	return resolve(event);
};
```

- [ ] **Step 5: Create OAuth callback**

```typescript
// sparkry-crm/src/routes/api/auth/callback/+server.ts
import { redirect, error } from '@sveltejs/kit';
import type { RequestHandler } from './$types';
import { getGoogleClient, isAllowedEmail, createSessionCookie } from '$lib/server/auth';
import { generateState, generateCodeVerifier } from 'arctic';

export const GET: RequestHandler = async ({ url, cookies, platform }) => {
	const env = platform!.env;
	const google = getGoogleClient(env.GOOGLE_CLIENT_ID, env.GOOGLE_CLIENT_SECRET, url.origin);

	const code = url.searchParams.get('code');
	const action = url.searchParams.get('action');

	if (action === 'login' || !code) {
		const state = generateState();
		const codeVerifier = generateCodeVerifier();
		cookies.set('oauth_state', state, { path: '/', httpOnly: true, maxAge: 600 });
		cookies.set('oauth_verifier', codeVerifier, { path: '/', httpOnly: true, maxAge: 600 });
		const authUrl = google.createAuthorizationURL(state, codeVerifier, ['openid', 'email', 'profile']);
		throw redirect(302, authUrl.toString());
	}

	const state = url.searchParams.get('state');
	const storedState = cookies.get('oauth_state');
	const codeVerifier = cookies.get('oauth_verifier');

	if (!state || !storedState || state !== storedState || !codeVerifier) {
		throw error(400, 'Invalid OAuth state');
	}

	const tokens = await google.validateAuthorizationCode(code, codeVerifier);
	const response = await fetch('https://openidconnect.googleapis.com/v1/userinfo', {
		headers: { Authorization: `Bearer ${tokens.accessToken()}` }
	});
	const userInfo = await response.json() as { email: string; name: string };

	if (!isAllowedEmail(userInfo.email, env.ALLOWED_EMAILS)) {
		throw error(403, 'Access denied. Your account is not authorized.');
	}

	const sessionCookie = createSessionCookie(userInfo.email, userInfo.name, env.SESSION_SIGNING_KEY);
	cookies.set('session', sessionCookie, { path: '/', httpOnly: true, secure: true, sameSite: 'lax', maxAge: 7 * 24 * 60 * 60 });
	cookies.delete('oauth_state', { path: '/' });
	cookies.delete('oauth_verifier', { path: '/' });

	const returnTo = cookies.get('return_to') || '/';
	cookies.delete('return_to', { path: '/' });
	throw redirect(302, returnTo);
};
```

- [ ] **Step 6: Create logout endpoint**

```typescript
// sparkry-crm/src/routes/api/auth/logout/+server.ts
import { redirect } from '@sveltejs/kit';
import type { RequestHandler } from './$types';

export const POST: RequestHandler = async ({ cookies }) => {
	cookies.delete('session', { path: '/' });
	throw redirect(302, '/api/auth/callback?action=login');
};
```

- [ ] **Step 7: Run tests**

```bash
pnpm vitest run tests/unit/auth.test.ts
```

Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add .
git commit -m "feat: Google OAuth authentication — session cookies, allowlist, hooks"
```

---

### Task 5: Base Layout & Navigation

**Files:**
- Create: `src/routes/+layout.server.ts`
- Create: `src/routes/+layout.svelte`
- Create: `src/lib/components/Nav.svelte`
- Create: `src/lib/components/Toast.svelte`

- [ ] **Step 1: Create layout server (pass user to client)**

```typescript
// sparkry-crm/src/routes/+layout.server.ts
import type { LayoutServerLoad } from './$types';

export const load: LayoutServerLoad = async ({ locals }) => {
	return { user: locals.user };
};
```

- [ ] **Step 2: Create Nav component**

```svelte
<!-- sparkry-crm/src/lib/components/Nav.svelte -->
<script lang="ts">
	import { page } from '$app/stores';

	let { user } = $props<{ user: { email: string; name: string } | null }>();

	const tabs = [
		{ href: '/', label: 'Dashboard' },
		{ href: '/customers', label: 'Customers' },
		{ href: '/work-orders', label: 'Projects' },
		{ href: '/invoices', label: 'Invoices' }
	];

	function isActive(href: string, pathname: string): boolean {
		if (href === '/') return pathname === '/';
		return pathname.startsWith(href);
	}

	function getInitials(name: string): string {
		return name.split(' ').map(n => n[0]).join('').toUpperCase().slice(0, 2);
	}
</script>

<nav class="nav">
	<div class="nav-inner">
		<div class="nav-left">
			<span class="brand">Sparkry</span>
			{#each tabs as tab}
				<a href={tab.href} class="tab" class:active={isActive(tab.href, $page.url.pathname)}>
					{tab.label}
				</a>
			{/each}
		</div>
		{#if user}
			<div class="nav-right">
				<form method="POST" action="/api/auth/logout">
					<button type="submit" class="avatar" title="Sign out">
						{getInitials(user.name)}
					</button>
				</form>
			</div>
		{/if}
	</div>
</nav>

<style>
	.nav { background: #1e293b; border-bottom: 1px solid #334155; padding: 0 24px; }
	.nav-inner { display: flex; align-items: center; justify-content: space-between; max-width: 800px; margin: 0 auto; height: 48px; }
	.nav-left { display: flex; align-items: center; gap: 24px; }
	.brand { font-weight: 700; font-size: 15px; color: white; }
	.tab { font-size: 13px; color: #94a3b8; text-decoration: none; padding: 14px 0; border-bottom: 2px solid transparent; }
	.tab:hover { color: #e2e8f0; }
	.tab.active { color: #93c5fd; border-bottom-color: #3b82f6; }
	.avatar { width: 28px; height: 28px; border-radius: 50%; background: #7c3aed; display: flex; align-items: center; justify-content: center; font-size: 11px; color: white; font-weight: 600; border: none; cursor: pointer; }
	.nav-right { display: flex; align-items: center; }

	@media (max-width: 640px) {
		.nav-left { gap: 16px; }
		.tab { font-size: 12px; }
	}
</style>
```

- [ ] **Step 3: Create Toast component**

```svelte
<!-- sparkry-crm/src/lib/components/Toast.svelte -->
<script lang="ts">
	import { onMount } from 'svelte';

	let { message, type = 'success', onDismiss } = $props<{
		message: string;
		type?: 'success' | 'error' | 'warning';
		onDismiss: () => void;
	}>();

	onMount(() => {
		const timer = setTimeout(onDismiss, 5000);
		return () => clearTimeout(timer);
	});
</script>

<div class="toast toast-{type}" role="alert">
	<span>{message}</span>
	<button onclick={onDismiss} aria-label="Dismiss">&times;</button>
</div>

<style>
	.toast { position: fixed; bottom: 20px; right: 20px; padding: 12px 16px; border-radius: 8px; display: flex; align-items: center; gap: 12px; font-size: 13px; z-index: 1000; animation: slideIn 0.2s ease; }
	.toast-success { background: rgba(74, 222, 128, 0.15); border: 1px solid rgba(74, 222, 128, 0.3); color: #4ade80; }
	.toast-error { background: rgba(248, 113, 113, 0.15); border: 1px solid rgba(248, 113, 113, 0.3); color: #f87171; }
	.toast-warning { background: rgba(251, 191, 36, 0.15); border: 1px solid rgba(251, 191, 36, 0.3); color: #fbbf24; }
	button { background: none; border: none; color: inherit; font-size: 18px; cursor: pointer; padding: 0; }
	@keyframes slideIn { from { transform: translateX(100%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }
</style>
```

- [ ] **Step 4: Create root layout**

```svelte
<!-- sparkry-crm/src/routes/+layout.svelte -->
<script lang="ts">
	import Nav from '$lib/components/Nav.svelte';
	import Toast from '$lib/components/Toast.svelte';

	let { data, children } = $props();
	let toast = $state<{ message: string; type: 'success' | 'error' | 'warning' } | null>(null);
</script>

<div class="app">
	<Nav user={data.user} />
	<main class="content">
		{@render children()}
	</main>
	{#if toast}
		<Toast message={toast.message} type={toast.type} onDismiss={() => toast = null} />
	{/if}
</div>

<style>
	:global(body) { margin: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f172a; color: #e2e8f0; }
	:global(*) { box-sizing: border-box; }
	.content { max-width: 800px; margin: 0 auto; padding: 24px; }
</style>
```

- [ ] **Step 5: Commit**

```bash
git add .
git commit -m "feat: app layout — top nav, toast notifications, dark theme, responsive shell"
```

---

### Task 6: Health Check & API Infrastructure

**Files:**
- Create: `src/routes/api/health/+server.ts`

- [ ] **Step 1: Create health endpoint**

```typescript
// sparkry-crm/src/routes/api/health/+server.ts
import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';
import { getDB } from '$lib/server/db';

export const GET: RequestHandler = async ({ platform }) => {
	try {
		const db = getDB(platform!.env.DB);
		await db.run(sql`SELECT 1`);
		return json({ status: 'ok', timestamp: new Date().toISOString() });
	} catch (e) {
		return json({ status: 'error', error: String(e) }, { status: 500 });
	}
};
```

- [ ] **Step 2: Commit**

```bash
git add .
git commit -m "feat: health check endpoint — D1 connectivity verification"
```

---

## Phase 2: Parallel Workstreams (after Phase 1 merge)

> **Execution note:** Create worktrees for Workstream A and Workstream B. Run them in parallel. Merge both back to `feat/crm-invoicing` when complete.

---

## Workstream A: Customers

### Task 7: Customer CRUD — Server Routes

**Files:**
- Create: `src/routes/customers/+page.server.ts`
- Create: `src/routes/customers/new/+page.server.ts`
- Create: `src/routes/customers/[id]/+page.server.ts`
- Create: `tests/integration/customers.test.ts`

- [ ] **Step 1: Write customer integration tests**

```typescript
// sparkry-crm/tests/integration/customers.test.ts
import { describe, it, expect, beforeEach } from 'vitest';
// Note: These test the server logic directly, mocking D1 via miniflare
// Full integration tests will use Playwright against the running app

import { createCustomer, listCustomers, updateCustomer, findDuplicates } from './helpers/customer-helpers';

describe('Customer CRUD', () => {
	it('creates a customer with all fields', async () => {
		const customer = await createCustomer({
			name: 'Xternal Source',
			contactName: 'Tiffany Broderson',
			contactEmail: 'tiffany@xternalsource.com',
			contactPhone: '480.382.3076',
			website: 'https://www.xternalsource.com',
			billingModel: 'project',
			defaultRate: 0,
			paymentTerms: 'Net 14',
			invoicePrefix: 'XS',
			paymentMethods: ['stripe_cc', 'ach'],
			ccFeePassthrough: true
		});
		expect(customer.id).toBeDefined();
		expect(customer.name).toBe('Xternal Source');
		expect(customer.active).toBe(true);
	});

	it('lists only active customers', async () => {
		await createCustomer({ name: 'Active Co', active: true });
		await createCustomer({ name: 'Inactive Co', active: false });
		const list = await listCustomers({ activeOnly: true });
		expect(list.every(c => c.active)).toBe(true);
	});

	it('detects duplicate customer names', async () => {
		await createCustomer({ name: 'Xternal Source' });
		const dupes = await findDuplicates('Xternal');
		expect(dupes.length).toBeGreaterThan(0);
		expect(dupes[0].name).toBe('Xternal Source');
	});

	it('detects fuzzy duplicates', async () => {
		await createCustomer({ name: 'Xternal Source LLC' });
		const dupes = await findDuplicates('Xternal Source');
		expect(dupes.length).toBeGreaterThan(0);
	});

	it('updates customer fields', async () => {
		const customer = await createCustomer({ name: 'Test Co' });
		const updated = await updateCustomer(customer.id, { contactEmail: 'new@test.com' });
		expect(updated.contactEmail).toBe('new@test.com');
		expect(updated.updatedAt).not.toBe(customer.updatedAt);
	});

	it('soft-deletes (deactivates) a customer', async () => {
		const customer = await createCustomer({ name: 'To Deactivate' });
		const updated = await updateCustomer(customer.id, { active: false });
		expect(updated.active).toBe(false);
	});

	it('validates email format', async () => {
		await expect(createCustomer({ name: 'Bad Email', contactEmail: 'not-an-email' }))
			.rejects.toThrow();
	});
});
```

- [ ] **Step 2: Implement customer list server route**

```typescript
// sparkry-crm/src/routes/customers/+page.server.ts
import type { PageServerLoad, Actions } from './$types';
import { getDB } from '$lib/server/db';
import { customers } from '$lib/server/db/schema';
import { eq, like, and } from 'drizzle-orm';

export const load: PageServerLoad = async ({ platform, url }) => {
	const db = getDB(platform!.env.DB);
	const search = url.searchParams.get('q') || '';

	let query = db.select().from(customers).where(eq(customers.active, true));
	if (search) {
		query = db.select().from(customers).where(
			and(eq(customers.active, true), like(customers.name, `%${search}%`))
		);
	}

	const results = await query.orderBy(customers.name);
	return { customers: results, search };
};
```

- [ ] **Step 3: Implement customer create server route**

```typescript
// sparkry-crm/src/routes/customers/new/+page.server.ts
import type { PageServerLoad, Actions } from './$types';
import { fail, redirect } from '@sveltejs/kit';
import { getDB } from '$lib/server/db';
import { customers, activityLog } from '$lib/server/db/schema';
import { like } from 'drizzle-orm';

export const load: PageServerLoad = async () => {
	return {};
};

export const actions: Actions = {
	create: async ({ request, platform, locals }) => {
		const db = getDB(platform!.env.DB);
		const form = await request.formData();

		const name = form.get('name') as string;
		const contactEmail = form.get('contactEmail') as string;

		if (!name?.trim()) return fail(400, { error: 'Name is required' });
		if (contactEmail && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(contactEmail)) {
			return fail(400, { error: 'Invalid email address' });
		}

		const id = crypto.randomUUID();
		await db.batch([
			db.insert(customers).values({
				id,
				name: name.trim(),
				contactName: form.get('contactName') as string || null,
				contactEmail: contactEmail || null,
				contactPhone: form.get('contactPhone') as string || null,
				website: form.get('website') as string || null,
				billingModel: (form.get('billingModel') as string) || 'project',
				defaultRate: parseInt(form.get('defaultRate') as string || '0'),
				paymentTerms: form.get('paymentTerms') as string || 'Net 14',
				invoicePrefix: form.get('invoicePrefix') as string || null,
				paymentMethods: JSON.parse(form.get('paymentMethods') as string || '[]'),
				ccFeePassthrough: form.get('ccFeePassthrough') === 'true',
				taxId: form.get('taxId') as string || null,
				notes: form.get('notes') as string || null
			}),
			db.insert(activityLog).values({
				id: crypto.randomUUID(),
				entityType: 'customer',
				entityId: id,
				action: 'created',
				userEmail: locals.user!.email
			})
		]);

		throw redirect(303, `/customers/${id}`);
	},

	checkDuplicate: async ({ request, platform }) => {
		const db = getDB(platform!.env.DB);
		const form = await request.formData();
		const name = form.get('name') as string;

		if (!name || name.length < 3) return { duplicates: [] };

		const results = await db.select({ id: customers.id, name: customers.name })
			.from(customers)
			.where(like(customers.name, `%${name}%`))
			.limit(5);

		return { duplicates: results };
	}
};
```

- [ ] **Step 4: Implement customer detail/edit server route**

```typescript
// sparkry-crm/src/routes/customers/[id]/+page.server.ts
import type { PageServerLoad, Actions } from './$types';
import { error, fail } from '@sveltejs/kit';
import { getDB } from '$lib/server/db';
import { customers, contacts, workOrders, activityLog } from '$lib/server/db/schema';
import { eq } from 'drizzle-orm';

export const load: PageServerLoad = async ({ params, platform }) => {
	const db = getDB(platform!.env.DB);

	const customer = await db.select().from(customers).where(eq(customers.id, params.id)).get();
	if (!customer) throw error(404, 'Customer not found');

	const customerContacts = await db.select().from(contacts).where(eq(contacts.customerId, params.id));
	const customerWOs = await db.select().from(workOrders).where(eq(workOrders.customerId, params.id));

	return { customer, contacts: customerContacts, workOrders: customerWOs };
};

export const actions: Actions = {
	update: async ({ params, request, platform, locals }) => {
		const db = getDB(platform!.env.DB);
		const form = await request.formData();
		const updatedAt = new Date().toISOString();

		const contactEmail = form.get('contactEmail') as string;
		if (contactEmail && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(contactEmail)) {
			return fail(400, { error: 'Invalid email address' });
		}

		await db.batch([
			db.update(customers).set({
				name: form.get('name') as string,
				contactName: form.get('contactName') as string || null,
				contactEmail: contactEmail || null,
				contactPhone: form.get('contactPhone') as string || null,
				website: form.get('website') as string || null,
				billingModel: form.get('billingModel') as string,
				defaultRate: parseInt(form.get('defaultRate') as string || '0'),
				paymentTerms: form.get('paymentTerms') as string,
				invoicePrefix: form.get('invoicePrefix') as string || null,
				ccFeePassthrough: form.get('ccFeePassthrough') === 'true',
				taxId: form.get('taxId') as string || null,
				notes: form.get('notes') as string || null,
				updatedAt
			}).where(eq(customers.id, params.id)),
			db.insert(activityLog).values({
				id: crypto.randomUUID(),
				entityType: 'customer',
				entityId: params.id,
				action: 'updated',
				userEmail: locals.user!.email
			})
		]);

		return { success: true };
	},

	addContact: async ({ params, request, platform }) => {
		const db = getDB(platform!.env.DB);
		const form = await request.formData();

		await db.insert(contacts).values({
			id: crypto.randomUUID(),
			customerId: params.id,
			name: form.get('name') as string,
			email: form.get('email') as string || null,
			phone: form.get('phone') as string || null,
			role: form.get('role') as string || 'primary',
			isDefault: form.get('isDefault') === 'true'
		});

		return { success: true };
	},

	deactivate: async ({ params, platform, locals }) => {
		const db = getDB(platform!.env.DB);
		await db.batch([
			db.update(customers).set({ active: false, updatedAt: new Date().toISOString() }).where(eq(customers.id, params.id)),
			db.insert(activityLog).values({
				id: crypto.randomUUID(),
				entityType: 'customer',
				entityId: params.id,
				action: 'deactivated',
				userEmail: locals.user!.email
			})
		]);
		return { success: true };
	}
};
```

- [ ] **Step 5: Commit**

```bash
git add .
git commit -m "feat: customer CRUD server routes — list, create, update, contacts, duplicate detection"
```

---

### Task 8: Customer UI Pages

**Files:**
- Create: `src/routes/customers/+page.svelte`
- Create: `src/routes/customers/new/+page.svelte`
- Create: `src/routes/customers/[id]/+page.svelte`
- Create: `src/lib/components/EmptyState.svelte`

- [ ] **Step 1: Create EmptyState component**

```svelte
<!-- sparkry-crm/src/lib/components/EmptyState.svelte -->
<script lang="ts">
	let { title, description, actionLabel, actionHref } = $props<{
		title: string;
		description: string;
		actionLabel?: string;
		actionHref?: string;
	}>();
</script>

<div class="empty">
	<h3>{title}</h3>
	<p>{description}</p>
	{#if actionLabel && actionHref}
		<a href={actionHref} class="action">{actionLabel}</a>
	{/if}
</div>

<style>
	.empty { text-align: center; padding: 48px 24px; }
	h3 { color: #f1f5f9; margin: 0 0 8px; font-size: 16px; }
	p { color: #94a3b8; margin: 0 0 16px; font-size: 13px; max-width: 400px; margin-left: auto; margin-right: auto; }
	.action { display: inline-block; background: #3b82f6; color: white; padding: 8px 16px; border-radius: 6px; text-decoration: none; font-size: 13px; }
	.action:hover { background: #2563eb; }
</style>
```

- [ ] **Step 2: Create customer list page**

```svelte
<!-- sparkry-crm/src/routes/customers/+page.svelte -->
<script lang="ts">
	import EmptyState from '$lib/components/EmptyState.svelte';
	let { data } = $props();
	let search = $state(data.search || '');
</script>

<svelte:head><title>Customers — Sparkry</title></svelte:head>

<div class="header">
	<h1>Customers</h1>
	<a href="/customers/new" class="btn-primary">+ Add Customer</a>
</div>

<form method="GET" class="search-form">
	<input type="text" name="q" bind:value={search} placeholder="Search customers..." class="search-input" />
</form>

{#if data.customers.length === 0 && !data.search}
	<EmptyState
		title="No customers yet"
		description="Add your first customer to start creating projects and invoices."
		actionLabel="Add Your First Customer"
		actionHref="/customers/new"
	/>
{:else if data.customers.length === 0}
	<p class="no-results">No customers matching "{data.search}"</p>
{:else}
	<div class="list">
		{#each data.customers as customer}
			<a href="/customers/{customer.id}" class="row">
				<div class="row-main">
					<span class="name">{customer.name}</span>
					<span class="contact">{customer.contactName || ''}</span>
				</div>
				<span class="meta">{customer.billingModel}</span>
			</a>
		{/each}
	</div>
{/if}

<style>
	.header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
	h1 { font-size: 20px; color: #f1f5f9; margin: 0; }
	.btn-primary { background: #3b82f6; color: white; padding: 8px 14px; border-radius: 6px; text-decoration: none; font-size: 13px; }
	.search-form { margin-bottom: 16px; }
	.search-input { width: 100%; padding: 10px 14px; background: #1e293b; border: 1px solid #334155; border-radius: 8px; color: #e2e8f0; font-size: 13px; }
	.search-input::placeholder { color: #64748b; }
	.list { background: #1e293b; border-radius: 10px; border: 1px solid #334155; overflow: hidden; }
	.row { display: flex; justify-content: space-between; align-items: center; padding: 12px 16px; border-bottom: 1px solid #334155; text-decoration: none; color: inherit; }
	.row:last-child { border-bottom: none; }
	.row:hover { background: rgba(59, 130, 246, 0.05); }
	.name { color: #e2e8f0; font-size: 14px; font-weight: 500; }
	.contact { color: #94a3b8; font-size: 12px; margin-left: 12px; }
	.meta { color: #64748b; font-size: 12px; }
	.no-results { color: #94a3b8; text-align: center; padding: 32px; }
</style>
```

- [ ] **Step 3: Create new customer form page**

```svelte
<!-- sparkry-crm/src/routes/customers/new/+page.svelte -->
<script lang="ts">
	import { enhance } from '$app/forms';
	import { PAYMENT_TERMS_OPTIONS } from '$lib/types';

	let { form } = $props();
	let duplicates = $state<Array<{id: string; name: string}>>([]);
	let name = $state('');
</script>

<svelte:head><title>New Customer — Sparkry</title></svelte:head>

<div class="header">
	<a href="/customers" class="back">← Customers</a>
	<h1>Add Customer</h1>
</div>

{#if form?.error}
	<div class="error-banner">{form.error}</div>
{/if}

{#if duplicates.length > 0}
	<div class="warning-banner">
		A similar customer already exists: {duplicates.map(d => d.name).join(', ')}.
		<a href="/customers/{duplicates[0].id}">Did you mean to edit them?</a>
	</div>
{/if}

<form method="POST" action="?/create" use:enhance class="form">
	<div class="field">
		<label for="name">Company Name *</label>
		<input id="name" name="name" type="text" required bind:value={name}
			onblur={async () => {
				if (name.length >= 3) {
					const res = await fetch(`?/checkDuplicate`, { method: 'POST', body: new FormData(document.querySelector('form')!) });
					const data = await res.json();
					duplicates = data?.duplicates || [];
				}
			}} />
	</div>

	<div class="row">
		<div class="field">
			<label for="contactName">Contact Name</label>
			<input id="contactName" name="contactName" type="text" />
		</div>
		<div class="field">
			<label for="contactEmail">Contact Email</label>
			<input id="contactEmail" name="contactEmail" type="email" />
		</div>
	</div>

	<div class="row">
		<div class="field">
			<label for="contactPhone">Phone</label>
			<input id="contactPhone" name="contactPhone" type="tel" />
		</div>
		<div class="field">
			<label for="website">Website</label>
			<input id="website" name="website" type="url" />
		</div>
	</div>

	<div class="row">
		<div class="field">
			<label for="billingModel">Billing Model</label>
			<select id="billingModel" name="billingModel">
				<option value="project">Project — one-time work with milestones</option>
				<option value="hourly">Hourly — bill by the hour</option>
				<option value="flat_rate">Flat rate — same amount each month</option>
			</select>
		</div>
		<div class="field">
			<label for="paymentTerms">Payment Terms</label>
			<select id="paymentTerms" name="paymentTerms">
				{#each PAYMENT_TERMS_OPTIONS as opt}
					<option value={opt.value}>{opt.label}</option>
				{/each}
			</select>
		</div>
	</div>

	<div class="row">
		<div class="field">
			<label for="invoicePrefix">Invoice Prefix</label>
			<input id="invoicePrefix" name="invoicePrefix" type="text" maxlength="5" placeholder="e.g. XS" />
		</div>
		<div class="field">
			<label for="defaultRate">Default Rate (cents)</label>
			<input id="defaultRate" name="defaultRate" type="number" value="0" />
		</div>
	</div>

	<div class="field">
		<label class="checkbox-label">
			<input type="hidden" name="ccFeePassthrough" value="false" />
			<input type="checkbox" name="ccFeePassthrough" value="true" checked />
			Add credit card processing fee to invoices over $1,000 (3.5%) — the client pays the fee instead of us absorbing it
		</label>
	</div>

	<div class="field">
		<label for="notes">Notes</label>
		<textarea id="notes" name="notes" rows="3"></textarea>
	</div>

	<div class="actions">
		<a href="/customers" class="btn-secondary">Cancel</a>
		<button type="submit" class="btn-primary">Create Customer</button>
	</div>
</form>

<style>
	.header { margin-bottom: 20px; }
	.back { color: #64748b; text-decoration: none; font-size: 12px; }
	h1 { font-size: 20px; color: #f1f5f9; margin: 8px 0 0; }
	.form { background: #1e293b; border-radius: 10px; border: 1px solid #334155; padding: 20px; }
	.field { margin-bottom: 16px; flex: 1; }
	.row { display: flex; gap: 16px; }
	label { display: block; font-size: 12px; color: #94a3b8; margin-bottom: 4px; }
	input, select, textarea { width: 100%; padding: 8px 12px; background: #0f172a; border: 1px solid #334155; border-radius: 6px; color: #e2e8f0; font-size: 13px; }
	.checkbox-label { display: flex; align-items: flex-start; gap: 8px; font-size: 12px; color: #94a3b8; cursor: pointer; }
	.checkbox-label input[type="checkbox"] { width: auto; margin-top: 2px; }
	.actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 20px; }
	.btn-primary { background: #3b82f6; color: white; padding: 8px 16px; border-radius: 6px; border: none; font-size: 13px; cursor: pointer; }
	.btn-secondary { background: transparent; border: 1px solid #334155; color: #94a3b8; padding: 8px 16px; border-radius: 6px; text-decoration: none; font-size: 13px; }
	.error-banner { background: rgba(248,113,113,0.1); border: 1px solid rgba(248,113,113,0.3); padding: 10px 14px; border-radius: 8px; color: #f87171; font-size: 13px; margin-bottom: 16px; }
	.warning-banner { background: rgba(251,191,36,0.1); border: 1px solid rgba(251,191,36,0.3); padding: 10px 14px; border-radius: 8px; color: #fbbf24; font-size: 13px; margin-bottom: 16px; }
	.warning-banner a { color: #93c5fd; }

	@media (max-width: 640px) { .row { flex-direction: column; gap: 0; } }
</style>
```

- [ ] **Step 4: Create customer detail/edit page**

(Similar structure to new page but with pre-filled values, edit mode toggle, contacts list, and deactivate action. Follow the same patterns as the new page — form fields pre-populated from `data.customer`, contacts section with add form, deactivate button with confirmation.)

- [ ] **Step 5: Run dev server and verify pages render**

```bash
cd /Users/travis/SGDrive/dev/sparkry-crm
pnpm dev
# Visit http://localhost:5173/customers
```

- [ ] **Step 6: Commit**

```bash
git add .
git commit -m "feat: customer UI — list with search, create form with dupe detection, detail/edit page"
```

---

## Workstream B: Work Orders + Milestones

### Task 9: Work Order CRUD — Server Routes

**Files:**
- Create: `src/routes/work-orders/+page.server.ts`
- Create: `src/routes/work-orders/new/+page.server.ts`
- Create: `src/routes/work-orders/[id]/+page.server.ts`
- Create: `tests/integration/work-orders.test.ts`

(Same TDD pattern as Task 7 — write integration tests first, then implement server routes for list/create/detail/milestone-management. Key behaviors: WO total vs milestone sum validation, milestone locking after invoiced, cancellation policy with auto-void, "Mark Complete" action.)

### Task 10: Work Order UI Pages

**Files:**
- Create: `src/routes/work-orders/+page.svelte`
- Create: `src/routes/work-orders/new/+page.svelte`
- Create: `src/routes/work-orders/[id]/+page.svelte`
- Create: `src/lib/components/StatusBadge.svelte`
- Create: `src/lib/components/ConfirmDialog.svelte`

(WO list with empty state, create form with existing-WO-for-customer warning, detail page with milestone timeline — green/amber/grey indicators, progress bar, Mark Complete with confirmation dialog, Create Invoice button.)

---

## Phase 3: Parallel Workstreams (after Phase 2 merge)

## Workstream C: Invoicing + Payments

### Task 11: Invoice CRUD — Server Routes

**Files:**
- Create: `src/routes/invoices/+page.server.ts`
- Create: `src/routes/invoices/[id]/+page.server.ts`
- Create: `tests/integration/invoices.test.ts`

(Invoice list with filters, invoice detail with line item editing, status transitions with state machine validation, invoice number generation, optimistic locking.)

### Task 12: Send Pipeline

**Files:**
- Create: `src/lib/server/invoice-pipeline.ts`
- Create: `src/lib/server/stripe.ts`
- Create: `src/lib/server/email.ts`
- Create: `tests/unit/invoice-pipeline.test.ts`

- [ ] **Step 1: Write pipeline tests**

```typescript
// sparkry-crm/tests/unit/invoice-pipeline.test.ts
import { describe, it, expect, vi } from 'vitest';
import { validateInvoiceForSend, computeInvoiceTotal, PIPELINE_STEPS } from '$lib/server/invoice-pipeline';

describe('Invoice Send Pipeline', () => {
	describe('validateInvoiceForSend', () => {
		it('rejects non-draft invoices', () => {
			const result = validateInvoiceForSend({ status: 'sent', lineItems: [{}], sentTo: 'a@b.com' });
			expect(result.valid).toBe(false);
			expect(result.error).toContain('draft');
		});

		it('rejects invoices with no line items', () => {
			const result = validateInvoiceForSend({ status: 'draft', lineItems: [], sentTo: 'a@b.com' });
			expect(result.valid).toBe(false);
			expect(result.error).toContain('line items');
		});

		it('rejects invoices with no recipient', () => {
			const result = validateInvoiceForSend({ status: 'draft', lineItems: [{}], sentTo: '' });
			expect(result.valid).toBe(false);
			expect(result.error).toContain('recipient');
		});

		it('accepts valid draft invoices', () => {
			const result = validateInvoiceForSend({ status: 'draft', lineItems: [{}], sentTo: 'a@b.com' });
			expect(result.valid).toBe(true);
		});
	});

	describe('computeInvoiceTotal', () => {
		it('sums line items correctly', () => {
			const lineItems = [
				{ quantity: 10000, unitPrice: 200000, totalPrice: 200000 },
				{ quantity: 20000, unitPrice: 5000, totalPrice: 10000 }
			];
			const result = computeInvoiceTotal(lineItems, 'stripe_cc');
			expect(result.subtotal).toBe(210000);
			expect(result.ccFee).toBe(7350); // 210000 * 0.035
			expect(result.total).toBe(217350);
		});

		it('does not add CC fee for ACH', () => {
			const lineItems = [{ quantity: 10000, unitPrice: 200000, totalPrice: 200000 }];
			const result = computeInvoiceTotal(lineItems, 'ach');
			expect(result.subtotal).toBe(200000);
			expect(result.ccFee).toBe(0);
			expect(result.total).toBe(200000);
		});

		it('does not add CC fee under $1000', () => {
			const lineItems = [{ quantity: 10000, unitPrice: 50000, totalPrice: 50000 }];
			const result = computeInvoiceTotal(lineItems, 'stripe_cc');
			expect(result.ccFee).toBe(0);
			expect(result.total).toBe(50000);
		});
	});

	it('pipeline steps are in correct order', () => {
		expect(PIPELINE_STEPS).toEqual([
			'validate',
			'compute_total',
			'create_payment_link',
			'schedule_email',
			'persist_and_transition'
		]);
	});
});
```

- [ ] **Step 2: Implement pipeline**

```typescript
// sparkry-crm/src/lib/server/invoice-pipeline.ts
import { calculateCcFee } from '$lib/utils';
import type { PaymentMethod } from '$lib/types';

export const PIPELINE_STEPS = [
	'validate',
	'compute_total',
	'create_payment_link',
	'schedule_email',
	'persist_and_transition'
] as const;

interface LineItem {
	quantity: number;
	unitPrice: number;
	totalPrice: number;
}

export function validateInvoiceForSend(invoice: { status: string; lineItems: any[]; sentTo: string }) {
	if (invoice.status !== 'draft') return { valid: false, error: 'Invoice must be in draft status to send' };
	if (!invoice.lineItems || invoice.lineItems.length === 0) return { valid: false, error: 'Invoice must have at least one line item' };
	if (!invoice.sentTo || !invoice.sentTo.trim()) return { valid: false, error: 'Invoice must have a recipient email' };
	return { valid: true, error: null };
}

export function computeInvoiceTotal(lineItems: LineItem[], paymentMethod: PaymentMethod | null) {
	const subtotal = lineItems.reduce((sum, item) => sum + item.totalPrice, 0);
	const ccFee = calculateCcFee(subtotal, paymentMethod);
	return { subtotal, ccFee, total: subtotal + ccFee };
}
```

- [ ] **Step 3: Implement Stripe helpers**

```typescript
// sparkry-crm/src/lib/server/stripe.ts
import Stripe from 'stripe';

export function getStripe(secretKey: string) {
	return new Stripe(secretKey, { apiVersion: '2024-12-18.acacia', httpClient: Stripe.createFetchHttpClient() });
}

export async function createPaymentLink(stripe: Stripe, invoice: { id: string; invoiceNumber: string; total: number; customerId: string; paymentMethod: string }): Promise<{ url: string; id: string }> {
	const paymentMethodTypes: Stripe.PaymentLinkCreateParams.PaymentMethodType[] =
		invoice.paymentMethod === 'ach' ? ['us_bank_account'] :
		invoice.paymentMethod === 'venmo' ? ['venmo'] :
		['card'];

	const product = await stripe.products.create({
		name: `Invoice ${invoice.invoiceNumber}`,
		metadata: { invoice_id: invoice.id, customer_id: invoice.customerId }
	});

	const price = await stripe.prices.create({
		product: product.id,
		unit_amount: invoice.total,
		currency: 'usd'
	});

	const link = await stripe.paymentLinks.create({
		line_items: [{ price: price.id, quantity: 1 }],
		payment_method_types: paymentMethodTypes,
		restrictions: { completed_sessions: { limit: 1 } },
		metadata: { invoice_id: invoice.id }
	});

	return { url: link.url, id: link.id };
}

export function verifyWebhookSignature(stripe: Stripe, body: string, signature: string, secret: string): Stripe.Event {
	return stripe.webhooks.constructEvent(body, signature, secret);
}
```

- [ ] **Step 4: Implement email helpers**

```typescript
// sparkry-crm/src/lib/server/email.ts
import { Resend } from 'resend';

export function getResend(apiKey: string) {
	return new Resend(apiKey);
}

interface InvoiceEmailParams {
	to: string;
	invoiceNumber: string;
	customerName: string;
	total: number;
	dueDate: string;
	paymentLinkUrl?: string;
	paymentMethod: string;
}

export async function sendInvoiceEmail(resend: Resend, params: InvoiceEmailParams): Promise<{ id: string }> {
	const { to, invoiceNumber, customerName, total, dueDate, paymentLinkUrl, paymentMethod } = params;
	const formattedTotal = (total / 100).toLocaleString('en-US', { style: 'currency', currency: 'USD' });
	const scheduledAt = new Date(Date.now() + 30_000).toISOString();

	const paymentSection = paymentMethod === 'check'
		? `<p>Please make checks payable to <strong>Sparkry AI LLC</strong> and mail to our business address.</p>`
		: paymentLinkUrl
			? `<p><a href="${paymentLinkUrl}" style="display:inline-block;background:#3b82f6;color:white;padding:12px 24px;border-radius:6px;text-decoration:none;font-weight:500;">Pay ${formattedTotal}</a></p>`
			: '';

	const result = await resend.emails.send({
		from: 'Sparkry AI LLC <invoices@sparkry.ai>',
		to,
		subject: `Invoice ${invoiceNumber} — ${formattedTotal}`,
		scheduledAt,
		html: `
			<div style="font-family:-apple-system,sans-serif;max-width:600px;margin:0 auto;">
				<h2 style="color:#1e293b;">Invoice ${invoiceNumber}</h2>
				<p>Hi,</p>
				<p>Please find attached invoice <strong>${invoiceNumber}</strong> for <strong>${formattedTotal}</strong>, due <strong>${dueDate}</strong>.</p>
				${paymentSection}
				<p style="color:#64748b;font-size:13px;margin-top:24px;">Thank you for your business.<br/>Sparkry AI LLC</p>
			</div>
		`
	});
	return { id: result.data?.id ?? '' };
}

export async function cancelScheduledEmail(resend: Resend, emailId: string): Promise<boolean> {
	try {
		await resend.emails.cancel(emailId);
		return true;
	} catch {
		return false;
	}
}

export async function sendAlertEmail(resend: Resend, params: { to: string[]; subject: string; body: string; ctaUrl?: string; ctaLabel?: string }) {
	return resend.emails.send({
		from: 'Sparkry CRM <alerts@sparkry.ai>',
		to: params.to,
		subject: params.subject,
		html: `
			<div style="font-family:-apple-system,sans-serif;max-width:600px;margin:0 auto;">
				<p>${params.body}</p>
				${params.ctaUrl ? `<p><a href="${params.ctaUrl}" style="display:inline-block;background:#3b82f6;color:white;padding:10px 20px;border-radius:6px;text-decoration:none;">${params.ctaLabel || 'View'}</a></p>` : ''}
			</div>
		`
	});
}
```

- [ ] **Step 5: Run tests**

```bash
pnpm vitest run tests/unit/invoice-pipeline.test.ts
```

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add .
git commit -m "feat: invoice send pipeline — validation, total computation, Stripe links, Resend email"
```

### Task 13: Stripe Webhook Handler

**Files:**
- Create: `src/routes/api/webhooks/stripe/+server.ts`
- Create: `tests/unit/stripe.test.ts`

(Signature verification, idempotency check on stripe_event_id, D1 batch update invoice+milestone, send payment confirmation email.)

### Task 14: Invoice UI Pages

**Files:**
- Create: `src/routes/invoices/+page.svelte`
- Create: `src/routes/invoices/[id]/+page.svelte`
- Create: `src/routes/invoices/[id]/print/+page.svelte`
- Create: `src/routes/invoices/[id]/print/+page.server.ts`

(Invoice list with status filters, invoice detail with line item table, payment method selector, Review & Send confirmation, undo-send window, printable HTML invoice.)

---

## Workstream D: Notifications

### Task 15: Cron Handler

**Files:**
- Create: `src/worker.ts`
- Create: `src/lib/server/cron.ts`
- Create: `tests/unit/cron.test.ts`

- [ ] **Step 1: Write cron logic tests**

```typescript
// sparkry-crm/tests/unit/cron.test.ts
import { describe, it, expect } from 'vitest';
import { findDueMilestones, findOverdueInvoices, shouldSendReminder } from '$lib/server/cron';

describe('Cron Logic', () => {
	it('finds milestones due today or earlier that are still pending', () => {
		const milestones = [
			{ id: '1', dueDate: '2026-04-26', status: 'pending', triggerType: 'date', notifiedAt: null },
			{ id: '2', dueDate: '2026-04-25', status: 'pending', triggerType: 'both', notifiedAt: null },
			{ id: '3', dueDate: '2026-04-27', status: 'pending', triggerType: 'date', notifiedAt: null },
			{ id: '4', dueDate: '2026-04-26', status: 'ready', triggerType: 'date', notifiedAt: null },
			{ id: '5', dueDate: '2026-04-26', status: 'pending', triggerType: 'manual', notifiedAt: null },
			{ id: '6', dueDate: '2026-04-26', status: 'pending', triggerType: 'date', notifiedAt: '2026-04-26T08:00:00Z' },
		];
		const due = findDueMilestones(milestones, '2026-04-26');
		expect(due.map(m => m.id)).toEqual(['1', '2']);
	});

	it('determines reminder schedule (3, 7, 14 days)', () => {
		expect(shouldSendReminder(3)).toBe(true);
		expect(shouldSendReminder(7)).toBe(true);
		expect(shouldSendReminder(14)).toBe(true);
		expect(shouldSendReminder(5)).toBe(false);
		expect(shouldSendReminder(1)).toBe(false);
	});
});
```

- [ ] **Step 2: Implement cron logic**

```typescript
// sparkry-crm/src/lib/server/cron.ts
interface MilestoneRow {
	id: string;
	dueDate: string | null;
	status: string;
	triggerType: string;
	notifiedAt: string | null;
}

export function findDueMilestones(milestones: MilestoneRow[], today: string): MilestoneRow[] {
	return milestones.filter(m =>
		m.status === 'pending' &&
		m.dueDate !== null &&
		m.dueDate <= today &&
		m.triggerType !== 'manual' &&
		m.notifiedAt === null
	);
}

export function findOverdueInvoices(invoices: Array<{ status: string; dueDate: string | null }>, today: string) {
	return invoices.filter(inv =>
		inv.status === 'sent' &&
		inv.dueDate !== null &&
		inv.dueDate < today
	);
}

export function shouldSendReminder(daysOverdue: number): boolean {
	return [3, 7, 14].includes(daysOverdue);
}

export function daysBetween(dateA: string, dateB: string): number {
	const a = new Date(dateA + 'T00:00:00');
	const b = new Date(dateB + 'T00:00:00');
	return Math.floor((b.getTime() - a.getTime()) / (86400000));
}
```

- [ ] **Step 3: Implement Workers scheduled handler**

```typescript
// sparkry-crm/src/worker.ts
import { getDB } from '$lib/server/db';
import { milestones, invoices, invoiceLineItems, customers, workOrders, activityLog } from '$lib/server/db/schema';
import { eq, and, lte, isNull, like, desc } from 'drizzle-orm';
import { findDueMilestones, findOverdueInvoices, shouldSendReminder, daysBetween } from '$lib/server/cron';
import { sendAlertEmail, getResend } from '$lib/server/email';
import { generateInvoiceNumber } from '$lib/utils';

export default {
	async scheduled(event: ScheduledEvent, env: any) {
		const db = getDB(env.DB);
		const resend = getResend(env.RESEND_API_KEY);
		const alertRecipients = env.ALLOWED_EMAILS.split(',').map((e: string) => e.trim());
		const today = new Date().toISOString().split('T')[0];

		try {
			// 1. Process due milestones
			const allMilestones = await db.select().from(milestones)
				.where(and(eq(milestones.status, 'pending'), lte(milestones.dueDate, today), isNull(milestones.notifiedAt)));

			for (const milestone of allMilestones) {
				if (milestone.triggerType === 'manual') continue;

				const customer = await db.select().from(customers)
					.innerJoin(workOrders, eq(workOrders.customerId, customers.id))
					.where(eq(workOrders.id, milestone.workOrderId))
					.get();

				if (!customer) continue;

				const prefix = customer.customers.invoicePrefix || 'INV';
				const existing = await db.select({ invoiceNumber: invoices.invoiceNumber })
					.from(invoices).where(like(invoices.invoiceNumber, `${prefix}-%`))
					.orderBy(desc(invoices.invoiceNumber)).limit(1).get();

				const invoiceNumber = generateInvoiceNumber(prefix, existing?.invoiceNumber || null);
				const invoiceId = crypto.randomUUID();
				const now = new Date().toISOString();

				await db.batch([
					db.insert(invoices).values({
						id: invoiceId,
						invoiceNumber,
						customerId: customer.customers.id,
						workOrderId: milestone.workOrderId,
						milestoneId: milestone.id,
						status: 'draft',
						subtotal: milestone.amount,
						total: milestone.amount,
						paymentTerms: customer.customers.paymentTerms || 'Net 14',
						submittedDate: today
					}),
					db.insert(invoiceLineItems).values({
						id: crypto.randomUUID(),
						invoiceId,
						description: milestone.title,
						quantity: 10000,
						unitPrice: milestone.amount,
						totalPrice: milestone.amount,
						sortOrder: 0
					}),
					db.update(milestones).set({ status: 'ready', invoiceId, notifiedAt: now, updatedAt: now }).where(eq(milestones.id, milestone.id)),
					db.insert(activityLog).values({
						id: crypto.randomUUID(),
						entityType: 'milestone',
						entityId: milestone.id,
						action: 'status_changed',
						userEmail: 'system',
						oldValue: 'pending',
						newValue: 'ready'
					})
				]);

				await sendAlertEmail(resend, {
					to: alertRecipients,
					subject: `Invoice ready: ${customer.customers.name} — ${milestone.title} ($${(milestone.amount / 100).toFixed(2)})`,
					body: `A milestone is due for <strong>${customer.customers.name}</strong>: ${milestone.title} — $${(milestone.amount / 100).toFixed(2)}`,
					ctaUrl: `https://internal.sparkry.ai/invoices/${invoiceId}`,
					ctaLabel: 'Review & Send'
				});
			}

			// 2. Mark overdue invoices
			const sentInvoices = await db.select().from(invoices).where(eq(invoices.status, 'sent'));
			for (const inv of sentInvoices) {
				if (inv.dueDate && inv.dueDate < today) {
					await db.update(invoices).set({ status: 'overdue', updatedAt: new Date().toISOString() }).where(eq(invoices.id, inv.id));
				}
			}

			// 3. Send payment reminders for overdue invoices
			const overdueInvoices = await db.select().from(invoices).where(eq(invoices.status, 'overdue'));
			for (const inv of overdueInvoices) {
				if (!inv.dueDate || !inv.sentTo) continue;
				const daysOver = daysBetween(inv.dueDate, today);
				if (shouldSendReminder(daysOver)) {
					await sendAlertEmail(resend, {
						to: [inv.sentTo],
						subject: `Reminder: Invoice ${inv.invoiceNumber} is ${daysOver} days past due`,
						body: `Invoice ${inv.invoiceNumber} for $${(inv.total / 100).toFixed(2)} was due on ${inv.dueDate}. Please remit payment at your earliest convenience.`,
						ctaUrl: inv.paymentLinkUrl || undefined,
						ctaLabel: 'Pay Now'
					});
				}
			}

			// Log success
			await db.insert(activityLog).values({
				id: crypto.randomUUID(),
				entityType: 'invoice',
				entityId: 'cron',
				action: 'cron_completed',
				userEmail: 'system',
				metadata: JSON.stringify({ milestonesProcessed: allMilestones.length, date: today })
			});
		} catch (error) {
			// Alert on failure
			try {
				await sendAlertEmail(resend, {
					to: ['travis@sparkry.com'],
					subject: 'CRM Cron FAILED',
					body: `Daily milestone check failed: ${String(error)}`
				});
			} catch {}
			throw error;
		}
	}
};
```

- [ ] **Step 4: Run tests**

```bash
pnpm vitest run tests/unit/cron.test.ts
```

- [ ] **Step 5: Commit**

```bash
git add .
git commit -m "feat: cron handler — daily milestone check, overdue detection, payment reminders"
```

### Task 16: Resend Bounce Webhook

**Files:**
- Create: `src/routes/api/webhooks/resend/+server.ts`

(Handle delivery status updates from Resend. Update invoice delivery_status field.)

---

## Phase 4: Dashboard + Ops

## Workstream E: Dashboard

### Task 17: Dashboard Page

**Files:**
- Create: `src/routes/+page.server.ts`
- Create: `src/routes/+page.svelte`

(Dashboard data loader: count ready-to-send invoices, paid this month total, outstanding total, coming-up milestones. Empty state with 3-step guide. Normal state with action cards + coming up section.)

## Workstream F: Ops

### Task 18: Sentry + Error Monitoring

**Files:**
- Modify: `src/hooks.server.ts`

(Add Sentry init, capture exceptions.)

### Task 19: Cloudflare Deployment Configuration

**Files:**
- Modify: `wrangler.toml`

- [ ] **Step 1: Create D1 databases**

```bash
wrangler d1 create sparkry-crm-prod
wrangler d1 create sparkry-crm-staging
```

- [ ] **Step 2: Update wrangler.toml with database IDs**

Fill in the `database_id` values from step 1.

- [ ] **Step 3: Apply migrations**

```bash
wrangler d1 migrations apply sparkry-crm-prod --remote
wrangler d1 migrations apply sparkry-crm-staging --remote
```

- [ ] **Step 4: Configure secrets**

```bash
wrangler secret put GOOGLE_CLIENT_ID
wrangler secret put GOOGLE_CLIENT_SECRET
wrangler secret put STRIPE_SECRET_KEY
wrangler secret put STRIPE_WEBHOOK_SECRET
wrangler secret put RESEND_API_KEY
wrangler secret put ALLOWED_EMAILS
wrangler secret put SESSION_SIGNING_KEY
wrangler secret put SENTRY_DSN
```

- [ ] **Step 5: Configure custom domain**

```bash
# In Cloudflare dashboard: Pages > sparkry-crm > Custom domains > Add internal.sparkry.ai
# Or via wrangler if supported
```

- [ ] **Step 6: Deploy**

```bash
pnpm build
wrangler pages deploy .svelte-kit/cloudflare
```

- [ ] **Step 7: Verify health check**

```bash
curl https://internal.sparkry.ai/api/health
```

Expected: `{"status":"ok","timestamp":"..."}`

- [ ] **Step 8: Commit**

```bash
git add .
git commit -m "feat: Cloudflare deployment — D1 databases, secrets, custom domain"
```

---

## Phase 5: Integration Testing & Deploy

### Task 20: End-to-End Smoke Test

- [ ] **Step 1: Verify auth flow** — visit internal.sparkry.ai, redirects to Google, sign in, lands on dashboard
- [ ] **Step 2: Create customer** — add Xternal Source via UI with all fields
- [ ] **Step 3: Create work order** — $8,000 project with 3 milestones
- [ ] **Step 4: Mark milestone complete** — triggers draft invoice creation + email alert
- [ ] **Step 5: Review and send invoice** — verify line items, select payment method, confirm send
- [ ] **Step 6: Verify email received** — check Resend dashboard for delivery
- [ ] **Step 7: Verify Stripe payment link** — click link, verify amount is correct
- [ ] **Step 8: Test webhook** — use Stripe CLI to trigger test payment event
- [ ] **Step 9: Verify payment recorded** — invoice status updates to paid

### Task 21: Create PR

```bash
git checkout feat/crm-invoicing
git push -u origin feat/crm-invoicing
gh pr create --title "feat: CRM + Work Order Invoicing system" --body "..."
```

---

## Review Cycle Process

After each workstream completes:

1. Run `qcheckt` — review test quality against requirements
2. Run `qcheck` — comprehensive skeptical review
3. Run `qcheckf` — focused review of functional code
4. Fix all P0, P1, P2 findings
5. Re-run reviews until no P0 or P1 items remain
6. Merge workstream branch

---

## Design Decisions

- **Undo-send: Resend scheduled emails.** Send with `scheduledAt: now + 30s`, store `resend_email_id`, cancel via `POST /emails/{id}/cancel` if Undo clicked within 30s. No Durable Objects or cron needed.
- **Auth signing: Use HMAC-SHA256 from start** via Web Crypto API (`crypto.subtle`). Make session functions async.
- **Fuzzy duplicate detection: LIKE is acceptable for MVP.** True fuzzy matching deferred.

## Additional Tasks (not fully detailed — agents use spec as context)

- **Task 22: Settings page** — company details (EIN, address), T&C template, notification prefs. CRUD against `settings` table.
- **Task 23: Data migration** — one-time script: local SQLite → D1. Transform customers + invoices, generate WO stubs for Fascinate/Cardinal. Use `better-sqlite3` + `wrangler d1 execute`.
- **Task 24: Cloudflare Access** — create Access application on `internal.sparkry.ai`, Google IdP, email allowlist, bypass rule for `/api/webhooks/stripe`.
- **Task 25: R2 backup** — create `sparkry-crm-backups` R2 bucket, bind in `wrangler.toml`, scheduled Worker exports D1 weekly.
- **Task 26: WAF rate limiting** — Cloudflare dashboard rules: webhook 100/min, OAuth 10/min.
- **Task 27: GitHub Actions CI** — test + lint on PR, `drizzle-kit generate` check, deploy on merge to main.
- **Task 28: Credit note auto-creation** — voiding a paid invoice auto-creates credit note, credit notes applicable to future invoices.
- **Task 29: Invoice print layout reset** — `[id]/print/+layout@.svelte` to reset dark app shell, render clean white for printing.

## Notes for Implementing Agents

- **Each workstream runs in its own git worktree** branched from `feat/crm-invoicing`
- **Test-writing agents** write failing tests FIRST, commit, then hand off to implementation agents
- **Implementation agents** make tests pass with minimal code, commit
- **Review agents** (qcheckt/qcheck/qcheckf) run after each workstream completes
- **All amounts are stored in integer cents** — never use floats for money
- **D1 `batch()`** for all multi-table writes — this is the atomicity guarantee
- **`$lib/server/`** prefix means server-only code — never import in `.svelte` files
- **optimistic locking**: include `updatedAt` in all update mutations, reject if stale
