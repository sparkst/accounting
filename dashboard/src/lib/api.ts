import type {
	Transaction,
	TransactionList,
	TransactionUpdate,
	HealthResponse,
	IngestResult,
	IngestSummary,
	Invoice,
	InvoiceListResponse,
	Customer,
	CalendarSession,
	ICalUploadResult
} from './types';

const BASE = '/api';

async function request<T>(path: string, init?: RequestInit): Promise<T> {
	const res = await fetch(`${BASE}${path}`, {
		headers: { 'Content-Type': 'application/json', ...init?.headers },
		...init
	});
	if (!res.ok) {
		const text = await res.text().catch(() => res.statusText);
		throw new Error(`API ${res.status}: ${text}`);
	}
	return res.json() as Promise<T>;
}

export interface TransactionFilters {
	entity?: string;
	status?: string;
	date_from?: string;
	date_to?: string;
	search?: string;
	limit?: number;
	offset?: number;
	sort_by?: string;
	sort_order?: 'asc' | 'desc';
}

export async function fetchTransactions(filters: TransactionFilters = {}): Promise<TransactionList> {
	const params = new URLSearchParams();
	for (const [key, val] of Object.entries(filters)) {
		if (val !== undefined && val !== '') {
			params.set(key, String(val));
		}
	}
	const qs = params.toString();
	return request<TransactionList>(`/transactions${qs ? `?${qs}` : ''}`);
}

export async function fetchReviewQueue(): Promise<Transaction[]> {
	return request<Transaction[]>('/transactions/review');
}

export async function fetchTransaction(id: string): Promise<Transaction> {
	return request<Transaction>(`/transactions/${id}`);
}

export async function confirmTransaction(id: string): Promise<Transaction> {
	return request<Transaction>(`/transactions/${id}`, {
		method: 'PATCH',
		body: JSON.stringify({ status: 'confirmed' })
	});
}

export async function updateTransaction(
	id: string,
	updates: TransactionUpdate
): Promise<Transaction> {
	return request<Transaction>(`/transactions/${id}`, {
		method: 'PATCH',
		body: JSON.stringify(updates)
	});
}

export async function triggerIngest(): Promise<IngestResult> {
	return request<IngestResult>('/ingest/run', { method: 'POST' });
}

/** Trigger an ingestion run for a specific source. */
export async function triggerSourceIngest(source: string): Promise<IngestSummary> {
	return request<IngestSummary>(`/ingest/run?source=${encodeURIComponent(source)}`, {
		method: 'POST'
	});
}

export async function fetchHealth(): Promise<HealthResponse> {
	return request<HealthResponse>('/health');
}

export interface SourceConfigItem {
	source: string;
	label: string;
	mode: string;
	configured: boolean;
	missing_env_vars: string[];
	notes: string;
}

export async function fetchSourceConfig(): Promise<SourceConfigItem[]> {
	return request<SourceConfigItem[]>('/health/source-config');
}

export interface ExtractReceiptResponse {
	transaction: Transaction;
	extraction: Record<string, unknown>;
	fields_updated: string[];
}

export async function extractReceipt(id: string, attachmentIndex = 0): Promise<ExtractReceiptResponse> {
	return request<ExtractReceiptResponse>(`/transactions/${id}/extract-receipt`, {
		method: 'POST',
		body: JSON.stringify({ attachment_index: attachmentIndex })
	});
}

export interface SplitLineItem {
	amount: number;
	entity?: string | null;
	tax_category?: string | null;
	description?: string | null;
}

export interface SplitResponse {
	parent: Transaction;
	children: Transaction[];
	hotel_suggestion?: {
		room_amount: string;
		meals_amount: string;
		entity: string | null;
		line_items: Array<{
			amount: string;
			entity: string | null;
			tax_category: string | null;
			description: string | null;
		}>;
	} | null;
}

