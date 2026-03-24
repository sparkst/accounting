<script lang="ts">
	import { fetchTaxSummary } from '$lib/api';
	import type { TaxLineItem } from '$lib/api';
	import { CATEGORY_LABELS, formatAmount, amountClass } from '$lib/categories';

	// ── Constants ─────────────────────────────────────────────────────────────
	const ENTITIES = [
		{ value: 'all',       label: 'All Entities' },
		{ value: 'sparkry',   label: 'Sparkry AI LLC' },
		{ value: 'blackline', label: 'BlackLine MTB LLC' },
		{ value: 'personal',  label: 'Personal' },
	] as const;

	type EntityValue = typeof ENTITIES[number]['value'];

	const CURRENT_YEAR = new Date().getFullYear();
	const YEARS: number[] = [];
	for (let y = 2024; y <= CURRENT_YEAR + 1; y++) YEARS.push(y);

	// Categories that map to Investing activities (capital / asset related)
	const INVESTING_CATEGORIES = new Set([
		'CAPITAL_CONTRIBUTION',
		'INVESTMENT_INCOME',
		'CAR_AND_TRUCK',   // vehicle purchases
	]);

	// Categories that map to Financing activities (owner draws, loans, transfers)
	const FINANCING_CATEGORIES = new Set([
		'CAPITAL_CONTRIBUTION',
	]);

	// ── State ─────────────────────────────────────────────────────────────────
	let selectedEntity = $state<EntityValue>('sparkry');
	let selectedYear   = $state(CURRENT_YEAR);
	let loading        = $state(false);
	let fetchError     = $state('');

	// Section collapse
	let showOperatingDetail  = $state(true);
	let showInvestingDetail  = $state(true);
	let showFinancingDetail  = $state(true);

	// Raw line items for selected entity/year
	let lineItems = $state<TaxLineItem[]>([]);
	let grossIncome   = $state(0);
	let totalExpenses = $state(0);

	// ── Cash-flow mapping ─────────────────────────────────────────────────────
	// Operating: income + regular expenses (the everyday business cash flows)
	// Investing:  INVESTMENT_INCOME, vehicle/asset purchases (CAR_AND_TRUCK)
	// Financing: CAPITAL_CONTRIBUTION (owner puts money in or takes it out)
	//
	// Because the data model uses direction (income/expense/transfer) rather
	// than an explicit investing/financing flag, we use tax_category as a proxy.

	type CashFlowLine = { label: string; amount: number };

	let operatingLines = $derived.by((): CashFlowLine[] => {
		return lineItems
			.filter(li =>
				!INVESTING_CATEGORIES.has(li.tax_category) &&
				!FINANCING_CATEGORIES.has(li.tax_category)
			)
			.map(li => ({
				label: catLabel(li.tax_category),
				amount: li.is_income ? li.total : -Math.abs(li.total),
			}));
	});

	let investingLines = $derived.by((): CashFlowLine[] => {
		return lineItems
			.filter(li => INVESTING_CATEGORIES.has(li.tax_category))
			.map(li => ({
				label: catLabel(li.tax_category),
				amount: li.is_income ? li.total : -Math.abs(li.total),
			}));
	});

	// Financing: treat transfers from the register. Capital contributions are
	// inflows; owner draws (negative capital contribution) are outflows.
	let financingLines = $derived.by((): CashFlowLine[] => {
		return lineItems
			.filter(li => FINANCING_CATEGORIES.has(li.tax_category))
			.map(li => ({
				label: catLabel(li.tax_category),
				amount: li.is_income ? li.total : -Math.abs(li.total),
			}));
	});

	let operatingTotal  = $derived(operatingLines.reduce((s, l) => s + l.amount, 0));
	let investingTotal  = $derived(investingLines.reduce((s, l) => s + l.amount, 0));
	let financingTotal  = $derived(financingLines.reduce((s, l) => s + l.amount, 0));
	let netCashChange   = $derived(operatingTotal + investingTotal + financingTotal);

	// Net income for the BLUF summary card (revenue − expenses)
	let netIncome = $derived(grossIncome + totalExpenses); // totalExpenses is already negative

	// ── Load ─────────────────────────────────────────────────────────────────
	async function load() {
		loading = true;
		fetchError = '';
		lineItems = [];
		grossIncome = 0;
		totalExpenses = 0;

		try {
			if (selectedEntity === 'all') {
				// Fetch both business entities and merge
				const [sp, bl] = await Promise.all([
					fetchTaxSummary('sparkry', selectedYear),
					fetchTaxSummary('blackline', selectedYear),
				]);
				grossIncome   = sp.gross_income   + bl.gross_income;
				totalExpenses = sp.total_expenses + bl.total_expenses;

				// Merge line items by category
				const merged = new Map<string, TaxLineItem>();
				for (const li of [...sp.line_items, ...bl.line_items]) {
					if (merged.has(li.tax_category)) {
						const existing = merged.get(li.tax_category)!;
						merged.set(li.tax_category, { ...existing, total: existing.total + li.total });
					} else {
						merged.set(li.tax_category, { ...li });
					}
				}
				lineItems = [...merged.values()];
			} else if (selectedEntity === 'personal') {
				// Personal entity — attempt; API may return empty data
				try {
					const data = await fetchTaxSummary('personal', selectedYear);
					lineItems     = data.line_items;
					grossIncome   = data.gross_income;
					totalExpenses = data.total_expenses;
				} catch {
					lineItems     = [];
					grossIncome   = 0;
					totalExpenses = 0;
				}
			} else {
				const data = await fetchTaxSummary(selectedEntity, selectedYear);
				lineItems     = data.line_items;
				grossIncome   = data.gross_income;
				totalExpenses = data.total_expenses;
			}
		} catch (e) {
			fetchError = e instanceof Error ? e.message : 'Failed to load cash flow data';
		} finally {
			loading = false;
		}
	}

	$effect(() => {
		void selectedEntity;
		void selectedYear;
		load();
	});

	// ── Helpers ───────────────────────────────────────────────────────────────
	function catLabel(cat: string): string {
		return CATEGORY_LABELS[cat] ?? cat.replace(/_/g, ' ');
	}

	function entityLabel(e: EntityValue): string {
		return ENTITIES.find(ent => ent.value === e)?.label ?? e;
	}
