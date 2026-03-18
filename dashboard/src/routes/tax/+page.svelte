<script lang="ts">
	import { onMount } from 'svelte';
	import { fetchTaxSummary, downloadExport } from '$lib/api';
	import type { TaxSummary, TaxLineItem } from '$lib/api';

	// ── Constants ─────────────────────────────────────────────────────────────
	const ENTITIES = [
		{ value: 'sparkry',  label: 'Sparkry AI' },
		{ value: 'blackline', label: 'BlackLine MTB' },
		{ value: 'personal', label: 'Personal' }
	] as const;

	const CURRENT_YEAR = new Date().getFullYear();
	const YEARS: number[] = [];
	for (let y = 2024; y <= CURRENT_YEAR + 1; y++) YEARS.push(y);

	const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
	const QUARTERS = ['Q1','Q2','Q3','Q4'];

	// ── Category display labels ───────────────────────────────────────────────
	const CATEGORY_LABELS: Record<string, string> = {
		ADVERTISING:            'Advertising',
		CAR_AND_TRUCK:          'Car & Truck',
		CONTRACT_LABOR:         'Contract Labor',
		INSURANCE:              'Insurance',
		LEGAL_AND_PROFESSIONAL: 'Legal & Professional',
		OFFICE_EXPENSE:         'Office Expense',
		SUPPLIES:               'Supplies',
		TAXES_AND_LICENSES:     'Taxes & Licenses',
		TRAVEL:                 'Travel',
		MEALS:                  'Meals',
		COGS:                   'Cost of Goods Sold',
		CONSULTING_INCOME:      'Consulting Income',
		SUBSCRIPTION_INCOME:    'Subscription Income',
		SALES_INCOME:           'Sales Income',
		REIMBURSABLE:           'Reimbursable',
		CHARITABLE_CASH:        'Charitable (Cash)',
		CHARITABLE_STOCK:       'Charitable (Non-Cash)',
		MEDICAL:                'Medical',
		STATE_LOCAL_TAX:        'State & Local Tax',
		MORTGAGE_INTEREST:      'Mortgage Interest',
		INVESTMENT_INCOME:      'Investment Income',
		PERSONAL_NON_DEDUCTIBLE:'Personal Non-Deductible',
		CAPITAL_CONTRIBUTION:   'Capital Contribution',
		OTHER_EXPENSE:          'Other Expense'
	};

	// ── State ─────────────────────────────────────────────────────────────────
	let selectedEntity = $state<'sparkry' | 'blackline' | 'personal'>('sparkry');
	let selectedYear   = $state(CURRENT_YEAR);
	let summary        = $state<TaxSummary | null>(null);
	let loading        = $state(false);
	let fetchError     = $state('');
	let downloading    = $state<string | null>(null);
	let downloadError  = $state('');

	// ── Derived ───────────────────────────────────────────────────────────────
	let incomeItems = $derived.by(() =>
		(summary?.line_items ?? []).filter((li: TaxLineItem) => li.is_income)
	);

	let expenseItems = $derived.by(() =>
		(summary?.line_items ?? []).filter((li: TaxLineItem) => !li.is_income && !li.is_reimbursable)
	);

	let readinessPct = $derived(summary?.readiness.readiness_pct ?? 0);

	let unconfirmedCount = $derived(summary?.readiness.unconfirmed_count ?? 0);

	let totalCount = $derived(summary?.readiness.total_count ?? 0);

	let needsReviewCount = $derived(summary?.readiness.needs_review_count ?? 0);
	let autoClassifiedCount = $derived(summary?.readiness.auto_classified_count ?? 0);

	let reviewLink = $derived(
		`/?entity=${selectedEntity}&dateFrom=${selectedYear}-01-01&dateTo=${selectedYear}-12-31`
	);

	let registerLink = $derived(
		`/register?entity=${selectedEntity}&status=auto_classified&dateFrom=${selectedYear}-01-01&dateTo=${selectedYear}-12-31`
	);

	// Month-by-month income for Sparkry B&O (from API bno_monthly array)
	let monthlyIncome = $derived.by((): number[] => {
		const data = (summary as any)?.bno_monthly as Array<{month: string; income: number}> | undefined;
		if (data && data.length === 12) {
			return data.map((d: {month: string; income: number}) => d.income);
		}
		return Array(12).fill(0);
	});

	// Quarter-by-quarter income for BlackLine B&O (from API bno_quarterly array)
	let quarterlyIncome = $derived.by((): number[] => {
		const data = (summary as any)?.bno_quarterly as Array<{quarter: string; income: number}> | undefined;
		if (data && data.length === 4) {
			return data.map((d: {quarter: string; income: number}) => d.income);
		}
		return Array(4).fill(0);
	});

	let showBno = $derived(selectedEntity !== 'personal');
	let showTaxact = $derived(selectedEntity === 'blackline');

	// ── Helpers ───────────────────────────────────────────────────────────────
	function fmtCurrency(amount: number): string {
		return amount.toLocaleString('en-US', { style: 'currency', currency: 'USD' });
	}

	function categoryLabel(cat: string): string {
		return CATEGORY_LABELS[cat] ?? cat.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
	}

	function readinessColor(pct: number): string {
		if (pct >= 90) return 'var(--green-600)';
		if (pct >= 70) return 'var(--amber-600)';
		return 'var(--red-600)';
	}

	function readinessBg(pct: number): string {
		if (pct >= 90) return 'var(--green-500)';
		if (pct >= 70) return 'var(--amber-500)';
		return 'var(--red-500)';
	}

	function exportFilename(type: 'freetaxusa' | 'taxact' | 'bno'): string {
		const entitySlug = selectedEntity;
		const ext = type === 'bno' ? 'csv' : 'txt';
		return `${entitySlug}-${selectedYear}-${type}.${ext}`;
	}

	// ── Load ─────────────────────────────────────────────────────────────────
	async function load() {
		loading = true;
		fetchError = '';
		summary = null;
		try {
			summary = await fetchTaxSummary(selectedEntity, selectedYear);
		} catch (e) {
			fetchError = e instanceof Error ? e.message : 'Failed to load tax summary';
		} finally {
			loading = false;
		}
	}

	async function triggerDownload(type: 'freetaxusa' | 'taxact' | 'bno') {
		downloading = type;
		downloadError = '';
		try {
			await downloadExport(type, selectedEntity, selectedYear, exportFilename(type));
		} catch (e) {
			downloadError = e instanceof Error ? e.message : 'Download failed';
		} finally {
			downloading = null;
		}
	}

	onMount(() => { load(); });
