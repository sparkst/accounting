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
