<script lang="ts">
	import { onMount } from 'svelte';
	import type { HealthResponse, Transaction, Invoice, SourceFreshness, TaxDeadline } from '$lib/types';
	import { fetchHealth, fetchTransactions, fetchInvoices, triggerIngest } from '$lib/api';

	let loading = $state(true);
	let health = $state<HealthResponse | null>(null);
	let recentTxns = $state<Transaction[]>([]);
	let outstandingInvoices = $state<Invoice[]>([]);
	let monthIncome = $state(0);
	let monthExpenses = $state(0);
	let fetchError = $state('');

	// Time-based greeting
	let greeting = $derived(() => {
		const hour = new Date().getHours();
		if (hour < 12) return 'Good morning';
		if (hour < 17) return 'Good afternoon';
		return 'Good evening';
	});

	// Current month boundaries
	function currentMonthRange(): { from: string; to: string } {
		const now = new Date();
		const y = now.getFullYear();
		const m = String(now.getMonth() + 1).padStart(2, '0');
		const lastDay = new Date(y, now.getMonth() + 1, 0).getDate();
		return { from: `${y}-${m}-01`, to: `${y}-${m}-${String(lastDay).padStart(2, '0')}` };
	}

	let reviewCount = $derived(health?.needs_review_count ?? 0);

	let deadlines = $derived<TaxDeadline[]>(health?.tax_deadlines ?? []);

	let sourceFreshness = $derived<SourceFreshness[]>(health?.source_freshness ?? []);

	let netIncome = $derived(monthIncome + monthExpenses);

	function deadlineUrgency(d: TaxDeadline): 'red' | 'amber' | 'gray' {
		if (d.days_until_due < 7) return 'red';
		if (d.days_until_due < 30) return 'amber';
		return 'gray';
	}

	function freshnessColor(s: SourceFreshness): string {
		if (s.freshness_status === 'green') return 'var(--green-500)';
		if (s.freshness_status === 'amber') return 'var(--amber-500)';
		if (s.freshness_status === 'red') return 'var(--red-500)';
		return 'var(--gray-400)';
	}

	function freshnessLabel(s: SourceFreshness): string {
		if (s.freshness_status === 'green') return 'Fresh';
		if (s.freshness_status === 'never') return 'Never synced';
		if (!s.last_run_at) return 'Unknown';
		const days = Math.floor((Date.now() - new Date(s.last_run_at).getTime()) / 86400000);
		if (days === 0) return 'Today';
		if (days === 1) return '1d ago';
		return `${days}d stale`;
	}

	function sourceName(source: string): string {
		const names: Record<string, string> = {
			gmail: 'Gmail',
			stripe_sparkry: 'Stripe (Sparkry)',
			stripe_blackline: 'Stripe (BL)',
			shopify: 'Shopify',
			bank_csv: 'Bank CSV',
			manual: 'Manual',
		};
		return names[source] ?? source;
	}

	function formatAmount(n: number): string {
		return n.toLocaleString('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 0, maximumFractionDigits: 0 });
	}

	function formatAmountFull(n: number): string {
		return n.toLocaleString('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2 });
	}

	function formatDate(iso: string): string {
		const d = new Date(iso + 'T00:00:00');
		const now = new Date();
		const today = now.toISOString().slice(0, 10);
		const yesterday = new Date(now.getTime() - 86400000).toISOString().slice(0, 10);
		if (iso === today) return 'Today';
		if (iso === yesterday) return 'Yesterday';
		return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
	}

	function entityLabel(e: string | null): string {
		if (e === 'sparkry') return 'Sparkry';
		if (e === 'blackline') return 'BlackLine';
		if (e === 'personal') return 'Personal';
		return '';
	}

	onMount(async () => {
		try {
			const range = currentMonthRange();

			const [healthData, recentData, monthData, invoiceData] = await Promise.all([
				fetchHealth(),
				fetchTransactions({ limit: 8, sort_by: 'date', sort_order: 'desc' }),
				fetchTransactions({ date_from: range.from, date_to: range.to, limit: 1 }),
				fetchInvoices(),
			]);

			health = healthData;
			recentTxns = recentData.items;
			monthIncome = monthData.income_total;
			monthExpenses = monthData.expense_total;

			const today = new Date().toISOString().slice(0, 10);
			outstandingInvoices = invoiceData.items.filter((inv) => {
				const s = inv.status as string;
				return s === 'sent' || s === 'overdue' || (s === 'sent' && inv.due_date && inv.due_date < today);
			});
		} catch (e) {
			fetchError = e instanceof Error ? e.message : 'Failed to load dashboard data';
		} finally {
			loading = false;
		}
	});

	let importing = $state(false);
	async function handleImport() {
		importing = true;
		try {
			await triggerIngest();
			// Reload data after import
			const healthData = await fetchHealth();
			health = healthData;
		} catch {
			// Silent fail — import runs in background
		} finally {
			importing = false;
		}
	}
</script>

<div class="container page-shell">
	<header class="page-header">
		<div>
			<h1>Dashboard</h1>
			<p class="page-subtitle">{greeting()}, Travis</p>
		</div>
	</header>

	{#if loading}
		<div class="dash-grid">
			<div class="card skeleton-block" style="grid-column: 1 / -1; height: 56px;"></div>
			<div class="card skeleton-block" style="height: 140px;"></div>
			<div class="card skeleton-block" style="height: 140px;"></div>
			<div class="card skeleton-block" style="grid-column: 1 / -1; height: 200px;"></div>
		</div>
	{:else if fetchError}
		<div class="card error-card">
			<p class="error-msg">{fetchError}</p>
		</div>
	{:else}
		<!-- Quick Actions -->
		<section class="quick-actions card" aria-label="Quick actions">
			<a
				href="/review"
				class="btn {reviewCount > 0 ? 'btn-primary' : 'btn-ghost'}"
			>
				{#if reviewCount > 0}
					Review {reviewCount} item{reviewCount !== 1 ? 's' : ''}
				{:else}
					Review Queue
				{/if}
			</a>
			<a href="/invoices" class="btn btn-ghost">Generate Invoice</a>
			{#if deadlines.some((d: TaxDeadline) => d.label.includes('B&O') && d.days_until_due <= 14)}
				<a href="/tax" class="btn btn-primary">File B&O</a>
			{:else}
				<a href="/tax" class="btn btn-ghost">Tax Summary</a>
			{/if}
			<button class="btn btn-ghost" onclick={handleImport} disabled={importing}>
				{importing ? 'Importing...' : 'Import'}
			</button>
		</section>

		<div class="dash-grid">
			<!-- This Month -->
			<section class="card dash-card" aria-label="This month summary">
				<h2 class="dash-card-title">This Month</h2>
				<div class="summary-rows">
					<div class="summary-row">
						<span class="summary-label">Income</span>
						<span class="summary-value amount-positive">{formatAmount(monthIncome)}</span>
					</div>
					<div class="summary-row">
						<span class="summary-label">Expenses</span>
						<span class="summary-value amount-negative">-{formatAmount(Math.abs(monthExpenses))}</span>
					</div>
					<div class="summary-row summary-row-total">
						<span class="summary-label">Net</span>
						<span class="summary-value {netIncome >= 0 ? 'amount-positive' : 'amount-negative'}">
							{formatAmount(netIncome)}
						</span>
					</div>
				</div>
			</section>

			<!-- Outstanding -->
			<section class="card dash-card" aria-label="Outstanding items">
				<h2 class="dash-card-title">Outstanding</h2>
				<div class="outstanding-list">
					{#if outstandingInvoices.length > 0}
						{#each outstandingInvoices as inv}
							<a href="/invoices" class="outstanding-item">
								<span class="outstanding-icon">&#x25CB;</span>
								<span>
									{inv.invoice_number}: {formatAmountFull(Number(inv.total ?? 0))}
									{#if inv.days_outstanding}
										<span class="outstanding-age">({inv.days_outstanding}d)</span>
									{/if}
								</span>
							</a>
						{/each}
					{:else}
						<span class="outstanding-none">No outstanding invoices</span>
					{/if}

					{#if reviewCount > 0}
						<a href="/review" class="outstanding-item">
							<span class="outstanding-icon outstanding-icon-amber">&#x25CF;</span>
							<span>{reviewCount} item{reviewCount !== 1 ? 's' : ''} need review</span>
						</a>
					{/if}

					{#each deadlines.filter((d: TaxDeadline) => d.days_until_due <= 14) as d}
						<span class="outstanding-item outstanding-deadline">
							<span class="outstanding-icon outstanding-icon-{deadlineUrgency(d)}">&#x25CF;</span>
							<span>{d.label} due in {d.days_until_due}d</span>
						</span>
					{/each}
				</div>
			</section>

			<!-- Recent Activity -->
			<section class="card dash-card dash-card-wide" aria-label="Recent activity">
				<h2 class="dash-card-title">Recent Activity</h2>
				{#if recentTxns.length === 0}
					<p class="dash-empty">No recent transactions</p>
				{:else}
					<ul class="activity-list">
						{#each recentTxns as tx}
							<li class="activity-item">
								<span class="activity-date">{formatDate(tx.date)}</span>
								<span class="activity-desc truncate">
									{tx.vendor ?? tx.description}
									{#if tx.entity}
										<span class="activity-entity">{entityLabel(tx.entity)}</span>
									{/if}
								</span>
								<span class="activity-amount {tx.direction === 'income' ? 'amount-positive' : tx.direction === 'expense' ? 'amount-negative' : ''}">
									{#if tx.amount}
										{tx.direction === 'expense' ? '-' : ''}{formatAmountFull(tx.amount)}
									{:else}
										--
									{/if}
								</span>
							</li>
						{/each}
					</ul>
				{/if}
			</section>

			<!-- Upcoming Deadlines -->
			{#if deadlines.length > 0}
				<section class="card dash-card dash-card-wide" aria-label="Upcoming deadlines">
					<h2 class="dash-card-title">Upcoming Deadlines</h2>
					<ul class="deadline-list">
						{#each deadlines as d}
							{@const urgency = deadlineUrgency(d)}
							<li class="deadline-item">
								<span class="deadline-date">{formatDate(d.due_date)}</span>
								<span class="deadline-label truncate">
									{d.label}
									<span class="deadline-entity">{entityLabel(d.entity)}</span>
								</span>
								<span class="deadline-days deadline-{urgency}">
									{d.days_until_due}d
									{#if urgency === 'red'}
										&#x26A0;
									{/if}
								</span>
							</li>
						{/each}
					</ul>
				</section>
			{/if}

			<!-- Source Health -->
			{#if sourceFreshness.length > 0}
				<section class="card dash-card dash-card-wide" aria-label="Source health">
					<h2 class="dash-card-title">Source Health</h2>
					<div class="source-row">
						{#each sourceFreshness as s}
							<span class="source-item">
								<span class="source-dot" style="color: {freshnessColor(s)}">&#x25CF;</span>
								<span class="source-name">{sourceName(s.source)}</span>
								<span class="source-status">{freshnessLabel(s)}</span>
							</span>
						{/each}
					</div>
				</section>
			{/if}
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

	.error-card {
		padding: 24px;
	}

	.error-msg {
		color: var(--red-600);
		font-size: .875rem;
	}

	/* ── Quick Actions ──────────────────────────────────────────────────── */
	.quick-actions {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 12px 16px;
		margin-bottom: 20px;
		flex-wrap: wrap;
	}

	/* ── Grid ───────────────────────────────────────────────────────────── */
	.dash-grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 16px;
	}

	.dash-card {
		padding: 20px 22px;
	}

	.dash-card-wide {
		grid-column: 1 / -1;
	}

	.dash-card-title {
		font-size: .8rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: .05em;
		color: var(--text-muted);
		margin-bottom: 14px;
	}

	.dash-empty {
		color: var(--text-muted);
		font-size: .875rem;
	}

	/* ── Skeleton ───────────────────────────────────────────────────────── */
	.skeleton-block {
		animation: pulse 1.5s ease-in-out infinite;
		background: var(--gray-100);
	}

	@keyframes pulse {
		0%, 100% { opacity: 1; }
		50% { opacity: .4; }
	}

	/* ── This Month summary ─────────────────────────────────────────────── */
	.summary-rows {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.summary-row {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
		font-size: .9rem;
	}

	.summary-row-total {
		border-top: 1px solid var(--border);
		padding-top: 8px;
		margin-top: 4px;
		font-weight: 600;
	}

	.summary-label {
		color: var(--text-muted);
	}

	.summary-value {
		font-variant-numeric: tabular-nums;
		font-weight: 500;
	}

	/* ── Outstanding ────────────────────────────────────────────────────── */
	.outstanding-list {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.outstanding-item {
		display: flex;
		align-items: center;
		gap: 8px;
		font-size: .875rem;
		color: var(--text);
		text-decoration: none;
	}

	.outstanding-item:hover {
		color: var(--blue-600);
	}

	.outstanding-icon {
		font-size: .6rem;
		color: var(--gray-400);
		flex-shrink: 0;
	}

	.outstanding-icon-amber {
		color: var(--amber-500);
	}

	.outstanding-icon-red {
		color: var(--red-500);
	}

	.outstanding-age {
		color: var(--text-muted);
		font-size: .8rem;
	}

	.outstanding-none {
		color: var(--text-muted);
		font-size: .875rem;
	}

	.outstanding-deadline {
		cursor: default;
	}

	/* ── Activity list ──────────────────────────────────────────────────── */
	.activity-list {
		list-style: none;
	}

	.activity-item {
		display: flex;
		align-items: baseline;
		gap: 12px;
		padding: 6px 0;
		font-size: .875rem;
		border-bottom: 1px solid var(--gray-100);
	}

	.activity-item:last-child {
		border-bottom: none;
	}

	.activity-date {
		flex-shrink: 0;
		width: 72px;
		color: var(--text-muted);
		font-size: .8rem;
	}

	.activity-desc {
		flex: 1;
		min-width: 0;
	}

	.activity-entity {
		font-size: .75rem;
		color: var(--text-muted);
		margin-left: 4px;
	}

	.activity-amount {
		flex-shrink: 0;
		font-variant-numeric: tabular-nums;
		font-weight: 500;
		text-align: right;
		min-width: 80px;
	}

	/* ── Deadline list ──────────────────────────────────────────────────── */
	.deadline-list {
		list-style: none;
	}

	.deadline-item {
		display: flex;
		align-items: baseline;
		gap: 12px;
		padding: 6px 0;
		font-size: .875rem;
		border-bottom: 1px solid var(--gray-100);
	}

	.deadline-item:last-child {
		border-bottom: none;
	}

	.deadline-date {
		flex-shrink: 0;
		width: 72px;
		color: var(--text-muted);
		font-size: .8rem;
	}

	.deadline-label {
		flex: 1;
		min-width: 0;
	}

	.deadline-entity {
		font-size: .75rem;
		color: var(--text-muted);
		margin-left: 4px;
	}

	.deadline-days {
		flex-shrink: 0;
		font-weight: 600;
		font-size: .8rem;
		text-align: right;
		min-width: 48px;
	}

	.deadline-red {
		color: var(--red-600);
	}

	.deadline-amber {
		color: var(--amber-600);
	}

	.deadline-gray {
		color: var(--text-muted);
	}

	/* ── Source health ───────────────────────────────────────────────────── */
	.source-row {
		display: flex;
		flex-wrap: wrap;
		gap: 16px;
	}

	.source-item {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		font-size: .85rem;
	}

	.source-dot {
		font-size: .7rem;
		line-height: 1;
	}

	.source-name {
		font-weight: 500;
	}

	.source-status {
		color: var(--text-muted);
		font-size: .8rem;
	}

	/* ── Responsive ─────────────────────────────────────────────────────── */
	@media (max-width: 640px) {
		.dash-grid {
			grid-template-columns: 1fr;
		}
	}
</style>