</script>

<!-- ── Page ──────────────────────────────────────────────────────────────────── -->
<div class="container page-shell">

	<!-- ── Header ────────────────────────────────────────────────────────────── -->
	<header class="page-header">
		<div>
			<h1>Tax Summary</h1>
			<p class="page-subtitle">IRS line-item breakdown, B&amp;O totals, and export</p>
		</div>
		<div class="page-header-actions no-print">
			<select
				class="year-select"
				bind:value={selectedYear}
				onchange={load}
				aria-label="Tax year"
			>
				{#each YEARS as y (y)}
					<option value={y}>{y}</option>
				{/each}
			</select>
		</div>
	</header>

	<!-- ── Entity tabs ───────────────────────────────────────────────────────── -->
	<div class="entity-tabs no-print" role="tablist" aria-label="Entity">
		{#each ENTITIES as ent (ent.value)}
			<button
				role="tab"
				aria-selected={selectedEntity === ent.value}
				class="entity-tab"
				class:active={selectedEntity === ent.value}
				onclick={() => { selectedEntity = ent.value; load(); }}
			>
				{ent.label}
			</button>
		{/each}
	</div>

	<!-- ── Download error banner ─────────────────────────────────────────────── -->
	{#if downloadError}
		<div class="alert-banner alert-error no-print">
			<span>{downloadError}</span>
			<button class="dismiss-btn" onclick={() => (downloadError = '')}>✕</button>
		</div>
	{/if}

	<!-- ── Loading skeleton ──────────────────────────────────────────────────── -->
	{#if loading}
		<div class="skeleton-section">
			<div class="card skeleton-card">
				<div class="skeleton" style="height: 14px; width: 30%; margin-bottom: 12px;"></div>
				<div class="skeleton" style="height: 10px; width: 100%; margin-bottom: 8px;"></div>
				<div class="skeleton" style="height: 10px; width: 80%;"></div>
			</div>
			<div class="card skeleton-card">
				<div class="skeleton" style="height: 14px; width: 40%; margin-bottom: 12px;"></div>
				{#each Array(5) as _}
					<div class="skeleton" style="height: 10px; width: 100%; margin-bottom: 8px;"></div>
				{/each}
			</div>
		</div>

	<!-- ── Error state ───────────────────────────────────────────────────────── -->
	{:else if fetchError}
		<div class="card error-card">
			<p class="error-msg">{fetchError}</p>
			<button class="btn btn-ghost no-print" onclick={load}>Try again</button>
		</div>

	<!-- ── Empty state ───────────────────────────────────────────────────────── -->
	{:else if summary && totalCount === 0}
		<div class="card">
			<div class="empty-state">
				<span class="icon">📂</span>
				<p>No transactions found for {selectedEntity} in {selectedYear}.</p>
				<p class="empty-hint">Import data or select a different entity or year.</p>
			</div>
		</div>

	<!-- ── Main content ──────────────────────────────────────────────────────── -->
	{:else if summary}

		<!-- ── Readiness ──────────────────────────────────────────────────────── -->
		<section class="dashboard-section">
			<h2 class="section-title">Tax Readiness</h2>
			<div class="card readiness-card">
				<div class="readiness-header">
					<div class="readiness-pct-group">
						<span class="readiness-pct" style="color: {readinessColor(readinessPct)}">
							{readinessPct}%
						</span>
						<span class="readiness-label">ready</span>
					</div>
					<span class="readiness-detail">
						{summary.readiness.confirmed_count} of {totalCount} transactions confirmed
					</span>
				</div>

				<div class="progress-bar-container" role="progressbar" aria-valuenow={readinessPct} aria-valuemin={0} aria-valuemax={100}>
					<div
						class="progress-fill"
						style="width: {readinessPct}%; background: {readinessBg(readinessPct)}"
					></div>
				</div>

				{#if needsReviewCount > 0}
					<a class="readiness-cta no-print" href={reviewLink}>
						{needsReviewCount} {needsReviewCount === 1 ? 'item needs' : 'items need'} review →
					</a>
				{:else if autoClassifiedCount > 0}
					<a class="readiness-cta readiness-cta-auto no-print" href={registerLink}>
						{autoClassifiedCount} auto-classified {autoClassifiedCount === 1 ? 'item' : 'items'} (confirm in Register)
					</a>
				{:else}
					<p class="readiness-all-clear">All confirmed</p>
				{/if}
			</div>
		</section>

		<!-- ── Warnings ───────────────────────────────────────────────────────── -->
		{#if summary.warnings.length > 0}
			{#each summary.warnings as w (w.warning)}
				<div class="alert-banner alert-warning">
					{w.warning}
				</div>
			{/each}
		{/if}

		<!-- ── IRS Line-Item Breakdown ────────────────────────────────────────── -->
		<section class="dashboard-section">
			<h2 class="section-title">IRS Line-Item Breakdown</h2>
			<div class="card table-card">
				{#if incomeItems.length === 0 && expenseItems.length === 0}
					<p class="no-data table-empty">No classified transactions for {selectedYear}.</p>
				{:else}
					<table class="data-table">
						<thead>
							<tr>
								<th>Category</th>
								<th class="th-right">Amount</th>
								<th>IRS Line</th>
							</tr>
						</thead>
						<tbody>
							<!-- Income section -->
							{#if incomeItems.length > 0}
								<tr class="section-header-row">
									<td colspan="3" class="section-label">Income</td>
								</tr>
								{#each incomeItems as item (item.tax_category)}
									<tr>
										<td class="td-category">{categoryLabel(item.tax_category)}</td>
										<td class="td-amount amount-positive">{fmtCurrency(item.total)}</td>
										<td class="td-irs-line">{item.irs_line}</td>
									</tr>
								{/each}
								<tr class="subtotal-row">
									<td class="subtotal-label">Gross Income</td>
									<td class="td-amount amount-positive subtotal-val">{fmtCurrency(summary.gross_income)}</td>
									<td></td>
								</tr>
							{/if}

							<!-- Expenses section -->
							{#if expenseItems.length > 0}
								<tr class="section-header-row">
									<td colspan="3" class="section-label">Expenses</td>
								</tr>
								{#each expenseItems as item (item.tax_category)}
									<tr>
										<td class="td-category">{categoryLabel(item.tax_category)}</td>
										<td class="td-amount amount-negative">({fmtCurrency(item.total)})</td>
										<td class="td-irs-line">{item.irs_line}</td>
									</tr>
								{/each}
								<tr class="subtotal-row">
									<td class="subtotal-label">Total Expenses</td>
									<td class="td-amount amount-negative subtotal-val">({fmtCurrency(summary.total_expenses)})</td>
									<td></td>
								</tr>
							{/if}
						</tbody>

						<!-- Net profit footer -->
						<tfoot>
							<tr class="net-profit-row" class:profit-positive={summary.net_profit >= 0} class:profit-negative={summary.net_profit < 0}>
								<td class="net-profit-label">
									{selectedEntity === 'personal' ? 'Net Deductions' : 'Net Profit'}
								</td>
								<td class="td-amount net-profit-amount">
									{#if summary.net_profit < 0}
										({fmtCurrency(Math.abs(summary.net_profit))})
									{:else}
										{fmtCurrency(summary.net_profit)}
									{/if}
								</td>
								<td class="td-irs-line net-profit-line">
									{selectedEntity === 'personal' ? 'Schedule A' : 'Schedule C Line 31'}
								</td>
							</tr>
						</tfoot>
					</table>
				{/if}
			</div>
		</section>

		<!-- ── B&O Subtotals ──────────────────────────────────────────────────── -->
		{#if showBno}
			<section class="dashboard-section">
				<h2 class="section-title">
					B&amp;O Revenue Subtotals
					<span class="section-title-note">
						({selectedEntity === 'sparkry' ? 'Monthly' : 'Quarterly'} — download for full detail)
					</span>
				</h2>
				<div class="card table-card">
					<table class="data-table bno-table">
						<thead>
							<tr>
								<th>{selectedEntity === 'sparkry' ? 'Month' : 'Quarter'}</th>
								<th class="th-right">Income</th>
							</tr>
						</thead>
						<tbody>
							{#if selectedEntity === 'sparkry'}
								{#each MONTHS as month, i (month)}
									<tr>
										<td class="td-period">{month} {selectedYear}</td>
										<td class="td-amount">
											{#if monthlyIncome[i] > 0}
												<span class="amount-positive">{fmtCurrency(monthlyIncome[i])}</span>
											{:else}
												<span class="no-data">—</span>
											{/if}
										</td>
									</tr>
								{/each}
							{:else}
								{#each QUARTERS as quarter, i (quarter)}
									<tr>
										<td class="td-period">{quarter} {selectedYear}</td>
										<td class="td-amount">
											{#if quarterlyIncome[i] > 0}
												<span class="amount-positive">{fmtCurrency(quarterlyIncome[i])}</span>
											{:else}
												<span class="no-data">—</span>
											{/if}
										</td>
									</tr>
								{/each}
							{/if}
						</tbody>
						<tfoot>
							<tr>
								<td>Total</td>
								<td class="td-amount amount-positive">{fmtCurrency(summary.gross_income)}</td>
							</tr>
						</tfoot>
					</table>
					<p class="bno-note no-print">
						Download the B&amp;O report for per-period breakdown with all classifications.
					</p>
				</div>
			</section>
		{/if}

		<!-- ── Export buttons ─────────────────────────────────────────────────── -->
		<section class="dashboard-section no-print">
			<h2 class="section-title">Export</h2>
			<div class="export-actions">
				<!-- FreeTaxUSA -->
				<div class="export-card card">
					<div class="export-info">
						<p class="export-name">FreeTaxUSA</p>
						<p class="export-desc">
							{selectedEntity === 'personal' ? 'Schedule A / D summary' : 'Schedule C summary'} (.txt)
						</p>
					</div>
					<button
						class="btn btn-primary"
						onclick={() => triggerDownload('freetaxusa')}
						disabled={downloading !== null}
					>
						{#if downloading === 'freetaxusa'}
							<span class="spinner" aria-hidden="true"></span>
							Downloading…
						{:else}
							Download FreeTaxUSA
						{/if}
					</button>
				</div>

				<!-- TaxAct — BlackLine only -->
				{#if showTaxact}
					<div class="export-card card">
						<div class="export-info">
							<p class="export-name">TaxAct</p>
							<p class="export-desc">Form 1065 / K-1 summary (.txt)</p>
						</div>
						<button
							class="btn btn-primary"
							onclick={() => triggerDownload('taxact')}
							disabled={downloading !== null}
						>
							{#if downloading === 'taxact'}
								<span class="spinner" aria-hidden="true"></span>
								Downloading…
							{:else}
								Download TaxAct
							{/if}
						</button>
					</div>
				{/if}

				<!-- B&O — Sparkry & BlackLine only -->
				{#if showBno}
					<div class="export-card card">
						<div class="export-info">
							<p class="export-name">WA B&amp;O Report</p>
							<p class="export-desc">
								{selectedEntity === 'sparkry' ? 'Monthly' : 'Quarterly'} revenue CSV (.csv)
							</p>
						</div>
						<button
							class="btn btn-ghost"
							onclick={() => triggerDownload('bno')}
							disabled={downloading !== null}
						>
							{#if downloading === 'bno'}
								<span class="spinner" aria-hidden="true"></span>
								Downloading…
							{:else}
								Download B&amp;O Report
							{/if}
						</button>
					</div>
				{/if}
			</div>
		</section>

	{/if}

</div>

<style>
	/* ── Page layout ─────────────────────────────────────────────────────────── */
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
		font-size: 0.9rem;
	}

	.page-header-actions {
		display: flex;
		align-items: center;
		gap: 12px;
		flex-shrink: 0;
	}

	.year-select {
		min-width: 90px;
	}

	/* ── Entity tabs ─────────────────────────────────────────────────────────── */
	.entity-tabs {
		display: flex;
		gap: 4px;
		margin-bottom: 28px;
		border-bottom: 1px solid var(--border);
		padding-bottom: 0;
	}

	.entity-tab {
		padding: 8px 20px;
		background: none;
		border: none;
		border-bottom: 2px solid transparent;
		margin-bottom: -1px;
		font-family: var(--font);
		font-size: 0.875rem;
		font-weight: 500;
		color: var(--text-muted);
		cursor: pointer;
		transition: color 0.12s, border-color 0.12s;
		white-space: nowrap;
	}

	.entity-tab:hover {
		color: var(--text);
	}

	.entity-tab.active {
		color: var(--text);
		border-bottom-color: var(--gray-900);
	}

	/* ── Alert banners ───────────────────────────────────────────────────────── */
	.alert-banner {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		padding: 10px 16px;
		border-radius: var(--radius-sm);
		font-size: 0.875rem;
		margin-bottom: 16px;
	}

	.alert-error {
		background: var(--red-100);
		border: 1px solid var(--red-500);
		color: var(--red-700);
	}

	.alert-warning {
		background: var(--amber-100);
		border: 1px solid var(--amber-500);
		color: var(--amber-700);
	}

	.dismiss-btn {
		background: none;
		border: none;
		cursor: pointer;
		color: inherit;
		font-size: 0.9rem;
		padding: 0 4px;
		flex-shrink: 0;
	}

	/* ── Dashboard sections ──────────────────────────────────────────────────── */
	.dashboard-section {
		margin-bottom: 36px;
	}

	.section-title {
		margin-bottom: 14px;
		font-size: 1rem;
		font-weight: 600;
		color: var(--text);
		display: flex;
		align-items: baseline;
		gap: 8px;
	}

	.section-title-note {
		font-size: 0.8rem;
		font-weight: 400;
		color: var(--text-muted);
	}

	/* ── Skeleton ────────────────────────────────────────────────────────────── */
	.skeleton-section {
		display: flex;
		flex-direction: column;
		gap: 16px;
	}

	.skeleton-card {
		padding: 24px;
	}

	/* ── Error card ──────────────────────────────────────────────────────────── */
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

	/* ── Empty state additions ───────────────────────────────────────────────── */
	.empty-hint {
		font-size: 0.8rem;
		color: var(--text-muted);
	}

	/* ── Readiness card ──────────────────────────────────────────────────────── */
	.readiness-card {
		padding: 24px 28px;
		display: flex;
		flex-direction: column;
		gap: 14px;
	}

	.readiness-header {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 16px;
		flex-wrap: wrap;
	}

	.readiness-pct-group {
		display: flex;
		align-items: baseline;
		gap: 6px;
	}

	.readiness-pct {
		font-size: 2rem;
		font-weight: 700;
		font-variant-numeric: tabular-nums;
		line-height: 1;
	}

	.readiness-label {
		font-size: 1rem;
		color: var(--text-muted);
	}

	.readiness-detail {
		font-size: 0.875rem;
		color: var(--text-muted);
	}

	.progress-bar-container {
		height: 10px;
		border-radius: 999px;
		background: var(--gray-100);
		overflow: hidden;
	}

	.progress-fill {
		height: 100%;
		border-radius: 999px;
		transition: width 0.5s ease;
	}

	.readiness-cta {
		font-size: 0.875rem;
		font-weight: 500;
		color: var(--blue-600);
		text-decoration: none;
		align-self: flex-start;
	}

	.readiness-cta:hover {
		text-decoration: underline;
	}

	.readiness-cta-auto {
		color: var(--amber-600);
	}

	.readiness-all-clear {
		font-size: 0.875rem;
		color: var(--green-600);
	}

	/* ── Table card ──────────────────────────────────────────────────────────── */
	.table-card {
		overflow-x: auto;
	}

	.table-empty {
		padding: 32px 24px;
	}

	/* ── Table overrides for tax page ────────────────────────────────────────── */
	.th-right {
		text-align: right;
	}

	.td-category {
		font-weight: 500;
	}

	.td-amount {
		text-align: right;
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
	}

	.td-irs-line {
		color: var(--text-muted);
		font-size: 0.8rem;
		white-space: nowrap;
	}

	.td-period {
		font-weight: 500;
	}

	/* Section header rows (Income / Expenses labels within the table) */
	.section-header-row td {
		padding: 6px 14px 4px;
		background: var(--gray-50);
	}

	.section-label {
		font-size: 0.7rem;
		font-weight: 700;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--text-muted);
	}

	/* Subtotal rows (inside tbody) */
	.subtotal-row td {
		padding: 8px 14px;
		border-top: 1px solid var(--border);
		background: var(--gray-50);
	}

	.subtotal-label {
		font-weight: 600;
		font-size: 0.875rem;
	}

	.subtotal-val {
		font-weight: 600;
	}

	/* Net profit footer */
	.net-profit-row td {
		padding: 12px 14px;
	}

	.net-profit-label {
		font-size: 1rem;
		font-weight: 700;
	}

	.net-profit-amount {
		font-size: 1.1rem;
		font-weight: 700;
	}

	.net-profit-line {
		font-weight: 600;
		color: var(--text-muted);
	}

	.profit-positive .net-profit-amount {
		color: var(--green-600);
	}

	.profit-negative .net-profit-amount {
		color: var(--red-600);
	}

	/* B&O table */
	.bno-table {
		max-width: 480px;
	}

	.bno-note {
		padding: 10px 14px;
		font-size: 0.8rem;
		color: var(--text-muted);
		border-top: 1px solid var(--border);
	}

	.no-data {
		color: var(--text-muted);
		font-size: 0.875rem;
	}

	/* ── Export section ──────────────────────────────────────────────────────── */
	.export-actions {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.export-card {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 16px;
		padding: 16px 20px;
		flex-wrap: wrap;
	}

	.export-info {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.export-name {
		font-weight: 600;
		font-size: 0.9rem;
	}

	.export-desc {
		font-size: 0.8rem;
		color: var(--text-muted);
	}

	/* ── Spinner ─────────────────────────────────────────────────────────────── */
	.spinner {
		display: inline-block;
		width: 12px;
		height: 12px;
		border: 2px solid var(--gray-300);
		border-top-color: currentColor;
		border-radius: 50%;
		animation: spin 0.6s linear infinite;
	}

	@keyframes spin {
		to { transform: rotate(360deg); }
	}

	/* ── Print ───────────────────────────────────────────────────────────────── */
	@media print {
		.no-print {
			display: none !important;
		}

		.page-shell {
			padding-top: 0;
			padding-bottom: 0;
		}

		.card {
			border: 1px solid #ccc;
			box-shadow: none;
		}

		.dashboard-section {
			margin-bottom: 20px;
		}

		.readiness-card {
			padding: 12px 16px;
		}

		.table-card {
			overflow: visible;
		}

		.data-table {
			font-size: 0.8rem;
		}

		h1 { font-size: 1.25rem; }
		h2 { font-size: 0.95rem; }
	}
</style>
