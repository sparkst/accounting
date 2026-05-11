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

/**
 * Read the API key from the Vite public env var VITE_API_KEY.
 * In local dev this is typically unset (auth disabled on the server).
 * Set VITE_API_KEY in dashboard/.env.local to match the server's API_KEY (managed via Doppler).
 */
function getApiKeyHeader(): Record<string, string> {
	const key =
		typeof import.meta !== 'undefined' && import.meta.env
			? (import.meta.env.VITE_API_KEY as string | undefined)
			: undefined;
	return key ? { 'X-Api-Key': key } : {};
}

/**
 * Per-endpoint AbortController map. Rapid consecutive calls to the same path
 * (e.g. fast filter changes in Register) abort the previous in-flight request
 * so a slow stale response cannot overwrite a fresher one.
 */
const _controllers = new Map<string, AbortController>();

async function request<T>(path: string, init?: RequestInit): Promise<T> {
	// Abort previous in-flight GET to the SAME full path (including query params).
	// This prevents stale filter responses in Register without interfering with
	// concurrent calls to the same endpoint with different params (e.g. Dashboard
	// calls /transactions twice with different filters via Promise.all).
	const method = (init?.method ?? 'GET').toUpperCase();
	let controller: AbortController | undefined;

	if (method === 'GET') {
		const previous = _controllers.get(path);
		if (previous) {
			previous.abort();
		}
		controller = new AbortController();
		_controllers.set(path, controller);
	}

	try {
		const res = await fetch(`${BASE}${path}`, {
			headers: { 'Content-Type': 'application/json', ...getApiKeyHeader(), ...init?.headers },
			...init,
			...(controller ? { signal: controller.signal } : {})
		});
		if (!res.ok) {
			const text = await res.text().catch(() => res.statusText);
			throw new Error(`API ${res.status}: ${text}`);
		}
		return res.json() as Promise<T>;
	} finally {
		if (controller && _controllers.get(path) === controller) {
			_controllers.delete(path);
		}
	}
}

/** Build a query string from a filters object, omitting undefined/empty values. */
function toQueryString(filters: object): string {
	const params = new URLSearchParams();
	for (const [key, val] of Object.entries(filters)) {
		if (val !== undefined && val !== '') {
			params.set(key, String(val));
		}
	}
	const qs = params.toString();
	return qs ? `?${qs}` : '';
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
	return request<TransactionList>(`/transactions${toQueryString(filters)}`);
}

export async function fetchReviewQueue(status?: string): Promise<Transaction[]> {
	const params = status ? `?status=${encodeURIComponent(status)}` : '';
	return request<Transaction[]>(`/transactions/review${params}`);
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

export interface UploadReceiptResult {
	path: string;
	filename: string;
	attachments: string[];
}

export async function uploadReceipt(
	transactionId: string,
	file: File,
	onProgress?: (pct: number) => void
): Promise<UploadReceiptResult> {
	const formData = new FormData();
	formData.append('file', file);

	return new Promise((resolve, reject) => {
		const xhr = new XMLHttpRequest();
		const apiKey =
			typeof import.meta !== 'undefined' && import.meta.env
				? (import.meta.env.VITE_API_KEY as string | undefined)
				: undefined;

		xhr.open('POST', `${BASE}/transactions/${transactionId}/upload-receipt`);
		if (apiKey) xhr.setRequestHeader('X-Api-Key', apiKey);

		if (onProgress) {
			xhr.upload.onprogress = (e) => {
				if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100));
			};
		}

		xhr.onload = () => {
			if (xhr.status >= 200 && xhr.status < 300) {
				resolve(JSON.parse(xhr.responseText) as UploadReceiptResult);
			} else {
				let detail = xhr.statusText;
				try {
					detail = JSON.parse(xhr.responseText)?.detail ?? detail;
				} catch { /* ignore */ }
				reject(new Error(`Upload failed (${xhr.status}): ${detail}`));
			}
		};

		xhr.onerror = () => reject(new Error('Network error during upload'));
		xhr.send(formData);
	});
}

export async function bulkConfirmTransactions(
	ids: string[],
	entity: string,
	tax_category: string
): Promise<{ confirmed: number; rules_created: number }> {
	const payload: Record<string, unknown> = { ids };
	if (entity) payload.entity = entity;
	if (tax_category) payload.tax_category = tax_category;
	return request<{ confirmed: number; rules_created: number }>('/transactions/bulk-confirm', {
		method: 'POST',
		body: JSON.stringify(payload)
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
	return request<InvoiceListResponse>(`/invoices${toQueryString(filters)}`);
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
		headers: getApiKeyHeader(),
		body: formData
	});
	if (!res.ok) {
		const text = await res.text().catch(() => res.statusText);
		throw new Error(`API ${res.status}: ${text}`);
	}
	return res.json() as Promise<ICalUploadResult>;
}

