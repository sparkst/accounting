<script lang="ts">
	import type { AggregationData, TimeSeriesPoint, TopVendorItem, ConcentrationWarning, AnomalyItem, CategoryBreakdownItem } from '$lib/api';

	interface Props {
		data: AggregationData;
		card: 'income' | 'expenses' | 'net';
		onclose: () => void;
	}

	let { data, card, onclose }: Props = $props();

	// Pick series based on active card
	let series = $derived(
		card === 'income'
			? data.time_series.income
			: card === 'expenses'
				? data.time_series.expenses
				: mergeSeries(data.time_series.income, data.time_series.expenses)
	);

	let vendors = $derived(
		card === 'income'
			? data.top_vendors.income
			: card === 'expenses'
				? data.top_vendors.expense
				: [...data.top_vendors.income, ...data.top_vendors.expense]
					.sort((a, b) => b.total - a.total)
					.slice(0, 5)
	);

	let momDelta = $derived(
		card === 'income'
			? data.mom_change.income_delta
			: card === 'expenses'
				? data.mom_change.expense_delta
				: data.mom_change.income_delta - data.mom_change.expense_delta
	);

	let momPct = $derived(
		card === 'income'
			? data.mom_change.income_pct
			: card === 'expenses'
				? data.mom_change.expense_pct
				: 0
	);

	let accentColor = $derived(
		card === 'income' ? 'var(--green-500)' : card === 'expenses' ? 'var(--red-500)' : 'var(--blue-500)'
	);

	// Concentration warnings — only shown for income card
	let concentrationWarnings = $derived(
		card === 'income' ? (data.concentration_warnings ?? []) : []
	);

	// Expense-specific insight fields
	let anomalies = $derived<AnomalyItem[]>(
		card === 'expenses' ? (data.anomalies ?? []) : []
	);
	let categoryBreakdown = $derived<CategoryBreakdownItem[]>(
		card === 'expenses' ? (data.category_breakdown ?? []) : []
	);
	let expenseAttribution = $derived(
		card === 'expenses' ? (data.expense_attribution ?? '') : ''
	);

	function mergeSeries(income: TimeSeriesPoint[], expenses: TimeSeriesPoint[]): TimeSeriesPoint[] {
		const map = new Map<string, number>();
		for (const p of income) map.set(p.period, (map.get(p.period) ?? 0) + p.total);
		for (const p of expenses) map.set(p.period, (map.get(p.period) ?? 0) - p.total);
		return [...map.entries()]
			.sort(([a], [b]) => a.localeCompare(b))
			.map(([period, total]) => ({ period, total }));
	}

	// SVG chart dimensions
	const W = 460;
	const H = 180;
	const PAD_X = 40;
	const PAD_Y = 20;
	const CHART_W = W - PAD_X * 2;
	const CHART_H = H - PAD_Y * 2;

	let points = $derived(computePoints(series));
	let yLabels = $derived(computeYLabels(series));

	function computePoints(s: TimeSeriesPoint[]): string {
		if (s.length === 0) return '';
		const vals = s.map((p) => p.total);
		const maxV = Math.max(...vals, 1);
		const minV = Math.min(...vals, 0);
		const range = maxV - minV || 1;

		return s
			.map((p, i) => {
				const x = PAD_X + (s.length === 1 ? CHART_W / 2 : (i / (s.length - 1)) * CHART_W);
				const y = PAD_Y + CHART_H - ((p.total - minV) / range) * CHART_H;
				return `${x},${y}`;
			})
			.join(' ');
	}

	function computeYLabels(s: TimeSeriesPoint[]): { y: number; label: string }[] {
		if (s.length === 0) return [];
		const vals = s.map((p) => p.total);
		const maxV = Math.max(...vals, 1);
		const minV = Math.min(...vals, 0);
		const mid = (maxV + minV) / 2;
		return [
			{ y: PAD_Y, label: formatCompact(maxV) },
			{ y: PAD_Y + CHART_H / 2, label: formatCompact(mid) },
			{ y: PAD_Y + CHART_H, label: formatCompact(minV) }
		];
	}

	function formatCompact(n: number): string {
		if (Math.abs(n) >= 1000) return `$${(n / 1000).toFixed(1)}k`;
		return `$${n.toFixed(0)}`;
	}

	function formatCurrency(n: number): string {
		return new Intl.NumberFormat('en-US', {
			style: 'currency',
			currency: 'USD',
			minimumFractionDigits: 0,
			maximumFractionDigits: 0
		}).format(n);
	}

	function shortPeriod(p: string): string {
		// "2026-01" -> "Jan", "2026-01-15" -> "Jan 15", "2026-W03" -> "W03"
		if (p.includes('W')) return p.split('-').pop() ?? p;
		const parts = p.split('-');
		if (parts.length === 2) {
			const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
			return months[parseInt(parts[1], 10) - 1] ?? p;
		}
		if (parts.length === 3) {
			const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
			return `${months[parseInt(parts[1], 10) - 1]} ${parseInt(parts[2], 10)}`;
		}
		return p;
	}

	// X-axis labels (show up to 6)
	let xLabels = $derived(computeXLabels(series));

	function computeXLabels(s: TimeSeriesPoint[]): { x: number; label: string }[] {
		if (s.length === 0) return [];
		const step = Math.max(1, Math.floor(s.length / 6));
		const labels: { x: number; label: string }[] = [];
		for (let i = 0; i < s.length; i += step) {
			const x = PAD_X + (s.length === 1 ? CHART_W / 2 : (i / (s.length - 1)) * CHART_W);
			labels.push({ x, label: shortPeriod(s[i].period) });
		}
		return labels;
	}
