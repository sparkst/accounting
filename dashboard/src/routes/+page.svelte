<script lang="ts">
	import { onMount } from 'svelte';
	import type { Transaction } from '$lib/types';
	import { fetchReviewQueue } from '$lib/api';
	import TransactionCard from '$lib/components/TransactionCard.svelte';
	import { DATE_PRESETS } from '$lib/datePresets';

	let items: Transaction[] = $state([]);
	let loading = $state(true);
	let fetchError = $state('');
	let focusedIndex = $state(0);

	// Filters (client-side — review queue is typically <200 items)
	let search = $state('');
	let entityFilter = $state('');
	let categoryFilter = $state('');
	let amountFilter = $state(''); // '' | 'has' | 'missing'
	let datePreset = $state('');
	let dateFrom = $state('');
	let dateTo = $state('');

	const BUSINESS_CATEGORIES = [
		{ value: 'ADVERTISING',           label: 'Advertising' },
		{ value: 'CAR_AND_TRUCK',          label: 'Car & Truck' },
		{ value: 'CONTRACT_LABOR',         label: 'Contract Labor' },
		{ value: 'INSURANCE',              label: 'Insurance' },
		{ value: 'LEGAL_AND_PROFESSIONAL', label: 'Legal & Professional' },
		{ value: 'OFFICE_EXPENSE',         label: 'Office Expense' },
		{ value: 'SUPPLIES',               label: 'Supplies' },
		{ value: 'TAXES_AND_LICENSES',     label: 'Taxes & Licenses' },
		{ value: 'TRAVEL',                 label: 'Travel' },
		{ value: 'MEALS',                  label: 'Meals (50%)' },
		{ value: 'COGS',                   label: 'COGS' },
		{ value: 'CONSULTING_INCOME',      label: 'Consulting Income' },
		{ value: 'SUBSCRIPTION_INCOME',    label: 'Subscription Income' },
		{ value: 'SALES_INCOME',           label: 'Sales Income' },
		{ value: 'REIMBURSABLE',           label: 'Reimbursable' }
	];

	const PERSONAL_CATEGORIES = [
		{ value: 'CHARITABLE_CASH',         label: 'Charitable (Cash)' },
		{ value: 'CHARITABLE_STOCK',        label: 'Charitable (Stock)' },
		{ value: 'MEDICAL',                 label: 'Medical' },
		{ value: 'STATE_LOCAL_TAX',         label: 'State & Local Tax' },
		{ value: 'MORTGAGE_INTEREST',       label: 'Mortgage Interest' },
		{ value: 'INVESTMENT_INCOME',       label: 'Investment Income' },
		{ value: 'PERSONAL_NON_DEDUCTIBLE', label: 'Personal (Non-deductible)' }
	];

	let filteredItems = $derived(items.filter((tx) => {
		if (entityFilter && tx.entity !== entityFilter) return false;
		if (categoryFilter && tx.tax_category !== categoryFilter) return false;
		if (amountFilter === 'has' && !tx.amount) return false;
		if (amountFilter === 'missing' && tx.amount) return false;
		if (dateFrom && tx.date < dateFrom) return false;
		if (dateTo && tx.date > dateTo) return false;
		if (search) {
			const q = search.toLowerCase();
			const haystack = `${tx.vendor ?? ''} ${tx.description}`.toLowerCase();
			if (!haystack.includes(q)) return false;
		}
		return true;
	}));

	let hasActiveFilters = $derived(
		!!search || !!entityFilter || !!categoryFilter || !!amountFilter || !!dateFrom || !!dateTo
	);

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
	}

	onMount(async () => {
		await load();
	});

	async function load() {
		loading = true;
		fetchError = '';
		try {
			items = await fetchReviewQueue();
		} catch (e) {
			fetchError = e instanceof Error ? e.message : 'Failed to load review queue';
		} finally {
			loading = false;
		}
	}

	function handleConfirmed(tx: Transaction) {
		items = items.filter((t) => t.id !== tx.id);
		if (focusedIndex >= filteredItems.length) {
			focusedIndex = Math.max(0, filteredItems.length - 1);
		}
	}

	function clearFilters() {
		search = '';
		entityFilter = '';
		categoryFilter = '';
		amountFilter = '';
		datePreset = '';
		dateFrom = '';
		dateTo = '';
	}

	function handleKeydown(e: KeyboardEvent) {
		const tag = (e.target as HTMLElement)?.tagName;
		if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;

		if (e.key === 'j' || e.key === 'ArrowDown') {
			e.preventDefault();
			focusedIndex = Math.min(focusedIndex + 1, filteredItems.length - 1);
			scrollToFocused();
		} else if (e.key === 'k' || e.key === 'ArrowUp') {
			e.preventDefault();
			focusedIndex = Math.max(focusedIndex - 1, 0);
			scrollToFocused();
		}
	}

	function scrollToFocused() {
		requestAnimationFrame(() => {
			const el = document.querySelector('.tx-card.card-focused');
			el?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
		});
	}
</script>

<svelte:window onkeydown={handleKeydown} />