export interface SendInvoiceResponse {
	invoice: Invoice;
	message: string;
}

export async function sendInvoice(
	invoiceId: string,
	toEmail?: string
): Promise<SendInvoiceResponse> {
	const body: Record<string, string> = {};
	if (toEmail) body.to_email = toEmail;
	return request<SendInvoiceResponse>(`/invoices/${invoiceId}/send`, {
		method: 'POST',
		body: JSON.stringify(body)
	});
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
	return request<VendorRuleListResponse>(`/vendor-rules${toQueryString(filters)}`);
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
	const res = await fetch(`${BASE}/vendor-rules/${id}`, { method: 'DELETE', headers: getApiKeyHeader() });
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
	warning?: string;
}

export interface Tax1099Entry {
	payer: string;
	type: string | null;
	total: number;
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
	income_1099_breakdown: Tax1099Entry[];
}

// ── Monthly breakdown types ───────────────────────────────────────────────────

export interface MonthlyCategoryItem {
	tax_category: string;
	total: number;
	is_income: boolean;
	is_reimbursable: boolean;
}

export interface MonthlyBreakdownMonth {
	month: string; // "YYYY-MM"
	categories: MonthlyCategoryItem[];
}

export interface MonthlyBreakdown {
	entity: string;
	year: number;
	months: MonthlyBreakdownMonth[];
}

export async function fetchMonthlyBreakdown(
	entity: string,
	year: number
): Promise<MonthlyBreakdown> {
	return request<MonthlyBreakdown>(
		`/tax-summary/monthly?entity=${encodeURIComponent(entity)}&year=${year}`
	);
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
	const res = await fetch(url, { headers: getApiKeyHeader() });
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
	const res = await fetch(`${BASE}/transactions/aggregations?${qs}`, { headers: getApiKeyHeader() });
	if (!res.ok) throw new Error(`Aggregations failed: ${res.status}`);
	return res.json();
}

// ── Import API ────────────────────────────────────────────────────────────────

export interface BankCsvConfig {
	bank_name: string;
	label: string;
	date_col: string;
	amount_col: string;
	description_col: string;
	entity: string | null;
}

export interface BankCsvPreviewRow {
	[key: string]: string;
}

export interface BankCsvPreview {
	bank_name: string | null;
	headers: string[];
	sample_rows: BankCsvPreviewRow[];
	row_count: number;
	detected_config: BankCsvConfig | null;
}

export interface BankCsvCommitResult {
	created: number;
	skipped: number;
	errors: string[];
}

export interface BrokerageCsvResult {
	created: number;
	skipped: number;
	errors: string[];
}

/** Fetch saved bank CSV configs from the server. */
export async function fetchBankCsvConfigs(): Promise<BankCsvConfig[]> {
	return request<BankCsvConfig[]>('/import/bank-csv/configs');
}

/**
 * Upload a bank CSV for preview. Returns detected headers, sample rows,
 * and auto-detected config if the bank was recognized.
 */
export async function previewBankCsv(
	file: File,
	bankName?: string
): Promise<BankCsvPreview> {
	const formData = new FormData();
	formData.append('file', file);
	if (bankName) formData.append('bank_name', bankName);

	const res = await fetch(`${BASE}/import/bank-csv/preview`, {
		method: 'POST',
		headers: getApiKeyHeader(),
		body: formData
	});
	if (!res.ok) {
		const text = await res.text().catch(() => res.statusText);
		throw new Error(`Preview failed (${res.status}): ${text}`);
	}
	return res.json() as Promise<BankCsvPreview>;
}

/**
 * Commit a bank CSV import. Sends the file plus selected bank name,
 * and returns counts of created/skipped/errors.
 */
export async function commitBankCsv(
	file: File,
	bankName: string
): Promise<BankCsvCommitResult> {
	const formData = new FormData();
	formData.append('file', file);
	formData.append('bank_name', bankName);

	const res = await fetch(`${BASE}/import/bank-csv/commit`, {
		method: 'POST',
		headers: getApiKeyHeader(),
		body: formData
	});
	if (!res.ok) {
		const text = await res.text().catch(() => res.statusText);
		throw new Error(`Import failed (${res.status}): ${text}`);
	}
	return res.json() as Promise<BankCsvCommitResult>;
}

