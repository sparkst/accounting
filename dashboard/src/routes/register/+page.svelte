<script lang="ts">
	import { onMount } from 'svelte';
	import type { TransactionList } from '$lib/types';
	import { fetchTransactions } from '$lib/api';
	import type { TransactionFilters } from '$lib/api';

	const PAGE_SIZE = 50;

	let data: TransactionList | null = $state(null);
	let loading = $state(true);
	let fetchError = $state('');

	// Filters
	let search = $state('');
	let entityFilter = $state('');
	let statusFilter = $state('');
	let dateFrom = $state('');
	let dateTo = $state('');

	// Sort
	let sortBy = $state('date');
	let sortOrder: 'asc' | 'desc' = $state('desc');

	// Pagination
	let offset = $state(0);

	// Derived
	let items = $derived(data?.items ?? []);
	let income   = $derived(items.filter(t => t.amount > 0).reduce((s, t) => s + t.amount, 0));
	let expenses = $derived(items.filter(t => t.amount < 0).reduce((s, t) => s + t.amount, 0));
	let net      = $derived(income + expenses);
	let totalPages  = $derived(data ? Math.ceil(data.total / PAGE_SIZE) : 0);
	let currentPage = $derived(Math.floor(offset / PAGE_SIZE) + 1);

	onMount(() => load());

	async function load() {
		loading = true;
		fetchError = '';
		try {
			const filters: TransactionFilters = {
				limit: PAGE_SIZE,
				offset,
				sort_by: sortBy,
				sort_order: sortOrder
			};
			if (search)       filters.search    = search;
			if (entityFilter) filters.entity    = entityFilter;
			if (statusFilter) filters.status    = statusFilter;
			if (dateFrom)     filters.date_from = dateFrom;
			if (dateTo)       filters.date_to   = dateTo;

			data = await fetchTransactions(filters);
		} catch (e) {
			fetchError = e instanceof Error ? e.message : 'Failed to load transactions';
		} finally {
			loading = false;
		}
	}

	function applyFilters() {
		offset = 0;
		load();
	}

	function handleSort(col: string) {
		if (sortBy === col) {
			sortOrder = sortOrder === 'asc' ? 'desc' : 'asc';
		} else {
			sortBy = col;
			sortOrder = col === 'date' ? 'desc' : 'asc';
		}
		offset = 0;
		load();
	}

	function sortIndicator(col: string): string {
		if (sortBy !== col) return '';
		return sortOrder === 'asc' ? ' ↑' : ' ↓';
	}

	function formatDate(iso: string): string {
		return new Intl.DateTimeFormat('en-US', {
			month: 'short', day: 'numeric', year: 'numeric'
		}).format(new Date(iso));
	}

	function formatCurrency(amount: number): string {
		return new Intl.NumberFormat('en-US', {
			style: 'currency',
			currency: 'USD',
			minimumFractionDigits: 2
		}).format(amount);
	}

	function entityLabel(e: string | null): string {
		if (!e) return '—';
		const m: Record<string, string> = {
			sparkry: 'Sparkry',
			blackline: 'BlackLine',
			personal: 'Personal'
		};
		return m[e] ?? e;
	}

	function categoryLabel(c: string | null): string {
		if (!c) return '—';
		return c.replace(/_/g, ' ').toLowerCase().replace(/\b\w/g, (ch) => ch.toUpperCase());
	}

	let searchTimeout: ReturnType<typeof setTimeout>;
	function onSearchInput() {
		clearTimeout(searchTimeout);
		searchTimeout = setTimeout(applyFilters, 300);
	}

	function prevPage() {
		offset = Math.max(0, offset - PAGE_SIZE);
		load();
	}

	function nextPage() {
		offset += PAGE_SIZE;
		load();
	}

	function clearFilters() {
		search = '';
		entityFilter = '';
		statusFilter = '';
		dateFrom = '';
		dateTo = '';
		applyFilters();
	}
</script>