</script>

<div class="container page-shell" aria-busy={loading}>
	<!-- ── Header ──────────────────────────────────────────────────────────── -->
	<header class="page-header">
		<div>
			<h1>Cash Flow</h1>
			<p class="page-subtitle">Operating, Investing, and Financing activities</p>
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
		</div>
	</header>

	{#if loading && lineItems.length === 0}
		<div class="skeleton-grid">
			{#each Array(4) as _}
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
	{:else}
		<!-- ── BLUF Summary Cards ──────────────────────────────────────────── -->
		<div class="bluf-grid">
			<div class="card bluf-card bluf-primary">
				<span class="bluf-label">Net Change in Cash</span>
				<div class="bluf-amount {amountClass(netCashChange)}">{formatAmount(netCashChange)}</div>
				<span class="bluf-sub">{entityLabel(selectedEntity)} — {selectedYear}</span>
			</div>
			<div class="card bluf-card">
				<span class="bluf-label">Operating</span>
				<div class="bluf-amount {amountClass(operatingTotal)}">{formatAmount(operatingTotal)}</div>
				<span class="bluf-sub">Day-to-day business</span>
			</div>
			<div class="card bluf-card">
				<span class="bluf-label">Investing</span>
				<div class="bluf-amount {amountClass(investingTotal)}">{formatAmount(investingTotal)}</div>
				<span class="bluf-sub">Capital &amp; assets</span>
			</div>
			<div class="card bluf-card">
				<span class="bluf-label">Financing</span>
				<div class="bluf-amount {amountClass(financingTotal)}">{formatAmount(financingTotal)}</div>
				<span class="bluf-sub">Owner draws &amp; contributions</span>
			</div>
		</div>

		<!-- ── Statement Table ────────────────────────────────────────────── -->
		<section class="dashboard-section">
			<h2 class="section-title">
				Cash Flow Statement — {entityLabel(selectedEntity)}, {selectedYear}
			</h2>
			<div class="card table-card">
				<table class="data-table financial-table">
					<thead>
						<tr>
							<th class="cat-col">Activity</th>
							<th class="amt-col">Amount</th>
						</tr>
					</thead>
					<tbody>
						<!-- ── Operating Activities ──────────────────────── -->
						<tr class="section-header-row">
							<td colspan="2">
								<button
									class="inline-toggle"
									onclick={() => showOperatingDetail = !showOperatingDetail}
									aria-expanded={showOperatingDetail}
								>
									<strong>Cash from Operating Activities</strong>
									<span class="toggle-hint" aria-hidden="true">{showOperatingDetail ? '−' : '+'}</span>
								</button>
							</td>
						</tr>
						{#if showOperatingDetail}
							{#if operatingLines.length > 0}
								{#each operatingLines as line}
									<tr>
										<td class="indent-1">{line.label}</td>
										<td class="amt-cell {amountClass(line.amount)}">{formatAmount(line.amount)}</td>
									</tr>
								{/each}
							{:else}
								<tr>
									<td class="indent-1 no-data-note" colspan="2">No operating transactions recorded</td>
								</tr>
							{/if}
						{/if}
						<tr class="subtotal-row">
							<td><strong>Net Cash from Operating Activities</strong></td>
							<td class="amt-cell {amountClass(operatingTotal)}">
								<strong>{formatAmount(operatingTotal)}</strong>
							</td>
						</tr>

						<!-- ── Investing Activities ──────────────────────── -->
						<tr class="section-header-row">
							<td colspan="2">
								<button
									class="inline-toggle"
									onclick={() => showInvestingDetail = !showInvestingDetail}
									aria-expanded={showInvestingDetail}
								>
									<strong>Cash from Investing Activities</strong>
									<span class="toggle-hint" aria-hidden="true">{showInvestingDetail ? '−' : '+'}</span>
								</button>
							</td>
						</tr>
						{#if showInvestingDetail}
							{#if investingLines.length > 0}
								{#each investingLines as line}
									<tr>
										<td class="indent-1">{line.label}</td>
										<td class="amt-cell {amountClass(line.amount)}">{formatAmount(line.amount)}</td>
									</tr>
								{/each}
							{:else}
								<tr>
									<td class="indent-1 no-data-note" colspan="2">No investing transactions recorded</td>
								</tr>
							{/if}
						{/if}
						<tr class="subtotal-row">
							<td><strong>Net Cash from Investing Activities</strong></td>
							<td class="amt-cell {amountClass(investingTotal)}">
								<strong>{formatAmount(investingTotal)}</strong>
							</td>
						</tr>

						<!-- ── Financing Activities ──────────────────────── -->
						<tr class="section-header-row">
							<td colspan="2">
								<button
									class="inline-toggle"
									onclick={() => showFinancingDetail = !showFinancingDetail}
									aria-expanded={showFinancingDetail}
								>
									<strong>Cash from Financing Activities</strong>
									<span class="toggle-hint" aria-hidden="true">{showFinancingDetail ? '−' : '+'}</span>
								</button>
							</td>
						</tr>
						{#if showFinancingDetail}
							{#if financingLines.length > 0}
								{#each financingLines as line}
									<tr>
										<td class="indent-1">{line.label}</td>
										<td class="amt-cell {amountClass(line.amount)}">{formatAmount(line.amount)}</td>
									</tr>
								{/each}
							{:else}
								<tr>
									<td class="indent-1 no-data-note" colspan="2">No financing transactions recorded</td>
								</tr>
							{/if}
						{/if}
						<tr class="subtotal-row">
							<td><strong>Net Cash from Financing Activities</strong></td>
							<td class="amt-cell {amountClass(financingTotal)}">
								<strong>{formatAmount(financingTotal)}</strong>
							</td>
						</tr>

						<!-- ── Net Change in Cash ─────────────────────────── -->
						<tr class="total-row">
							<td><strong>Net Change in Cash</strong></td>
							<td class="amt-cell {amountClass(netCashChange)}">
								<strong>{formatAmount(netCashChange)}</strong>
							</td>
						</tr>
					</tbody>
				</table>
			</div>
		</section>

		<!-- ── Waterfall Visual ───────────────────────────────────────────── -->
		{#if operatingTotal !== 0 || investingTotal !== 0 || financingTotal !== 0}
			{@const maxAbs = Math.max(
				Math.abs(operatingTotal),
				Math.abs(investingTotal),
				Math.abs(financingTotal),
				Math.abs(netCashChange),
				1
			)}
			<section class="dashboard-section">
				<h2 class="section-title">Cash Flow Breakdown</h2>
				<div class="card waterfall-card">
					{#each [
						{ label: 'Operating',  amount: operatingTotal,  color: operatingTotal >= 0 ? 'bar-positive' : 'bar-negative' },
						{ label: 'Investing',  amount: investingTotal,  color: investingTotal >= 0 ? 'bar-positive' : 'bar-negative' },
						{ label: 'Financing',  amount: financingTotal,  color: financingTotal >= 0 ? 'bar-positive' : 'bar-negative' },
						{ label: 'Net Change', amount: netCashChange,   color: netCashChange >= 0 ? 'bar-net-positive' : 'bar-net-negative' },
					] as row}
						<div class="waterfall-row">
							<span class="waterfall-label">{row.label}</span>
							<div class="waterfall-track">
								<div
									class="waterfall-fill {row.color}"
									style="width: {(Math.abs(row.amount) / maxAbs) * 100}%"
									role="meter"
									aria-valuenow={row.amount}
									aria-valuemin={-maxAbs}
									aria-valuemax={maxAbs}
									aria-label="{row.label} {formatAmount(row.amount)}"
								></div>
							</div>
							<span class="waterfall-amount {amountClass(row.amount)}">{formatAmount(row.amount)}</span>
						</div>
					{/each}
				</div>
			</section>
		{/if}

		<!-- ── Footer ────────────────────────────────────────────────────── -->
		<p class="page-footer-note">
			Cash flow is derived from confirmed and classified transactions.
			Operating activities include all income and expenses not categorized as investing or financing.
			<a href="/financials?entity={selectedEntity === 'all' ? 'sparkry' : selectedEntity}">View Financials &rarr;</a>
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

	/* ── BLUF Cards ───────────────────────────────────────────────────────── */
	.bluf-grid {
		display: grid;
		grid-template-columns: repeat(4, 1fr);
		gap: 12px;
		margin-bottom: 28px;
	}

	@media (max-width: 800px) {
		.bluf-grid { grid-template-columns: 1fr 1fr; }
	}

	@media (max-width: 500px) {
		.bluf-grid { grid-template-columns: 1fr; }
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

	.bluf-sub {
		font-size: .75rem;
		color: var(--text-muted);
	}

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
		width: 160px;
	}

	.amt-cell {
		text-align: right;
		font-variant-numeric: tabular-nums;
	}

	.indent-1 {
		padding-left: 28px !important;
	}

	.no-data-note {
		color: var(--text-muted);
		font-size: .8rem;
		font-style: italic;
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

	/* ── Waterfall Chart ──────────────────────────────────────────────────── */
	.waterfall-card {
		display: flex;
		flex-direction: column;
		gap: 14px;
		padding: 20px 22px;
	}

	.waterfall-row {
		display: grid;
		grid-template-columns: 120px 1fr 120px;
		align-items: center;
		gap: 14px;
		font-size: .85rem;
	}

	@media (max-width: 600px) {
		.waterfall-row {
			grid-template-columns: 90px 1fr 90px;
		}
	}

	.waterfall-label {
		font-weight: 500;
		color: var(--text);
	}

	.waterfall-track {
		height: 10px;
		background: var(--gray-100);
		border-radius: 999px;
		overflow: hidden;
	}

	.waterfall-fill {
		height: 100%;
		border-radius: 999px;
		transition: width .4s ease;
	}

	.bar-positive      { background: var(--green-500); opacity: .75; }
	.bar-negative      { background: var(--red-500);   opacity: .75; }
	.bar-net-positive  { background: var(--blue-500);  opacity: .9; }
	.bar-net-negative  { background: var(--red-600);   opacity: .9; }

	.waterfall-amount {
		text-align: right;
		font-variant-numeric: tabular-nums;
		font-weight: 500;
	}

	/* ── Skeleton ─────────────────────────────────────────────────────────── */
	.skeleton-grid {
		display: grid;
		grid-template-columns: repeat(4, 1fr);
		gap: 12px;
		margin-bottom: 28px;
	}

	@media (max-width: 800px) {
		.skeleton-grid { grid-template-columns: 1fr 1fr; }
	}

	.skeleton-card {
		padding: 20px;
	}

	.skeleton {
		background: var(--gray-200);
		border-radius: var(--radius-sm);
		animation: pulse 1.5s ease-in-out infinite;
	}

	@keyframes pulse {
		0%, 100% { opacity: 1; }
		50%       { opacity: .4; }
	}

	/* ── Error ────────────────────────────────────────────────────────────── */
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

	/* ── Footer note ──────────────────────────────────────────────────────── */
	.page-footer-note {
		font-size: .8rem;
		color: var(--text-muted);
		line-height: 1.6;
	}

	.page-footer-note a {
		color: var(--text-muted);
		text-decoration: none;
		margin-left: 6px;
	}

	.page-footer-note a:hover {
		color: var(--text);
		text-decoration: underline;
	}

	/* ── Print ────────────────────────────────────────────────────────────── */
	@media print {
		.page-controls,
		.inline-toggle .toggle-hint,
		.waterfall-card,
		.page-footer-note { display: none !important; }

		.bluf-grid { grid-template-columns: repeat(4, 1fr); }
		.financial-table { page-break-inside: avoid; }
		.total-row { page-break-inside: avoid; }
	}
</style>