/**
 * Import a brokerage CSV. Returns counts of created/skipped/errors.
 */
export async function importBrokerageCsv(file: File): Promise<BrokerageCsvResult> {
	const formData = new FormData();
	formData.append('file', file);

	const res = await fetch(`${BASE}/import/brokerage-csv`, {
		method: 'POST',
		headers: getApiKeyHeader(),
		body: formData
	});
	if (!res.ok) {
		const text = await res.text().catch(() => res.statusText);
		throw new Error(`Import failed (${res.status}): ${text}`);
	}
	return res.json() as Promise<BrokerageCsvResult>;
}

// ─── Brokerage (Phase 2 / Option 2) ─────────────────────────────────────────

export interface BrokerageNetWorth {
	total: number;
	by_broker: Record<string, number>;
	by_entity: Record<string, number>;
	as_of_min: string | null;
	as_of_max: string | null;
	zero_snapshot_account_count: number;
	plan_wrapper_excluded_count: number;
}

export interface BrokerageAccount {
	account_id: string;
	broker: string;
	account_number_masked: string;
	account_name: string | null;
	account_type: string;
	entity: string;
	tax_sheltered: boolean;
	is_plan_wrapper: boolean;
	as_of: string | null;
	market_value: number;
	tags: string[];
}

export async function updateBrokerageAccountTags(
	accountId: string,
	tags: string[]
): Promise<{ account_id: string; tags: string[] }> {
	return request<{ account_id: string; tags: string[] }>(
		`/brokerage/accounts/${encodeURIComponent(accountId)}/tags`,
		{
			method: 'PUT',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ tags })
		}
	);
}

export interface BrokerageHolding {
	symbol: string | null;
	description: string | null;
	total_quantity: number;
	total_market_value: number;
	pct_of_net_worth: number;
	account_count: number;
	is_cash_sleeve: boolean;
}

export interface BrokerageRecentTransaction {
	trade_date: string;
	broker: string;
	account_number_masked: string;
	action: string;
	canonical_action: string;
	symbol: string | null;
	quantity: number | null;
	amount: number | null;
}

export interface BrokerageRealizedGLBucket {
	short_term: number;
	long_term: number;
	unknown: number;
	total: number;
	lots: number;
}

export interface BrokerageRealizedGL {
	by_year: Record<string, BrokerageRealizedGLBucket>;
	wash_sales: { lots: number; total_disallowed_loss: number };
}

export interface BrokerageDataIntegrity {
	accounts: number;
	transactions: number;
	position_snapshots: number;
	realized_lots: number;
	orphan_transactions: number;
	orphan_snapshots: number;
	stale_snapshot_accounts: number;
	suspect_symbols: number;
	duplicate_position_groups: number;
	duplicate_transaction_groups: number;
}

export async function fetchBrokerageNetWorth(): Promise<BrokerageNetWorth> {
	return request<BrokerageNetWorth>('/brokerage/networth');
}

export async function fetchBrokerageAccounts(): Promise<BrokerageAccount[]> {
	return request<BrokerageAccount[]>('/brokerage/accounts');
}

export async function fetchBrokerageTopHoldings(n = 10): Promise<BrokerageHolding[]> {
	return request<BrokerageHolding[]>(`/brokerage/top-holdings?n=${n}`);
}

export async function fetchBrokerageRecentTransactions(
	days = 14
): Promise<BrokerageRecentTransaction[]> {
	return request<BrokerageRecentTransaction[]>(`/brokerage/recent-transactions?days=${days}`);
}

export async function fetchBrokerageRealizedGL(): Promise<BrokerageRealizedGL> {
	return request<BrokerageRealizedGL>('/brokerage/realized-gl');
}

export async function fetchBrokerageDataIntegrity(): Promise<BrokerageDataIntegrity> {
	return request<BrokerageDataIntegrity>('/brokerage/data-integrity');
}

export interface BrokerageNetWorthHistoryPoint {
	as_of: string;
	balance_total: number;
	account_count: number;
}

export interface BrokerageHistoryQuery {
	includeUnmatched?: boolean;
	accountIds?: string[];
	tagsInclude?: string[];
	tagsExclude?: string[];
}

