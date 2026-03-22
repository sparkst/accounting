<script lang="ts">
	import { fetchTaxSummary, fetchAggregations } from '$lib/api';
	import type { TaxSummary, TaxLineItem, AggregationData } from '$lib/api';
	import { CATEGORY_LABELS, formatAmount, amountClass, entityBadgeClass } from '$lib/categories';

	// ── Constants ─────────────────────────────────────────────────────────────
	const ENTITIES = [
		{ value: 'sparkry', label: 'Sparkry AI LLC' },
		{ value: 'blackline', label: 'BlackLine MTB LLC' }
	] as const;

	const CURRENT_YEAR = new Date().getFullYear();
	const YEARS: number[] = [];
	for (let y = 2024; y <= CURRENT_YEAR + 1; y++) YEARS.push(y);

	const COGS_CAT = 'COGS';

	// ── State ─────────────────────────────────────────────────────────────────
	let selectedEntity = $state<'sparkry' | 'blackline'>('sparkry');
	let selectedYear = $state(CURRENT_YEAR);
	let compareMode = $state(false);
	let loading = $state(false);
	let fetchError = $state('');

	// Single entity data
	let summary = $state<TaxSummary | null>(null);
	let aggregations = $state<AggregationData | null>(null);

	// Comparison data (both entities)
	let sparkrySummary = $state<TaxSummary | null>(null);
	let blacklineSummary = $state<TaxSummary | null>(null);

	// Section collapse state — BLUF progressive disclosure
	let showExpenseDetail = $state(false);
	let showIncomeDetail = $state(false);
	let showMargins = $state(true);

	// ── Derived: single entity ───────────────────────────────────────────────
	let incomeItems = $derived.by((): TaxLineItem[] =>
		(summary?.line_items ?? []).filter(li => li.is_income)
	);

	let cogsItems = $derived.by((): TaxLineItem[] =>
		(summary?.line_items ?? []).filter(li => li.tax_category === COGS_CAT)
	);

	let expenseItems = $derived.by((): TaxLineItem[] =>
		(summary?.line_items ?? []).filter(li =>
			!li.is_income && !li.is_reimbursable && li.tax_category !== COGS_CAT
		)
	);

	let reimbursableItems = $derived.by((): TaxLineItem[] =>
		(summary?.line_items ?? []).filter(li => li.is_reimbursable)
	);

	let grossIncome = $derived(summary?.gross_income ?? 0);
	let totalCogs = $derived(cogsItems.reduce((s, li) => s + Math.abs(li.total), 0));
	let grossProfit = $derived(grossIncome - totalCogs);
	let totalExpenses = $derived(summary?.total_expenses ?? 0);
	let operatingExpenses = $derived(Math.abs(totalExpenses) - totalCogs);
	let netProfit = $derived(summary?.net_profit ?? 0);

	// Margins
	let grossMarginPct = $derived(grossIncome > 0 ? (grossProfit / grossIncome) * 100 : 0);
	let netMarginPct = $derived(grossIncome > 0 ? (netProfit / grossIncome) * 100 : 0);

	// Readiness
	let readinessPct = $derived(summary?.readiness.readiness_pct ?? 0);
	let needsReviewCount = $derived(summary?.readiness.needs_review_count ?? 0);

	// MoM data
	let momIncomePct = $derived(aggregations?.mom_change.income_pct ?? 0);
	let momExpensePct = $derived(aggregations?.mom_change.expense_pct ?? 0);

	// Expenses sorted by absolute value (used for bar chart and top-5 summary)
	let sortedExpenses = $derived.by(() =>
		[...expenseItems].sort((a, b) => Math.abs(b.total) - Math.abs(a.total))
	);
	let topExpenses = $derived(sortedExpenses.slice(0, 5));
	let maxExpense = $derived(Math.max(...expenseItems.map(li => Math.abs(li.total)), 1));

	// ── Derived: comparison mode ─────────────────────────────────────────
	let spIncome = $derived.by(() => extractItems(sparkrySummary, li => li.is_income));
	let blIncome = $derived.by(() => extractItems(blacklineSummary, li => li.is_income));
	let compareIncomeCats = $derived.by(() =>
		[...new Set([...spIncome.map(li => li.tax_category), ...blIncome.map(li => li.tax_category)])]
	);

	let spExpenses = $derived.by(() => extractItems(sparkrySummary, li => !li.is_income && !li.is_reimbursable));
	let blExpenses = $derived.by(() => extractItems(blacklineSummary, li => !li.is_income && !li.is_reimbursable));
	let compareExpenseCats = $derived.by(() =>
		[...new Set([...spExpenses.map(li => li.tax_category), ...blExpenses.map(li => li.tax_category)])]
	);

	// ── Load ─────────────────────────────────────────────────────────────────
	async function load() {
		loading = true;
		fetchError = '';
		try {
			if (compareMode) {
				const [sp, bl] = await Promise.all([
					fetchTaxSummary('sparkry', selectedYear),
					fetchTaxSummary('blackline', selectedYear)
				]);
				sparkrySummary = sp;
				blacklineSummary = bl;
				summary = selectedEntity === 'sparkry' ? sp : bl;
			} else {
				const [sum, agg] = await Promise.all([
					fetchTaxSummary(selectedEntity, selectedYear),
					fetchAggregations({
						entity: selectedEntity,
						date_from: `${selectedYear}-01-01`,
						date_to: `${selectedYear}-12-31`
					}).catch(() => null)
				]);
				summary = sum;
				aggregations = agg;
				sparkrySummary = null;
				blacklineSummary = null;
			}
		} catch (e) {
			fetchError = e instanceof Error ? e.message : 'Failed to load financial data';
		} finally {
			loading = false;
		}
	}

	// Load on mount and reload when entity/year/mode changes
	$effect(() => {
		void selectedEntity;
		void selectedYear;
		void compareMode;
		load();
	});

	// ── Helpers ───────────────────────────────────────────────────────────────
	function catLabel(cat: string): string {
		return CATEGORY_LABELS[cat] ?? cat.replace(/_/g, ' ');
	}

	function pctBadge(pct: number): string {
		if (pct > 0) return 'pct-up';
		if (pct < 0) return 'pct-down';
		return 'pct-flat';
	}

	function entityLabel(entity: string): string {
		if (entity === 'sparkry') return 'Sparkry AI';
		if (entity === 'blackline') return 'BlackLine MTB';
		return entity;
	}

	function extractItems(s: TaxSummary | null, filter: (li: TaxLineItem) => boolean): TaxLineItem[] {
		return (s?.line_items ?? []).filter(filter);
	}
