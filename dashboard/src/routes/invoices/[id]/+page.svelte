<script lang="ts">
	import { onMount } from 'svelte';
	import { page } from '$app/stores';
	import type { Invoice, Customer } from '$lib/types';
	import {
		fetchInvoice,
		fetchCustomers,
		patchInvoice,
		transitionInvoiceStatus,
		getInvoicePdfUrl,
		getInvoiceHtmlUrl
	} from '$lib/api';
	import Toast from '$lib/components/Toast.svelte';

	// ── State ─────────────────────────────────────────────────────────────────
	let invoice = $state<Invoice | null>(null);
	let customers = $state<Customer[]>([]);
	let loading = $state(true);
	let fetchError = $state('');
	let pdfLoading = $state(false);

	// Toast state
	let toasts = $state<Array<{ id: number; message: string; type: 'info' | 'success' | 'error' }>>([]);
	let toastCounter = $state(0);

	// Void confirmation
	let showVoidConfirm = $state(false);

	// SAP
	const SAP_STEPS = [
		'Log into SAP Ariba',
		'Open existing order (PO# shown below)',
		'Find the most recent invoice and copy it',
		'Update service period dates to match this invoice',
		'Update description with month number',
		'Enter Sparkry invoice number',
		'Verify amount',
		'Submit'
	];
	let sapChecklist = $state<Record<string, boolean>>({});
	let sapSaving = $state(false);
	let allSapChecked = $derived(SAP_STEPS.every((_, i) => sapChecklist[String(i)]));

	// ── Derived ───────────────────────────────────────────────────────────────
	let customer = $derived(customers.find((c) => c.id === invoice?.customer_id) ?? null);
	let isSapCustomer = $derived(customer?.billing_model === 'flat_rate' && invoice?.po_number);

	// ── Helpers ───────────────────────────────────────────────────────────────
	function addToast(message: string, type: 'info' | 'success' | 'error' = 'info') {
		const id = ++toastCounter;
		toasts = [...toasts, { id, message, type }];
	}

	function removeToast(id: number) {
		toasts = toasts.filter((t) => t.id !== id);
	}

	function fmtCurrency(val: string | null): string {
		if (!val) return '$0.00';
		const n = parseFloat(val);
		return n.toLocaleString('en-US', { style: 'currency', currency: 'USD' });
	}

	function fmtDate(iso: string | null): string {
		if (!iso) return '--';
		const d = new Date(iso + 'T00:00:00');
		return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
	}

	function statusClass(status: string): string {
		const map: Record<string, string> = {
			draft: 'status-draft',
			sent: 'status-sent',
			paid: 'status-paid',
			overdue: 'status-overdue',
			void: 'status-void'
		};
		return map[status] ?? '';
	}

	function dueLabel(inv: Invoice): string {
		if (inv.status === 'paid') return `Paid ${fmtDate(inv.paid_date)}`;
		if (inv.status === 'void') return 'Voided';
		if (inv.status === 'draft') return 'Draft';
		if (!inv.due_date) return '--';
		const today = new Date();
		today.setHours(0, 0, 0, 0);
		const due = new Date(inv.due_date + 'T00:00:00');
		const diff = Math.ceil((due.getTime() - today.getTime()) / (1000 * 60 * 60 * 24));
		if (diff < 0) return `${Math.abs(diff)} days overdue`;
		if (diff === 0) return 'Due today';
		return `${diff} days until due`;
	}

	async function toggleSapStep(index: number) {
		if (!invoice) return;
		sapChecklist = { ...sapChecklist, [String(index)]: !sapChecklist[String(index)] };
		sapSaving = true;
		try {
			await patchInvoice(invoice.id, { sap_checklist_state: sapChecklist });
		} catch {
			// silently ignore — local state is still updated, will retry on next toggle
		} finally {
			sapSaving = false;
		}
	}

	async function markSubmittedInSap() {
		if (!invoice) return;
		try {
			await transitionInvoiceStatus(invoice.id, 'sent');
			addToast(`Invoice ${invoice.invoice_number} marked as submitted in SAP Ariba`, 'success');
			await load();
		} catch (e) {
			addToast(e instanceof Error ? e.message : 'Failed to update status', 'error');
		}
	}

	// ── Load ─────────────────────────────────────────────────────────────────
	async function load() {
		loading = true;
		fetchError = '';
		try {
			const invoiceId = $page.params.id ?? '';
			const [inv, custs] = await Promise.all([fetchInvoice(invoiceId), fetchCustomers()]);
			invoice = inv;
			customers = custs;
			// Init SAP checklist — restore persisted state if available, else all false
			const saved = inv.sap_checklist_state as Record<string, boolean> | null;
			sapChecklist = {};
			SAP_STEPS.forEach((_, i) => {
				sapChecklist[String(i)] = saved ? (saved[String(i)] ?? false) : false;
			});
		} catch (e) {
			fetchError = e instanceof Error ? e.message : 'Failed to load invoice';
		} finally {
			loading = false;
		}
	}

	onMount(() => {
		load();
	});

	// ── Status transitions ───────────────────────────────────────────────────
	async function markSent() {
		if (!invoice) return;
		try {
			await transitionInvoiceStatus(invoice.id, 'sent');
			addToast(`Invoice ${invoice.invoice_number} marked as sent`, 'success');
			await load();
		} catch (e) {
			addToast(e instanceof Error ? e.message : 'Failed', 'error');
		}
	}

	async function markPaid() {
		if (!invoice) return;
		try {
			await transitionInvoiceStatus(invoice.id, 'paid');
			addToast(`Invoice ${invoice.invoice_number} marked as paid`, 'success');
			await load();
		} catch (e) {
			addToast(e instanceof Error ? e.message : 'Failed', 'error');
		}
	}

	async function voidInvoice() {
		if (!invoice) return;
		try {
			await transitionInvoiceStatus(invoice.id, 'void');
			addToast(`Invoice ${invoice.invoice_number} voided`, 'success');
			showVoidConfirm = false;
			await load();
		} catch (e) {
			addToast(e instanceof Error ? e.message : 'Failed', 'error');
		}
	}

	function handlePdfClick() {
		pdfLoading = true;
		// PDF downloads via browser; reset after a delay
		setTimeout(() => {
			pdfLoading = false;
		}, 3000);
	}
