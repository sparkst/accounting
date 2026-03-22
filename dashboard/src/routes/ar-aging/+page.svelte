<script lang="ts">
	import { onMount } from 'svelte';
	import type { Invoice, Customer } from '$lib/types';
	import { fetchInvoices, fetchCustomers } from '$lib/api';
	import { formatAmount, amountClass, entityBadgeClass } from '$lib/categories';

	// ── Types ─────────────────────────────────────────────────────────────────

	type AgingBucket = 'current' | '1_30' | '31_60' | '61_90' | '90plus';

	interface CustomerRow {
		id: string;
		name: string;
		entity: string;
		current: number;
		b1_30: number;
		b31_60: number;
		b61_90: number;
		b90plus: number;
		total: number;
		invoices: Invoice[];
	}

	// ── State ─────────────────────────────────────────────────────────────────

	let invoices = $state<Invoice[]>([]);
	let customers = $state<Customer[]>([]);
	let loading = $state(true);
	let fetchError = $state('');
	let expandedCustomerId = $state<string | null>(null);

	// ── Load ──────────────────────────────────────────────────────────────────

	async function load() {
		loading = true;
		fetchError = '';
		try {
			const [invRes, custRes] = await Promise.all([fetchInvoices(), fetchCustomers()]);
			invoices = invRes.items;
			customers = custRes;
		} catch (e) {
			fetchError = e instanceof Error ? e.message : 'Failed to load data';
		} finally {
			loading = false;
		}
	}

	onMount(() => { load(); });

	// ── Helpers ───────────────────────────────────────────────────────────────

	const TODAY = new Date();
	TODAY.setHours(0, 0, 0, 0);

	/** Only sent/overdue invoices count toward AR aging. */
	function isOutstanding(inv: Invoice): boolean {
		return inv.status === 'sent' || inv.status === 'overdue';
	}

	/** Parse invoice total as number. */
	function invTotal(inv: Invoice): number {
		return parseFloat(inv.total ?? '0') || 0;
	}

	/**
	 * Classify an invoice into an aging bucket based on days past due.
	 * If due_date is in the future → current.
	 * Otherwise days_past_due drives the bucket.
	 */
	function agingBucket(inv: Invoice): AgingBucket {
		if (!inv.due_date) return 'current';
		const due = new Date(inv.due_date + 'T00:00:00');
		const daysPastDue = Math.floor((TODAY.getTime() - due.getTime()) / (1000 * 60 * 60 * 24));
		if (daysPastDue <= 0) return 'current';
		if (daysPastDue <= 30) return '1_30';
		if (daysPastDue <= 60) return '31_60';
		if (daysPastDue <= 90) return '61_90';
		return '90plus';
	}

	function customerName(id: string): string {
		return customers.find(c => c.id === id)?.name ?? id.slice(0, 8);
	}

	function customerEntity(id: string): string {
		// Determine entity from the customer's invoices
		const inv = invoices.find(i => i.customer_id === id);
		return inv?.entity ?? 'sparkry';
	}

	function fmtDate(iso: string | null): string {
		if (!iso) return '--';
		const d = new Date(iso + 'T00:00:00');
		return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
	}

	function daysOverdueLabel(inv: Invoice): string {
		if (!inv.due_date) return '--';
		const due = new Date(inv.due_date + 'T00:00:00');
		const diff = Math.floor((TODAY.getTime() - due.getTime()) / (1000 * 60 * 60 * 24));
		if (diff <= 0) {
			const daysUntil = Math.abs(diff);
			if (daysUntil === 0) return 'Due today';
			return `Due in ${daysUntil}d`;
		}
		return `${diff}d overdue`;
	}

	function bucketColor(bucket: AgingBucket): string {
		const map: Record<AgingBucket, string> = {
			current:  'var(--green-500)',
			'1_30':   'var(--amber-500)',
			'31_60':  '#f97316',   // orange-500
			'61_90':  'var(--red-500)',
			'90plus': 'var(--red-700)',
		};
		return map[bucket];
	}

	function bucketCellClass(bucket: AgingBucket): string {
		const map: Record<AgingBucket, string> = {
			current:  '',
			'1_30':   'bucket-warn',
			'31_60':  'bucket-orange',
			'61_90':  'bucket-danger',
			'90plus': 'bucket-critical',
		};
		return map[bucket];
	}

	function invoiceBucketClass(inv: Invoice): string {
		return bucketCellClass(agingBucket(inv));
	}

	// ── Derived: outstanding invoices ─────────────────────────────────────────

	let outstanding = $derived(invoices.filter(isOutstanding));

	// ── Derived: BLUF totals ──────────────────────────────────────────────────

	let totals = $derived.by(() => {
		let current = 0, b1_30 = 0, b31_60 = 0, b61_90 = 0, b90plus = 0;
		for (const inv of outstanding) {
			const amt = invTotal(inv);
			const bucket = agingBucket(inv);
			if (bucket === 'current') current += amt;
			else if (bucket === '1_30') b1_30 += amt;
			else if (bucket === '31_60') b31_60 += amt;
			else if (bucket === '61_90') b61_90 += amt;
			else b90plus += amt;
		}
		const total = current + b1_30 + b31_60 + b61_90 + b90plus;
		return { current, b1_30, b31_60, b61_90, b90plus, total };
	});

	// ── Derived: distribution bar segments ───────────────────────────────────

	let barSegments = $derived.by(() => {
		if (totals.total === 0) return [];
		const buckets: Array<{ bucket: AgingBucket; amount: number; label: string }> = [
			{ bucket: 'current', amount: totals.current, label: 'Current' },
			{ bucket: '1_30',   amount: totals.b1_30,   label: '1–30d' },
			{ bucket: '31_60',  amount: totals.b31_60,  label: '31–60d' },
			{ bucket: '61_90',  amount: totals.b61_90,  label: '61–90d' },
			{ bucket: '90plus', amount: totals.b90plus,  label: '90+d' },
		];
		return buckets
			.filter(s => s.amount > 0)
			.map(s => ({
				...s,
				pct: (s.amount / totals.total) * 100,
				color: bucketColor(s.bucket),
			}));
	});

	// ── Derived: per-customer rows ────────────────────────────────────────────

	let customerRows = $derived.by((): CustomerRow[] => {
		const map = new Map<string, CustomerRow>();

		for (const inv of outstanding) {
			const cid = inv.customer_id;
			if (!map.has(cid)) {
				map.set(cid, {
					id: cid,
					name: customerName(cid),
					entity: customerEntity(cid),
					current: 0,
					b1_30: 0,
					b31_60: 0,
					b61_90: 0,
					b90plus: 0,
					total: 0,
					invoices: [],
				});
			}
			const row = map.get(cid)!;
			const amt = invTotal(inv);
			const bucket = agingBucket(inv);
			if (bucket === 'current') row.current += amt;
			else if (bucket === '1_30') row.b1_30 += amt;
			else if (bucket === '31_60') row.b31_60 += amt;
			else if (bucket === '61_90') row.b61_90 += amt;
			else row.b90plus += amt;
			row.total += amt;
			row.invoices.push(inv);
		}

		// Sort by total descending
		return Array.from(map.values()).sort((a, b) => b.total - a.total);
	});

	// ── UI helpers ────────────────────────────────────────────────────────────

	function toggleCustomer(id: string) {
		expandedCustomerId = expandedCustomerId === id ? null : id;
	}

	function fmtBucket(amount: number): string {
		if (amount === 0) return '—';
		return formatAmount(amount);
	}

	function bucketAmtClass(amount: number, bucket: AgingBucket): string {
		if (amount === 0) return 'amount-zero';
		if (bucket === 'current') return 'amount-positive tabular-nums';
		if (bucket === '1_30') return 'amount-warn tabular-nums';
		if (bucket === '31_60') return 'amount-orange tabular-nums';
		return 'amount-negative tabular-nums';
	}