<div class="container page-shell">
	<header class="page-header">
		<div>
			<h1>Review Queue</h1>
			{#if !loading}
				<p class="page-subtitle">
					{#if items.length > 0}
						{items.length} item{items.length !== 1 ? 's' : ''} need attention
					{:else}
						All caught up
					{/if}
				</p>
			{/if}
		</div>
		<div class="page-header-actions">
			<button class="btn btn-ghost" onclick={load} disabled={loading}>
				{loading ? 'Loading…' : 'Refresh'}
			</button>
		</div>
	</header>

	{#if loading}
		<div class="queue-list">
			{#each Array(3) as _}
				<div class="card skeleton-card">
					<div class="skeleton" style="height: 18px; width: 40%; margin-bottom: 10px;"></div>
					<div class="skeleton" style="height: 28px; width: 65%; margin-bottom: 16px;"></div>
					<div class="skeleton" style="height: 36px; margin-bottom: 10px;"></div>
					<div class="skeleton" style="height: 36px; width: 50%;"></div>
				</div>
			{/each}
		</div>
	{:else if fetchError}
		<div class="card error-card">
			<p class="error-msg">{fetchError}</p>
			<button class="btn btn-ghost" onclick={load}>Try again</button>
		</div>
	{:else if items.length === 0}
		<div class="empty-state">
			<span class="icon">✓</span>
			<h2>All caught up!</h2>
			<p>There are no transactions waiting for review.</p>
			<button class="btn btn-ghost" onclick={load}>Refresh</button>
		</div>
	{:else}
		<!-- Filter bar -->
		<div class="filter-bar card">
			<input
				type="search"
				placeholder="Search…"
				bind:value={search}
				class="filter-search filter-search-compact"
				aria-label="Search transactions"
			/>

			<select bind:value={entityFilter} aria-label="Filter by entity">
				<option value="">All entities</option>
				<option value="sparkry">Sparkry AI LLC</option>
				<option value="blackline">BlackLine MTB LLC</option>
				<option value="personal">Personal</option>
			</select>

			<select bind:value={categoryFilter} aria-label="Filter by category">
				<option value="">All categories</option>
				<optgroup label="Business (Schedule C / 1065)">
					{#each BUSINESS_CATEGORIES as cat}
						<option value={cat.value}>{cat.label}</option>
					{/each}
				</optgroup>
				<optgroup label="Personal (Schedule A)">
					{#each PERSONAL_CATEGORIES as cat}
						<option value={cat.value}>{cat.label}</option>
					{/each}
				</optgroup>
			</select>

			<select bind:value={amountFilter} aria-label="Filter by amount">
				<option value="">All amounts</option>
				<option value="has">Has amount</option>
				<option value="missing">Missing amount</option>
			</select>

			<select bind:value={datePreset} onchange={handleDatePreset} aria-label="Date range" class="filter-date-preset">
				<option value="">Date range…</option>
				{#each datePresetGroups as group}
					<optgroup label={group}>
						{#each DATE_PRESETS.filter(p => p.group === group) as preset}
							<option value={preset.label}>{preset.label}</option>
						{/each}
					</optgroup>
				{/each}
			</select>

			{#if hasActiveFilters}
				<button class="btn btn-ghost filter-clear" onclick={clearFilters}>Clear</button>
			{/if}
		</div>

		<!-- Result count -->
		<p class="results-count" aria-live="polite">
			{#if hasActiveFilters}
				Showing {filteredItems.length} of {items.length} item{items.length !== 1 ? 's' : ''}
			{:else}
				{filteredItems.length} item{filteredItems.length !== 1 ? 's' : ''}
			{/if}
		</p>

		<div class="keyboard-hint" aria-live="polite">
			<kbd>j</kbd><kbd>k</kbd> navigate &nbsp;·&nbsp; <kbd>y</kbd> confirm focused card
		</div>

		{#if filteredItems.length === 0}
			<div class="empty-state">
				<span class="icon">○</span>
				<h2>No matches</h2>
				<p>No items match your current filters.</p>
				<button class="btn btn-ghost" onclick={clearFilters}>Clear filters</button>
			</div>
		{:else}
			<ul class="queue-list" aria-label="Review queue">
				{#each filteredItems as tx, i (tx.id)}
					<li>
						<TransactionCard
							transaction={tx}
							focused={focusedIndex === i}
							onconfirmed={handleConfirmed}
							onfocusrequest={() => (focusedIndex = i)}
						/>
					</li>
				{/each}
			</ul>
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

	/* ── Filter bar ──────────────────────────────────────────────────────── */
	.filter-bar {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: 8px;
		padding: 12px 16px;
		margin-bottom: 10px;
	}

	.filter-search {
		flex: 1;
		min-width: 120px;
	}

	.filter-search-compact {
		max-width: 160px;
	}

	.filter-date-preset {
		min-width: 140px;
	}

	.filter-clear {
		flex-shrink: 0;
	}

	.results-count {
		font-size: .8rem;
		color: var(--text-muted);
		margin-bottom: 10px;
	}

	/* ── Keyboard hint ───────────────────────────────────────────────────── */
	.keyboard-hint {
		font-size: .75rem;
		color: var(--text-muted);
		margin-bottom: 14px;
		display: flex;
		align-items: center;
		gap: 4px;
	}

	/* ── Queue list ──────────────────────────────────────────────────────── */
	.queue-list {
		display: flex;
		flex-direction: column;
		gap: 12px;
		list-style: none;
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

	kbd {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-width: 18px;
		height: 18px;
		padding: 0 4px;
		background: var(--gray-100);
		border: 1px solid var(--gray-300);
		border-radius: 3px;
		font-family: var(--font-mono);
		font-size: .65rem;
		color: var(--gray-700);
	}
</style>