export async function fetchBrokerageNetWorthHistory(
	q: BrokerageHistoryQuery | boolean = true
): Promise<BrokerageNetWorthHistoryPoint[]> {
	// Backwards-compatible: callers passing a bare boolean get the
	// include_unmatched-only behavior (default true so the chart renders
	// the full XLSX series before the manual matching step lands).
	const opts: BrokerageHistoryQuery =
		typeof q === 'boolean' ? { includeUnmatched: q } : q;
	const params = new URLSearchParams();
	if (opts.includeUnmatched ?? true) params.set('include_unmatched', 'true');
	if (opts.accountIds?.length) params.set('account_ids', opts.accountIds.join(','));
	if (opts.tagsInclude?.length) params.set('tags_include', opts.tagsInclude.join(','));
	if (opts.tagsExclude?.length) params.set('tags_exclude', opts.tagsExclude.join(','));
	const qs = params.toString();
	return request<BrokerageNetWorthHistoryPoint[]>(
		`/brokerage/networth-history${qs ? `?${qs}` : ''}`
	);
}

export interface BrokerageHoldingValuePoint {
	as_of: string;
	market_value: number;
	quantity: number;
}

export interface BrokerageHoldingLot {
	open_date: string;
	raw_account_name: string;
	quantity: number;
	cost_per_share: number;
	cost_total: number;
	source: string;
}

export interface BrokerageHoldingHistory {
	symbol: string;
	security_name: string | null;
	current_value: number;
	current_quantity: number;
	cost_basis: number;
	unrealized_gain: number;
	unrealized_pct: number;
	value_series: BrokerageHoldingValuePoint[];
	lots: BrokerageHoldingLot[];
}

export async function fetchBrokerageHoldingHistory(
	symbol: string
): Promise<BrokerageHoldingHistory> {
	return request<BrokerageHoldingHistory>(
		`/brokerage/holdings/${encodeURIComponent(symbol)}/history`
	);
}

export interface BrokerageBenchmarkPoint {
	as_of: string;
	portfolio_value: number;
	benchmark_value: number | null;
}

export interface BrokerageBenchmarkComparison {
	benchmark_symbol: string;
	series: BrokerageBenchmarkPoint[];
	portfolio_pct: number | null;
	benchmark_pct: number | null;
}

export async function fetchBrokerageBenchmarkComparison(
	benchmark = 'SPY'
): Promise<BrokerageBenchmarkComparison> {
	return request<BrokerageBenchmarkComparison>(
		`/brokerage/networth-history-benchmark?benchmark=${encodeURIComponent(benchmark)}`
	);
}

export interface BrokerageMissingAccount {
	id: string;
	institution: string;
	account_name: string;
	last_4: string | null;
	status: string;
	source: string;
	resolved_account_id: string | null;
	last_seen_days_ago: number | null;
}

export async function fetchBrokerageMissingAccounts(): Promise<BrokerageMissingAccount[]> {
	return request<BrokerageMissingAccount[]>('/brokerage/missing-accounts');
}

// ─── Brokerage account PATCH + detail ────────────────────────────────────────

/**
 * Partial-update payload for PATCH /brokerage/accounts/{id}.
 *
 * All fields are optional. The API uses Pydantic's ``model_fields_set`` to
 * distinguish "field omitted" (leave existing value alone) from "field
 * explicitly set to null" (clear the column). Pass ``null`` to clear, omit
 * the key to leave unchanged.
 */
export interface AccountPatchBody {
	account_name?: string | null;
	beneficiary?: string | null;
	notes?: string | null;
}

export interface AccountPatchResponse {
	account_id: string;
	account_name: string | null;
	beneficiary: string | null;
	notes: string | null;
	updated_at: string;
}

export async function patchBrokerageAccount(
	accountId: string,
	patch: AccountPatchBody
): Promise<AccountPatchResponse> {
	return request<AccountPatchResponse>(
		`/brokerage/accounts/${encodeURIComponent(accountId)}`,
		{
			method: 'PATCH',
			body: JSON.stringify(patch)
		}
	);
}

export interface AccountDetailAccount {
	id: string;
	broker: string;
	account_number_masked: string;
	account_name: string | null;
	account_type: string;
	entity: string;
	tax_sheltered: boolean;
	beneficiary: string | null;
	notes: string | null;
	parent_account_id: string | null;
	is_plan_wrapper: boolean;
	created_at: string;
	updated_at: string;
	tags: string[];
}

export interface PositionSnapshotDetailRow {
	id: string;
	as_of: string;
	symbol: string | null;
	description: string | null;
	quantity: number | null;
	price: number | null;
	market_value: number | null;
	source_file: string;
	source_row_hash: string;
}

