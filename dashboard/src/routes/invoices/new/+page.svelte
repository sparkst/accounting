<script lang="ts">
	import { onMount } from 'svelte';
	import { page } from '$app/stores';
	import type { Customer, CalendarSession, Invoice } from '$lib/types';
	import {
		fetchCustomers,
		generateFlatInvoice,
		generateCalendarInvoice,
		uploadIcal,
		fetchInvoice,
		patchInvoice,
		getInvoicePdfUrl,
		getInvoiceHtmlUrl,
		sendInvoice
	} from '$lib/api';
	import Toast from '$lib/components/Toast.svelte';

	// ── State ─────────────────────────────────────────────────────────────────
	let customers = $state<Customer[]>([]);
	let selectedCustomerId = $state('');
	let loading = $state(true);
	let fetchError = $state('');

	// Toast state
	let toasts = $state<Array<{ id: number; message: string; type: 'info' | 'success' | 'error' }>>([]);
	let toastCounter = $state(0);

	// Flat-rate state
	let flatMonth = $state('');
	let flatGenerating = $state(false);
	let generatedInvoice = $state<Invoice | null>(null);

	// Calendar state
	let calendarStep = $state(1); // 1=upload, 2=pick sessions, 3=review
	let billingMonth = $state('');
	let icalFile = $state<File | null>(null);
	let icalParsing = $state(false);
	let matchedSessions = $state<Array<CalendarSession & { selected: boolean; alreadyBilled?: string }>>([]);
	let unmatchedEvents = $state<Array<Record<string, unknown>>>([]);
	let showUnmatched = $state(false);
	let sessionRate = $state(100);
	let calendarGenerating = $state(false);

	// Calendar step 3
	let calInvoiceProject = $state('');
	let calInvoiceNotes = $state('');

	// Send state
	let sendEmail = $state('');
	let sending = $state(false);
	let sendSuccess = $state(false);
	let sendError = $state('');
	let showSendConfirm = $state(false);
	let linkCopied = $state(false);

	// ── Derived ───────────────────────────────────────────────────────────────
	let selectedCustomer = $derived(
		customers.find(c => c.id === selectedCustomerId) ?? null
	);

	let isFlat = $derived(selectedCustomer?.billing_model === 'flat_rate');
	let isCalendar = $derived(selectedCustomer?.billing_model === 'hourly');

	let selectedSessions = $derived(
		matchedSessions.filter(s => s.selected && !s.alreadyBilled)
	);

	let liveSubtotal = $derived(
		selectedSessions.reduce((sum, s) => sum + s.hours * sessionRate, 0)
	);

	// SAP instructions
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
	let allSapChecked = $derived(
		SAP_STEPS.every((_, i) => sapChecklist[String(i)])
	);

	// ── Helpers ───────────────────────────────────────────────────────────────
	function addToast(message: string, type: 'info' | 'success' | 'error' = 'info') {
		const id = ++toastCounter;
		toasts = [...toasts, { id, message, type }];
	}

	function removeToast(id: number) {
		toasts = toasts.filter(t => t.id !== id);
	}

	function fmtCurrency(val: string | number | null): string {
		if (val === null) return '$0.00';
		const n = typeof val === 'number' ? val : parseFloat(val);
		return n.toLocaleString('en-US', { style: 'currency', currency: 'USD' });
	}

	function fmtDate(iso: string | null): string {
		if (!iso) return '--';
		const d = new Date(iso + 'T00:00:00');
		return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
	}

	function defaultMonth(): string {
		const now = new Date();
		const y = now.getMonth() === 11 ? now.getFullYear() + 1 : now.getFullYear();
		const m = now.getMonth() === 11 ? 1 : now.getMonth() + 2;
		return `${y}-${String(m).padStart(2, '0')}`;
	}

	function previousMonth(): string {
		const now = new Date();
		const y = now.getMonth() === 0 ? now.getFullYear() - 1 : now.getFullYear();
		const m = now.getMonth() === 0 ? 12 : now.getMonth();
		return `${y}-${String(m).padStart(2, '0')}`;
	}

	function monthDateRange(ym: string): { start: string; end: string } {
		const [y, m] = ym.split('-').map(Number);
		const start = `${y}-${String(m).padStart(2, '0')}-01`;
		const lastDay = new Date(y, m, 0).getDate();
		const end = `${y}-${String(m).padStart(2, '0')}-${String(lastDay).padStart(2, '0')}`;
		return { start, end };
	}

	function toggleSapStep(index: number) {
		sapChecklist = { ...sapChecklist, [String(index)]: !sapChecklist[String(index)] };
	}

	// ── Load ─────────────────────────────────────────────────────────────────
	onMount(async () => {
		try {
			customers = await fetchCustomers();
			// Pre-select customer from query param
			const qc = $page.url.searchParams.get('customer');
			if (qc) {
				const match = customers.find(
					c => c.id === qc || c.name.toLowerCase().includes(qc.toLowerCase())
				);
				if (match) selectedCustomerId = match.id;
			}
			flatMonth = defaultMonth();
		} catch (e) {
			fetchError = e instanceof Error ? e.message : 'Failed to load customers';
		} finally {
			loading = false;
		}
	});

	// ── Flat-rate generate ───────────────────────────────────────────────────
	async function handleGenerateFlat() {
		if (!selectedCustomerId || !flatMonth) return;
		flatGenerating = true;
		try {
			const inv = await generateFlatInvoice(selectedCustomerId, flatMonth);
			generatedInvoice = await fetchInvoice(inv.id);
			sendEmail = selectedCustomer?.contact_email ?? '';
			sendSuccess = false;
			sendError = '';
			showSendConfirm = false;
			addToast(`Generated invoice ${inv.invoice_number}`, 'success');
			// Initialize SAP checklist
			sapChecklist = {};
			SAP_STEPS.forEach((_, i) => { sapChecklist[String(i)] = false; });
		} catch (e) {
			addToast(e instanceof Error ? e.message : 'Generation failed', 'error');
		} finally {
			flatGenerating = false;
		}
	}

	// ── iCal upload ──────────────────────────────────────────────────────────
	function handleFileDrop(e: DragEvent) {
		e.preventDefault();
		const files = e.dataTransfer?.files;
		if (files && files.length > 0) {
			const f = files[0];
			if (f.name.endsWith('.ics') || f.name.endsWith('.ical')) {
				icalFile = f;
			} else {
				addToast('Please upload a .ics file', 'error');
			}
		}
	}

	function handleFileSelect(e: Event) {
		const input = e.target as HTMLInputElement;
		if (input.files && input.files.length > 0) {
			icalFile = input.files[0];
		}
	}

	async function parseIcal() {
		if (!icalFile || !selectedCustomerId) return;
		icalParsing = true;
		try {
			const { start, end } = monthDateRange(billingMonth);
			const result = await uploadIcal(icalFile, selectedCustomerId, start, end);
			matchedSessions = result.matched_sessions.map(s => ({
				...s,
				selected: true,
				alreadyBilled: undefined
			}));
			unmatchedEvents = result.unmatched_events;
			if (selectedCustomer?.default_rate) {
				sessionRate = parseFloat(selectedCustomer.default_rate);
			}
			if (selectedCustomer) {
				calInvoiceProject = selectedCustomer.name;
			}
			calendarStep = 2;

			if (result.warnings.length > 0) {
				result.warnings.forEach(w => addToast(w, 'info'));
			}
		} catch (e) {
			addToast(e instanceof Error ? e.message : 'iCal parsing failed', 'error');
		} finally {
			icalParsing = false;
		}
	}

	// ── Calendar generate ────────────────────────────────────────────────────
	async function handleGenerateCalendar() {
		if (!selectedCustomerId || selectedSessions.length === 0) return;
		calendarGenerating = true;
		try {
			const sessions: CalendarSession[] = selectedSessions.map(s => ({
				date: s.date,
				description: s.description,
				hours: s.hours,
				rate: sessionRate
			}));
			const inv = await generateCalendarInvoice(selectedCustomerId, sessions);
			generatedInvoice = await fetchInvoice(inv.id);
			sendEmail = selectedCustomer?.contact_email ?? '';
			sendSuccess = false;
			sendError = '';
			showSendConfirm = false;
			addToast(`Generated invoice ${inv.invoice_number}`, 'success');
		} catch (e) {
			addToast(e instanceof Error ? e.message : 'Generation failed', 'error');
		} finally {
			calendarGenerating = false;
		}
	}
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
			<h1>New Invoice</h1>
			<p class="page-subtitle">Select a customer and generate an invoice</p>
		</div>
		<div class="page-header-actions">
			<a href="/invoices" class="btn btn-ghost">Back to Invoices</a>
		</div>
	</header>

	{#if loading}
		<div class="card skeleton-card">
			<div class="skeleton" style="height: 20px; width: 40%; margin-bottom: 12px;"></div>
			<div class="skeleton" style="height: 36px; width: 60%;"></div>
		</div>
	{:else if fetchError}
		<div class="card error-card">
			<p class="error-msg">{fetchError}</p>
		</div>
	{:else if generatedInvoice}
		<!-- Show generated invoice result -->
		<div class="card generated-card">
			<div class="generated-header">
				<h2>Invoice Generated</h2>
				<span class="status-pill status-{generatedInvoice.status}">{generatedInvoice.status}</span>
			</div>

			<div class="generated-meta">
				<div class="meta-item">
					<span class="meta-label">Invoice #</span>
					<span class="meta-value">{generatedInvoice.invoice_number}</span>
				</div>
				<div class="meta-item">
					<span class="meta-label">Amount</span>
					<span class="meta-value meta-value-large">{fmtCurrency(generatedInvoice.total)}</span>
				</div>
				<div class="meta-item">
					<span class="meta-label">Service Period</span>
					<span class="meta-value">
						{fmtDate(generatedInvoice.service_period_start)} - {fmtDate(generatedInvoice.service_period_end)}
					</span>
				</div>
				{#if generatedInvoice.po_number}
					<div class="meta-item">
						<span class="meta-label">PO #</span>
						<span class="meta-value">{generatedInvoice.po_number}</span>
					</div>
				{/if}
				<div class="meta-item">
					<label for="submitted-date" class="meta-label">Submitted Date</label>
					<input
						id="submitted-date"
						type="date"
						value={generatedInvoice.submitted_date ?? ''}
						class="form-input form-input-sm"
						onchange={async (e) => {
							const val = (e.target as HTMLInputElement).value;
							await patchInvoice(generatedInvoice!.id, { submitted_date: val });
							generatedInvoice = await fetchInvoice(generatedInvoice!.id);
						}}
					/>
				</div>
				<div class="meta-item">
					<label for="due-date" class="meta-label">Due Date</label>
					<input
						id="due-date"
						type="date"
						value={generatedInvoice.due_date ?? ''}
						class="form-input form-input-sm"
						onchange={async (e) => {
							const val = (e.target as HTMLInputElement).value;
							await patchInvoice(generatedInvoice!.id, { due_date: val });
							generatedInvoice = await fetchInvoice(generatedInvoice!.id);
						}}
					/>
				</div>
			</div>

			{#if generatedInvoice.line_items && generatedInvoice.line_items.length > 0}
				<table class="data-table line-items-table">
					<thead>
						<tr>
							<th>Description</th>
							<th>Qty</th>
							<th>Unit Price</th>
							<th>Total</th>
						</tr>
					</thead>
					<tbody>
						{#each generatedInvoice.line_items as li (li.id)}
							<tr>
								<td>{li.description}</td>
								<td class="td-qty">{li.quantity ?? '--'}</td>
								<td class="td-amount">{fmtCurrency(li.unit_price)}</td>
								<td class="td-amount">{fmtCurrency(li.total_price)}</td>
							</tr>
						{/each}
					</tbody>
					<tfoot>
						<tr>
							<td colspan="3">Total</td>
							<td class="td-amount td-total">{fmtCurrency(generatedInvoice.total)}</td>
						</tr>
					</tfoot>
				</table>
			{/if}

			<div class="generated-actions">
				<a href={getInvoicePdfUrl(generatedInvoice.id)} target="_blank" rel="noopener" class="btn btn-ghost">
					Download PDF
				</a>
				<a href={getInvoiceHtmlUrl(generatedInvoice.id)} target="_blank" rel="noopener" class="btn btn-ghost">
					HTML Preview
				</a>
				<a href="/invoices" class="btn btn-primary">View All Invoices</a>
			</div>

			<!-- Send Invoice section -->
			{#if generatedInvoice.status !== 'paid' && generatedInvoice.status !== 'void' && !generatedInvoice.po_number}
				<div class="send-section card">
					<h3 class="send-title">
						{generatedInvoice.sent_at ? 'Resend Invoice' : 'Send Invoice'}
					</h3>

					{#if generatedInvoice.sent_at}
						<p class="send-sent-info">
							Sent to {generatedInvoice.sent_to} on {new Date(generatedInvoice.sent_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })}
						</p>
					{/if}

					{#if sendSuccess}
						<div class="send-success">
							<p>Invoice sent to {generatedInvoice.sent_to}</p>
							{#if generatedInvoice.payment_link_url}
								<div class="payment-link-row">
									<span class="payment-link-label">Payment link:</span>
									<a href={generatedInvoice.payment_link_url} target="_blank" rel="noopener" class="payment-link-url">
										{generatedInvoice.payment_link_url}
									</a>
									<button
										class="btn btn-ghost btn-sm"
										onclick={async () => {
											try {
												await navigator.clipboard.writeText(generatedInvoice!.payment_link_url!);
												linkCopied = true;
												setTimeout(() => linkCopied = false, 2000);
											} catch {
												/* clipboard unavailable */
											}
										}}
									>
										{linkCopied ? 'Copied!' : 'Copy'}
									</button>
								</div>
							{/if}
							<button class="btn btn-ghost btn-sm" onclick={() => { sendSuccess = false; showSendConfirm = false; }}>
								Resend to a different address
							</button>
						</div>
					{:else}
						<div class="send-form">
							<div class="send-email-row">
								<label for="send-email" class="meta-label">Recipient Email</label>
								<input
									id="send-email"
									type="email"
									bind:value={sendEmail}
									placeholder={selectedCustomer?.contact_email || 'Enter email address'}
									class="form-input"
									disabled={sending}
								/>
								{#if !sendEmail && !selectedCustomer?.contact_email}
									<span class="send-helper">No contact email on file — enter a recipient address</span>
								{/if}
							</div>

							{#if sendError}
								<p class="error-msg">{sendError}</p>
							{/if}

							{#if showSendConfirm}
								<div class="send-confirm">
									<p>Send invoice for {fmtCurrency(generatedInvoice.total)} to <strong>{sendEmail || selectedCustomer?.contact_email}</strong>?</p>
									<div class="send-confirm-actions">
										<button
											class="btn btn-primary"
											disabled={sending}
											onclick={async () => {
												sending = true;
												sendError = '';
												try {
													const emailToSend = sendEmail || selectedCustomer?.contact_email || undefined;
													const result = await sendInvoice(generatedInvoice!.id, emailToSend);
													generatedInvoice = result.invoice;
													sendSuccess = true;
													showSendConfirm = false;
													addToast(result.message, 'success');
												} catch (err) {
													sendError = err instanceof Error ? err.message : 'Send failed';
													addToast(sendError, 'error');
												} finally {
													sending = false;
												}
											}}
										>
											{sending ? 'Sending...' : 'Yes, Send'}
										</button>
										<button class="btn btn-ghost" onclick={() => showSendConfirm = false} disabled={sending}>
											Cancel
										</button>
									</div>
								</div>
							{:else}
								<button
									class="btn btn-primary"
									disabled={sending || (!sendEmail && !selectedCustomer?.contact_email)}
									onclick={() => showSendConfirm = true}
								>
									{generatedInvoice.sent_at ? 'Resend Invoice' : 'Send Invoice'}
								</button>
							{/if}
						</div>
					{/if}
				</div>
			{/if}

			<!-- SAP Ariba instructions for flat-rate (Cardinal Health) -->
			{#if generatedInvoice.po_number && selectedCustomer?.billing_model === 'flat_rate'}
				<div class="sap-panel card">
					<h3 class="sap-title">SAP Ariba Submission Instructions</h3>
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
											{step.replace('PO# shown below', `PO# ${generatedInvoice.po_number}`)}
										{:else if step.includes('Verify amount')}
											Verify amount ({fmtCurrency(generatedInvoice.total)})
										{:else if step.includes('invoice number')}
											Enter Sparkry invoice number ({generatedInvoice.invoice_number})
										{:else if step.includes('description')}
											{@const lineDesc = generatedInvoice.line_items?.[0]?.description ?? ''}
											Update description ("{lineDesc}")
										{:else if step.includes('service period')}
											Update service period ({fmtDate(generatedInvoice.service_period_start)} - {fmtDate(generatedInvoice.service_period_end)})
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
	{:else}
		<!-- Customer selector -->
		<div class="customer-selector">
			<h2 class="section-title">Select Customer</h2>
			<div class="customer-grid">
				{#each customers.filter(c => c.active) as customer (customer.id)}
					<button
						class="card customer-card"
						class:customer-card-selected={selectedCustomerId === customer.id}
						onclick={() => {
							selectedCustomerId = customer.id;
							// Reset wizard state
							calendarStep = 1;
							icalFile = null;
							matchedSessions = [];
							unmatchedEvents = [];
							generatedInvoice = null;
							billingMonth = previousMonth();
							if (customer.default_rate) sessionRate = parseFloat(customer.default_rate);
							calInvoiceProject = customer.name;
						}}
					>
						<span class="customer-card-name">{customer.name}</span>
						<span class="customer-card-model">
							{customer.billing_model === 'flat_rate' ? 'Flat rate' : 'Hourly'} -
							{customer.default_rate ? fmtCurrency(customer.default_rate) : '--'}
							{customer.billing_model === 'flat_rate' ? '/mo' : '/hr'}
						</span>
						{#if customer.last_invoiced_date}
							<span class="customer-card-last">Last invoice: {fmtDate(customer.last_invoiced_date)}</span>
						{/if}
					</button>
				{/each}
			</div>
		</div>

		{#if selectedCustomer}
			<!-- ── Flat-rate flow ──────────────────────────────────────────────── -->
			{#if isFlat}
				<div class="card flow-card">
					<h2 class="flow-title">Flat-Rate Invoice</h2>
					<div class="flat-form">
						<div class="form-field">
							<label for="flat-month" class="form-label">Month</label>
							<input
								id="flat-month"
								type="month"
								bind:value={flatMonth}
								class="form-input"
							/>
						</div>
						<div class="flat-preview">
							<div class="meta-item">
								<span class="meta-label">Amount</span>
								<span class="meta-value meta-value-large">{fmtCurrency(selectedCustomer.default_rate)}</span>
							</div>
							{#if selectedCustomer.po_number}
								<div class="meta-item">
									<span class="meta-label">PO #</span>
									<span class="meta-value">{selectedCustomer.po_number}</span>
								</div>
							{/if}
							<div class="meta-item">
								<span class="meta-label">Payment Terms</span>
								<span class="meta-value">{selectedCustomer.payment_terms ?? '--'}</span>
							</div>
						</div>
						<button
							class="btn btn-primary"
							disabled={flatGenerating || !flatMonth}
							onclick={handleGenerateFlat}
						>
							{#if flatGenerating}
								<span class="spinner" aria-hidden="true"></span>
								Generating...
							{:else}
								Generate Invoice
							{/if}
						</button>
					</div>
				</div>

			<!-- ── Calendar flow ───────────────────────────────────────────────── -->
			{:else if isCalendar}
				<!-- Step indicators -->
				<div class="steps-bar">
					<span class="step" class:step-active={calendarStep >= 1} class:step-done={calendarStep > 1}>1. Upload</span>
					<span class="step-sep"></span>
					<span class="step" class:step-active={calendarStep >= 2} class:step-done={calendarStep > 2}>2. Sessions</span>
					<span class="step-sep"></span>
					<span class="step" class:step-active={calendarStep >= 3}>3. Review</span>
				</div>

				{#if calendarStep === 1}
					<!-- Step 1: Upload .ics -->
					<div class="card flow-card">
						<h2 class="flow-title">Upload Calendar File</h2>

						<div class="form-field" style="margin-bottom: 16px;">
							<label for="billing-month" class="form-label">Billing Month</label>
							<input
								id="billing-month"
								type="month"
								bind:value={billingMonth}
								class="form-input"
								style="max-width: 200px;"
							/>
						</div>

						<div
							class="drop-zone"
							class:drop-zone-active={icalFile !== null}
							role="button"
							tabindex="0"
							ondragover={(e) => e.preventDefault()}
							ondrop={handleFileDrop}
						>
							{#if icalFile}
								<span class="drop-file">{icalFile.name}</span>
								<button class="btn btn-ghost btn-sm" onclick={() => (icalFile = null)}>Remove</button>
							{:else}
								<span class="drop-label">Drop .ics file here or click to browse</span>
								<input
									type="file"
									accept=".ics,.ical"
									class="drop-input"
									onchange={handleFileSelect}
								/>
							{/if}
						</div>
						<div class="flow-actions">
							<button
								class="btn btn-primary"
								disabled={!icalFile || icalParsing}
								onclick={parseIcal}
							>
								{#if icalParsing}
									<span class="spinner" aria-hidden="true"></span>
									Parsing...
								{:else}
									Parse Calendar
								{/if}
							</button>
						</div>
					</div>

				{:else if calendarStep === 2}
					<!-- Step 2: Session picker -->
					<div class="card flow-card">
						<h2 class="flow-title">Select Billable Sessions</h2>

						<div class="rate-row">
							<label for="session-rate" class="form-label">Rate per hour</label>
							<div class="rate-input-wrap">
								<span class="rate-prefix">$</span>
								<input
									id="session-rate"
									type="number"
									bind:value={sessionRate}
									class="form-input rate-input"
									min="0"
									step="1"
								/>
							</div>
							<span class="live-subtotal">
								Subtotal: {fmtCurrency(liveSubtotal)}
								({selectedSessions.length} session{selectedSessions.length !== 1 ? 's' : ''})
							</span>
						</div>

						{#if matchedSessions.length === 0}
							<div class="empty-state">
								<p>No matching sessions found in the calendar file.</p>
							</div>
						{:else}
							<div class="session-list">
								{#each matchedSessions as session, i}
									<label
										class="session-row"
										class:session-billed={session.alreadyBilled}
										class:session-unselected={!session.selected && !session.alreadyBilled}
									>
										<input
											type="checkbox"
											bind:checked={matchedSessions[i].selected}
											disabled={!!session.alreadyBilled}
										/>
										<span class="session-date">{fmtDate(session.date)}</span>
										<span class="session-desc">{session.description}</span>
										<span class="session-hours">{session.hours}h</span>
										<span class="session-total">{fmtCurrency(session.hours * sessionRate)}</span>
										{#if session.alreadyBilled}
											<span class="session-billed-tag">(Already billed)</span>
										{/if}
									</label>
								{/each}
							</div>
						{/if}

						{#if unmatchedEvents.length > 0}
							<details class="unmatched-section">
								<summary class="unmatched-summary">
									{unmatchedEvents.length} unmatched event{unmatchedEvents.length !== 1 ? 's' : ''} (collapsed)
								</summary>
								<div class="unmatched-list">
									{#each unmatchedEvents as evt}
										<div class="unmatched-item">
											{evt.summary ?? evt.description ?? 'Unknown event'} - {evt.date ?? ''}
										</div>
									{/each}
								</div>
							</details>
						{/if}

						<div class="flow-actions">
							<button class="btn btn-ghost" onclick={() => (calendarStep = 1)}>Back</button>
							<button
								class="btn btn-primary"
								disabled={selectedSessions.length === 0}
								onclick={() => (calendarStep = 3)}
							>
								Review ({selectedSessions.length} sessions)
							</button>
						</div>
					</div>

				{:else if calendarStep === 3}
					<!-- Step 3: Review & generate -->
					<div class="card flow-card">
						<h2 class="flow-title">Review & Generate</h2>

						<div class="review-form">
							<div class="form-field">
								<label for="cal-project" class="form-label">Project</label>
								<input
									id="cal-project"
									type="text"
									bind:value={calInvoiceProject}
									class="form-input"
								/>
							</div>
							<div class="form-field">
								<label for="cal-notes" class="form-label">Notes</label>
								<textarea
									id="cal-notes"
									bind:value={calInvoiceNotes}
									class="form-input"
									rows="2"
								></textarea>
							</div>
						</div>

						<table class="data-table">
							<thead>
								<tr>
									<th>Date</th>
									<th>Description</th>
									<th>Hours</th>
									<th>Rate</th>
									<th>Total</th>
								</tr>
							</thead>
							<tbody>
								{#each selectedSessions as session}
									<tr>
										<td class="td-date">{fmtDate(session.date)}</td>
										<td>{session.description}</td>
										<td class="td-qty">{session.hours}</td>
										<td class="td-amount">{fmtCurrency(sessionRate)}</td>
										<td class="td-amount">{fmtCurrency(session.hours * sessionRate)}</td>
									</tr>
								{/each}
							</tbody>
							<tfoot>
								<tr>
									<td colspan="4">Total</td>
									<td class="td-amount td-total">{fmtCurrency(liveSubtotal)}</td>
								</tr>
							</tfoot>
						</table>

						<div class="flow-actions">
							<button class="btn btn-ghost" onclick={() => (calendarStep = 2)}>Back</button>
							<button
								class="btn btn-primary"
								disabled={calendarGenerating || selectedSessions.length === 0}
								onclick={handleGenerateCalendar}
							>
								{#if calendarGenerating}
									<span class="spinner" aria-hidden="true"></span>
									Generating...
								{:else}
									Generate Invoice
								{/if}
							</button>
						</div>
					</div>
				{/if}
			{/if}
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

	/* ── Section title ─────────────────────────────────────────────────── */
	.section-title {
		margin-bottom: 14px;
		font-size: 1rem;
		font-weight: 600;
	}

	/* ── Customer selector ─────────────────────────────────────────────── */
	.customer-selector {
		margin-bottom: 24px;
	}

	.customer-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
		gap: 12px;
	}

	.customer-card {
		padding: 16px 20px;
		display: flex;
		flex-direction: column;
		gap: 4px;
		cursor: pointer;
		border: 2px solid var(--border);
		background: var(--surface);
		text-align: left;
		transition: border-color .12s, box-shadow .12s;
		font-family: var(--font);
	}

	.customer-card:hover {
		border-color: var(--blue-500);
	}

	.customer-card-selected {
		border-color: var(--blue-500);
		box-shadow: 0 0 0 3px rgba(59,130,246,.15);
	}

	.customer-card-name {
		font-weight: 600;
		font-size: .95rem;
	}

	.customer-card-model {
		font-size: .8rem;
		color: var(--text-muted);
	}

	.customer-card-last {
		font-size: .75rem;
		color: var(--text-muted);
		margin-top: 4px;
	}

	/* ── Flow card ─────────────────────────────────────────────────────── */
	.flow-card {
		padding: 24px;
		margin-bottom: 16px;
	}

	.flow-title {
		margin-bottom: 16px;
		font-size: 1.1rem;
	}

	.flow-actions {
		display: flex;
		gap: 8px;
		margin-top: 16px;
	}

	/* ── Flat-rate form ────────────────────────────────────────────────── */
	.flat-form {
		display: flex;
		flex-direction: column;
		gap: 16px;
	}

	.flat-preview {
		display: flex;
		gap: 24px;
		flex-wrap: wrap;
	}

	/* ── Form fields ───────────────────────────────────────────────────── */
	.form-field {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.form-label {
		font-size: .75rem;
		font-weight: 600;
		color: var(--text-muted);
		text-transform: uppercase;
		letter-spacing: .04em;
	}

	.form-input {
		padding: 8px 12px;
		font-size: .875rem;
		font-family: var(--font);
		color: var(--text);
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
	}

	.form-input-sm {
		padding: 4px 8px;
		font-size: .8rem;
		max-width: 160px;
	}

	.form-input:focus {
		outline: 2px solid var(--focus);
		outline-offset: 0;
		border-color: var(--blue-500);
	}

	/* ── Meta items ────────────────────────────────────────────────────── */
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

	.meta-value-large {
		font-size: 1.25rem;
		font-weight: 700;
	}

	/* ── Steps bar ─────────────────────────────────────────────────────── */
	.steps-bar {
		display: flex;
		align-items: center;
		gap: 8px;
		margin-bottom: 16px;
	}

	.step {
		font-size: .85rem;
		color: var(--text-muted);
		font-weight: 500;
		padding: 4px 10px;
		border-radius: var(--radius-sm);
	}

	.step-active {
		color: var(--text);
		font-weight: 600;
	}

	.step-done {
		color: var(--green-700);
	}

	.step-sep {
		width: 20px;
		height: 1px;
		background: var(--gray-300);
	}

	/* ── Drop zone ─────────────────────────────────────────────────────── */
	.drop-zone {
		position: relative;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 8px;
		padding: 40px 24px;
		border: 2px dashed var(--gray-300);
		border-radius: var(--radius);
		text-align: center;
		transition: border-color .12s, background .12s;
	}

	.drop-zone:hover {
		border-color: var(--blue-500);
		background: rgba(59,130,246,.03);
	}

	.drop-zone-active {
		border-color: var(--green-500);
		background: rgba(34,197,94,.03);
	}

	.drop-label {
		font-size: .875rem;
		color: var(--text-muted);
	}

	.drop-file {
		font-weight: 600;
		font-size: .9rem;
	}

	.drop-input {
		position: absolute;
		inset: 0;
		opacity: 0;
		cursor: pointer;
	}

	/* ── Rate row ──────────────────────────────────────────────────────── */
	.rate-row {
		display: flex;
		align-items: center;
		gap: 12px;
		margin-bottom: 16px;
		flex-wrap: wrap;
	}

	.rate-input-wrap {
		display: flex;
		align-items: center;
		gap: 2px;
	}

	.rate-prefix {
		font-size: .875rem;
		color: var(--text-muted);
		font-weight: 500;
	}

	.rate-input {
		width: 80px;
	}

	.live-subtotal {
		font-size: .9rem;
		font-weight: 600;
		color: var(--text);
		margin-left: auto;
	}

	/* ── Session list ──────────────────────────────────────────────────── */
	.session-list {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.session-row {
		display: flex;
		align-items: center;
		gap: 12px;
		padding: 8px 10px;
		border-radius: var(--radius-sm);
		cursor: pointer;
		transition: background .1s;
		font-size: .875rem;
	}

	.session-row:hover {
		background: var(--gray-50);
	}

	.session-billed {
		opacity: .5;
		text-decoration: line-through;
	}

	.session-unselected {
		opacity: .6;
	}

	.session-date {
		min-width: 100px;
		color: var(--text-muted);
		font-size: .8rem;
	}

	.session-desc {
		flex: 1;
	}

	.session-hours {
		font-variant-numeric: tabular-nums;
		min-width: 30px;
		text-align: right;
	}

	.session-total {
		font-variant-numeric: tabular-nums;
		min-width: 70px;
		text-align: right;
		font-weight: 500;
	}

	.session-billed-tag {
		font-size: .7rem;
		color: var(--amber-600);
		white-space: nowrap;
	}

	/* ── Unmatched events ──────────────────────────────────────────────── */
	.unmatched-section {
		margin-top: 12px;
	}

	.unmatched-summary {
		cursor: pointer;
		font-size: .8rem;
		color: var(--text-muted);
	}

	.unmatched-list {
		margin-top: 8px;
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.unmatched-item {
		font-size: .8rem;
		color: var(--text-muted);
		padding: 4px 8px;
		background: var(--gray-50);
		border-radius: var(--radius-sm);
	}

	/* ── Review form ───────────────────────────────────────────────────── */
	.review-form {
		display: flex;
		flex-direction: column;
		gap: 12px;
		margin-bottom: 16px;
	}

	/* ── Generated invoice ─────────────────────────────────────────────── */
	.generated-card {
		padding: 24px;
	}

	.generated-header {
		display: flex;
		align-items: center;
		gap: 12px;
		margin-bottom: 20px;
	}

	.generated-meta {
		display: flex;
		gap: 24px;
		flex-wrap: wrap;
		margin-bottom: 20px;
	}

	.generated-actions {
		display: flex;
		gap: 8px;
		margin-top: 20px;
	}

	/* ── Table cells ───────────────────────────────────────────────────── */
	.td-date {
		white-space: nowrap;
		color: var(--text-muted);
		font-size: .85rem;
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

	.line-items-table {
		font-size: .85rem;
		margin-bottom: 8px;
	}

	/* ── Status pills ──────────────────────────────────────────────────── */
	.status-draft {
		background: var(--gray-100);
		color: var(--gray-600);
	}

	/* ── SAP Panel ─────────────────────────────────────────────────────── */
	.sap-panel {
		padding: 18px 20px;
		margin-top: 20px;
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
	}

	.sap-step {
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

	/* ── Misc ──────────────────────────────────────────────────────────── */
	.skeleton-card {
		padding: 20px 22px;
	}

	.error-card {
		padding: 24px;
	}

	.error-msg {
		color: var(--red-600);
		font-size: .875rem;
	}

	.btn-sm {
		padding: 4px 12px;
		font-size: .8rem;
	}

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

	/* ── Send Invoice section ──────────────────────────────────────── */
	.send-section {
		margin-top: 16px;
		padding: 20px;
		border: 1px solid var(--gray-200);
	}

	.send-title {
		font-size: 1rem;
		font-weight: 600;
		margin-bottom: 12px;
	}

	.send-sent-info {
		font-size: .85rem;
		color: var(--gray-500);
		margin-bottom: 12px;
	}

	.send-form {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.send-email-row {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.send-helper {
		font-size: .8rem;
		color: var(--gray-400);
	}

	.send-confirm {
		background: var(--gray-50);
		border: 1px solid var(--gray-200);
		border-radius: 8px;
		padding: 16px;
	}

	.send-confirm p {
		margin-bottom: 12px;
	}

	.send-confirm-actions {
		display: flex;
		gap: 8px;
	}

	.send-success {
		background: #f0fdf4;
		border: 1px solid #bbf7d0;
		border-radius: 8px;
		padding: 16px;
	}

	.send-success p {
		color: #166534;
		font-weight: 500;
		margin-bottom: 8px;
	}

	.payment-link-row {
		display: flex;
		align-items: center;
		gap: 8px;
		flex-wrap: wrap;
	}

	.payment-link-label {
		font-size: .85rem;
		color: var(--gray-500);
	}

	.payment-link-url {
		font-size: .85rem;
		color: var(--link-color, #2563eb);
		word-break: break-all;
	}

	.status-sent {
		background: #dbeafe;
		color: #1e40af;
	}

	.status-overdue {
		background: #fee2e2;
		color: #991b1b;
	}

	.status-paid {
		background: #dcfce7;
		color: #166534;
	}

	.status-void {
		background: #f3f4f6;
		color: #6b7280;
	}
</style>