export async function splitTransaction(id: string, lineItems: SplitLineItem[]): Promise<SplitResponse> {
	return request<SplitResponse>(`/transactions/${id}/split`, {
		method: 'POST',
		body: JSON.stringify({ line_items: lineItems })
	});
}

// ── Invoice API ─────────────────────────────────────────────────────────────

export interface InvoiceFilters {
	customer_id?: string;
	status?: string;
	date_from?: string;
	date_to?: string;
}

export async function fetchInvoices(filters: InvoiceFilters = {}): Promise<InvoiceListResponse> {
	const params = new URLSearchParams();
	for (const [key, val] of Object.entries(filters)) {
		if (val !== undefined && val !== '') {
			params.set(key, String(val));
		}
	}
	const qs = params.toString();
	return request<InvoiceListResponse>(`/invoices${qs ? `?${qs}` : ''}`);
}

export async function fetchInvoice(id: string): Promise<Invoice> {
	return request<Invoice>(`/invoices/${id}`);
}

export async function patchInvoice(
	id: string,
	updates: Record<string, unknown>
): Promise<Invoice> {
	return request<Invoice>(`/invoices/${id}`, {
		method: 'PATCH',
		body: JSON.stringify(updates)
	});
}

export async function transitionInvoiceStatus(
	id: string,
	status: string,
	extra?: { paid_date?: string; payment_transaction_id?: string }
): Promise<Invoice> {
	return request<Invoice>(`/invoices/${id}/status`, {
		method: 'PATCH',
		body: JSON.stringify({ status, ...extra })
	});
}

export async function generateFlatInvoice(
	customer_id: string,
	month: string
): Promise<Invoice> {
	return request<Invoice>('/invoices/generate-flat', {
		method: 'POST',
		body: JSON.stringify({ customer_id, month })
	});
}

export async function generateCalendarInvoice(
	customer_id: string,
	sessions: CalendarSession[]
): Promise<Invoice> {
	return request<Invoice>('/invoices/generate-calendar', {
		method: 'POST',
		body: JSON.stringify({ customer_id, sessions })
	});
}

export async function uploadIcal(
	file: File,
	customer_id?: string,
	start_date?: string,
	end_date?: string
): Promise<ICalUploadResult> {
	const formData = new FormData();
	formData.append('file', file);
	const params = new URLSearchParams();
	if (customer_id) params.set('customer_id', customer_id);
	if (start_date) params.set('start_date', start_date);
	if (end_date) params.set('end_date', end_date);
	const qs = params.toString();

	const res = await fetch(`${BASE}/invoices/ical-upload${qs ? `?${qs}` : ''}`, {
		method: 'POST',
		body: formData
	});
	if (!res.ok) {
		const text = await res.text().catch(() => res.statusText);
		throw new Error(`API ${res.status}: ${text}`);
	}
	return res.json() as Promise<ICalUploadResult>;
}

export function getInvoicePdfUrl(id: string): string {
	return `${BASE}/invoices/${id}/pdf`;
}

export function getInvoiceHtmlUrl(id: string): string {
	return `${BASE}/invoices/${id}/html`;
}

export async function fetchCustomers(): Promise<Customer[]> {
	return request<Customer[]>('/customers');
}

export async function createCustomer(data: Record<string, unknown>): Promise<Customer> {
	return request<Customer>('/customers', {
		method: 'POST',
		body: JSON.stringify(data)
	});
}

export async function patchCustomer(id: string, data: Record<string, unknown>): Promise<Customer> {
	return request<Customer>(`/customers/${id}`, {
		method: 'PATCH',
		body: JSON.stringify(data)
	});
}

// ── Vendor Rules API ─────────────────────────────────────────────────────────

export interface VendorRule {
	id: string;
	vendor_pattern: string;
	entity: string;
	tax_category: string;
	tax_subcategory: string | null;
	direction: string;
	deductible_pct: number;
	confidence: number;
	source: string;
	examples: number;
	last_matched: string | null;
	created_at: string;
}

