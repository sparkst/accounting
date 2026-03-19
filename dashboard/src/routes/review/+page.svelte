<script lang="ts">
	import { onMount } from 'svelte';
	import type { Transaction } from '$lib/types';
	import { fetchReviewQueue, updateTransaction, bulkConfirmTransactions } from '$lib/api';
	import TransactionCard from '$lib/components/TransactionCard.svelte';
	import Toast from '$lib/components/Toast.svelte';
	import ShortcutOverlay from '$lib/components/ShortcutOverlay.svelte';
	import { DATE_PRESETS } from '$lib/datePresets';
	import { BUSINESS_CATEGORIES, PERSONAL_CATEGORIES } from '$lib/categories';

	let items: Transaction[] = $state([]);
	let loading = $state(true);
	let fetchError = $state('');
	let focusedIndex = $state(0);

	// Status filter (server-side — determines which statuses are fetched)
	let statusFilter = $state('needs_review'); // 'needs_review' | 'needs_review,auto_classified' | 'auto_classified'

	// Filters (client-side — review queue is typically <200 items)
	let search = $state('');
	let entityFilter = $state('');
	let directionFilter = $state('');
	let categoryFilter = $state('');
	let amountFilter = $state(''); // '' | 'has' | 'missing'
	let datePreset = $state('');
	let dateFrom = $state('');
	let dateTo = $state('');

	// Batch selection
	let selectedIds = $state<Set<string>>(new Set());
	let lastSelectedIndex = $state<number | null>(null);
	let batchEntity = $state('');
	let batchCategory = $state('');
	let batchSaving = $state(false);

	// Toast state
	let toasts = $state<Array<{ id: number; message: string; type: 'info' | 'success' | 'error'; undoAction?: () => void }>>([]);
	let toastCounter = $state(0);

	// Shortcut overlay
	let showShortcuts = $state(false);

	// Card component refs
	let cardRefs = $state<Record<string, TransactionCard>>({});


	// Priority sorting for review queue
	function priorityScore(tx: Transaction): number {
		// Lower score = higher priority
		if (tx.status === 'needs_review') {
			// errors first (no amount or missing entity)
			if (!tx.amount) return 0;
			// duplicates (check for duplicate-related notes/reasoning)
			const text = `${tx.reasoning ?? ''} ${tx.notes ?? ''}`.toLowerCase();
			if (text.includes('duplicate')) return 1;
			// low confidence
			if (tx.confidence !== null && tx.confidence < 0.7) return 2;
			// first-time vendors (no confidence = never seen)
			if (tx.confidence === null) return 3;
			// pending reimbursables
			if (tx.direction === 'reimbursable') return 4;
			return 5;
		}
		return 10;
	}

	let filteredItems = $derived(
		items.filter((tx) => {
			if (entityFilter && tx.entity !== entityFilter) return false;
			if (directionFilter) {
				if (directionFilter === 'other') {
					if (tx.direction === 'income' || tx.direction === 'expense') return false;
				} else if (tx.direction !== directionFilter) {
					return false;
				}
			}
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
		}).sort((a, b) => priorityScore(a) - priorityScore(b))
	);

	let hasActiveFilters = $derived(
		!!search || !!entityFilter || !!directionFilter || !!categoryFilter || !!amountFilter || !!dateFrom || !!dateTo || statusFilter !== 'needs_review'
	);

	let hasSelection = $derived(selectedIds.size > 0);

	let allVisibleSelected = $derived(
		filteredItems.length > 0 && filteredItems.every(item => selectedIds.has(item.id))
	);

	function toggleSelectAll() {
		if (allVisibleSelected) {
			selectedIds = new Set();
		} else {
			selectedIds = new Set(filteredItems.map(item => item.id));
		}
	}

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
			items = await fetchReviewQueue(statusFilter);
		} catch (e) {
			fetchError = e instanceof Error ? e.message : 'Failed to load review queue';
		} finally {
			loading = false;
		}
	}

	async function handleStatusFilterChange() {
		await load();
	}

	function handleConfirmed(tx: Transaction) {
		items = items.filter((t) => t.id !== tx.id);
		selectedIds.delete(tx.id);
		selectedIds = new Set(selectedIds);
		if (focusedIndex >= filteredItems.length) {
			focusedIndex = Math.max(0, filteredItems.length - 1);
		}
	}

	function clearFilters() {
		search = '';
		entityFilter = '';
		directionFilter = '';
		categoryFilter = '';
		amountFilter = '';
		datePreset = '';
		dateFrom = '';
		dateTo = '';
	}

	// ── Toast helpers ────────────────────────────────────────────────────────

	function addToast(message: string, type: 'info' | 'success' | 'error' = 'info', undoAction?: () => void) {
		const id = ++toastCounter;
		toasts = [...toasts, { id, message, type, undoAction }];
		return id;
	}

	function removeToast(id: number) {
		toasts = toasts.filter(t => t.id !== id);
	}

	// ── Reject with undo ─────────────────────────────────────────────────────

	async function rejectWithUndo() {
		const tx = filteredItems[focusedIndex];
		if (!tx) return;

		// Optimistically remove
		const originalItems = [...items];
		items = items.filter(t => t.id !== tx.id);
		if (focusedIndex >= filteredItems.length) {
			focusedIndex = Math.max(0, filteredItems.length - 1);
		}

		let undone = false;
		const toastId = addToast(`Rejected "${tx.vendor ?? tx.description}"`, 'info', () => {
			undone = true;
			items = originalItems;
			removeToast(toastId);
		});

		// After 5 seconds, actually persist if not undone
		setTimeout(async () => {
			removeToast(toastId);
			if (!undone) {
				try {
					await updateTransaction(tx.id, { status: 'rejected' });
				} catch {
					// If the API call fails, restore the item
					items = originalItems;
					addToast('Failed to reject transaction', 'error');
				}
			}
		}, 5000);
	}

	// ── Selection ────────────────────────────────────────────────────────────

	function toggleSelect(tx: Transaction) {
		if (selectedIds.has(tx.id)) {
			selectedIds.delete(tx.id);
		} else {
			selectedIds.add(tx.id);
		}
		selectedIds = new Set(selectedIds);
		lastSelectedIndex = filteredItems.findIndex(t => t.id === tx.id);
	}

	function handleShiftClick(tx: Transaction, index: number) {
		if (lastSelectedIndex === null) {
			toggleSelect(tx);
			return;
		}
		const start = Math.min(lastSelectedIndex, index);
		const end = Math.max(lastSelectedIndex, index);
		for (let i = start; i <= end; i++) {
			selectedIds.add(filteredItems[i].id);
		}
		selectedIds = new Set(selectedIds);
		lastSelectedIndex = index;
	}

	function clearSelection() {
		selectedIds = new Set();
		lastSelectedIndex = null;
	}

	async function bulkConfirm() {
		if (batchSaving || selectedIds.size === 0) return;
		batchSaving = true;
		try {
			const ids = [...selectedIds];
			await bulkConfirmTransactions(ids, batchEntity, batchCategory);
			items = items.filter(t => !selectedIds.has(t.id));
			const count = ids.length;
			clearSelection();
			batchEntity = '';
			batchCategory = '';
			addToast(`Confirmed ${count} transaction${count !== 1 ? 's' : ''}`, 'success');
			if (focusedIndex >= filteredItems.length) {
				focusedIndex = Math.max(0, filteredItems.length - 1);
			}
		} catch (e) {
			addToast(e instanceof Error ? e.message : 'Bulk confirm failed', 'error');
		} finally {
			batchSaving = false;
		}
	}

	// ── Keyboard ─────────────────────────────────────────────────────────────

	function handleKeydown(e: KeyboardEvent) {
		const tag = (e.target as HTMLElement)?.tagName;
		if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;

		if (e.key === '?') {
			e.preventDefault();
			showShortcuts = !showShortcuts;
			return;
		}

		if (showShortcuts) return;

		if (e.key === 'j' || e.key === 'ArrowDown') {
			e.preventDefault();
			focusedIndex = Math.min(focusedIndex + 1, filteredItems.length - 1);
			scrollToFocused();
		} else if (e.key === 'k' || e.key === 'ArrowUp') {
			e.preventDefault();
			focusedIndex = Math.max(focusedIndex - 1, 0);
			scrollToFocused();
		} else if (e.key === 'e') {
			e.preventDefault();
			const tx = filteredItems[focusedIndex];
			if (tx) {
				const ref = cardRefs[tx.id];
				ref?.focusEntityField();
			}
		} else if (e.key === 's') {
			e.preventDefault();
			const tx = filteredItems[focusedIndex];
			if (tx) {
				const ref = cardRefs[tx.id];
				ref?.doToggleSplit();
			}
		} else if (e.key === 'd') {
			e.preventDefault();
			const tx = filteredItems[focusedIndex];
			if (tx) {
				// Mark as duplicate = reject
				rejectWithUndo();
			}
		} else if (e.key === 'r') {
			e.preventDefault();
			rejectWithUndo();
		} else if (e.key === 'n') {
			e.preventDefault();
			const tx = filteredItems[focusedIndex];
			if (tx) {
				const ref = cardRefs[tx.id];
				ref?.focusNotesField();
			}
		} else if (e.key === 'c') {
			e.preventDefault();
			const tx = filteredItems[focusedIndex];
			if (tx) {
				const ref = cardRefs[tx.id];
				ref?.focusCategoryField();
			}
		} else if (e.key === '1') {
			e.preventDefault();
			const tx = filteredItems[focusedIndex];
			if (tx) {
				const ref = cardRefs[tx.id];
				ref?.setEntity('sparkry');
			}
		} else if (e.key === '2') {
			e.preventDefault();
			const tx = filteredItems[focusedIndex];
			if (tx) {
				const ref = cardRefs[tx.id];
				ref?.setEntity('blackline');
			}
		} else if (e.key === '3') {
			e.preventDefault();
			const tx = filteredItems[focusedIndex];
			if (tx) {
				const ref = cardRefs[tx.id];
				ref?.setEntity('personal');
			}
		} else if (e.key === 'x') {
			e.preventDefault();
			const tx = filteredItems[focusedIndex];
			if (tx) toggleSelect(tx);
		} else if (e.key === 'A' && e.shiftKey) {
			e.preventDefault();
			toggleSelectAll();
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

{#if showShortcuts}
	<ShortcutOverlay onclose={() => (showShortcuts = false)} />
{/if}

<!-- Toasts -->
{#each toasts as toast (toast.id)}
	<Toast
		message={toast.message}
		type={toast.type}
		undoLabel={toast.undoAction ? 'Undo' : undefined}
		onundo={toast.undoAction}
		ondismiss={() => removeToast(toast.id)}
	/>
{/each}

<div class="container page-shell">
	<header class="page-header">
		<div>
			<h1>Review Queue</h1>
			{#if !loading}
				<p class="page-subtitle">
					{#if items.length > 0}
						{#if statusFilter === 'auto_classified'}
							{items.length} auto-classified item{items.length !== 1 ? 's' : ''} to review
						{:else if statusFilter === 'needs_review,auto_classified'}
							{items.length} item{items.length !== 1 ? 's' : ''} need attention (including auto-classified)
						{:else}
							{items.length} item{items.length !== 1 ? 's' : ''} need attention
						{/if}
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
			<select
				bind:value={statusFilter}
				onchange={handleStatusFilterChange}
				aria-label="Status filter"
				class="filter-status"
			>
				<option value="needs_review">Needs review only</option>
				<option value="needs_review,auto_classified">Include auto-classified</option>
				<option value="auto_classified">Auto-classified only</option>
			</select>

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

			<select bind:value={directionFilter} aria-label="Filter by direction">
				<option value="">All directions</option>
				<option value="income">Income</option>
				<option value="expense">Expense</option>
				<option value="other">Other (transfers, capital, reimbursable)</option>
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

			<button
				class="btn btn-ghost"
				onclick={toggleSelectAll}
				title="Shift+A"
			>
				{allVisibleSelected ? 'Deselect all' : `Select all (${filteredItems.length})`}
			</button>

			{#if hasActiveFilters}
				<button class="btn btn-ghost filter-clear" onclick={clearFilters}>Clear</button>
			{/if}
		</div>

		<!-- Batch bar (appears when items selected) -->
		{#if hasSelection}
			<div class="batch-bar card">
				<span class="batch-count">{selectedIds.size} selected</span>

				<select bind:value={batchEntity} aria-label="Set entity for selection" class="batch-select">
					<option value="">Entity…</option>
					<option value="sparkry">Sparkry AI LLC</option>
					<option value="blackline">BlackLine MTB LLC</option>
					<option value="personal">Personal</option>
				</select>

				<select bind:value={batchCategory} aria-label="Set category for selection" class="batch-select">
					<option value="">Category…</option>
					<optgroup label="Business">
						{#each BUSINESS_CATEGORIES as cat}
							<option value={cat.value}>{cat.label}</option>
						{/each}
					</optgroup>
					<optgroup label="Personal">
						{#each PERSONAL_CATEGORIES as cat}
							<option value={cat.value}>{cat.label}</option>
						{/each}
					</optgroup>
				</select>

				<button class="btn btn-primary" onclick={bulkConfirm} disabled={batchSaving}>
					{batchSaving ? 'Confirming…' : `Bulk Confirm (${selectedIds.size})`}
				</button>
				<button class="btn btn-ghost" onclick={clearSelection}>Cancel</button>
			</div>
		{/if}

		<!-- Result count -->
		<p class="results-count" aria-live="polite">
			{#if hasActiveFilters}
				Showing {filteredItems.length} of {items.length} item{items.length !== 1 ? 's' : ''}
			{:else}
				{filteredItems.length} item{filteredItems.length !== 1 ? 's' : ''}
			{/if}
		</p>

		<div class="keyboard-hint" aria-live="polite">
			<kbd>j</kbd><kbd>k</kbd> navigate &nbsp;·&nbsp;
			<kbd>y</kbd> confirm &nbsp;·&nbsp;
			<kbd>e</kbd> edit &nbsp;·&nbsp;
			<kbd>s</kbd> split &nbsp;·&nbsp;
			<kbd>r</kbd> reject &nbsp;·&nbsp;
			<kbd>1</kbd><kbd>2</kbd><kbd>3</kbd> entity &nbsp;·&nbsp;
			<kbd>?</kbd> all shortcuts
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
							bind:this={cardRefs[tx.id]}
							transaction={tx}
							focused={focusedIndex === i}
							selected={selectedIds.has(tx.id)}
							onconfirmed={handleConfirmed}
							onfocusrequest={() => (focusedIndex = i)}
							onselect={(t) => toggleSelect(t)}
							onreject={(t) => {
								// handled by onconfirmed removing from list
							}}
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

	.filter-status {
		min-width: 180px;
		flex-shrink: 0;
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

	/* ── Batch bar ───────────────────────────────────────────────────────── */
	.batch-bar {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 10px 16px;
		margin-bottom: 10px;
		background: rgba(59,130,246,.06);
		border-color: var(--blue-500);
		flex-wrap: wrap;
	}

	.batch-count {
		font-size: .85rem;
		font-weight: 600;
		color: var(--blue-600);
	}

	.batch-select {
		font-size: .82rem;
		padding: 5px 8px;
		min-width: 120px;
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
		flex-wrap: wrap;
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
