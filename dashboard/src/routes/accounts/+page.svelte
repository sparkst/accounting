<script lang="ts">
	import { onMount } from 'svelte';
	import {
		fetchVendorRules,
		createVendorRule,
		patchVendorRule,
		deleteVendorRule,
		type VendorRule,
		type VendorRuleCreate
	} from '$lib/api';
	import { entityBadgeClass } from '$lib/categories';
	import Toast from '$lib/components/Toast.svelte';

	// ── Constants ────────────────────────────────────────────────────────────
	const ENTITIES = ['sparkry', 'blackline', 'personal'] as const;
	const DIRECTIONS = ['income', 'expense', 'transfer', 'reimbursable'] as const;
	const TAX_CATEGORIES = [
		'ADVERTISING', 'CAR_AND_TRUCK', 'CONTRACT_LABOR', 'INSURANCE',
		'LEGAL_AND_PROFESSIONAL', 'OFFICE_EXPENSE', 'SUPPLIES', 'TAXES_AND_LICENSES',
		'TRAVEL', 'MEALS', 'COGS', 'CONSULTING_INCOME', 'SUBSCRIPTION_INCOME',
		'SALES_INCOME', 'REIMBURSABLE', 'CHARITABLE_CASH', 'CHARITABLE_STOCK',
		'MEDICAL', 'STATE_LOCAL_TAX', 'MORTGAGE_INTEREST', 'INVESTMENT_INCOME',
		'PERSONAL_NON_DEDUCTIBLE'
	] as const;

	const PAGE_SIZE = 25;

	// ── State ─────────────────────────────────────────────────────────────────
	let rules = $state<VendorRule[]>([]);
	let total = $state(0);
	let loading = $state(true);
	let error = $state('');

	// Search & pagination
	let searchInput = $state('');
	let searchQuery = $state('');
	let entityFilter = $state('');
	let offset = $state(0);

	// Inline editing state
	let editingCell = $state<{ ruleId: string; field: string } | null>(null);
	let editValue = $state('');
	let savingCell = $state(false);

	// Undo toast
	interface UndoToast {
		message: string;
		ruleId: string;
		field: string;
		previousValue: string;
	}
	let undoToast = $state<UndoToast | null>(null);

	// Add rule form
	let showAddForm = $state(false);
	let addForm = $state<VendorRuleCreate>({
		vendor_pattern: '',
		entity: 'sparkry',
		tax_category: 'OFFICE_EXPENSE',
		direction: 'expense',
		deductible_pct: 1.0,
		confidence: 1.0,
		source: 'human'
	});
	let addError = $state('');
	let addSaving = $state(false);

	// Delete confirmation
	let deleteTarget = $state<VendorRule | null>(null);
	let deleteError = $state('');
	let deleting = $state(false);

	// ── Derived ───────────────────────────────────────────────────────────────
	let totalPages = $derived(Math.ceil(total / PAGE_SIZE));
	let currentPage = $derived(Math.floor(offset / PAGE_SIZE) + 1);

	// ── Debounced search ─────────────────────────────────────────────────────
	let debounceTimer: ReturnType<typeof setTimeout> | null = null;

	function onSearchInput() {
		if (debounceTimer) clearTimeout(debounceTimer);
		debounceTimer = setTimeout(() => {
			searchQuery = searchInput;
			offset = 0;
			load();
		}, 300);
	}

	function onEntityFilterChange() {
		offset = 0;
		load();
	}

	// ── Data loading ─────────────────────────────────────────────────────────
	async function load() {
		loading = true;
		error = '';
		try {
			const resp = await fetchVendorRules({
				search: searchQuery || undefined,
				entity: entityFilter || undefined,
				limit: PAGE_SIZE,
				offset
			});
			rules = resp.items;
			total = resp.total;
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load vendor rules';
		} finally {
			loading = false;
		}
	}

	// ── Pagination ────────────────────────────────────────────────────────────
	function prevPage() {
		if (offset >= PAGE_SIZE) {
			offset -= PAGE_SIZE;
			load();
		}
	}

	function nextPage() {
		if (offset + PAGE_SIZE < total) {
			offset += PAGE_SIZE;
			load();
		}
	}

	// ── Inline editing ────────────────────────────────────────────────────────
	function startEdit(ruleId: string, field: string, currentValue: string) {
		editingCell = { ruleId, field };
		editValue = currentValue;
	}

	function cancelEdit() {
		editingCell = null;
		editValue = '';
	}

	async function saveEdit() {
		if (!editingCell) return;
		const { ruleId, field } = editingCell;

		// Capture previous value for undo
		const rule = rules.find((r) => r.id === ruleId);
		const previousValue = rule ? String((rule as unknown as Record<string, unknown>)[field] ?? '') : '';

		// Skip if value didn't change
		if (editValue === previousValue) {
			cancelEdit();
			return;
		}

		savingCell = true;
		try {
			await patchVendorRule(ruleId, { [field]: editValue });
			const fieldLabel = field === 'vendor_pattern' ? 'pattern'
				: field === 'tax_category' ? 'category'
				: field;
			undoToast = { message: `${fieldLabel} updated`, ruleId, field, previousValue };
			editingCell = null;
			editValue = '';
			await load();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Save failed';
		} finally {
			savingCell = false;
		}
	}

	async function handleUndoEdit() {
		if (!undoToast) return;
		const { ruleId, field, previousValue } = undoToast;
		undoToast = null;
		try {
			await patchVendorRule(ruleId, { [field]: previousValue });
			await load();
		} catch {
			// undo failed silently
		}
	}

	function onCellKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter') {
			e.preventDefault();
			saveEdit();
		} else if (e.key === 'Escape') {
			cancelEdit();
		}
	}

	// ── Add rule ──────────────────────────────────────────────────────────────
	function resetAddForm() {
		addForm = {
			vendor_pattern: '',
			entity: 'sparkry',
			tax_category: 'OFFICE_EXPENSE',
			direction: 'expense',
			deductible_pct: 1.0,
			confidence: 1.0,
			source: 'human'
		};
		addError = '';
	}

	async function submitAdd() {
		if (!addForm.vendor_pattern.trim()) {
			addError = 'Vendor pattern is required.';
			return;
		}
		addSaving = true;
		addError = '';
		try {
			await createVendorRule(addForm);
			showAddForm = false;
			resetAddForm();
			await load();
		} catch (e) {
			addError = e instanceof Error ? e.message : 'Failed to create rule';
		} finally {
			addSaving = false;
		}
	}

	// ── Delete ────────────────────────────────────────────────────────────────
	function confirmDelete(rule: VendorRule) {
		deleteTarget = rule;
		deleteError = '';
	}

	async function executeDelete() {
		if (!deleteTarget) return;
		deleting = true;
		deleteError = '';
		try {
			await deleteVendorRule(deleteTarget.id);
			deleteTarget = null;
			await load();
		} catch (e) {
			deleteError = e instanceof Error ? e.message : 'Delete failed';
		} finally {
			deleting = false;
		}
	}

	// ── Formatting helpers ────────────────────────────────────────────────────
	function entityLabel(e: string): string {
		const map: Record<string, string> = {
			sparkry: 'Sparkry AI',
			blackline: 'BlackLine MTB',
			personal: 'Personal'
		};
		return map[e] ?? e;
	}

	function fmtDate(iso: string | null): string {
		if (!iso) return '—';
		const d = new Date(iso.includes('T') ? iso : iso + 'T00:00:00');
		return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
	}

	function fmtCategory(c: string): string {
		return c.replace(/_/g, ' ').toLowerCase().replace(/\b\w/g, (ch) => ch.toUpperCase());
	}

	function confidenceColor(c: number): string {
		if (c >= 0.9) return 'var(--green-600)';
		if (c >= 0.7) return 'var(--amber-600)';
		return 'var(--red-600)';
	}

	onMount(() => {
		load();
	});