</script>

<div class="insight-panel card" role="region" aria-label="{card} insights">
	<button class="insight-close" onclick={onclose} aria-label="Close insight panel">x</button>

	<div class="insight-body">
		<!-- Left: SVG chart -->
		<div class="insight-chart">
			{#if series.length === 0}
				<div class="insight-empty">No data for this period</div>
			{:else}
				<svg viewBox="0 0 {W} {H}" class="insight-svg" aria-hidden="true">
					<!-- Y-axis labels -->
					{#each yLabels as yl}
						<text x={PAD_X - 6} y={yl.y + 4} text-anchor="end" class="axis-label">{yl.label}</text>
						<line x1={PAD_X} y1={yl.y} x2={W - PAD_X} y2={yl.y} class="grid-line" />
					{/each}

					<!-- X-axis labels -->
					{#each xLabels as xl}
						<text x={xl.x} y={H - 2} text-anchor="middle" class="axis-label">{xl.label}</text>
					{/each}

					<!-- Line -->
					<polyline
						points={points}
						fill="none"
						stroke={accentColor}
						stroke-width="2.5"
						stroke-linejoin="round"
						stroke-linecap="round"
					/>

					<!-- Dots -->
					{#each series as p, i}
						{@const vals = series.map((s: TimeSeriesPoint) => s.total)}
						{@const maxV = Math.max(...vals, 1)}
						{@const minV = Math.min(...vals, 0)}
						{@const range = maxV - minV || 1}
						{@const cx = PAD_X + (series.length === 1 ? CHART_W / 2 : (i / (series.length - 1)) * CHART_W)}
						{@const cy = PAD_Y + CHART_H - ((p.total - minV) / range) * CHART_H}
						<circle {cx} {cy} r="3.5" fill={accentColor} />
					{/each}
				</svg>
			{/if}
		</div>

		<!-- Right: Top vendors -->
		<div class="insight-vendors">
			<h3 class="insight-vendors-title">Top Vendors</h3>
			{#if vendors.length === 0}
				<p class="insight-empty-text">No vendor data</p>
			{:else}
				<ul class="vendor-list">
					{#each vendors as v}
						<li class="vendor-item">
							<div class="vendor-info">
								<span class="vendor-name truncate">{v.vendor}</span>
								<span class="vendor-amount">{formatCurrency(v.total)}</span>
							</div>
							<div class="vendor-bar-track">
								<div
									class="vendor-bar-fill"
									style="width: {v.pct}%; background: {accentColor};"
								></div>
							</div>
						</li>
					{/each}
				</ul>
			{/if}
		</div>
	</div>

	<!-- Concentration warnings (income card only) -->
	{#if concentrationWarnings.length > 0}
		<div class="insight-warnings">
			{#each concentrationWarnings as w (w.vendor)}
				<div class="concentration-warning" role="alert" aria-label="Concentration risk">
					<span class="warning-icon">&#9651;</span>
					{w.message}
				</div>
			{/each}
		</div>
	{/if}

	<!-- Category breakdown (expenses card only) -->
	{#if categoryBreakdown.length > 0}
		<div class="insight-section">
			<h3 class="insight-section-title">Top Categories</h3>
			<ul class="category-list">
				{#each categoryBreakdown as cat (cat.category)}
					<li class="category-item">
						<div class="category-info">
							<span class="category-name">{cat.category.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())}</span>
							<span class="category-amount">{formatCurrency(cat.total)} <span class="category-pct">{cat.pct}%</span></span>
						</div>
						<div class="vendor-bar-track">
							<div class="vendor-bar-fill" style="width: {cat.pct}%; background: var(--red-500);"></div>
						</div>
					</li>
				{/each}
			</ul>
		</div>
	{/if}

	<!-- Anomaly callouts (expenses card only) -->
	{#if anomalies.length > 0}
		<div class="insight-warnings">
			{#each anomalies as a (a.tx_id)}
				<div class="anomaly-warning" role="alert" aria-label="Unusual charge">
					<span class="warning-icon">&#9651;</span>
					{a.message}
				</div>
			{/each}
		</div>
	{/if}

	<!-- Expense attribution (expenses card only) -->
	{#if expenseAttribution}
		<div class="insight-attribution">
			{expenseAttribution}
		</div>
	{/if}

	<!-- Bottom: MoM change indicator -->
	<div class="insight-mom">
		{#if momDelta > 0}
			<span class="mom-indicator mom-up">
				{card === 'income' ? 'Income' : card === 'expenses' ? 'Expenses' : 'Net'} up {momPct > 0 ? '+' : ''}{momPct.toFixed(1)}% vs prior period
			</span>
		{:else if momDelta < 0}
			<span class="mom-indicator mom-down">
				{card === 'income' ? 'Income' : card === 'expenses' ? 'Expenses' : 'Net'} down {Math.abs(momPct).toFixed(1)}% vs prior period
			</span>
		{:else}
			<span class="mom-indicator mom-flat">No change vs prior period</span>
		{/if}
	</div>
</div>

<style>
	.insight-panel {
		position: relative;
		padding: 20px 24px;
		margin-bottom: 16px;
		animation: slideDown 0.2s ease-out;
	}

	@keyframes slideDown {
		from { opacity: 0; transform: translateY(-8px); }
		to { opacity: 1; transform: translateY(0); }
	}

	.insight-close {
		position: absolute;
		top: 12px;
		right: 16px;
		background: none;
		border: none;
		font-size: 1rem;
		color: var(--text-muted);
		cursor: pointer;
		padding: 4px 8px;
		border-radius: var(--radius-sm);
		line-height: 1;
	}
	.insight-close:hover {
		background: var(--gray-100);
		color: var(--text);
	}

	.insight-body {
		display: flex;
		gap: 24px;
		align-items: flex-start;
	}

	.insight-chart {
		flex: 1;
		min-width: 0;
	}

	.insight-svg {
		width: 100%;
		height: auto;
		max-height: 200px;
	}

	.insight-svg .axis-label {
		font-size: 10px;
		fill: var(--text-muted);
		font-family: var(--font-mono);
	}

	.insight-svg .grid-line {
		stroke: var(--gray-200);
		stroke-width: 0.5;
		stroke-dasharray: 3 3;
	}

	.insight-empty {
		display: flex;
		align-items: center;
		justify-content: center;
		height: 120px;
		color: var(--text-muted);
		font-size: .875rem;
	}

	.insight-vendors {
		width: 240px;
		flex-shrink: 0;
	}

	.insight-vendors-title {
		font-size: .75rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: .06em;
		color: var(--text-muted);
		margin-bottom: 10px;
	}

	.insight-empty-text {
		color: var(--text-muted);
		font-size: .8rem;
	}

	.vendor-list {
		list-style: none;
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.vendor-item {
		display: flex;
		flex-direction: column;
		gap: 3px;
	}

	.vendor-info {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
		gap: 8px;
	}

	.vendor-name {
		font-size: .8rem;
		max-width: 150px;
	}

	.vendor-amount {
		font-size: .75rem;
		font-weight: 600;
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
		color: var(--text-muted);
	}

	.vendor-bar-track {
		height: 4px;
		background: var(--gray-100);
		border-radius: 2px;
		overflow: hidden;
	}

	.vendor-bar-fill {
		height: 100%;
		border-radius: 2px;
		transition: width 0.3s ease;
	}

	.insight-warnings {
		margin-top: 12px;
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.concentration-warning {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 7px 10px;
		border-radius: var(--radius-sm);
		background: color-mix(in srgb, var(--amber-500) 12%, transparent);
		border: 1px solid color-mix(in srgb, var(--amber-500) 30%, transparent);
		color: var(--amber-700, #92400e);
		font-size: .8rem;
		font-weight: 500;
	}

	.warning-icon {
		font-size: .7rem;
		flex-shrink: 0;
		color: var(--amber-600, #d97706);
	}

	.insight-mom {
		margin-top: 14px;
		padding-top: 12px;
		border-top: 1px solid var(--border);
	}

	.mom-indicator {
		font-size: .8rem;
		font-weight: 500;
	}

	.mom-up {
		color: var(--green-600);
	}
	.mom-up::before {
		content: '\2191 ';
	}

	.mom-down {
		color: var(--red-600);
	}
	.mom-down::before {
		content: '\2193 ';
	}

	.mom-flat {
		color: var(--text-muted);
	}

	.insight-section {
		margin-top: 14px;
		padding-top: 12px;
		border-top: 1px solid var(--border);
	}

	.insight-section-title {
		font-size: .75rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: .06em;
		color: var(--text-muted);
		margin-bottom: 10px;
	}

	.category-list {
		list-style: none;
		display: flex;
		flex-direction: column;
		gap: 7px;
	}

	.category-item {
		display: flex;
		flex-direction: column;
		gap: 3px;
	}

	.category-info {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
		gap: 8px;
	}

	.category-name {
		font-size: .8rem;
		color: var(--text);
	}

	.category-amount {
		font-size: .75rem;
		font-weight: 600;
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
		color: var(--text-muted);
	}

	.category-pct {
		font-weight: 400;
		opacity: 0.7;
	}

	.anomaly-warning {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 7px 10px;
		border-radius: var(--radius-sm);
		background: color-mix(in srgb, var(--amber-500) 12%, transparent);
		border: 1px solid color-mix(in srgb, var(--amber-500) 30%, transparent);
		color: var(--amber-700, #92400e);
		font-size: .8rem;
		font-weight: 500;
	}

	.insight-attribution {
		margin-top: 10px;
		font-size: .8rem;
		color: var(--text-muted);
		font-style: italic;
	}

	@media (max-width: 700px) {
		.insight-body {
			flex-direction: column;
		}
		.insight-vendors {
			width: 100%;
		}
	}
</style>