export interface VendorRuleWithMatches extends VendorRule {
	match_count: number;
	last_matches: Array<{
		id: string;
		date: string;
		description: string;
		amount: string | null;
		entity: string | null;
		tax_category: string | null;
		status: string;
	}>;
}

export interface VendorRuleListResponse {
	items: VendorRule[];
	total: number;
	limit: number;
	offset: number;
}

export interface VendorRuleFilters {
	search?: string;
	entity?: string;
	limit?: number;
	offset?: number;
}

export interface VendorRuleCreate {
	vendor_pattern: string;
	entity: string;
	tax_category: string;
	tax_subcategory?: string | null;
	direction: string;
	deductible_pct?: number;
	confidence?: number;
	source?: string;
}

export interface VendorRulePatch {
	vendor_pattern?: string;
	entity?: string;
	tax_category?: string;
	tax_subcategory?: string | null;
	direction?: string;
	deductible_pct?: number;
	confidence?: number;
}

export async function fetchVendorRules(
	filters: VendorRuleFilters = {}
): Promise<VendorRuleListResponse> {
	const params = new URLSearchParams();
	for (const [key, val] of Object.entries(filters)) {
		if (val !== undefined && val !== '') {
			params.set(key, String(val));
		}
	}
	const qs = params.toString();
	return request<VendorRuleListResponse>(`/vendor-rules${qs ? `?${qs}` : ''}`);
}

export async function fetchVendorRule(id: string): Promise<VendorRuleWithMatches> {
	return request<VendorRuleWithMatches>(`/vendor-rules/${id}`);
}

export async function createVendorRule(data: VendorRuleCreate): Promise<VendorRule> {
	return request<VendorRule>('/vendor-rules', {
		method: 'POST',
		body: JSON.stringify(data)
	});
}

export async function patchVendorRule(id: string, data: VendorRulePatch): Promise<VendorRule> {
	return request<VendorRule>(`/vendor-rules/${id}`, {
		method: 'PATCH',
		body: JSON.stringify(data)
	});
}

export async function deleteVendorRule(id: string): Promise<void> {
	const res = await fetch(`${BASE}/vendor-rules/${id}`, { method: 'DELETE' });
	if (!res.ok && res.status !== 204) {
		const text = await res.text().catch(() => res.statusText);
		throw new Error(`API ${res.status}: ${text}`);
	}
}

// ── Tax Summary & Export API ──────────────────────────────────────────────────

export interface TaxLineItem {
	tax_category: string;
	irs_line: string;
	total: number;
	is_income: boolean;
	is_reimbursable: boolean;
}

export interface TaxReadiness {
	total_count: number;
	confirmed_count: number;
	unconfirmed_count: number;
	needs_review_count: number;
	auto_classified_count: number;
	readiness_pct: number;
	unconfirmed_ids: string[];
}

export interface TaxWarning {
	warning: string;
	unconfirmed_count: number;
	unconfirmed_ids: string[];
}

export interface TaxYoyDelta {
	tax_category: string;
	irs_line: string;
	is_income: boolean;
	is_reimbursable: boolean;
	current: number;
	prior: number;
	delta: number;
	delta_pct: number | null;
}

export interface TaxBnoMonthlyDelta {
	month: string;
	current: number;
	prior: number;
	delta: number;
}

export interface TaxBnoQuarterlyDelta {
	quarter: string;
	current: number;
	prior: number;
	delta: number;
}

export interface TaxYoyComparison {
	prior_year: number;
	prior_year_items: TaxLineItem[];
	prior_gross_income: number;
	prior_total_expenses: number;
	prior_net_profit: number;
	deltas: TaxYoyDelta[];
	net_profit_delta: number;
	net_profit_delta_pct: number | null;
	bno_monthly_deltas: TaxBnoMonthlyDelta[];
	bno_quarterly_deltas: TaxBnoQuarterlyDelta[];
}

export interface TaxTip {
	id: string;
	type: 'home_office' | 'estimated_tax' | 'reimbursable' | 'vehicle' | 'unlinked_income';
	title: string;
	detail: string;
	action_url: string | null;
	dismissible: boolean;
}

