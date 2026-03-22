<script lang="ts">
	import { onMount } from 'svelte';
	import type { HealthResponse, Transaction, Invoice, SourceFreshness, TaxDeadline } from '$lib/types';
	import { fetchHealth, fetchTransactions, fetchInvoices, triggerIngest } from '$lib/api';
	import { formatAmount, amountClass } from '$lib/categories';

	let loading = $state(true);
	let health = $state<HealthResponse | null>(null);
	let recentTxns = $state<Transaction[]>([]);
	let outstandingInvoices = $state<Invoice[]>([]);
	let monthIncome = $state(0);
	let monthExpenses = $state(0);
	let fetchError = $state('');
	let importStatus = $state<'idle' | 'success' | 'error'>('idle');

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

	let monthRange = $derived.by(() => currentMonthRange());

	let reviewCount = $derived(health?.needs_review_count ?? 0);

	let deadlines = $derived<TaxDeadline[]>(health?.tax_deadlines ?? []);

	let sourceFreshness = $derived<SourceFreshness[]>(health?.source_freshness ?? []);

	let netIncome = $derived(monthIncome + monthExpenses);

	// Overdue invoices
	let overdueInvoices = $derived(outstandingInvoices.filter((inv) => (inv.status as string) === 'overdue'));
	let overdueTotal = $derived(overdueInvoices.reduce((sum, inv) => sum + Number(inv.total ?? 0), 0));

	// Upcoming deadlines within 7 days
	let urgentDeadlines = $derived(deadlines.filter((d: TaxDeadline) => d.days_until_due <= 7));

	// BLUF next action
	let nextAction = $derived.by<{ type: 'overdue' | 'deadline' | 'review' | 'clear'; message: string; href: string; urgency: 'red' | 'amber' | 'green' }>(() => {
		if (overdueInvoices.length > 0) {
			return {
				type: 'overdue',
				message: `You have ${overdueInvoices.length} overdue invoice${overdueInvoices.length !== 1 ? 's' : ''} totaling ${formatAmount(overdueTotal)}`,
				href: '/invoices',
				urgency: 'red',
			};
		}
		if (urgentDeadlines.length > 0) {
			const nearest = urgentDeadlines.reduce((a, b) => (a.days_until_due < b.days_until_due ? a : b));
			return {
				type: 'deadline',
				message: `B&O filing due in ${nearest.days_until_due} day${nearest.days_until_due !== 1 ? 's' : ''}`,
				href: '/tax',
				urgency: 'amber',
			};
		}
		if (reviewCount > 0) {
			return {
				type: 'review',
				message: `${reviewCount} transaction${reviewCount !== 1 ? 's' : ''} need review`,
				href: '/review',
				urgency: 'amber',
			};
		}
		return {
			type: 'clear',
			message: "You're all set! No urgent actions.",
			href: '',
			urgency: 'green',
		};
	});

	// Capped lists for progressive disclosure
	let recentTxnsCapped = $derived(recentTxns.slice(0, 5));
	let deadlinesCapped = $derived(deadlines.slice(0, 3));

	// Source health summary
	let healthySourceCount = $derived(sourceFreshness.filter((s: SourceFreshness) => s.freshness_status === 'green').length);
	let totalSourceCount = $derived(sourceFreshness.length);
	let allSourcesHealthy = $derived(healthySourceCount === totalSourceCount);

	function deadlineUrgency(d: TaxDeadline): 'red' | 'amber' | 'gray' {
		if (d.days_until_due < 7) return 'red';
		if (d.days_until_due < 30) return 'amber';
		return 'gray';
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
		importStatus = 'idle';
		try {
			await triggerIngest();
			const healthData = await fetchHealth();
			health = healthData;
			importStatus = 'success';
			setTimeout(() => { importStatus = 'idle'; }, 3000);
		} catch {
			importStatus = 'error';
			setTimeout(() => { importStatus = 'idle'; }, 5000);
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
		{@const action = nextAction}

		<!-- BLUF Next Action -->
		<section class="bluf-card bluf-{action.urgency}" aria-label="Next action">
			<div class="bluf-content">
				<span class="bluf-message">{action.message}</span>
				{#if action.href}
					<a href={action.href} class="bluf-cta">
						{#if action.type === 'overdue'}Follow up now{:else if action.type === 'deadline'}Review now{:else if action.type === 'review'}Review now{/if}
						&rarr;
					</a>
				{/if}
			</div>
		</section>

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
			<button class="btn btn-ghost" onclick={handleImport} disabled={importing}>
				{importing ? 'Importing...' : 'Import'}
			</button>
			{#if importStatus === 'success'}
				<span class="import-toast import-success">Data imported</span>
			{:else if importStatus === 'error'}
				<span class="import-toast import-error">Import failed — check Health page</span>
			{/if}
		</section>

		<div class="dash-grid">
			<!-- This Month -->
			<section class="card dash-card" aria-label="This month summary">
				<div class="dash-card-header">
					<h2 class="dash-card-title">This Month</h2>
					<span class="entity-context">All Entities</span>
				</div>
				<div class="summary-rows">
					<div class="summary-row">
						<span class="summary-label">Income</span>
						<span class="summary-value {amountClass(monthIncome)}">{formatAmount(monthIncome)}</span>
					</div>
					<div class="summary-row">
						<span class="summary-label">Expenses</span>
						<span class="summary-value {amountClass(monthExpenses)}">{formatAmount(monthExpenses)}</span>
					</div>
					<div class="summary-row summary-row-total">
						<span class="summary-label">Net</span>
						<span class="summary-value {amountClass(netIncome)}">
							{formatAmount(netIncome)}
						</span>
					</div>
				</div>
				<a href="/register?date_from={currentMonthRange().from}&date_to={currentMonthRange().to}" class="section-link">View in Register &rarr;</a>
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
									{inv.invoice_number}: {formatAmount(Number(inv.total ?? 0))}
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

			<!-- Recent Activity (capped to 5) -->
			<section class="card dash-card dash-card-wide" aria-label="Recent activity">
				<h2 class="dash-card-title">Recent Activity</h2>
				{#if recentTxns.length === 0}
					<p class="dash-empty">No recent transactions</p>
				{:else}
					<ul class="activity-list">
						{#each recentTxnsCapped as tx}
							<li class="activity-item">
								<span class="activity-date">{formatDate(tx.date)}</span>
								<span class="activity-desc truncate">
									{tx.vendor ?? tx.description}
									{#if tx.entity}
										<span class="activity-entity">{entityLabel(tx.entity)}</span>
									{/if}
								</span>
								<span class="activity-amount {tx.amount ? amountClass(tx.direction === 'expense' ? -Math.abs(tx.amount) : tx.amount) : ''}">
									{#if tx.amount}
										{formatAmount(tx.direction === 'expense' ? -Math.abs(tx.amount) : tx.amount)}
									{:else}
										--
									{/if}
								</span>
							</li>
						{/each}
					</ul>
					{#if recentTxns.length > 5}
						<a href="/register" class="section-link">View all in Register &rarr;</a>
					{/if}
				{/if}
			</section>

			<!-- Upcoming Deadlines (capped to 3) -->
			{#if deadlines.length > 0}
				<section class="card dash-card dash-card-wide" aria-label="Upcoming deadlines">
					<h2 class="dash-card-title">Upcoming Deadlines</h2>
					<ul class="deadline-list">
						{#each deadlinesCapped as d}
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
					{#if deadlines.length > 3}
						<a href="/tax" class="section-link">View all {deadlines.length} deadlines &rarr;</a>
					{/if}
				</section>
			{/if}

			<!-- Source Health (collapsed summary) -->
			{#if sourceFreshness.length > 0}
				<section class="card dash-card dash-card-wide source-summary" aria-label="Source health">
					<span class="source-summary-dot" class:source-all-healthy={allSourcesHealthy} class:source-some-unhealthy={!allSourcesHealthy}>&#x25CF;</span>
					<span class="source-summary-text">{healthySourceCount} of {totalSourceCount} sources healthy</span>
					<a href="/health" class="section-link">View Health Dashboard &rarr;</a>
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

	/* ── BLUF Next Action ──────────────────────────────────────────────── */
	.bluf-card {
		border-radius: 8px;
		padding: 16px 20px;
		margin-bottom: 16px;
		border-left: 4px solid;
		background: var(--surface);
	}

	.bluf-red {
		border-left-color: var(--red-500);
		background: color-mix(in srgb, var(--red-500) 6%, var(--surface));
	}

	.bluf-amber {
		border-left-color: var(--amber-500);
		background: color-mix(in srgb, var(--amber-500) 6%, var(--surface));
	}

	.bluf-green {
		border-left-color: var(--green-500);
		background: color-mix(in srgb, var(--green-500) 6%, var(--surface));
	}

	.bluf-content {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 16px;
		flex-wrap: wrap;
	}

	.bluf-message {
		font-size: 1.05rem;
		font-weight: 600;
		color: var(--text);
	}

	.bluf-cta {
		font-size: .9rem;
		font-weight: 600;
		color: var(--blue-600);
		text-decoration: none;
		white-space: nowrap;
	}

	.bluf-cta:hover {
		text-decoration: underline;
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

	.import-toast {
		font-size: .8rem;
		font-weight: 500;
		padding: 3px 10px;
		border-radius: var(--radius-sm);
	}

	.import-success {
		color: var(--green-700);
		background: var(--green-100);
	}

	.import-error {
		color: var(--red-700);
		background: var(--red-100);
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

	.dash-card-header {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 8px;
		margin-bottom: 14px;
	}

	.dash-card-header .dash-card-title {
		margin-bottom: 0;
	}

	.entity-context {
		font-size: .7rem;
		font-weight: 600;
		color: var(--text-muted);
		padding: 2px 7px;
		background: var(--gray-100);
		border-radius: 999px;
		white-space: nowrap;
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

	/* ── Section link (view all) ───────────────────────────────────────── */
	.section-link {
		display: inline-block;
		margin-top: 12px;
		font-size: .825rem;
		font-weight: 500;
		color: var(--blue-600);
		text-decoration: none;
	}

	.section-link:hover {
		text-decoration: underline;
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

	/* ── Source health (collapsed summary) ──────────────────────────────── */
	.source-summary {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 14px 20px;
	}

	.source-summary-dot {
		font-size: .85rem;
		line-height: 1;
	}

	.source-all-healthy {
		color: var(--green-500);
	}

	.source-some-unhealthy {
		color: var(--amber-500);
	}

	.source-summary-text {
		font-size: .875rem;
		font-weight: 500;
		color: var(--text);
		flex: 1;
	}

	/* ── Responsive ─────────────────────────────────────────────────────── */
	@media (max-width: 640px) {
		.dash-grid {
			grid-template-columns: 1fr;
		}
	}
</style>