export interface BalanceSnapshotDetailRow {
	id: string;
	as_of: string;
	raw_account_name: string;
	balance: number;
	source: string;
}

export interface AccountRealizedGLSummary {
	short_term: number;
	long_term: number;
	total: number;
	lots: number;
}

export interface IngestionLogDetailRow {
	id: string;
	source: string;
	run_at: string;
	status: string;
	records_processed: number;
	records_failed: number;
	/** Truncated to 200 chars server-side. Null when no error occurred. */
	error_detail: string | null;
}

export interface AccountDetailResponse {
	account: AccountDetailAccount;
	latest_position_snapshots: PositionSnapshotDetailRow[];
	latest_balance_snapshots: BalanceSnapshotDetailRow[];
	transaction_count_by_action: Record<string, number>;
	realized_gl_summary: AccountRealizedGLSummary;
	ingestion_log_recent: IngestionLogDetailRow[];
}

export async function fetchBrokerageAccountDetail(
	accountId: string
): Promise<AccountDetailResponse> {
	return request<AccountDetailResponse>(
		`/brokerage/accounts/${encodeURIComponent(accountId)}/detail`
	);
}

// ───── Plaid Phase 1 — admin/connections ─────────────────────────────

export interface PlaidLinkTokenResponse {
	link_token: string;
	state_nonce: string;
	expires_at: string;
}

export interface PlaidExchangePayload {
	public_token: string;
	state_nonce: string;
	institution_id: string;
	institution_name: string;
}

export interface PlaidAccountFromExchange {
	account_id: string;
	mask?: string | null;
	name?: string | null;
	official_name?: string | null;
	type?: string | null;
	subtype?: string | null;
	balances?: {
		available?: number | null;
		current?: number | null;
		iso_currency_code?: string | null;
	};
}

export interface PlaidExchangeResponse {
	item_id: string;
	plaid_item_id: string;
	accounts: PlaidAccountFromExchange[];
}

export interface PlaidItemSummary {
	id: string;
	item_id: string;
	institution_id: string;
	institution_name: string;
	status: string;
	last_sync_at: string | null;
	last_sync_status: string | null;
	last_error: string | null;
	mapped_account_count: number;
}

export interface PlaidReconciliationRow {
	account_id: string;
	account_name: string | null;
	snapshot_date: string;
	plaid_account_type: string;
	plaid_total: number;
	computed_total: number | null;
	delta: number | null;
	delta_pct: number | null;
	exceeds_threshold: boolean;
}

export interface PlaidMapAccountsPayload {
	item_id: string;
	mappings: Array<{
		plaid_account_id: string;
		account_id?: string;
		create_new?: {
			broker: string;
			account_number: string;
			account_name?: string | null;
			account_type: string;
			entity?: string;
			tax_sheltered?: boolean;
		};
	}>;
}

export async function plaidCreateLinkToken(): Promise<PlaidLinkTokenResponse> {
	return request<PlaidLinkTokenResponse>('/plaid/link-token', { method: 'POST' });
}

export async function plaidExchangePublicToken(
	payload: PlaidExchangePayload
): Promise<PlaidExchangeResponse> {
	return request<PlaidExchangeResponse>('/plaid/exchange', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(payload)
	});
}

export async function plaidMapAccounts(
	payload: PlaidMapAccountsPayload
): Promise<{ mappings: Array<{ plaid_account_id: string; account_id: string }> }> {
	return request('/plaid/map-accounts', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(payload)
	});
}

export async function plaidDisconnect(
	plaidItemId: string
): Promise<{ status: string; item_id: string; accounts_unmapped: number; plaid_remove_called: boolean }> {
	return request(`/plaid/disconnect/${encodeURIComponent(plaidItemId)}`, { method: 'POST' });
}

export async function plaidRelink(plaidItemId: string): Promise<PlaidLinkTokenResponse> {
	return request<PlaidLinkTokenResponse>(`/plaid/relink/${encodeURIComponent(plaidItemId)}`, {
		method: 'POST'
	});
}

export async function plaidListItems(): Promise<PlaidItemSummary[]> {
	return request<PlaidItemSummary[]>('/plaid/items');
}

export async function plaidSyncNow(
	itemId?: string
): Promise<unknown> {
	const qs = itemId ? `?item_id=${encodeURIComponent(itemId)}` : '';
	return request(`/plaid/sync-now${qs}`, { method: 'POST' });
}

export async function plaidReconciliationSummary(): Promise<PlaidReconciliationRow[]> {
	return request<PlaidReconciliationRow[]>('/plaid/reconciliation/summary');
}