export interface EstimatedTaxQuarter {
	quarter: string;
	due_date: string;
	projected_amount: number;
	paid: number;
	remaining: number;
	state: 'paid' | 'overdue' | 'upcoming';
}

export interface EstimatedTax {
	months_elapsed: number;
	ytd_net_profit: number;
	projected_annual_net: number;
	se_tax_annual: number;
	income_tax_annual: number;
	total_annual: number;
	quarterly_payment: number;
	total_paid: number;
	quarters: EstimatedTaxQuarter[];
}

export interface TaxSummary {
	entity: string;
	year: number;
	line_items: TaxLineItem[];
	gross_income: number;
	total_expenses: number;
	net_profit: number;
	readiness: TaxReadiness;
	warnings: TaxWarning[];
	comparison: TaxYoyComparison | null;
	tax_tips: TaxTip[];
	estimated_tax: EstimatedTax | null;
}

export async function fetchTaxSummary(
	entity: string,
	year: number,
	compareYear?: number
): Promise<TaxSummary> {
	let url = `/tax-summary?entity=${encodeURIComponent(entity)}&year=${year}`;
	if (compareYear !== undefined) {
		url += `&compare_year=${compareYear}`;
	}
	return request<TaxSummary>(url);
}

/**
 * Trigger a file download from one of the export endpoints.
 * Fetches the response as a blob and programmatically triggers a browser download.
 */
export async function downloadExport(
	endpoint: 'freetaxusa' | 'taxact' | 'bno',
	entity: string,
	year: number,
	filename: string
): Promise<void> {
	const url = `${BASE}/export/${endpoint}?entity=${encodeURIComponent(entity)}&year=${year}`;
	const res = await fetch(url);
	if (!res.ok) {
		const text = await res.text().catch(() => res.statusText);
		throw new Error(`Export failed (${res.status}): ${text}`);
	}
	const blob = await res.blob();
	const objectUrl = URL.createObjectURL(blob);
	const a = document.createElement('a');
	a.href = objectUrl;
	a.download = filename;
	document.body.appendChild(a);
	a.click();
	document.body.removeChild(a);
	URL.revokeObjectURL(objectUrl);
}

// --- Aggregations (for InsightPanel) ---

export interface TimeSeriesPoint {
	period: string;
	total: number;
}

export interface TopVendorItem {
	vendor: string;
	total: number;
	pct: number;
}

export interface MoMChange {
	income_delta: number;
	income_pct: number;
	expense_delta: number;
	expense_pct: number;
}

export interface ConcentrationWarning {
	vendor: string;
	pct: number;
	message: string;
}

export interface AnomalyItem {
	tx_id: string;
	vendor: string;
	amount: number;
	avg_for_vendor: number;
	message: string;
}

export interface CategoryBreakdownItem {
	category: string;
	total: number;
	pct: number;
}

export interface AggregationData {
	time_series: {
		income: TimeSeriesPoint[];
		expenses: TimeSeriesPoint[];
	};
	top_vendors: {
		income: TopVendorItem[];
		expense: TopVendorItem[];
	};
	mom_change: MoMChange;
	concentration_warnings: ConcentrationWarning[];
	anomalies: AnomalyItem[];
	category_breakdown: CategoryBreakdownItem[];
	expense_attribution: string;
}

export async function fetchAggregations(params: {
	entity?: string;
	date_from?: string;
	date_to?: string;
}): Promise<AggregationData> {
	const qs = new URLSearchParams();
	if (params.entity) qs.set('entity', params.entity);
	if (params.date_from) qs.set('date_from', params.date_from);
	if (params.date_to) qs.set('date_to', params.date_to);
	const res = await fetch(`${BASE}/transactions/aggregations?${qs}`);
	if (!res.ok) throw new Error(`Aggregations failed: ${res.status}`);
	return res.json();
}
