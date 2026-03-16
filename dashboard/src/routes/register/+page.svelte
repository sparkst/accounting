<script lang="ts">
	import { onMount } from 'svelte';
	import type { Transaction, TransactionList } from '$lib/types';
	import { fetchTransactions, updateTransaction, confirmTransaction } from '$lib/api';
	import type { TransactionFilters } from '$lib/api';
	import TransactionCard from '$lib/components/TransactionCard.svelte';
	import { DATE_PRESETS } from '$lib/datePresets';

	const PAGE_SIZES = [25, 50, 100, 200];
	let pageSize = $state(50);

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

	// Expanded row
	let expandedId = $state<string | null>(null);

	// Derived
	let items = $derived(data?.items ?? []);
	let incomeTotalAll  = $derived(data?.income_total ?? 0);
	let expenseTotalAll = $derived(data?.expense_total ?? 0);
	let netAll          = $derived(incomeTotalAll + expenseTotalAll);
	let totalPages  = $derived(data ? Math.ceil(data.total / pageSize) : 0);
	let currentPage = $derived(Math.floor(offset / pageSize) + 1);

	onMount(() => load());

	async function load() {
		loading = true;
		fetchError = '';
		try {
			const filters: TransactionFilters = {
				limit: pageSize,
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

	let datePreset = $state('');
	const datePresetGroups = [...new Set(DATE_PRESETS.map(p => p.group))];

	function handleDatePreset() {
		if (!datePreset) {
			dateFrom = '';
			dateTo = '';
		} else {
			const preset = DATE_PRESETS.find(p => p.label === datePreset);
			if (preset) {
				const range = preset.range();
				dateFrom = range.from;
				dateTo = range.to;
			}
		}
		applyFilters();
	}

	let searchTimeout: ReturnType<typeof setTimeout>;
	function onSearchInput() {
		clearTimeout(searchTimeout);
		searchTimeout = setTimeout(applyFilters, 300);
	}

	function prevPage() {
		offset = Math.max(0, offset - pageSize);
		load();
	}

	function nextPage() {
		offset += pageSize;
		load();
	}

	function handlePageSizeChange() {
		offset = 0;
		load();
	}

	function clearFilters() {
		search = '';
		entityFilter = '';
		statusFilter = '';
		dateFrom = '';
		dateTo = '';
		datePreset = '';
		applyFilters();
	}

	function toggleRow(id: string) {
		expandedId = expandedId === id ? null : id;
	}

	function handleRowConfirmed(tx: Transaction) {
		// Refresh the list after a confirm/reject from the expanded card
		load();
		expandedId = null;
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

	<!-- Summary cards -->
	{#if data && data.total > 0}
		<div class="summary-row">
			<div class="summary-card">
				<span class="summary-label">Income</span>
				<span class="summary-value amount-positive">{formatCurrency(incomeTotalAll)}</span>
			</div>
			<div class="summary-card">
				<span class="summary-label">Expenses</span>
				<span class="summary-value amount-negative">{formatCurrency(expenseTotalAll)}</span>
			</div>
			<div class="summary-card">
				<span class="summary-label">Net</span>
				<span class="summary-value" class:amount-positive={netAll >= 0} class:amount-negative={netAll < 0}>
					{formatCurrency(netAll)}
				</span>
			</div>
			<div class="summary-card">
				<span class="summary-label">Transactions</span>
				<span class="summary-value">{data.total.toLocaleString()}</span>
			</div>
		</div>
	{/if}

	<!-- Filter bar -->
	<div class="filter-bar card">
		<input
			type="search"
			placeholder="Search…"
			bind:value={search}
			oninput={onSearchInput}
			class="filter-search filter-search-compact"
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
		</select>

		<select bind:value={datePreset} onchange={handleDatePreset} aria-label="Date range preset" class="filter-date-preset">
			<option value="">Date range…</option>
			{#each datePresetGroups as group}
				<optgroup label={group}>
					{#each DATE_PRESETS.filter(p => p.group === group) as preset}
						<option value={preset.label}>{preset.label}</option>
					{/each}
				</optgroup>
			{/each}
		</select>

		<input
			type="date"
			bind:value={dateFrom}
			onchange={() => { datePreset = ''; applyFilters(); }}
			aria-label="Start date"
			class="filter-date-input"
		/>
		<span class="filter-date-sep">–</span>
		<input
			type="date"
			bind:value={dateTo}
			onchange={() => { datePreset = ''; applyFilters(); }}
			aria-label="End date"
			class="filter-date-input"
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
								<th
									class="sortable"
									onclick={() => handleSort('status')}
									onkeydown={(e) => e.key === 'Enter' && handleSort('status')}
									role="columnheader"
									tabindex="0"
								>
									Status{sortIndicator('status')}
								</th>
							</tr>
						</thead>
						<tbody>
							{#each items as tx (tx.id)}
								<!-- svelte-ignore a11y_click_events_have_key_events -->
								<tr
									class="row-{tx.status}"
									class:row-expandable={true}
									class:row-expanded={expandedId === tx.id}
									onclick={() => toggleRow(tx.id)}
								>
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
										{tx.amount ? formatCurrency(tx.amount) : '—'}
									</td>
									<td class="col-entity">{entityLabel(tx.entity)}</td>
									<td class="no-strike">
										<span class="status-pill status-{tx.status}">
											{tx.status.replace(/_/g, ' ')}
										</span>
									</td>
								</tr>
								{#if expandedId === tx.id}
									<tr class="expanded-row">
										<td colspan="6">
											<div class="expanded-card-wrap">
												<TransactionCard
													transaction={tx}
													onconfirmed={handleRowConfirmed}
												/>
											</div>
										</td>
									</tr>
								{/if}
							{/each}
						</tbody>
					</table>
				</div>

				<div class="pagination">
					<div class="pagination-left">
						<label class="page-size-label" for="page-size">Show</label>
						<select id="page-size" bind:value={pageSize} onchange={handlePageSizeChange} class="page-size-select">
							{#each PAGE_SIZES as size}
								<option value={size}>{size}</option>
							{/each}
						</select>
					</div>
					{#if totalPages > 1}
						<div class="pagination-center">
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
								disabled={offset + pageSize >= (data?.total ?? 0)}
								onclick={nextPage}
							>
								Next →
							</button>
						</div>
					{/if}
				</div>
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

	/* Summary cards */
	.summary-row {
		display: flex;
		gap: 12px;
		margin-bottom: 16px;
		flex-wrap: wrap;
	}

	.summary-card {
		flex: 1;
		min-width: 140px;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		padding: 16px 20px;
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.summary-label {
		font-size: .7rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: .06em;
		color: var(--text-muted);
	}

	.summary-value {
		font-size: 1.3rem;
		font-weight: 700;
		font-variant-numeric: tabular-nums;
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
		min-width: 120px;
	}

	.filter-search-compact {
		max-width: 180px;
	}

	.filter-date-preset {
		min-width: 140px;
	}

	.filter-date-input {
		width: 130px;
		font-size: .8rem;
	}

	.filter-date-sep {
		color: var(--text-muted);
		font-size: .8rem;
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

	.row-expandable {
		cursor: pointer;
		transition: background .1s;
	}
	.row-expandable:hover {
		background: var(--gray-50);
	}
	.row-expanded {
		background: var(--gray-50);
	}

	.expanded-row td {
		padding: 0;
		border-top: none;
	}

	.expanded-card-wrap {
		padding: 8px 12px 16px;
		border-bottom: 2px solid var(--border);
	}

	.pagination {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 12px 16px;
		border-top: 1px solid var(--border);
	}

	.pagination-left {
		display: flex;
		align-items: center;
		gap: 6px;
	}

	.pagination-center {
		display: flex;
		align-items: center;
		gap: 16px;
	}

	.page-size-label {
		font-size: .75rem;
		color: var(--text-muted);
	}

	.page-size-select {
		width: 70px;
		padding: 4px 6px;
		font-size: .8rem;
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