</script>

<!-- Toasts -->
{#each toasts as toast (toast.id)}
	<Toast message={toast.message} type={toast.type} ondismiss={() => removeToast(toast.id)} />
{/each}

<div class="container page-shell">
	<header class="page-header">
		<div>
			<a href="/invoices" class="back-link">Invoices</a>
			{#if invoice}
				<h1>Invoice {invoice.invoice_number}</h1>
			{:else}
				<h1>Invoice Detail</h1>
			{/if}
		</div>
	</header>

	{#if loading}
		<div class="card skeleton-card">
			<div class="skeleton" style="height: 20px; width: 40%; margin-bottom: 12px;"></div>
			<div class="skeleton" style="height: 28px; width: 60%; margin-bottom: 16px;"></div>
			<div class="skeleton" style="height: 16px; width: 50%;"></div>
		</div>
	{:else if fetchError}
		<div class="card error-card">
			<p class="error-msg">{fetchError}</p>
			<button class="btn btn-ghost" onclick={load}>Try again</button>
		</div>
	{:else if invoice}
		<!-- Header card -->
		<div class="card detail-card">
			<div class="detail-top">
				<div class="detail-top-left">
					<span class="status-pill {statusClass(invoice.status)}">{invoice.status}</span>
					<span class="detail-amount">{fmtCurrency(invoice.total)}</span>
				</div>
				<div class="detail-top-right">
					<span class="detail-due {invoice.status === 'overdue' ? 'due-overdue' : ''}">
						{dueLabel(invoice)}
					</span>
				</div>
			</div>

			<!-- Metadata grid -->
			<div class="detail-meta">
				<div class="meta-item">
					<span class="meta-label">Customer</span>
					<span class="meta-value">{customer?.name ?? invoice.customer_id.slice(0, 8)}</span>
				</div>
				<div class="meta-item">
					<span class="meta-label">Project</span>
					<span class="meta-value">{invoice.project ?? '--'}</span>
				</div>
				<div class="meta-item">
					<span class="meta-label">Service Period</span>
					<span class="meta-value">
						{fmtDate(invoice.service_period_start)} - {fmtDate(invoice.service_period_end)}
					</span>
				</div>
				<div class="meta-item">
					<span class="meta-label">Payment Terms</span>
					<span class="meta-value">{invoice.payment_terms ?? '--'}</span>
				</div>
				{#if invoice.po_number}
					<div class="meta-item">
						<span class="meta-label">PO #</span>
						<span class="meta-value">{invoice.po_number}</span>
					</div>
				{/if}
				<div class="meta-item">
					<span class="meta-label">Due Date</span>
					<span class="meta-value">{fmtDate(invoice.due_date)}</span>
				</div>
				{#if invoice.submitted_date}
					<div class="meta-item">
						<span class="meta-label">Submitted</span>
						<span class="meta-value">{fmtDate(invoice.submitted_date)}</span>
					</div>
				{/if}
				{#if invoice.paid_date}
					<div class="meta-item">
						<span class="meta-label">Paid</span>
						<span class="meta-value">{fmtDate(invoice.paid_date)}</span>
					</div>
				{/if}
			</div>

			<!-- Line items -->
			{#if invoice.line_items && invoice.line_items.length > 0}
				<div class="line-items-section">
					<h3>Line Items</h3>
					<table class="data-table">
						<thead>
							<tr>
								<th>Description</th>
								<th>Date</th>
								<th>Qty</th>
								<th>Unit Price</th>
								<th>Total</th>
							</tr>
						</thead>
						<tbody>
							{#each invoice.line_items as li (li.id)}
								<tr>
									<td>{li.description}</td>
									<td class="td-date">{fmtDate(li.date)}</td>
									<td class="td-qty">{li.quantity ?? '--'}</td>
									<td class="td-amount">{fmtCurrency(li.unit_price)}</td>
									<td class="td-amount">{fmtCurrency(li.total_price)}</td>
								</tr>
							{/each}
						</tbody>
						<tfoot>
							<tr>
								<td colspan="4">Subtotal</td>
								<td class="td-amount">{fmtCurrency(invoice.subtotal)}</td>
							</tr>
							{#if invoice.adjustments && parseFloat(invoice.adjustments) !== 0}
								<tr>
									<td colspan="4">Adjustments</td>
									<td class="td-amount">{fmtCurrency(invoice.adjustments)}</td>
								</tr>
							{/if}
							{#if invoice.tax && parseFloat(invoice.tax) !== 0}
								<tr>
									<td colspan="4">Tax</td>
									<td class="td-amount">{fmtCurrency(invoice.tax)}</td>
								</tr>
							{/if}
							<tr class="total-row">
								<td colspan="4">Total</td>
								<td class="td-amount td-total">{fmtCurrency(invoice.total)}</td>
							</tr>
						</tfoot>
					</table>
				</div>
			{/if}

			{#if invoice.notes}
				<div class="notes-section">
					<h3>Notes</h3>
					<p>{invoice.notes}</p>
				</div>
			{/if}

			<!-- Action buttons -->
			<div class="action-bar">
				<a
					href={getInvoicePdfUrl(invoice.id)}
					target="_blank"
					rel="noopener"
					class="btn btn-ghost"
					onclick={handlePdfClick}
				>
					{#if pdfLoading}
						<span class="spinner" aria-hidden="true"></span>
					{/if}
					Download PDF
				</a>
				<a
					href={getInvoiceHtmlUrl(invoice.id)}
					target="_blank"
					rel="noopener"
					class="btn btn-ghost"
				>
					HTML Preview
				</a>

				{#if invoice.status === 'draft'}
					<button class="btn btn-primary" onclick={markSent}>Mark Sent</button>
				{/if}
				{#if invoice.status === 'sent' || invoice.status === 'overdue'}
					<button class="btn btn-primary" onclick={markPaid}>Mark Paid</button>
				{/if}
				{#if invoice.status !== 'void'}
					{#if showVoidConfirm}
						<span class="void-confirm">
							Are you sure?
							<button class="btn btn-danger" onclick={voidInvoice}>Yes, Void</button>
							<button class="btn btn-ghost" onclick={() => (showVoidConfirm = false)}>
								Cancel
							</button>
						</span>
					{:else}
						<button class="btn btn-danger" onclick={() => (showVoidConfirm = true)}>
							Void
						</button>
					{/if}
				{/if}
			</div>
		</div>

		<!-- SAP Ariba instructions panel -->
		{#if isSapCustomer}
			<div class="card sap-panel">
				<div class="sap-header">
					<h3 class="sap-title">SAP Ariba Submission Instructions</h3>
					{#if sapSaving}
						<span class="sap-saving">Saving...</span>
					{/if}
				</div>
				<ol class="sap-steps">
					{#each SAP_STEPS as step, i}
						<li class="sap-step" class:sap-step-done={sapChecklist[String(i)]}>
							<label class="sap-label">
								<input
									type="checkbox"
									checked={sapChecklist[String(i)] ?? false}
									onchange={() => toggleSapStep(i)}
								/>
								<span>
									{#if step.includes('PO#')}
										{step.replace('PO# shown below', `PO# ${invoice.po_number}`)}
									{:else if step.includes('Verify amount')}
										Verify amount ({fmtCurrency(invoice.total)})
									{:else if step.includes('invoice number')}
										Enter Sparkry invoice number ({invoice.invoice_number})
									{:else if step.includes('description')}
										{@const lineDesc = invoice.line_items?.[0]?.description ?? ''}
										Update description ("{lineDesc}")
									{:else if step.includes('service period')}
										Update service period ({fmtDate(invoice.service_period_start)} –
										{fmtDate(invoice.service_period_end)})
									{:else}
										{step}
									{/if}
								</span>
							</label>
						</li>
					{/each}
				</ol>
				<div class="sap-footer">
					<button
						class="btn btn-primary"
						disabled={!allSapChecked || invoice.status === 'sent' || invoice.status === 'paid'}
						onclick={markSubmittedInSap}
					>
						Mark as Submitted in SAP
					</button>
					{#if invoice.status === 'sent' || invoice.status === 'paid'}
						<span class="sap-submitted-note">Already submitted</span>
					{/if}
				</div>
			</div>
		{/if}
	{/if}
</div>

<style>
	.page-shell {
		padding-top: 32px;
		padding-bottom: 64px;
	}

	.page-header {
		margin-bottom: 24px;
	}

	.back-link {
		display: inline-block;
		font-size: 0.8rem;
		color: var(--text-muted);
		text-decoration: none;
		margin-bottom: 4px;
	}

	.back-link:hover {
		color: var(--text);
	}

	/* ── Detail card ───────────────────────────────────────────────────── */
	.detail-card {
		padding: 24px;
		display: flex;
		flex-direction: column;
		gap: 24px;
	}

	.detail-top {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 16px;
	}

	.detail-top-left {
		display: flex;
		align-items: center;
		gap: 12px;
	}

	.detail-amount {
		font-size: 1.5rem;
		font-weight: 700;
		font-variant-numeric: tabular-nums;
	}

	.detail-due {
		font-size: 0.875rem;
		color: var(--text-muted);
	}

	.due-overdue {
		color: var(--red-600);
		font-weight: 600;
	}

	.detail-meta {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
		gap: 16px;
	}

	.meta-item {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.meta-label {
		font-size: 0.75rem;
		color: var(--text-muted);
		text-transform: uppercase;
		letter-spacing: 0.04em;
		font-weight: 600;
	}

	.meta-value {
		font-size: 0.875rem;
		color: var(--text);
	}

	/* ── Line items ────────────────────────────────────────────────────── */
	.line-items-section h3 {
		margin-bottom: 8px;
		font-size: 0.95rem;
	}

	.td-date {
		white-space: nowrap;
		color: var(--text-muted);
		font-size: 0.85rem;
	}

	.td-amount {
		font-variant-numeric: tabular-nums;
		text-align: right;
		white-space: nowrap;
	}

	.td-qty {
		text-align: center;
		font-variant-numeric: tabular-nums;
	}

	.td-total {
		font-weight: 700;
		font-size: 1rem;
	}

	.total-row td {
		font-weight: 700;
		border-top: 2px solid var(--border);
	}

	.notes-section h3 {
		margin-bottom: 4px;
		font-size: 0.95rem;
	}

	.notes-section p {
		font-size: 0.875rem;
		color: var(--text);
	}

	/* ── Actions ────────────────────────────────────────────────────────── */
	.action-bar {
		display: flex;
		gap: 8px;
		align-items: center;
		flex-wrap: wrap;
		padding-top: 8px;
		border-top: 1px solid var(--border);
	}

	.void-confirm {
		display: inline-flex;
		align-items: center;
		gap: 8px;
		font-size: 0.85rem;
		color: var(--red-600);
	}

	/* ── Status pills ──────────────────────────────────────────────────── */
	.status-draft {
		background: var(--gray-100);
		color: var(--gray-600);
	}

	.status-sent {
		background: rgba(59, 130, 246, 0.1);
		color: var(--blue-600);
	}

	.status-paid {
		background: var(--green-100);
		color: var(--green-700);
	}

	.status-overdue {
		background: var(--red-100);
		color: var(--red-700);
	}

	.status-void {
		background: var(--gray-100);
		color: var(--gray-500);
		text-decoration: line-through;
	}

	/* ── SAP Panel ─────────────────────────────────────────────────────── */
	.sap-panel {
		padding: 18px 20px;
		margin-top: 16px;
		border-color: var(--blue-500);
	}

	.sap-header {
		display: flex;
		align-items: center;
		gap: 12px;
		margin-bottom: 12px;
	}

	.sap-title {
		margin: 0;
		font-size: 0.95rem;
		color: var(--blue-600);
	}

	.sap-saving {
		font-size: 0.75rem;
		color: var(--text-muted);
	}

	.sap-steps {
		display: flex;
		flex-direction: column;
		gap: 8px;
		padding-left: 0;
		list-style: none;
	}

	.sap-step {
		font-size: 0.875rem;
	}

	.sap-step-done {
		color: var(--text-muted);
		text-decoration: line-through;
	}

	.sap-label {
		display: flex;
		align-items: flex-start;
		gap: 10px;
		cursor: pointer;
	}

	.sap-label input[type='checkbox'] {
		margin-top: 3px;
		flex-shrink: 0;
	}

	.sap-footer {
		display: flex;
		align-items: center;
		gap: 12px;
		margin-top: 16px;
		padding-top: 12px;
		border-top: 1px solid var(--border);
	}

	.sap-submitted-note {
		font-size: 0.8rem;
		color: var(--green-700);
		font-weight: 600;
	}

	/* ── Misc ──────────────────────────────────────────────────────────── */
	.skeleton-card {
		padding: 20px 22px;
	}

	.error-card {
		padding: 24px;
		display: flex;
		flex-direction: column;
		gap: 12px;
		align-items: flex-start;
	}

	.error-msg {
		color: var(--red-600);
		font-size: 0.875rem;
	}

	.spinner {
		display: inline-block;
		width: 12px;
		height: 12px;
		border: 2px solid var(--gray-300);
		border-top-color: var(--gray-700);
		border-radius: 50%;
		animation: spin 0.6s linear infinite;
	}

	@keyframes spin {
		to {
			transform: rotate(360deg);
		}
	}
</style>