</script>

<div class="container page-shell" aria-busy={loading}>
	<!-- ── Header ──────────────────────────────────────────────────────────── -->
	<header class="page-header">
		<div>
			<h1>Financials</h1>
			<p class="page-subtitle">Income statement and P&L by entity</p>
		</div>
		<div class="page-controls">
			<select
				bind:value={selectedEntity}
				class="control-select"
				aria-label="Select entity"
			>
				{#each ENTITIES as ent}
					<option value={ent.value}>{ent.label}</option>
				{/each}
			</select>
			<select
				bind:value={selectedYear}
				class="control-select"
				aria-label="Select year"
			>
				{#each YEARS as y}
					<option value={y}>{y}</option>
				{/each}
			</select>
			<label class="compare-toggle">
				<input type="checkbox" bind:checked={compareMode} />
				Compare entities
			</label>
		</div>
	</header>

	{#if loading && !summary}
		<div class="skeleton-grid">
			{#each Array(3) as _}
				<div class="card skeleton-card">
					<div class="skeleton" style="height: 14px; width: 40%; margin-bottom: 10px;"></div>
					<div class="skeleton" style="height: 28px; width: 60%;"></div>
				</div>
			{/each}
		</div>
	{:else if fetchError}
		<div class="card error-card">
			<p class="error-msg">{fetchError}</p>
			<button class="btn btn-ghost" onclick={load}>Try again</button>
		</div>
	{:else if compareMode && sparkrySummary && blacklineSummary}
		<!-- ═══════════════════════════════════════════════════════════════════ -->
		<!-- COMPARISON MODE: Side-by-side P&L                                 -->
		<!-- ═══════════════════════════════════════════════════════════════════ -->

		<!-- BLUF Summary Cards -->
		<div class="bluf-grid compare-bluf">
			{#each [sparkrySummary, blacklineSummary] as s}
				<div class="card bluf-card">
					<span class={entityBadgeClass(s.entity)}>{entityLabel(s.entity)}</span>
					<div class="bluf-amount {amountClass(s.net_profit)}">
						{formatAmount(s.net_profit)}
					</div>
					<span class="bluf-label">Net Profit — {selectedYear}</span>
					<div class="bluf-row">
						<span class="bluf-sub">Revenue {formatAmount(s.gross_income)}</span>
						<span class="bluf-sub">Expenses {formatAmount(s.total_expenses)}</span>
					</div>
				</div>
			{/each}
		</div>

		<!-- Side-by-side Income Statement -->
		<section class="dashboard-section">
			<h2 class="section-title">Income Statement Comparison — {selectedYear}</h2>
			<div class="card table-card">
				<table class="data-table financial-table">
					<thead>
						<tr>
							<th class="cat-col">Category</th>
							<th class="amt-col">
								<span class={entityBadgeClass('sparkry')}>Sparkry</span>
							</th>
							<th class="amt-col">
								<span class={entityBadgeClass('blackline')}>BlackLine</span>
							</th>
							<th class="amt-col">Combined</th>
						</tr>
					</thead>
					<tbody>
						<!-- Revenue -->
						<tr class="section-header-row">
							<td colspan="4"><strong>Revenue</strong></td>
						</tr>
						{#each compareIncomeCats as cat}
							{@const spAmt = spIncome.find(li => li.tax_category === cat)?.total ?? 0}
							{@const blAmt = blIncome.find(li => li.tax_category === cat)?.total ?? 0}
							<tr>
								<td class="indent-1">{catLabel(cat)}</td>
								<td class="amt-cell {amountClass(spAmt)}">{formatAmount(spAmt)}</td>
								<td class="amt-cell {amountClass(blAmt)}">{formatAmount(blAmt)}</td>
								<td class="amt-cell {amountClass(spAmt + blAmt)}">{formatAmount(spAmt + blAmt)}</td>
							</tr>
						{/each}
						<tr class="subtotal-row">
							<td><strong>Total Revenue</strong></td>
							<td class="amt-cell {amountClass(sparkrySummary.gross_income)}">
								<strong>{formatAmount(sparkrySummary.gross_income)}</strong>
							</td>
							<td class="amt-cell {amountClass(blacklineSummary.gross_income)}">
								<strong>{formatAmount(blacklineSummary.gross_income)}</strong>
							</td>
							<td class="amt-cell {amountClass(sparkrySummary.gross_income + blacklineSummary.gross_income)}">
								<strong>{formatAmount(sparkrySummary.gross_income + blacklineSummary.gross_income)}</strong>
							</td>
						</tr>

						<!-- Expenses -->
						<tr class="section-header-row">
							<td colspan="4"><strong>Operating Expenses</strong></td>
						</tr>
						{#each compareExpenseCats as cat}
							{@const spAmt = spExpenses.find(li => li.tax_category === cat)?.total ?? 0}
							{@const blAmt = blExpenses.find(li => li.tax_category === cat)?.total ?? 0}
							<tr>
								<td class="indent-1">{catLabel(cat)}</td>
								<td class="amt-cell {amountClass(spAmt)}">{formatAmount(Math.abs(spAmt))}</td>
								<td class="amt-cell {amountClass(blAmt)}">{formatAmount(Math.abs(blAmt))}</td>
								<td class="amt-cell">{formatAmount(Math.abs(spAmt) + Math.abs(blAmt))}</td>
							</tr>
						{/each}
						<tr class="subtotal-row">
							<td><strong>Total Expenses</strong></td>
							<td class="amt-cell">
								<strong>{formatAmount(Math.abs(sparkrySummary.total_expenses))}</strong>
							</td>
							<td class="amt-cell">
								<strong>{formatAmount(Math.abs(blacklineSummary.total_expenses))}</strong>
							</td>
							<td class="amt-cell">
								<strong>{formatAmount(Math.abs(sparkrySummary.total_expenses) + Math.abs(blacklineSummary.total_expenses))}</strong>
							</td>
						</tr>

						<!-- Net Profit -->
						<tr class="total-row">
							<td><strong>Net Profit</strong></td>
							<td class="amt-cell {amountClass(sparkrySummary.net_profit)}">
								<strong>{formatAmount(sparkrySummary.net_profit)}</strong>
							</td>
							<td class="amt-cell {amountClass(blacklineSummary.net_profit)}">
								<strong>{formatAmount(blacklineSummary.net_profit)}</strong>
							</td>
							<td class="amt-cell {amountClass(sparkrySummary.net_profit + blacklineSummary.net_profit)}">
								<strong>{formatAmount(sparkrySummary.net_profit + blacklineSummary.net_profit)}</strong>
							</td>
						</tr>
					</tbody>
				</table>
			</div>
		</section>

	{:else if summary}
		<!-- ═══════════════════════════════════════════════════════════════════ -->
		<!-- SINGLE ENTITY MODE                                                 -->
		<!-- ═══════════════════════════════════════════════════════════════════ -->

		<!-- BLUF: Net Profit + Key Metrics -->
		<div class="bluf-grid">
			<div class="card bluf-card bluf-primary">
				<span class="bluf-label">Net Profit — {selectedYear} YTD</span>
				<div class="bluf-amount {amountClass(netProfit)}">
					{formatAmount(netProfit)}
				</div>
				{#if grossIncome > 0}
					<span class="bluf-margin {pctBadge(netMarginPct)}">
						{netMarginPct.toFixed(1)}% net margin
					</span>
				{/if}
			</div>
			<div class="card bluf-card">
				<span class="bluf-label">Revenue</span>
				<div class="bluf-amount {amountClass(grossIncome)}">{formatAmount(grossIncome)}</div>
				{#if aggregations}
					<span class="bluf-trend {pctBadge(momIncomePct)}">
						{momIncomePct > 0 ? '+' : ''}{momIncomePct.toFixed(0)}% vs prior month
					</span>
				{/if}
			</div>
			<div class="card bluf-card">
				<span class="bluf-label">Expenses</span>
				<div class="bluf-amount">{formatAmount(Math.abs(totalExpenses))}</div>
				{#if aggregations}
					<span class="bluf-trend {pctBadge(-momExpensePct)}">
						{momExpensePct > 0 ? '+' : ''}{momExpensePct.toFixed(0)}% vs prior month
					</span>
				{/if}
			</div>
			<div class="card bluf-card">
				<span class="bluf-label">Data Confidence</span>
				<div class="bluf-amount confidence-value">{readinessPct.toFixed(0)}%</div>
				{#if needsReviewCount > 0}
					<a href="/register?status=needs_review&entity={selectedEntity}" class="bluf-link">
						{needsReviewCount} need review
					</a>
				{:else}
					<span class="bluf-ok">All confirmed</span>
				{/if}
			</div>
		</div>

		<!-- Margins bar (collapsible) -->
		{#if grossIncome > 0}
			<section class="dashboard-section">
				<button class="section-toggle" onclick={() => showMargins = !showMargins} aria-expanded={showMargins} aria-controls="profitability-panel">
					<h2 class="section-title">Profitability</h2>
					<span class="toggle-icon" aria-hidden="true">{showMargins ? '−' : '+'}</span>
				</button>
				{#if showMargins}
					<div id="profitability-panel" class="margins-grid">
						<div class="card margin-card">
							<div class="margin-bar-wrap">
								<div class="margin-bar">
									<div
										class="margin-fill margin-fill-gross"
										style="width: {Math.min(grossMarginPct, 100)}%"
										role="meter"
										aria-valuenow={grossMarginPct}
										aria-valuemin={0}
										aria-valuemax={100}
										aria-label="Gross margin {grossMarginPct.toFixed(1)}%"
									></div>
								</div>
								<div class="margin-labels">
									<span>Gross Margin</span>
									<span class="margin-pct">{grossMarginPct.toFixed(1)}%</span>
								</div>
							</div>
							<div class="margin-detail">
								<span>Revenue {formatAmount(grossIncome)}</span>
								<span>COGS {formatAmount(totalCogs)}</span>
								<span>Gross Profit <strong>{formatAmount(grossProfit)}</strong></span>
							</div>
						</div>
						<div class="card margin-card">
							<div class="margin-bar-wrap">
								<div class="margin-bar">
									<div
										class="margin-fill margin-fill-net"
										style="width: {Math.max(Math.min(netMarginPct, 100), 0)}%"
										role="meter"
										aria-valuenow={netMarginPct}
										aria-valuemin={0}
										aria-valuemax={100}
										aria-label="Net margin {netMarginPct.toFixed(1)}%"
									></div>
								</div>
								<div class="margin-labels">
									<span>Net Margin</span>
									<span class="margin-pct">{netMarginPct.toFixed(1)}%</span>
								</div>
							</div>
							<div class="margin-detail">
								<span>Gross Profit {formatAmount(grossProfit)}</span>
								<span>OpEx {formatAmount(operatingExpenses)}</span>
								<span>Net Profit <strong>{formatAmount(netProfit)}</strong></span>
							</div>
						</div>
					</div>
				{/if}
			</section>
		{/if}

		<!-- Income Statement -->
		<section class="dashboard-section">
			<h2 class="section-title">
				Income Statement — {entityLabel(selectedEntity)}, {selectedYear}
			</h2>

			<div class="card table-card">
				<table class="data-table financial-table">
					<thead>
						<tr>
							<th class="cat-col">Category</th>
							<th class="amt-col">Amount</th>
						</tr>
					</thead>
					<tbody>
						<!-- Revenue -->
						<tr class="section-header-row">
							<td colspan="2">
								<button class="inline-toggle" onclick={() => showIncomeDetail = !showIncomeDetail} aria-expanded={showIncomeDetail}>
									<strong>Revenue</strong>
									<span class="toggle-hint" aria-hidden="true">{showIncomeDetail ? '−' : '+'}</span>
								</button>
							</td>
						</tr>
						{#if showIncomeDetail}
							{#each incomeItems as li}
								<tr>
									<td class="indent-1">{catLabel(li.tax_category)}</td>
									<td class="amt-cell {amountClass(li.total)}">{formatAmount(li.total)}</td>
								</tr>
							{/each}
						{/if}
						<tr class="subtotal-row">
							<td><strong>Total Revenue</strong></td>
							<td class="amt-cell {amountClass(grossIncome)}">
								<strong>{formatAmount(grossIncome)}</strong>
							</td>
						</tr>

						<!-- COGS -->
						{#if cogsItems.length > 0}
							<tr class="section-header-row">
								<td colspan="2"><strong>Cost of Goods Sold</strong></td>
							</tr>
							{#each cogsItems as li}
								<tr>
									<td class="indent-1">{catLabel(li.tax_category)}</td>
									<td class="amt-cell">{formatAmount(Math.abs(li.total))}</td>
								</tr>
							{/each}
							<tr class="subtotal-row">
								<td><strong>Gross Profit</strong></td>
								<td class="amt-cell {amountClass(grossProfit)}">
									<strong>{formatAmount(grossProfit)}</strong>
								</td>
							</tr>
						{/if}

						<!-- Operating Expenses -->
						<tr class="section-header-row">
							<td colspan="2">
								<button class="inline-toggle" onclick={() => showExpenseDetail = !showExpenseDetail} aria-expanded={showExpenseDetail}>
									<strong>Operating Expenses</strong>
									<span class="toggle-hint" aria-hidden="true">{showExpenseDetail ? '−' : '+'}</span>
								</button>
							</td>
						</tr>
						{#if showExpenseDetail}
							{#each expenseItems as li}
								<tr>
									<td class="indent-1">{catLabel(li.tax_category)}</td>
									<td class="amt-cell">{formatAmount(Math.abs(li.total))}</td>
								</tr>
							{/each}
						{:else}
							<!-- Show top 5 when collapsed -->
							{#each topExpenses as li}
								<tr>
									<td class="indent-1">{catLabel(li.tax_category)}</td>
									<td class="amt-cell">{formatAmount(Math.abs(li.total))}</td>
								</tr>
							{/each}
							{#if expenseItems.length > 5}
								<tr>
									<td class="indent-1" colspan="2">
										<button class="inline-toggle text-muted" onclick={() => showExpenseDetail = true}>
											+{expenseItems.length - 5} more categories
										</button>
									</td>
								</tr>
							{/if}
						{/if}
						<tr class="subtotal-row">
							<td><strong>Total Operating Expenses</strong></td>
							<td class="amt-cell">
								<strong>{formatAmount(operatingExpenses)}</strong>
							</td>
						</tr>

						<!-- Reimbursables (if any) -->
						{#if reimbursableItems.length > 0}
							<tr class="section-header-row">
								<td colspan="2"><strong>Reimbursable (nets to $0)</strong></td>
							</tr>
							{#each reimbursableItems as li}
								<tr>
									<td class="indent-1">{catLabel(li.tax_category)}</td>
									<td class="amt-cell">{formatAmount(li.total)}</td>
								</tr>
							{/each}
						{/if}

						<!-- Net Profit -->
						<tr class="total-row">
							<td><strong>Net Profit</strong></td>
							<td class="amt-cell {amountClass(netProfit)}">
								<strong>{formatAmount(netProfit)}</strong>
							</td>
						</tr>
					</tbody>
				</table>
			</div>
		</section>

		<!-- Top Expense Categories (visual) -->
		{#if expenseItems.length > 0}
			<section class="dashboard-section">
				<h2 class="section-title">Expense Breakdown</h2>
				<div class="card expense-bars-card">
					{#each sortedExpenses as li}
						<div class="expense-bar-row">
							<span class="expense-bar-label">{catLabel(li.tax_category)}</span>
							<div class="expense-bar-track">
								<div
									class="expense-bar-fill"
									style="width: {(Math.abs(li.total) / maxExpense) * 100}%"
									role="meter"
									aria-valuenow={Math.abs(li.total)}
									aria-valuemin={0}
									aria-valuemax={maxExpense}
									aria-label="{catLabel(li.tax_category)} {formatAmount(Math.abs(li.total))}"
								></div>
							</div>
							<span class="expense-bar-amount">{formatAmount(Math.abs(li.total))}</span>
						</div>
					{/each}
				</div>
			</section>
		{/if}

		<!-- Revenue Sources (if aggregation data available) -->
		{#if aggregations && aggregations.top_vendors.income.length > 0}
			<section class="dashboard-section">
				<h2 class="section-title">Revenue Sources</h2>
				<div class="card">
					<table class="data-table">
						<thead>
							<tr>
								<th>Source</th>
								<th class="amt-col">Revenue</th>
								<th class="pct-col">%</th>
							</tr>
						</thead>
						<tbody>
							{#each aggregations.top_vendors.income as v}
								<tr>
									<td>{v.vendor}</td>
									<td class="amt-cell {amountClass(v.total)}">{formatAmount(v.total)}</td>
									<td class="pct-cell">{v.pct.toFixed(1)}%</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			</section>
		{/if}

		<!-- Warnings/Anomalies -->
		{#if aggregations && aggregations.concentration_warnings.length > 0}
			<section class="dashboard-section">
				{#each aggregations.concentration_warnings as w}
					<div class="alert-banner alert-warning">
						{w.message}
					</div>
				{/each}
			</section>
		{/if}

		<p class="page-footer-link">
			<a href="/tax?entity={selectedEntity}">View tax filing details &rarr;</a>
		</p>

	{/if}
</div>

<style>
	.page-shell {
		padding-top: 32px;
		padding-bottom: 64px;
	}

	/* ── Header ───────────────────────────────────────────────────────────── */
	.page-header {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: 16px;
		margin-bottom: 28px;
		flex-wrap: wrap;
	}

	.page-subtitle {
		margin-top: 4px;
		color: var(--text-muted);
		font-size: .875rem;
	}

	.page-controls {
		display: flex;
		gap: 10px;
		align-items: center;
		flex-wrap: wrap;
	}

	.control-select {
		font-family: var(--font);
		font-size: .8rem;
		padding: 5px 10px;
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		background: var(--surface);
		color: var(--text);
	}

	.compare-toggle {
		display: flex;
		align-items: center;
		gap: 6px;
		font-size: .8rem;
		color: var(--text-muted);
		cursor: pointer;
		white-space: nowrap;
	}

	.compare-toggle input {
		accent-color: var(--blue-500);
	}

	/* ── BLUF Cards ───────────────────────────────────────────────────────── */
	.bluf-grid {
		display: grid;
		grid-template-columns: repeat(4, 1fr);
		gap: 12px;
		margin-bottom: 28px;
	}

	.compare-bluf {
		grid-template-columns: 1fr 1fr;
	}

	@media (max-width: 800px) {
		.bluf-grid {
			grid-template-columns: 1fr 1fr;
		}
	}

	@media (max-width: 500px) {
		.bluf-grid {
			grid-template-columns: 1fr;
		}
	}

	.bluf-card {
		display: flex;
		flex-direction: column;
		gap: 4px;
		padding: 18px 20px;
	}

	.bluf-primary {
		border-left: 3px solid var(--blue-500);
	}

	.bluf-label {
		font-size: .75rem;
		font-weight: 500;
		color: var(--text-muted);
		text-transform: uppercase;
		letter-spacing: .04em;
	}

	.bluf-amount {
		font-size: 1.5rem;
		font-weight: 700;
		font-variant-numeric: tabular-nums;
		letter-spacing: -.5px;
	}

	.bluf-margin, .bluf-trend {
		font-size: .75rem;
		font-weight: 500;
	}

	.bluf-row {
		display: flex;
		gap: 16px;
		margin-top: 4px;
	}

	.bluf-sub {
		font-size: .75rem;
		color: var(--text-muted);
		font-variant-numeric: tabular-nums;
	}

	.bluf-link {
		font-size: .75rem;
		color: var(--amber-600);
		text-decoration: none;
	}

	.bluf-link:hover {
		text-decoration: underline;
	}

	.bluf-ok {
		font-size: .75rem;
		color: var(--green-600);
	}

	.confidence-value {
		color: var(--text);
	}

	.pct-up { color: var(--green-700); }
	.pct-down { color: var(--red-700); }
	.pct-flat { color: var(--text-muted); }

	/* ── Sections ─────────────────────────────────────────────────────────── */
	.dashboard-section {
		margin-bottom: 28px;
	}

	.section-title {
		margin-bottom: 12px;
		font-size: 1rem;
		font-weight: 600;
		color: var(--text);
	}

	.section-toggle {
		display: flex;
		align-items: center;
		justify-content: space-between;
		width: 100%;
		background: none;
		border: none;
		cursor: pointer;
		padding: 0;
		margin-bottom: 12px;
	}

	.toggle-icon {
		font-size: 1rem;
		color: var(--text-muted);
	}

	/* ── Margins ──────────────────────────────────────────────────────────── */
	.margins-grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 12px;
	}

	@media (max-width: 600px) {
		.margins-grid {
			grid-template-columns: 1fr;
		}
	}

	.margin-card {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.margin-bar-wrap {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.margin-bar {
		height: 8px;
		background: var(--gray-100);
		border-radius: 999px;
		overflow: hidden;
	}

	.margin-fill {
		height: 100%;
		border-radius: 999px;
		transition: width .4s ease;
	}

	.margin-fill-gross { background: var(--green-500); }
	.margin-fill-net { background: var(--blue-500); }

	.margin-labels {
		display: flex;
		justify-content: space-between;
		font-size: .75rem;
		color: var(--text-muted);
	}

	.margin-pct {
		font-weight: 600;
		font-variant-numeric: tabular-nums;
		color: var(--text);
	}

	.margin-detail {
		display: flex;
		gap: 16px;
		font-size: .75rem;
		color: var(--text-muted);
		font-variant-numeric: tabular-nums;
	}

	/* ── Financial Table ──────────────────────────────────────────────────── */
	.table-card {
		overflow-x: auto;
	}

	.financial-table {
		min-width: 100%;
	}

	.cat-col {
		text-align: left;
	}

	.amt-col {
		text-align: right;
		width: 140px;
	}

	.pct-col {
		text-align: right;
		width: 70px;
	}

	.amt-cell {
		text-align: right;
		font-variant-numeric: tabular-nums;
	}

	.pct-cell {
		text-align: right;
		font-variant-numeric: tabular-nums;
		color: var(--text-muted);
		font-size: .8rem;
	}

	.indent-1 {
		padding-left: 28px !important;
	}

	.section-header-row td {
		background: var(--gray-50);
		font-size: .8rem;
		text-transform: uppercase;
		letter-spacing: .04em;
		color: var(--text-muted);
		padding-top: 12px !important;
		padding-bottom: 8px !important;
	}

	.subtotal-row td {
		border-top: 1px solid var(--gray-300);
	}

	.total-row td {
		border-top: 2px solid var(--gray-800);
		background: var(--gray-50);
		font-size: .95rem;
	}

	.inline-toggle {
		background: none;
		border: none;
		cursor: pointer;
		display: inline-flex;
		align-items: center;
		gap: 6px;
		font-family: var(--font);
		font-size: .8rem;
		text-transform: uppercase;
		letter-spacing: .04em;
		color: var(--text-muted);
		padding: 0;
	}

	.toggle-hint {
		font-size: .7rem;
		color: var(--gray-400);
	}

	.text-muted {
		color: var(--text-muted);
		font-style: italic;
		font-size: .8rem;
	}

	/* ── Expense Bars ─────────────────────────────────────────────────────── */
	.expense-bars-card {
		display: flex;
		flex-direction: column;
		gap: 10px;
	}

	.expense-bar-row {
		display: grid;
		grid-template-columns: 160px 1fr 100px;
		align-items: center;
		gap: 12px;
		font-size: .8rem;
	}

	@media (max-width: 600px) {
		.expense-bar-row {
			grid-template-columns: 120px 1fr 80px;
		}
	}

	.expense-bar-label {
		color: var(--text);
		font-weight: 500;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.expense-bar-track {
		height: 6px;
		background: var(--gray-100);
		border-radius: 999px;
		overflow: hidden;
	}

	.expense-bar-fill {
		height: 100%;
		background: var(--gray-400);
		border-radius: 999px;
		opacity: .7;
		transition: width .4s ease;
	}

	.expense-bar-amount {
		text-align: right;
		font-variant-numeric: tabular-nums;
		color: var(--text-muted);
	}

	/* ── Utility ──────────────────────────────────────────────────────────── */
	.skeleton-grid {
		display: grid;
		grid-template-columns: repeat(3, 1fr);
		gap: 12px;
		margin-bottom: 28px;
	}

	.skeleton-card {
		padding: 20px;
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

	.page-footer-link {
		margin-top: 16px;
		font-size: .85rem;
	}

	.page-footer-link a {
		color: var(--text-muted);
		text-decoration: none;
	}

	.page-footer-link a:hover {
		color: var(--text);
		text-decoration: underline;
	}

	/* ── Print ────────────────────────────────────────────────────────────── */
	@media print {
		.page-controls,
		.compare-toggle,
		.section-toggle,
		.inline-toggle .toggle-hint,
		.page-footer-link,
		.bluf-link,
		.expense-bars-card {
			display: none !important;
		}

		.bluf-grid { grid-template-columns: repeat(4, 1fr); }
		.financial-table { page-break-inside: avoid; }
		.total-row { page-break-inside: avoid; }
		.dashboard-section { page-break-inside: avoid; }
	}
</style>