</script>

<div class="container page-shell">
	<!-- ── Header ──────────────────────────────────────────────────────────── -->
	<header class="page-header">
		<div>
			<h1>Accounts &amp; Memory</h1>
			<p class="page-subtitle">Vendor classification rules and entity configuration</p>
		</div>
	</header>

	<!-- ── Vendor Rules ───────────────────────────────────────────────────── -->
	<section class="dashboard-section">
		<div class="section-header">
			<h2 class="section-title">Vendor Rules</h2>
			<button class="btn btn-primary btn-sm" onclick={() => { showAddForm = !showAddForm; if (!showAddForm) resetAddForm(); }}>
				{showAddForm ? 'Cancel' : '+ Add Rule'}
			</button>
		</div>

		<!-- Search & filter bar -->
		<div class="toolbar">
			<input
				class="search-input"
				type="search"
				placeholder="Search vendor patterns…"
				aria-label="Search vendor rules"
				bind:value={searchInput}
				oninput={onSearchInput}
			/>
			<select class="filter-select" bind:value={entityFilter} onchange={onEntityFilterChange}>
				<option value="">All entities</option>
				{#each ENTITIES as e}
					<option value={e}>{entityLabel(e)}</option>
				{/each}
			</select>
		</div>

		<!-- Add rule inline form -->
		{#if showAddForm}
			<div class="card add-form-card">
				<h3 class="add-form-title">New Vendor Rule</h3>
				{#if addError}
					<p class="form-error">{addError}</p>
				{/if}
				<div class="add-form-grid">
					<label class="form-field">
						<span class="form-label">Vendor Pattern</span>
						<input class="form-input" type="text" bind:value={addForm.vendor_pattern} placeholder="e.g. Anthropic PBC" />
					</label>
					<label class="form-field">
						<span class="form-label">Entity</span>
						<select class="form-select" bind:value={addForm.entity}>
							{#each ENTITIES as e}
								<option value={e}>{entityLabel(e)}</option>
							{/each}
						</select>
					</label>
					<label class="form-field">
						<span class="form-label">Tax Category</span>
						<select class="form-select" bind:value={addForm.tax_category}>
							{#each TAX_CATEGORIES as c}
								<option value={c}>{fmtCategory(c)}</option>
							{/each}
						</select>
					</label>
					<label class="form-field">
						<span class="form-label">Direction</span>
						<select class="form-select" bind:value={addForm.direction}>
							{#each DIRECTIONS as d}
								<option value={d}>{d}</option>
							{/each}
						</select>
					</label>
					<label class="form-field">
						<span class="form-label">Confidence</span>
						<input class="form-input" type="number" min="0" max="1" step="0.05" bind:value={addForm.confidence} />
					</label>
					<label class="form-field">
						<span class="form-label">Deductible %</span>
						<input class="form-input" type="number" min="0" max="1" step="0.05" bind:value={addForm.deductible_pct} />
					</label>
				</div>
				<div class="add-form-actions">
					<button class="btn btn-primary btn-sm" onclick={submitAdd} disabled={addSaving}>
						{addSaving ? 'Saving…' : 'Save Rule'}
					</button>
					<button class="btn btn-ghost btn-sm" onclick={() => { showAddForm = false; resetAddForm(); }}>
						Cancel
					</button>
				</div>
			</div>
		{/if}

		<!-- Rules table -->
		{#if loading && rules.length === 0}
			<div class="card table-card">
				<div class="skeleton-rows">
					{#each Array(5) as _}
						<div class="skeleton-row">
							<div class="skeleton" style="height: 14px; width: 30%;"></div>
							<div class="skeleton" style="height: 14px; width: 12%;"></div>
							<div class="skeleton" style="height: 14px; width: 18%;"></div>
							<div class="skeleton" style="height: 14px; width: 8%;"></div>
						</div>
					{/each}
				</div>
			</div>
		{:else if error}
			<div class="card error-card">
				<p class="error-msg">{error}</p>
				<button class="btn btn-ghost btn-sm" onclick={load}>Retry</button>
			</div>
		{:else if rules.length === 0}
			<div class="card empty-card">
				<p class="empty-msg">No vendor rules found. Add one above to start classifying transactions automatically.</p>
			</div>
		{:else}
			<div class="card table-card">
				<div class="table-wrap">
					<table class="data-table">
						<thead>
							<tr>
								<th>Vendor Pattern</th>
								<th>Entity</th>
								<th>Tax Category</th>
								<th>Subcategory</th>
								<th>Confidence</th>
								<th>Matches</th>
								<th>Last Match</th>
								<th></th>
							</tr>
						</thead>
						<tbody>
							{#each rules as rule (rule.id)}
								<tr class="rule-row">
									<!-- Vendor Pattern — inline editable -->
									<td class="td-pattern">
										{#if editingCell?.ruleId === rule.id && editingCell.field === 'vendor_pattern'}
											<input
												class="cell-input"
												type="text"
												bind:value={editValue}
												onkeydown={onCellKeydown}
												onblur={saveEdit}
												autofocus
											/>
										{:else}
											<button
												class="cell-btn"
												title="Click to edit"
												onclick={() => startEdit(rule.id, 'vendor_pattern', rule.vendor_pattern)}
											>
												<span class="pattern-text">{rule.vendor_pattern}</span>
												<span class="source-badge source-{rule.source}">{rule.source}</span>
											</button>
										{/if}
									</td>

									<!-- Entity — inline editable -->
									<td class="td-entity">
										{#if editingCell?.ruleId === rule.id && editingCell.field === 'entity'}
											<select
												class="cell-select"
												bind:value={editValue}
												onkeydown={onCellKeydown}
												onblur={saveEdit}
												autofocus
											>
												{#each ENTITIES as e}
													<option value={e}>{entityLabel(e)}</option>
												{/each}
											</select>
										{:else}
											<button
												class="cell-btn"
												title="Click to edit"
												onclick={() => startEdit(rule.id, 'entity', rule.entity)}
											>
												<span class={entityBadgeClass(rule.entity)}>{entityLabel(rule.entity)}</span>
											</button>
										{/if}
									</td>

									<!-- Tax Category — inline editable -->
									<td class="td-category">
										{#if editingCell?.ruleId === rule.id && editingCell.field === 'tax_category'}
											<select
												class="cell-select"
												bind:value={editValue}
												onkeydown={onCellKeydown}
												onblur={saveEdit}
												autofocus
											>
												{#each TAX_CATEGORIES as c}
													<option value={c}>{fmtCategory(c)}</option>
												{/each}
											</select>
										{:else}
											<button
												class="cell-btn"
												title="Click to edit"
												onclick={() => startEdit(rule.id, 'tax_category', rule.tax_category)}
											>
												{fmtCategory(rule.tax_category)}
											</button>
										{/if}
									</td>

									<!-- Subcategory — read-only display -->
									<td class="td-sub">
										{#if rule.tax_subcategory}
											{fmtCategory(rule.tax_subcategory)}
										{:else}
											<span class="muted">—</span>
										{/if}
									</td>

									<!-- Confidence -->
									<td class="td-confidence">
										<span class="confidence-val" style="color: {confidenceColor(rule.confidence)}">
											{(rule.confidence * 100).toFixed(0)}%
										</span>
									</td>

									<!-- Match count -->
									<td class="td-matches">
										<span class="match-count">{rule.examples.toLocaleString()}</span>
									</td>

									<!-- Last match date -->
									<td class="td-date">{fmtDate(rule.last_matched)}</td>

									<!-- Actions -->
									<td class="td-actions">
										<button
											class="icon-btn delete-btn"
											title="Delete rule"
											onclick={() => confirmDelete(rule)}
										>✕</button>
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>

				<!-- Pagination footer -->
				{#if total > PAGE_SIZE}
					<div class="pagination">
						<span class="pagination-info">
							{offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total.toLocaleString()}
						</span>
						<div class="pagination-btns">
							<button class="btn btn-ghost btn-sm" onclick={prevPage} disabled={offset === 0}>
								Previous
							</button>
							<span class="page-indicator">Page {currentPage} of {totalPages}</span>
							<button class="btn btn-ghost btn-sm" onclick={nextPage} disabled={offset + PAGE_SIZE >= total}>
								Next
							</button>
						</div>
					</div>
				{/if}
			</div>
		{/if}
	</section>

	<!-- ── Entity Configuration ───────────────────────────────────────────── -->
	<section class="dashboard-section">
		<h2 class="section-title">Entity Configuration</h2>
		<div class="entity-grid">
			<div class="card entity-card entity-card-sparkry">
				<div class="entity-header">
					<span class="entity-pill entity-sparkry">Sparkry AI LLC</span>
				</div>
				<dl class="entity-meta">
					<dt>Tax Form</dt><dd>Schedule C</dd>
					<dt>B&amp;O Filing</dt><dd>Monthly</dd>
					<dt>Structure</dt><dd>Single-member LLC</dd>
				</dl>
			</div>
			<div class="card entity-card entity-card-blackline">
				<div class="entity-header">
					<span class="entity-pill entity-blackline">BlackLine MTB LLC</span>
				</div>
				<dl class="entity-meta">
					<dt>Tax Form</dt><dd>Form 1065 + K-1</dd>
					<dt>B&amp;O Filing</dt><dd>Quarterly</dd>
					<dt>Structure</dt><dd>Partnership (Travis 100%)</dd>
				</dl>
			</div>
			<div class="card entity-card entity-card-personal">
				<div class="entity-header">
					<span class="entity-pill entity-personal">Personal</span>
				</div>
				<dl class="entity-meta">
					<dt>Tax Form</dt><dd>1040 Schedule A, D</dd>
					<dt>B&amp;O Filing</dt><dd>N/A</dd>
					<dt>Structure</dt><dd>Individual</dd>
				</dl>
			</div>
		</div>
	</section>

	<p class="deadlines-link">
		<a href="/">View upcoming deadlines on Dashboard &rarr;</a>
	</p>
</div>

{#if undoToast}
	<Toast
		message={undoToast.message}
		type="success"
		undoLabel="Undo"
		duration={5000}
		onundo={handleUndoEdit}
		ondismiss={() => { undoToast = null; }}
	/>
{/if}

<!-- ── Delete Confirmation Dialog ───────────────────────────────────────── -->
{#if deleteTarget}
	<div class="dialog-overlay" role="dialog" aria-modal="true">
		<div class="dialog-card card">
			<h3 class="dialog-title">Delete vendor rule?</h3>
			<p class="dialog-body">
				Delete rule for <strong>{deleteTarget.vendor_pattern}</strong>?
				This cannot be undone.
			</p>
			{#if deleteError}
				<p class="form-error">{deleteError}</p>
			{/if}
			<div class="dialog-actions">
				<button class="btn btn-danger btn-sm" onclick={executeDelete} disabled={deleting}>
					{deleting ? 'Deleting…' : 'Delete'}
				</button>
				<button class="btn btn-ghost btn-sm" onclick={() => { deleteTarget = null; deleteError = ''; }}>
					Cancel
				</button>
			</div>
		</div>
	</div>
{/if}

<style>
	.page-shell {
		padding-top: 32px;
		padding-bottom: 64px;
	}

	.page-header {
		margin-bottom: 32px;
	}

	.page-subtitle {
		margin-top: 4px;
		color: var(--text-muted);
		font-size: 0.9rem;
	}

	/* ── Sections ──────────────────────────────────────────────────────────── */
	.dashboard-section {
		margin-bottom: 40px;
	}

	.section-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		margin-bottom: 14px;
	}

	.section-title {
		margin-bottom: 0;
		font-size: 1rem;
		font-weight: 600;
		color: var(--text);
	}

	/* ── Toolbar ───────────────────────────────────────────────────────────── */
	.toolbar {
		display: flex;
		gap: 10px;
		margin-bottom: 12px;
		flex-wrap: wrap;
	}

	.search-input {
		flex: 1;
		min-width: 200px;
		padding: 7px 12px;
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		font-size: 0.875rem;
		background: var(--surface);
		color: var(--text);
	}

	.filter-select {
		padding: 7px 10px;
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		font-size: 0.875rem;
		background: var(--surface);
		color: var(--text);
	}

	/* ── Add Form ──────────────────────────────────────────────────────────── */
	.add-form-card {
		padding: 20px 22px;
		margin-bottom: 12px;
	}

	.add-form-title {
		font-size: 0.875rem;
		font-weight: 600;
		margin-bottom: 14px;
	}

	.add-form-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
		gap: 12px;
		margin-bottom: 16px;
	}

	.form-field {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.form-label {
		font-size: 0.75rem;
		color: var(--text-muted);
		font-weight: 500;
	}

	.form-input,
	.form-select {
		padding: 6px 10px;
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		font-size: 0.875rem;
		background: var(--surface);
		color: var(--text);
	}

	.add-form-actions {
		display: flex;
		gap: 8px;
	}

	.form-error {
		font-size: 0.8rem;
		color: var(--red-600);
		margin-bottom: 10px;
	}

	/* ── Table ─────────────────────────────────────────────────────────────── */
	.table-card {
		overflow: hidden;
	}

	.table-wrap {
		overflow-x: auto;
	}

	.rule-row:hover {
		background: var(--gray-50);
	}

	/* Column widths */
	.td-pattern   { min-width: 220px; }
	.td-entity    { min-width: 120px; }
	.td-category  { min-width: 160px; }
	.td-sub       { min-width: 120px; color: var(--text-muted); font-size: 0.8rem; }
	.td-confidence { min-width: 70px; text-align: right; }
	.td-matches   { min-width: 70px; text-align: right; }
	.td-date      { min-width: 120px; color: var(--text-muted); font-size: 0.8rem; white-space: nowrap; }
	.td-actions   { min-width: 40px; text-align: right; }

	.muted { color: var(--text-muted); }

	/* Inline edit */
	.cell-btn {
		background: none;
		border: none;
		cursor: pointer;
		padding: 2px 4px;
		text-align: left;
		font-size: 0.875rem;
		color: var(--text);
		display: flex;
		align-items: center;
		gap: 6px;
		border-radius: var(--radius-sm);
		width: 100%;
	}

	.cell-btn:hover {
		background: var(--gray-100);
	}

	.cell-input,
	.cell-select {
		width: 100%;
		padding: 3px 7px;
		border: 1px solid var(--blue-500);
		border-radius: var(--radius-sm);
		font-size: 0.875rem;
		background: var(--surface);
		color: var(--text);
		outline: none;
		box-shadow: 0 0 0 2px rgba(59,130,246,.15);
	}

	.pattern-text {
		font-family: var(--font-mono);
		font-size: 0.8rem;
	}

	/* Source badge */
	.source-badge {
		font-size: 0.65rem;
		font-weight: 600;
		padding: 1px 5px;
		border-radius: 999px;
		flex-shrink: 0;
	}

	.source-human {
		background: var(--green-100);
		color: var(--green-700);
	}

	.source-learned {
		background: var(--amber-100);
		color: var(--amber-700);
	}

	/* Entity pill */
	.entity-pill {
		font-size: 0.75rem;
		font-weight: 600;
		padding: 2px 8px;
		border-radius: 999px;
	}

	.entity-sparkry  { background: #eff6ff; color: #1d4ed8; }
	.entity-blackline { background: #f0fdf4; color: #15803d; }
	.entity-personal  { background: #faf5ff; color: #7e22ce; }

	/* Confidence */
	.confidence-val {
		font-size: 0.85rem;
		font-weight: 600;
		font-variant-numeric: tabular-nums;
	}

	.match-count {
		font-variant-numeric: tabular-nums;
		font-size: 0.875rem;
	}

	/* Delete icon button */
	.icon-btn {
		background: none;
		border: none;
		cursor: pointer;
		padding: 4px 6px;
		border-radius: var(--radius-sm);
		font-size: 0.8rem;
		line-height: 1;
		color: var(--text-muted);
	}

	.delete-btn:hover {
		background: var(--red-100);
		color: var(--red-600);
	}

	/* ── Skeleton ──────────────────────────────────────────────────────────── */
	.skeleton-rows {
		display: flex;
		flex-direction: column;
		gap: 0;
		padding: 0;
	}

	.skeleton-row {
		display: flex;
		gap: 16px;
		padding: 13px 20px;
		border-bottom: 1px solid var(--border);
	}

	.skeleton {
		background: var(--gray-100);
		border-radius: 4px;
		animation: pulse 1.4s ease-in-out infinite;
	}

	@keyframes pulse {
		0%, 100% { opacity: 1; }
		50% { opacity: .5; }
	}

	/* ── Pagination ────────────────────────────────────────────────────────── */
	.pagination {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 12px 20px;
		border-top: 1px solid var(--border);
		font-size: 0.8rem;
		color: var(--text-muted);
	}

	.pagination-btns {
		display: flex;
		align-items: center;
		gap: 10px;
	}

	.page-indicator {
		font-size: 0.8rem;
		color: var(--text-muted);
	}

	/* ── Error / empty ─────────────────────────────────────────────────────── */
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

	.empty-card {
		padding: 32px 24px;
	}

	.empty-msg {
		color: var(--text-muted);
		font-size: 0.875rem;
	}

	/* ── Entity config ─────────────────────────────────────────────────────── */
	.entity-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
		gap: 12px;
	}

	.entity-card {
		padding: 18px 20px;
	}

	.entity-card-sparkry  { border-top: 4px solid #1e40af; }
	.entity-card-blackline { border-top: 4px solid #7c3aed; }
	.entity-card-personal  { border-top: 4px solid #475569; }

	.entity-header {
		margin-bottom: 14px;
	}

	.entity-meta {
		display: grid;
		grid-template-columns: auto 1fr;
		gap: 4px 16px;
		font-size: 0.8rem;
	}

	.entity-meta dt {
		color: var(--text-muted);
		font-weight: 500;
	}

	.entity-meta dd {
		color: var(--text);
	}

	/* ── Deadlines link ───────────────────────────────────────────────────── */
	.deadlines-link {
		margin-top: 8px;
		font-size: 0.85rem;
	}

	.deadlines-link a {
		color: var(--text-muted);
		text-decoration: none;
	}

	.deadlines-link a:hover {
		color: var(--text);
		text-decoration: underline;
	}

	/* ── Buttons ───────────────────────────────────────────────────────────── */
	.btn-sm {
		padding: 5px 14px;
		font-size: 0.8rem;
	}

	.btn-danger {
		background: var(--red-600);
		color: #fff;
		border: none;
		border-radius: var(--radius-sm);
		cursor: pointer;
		font-size: 0.875rem;
		padding: 7px 18px;
	}

	.btn-danger:hover:not(:disabled) {
		background: var(--red-700);
	}

	/* ── Dialog ────────────────────────────────────────────────────────────── */
	.dialog-overlay {
		position: fixed;
		inset: 0;
		background: rgba(0, 0, 0, 0.35);
		display: flex;
		align-items: center;
		justify-content: center;
		z-index: 100;
	}

	.dialog-card {
		padding: 28px 32px;
		max-width: 400px;
		width: 90%;
	}

	.dialog-title {
		margin-bottom: 10px;
		font-size: 1rem;
		font-weight: 600;
	}

	.dialog-body {
		font-size: 0.875rem;
		color: var(--text-muted);
		margin-bottom: 20px;
	}

	.dialog-body strong {
		color: var(--text);
		font-family: var(--font-mono);
		font-size: 0.8rem;
	}

	.dialog-actions {
		display: flex;
		gap: 8px;
	}
</style>
