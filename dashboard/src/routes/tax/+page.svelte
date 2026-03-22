<script lang="ts">
	import { onMount } from 'svelte';
	import { fetchTaxSummary, downloadExport } from '$lib/api';
	import type { TaxSummary, TaxLineItem, TaxYoyDelta, TaxTip, EstimatedTax } from '$lib/api';
	import { CATEGORY_LABELS, formatAmount, amountClass } from '$lib/categories';

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


	// ── State ─────────────────────────────────────────────────────────────────
	let selectedEntity   = $state<'sparkry' | 'blackline' | 'personal'>('sparkry');
	let selectedYear     = $state(CURRENT_YEAR);
	let summary          = $state<TaxSummary | null>(null);
	let loading          = $state(false);
	let fetchError       = $state('');
	let downloading      = $state<string | null>(null);
	let downloadError    = $state('');
	let compareEnabled   = $state(false);
	let showDismissed    = $state(false);
	let insightsOpen     = $state(false);
	let bnoExpanded      = $state(false);

	// ── Derived ───────────────────────────────────────────────────────────────
	let compareYear = $derived(selectedYear - 1);

	let comparison = $derived(summary?.comparison ?? null);

	let deltasByCat = $derived.by((): Map<string, TaxYoyDelta> => {
		const m = new Map<string, TaxYoyDelta>();
		if (comparison) {
			for (const d of comparison.deltas) {
				m.set(d.tax_category, d);
			}
		}
		return m;
	});

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
		`/review?entity=${selectedEntity}&dateFrom=${selectedYear}-01-01&dateTo=${selectedYear}-12-31`
	);

	let registerLink = $derived(
		`/register?entity=${selectedEntity}&status=auto_classified&dateFrom=${selectedYear}-01-01&dateTo=${selectedYear}-12-31`
	);

	// ── Tax tips (dismissible) ────────────────────────────────────────────────
	function tipStorageKey(tipId: string): string {
		return `tip-${tipId}-${selectedYear}`;
	}

	function isTipDismissed(tipId: string): boolean {
		if (typeof localStorage === 'undefined') return false;
		return localStorage.getItem(tipStorageKey(tipId)) === 'dismissed';
	}

	function dismissTip(tipId: string): void {
		if (typeof localStorage !== 'undefined') {
			localStorage.setItem(tipStorageKey(tipId), 'dismissed');
		}
		// Force reactivity by reassigning summary (shallow trigger)
		summary = summary;
	}

	let allTips = $derived<TaxTip[]>(summary?.tax_tips ?? []);

	let visibleTips = $derived.by((): TaxTip[] => {
		return allTips.filter(t => !isTipDismissed(t.id));
	});

	let dismissedTips = $derived.by((): TaxTip[] => {
		return allTips.filter(t => isTipDismissed(t.id));
	});

	// Month-by-month income for Sparkry B&O (from API bno_monthly array)
	let monthlyIncome = $derived.by((): number[] => {
		const data = (summary as any)?.bno_monthly as Array<{month: string; income: number}> | undefined;
		if (data && data.length === 12) {
			return data.map((d: {month: string; income: number}) => d.income);
		}
		return Array(12).fill(0);
	});

	// Prior-year month-by-month income for B&O comparison
	let priorMonthlyIncome = $derived.by((): number[] => {
		if (!comparison) return Array(12).fill(0);
		const deltas = comparison.bno_monthly_deltas;
		if (!deltas || deltas.length === 0) return Array(12).fill(0);
		// Map by month string (YYYY-MM) → prior value
		const out = Array(12).fill(0);
		for (const d of deltas) {
			const month = parseInt(d.month.split('-')[1] ?? '0', 10) - 1;
			if (month >= 0 && month < 12) out[month] = d.prior;
		}
		return out;
	});

	// Quarter-by-quarter income for BlackLine B&O (from API bno_quarterly array)
	let quarterlyIncome = $derived.by((): number[] => {
		const data = (summary as any)?.bno_quarterly as Array<{quarter: string; income: number}> | undefined;
		if (data && data.length === 4) {
			return data.map((d: {quarter: string; income: number}) => d.income);
		}
		return Array(4).fill(0);
	});

	// Prior-year quarter-by-quarter income for B&O comparison
	let priorQuarterlyIncome = $derived.by((): number[] => {
		if (!comparison) return Array(4).fill(0);
		const deltas = comparison.bno_quarterly_deltas;
		if (!deltas || deltas.length === 0) return Array(4).fill(0);
		// Map by quarter string (Q1..Q4) → prior value
		const out = Array(4).fill(0);
		for (const d of deltas) {
			const q = parseInt(d.quarter.replace('Q', ''), 10) - 1;
			if (q >= 0 && q < 4) out[q] = d.prior;
		}
		return out;
	});

	let estimatedTax = $derived<EstimatedTax | null>(summary?.estimated_tax ?? null);

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

	function estStateBadge(state: string): { cls: string; label: string } {
		if (state === 'paid') return { cls: 'est-paid', label: 'Paid' };
		if (state === 'overdue') return { cls: 'est-overdue', label: 'Overdue' };
		return { cls: 'est-upcoming', label: 'Upcoming' };
	}

	function exportFilename(type: 'freetaxusa' | 'taxact' | 'bno'): string {
		const entitySlug = selectedEntity;
		const ext = type === 'bno' ? 'csv' : 'txt';
		return `${entitySlug}-${selectedYear}-${type}.${ext}`;
	}

	/** Format a delta amount with sign: +$1,234 or −$1,234 */
	function fmtDelta(delta: number): string {
		if (delta === 0) return '—';
		const abs = fmtCurrency(Math.abs(delta));
		return delta > 0 ? `+${abs}` : `−${abs}`;
	}

	/** Format a delta percentage with sign: +12.3% or −4.5% */
	function fmtDeltaPct(pct: number | null): string {
		if (pct === null) return '';
		if (pct === 0) return '';
		const sign = pct > 0 ? '+' : '−';
		return `${sign}${Math.abs(pct).toFixed(1)}%`;
	}

	/**
	 * CSS class for a line-item delta.
	 * For income: positive delta = good (green). For expenses: negative delta = good (green).
	 */
	function deltaClass(delta: number, isIncome: boolean): string {
		if (delta === 0) return 'delta-neutral';
		if (isIncome) return delta > 0 ? 'delta-positive' : 'delta-negative';
		// expenses: less spending is good
		return delta < 0 ? 'delta-positive' : 'delta-negative';
	}

	/** CSS class for net profit delta (higher net profit = good). */
	function netProfitDeltaClass(delta: number): string {
		if (delta === 0) return 'delta-neutral';
		return delta > 0 ? 'delta-positive' : 'delta-negative';
	}

	// ── Load ─────────────────────────────────────────────────────────────────
	async function load() {
		loading = true;
		fetchError = '';
		summary = null;
		try {
			const cy = compareEnabled ? compareYear : undefined;
			summary = await fetchTaxSummary(selectedEntity, selectedYear, cy);
		} catch (e) {
			fetchError = e instanceof Error ? e.message : 'Failed to load tax summary';
		} finally {
			loading = false;
		}
	}

	function toggleCompare() {
		compareEnabled = !compareEnabled;
		load();
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
			<button
				class="compare-toggle"
				class:compare-toggle-active={compareEnabled}
				onclick={toggleCompare}
				aria-pressed={compareEnabled}
				title={compareEnabled ? `Hide ${compareYear} comparison` : `Compare with ${compareYear}`}
			>
				{compareEnabled ? `Comparing ${compareYear}` : `vs ${compareYear}`}
			</button>
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

		<!-- ── Tax Optimization Insights (collapsible) ────────────────────────── -->
		{#if allTips.length > 0}
			<section class="dashboard-section no-print">
				<button
					class="insights-toggle"
					onclick={() => (insightsOpen = !insightsOpen)}
					aria-expanded={insightsOpen}
				>
					<span class="insights-toggle-text">
						{allTips.length} tax optimization {allTips.length === 1 ? 'tip' : 'tips'}
					</span>
					<span class="chevron" class:chevron-open={insightsOpen} aria-hidden="true">&#x25B8;</span>
				</button>

				{#if insightsOpen}
					{#if visibleTips.length > 0}
						<div class="tips-list" style="margin-top: 12px;">
							{#each visibleTips as tip (tip.id)}
								<div class="tip-card">
									<div class="tip-icon" aria-hidden="true">ℹ</div>
									<div class="tip-body">
										<p class="tip-title">{tip.title}</p>
										<p class="tip-detail">{tip.detail}</p>
										{#if tip.action_url}
											<a class="tip-action" href={tip.action_url}>Review →</a>
										{/if}
									</div>
									{#if tip.dismissible}
										<button
											class="tip-dismiss"
											onclick={() => dismissTip(tip.id)}
											aria-label="Dismiss tip"
											title="Dismiss"
										>✕</button>
									{/if}
								</div>
							{/each}
						</div>
					{:else}
						<p class="tips-all-dismissed">All insights dismissed.</p>
					{/if}

					{#if dismissedTips.length > 0}
						<button
							class="tips-show-dismissed"
							onclick={() => (showDismissed = !showDismissed)}
						>
							{showDismissed ? 'Hide dismissed' : `Show ${dismissedTips.length} dismissed ${dismissedTips.length === 1 ? 'tip' : 'tips'}`}
						</button>

						{#if showDismissed}
							<div class="tips-list tips-list-dismissed">
								{#each dismissedTips as tip (tip.id)}
									<div class="tip-card tip-card-dismissed">
										<div class="tip-icon" aria-hidden="true">ℹ</div>
										<div class="tip-body">
											<p class="tip-title">{tip.title}</p>
										</div>
									</div>
								{/each}
							</div>
						{/if}
					{/if}
				{/if}
			</section>
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
								<th class="th-right">{selectedYear}</th>
								{#if compareEnabled && comparison}
									<th class="th-right td-prior">{compareYear}</th>
									<th class="th-right td-change">Change</th>
								{/if}
								<th>IRS Line</th>
							</tr>
						</thead>
						<tbody>
							<!-- Income section -->
							{#if incomeItems.length > 0}
								<tr class="section-header-row">
									<td colspan={compareEnabled && comparison ? 5 : 3} class="section-label">Income</td>
								</tr>
								{#each incomeItems as item (item.tax_category)}
									{@const delta = deltasByCat.get(item.tax_category)}
									<tr>
										<td class="td-category">{categoryLabel(item.tax_category)}</td>
										<td class="td-amount {amountClass(item.total)}">{formatAmount(item.total)}</td>
										{#if compareEnabled && comparison}
											<td class="td-amount td-prior">
												{delta ? fmtCurrency(delta.prior) : '—'}
											</td>
											<td class="td-amount td-change {delta ? deltaClass(delta.delta, true) : 'delta-neutral'}">
												{#if delta}
													{fmtDelta(delta.delta)}
													{#if delta.delta_pct !== null && delta.delta_pct !== 0}
														<span class="delta-pct">{fmtDeltaPct(delta.delta_pct)}</span>
													{/if}
												{:else}
													<span class="no-data">—</span>
												{/if}
											</td>
										{/if}
										<td class="td-irs-line">{item.irs_line}</td>
									</tr>
								{/each}
								<tr class="subtotal-row">
									<td class="subtotal-label">Gross Income</td>
									<td class="td-amount {amountClass(summary.gross_income)} subtotal-val">{formatAmount(summary.gross_income)}</td>
									{#if compareEnabled && comparison}
										<td class="td-amount td-prior subtotal-val">{fmtCurrency(comparison.prior_gross_income)}</td>
										<td class="td-amount td-change subtotal-val {netProfitDeltaClass(summary.gross_income - comparison.prior_gross_income)}">
											{fmtDelta(summary.gross_income - comparison.prior_gross_income)}
										</td>
									{/if}
									<td></td>
								</tr>
							{/if}

							<!-- Expenses section -->
							{#if expenseItems.length > 0}
								<tr class="section-header-row">
									<td colspan={compareEnabled && comparison ? 5 : 3} class="section-label">Expenses</td>
								</tr>
								{#each expenseItems as item (item.tax_category)}
									{@const delta = deltasByCat.get(item.tax_category)}
									<tr>
										<td class="td-category">{categoryLabel(item.tax_category)}</td>
										<td class="td-amount {amountClass(-item.total)}">{formatAmount(-item.total)}</td>
										{#if compareEnabled && comparison}
											<td class="td-amount td-prior">
												{delta ? `(${fmtCurrency(delta.prior)})` : '—'}
											</td>
											<td class="td-amount td-change {delta ? deltaClass(delta.delta, false) : 'delta-neutral'}">
												{#if delta}
													{fmtDelta(delta.delta)}
													{#if delta.delta_pct !== null && delta.delta_pct !== 0}
														<span class="delta-pct">{fmtDeltaPct(delta.delta_pct)}</span>
													{/if}
												{:else}
													<span class="no-data">—</span>
												{/if}
											</td>
										{/if}
										<td class="td-irs-line">{item.irs_line}</td>
									</tr>
								{/each}
								<tr class="subtotal-row">
									<td class="subtotal-label">Total Expenses</td>
									<td class="td-amount {amountClass(-summary.total_expenses)} subtotal-val">{formatAmount(-summary.total_expenses)}</td>
									{#if compareEnabled && comparison}
										<td class="td-amount td-prior subtotal-val">({fmtCurrency(comparison.prior_total_expenses)})</td>
										<td class="td-amount td-change subtotal-val {deltaClass(summary.total_expenses - comparison.prior_total_expenses, false)}">
											{fmtDelta(summary.total_expenses - comparison.prior_total_expenses)}
										</td>
									{/if}
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
									{formatAmount(summary.net_profit)}
								</td>
								{#if compareEnabled && comparison}
									<td class="td-amount td-prior net-profit-amount">
										{formatAmount(comparison.prior_net_profit)}
									</td>
									<td class="td-amount td-change net-profit-amount {netProfitDeltaClass(comparison.net_profit_delta)}">
										{fmtDelta(comparison.net_profit_delta)}
										{#if comparison.net_profit_delta_pct !== null && comparison.net_profit_delta_pct !== 0}
											<span class="delta-pct">{fmtDeltaPct(comparison.net_profit_delta_pct)}</span>
										{/if}
									</td>
								{/if}
								<td class="td-irs-line net-profit-line">
									{selectedEntity === 'personal' ? 'Schedule A' : 'Schedule C Line 31'}
								</td>
							</tr>
						</tfoot>
					</table>
				{/if}
			</div>
		</section>

		<!-- ── Estimated Tax ──────────────────────────────────────────────────── -->
		{#if estimatedTax}
			<section class="dashboard-section">
				<h2 class="section-title">Estimated Quarterly Tax</h2>
				{#if estimatedTax.warning}
				<details class="est-warning-details">
					<summary class="est-warning-summary">
						<span class="est-warning-icon" aria-hidden="true">&#x26A0;&#xFE0E;</span>
						<span>Estimated tax notice</span>
					</summary>
					<p class="est-warning-text">{estimatedTax.warning}</p>
				</details>
				{/if}
				<div class="card est-card">
					<div class="est-summary">
						<div class="est-summary-item">
							<span class="est-label">YTD Net Profit</span>
							<span class="est-value">{fmtCurrency(estimatedTax.ytd_net_profit)}</span>
						</div>
						<div class="est-summary-item">
							<span class="est-label">Projected Annual</span>
							<span class="est-value">{fmtCurrency(estimatedTax.projected_annual_net)}</span>
						</div>
						<div class="est-summary-item">
							<span class="est-label">SE Tax</span>
							<span class="est-value">{fmtCurrency(estimatedTax.se_tax_annual)}</span>
						</div>
						<div class="est-summary-item">
							<span class="est-label">Income Tax</span>
							<span class="est-value">{fmtCurrency(estimatedTax.income_tax_annual)}</span>
						</div>
						<div class="est-summary-item est-summary-total">
							<span class="est-label">Total Annual</span>
							<span class="est-value">{fmtCurrency(estimatedTax.total_annual)}</span>
						</div>
						<div class="est-summary-item">
							<span class="est-label">Quarterly Payment</span>
							<span class="est-value">{fmtCurrency(estimatedTax.quarterly_payment)}</span>
						</div>
					</div>

					<table class="data-table est-table">
						<thead>
							<tr>
								<th>Quarter</th>
								<th class="th-right">Projected</th>
								<th class="th-right">Paid</th>
								<th class="th-right">Remaining</th>
								<th>Status</th>
							</tr>
						</thead>
						<tbody>
							{#each estimatedTax.quarters as q (q.quarter)}
								{@const badge = estStateBadge(q.state)}
								<tr>
									<td class="td-period">{q.quarter}</td>
									<td class="td-amount">{fmtCurrency(q.projected_amount)}</td>
									<td class="td-amount">{fmtCurrency(q.paid)}</td>
									<td class="td-amount">{q.remaining > 0 ? fmtCurrency(q.remaining) : '--'}</td>
									<td>
										<span class="est-badge {badge.cls}">{badge.label}</span>
									</td>
								</tr>
							{/each}
						</tbody>
						<tfoot>
							<tr>
								<td>Total</td>
								<td class="td-amount">{fmtCurrency(estimatedTax.total_annual)}</td>
								<td class="td-amount">{fmtCurrency(estimatedTax.total_paid)}</td>
								<td class="td-amount">
									{estimatedTax.total_annual - estimatedTax.total_paid > 0
										? fmtCurrency(estimatedTax.total_annual - estimatedTax.total_paid)
										: '--'}
								</td>
								<td></td>
							</tr>
						</tfoot>
					</table>
				</div>
			</section>
		{/if}

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
								<th class="th-right">{selectedYear}</th>
								{#if compareEnabled && comparison}
									<th class="th-right td-prior">{compareYear}</th>
									<th class="th-right td-change">Change</th>
								{/if}
							</tr>
						</thead>
						{#if bnoExpanded}
							<tbody>
								{#if selectedEntity === 'sparkry'}
									{#each MONTHS as month, i (month)}
										{@const curr = monthlyIncome[i]}
										{@const prior = priorMonthlyIncome[i]}
										{@const bnoD = curr - prior}
										<tr>
											<td class="td-period">{month} {selectedYear}</td>
											<td class="td-amount">
												{#if curr > 0}
													<span class="amount-positive">{formatAmount(curr)}</span>
												{:else}
													<span class="no-data">—</span>
												{/if}
											</td>
											{#if compareEnabled && comparison}
												<td class="td-amount td-prior">
													{#if prior > 0}
														{formatAmount(prior)}
													{:else}
														<span class="no-data">—</span>
													{/if}
												</td>
												<td class="td-amount td-change {netProfitDeltaClass(bnoD)}">
													{#if curr > 0 || prior > 0}
														{fmtDelta(bnoD)}
													{:else}
														<span class="no-data">—</span>
													{/if}
												</td>
											{/if}
										</tr>
									{/each}
								{:else}
									{#each QUARTERS as quarter, i (quarter)}
										{@const curr = quarterlyIncome[i]}
										{@const prior = priorQuarterlyIncome[i]}
										{@const bnoD = curr - prior}
										<tr>
											<td class="td-period">{quarter} {selectedYear}</td>
											<td class="td-amount">
												{#if curr > 0}
													<span class="amount-positive">{formatAmount(curr)}</span>
												{:else}
													<span class="no-data">—</span>
												{/if}
											</td>
											{#if compareEnabled && comparison}
												<td class="td-amount td-prior">
													{#if prior > 0}
														{formatAmount(prior)}
													{:else}
														<span class="no-data">—</span>
													{/if}
												</td>
												<td class="td-amount td-change {netProfitDeltaClass(bnoD)}">
													{#if curr > 0 || prior > 0}
														{fmtDelta(bnoD)}
													{:else}
														<span class="no-data">—</span>
													{/if}
												</td>
											{/if}
										</tr>
									{/each}
								{/if}
							</tbody>
						{/if}
						<tfoot>
							<tr>
								<td>Total</td>
								<td class="td-amount {amountClass(summary.gross_income)}">{formatAmount(summary.gross_income)}</td>
								{#if compareEnabled && comparison}
									<td class="td-amount td-prior">{formatAmount(comparison.prior_gross_income)}</td>
									<td class="td-amount td-change {netProfitDeltaClass(summary.gross_income - comparison.prior_gross_income)}">
										{fmtDelta(summary.gross_income - comparison.prior_gross_income)}
									</td>
								{/if}
							</tr>
						</tfoot>
					</table>
					<button
						class="bno-toggle-link no-print"
						onclick={() => (bnoExpanded = !bnoExpanded)}
						aria-expanded={bnoExpanded}
					>
						{bnoExpanded ? 'Hide' : 'View'} {selectedEntity === 'sparkry' ? 'monthly' : 'quarterly'} breakdown
					</button>
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

	/* ── Compare toggle button ───────────────────────────────────────────────── */
	.compare-toggle {
		padding: 6px 14px;
		background: none;
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		font-family: var(--font);
		font-size: 0.8rem;
		font-weight: 500;
		color: var(--text-muted);
		cursor: pointer;
		transition: background 0.12s, color 0.12s, border-color 0.12s;
		white-space: nowrap;
	}

	.compare-toggle:hover {
		color: var(--text);
		border-color: var(--gray-400);
	}

	.compare-toggle-active {
		background: var(--gray-900);
		border-color: var(--gray-900);
		color: var(--gray-50);
	}

	.compare-toggle-active:hover {
		background: var(--gray-700);
		border-color: var(--gray-700);
		color: var(--gray-50);
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

	/* ── YoY comparison columns ──────────────────────────────────────────────── */
	.td-prior {
		color: var(--text-muted);
		font-size: 0.875rem;
	}

	.td-change {
		min-width: 100px;
	}

	.delta-positive {
		color: var(--green-600);
	}

	.delta-negative {
		color: var(--red-600);
	}

	.delta-neutral {
		color: var(--text-muted);
	}

	.delta-pct {
		margin-left: 6px;
		font-size: 0.75rem;
		opacity: 0.8;
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
		max-width: 640px;
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

	/* ── Tax insight tips ────────────────────────────────────────────────────── */
	.tips-list {
		display: flex;
		flex-direction: column;
		gap: 10px;
	}

	.tip-card {
		display: flex;
		align-items: flex-start;
		gap: 12px;
		padding: 14px 16px;
		background: var(--blue-50, #eff6ff);
		border-left: 3px solid var(--blue-500, #3b82f6);
		border-radius: var(--radius-sm);
	}

	.tip-card-dismissed {
		opacity: 0.5;
		background: var(--gray-50);
		border-left-color: var(--gray-300);
	}

	.tip-icon {
		flex-shrink: 0;
		width: 18px;
		height: 18px;
		display: flex;
		align-items: center;
		justify-content: center;
		font-size: 0.85rem;
		color: var(--blue-500, #3b82f6);
		margin-top: 1px;
	}

	.tip-body {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 3px;
	}

	.tip-title {
		font-size: 0.875rem;
		font-weight: 600;
		color: var(--text);
		margin: 0;
	}

	.tip-detail {
		font-size: 0.8rem;
		color: var(--text-muted);
		margin: 0;
		line-height: 1.45;
	}

	.tip-action {
		display: inline-block;
		margin-top: 4px;
		font-size: 0.8rem;
		font-weight: 500;
		color: var(--blue-600, #2563eb);
		text-decoration: none;
	}

	.tip-action:hover {
		text-decoration: underline;
	}

	.tip-dismiss {
		flex-shrink: 0;
		background: none;
		border: none;
		cursor: pointer;
		color: var(--text-muted);
		font-size: 0.8rem;
		padding: 2px 4px;
		border-radius: 2px;
		line-height: 1;
		transition: color 0.1s;
	}

	.tip-dismiss:hover {
		color: var(--text);
	}

	.tips-all-dismissed {
		font-size: 0.875rem;
		color: var(--text-muted);
		padding: 8px 0;
	}

	.tips-show-dismissed {
		margin-top: 10px;
		background: none;
		border: none;
		cursor: pointer;
		font-family: var(--font);
		font-size: 0.8rem;
		color: var(--text-muted);
		padding: 0;
		text-decoration: underline;
		text-underline-offset: 2px;
	}

	.tips-show-dismissed:hover {
		color: var(--text);
	}

	.tips-list-dismissed {
		margin-top: 8px;
	}

	/* ── Estimated tax ──────────────────────────────────────────────────────── */
	.est-card {
		padding: 20px 22px;
	}

	.est-summary {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
		gap: 12px;
		margin-bottom: 20px;
	}

	.est-summary-item {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.est-summary-total {
		font-weight: 700;
	}

	.est-label {
		font-size: 0.75rem;
		color: var(--text-muted);
		text-transform: uppercase;
		letter-spacing: 0.04em;
		font-weight: 600;
	}

	.est-value {
		font-size: 1rem;
		font-variant-numeric: tabular-nums;
	}

	.est-table {
		max-width: 640px;
	}

	.est-badge {
		font-size: 0.7rem;
		font-weight: 600;
		padding: 2px 8px;
		border-radius: 999px;
		white-space: nowrap;
	}

	.est-paid {
		background: var(--green-100);
		color: var(--green-700);
	}

	.est-overdue {
		background: var(--red-100);
		color: var(--red-700);
	}

	.est-upcoming {
		background: var(--gray-100);
		color: var(--gray-600);
	}

	/* ── Insights toggle (collapsible) ──────────────────────────────────────── */
	.insights-toggle {
		display: flex;
		align-items: center;
		gap: 8px;
		background: none;
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		padding: 10px 16px;
		width: 100%;
		cursor: pointer;
		font-family: var(--font);
		font-size: 0.875rem;
		font-weight: 500;
		color: var(--text-muted);
		transition: background 0.12s, color 0.12s;
	}

	.insights-toggle:hover {
		background: var(--gray-50);
		color: var(--text);
	}

	.insights-toggle-text {
		flex: 1;
		text-align: left;
	}

	.chevron {
		font-size: 0.75rem;
		transition: transform 0.15s ease;
		display: inline-block;
	}

	.chevron-open {
		transform: rotate(90deg);
	}

	/* ── Estimated tax warning (details/summary) ───────────────────────────── */
	.est-warning-details {
		margin-bottom: 16px;
	}

	.est-warning-summary {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		cursor: pointer;
		font-size: 0.8rem;
		font-weight: 500;
		color: var(--amber-700);
		padding: 4px 0;
		list-style: none;
	}

	.est-warning-summary::-webkit-details-marker {
		display: none;
	}

	.est-warning-summary::marker {
		content: '';
	}

	.est-warning-icon {
		font-size: 0.9rem;
	}

	.est-warning-text {
		margin: 6px 0 0 0;
		padding: 8px 12px;
		background: var(--amber-100);
		border-left: 3px solid var(--amber-500);
		border-radius: var(--radius-sm);
		font-size: 0.825rem;
		color: var(--amber-700);
		line-height: 1.5;
	}

	/* ── B&O toggle link ────────────────────────────────────────────────────── */
	.bno-toggle-link {
		display: block;
		background: none;
		border: none;
		cursor: pointer;
		font-family: var(--font);
		font-size: 0.8rem;
		font-weight: 500;
		color: var(--blue-600);
		padding: 10px 14px;
		text-decoration: none;
		text-align: left;
		border-top: 1px solid var(--border);
	}

	.bno-toggle-link:hover {
		text-decoration: underline;
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
