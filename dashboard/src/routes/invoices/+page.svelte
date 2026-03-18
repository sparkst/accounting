<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import type { Invoice, Customer } from '$lib/types';
	import {
		fetchInvoices,
		fetchInvoice,
		fetchCustomers,
		generateFlatInvoice,
		transitionInvoiceStatus,
		getInvoicePdfUrl,
		getInvoiceHtmlUrl,
		patchInvoice
	} from '$lib/api';
	import Toast from '$lib/components/Toast.svelte';

	// ── State ─────────────────────────────────────────────────────────────────
	let invoices = $state<Invoice[]>([]);
	let customers = $state<Customer[]>([]);
	let loading = $state(true);
	let fetchError = $state('');
	let customerFilter = $state('');
	let expandedId = $state<string | null>(null);
	let expandedInvoice = $state<Invoice | null>(null);
	let expandLoading = $state(false);
	let generatingFlat = $state<string | null>(null);

	// Toast state
	let toasts = $state<Array<{ id: number; message: string; type: 'info' | 'success' | 'error' }>>([]);
	let toastCounter = $state(0);

	// Void confirmation
	let voidConfirmId = $state<string | null>(null);

	// ── Derived ───────────────────────────────────────────────────────────────
	let filteredInvoices = $derived(
		customerFilter
			? invoices.filter(inv => inv.customer_id === customerFilter)
			: invoices
	);

	let arSummary = $derived.by(() => {
		const outstanding = invoices.filter(
			inv => inv.status === 'sent' || inv.status === 'overdue'
		);
		const totalAmount = outstanding.reduce(
			(sum, inv) => sum + parseFloat(inv.total ?? '0'), 0
		);
		const maxDays = outstanding.reduce(
			(max, inv) => Math.max(max, inv.days_outstanding ?? 0), 0
		);
		return { count: outstanding.length, totalAmount, maxDays };
	});

	// ── Helpers ───────────────────────────────────────────────────────────────
	function addToast(message: string, type: 'info' | 'success' | 'error' = 'info') {
		const id = ++toastCounter;
		toasts = [...toasts, { id, message, type }];
	}

	function removeToast(id: number) {
		toasts = toasts.filter(t => t.id !== id);
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

	function customerName(id: string): string {
		return customers.find(c => c.id === id)?.name ?? id.slice(0, 8);
	}

	function customerById(id: string): Customer | undefined {
		return customers.find(c => c.id === id);
	}

	function nextMonth(customer?: Customer): string {
		// If customer has a last_invoiced_date, compute the next month after it.
		if (customer?.last_invoiced_date) {
			const last = new Date(customer.last_invoiced_date + 'T00:00:00');
			const nextM = last.getMonth() + 1; // 0-based, +1 = next month
			if (nextM > 11) {
				return `${last.getFullYear() + 1}-01`;
			}
			return `${last.getFullYear()}-${String(nextM + 1).padStart(2, '0')}`;
		}
		// Default: current month (invoicing for the period that just ended)
		const now = new Date();
		return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
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

	// ── Load ─────────────────────────────────────────────────────────────────
	async function load() {
		loading = true;
		fetchError = '';
		try {
			const [invRes, custRes] = await Promise.all([fetchInvoices(), fetchCustomers()]);
			invoices = invRes.items;
			customers = custRes;
		} catch (e) {
			fetchError = e instanceof Error ? e.message : 'Failed to load invoices';
		} finally {
			loading = false;
		}
	}

	onMount(() => { load(); });

	// ── Expand / collapse ────────────────────────────────────────────────────
	async function toggleExpand(inv: Invoice) {
		if (expandedId === inv.id) {
			expandedId = null;
			expandedInvoice = null;
			return;
		}
		expandedId = inv.id;
		expandLoading = true;
		try {
			expandedInvoice = await fetchInvoice(inv.id);
			initSapChecklist(expandedInvoice);
		} catch (e) {
			addToast('Failed to load invoice detail', 'error');
			expandedId = null;
		} finally {
			expandLoading = false;
		}
	}

	// ── Generate flat ────────────────────────────────────────────────────────
	async function handleGenerateFlat(customer: Customer) {
		generatingFlat = customer.id;
		try {
			const month = nextMonth(customer);
			const inv = await generateFlatInvoice(customer.id, month);
			addToast(`Generated invoice ${inv.invoice_number}`, 'success');
			await load();
			// Expand the new invoice and show detail (with SAP instructions)
			expandedId = inv.id;
			expandedInvoice = await fetchInvoice(inv.id);
		} catch (e) {
			const msg = e instanceof Error ? e.message : 'Generation failed';
			addToast(msg, 'error');
		} finally {
			generatingFlat = null;
		}
	}

	// ── Status transitions ───────────────────────────────────────────────────
	async function markSent(inv: Invoice) {
		try {
			await transitionInvoiceStatus(inv.id, 'sent');
			addToast(`Invoice ${inv.invoice_number} marked as sent`, 'success');
			await load();
			if (expandedId === inv.id) {
				expandedInvoice = await fetchInvoice(inv.id);
			}
		} catch (e) {
			addToast(e instanceof Error ? e.message : 'Failed', 'error');
		}
	}

	async function markPaid(inv: Invoice) {
		try {
			await transitionInvoiceStatus(inv.id, 'paid');
			addToast(`Invoice ${inv.invoice_number} marked as paid`, 'success');
			await load();
			if (expandedId === inv.id) {
				expandedInvoice = await fetchInvoice(inv.id);
			}
		} catch (e) {
			addToast(e instanceof Error ? e.message : 'Failed', 'error');
		}
	}

	async function voidInvoice(inv: Invoice) {
		try {
			await transitionInvoiceStatus(inv.id, 'void');
			addToast(`Invoice ${inv.invoice_number} voided`, 'success');
			voidConfirmId = null;
			await load();
			if (expandedId === inv.id) {
				expandedInvoice = await fetchInvoice(inv.id);
			}
		} catch (e) {
			addToast(e instanceof Error ? e.message : 'Failed', 'error');
		}
	}

	// ── SAP Instructions ─────────────────────────────────────────────────────
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

	function initSapChecklist(inv?: Invoice | null) {
		const saved = inv?.sap_checklist_state as Record<string, boolean> | null;
		sapChecklist = {};
		SAP_STEPS.forEach((_, i) => {
			sapChecklist[String(i)] = saved ? (saved[String(i)] ?? false) : false;
		});
	}

	async function toggleSapStep(index: number) {
		sapChecklist = { ...sapChecklist, [String(index)]: !sapChecklist[String(index)] };
		// Persist to backend
		if (expandedInvoice) {
			try {
				await patchInvoice(expandedInvoice.id, { sap_checklist_state: sapChecklist });
			} catch {
				// silently ignore — local state is still updated
			}
		}
	}

	let allSapChecked = $derived(
		SAP_STEPS.every((_, i) => sapChecklist[String(i)])
	);
</script>

<!-- Toasts -->
{#each toasts as toast (toast.id)}
	<Toast
		message={toast.message}
		type={toast.type}
		ondismiss={() => removeToast(toast.id)}
	/>
{/each}

<div class="container page-shell">
	<header class="page-header">
		<div>
			<h1>Invoices</h1>
			{#if !loading}
				<p class="page-subtitle">
					{invoices.length} invoice{invoices.length !== 1 ? 's' : ''}
				</p>
			{/if}
		</div>
		<div class="page-header-actions">
			<button class="btn btn-ghost" onclick={load} disabled={loading}>
				{loading ? 'Loading...' : 'Refresh'}
			</button>
			<a href="/invoices/new" class="btn btn-primary">+ New Invoice</a>
		</div>
	</header>

	{#if loading}
		<div class="skeleton-list">
			{#each Array(3) as _}
				<div class="card skeleton-card">
					<div class="skeleton" style="height: 16px; width: 30%; margin-bottom: 10px;"></div>
					<div class="skeleton" style="height: 24px; width: 50%; margin-bottom: 10px;"></div>
					<div class="skeleton" style="height: 16px; width: 40%;"></div>
				</div>
			{/each}
		</div>
	{:else if fetchError}
		<div class="card error-card">
			<p class="error-msg">{fetchError}</p>
			<button class="btn btn-ghost" onclick={load}>Try again</button>
		</div>
	{:else if invoices.length === 0 && customers.length === 0}
		<div class="empty-state">
			<span class="icon">$</span>
			<h2>No invoices yet</h2>
			<p>Create your first invoice.</p>
			<a href="/invoices/new" class="btn btn-primary">+ New Invoice</a>
		</div>
	{:else}
		<!-- AR Aging summary -->
		{#if arSummary.count > 0}
			<div class="card ar-card">
				<div class="ar-content">
					<span class="ar-label">
						{arSummary.count} invoice{arSummary.count !== 1 ? 's' : ''} outstanding:
					</span>
					<span class="ar-amount">{fmtCurrency(String(arSummary.totalAmount))}</span>
					{#if arSummary.maxDays > 0}
						<span class="ar-aging">({arSummary.maxDays} days oldest)</span>
					{/if}
				</div>
			</div>
		{/if}

		<!-- Filter bar -->
		<div class="filter-bar card">
			<select bind:value={customerFilter} aria-label="Filter by customer">
				<option value="">All customers</option>
				{#each customers as c (c.id)}
					<option value={c.id}>{c.name}</option>
				{/each}
			</select>
		</div>

		{#if filteredInvoices.length === 0}
			<div class="empty-state">
				<span class="icon">$</span>
				<h2>No invoices yet</h2>
				<p>Create your first invoice.</p>
				<a href="/invoices/new" class="btn btn-primary">+ New Invoice</a>
			</div>
		{:else}
			<!-- Invoice table -->
			<div class="card table-wrap">
				<table class="data-table">
					<thead>
						<tr>
							<th>#</th>
							<th>Date</th>
							<th>Customer</th>
							<th>Amount</th>
							<th>Status</th>
							<th>Due</th>
						</tr>
					</thead>
					<tbody>
						{#each filteredInvoices as inv (inv.id)}
							<tr
								class="inv-row"
								class:row-expanded={expandedId === inv.id}
								onclick={() => toggleExpand(inv)}
							>
								<td class="td-number">{inv.invoice_number}</td>
								<td class="td-date">{fmtDate(inv.service_period_start)}</td>
								<td class="td-customer">{customerName(inv.customer_id)}</td>
								<td class="td-amount">{fmtCurrency(inv.total)}</td>
								<td>
									<span class="status-pill {statusClass(inv.status)}">
										{inv.status}
									</span>
								</td>
								<td class="td-due">{dueLabel(inv)}</td>
							</tr>
							{#if expandedId === inv.id}
								<tr class="detail-row">
									<td colspan="6">
										{#if expandLoading}
											<div class="detail-loading">
												<div class="skeleton" style="height: 20px; width: 60%; margin-bottom: 8px;"></div>
												<div class="skeleton" style="height: 16px; width: 40%;"></div>
											</div>
										{:else if expandedInvoice}
											<div class="detail-panel">
												<!-- Invoice metadata -->
												<div class="detail-meta">
													<div class="meta-col">
														<div class="meta-item">
															<span class="meta-label">Invoice #</span>
															<span class="meta-value">{expandedInvoice.invoice_number}</span>
														</div>
														<div class="meta-item">
															<span class="meta-label">Project</span>
															<span class="meta-value">{expandedInvoice.project ?? '--'}</span>
														</div>
														<div class="meta-item">
															<span class="meta-label">Service Period</span>
															<span class="meta-value">
																{fmtDate(expandedInvoice.service_period_start)} - {fmtDate(expandedInvoice.service_period_end)}
															</span>
														</div>
													</div>
													<div class="meta-col">
														<div class="meta-item">
															<span class="meta-label">Payment Terms</span>
															<span class="meta-value">{expandedInvoice.payment_terms ?? '--'}</span>
														</div>
														{#if expandedInvoice.po_number}
															<div class="meta-item">
																<span class="meta-label">PO #</span>
																<span class="meta-value">{expandedInvoice.po_number}</span>
															</div>
														{/if}
														<div class="meta-item">
															<span class="meta-label">{dueLabel(expandedInvoice)}</span>
															<span class="meta-value">{fmtDate(expandedInvoice.due_date)}</span>
														</div>
													</div>
												</div>

												<!-- Line items -->
												{#if expandedInvoice.line_items && expandedInvoice.line_items.length > 0}
													<table class="data-table line-items-table">
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
															{#each expandedInvoice.line_items as li (li.id)}
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
																<td class="td-amount">{fmtCurrency(expandedInvoice.subtotal)}</td>
															</tr>
															{#if expandedInvoice.adjustments && parseFloat(expandedInvoice.adjustments) !== 0}
																<tr>
																	<td colspan="4">Adjustments</td>
																	<td class="td-amount">{fmtCurrency(expandedInvoice.adjustments)}</td>
																</tr>
															{/if}
															<tr class="total-row">
																<td colspan="4">Total</td>
																<td class="td-amount td-total">{fmtCurrency(expandedInvoice.total)}</td>
															</tr>
														</tfoot>
													</table>
												{/if}

												{#if expandedInvoice.notes}
													<div class="detail-notes">
														<span class="meta-label">Notes</span>
														<p>{expandedInvoice.notes}</p>
													</div>
												{/if}

												<!-- Action buttons -->
												<div class="detail-actions">
													<a
														href={getInvoicePdfUrl(expandedInvoice.id)}
														target="_blank"
														rel="noopener"
														class="btn btn-ghost"
													>
														PDF
													</a>
													<a
														href={getInvoiceHtmlUrl(expandedInvoice.id)}
														target="_blank"
														rel="noopener"
														class="btn btn-ghost"
													>
														HTML Preview
													</a>

													{#if expandedInvoice.status === 'draft'}
														<button class="btn btn-primary" onclick={() => markSent(expandedInvoice!)}>
															Mark Sent
														</button>
													{/if}
													{#if expandedInvoice.status === 'sent' || expandedInvoice.status === 'overdue'}
														<button class="btn btn-primary" onclick={() => markPaid(expandedInvoice!)}>
															Mark Paid
														</button>
													{/if}
													{#if expandedInvoice.status !== 'void'}
														{#if voidConfirmId === expandedInvoice.id}
															<span class="void-confirm">
																Are you sure?
																<button class="btn btn-danger" onclick={() => voidInvoice(expandedInvoice!)}>
																	Yes, Void
																</button>
																<button class="btn btn-ghost" onclick={() => (voidConfirmId = null)}>
																	Cancel
																</button>
															</span>
														{:else}
															<button class="btn btn-danger" onclick={() => (voidConfirmId = expandedInvoice!.id)}>
																Void
															</button>
														{/if}
													{/if}
												</div>

												<!-- SAP Ariba instructions for Cardinal Health -->
												{#if expandedInvoice.po_number && customerById(expandedInvoice.customer_id)?.billing_model === 'flat_rate'}
													<div class="sap-panel card">
														<h3 class="sap-title">SAP Ariba Submission</h3>
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
																				{step.replace('PO# shown below', `PO# ${expandedInvoice.po_number}`)}
																			{:else if step.includes('Verify amount')}
																				Verify amount ({fmtCurrency(expandedInvoice.total)})
																			{:else if step.includes('invoice number')}
																				Enter Sparkry invoice number ({expandedInvoice.invoice_number})
																			{:else if step.includes('description')}
																				{@const lineDesc = expandedInvoice.line_items?.[0]?.description ?? ''}
																				Update description ("{lineDesc}")
																			{:else if step.includes('service period')}
																				Update service period ({fmtDate(expandedInvoice.service_period_start)} - {fmtDate(expandedInvoice.service_period_end)})
																			{:else}
																				{step}
																			{/if}
																		</span>
																	</label>
																</li>
															{/each}
														</ol>
														{#if allSapChecked}
															<div class="sap-complete">
																All steps completed - invoice submitted in SAP Ariba
															</div>
														{/if}
													</div>
												{/if}
											</div>
										{/if}
									</td>
								</tr>
							{/if}
						{/each}
					</tbody>
				</table>
			</div>
		{/if}

		<!-- Generate Next Invoice buttons per customer -->
		{#if customers.length > 0}
			<section class="generate-section">
				<h2 class="section-title">Generate Next Invoice</h2>
				<div class="generate-grid">
					{#each customers.filter(c => c.active) as customer (customer.id)}
						<div class="card generate-card">
							<div class="generate-info">
								<span class="generate-name">{customer.name}</span>
								<span class="generate-model">
									{customer.billing_model === 'flat_rate' ? 'Flat rate' : 'Hourly'} -
									{customer.default_rate ? fmtCurrency(customer.default_rate) : '--'}
									{customer.billing_model === 'flat_rate' ? '/mo' : '/hr'}
								</span>
							</div>
							{#if customer.billing_model === 'flat_rate'}
								<button
									class="btn btn-primary"
									disabled={generatingFlat === customer.id}
									onclick={() => handleGenerateFlat(customer)}
								>
									{#if generatingFlat === customer.id}
										<span class="spinner" aria-hidden="true"></span>
										Generating...
									{:else}
										Generate {nextMonth(customer)}
									{/if}
								</button>
							{:else}
								<a
									href="/invoices/new?customer={customer.id}"
									class="btn btn-primary"
								>
									Create Invoice
								</a>
							{/if}
						</div>
					{/each}
				</div>
			</section>
		{/if}
	{/if}
</div>

<style>
	.page-shell {
		padding-top: 32px;
		padding-bottom: 64px;
	}

	.page-header {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: 16px;
		margin-bottom: 24px;
	}

	.page-subtitle {
		margin-top: 4px;
		color: var(--text-muted);
		font-size: .9rem;
	}

	.page-header-actions {
		display: flex;
		gap: 8px;
		align-items: center;
		flex-shrink: 0;
	}

	/* ── AR Summary ─────────────────────────────────────────────────────── */
	.ar-card {
		padding: 14px 20px;
		margin-bottom: 12px;
		background: #fffbeb;
		border-color: var(--amber-500);
	}

	.ar-content {
		display: flex;
		align-items: baseline;
		gap: 8px;
		flex-wrap: wrap;
	}

	.ar-label {
		font-size: .875rem;
		font-weight: 500;
		color: var(--amber-700);
	}

	.ar-amount {
		font-size: 1.1rem;
		font-weight: 700;
		color: var(--amber-700);
		font-variant-numeric: tabular-nums;
	}

	.ar-aging {
		font-size: .8rem;
		color: var(--amber-600);
	}

	/* ── Filter bar ─────────────────────────────────────────────────────── */
	.filter-bar {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 10px 16px;
		margin-bottom: 12px;
	}

	/* ── Table ──────────────────────────────────────────────────────────── */
	.table-wrap {
		overflow-x: auto;
		margin-bottom: 24px;
	}

	.inv-row {
		cursor: pointer;
		transition: background .1s;
	}

	.inv-row:hover td {
		background: var(--gray-50);
	}

	.row-expanded td {
		background: var(--gray-50);
		border-bottom-color: transparent;
	}

	.td-number {
		font-weight: 600;
		font-family: var(--font-mono);
		font-size: .8rem;
		white-space: nowrap;
	}

	.td-date {
		white-space: nowrap;
		color: var(--text-muted);
		font-size: .85rem;
	}

	.td-customer {
		font-weight: 500;
	}

	.td-amount {
		font-variant-numeric: tabular-nums;
		text-align: right;
		white-space: nowrap;
	}

	.td-due {
		font-size: .8rem;
		color: var(--text-muted);
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

	/* ── Status pills ──────────────────────────────────────────────────── */
	.status-draft {
		background: var(--gray-100);
		color: var(--gray-600);
	}

	.status-sent {
		background: rgba(59,130,246,.1);
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

	/* ── Detail panel ──────────────────────────────────────────────────── */
	.detail-row td {
		padding: 0;
		background: var(--gray-50);
	}

	.detail-panel {
		padding: 20px 24px;
		display: flex;
		flex-direction: column;
		gap: 20px;
	}

	.detail-loading {
		padding: 20px 24px;
	}

	.detail-meta {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 16px;
	}

	.meta-col {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.meta-item {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.meta-label {
		font-size: .75rem;
		color: var(--text-muted);
		text-transform: uppercase;
		letter-spacing: .04em;
		font-weight: 600;
	}

	.meta-value {
		font-size: .875rem;
		color: var(--text);
	}

	.line-items-table {
		font-size: .85rem;
	}

	.detail-notes {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.detail-notes p {
		font-size: .875rem;
		color: var(--text);
	}

	.detail-actions {
		display: flex;
		gap: 8px;
		align-items: center;
		flex-wrap: wrap;
	}

	.void-confirm {
		display: inline-flex;
		align-items: center;
		gap: 8px;
		font-size: .85rem;
		color: var(--red-600);
	}

	/* ── SAP Panel ─────────────────────────────────────────────────────── */
	.sap-panel {
		padding: 18px 20px;
		background: var(--surface);
		border-color: var(--blue-500);
	}

	.sap-title {
		margin-bottom: 12px;
		font-size: .95rem;
		color: var(--blue-600);
	}

	.sap-steps {
		display: flex;
		flex-direction: column;
		gap: 8px;
		padding-left: 0;
		list-style: none;
		counter-reset: sap;
	}

	.sap-step {
		counter-increment: sap;
		font-size: .875rem;
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

	.sap-label input[type="checkbox"] {
		margin-top: 3px;
		flex-shrink: 0;
	}

	.sap-complete {
		margin-top: 12px;
		padding: 10px 14px;
		background: var(--green-100);
		color: var(--green-700);
		border-radius: var(--radius-sm);
		font-size: .85rem;
		font-weight: 600;
	}

	/* ── Generate section ──────────────────────────────────────────────── */
	.generate-section {
		margin-top: 12px;
	}

	.section-title {
		margin-bottom: 14px;
		font-size: 1rem;
		font-weight: 600;
		color: var(--text);
	}

	.generate-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
		gap: 12px;
	}

	.generate-card {
		padding: 16px 20px;
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 16px;
	}

	.generate-info {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.generate-name {
		font-weight: 600;
		font-size: .9rem;
	}

	.generate-model {
		font-size: .8rem;
		color: var(--text-muted);
	}

	/* ── Skeletons ─────────────────────────────────────────────────────── */
	.skeleton-list {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

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
		font-size: .875rem;
	}

	/* ── Spinner ───────────────────────────────────────────────────────── */
	.spinner {
		display: inline-block;
		width: 12px;
		height: 12px;
		border: 2px solid var(--gray-300);
		border-top-color: #fff;
		border-radius: 50%;
		animation: spin 0.6s linear infinite;
	}

	@keyframes spin {
		to { transform: rotate(360deg); }
	}
</style>
