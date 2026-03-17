// TypeScript types matching the FastAPI response shapes.

export type Entity = 'sparkry' | 'blackline' | 'personal';

export type Direction = 'income' | 'expense' | 'transfer' | 'reimbursable';

export type TaxCategory =
	// Business
	| 'ADVERTISING'
	| 'CAR_AND_TRUCK'
	| 'CONTRACT_LABOR'
	| 'INSURANCE'
	| 'LEGAL_AND_PROFESSIONAL'
	| 'OFFICE_EXPENSE'
	| 'SUPPLIES'
	| 'TAXES_AND_LICENSES'
	| 'TRAVEL'
	| 'MEALS'
	| 'COGS'
	| 'CONSULTING_INCOME'
	| 'SUBSCRIPTION_INCOME'
	| 'SALES_INCOME'
	| 'REIMBURSABLE'
	// Personal
	| 'CHARITABLE_CASH'
	| 'CHARITABLE_STOCK'
	| 'MEDICAL'
	| 'STATE_LOCAL_TAX'
	| 'MORTGAGE_INTEREST'
	| 'INVESTMENT_INCOME'
	| 'PERSONAL_NON_DEDUCTIBLE';

export type TransactionStatus =
	| 'auto_classified'
	| 'needs_review'
	| 'confirmed'
	| 'split_parent'
	| 'rejected';

export interface RawEmailData {
	id?: string;
	filename?: string;
	date?: string;
	from?: string;
	subject?: string;
	body_text?: string;
	body_html?: string;
	[key: string]: unknown;
}

export interface Transaction {
	id: string;
	date: string; // ISO date string
	vendor: string | null;
	description: string;
	amount: number;
	entity: Entity | null;
	tax_category: TaxCategory | null;
	tax_subcategory: string | null;
	direction: Direction | null;
	status: TransactionStatus;
	confidence: number | null;
	reasoning: string | null;
	source: string;
	source_id: string | null;
	notes: string | null;
	payment_method: string | null;
	parent_id: string | null;
	deductible_pct: number | null;
	confirmed_by: string | null;
	created_at: string;
	updated_at: string;
	raw_data: RawEmailData | null;
	attachments: string[] | null;
}

export interface TransactionList {
	items: Transaction[];
	total: number;
	income_total: number;
	expense_total: number;
	limit: number;
	offset: number;
}

export interface SourceHealth {
	source: string;
	last_sync: string | null;
	record_count: number;
	status: 'ok' | 'stale' | 'error' | 'never';
	message: string | null;
}

export interface SourceFreshness {
	source: string;
	last_run_at: string | null;          // ISO datetime
	freshness_status: 'green' | 'amber' | 'red' | 'never';
	ingestion_status: string | null;     // 'success' | 'partial_failure' | 'failure'
	records_processed: number;
	records_failed: number;
	last_error: string | null;
}

export interface ClassificationStats {
	needs_review: number;
	auto_classified: number;
	confirmed: number;
	split_parent: number;
	rejected: number;
	total: number;
	auto_confirmed_pct: number;
	edited_pct: number;
	pending_pct: number;
	rejected_pct: number;
	pending_count: number;
}

export interface TaxDeadline {
	label: string;
	entity: string;
	due_date: string;         // YYYY-MM-DD
	days_until_due: number;
}

export interface FailureLogEntry {
	source: string;
	run_at: string;           // ISO datetime
	ingestion_status: string;
	error_detail: string | null;
	records_processed: number;
	records_failed: number;
}

export interface HealthResponse {
	ok: boolean;
	source_freshness: SourceFreshness[];
	classification_stats: ClassificationStats;
	tax_deadlines: TaxDeadline[];
	failure_log: FailureLogEntry[];
	total_transactions: number;
	needs_review_count: number;
	checked_at: string;       // ISO datetime
}

export interface IngestResult {
	triggered: boolean;
	message: string;
}

export interface IngestSummary {
	ingested_count: number;
	classified_count: number;
	needs_review_count: number;
	errors: string[];
}

export interface TransactionUpdate {
	entity?: Entity;
	tax_category?: TaxCategory;
	tax_subcategory?: string;
	direction?: Direction;
	status?: TransactionStatus;
	notes?: string;
	amount?: number;
}

// ── Invoice types ───────────────────────────────────────────────────────────

export type InvoiceStatus = 'draft' | 'sent' | 'paid' | 'overdue' | 'void';

export type BillingModel = 'hourly' | 'flat_rate' | 'project';

export interface InvoiceLineItem {
	id: string;
	invoice_id: string;
	description: string;
	quantity: string | null;
	unit_price: string | null;
	total_price: string | null;
	date: string | null;
	sort_order: number;
}

export interface Invoice {
	id: string;
	invoice_number: string;
	customer_id: string;
	entity: string;
	project: string | null;
	submitted_date: string | null;
	due_date: string | null;
	service_period_start: string | null;
	service_period_end: string | null;
	paid_date: string | null;
	subtotal: string | null;
	adjustments: string | null;
	tax: string | null;
	total: string | null;
	status: InvoiceStatus;
	payment_terms: string | null;
	payment_method: string | null;
	late_fee_pct: number;
	po_number: string | null;
	payment_transaction_id: string | null;
	sap_instructions: Record<string, unknown> | null;
	sap_checklist_state: Record<string, unknown> | null;
	pdf_path: string | null;
	notes: string | null;
	created_at: string;
	updated_at: string;
	days_outstanding: number | null;
	expected_payment_date: string | null;
	line_items: InvoiceLineItem[] | null;
}

export interface InvoiceListResponse {
	items: Invoice[];
	total: number;
}

export interface Customer {
	id: string;
	name: string;
	contact_name: string | null;
	contact_email: string | null;
	billing_model: BillingModel;
	default_rate: string | null;
	payment_terms: string | null;
	invoice_prefix: string;
	late_fee_pct: number;
	po_number: string | null;
	sap_config: Record<string, unknown> | null;
	calendar_patterns: string[] | null;
	calendar_exclusions: string[] | null;
	address: Record<string, unknown> | null;
	contract_start_date: string | null;
	last_invoiced_date: string | null;
	notes: string | null;
	active: boolean;
	created_at: string;
}

export interface CalendarSession {
	date: string;
	description: string;
	hours: number;
	rate: number;
}

export interface ICalUploadResult {
	matched_sessions: CalendarSession[];
	unmatched_events: Array<Record<string, unknown>>;
	warnings: string[];
}
