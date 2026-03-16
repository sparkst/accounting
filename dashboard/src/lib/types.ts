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

export interface HealthResponse {
	sources: SourceHealth[];
	total_transactions: number;
	needs_review_count: number;
	confirmed_count: number;
	auto_classified_count: number;
	rejected_count: number;
}

export interface IngestResult {
	triggered: boolean;
	message: string;
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