</script>

<div class="container page-shell">
	<header class="page-header">
		<div>
			<h1>AR Aging</h1>
			{#if !loading}
				<p class="page-subtitle">
					{outstanding.length} outstanding invoice{outstanding.length !== 1 ? 's' : ''}
					across {customerRows.length} customer{customerRows.length !== 1 ? 's' : ''}
				</p>
			{/if}
		</div>
		<div class="page-header-actions">
			<button class="btn btn-ghost" onclick={load} disabled={loading}>
				{loading ? 'Loading...' : 'Refresh'}
			</button>
			<a href="/invoices" class="btn btn-ghost">Invoices</a>
		</div>
	</header>

	{#if loading}
		<div class="skeleton-list">
			{#each Array(4) as _}
				<div class="card skeleton-card">
					<div class="skeleton" style="height: 18px; width: 35%; margin-bottom: 8px;"></div>
					<div class="skeleton" style="height: 28px; width: 50%;"></div>
				</div>
			{/each}
		</div>
	{:else if fetchError}
		<div class="card error-card">
			<p class="error-msg">{fetchError}</p>
			<button class="btn btn-ghost" onclick={load}>Try again</button>
		</div>
	{:else if outstanding.length === 0}
		<div class="empty-state">
			<span class="empty-icon">$</span>
			<h2>No outstanding invoices</h2>
			<p>All invoices are paid, voided, or still in draft.</p>
			<a href="/invoices" class="btn btn-primary">View Invoices</a>
		</div>
	{:else}
		<!-- ── BLUF Cards ──────────────────────────────────────────────────── -->
		<div class="bluf-grid">
			<!-- Total Outstanding -->
			<div class="card bluf-card bluf-total">
				<span class="bluf-label">Total Outstanding</span>
				<span class="bluf-amount {amountClass(totals.total)}">{formatAmount(totals.total)}</span>
				<span class="bluf-sub">{outstanding.length} invoice{outstanding.length !== 1 ? 's' : ''}</span>
			</div>

			<!-- Current -->
			<div class="card bluf-card">
				<span class="bluf-label">Current</span>
				<span class="bluf-amount bluf-current">{formatAmount(totals.current)}</span>
				<span class="bluf-sub">Not yet due</span>
			</div>

			<!-- 1–30 days -->
			<div class="card bluf-card" class:bluf-alert={totals.b1_30 > 0}>
				<span class="bluf-label">1–30 Days</span>
				<span class="bluf-amount bluf-warn">{formatAmount(totals.b1_30)}</span>
				<span class="bluf-sub">Overdue</span>
			</div>

			<!-- 31–60 days -->
			<div class="card bluf-card" class:bluf-alert={totals.b31_60 > 0}>
				<span class="bluf-label">31–60 Days</span>
				<span class="bluf-amount bluf-orange">{formatAmount(totals.b31_60)}</span>
				<span class="bluf-sub">Overdue</span>
			</div>

			<!-- 90+ days -->
			<div class="card bluf-card" class:bluf-critical={totals.b90plus > 0}>
				<span class="bluf-label">90+ Days</span>
				<span class="bluf-amount bluf-danger">{formatAmount(totals.b90plus + totals.b61_90)}</span>
				<span class="bluf-sub">61–90d + 90+d</span>
			</div>
		</div>

		<!-- ── Distribution Bar ───────────────────────────────────────────── -->
		{#if barSegments.length > 0}
			<div class="card dist-card">
				<div class="dist-header">
					<span class="dist-title">Aging Distribution</span>
					<span class="dist-total">{formatAmount(totals.total)} outstanding</span>
				</div>
				<div class="dist-bar" role="img" aria-label="Aging distribution bar">
					{#each barSegments as seg}
						<div
							class="dist-segment"
							style="width: {seg.pct}%; background: {seg.color};"
							title="{seg.label}: {formatAmount(seg.amount)} ({seg.pct.toFixed(1)}%)"
						></div>
					{/each}
				</div>
				<div class="dist-legend">
					{#each barSegments as seg}
						<div class="legend-item">
							<span class="legend-dot" style="background: {seg.color};"></span>
							<span class="legend-label">{seg.label}</span>
							<span class="legend-pct">{seg.pct.toFixed(0)}%</span>
						</div>
					{/each}
				</div>
			</div>
		{/if}

		<!-- ── Per-Customer Breakdown Table ──────────────────────────────── -->
		<div class="card table-wrap">
			<table class="data-table aging-table">
				<thead>
					<tr>
						<th class="th-customer">Customer</th>
						<th class="th-bucket th-current">Current</th>
						<th class="th-bucket th-warn">1–30d</th>
						<th class="th-bucket th-orange">31–60d</th>
						<th class="th-bucket th-danger">61–90d</th>
						<th class="th-bucket th-critical">90+d</th>
						<th class="th-total">Total</th>
					</tr>
				</thead>
				<tbody>
					{#each customerRows as row (row.id)}
						<!-- Customer summary row -->
						<tr
							class="customer-row"
							class:row-expanded={expandedCustomerId === row.id}
							onclick={() => toggleCustomer(row.id)}
							role="button"
							tabindex="0"
							onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleCustomer(row.id); } }}
							aria-expanded={expandedCustomerId === row.id}
						>
							<td class="td-customer">
								<div class="customer-cell">
									<span class="expand-icon" aria-hidden="true">
										{expandedCustomerId === row.id ? '▾' : '▸'}
									</span>
									<div class="customer-info">
										<span class="customer-name">{row.name}</span>
										<span class="{entityBadgeClass(row.entity)}">{row.entity}</span>
									</div>
									<span class="inv-count">{row.invoices.length} inv</span>
								</div>
							</td>
							<td class="td-bucket {bucketAmtClass(row.current, 'current')}">{fmtBucket(row.current)}</td>
							<td class="td-bucket {bucketAmtClass(row.b1_30, '1_30')}">{fmtBucket(row.b1_30)}</td>
							<td class="td-bucket {bucketAmtClass(row.b31_60, '31_60')}">{fmtBucket(row.b31_60)}</td>
							<td class="td-bucket {bucketAmtClass(row.b61_90, '61_90')}">{fmtBucket(row.b61_90)}</td>
							<td class="td-bucket {bucketAmtClass(row.b90plus, '90plus')}">{fmtBucket(row.b90plus)}</td>
							<td class="td-total tabular-nums">{formatAmount(row.total)}</td>
						</tr>

						<!-- Expanded invoice detail rows -->
						{#if expandedCustomerId === row.id}
							<tr class="detail-header-row">
								<td colspan="7">
									<div class="detail-panel">
										<table class="data-table inv-detail-table">
											<thead>
												<tr>
													<th>Invoice #</th>
													<th>Submitted</th>
													<th>Due</th>
													<th>Status</th>
													<th>Aging</th>
													<th class="th-right">Amount</th>
												</tr>
											</thead>
											<tbody>
												{#each row.invoices.sort((a, b) => {
													// Sort by days overdue descending (most urgent first)
													const da = agingBucket(a);
													const db = agingBucket(b);
													const order: Record<AgingBucket, number> = { '90plus': 0, '61_90': 1, '31_60': 2, '1_30': 3, current: 4 };
													return order[da] - order[db];
												}) as inv (inv.id)}
													<tr class="inv-detail-row {invoiceBucketClass(inv)}">
														<td class="td-invnum">
															<a href="/invoices" class="inv-link">{inv.invoice_number}</a>
														</td>
														<td class="td-date">{fmtDate(inv.submitted_date)}</td>
														<td class="td-date">{fmtDate(inv.due_date)}</td>
														<td>
															<span class="status-pill status-{inv.status}">{inv.status}</span>
														</td>
														<td class="td-aging-label">{daysOverdueLabel(inv)}</td>
														<td class="td-inv-amount tabular-nums {amountClass(invTotal(inv))}">{formatAmount(invTotal(inv))}</td>
													</tr>
												{/each}
											</tbody>
											<tfoot>
												<tr class="subtotal-row">
													<td colspan="5">Customer Total</td>
													<td class="td-inv-amount tabular-nums">{formatAmount(row.total)}</td>
												</tr>
											</tfoot>
										</table>
									</div>
								</td>
							</tr>
						{/if}
					{/each}
				</tbody>
				<tfoot>
					<tr class="grand-total-row">
						<td class="td-customer"><strong>Total</strong></td>
						<td class="td-bucket tabular-nums">{fmtBucket(totals.current)}</td>
						<td class="td-bucket tabular-nums">{fmtBucket(totals.b1_30)}</td>
						<td class="td-bucket tabular-nums">{fmtBucket(totals.b31_60)}</td>
						<td class="td-bucket tabular-nums">{fmtBucket(totals.b61_90)}</td>
						<td class="td-bucket tabular-nums">{fmtBucket(totals.b90plus)}</td>
						<td class="td-total tabular-nums"><strong>{formatAmount(totals.total)}</strong></td>
					</tr>
				</tfoot>
			</table>
		</div>
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

	/* ── BLUF Cards ──────────────────────────────────────────────────────── */
	.bluf-grid {
		display: grid;
		grid-template-columns: repeat(5, 1fr);
		gap: 12px;
		margin-bottom: 16px;
	}

	@media (max-width: 900px) {
		.bluf-grid {
			grid-template-columns: repeat(3, 1fr);
		}
	}

	@media (max-width: 600px) {
		.bluf-grid {
			grid-template-columns: repeat(2, 1fr);
		}
	}

	.bluf-card {
		padding: 16px 18px;
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.bluf-total {
		border-left: 3px solid var(--gray-400);
	}

	.bluf-alert {
		border-left: 3px solid var(--amber-500);
	}

	.bluf-critical {
		border-left: 3px solid var(--red-600);
	}

	.bluf-label {
		font-size: .72rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: .05em;
		color: var(--text-muted);
	}

	.bluf-amount {
		font-size: 1.25rem;
		font-weight: 700;
		font-variant-numeric: tabular-nums;
		line-height: 1.2;
	}

	.bluf-current { color: var(--green-600); }
	.bluf-warn    { color: var(--amber-600); }
	.bluf-orange  { color: #ea580c; }
	.bluf-danger  { color: var(--red-600); }

	.bluf-sub {
		font-size: .75rem;
		color: var(--text-muted);
	}

	/* ── Distribution Bar ────────────────────────────────────────────────── */
	.dist-card {
		padding: 16px 20px;
		margin-bottom: 16px;
	}

	.dist-header {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		margin-bottom: 10px;
	}

	.dist-title {
		font-size: .8rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: .05em;
		color: var(--text-muted);
	}

	.dist-total {
		font-size: .85rem;
		font-weight: 600;
		color: var(--text);
		font-variant-numeric: tabular-nums;
	}

	.dist-bar {
		display: flex;
		height: 12px;
		border-radius: var(--radius-sm);
		overflow: hidden;
		gap: 2px;
		background: var(--gray-100);
		margin-bottom: 10px;
	}

	.dist-segment {
		height: 100%;
		min-width: 2px;
		transition: opacity .15s;
	}

	.dist-segment:hover {
		opacity: .85;
	}

	.dist-legend {
		display: flex;
		gap: 16px;
		flex-wrap: wrap;
	}

	.legend-item {
		display: flex;
		align-items: center;
		gap: 5px;
		font-size: .75rem;
	}

	.legend-dot {
		width: 8px;
		height: 8px;
		border-radius: 50%;
		flex-shrink: 0;
	}

	.legend-label {
		color: var(--text-muted);
	}

	.legend-pct {
		color: var(--text);
		font-weight: 600;
		font-variant-numeric: tabular-nums;
	}

	/* ── Aging Table ─────────────────────────────────────────────────────── */
	.table-wrap {
		overflow-x: auto;
		margin-bottom: 24px;
	}

	.aging-table th,
	.aging-table td {
		padding: 10px 12px;
	}

	.th-customer { text-align: left; min-width: 180px; }
	.th-bucket   { text-align: right; min-width: 90px; }
	.th-total    { text-align: right; min-width: 110px; }
	.th-right    { text-align: right; }

	.th-current { color: var(--green-600); }
	.th-warn    { color: var(--amber-600); }
	.th-orange  { color: #ea580c; }
	.th-danger  { color: var(--red-600); }
	.th-critical{ color: var(--red-700); }

	/* Customer rows */
	.customer-row {
		cursor: pointer;
		transition: background .1s;
	}

	.customer-row:hover td {
		background: var(--gray-50);
	}

	.customer-row:focus-visible {
		outline: 2px solid var(--focus);
		outline-offset: -2px;
	}

	.row-expanded td {
		background: var(--gray-50);
		border-bottom-color: transparent;
	}

	.customer-cell {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.expand-icon {
		font-size: .7rem;
		color: var(--text-muted);
		width: 10px;
		flex-shrink: 0;
	}

	.customer-info {
		display: flex;
		flex-direction: column;
		gap: 2px;
		flex: 1;
	}

	.customer-name {
		font-weight: 600;
		font-size: .875rem;
	}

	.inv-count {
		font-size: .72rem;
		color: var(--text-muted);
		flex-shrink: 0;
		font-variant-numeric: tabular-nums;
	}

	.td-bucket {
		text-align: right;
		font-variant-numeric: tabular-nums;
		font-size: .875rem;
	}

	.td-total {
		text-align: right;
		font-weight: 700;
	}

	/* Bucket cell colors */
	.amount-zero   { color: var(--gray-300); }
	.amount-warn   { color: var(--amber-600); }
	.amount-orange { color: #ea580c; }

	/* Grand total footer */
	.grand-total-row td {
		border-top: 2px solid var(--border);
		font-size: .875rem;
		background: var(--gray-50);
	}

	/* ── Detail Panel ────────────────────────────────────────────────────── */
	.detail-header-row td {
		padding: 0;
		background: var(--gray-50);
	}

	.detail-panel {
		padding: 12px 16px 16px;
	}

	.inv-detail-table {
		font-size: .83rem;
		width: 100%;
	}

	.inv-detail-table th,
	.inv-detail-table td {
		padding: 7px 10px;
	}

	.td-invnum {
		font-weight: 600;
		font-family: var(--font-mono);
		font-size: .8rem;
		white-space: nowrap;
	}

	.inv-link {
		color: var(--blue-600);
		text-decoration: none;
	}

	.inv-link:hover {
		text-decoration: underline;
	}

	.td-date {
		white-space: nowrap;
		color: var(--text-muted);
	}

	.td-aging-label {
		font-size: .8rem;
		white-space: nowrap;
	}

	.td-inv-amount {
		text-align: right;
		white-space: nowrap;
	}

	.th-right {
		text-align: right;
	}

	/* Row tinting by bucket severity */
	.inv-detail-row.bucket-warn   { background: rgba(245, 158, 11, .06); }
	.inv-detail-row.bucket-orange { background: rgba(249, 115, 22, .08); }
	.inv-detail-row.bucket-danger { background: rgba(239, 68,  68, .08); }
	.inv-detail-row.bucket-critical { background: rgba(185, 28,  28, .10); }

	.subtotal-row td {
		font-weight: 700;
		border-top: 1px solid var(--border);
		background: var(--surface);
	}

	/* Status pills */
	.status-pill {
		display: inline-flex;
		align-items: center;
		padding: 2px 7px;
		border-radius: 999px;
		font-size: .72rem;
		font-weight: 600;
		white-space: nowrap;
	}

	.status-sent    { background: rgba(59,130,246,.1); color: var(--blue-600); }
	.status-overdue { background: var(--red-100); color: var(--red-700); }

	/* ── Skeleton / Error / Empty ─────────────────────────────────────────── */
	.skeleton-list {
		display: grid;
		grid-template-columns: repeat(4, 1fr);
		gap: 12px;
		margin-bottom: 24px;
	}

	.skeleton-card {
		padding: 20px 18px;
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

	.empty-state {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 10px;
		padding: 64px 24px;
		text-align: center;
	}

	.empty-icon {
		font-size: 2.5rem;
		color: var(--gray-300);
	}

	.empty-state h2 {
		font-size: 1.1rem;
		color: var(--text);
	}

	.empty-state p {
		color: var(--text-muted);
		font-size: .875rem;
	}
</style>