<div class="container page-shell">
	<header class="page-header">
		<div>
			<h1>Register</h1>
			{#if data}
				<p class="page-subtitle">{data.total.toLocaleString()} transactions</p>
			{/if}
		</div>
	</header>

	<!-- Filter bar -->
	<div class="filter-bar card">
		<input
			type="search"
			placeholder="Search vendor, description…"
			bind:value={search}
			oninput={onSearchInput}
			class="filter-search"
			aria-label="Search transactions"
		/>

		<select bind:value={entityFilter} onchange={applyFilters} aria-label="Filter by entity">
			<option value="">All entities</option>
			<option value="sparkry">Sparkry AI LLC</option>
			<option value="blackline">BlackLine MTB LLC</option>
			<option value="personal">Personal</option>
		</select>

		<select bind:value={statusFilter} onchange={applyFilters} aria-label="Filter by status">
			<option value="">All statuses</option>
			<option value="needs_review">Needs Review</option>
			<option value="confirmed">Confirmed</option>
			<option value="auto_classified">Auto Classified</option>
			<option value="rejected">Rejected</option>
			<option value="split_parent">Split Parent</option>
		</select>

		<label class="filter-date-label" for="date-from">From</label>
		<input
			id="date-from"
			type="date"
			bind:value={dateFrom}
			onchange={applyFilters}
			aria-label="Start date"
		/>

		<label class="filter-date-label" for="date-to">To</label>
		<input
			id="date-to"
			type="date"
			bind:value={dateTo}
			onchange={applyFilters}
			aria-label="End date"
		/>

		{#if search || entityFilter || statusFilter || dateFrom || dateTo}
			<button class="btn btn-ghost" onclick={clearFilters}>Clear</button>
		{/if}
	</div>

	{#if fetchError}
		<div class="card error-card">
			<p class="error-msg">{fetchError}</p>
			<button class="btn btn-ghost" onclick={load}>Try again</button>
		</div>
	{:else}
		<div class="table-wrapper card">
			{#if loading}
				<div class="table-loading">Loading…</div>
			{:else if items.length === 0}
				<div class="empty-state" style="padding: 48px 24px;">
					<span class="icon">○</span>
					<p>No transactions match your filters.</p>
				</div>
			{:else}
				<div class="table-scroll">
					<table class="data-table">
						<thead>
							<tr>
								<th
									class="sortable"
									onclick={() => handleSort('date')}
									onkeydown={(e) => e.key === 'Enter' && handleSort('date')}
									role="columnheader"
									tabindex="0"
								>
									Date{sortIndicator('date')}
								</th>
								<th
									class="sortable"
									onclick={() => handleSort('vendor')}
									onkeydown={(e) => e.key === 'Enter' && handleSort('vendor')}
									role="columnheader"
									tabindex="0"
								>
									Vendor{sortIndicator('vendor')}
								</th>
								<th
									class="sortable"
									onclick={() => handleSort('tax_category')}
									onkeydown={(e) => e.key === 'Enter' && handleSort('tax_category')}
									role="columnheader"
									tabindex="0"
								>
									Category{sortIndicator('tax_category')}
								</th>
								<th
									class="sortable col-amount"
									onclick={() => handleSort('amount')}
									onkeydown={(e) => e.key === 'Enter' && handleSort('amount')}
									role="columnheader"
									tabindex="0"
								>
									Amount{sortIndicator('amount')}
								</th>
								<th
									class="sortable"
									onclick={() => handleSort('entity')}
									onkeydown={(e) => e.key === 'Enter' && handleSort('entity')}
									role="columnheader"
									tabindex="0"
								>
									Entity{sortIndicator('entity')}
								</th>
								<th>Status</th>
							</tr>
						</thead>
						<tbody>
							{#each items as tx (tx.id)}
								<tr class="row-{tx.status}">
									<td class="col-date">{formatDate(tx.date)}</td>
									<td class="col-vendor">
										<span class="truncate" style="max-width: 280px; display: block;">
											{tx.vendor ?? tx.description}
										</span>
										{#if tx.vendor && tx.description !== tx.vendor}
											<span class="row-desc truncate" style="max-width: 280px; display: block;">
												{tx.description}
											</span>
										{/if}
									</td>
									<td class="col-category">{categoryLabel(tx.tax_category)}</td>
									<td
										class="col-amount"
										class:amount-positive={tx.amount > 0}
										class:amount-negative={tx.amount < 0}
									>
										{formatCurrency(tx.amount)}
									</td>
									<td class="col-entity">{entityLabel(tx.entity)}</td>
									<td class="no-strike">
										<span class="status-pill status-{tx.status}">
											{tx.status.replace(/_/g, ' ')}
										</span>
									</td>
								</tr>
							{/each}
						</tbody>
						<tfoot>
							<tr>
								<td colspan="3" class="footer-label">Page totals</td>
								<td class="col-amount">
									<div class="totals">
										<span class="total-row">
											<span class="total-label">Income</span>
											<span class="amount-positive">{formatCurrency(income)}</span>
										</span>
										<span class="total-row">
											<span class="total-label">Expenses</span>
											<span class="amount-negative">{formatCurrency(expenses)}</span>
										</span>
										<span class="total-row total-net">
											<span class="total-label">Net</span>
											<span class:amount-positive={net >= 0} class:amount-negative={net < 0}>
												{formatCurrency(net)}
											</span>
										</span>
									</div>
								</td>
								<td colspan="2"></td>
							</tr>
						</tfoot>
					</table>
				</div>

				{#if totalPages > 1}
					<div class="pagination">
						<button
							class="btn btn-ghost"
							disabled={offset === 0}
							onclick={prevPage}
						>
							← Previous
						</button>
						<span class="page-info">Page {currentPage} of {totalPages}</span>
						<button
							class="btn btn-ghost"
							disabled={offset + PAGE_SIZE >= (data?.total ?? 0)}
							onclick={nextPage}
						>
							Next →
						</button>
					</div>
				{/if}
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
		margin-bottom: 20px;
	}

	.page-subtitle {
		margin-top: 4px;
		color: var(--text-muted);
		font-size: .9rem;
	}

	.filter-bar {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: 8px;
		padding: 12px 16px;
		margin-bottom: 16px;
	}

	.filter-search {
		flex: 1;
		min-width: 200px;
	}

	.filter-date-label {
		font-size: .75rem;
		color: var(--text-muted);
		font-weight: 500;
	}

	.table-wrapper {
		overflow: hidden;
	}

	.table-scroll {
		overflow-x: auto;
	}

	.table-loading {
		padding: 40px;
		text-align: center;
		color: var(--text-muted);
		font-size: .875rem;
	}

	.col-date     { white-space: nowrap; color: var(--text-muted); font-size: .8rem; }
	.col-vendor   { max-width: 300px; }
	.col-amount   { text-align: right; font-variant-numeric: tabular-nums; font-weight: 600; white-space: nowrap; }
	.col-category { white-space: nowrap; font-size: .8rem; }
	.col-entity   { white-space: nowrap; font-size: .8rem; color: var(--text-muted); }

	.row-desc {
		font-size: .75rem;
		color: var(--text-muted);
	}

	.totals {
		display: flex;
		flex-direction: column;
		gap: 2px;
		align-items: flex-end;
	}

	.total-row {
		display: flex;
		gap: 10px;
		font-size: .8rem;
	}

	.total-net {
		border-top: 1px solid var(--border);
		padding-top: 3px;
		margin-top: 2px;
		font-weight: 700;
	}

	.total-label {
		color: var(--text-muted);
	}

	.footer-label {
		color: var(--text-muted);
		font-size: .75rem;
		text-transform: uppercase;
		letter-spacing: .05em;
	}

	.pagination {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 16px;
		padding: 16px;
		border-top: 1px solid var(--border);
	}

	.page-info {
		font-size: .875rem;
		color: var(--text-muted);
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
</style>
