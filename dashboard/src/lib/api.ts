import type {
	Transaction,
	TransactionList,
	TransactionUpdate,
	HealthResponse,
	IngestResult
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
