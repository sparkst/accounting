<script lang="ts">
	import { page } from '$app/state';
	import {
		fetchBrokerageHoldingHistory,
		type BrokerageHoldingHistory
	} from '$lib/api';

	let history = $state<BrokerageHoldingHistory | null>(null);
	let loading = $state(true);
	let error = $state('');
	let rangeKey = $state<'3M' | '6M' | 'YTD' | '1Y' | 'All'>('1Y');

	const symbol = $derived(page.params.symbol?.toUpperCase() ?? '');

	function fmtCurrency(n: number | null | undefined): string {
		if (n === null || n === undefined) return '—';
		const sign = n < 0 ? '-' : '';
		return `${sign}$${Math.abs(n).toLocaleString('en-US', {
			minimumFractionDigits: 2,
			maximumFractionDigits: 2
		})}`;
	}

	function fmtQty(n: number | null | undefined): string {
		if (n === null || n === undefined) return '—';
		return n.toLocaleString('en-US', { maximumFractionDigits: 4 });
	}

	function fmtPct(n: number): string {
		const sign = n >= 0 ? '+' : '';
		return `${sign}${(n * 100).toFixed(2)}%`;
	}

	async function load() {
		if (!symbol) return;
		loading = true;
		error = '';
		try {
			history = await fetchBrokerageHoldingHistory(symbol);
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			loading = false;
		}
	}

	// $effect picks up route-param changes on mount AND on subsequent navigation;
	// no separate onMount needed.
	$effect(() => {
		if (symbol) load();
	});

	// Filter time series to selected range.
	const rangeStart = $derived.by(() => {
		if (!history?.value_series?.length) return null;
		const last = new Date(history.value_series[history.value_series.length - 1].as_of);
		switch (rangeKey) {
			case '3M':
				return new Date(last.getTime() - 90 * 86400000);
			case '6M':
				return new Date(last.getTime() - 180 * 86400000);
			case 'YTD':
				return new Date(last.getFullYear(), 0, 1);
			case '1Y':
				return new Date(last.getTime() - 365 * 86400000);
			case 'All':
				return null;
		}
	});

	const filteredSeries = $derived.by(() => {
		const series = history?.value_series ?? [];
		if (!rangeStart) return series;
		return series.filter((p) => new Date(p.as_of) >= rangeStart);
	});

	// Chart geometry
	const CHART_W = 720;
	const CHART_H = 220;
	const PAD_X = 40;
	const PAD_Y = 16;

	const chart = $derived.by(() => {
		const points = filteredSeries;
		if (points.length < 2) return null;
		const values = points.map((p) => p.market_value);
		const min = Math.min(...values);
		const max = Math.max(...values);
		const range = max - min || 1;
		const innerW = CHART_W - PAD_X * 2;
		const innerH = CHART_H - PAD_Y * 2;
		const xs = points.map((_, i) =>
			points.length === 1 ? PAD_X + innerW / 2 : PAD_X + (i / (points.length - 1)) * innerW
		);
		const ys = values.map((v) => PAD_Y + innerH - ((v - min) / range) * innerH);
		const linePath = xs.map((x, i) => `${i === 0 ? 'M' : 'L'} ${x.toFixed(1)} ${ys[i].toFixed(1)}`).join(' ');
		const areaPath = `${linePath} L ${xs[xs.length - 1].toFixed(1)} ${(PAD_Y + innerH).toFixed(1)} L ${xs[0].toFixed(1)} ${(PAD_Y + innerH).toFixed(1)} Z`;
		const startVal = values[0];
		const endVal = values[values.length - 1];
		return {
			points,
			xs,
			ys,
			min,
			max,
			linePath,
			areaPath,
			deltaAbs: endVal - startVal,
			deltaPct: startVal > 0 ? (endVal - startVal) / startVal : 0
		};
	});
</script>

<svelte:head>
	<title>{symbol} · Wealth</title>
</svelte:head>

<div class="page">
	<header class="header">
		<a class="back" href="/wealth">‹ Wealth</a>
		<h1>{symbol}</h1>
		{#if history}
			<p class="sub">{history.security_name ?? ''}</p>
		{/if}
	</header>

	{#if loading && !history}
		<div class="state">Loading…</div>
	{:else if error}
		<div class="state error">⚠ {error}</div>
	{:else if history}
		<section class="section">
			<div class="headline">
				<div class="value">{fmtCurrency(history.current_value)}</div>
				<div class="meta">
					Quantity {fmtQty(history.current_quantity)} ·
					Cost basis {fmtCurrency(history.cost_basis)} ·
					Unrealized
					<span class={history.unrealized_gain >= 0 ? 'pos' : 'neg'}>
						{fmtCurrency(history.unrealized_gain)} ({fmtPct(history.unrealized_pct)})
					</span>
				</div>
			</div>

			<div class="range-toggle">
				{#each ['3M', '6M', 'YTD', '1Y', 'All'] as r (r)}
					<button
						type="button"
						class="range-btn"
						class:active={rangeKey === r}
						onclick={() => (rangeKey = r as typeof rangeKey)}
					>
						{r}
					</button>
				{/each}
			</div>

			{#if chart}
				<div class="chart-meta">
					<span>Range:</span>
					<span class={chart.deltaAbs >= 0 ? 'pos' : 'neg'}>
						{chart.deltaAbs >= 0 ? '+' : ''}{fmtCurrency(chart.deltaAbs)} ({fmtPct(chart.deltaPct)})
					</span>
				</div>
				<svg class="chart" viewBox={`0 0 ${CHART_W} ${CHART_H}`} preserveAspectRatio="xMidYMid meet" role="img" aria-label="{symbol} value over time">
					<defs>
						<linearGradient id="holding-fill" x1="0" y1="0" x2="0" y2="1">
							<stop offset="0%" stop-color="#007aff" stop-opacity="0.18" />
							<stop offset="100%" stop-color="#007aff" stop-opacity="0" />
						</linearGradient>
					</defs>
					<path d={chart.areaPath} fill="url(#holding-fill)" />
					<path d={chart.linePath} fill="none" stroke="#007aff" stroke-width="2" />
				</svg>
				<div class="chart-axis muted">
					<span>{fmtCurrency(chart.min)}</span>
					<span>{fmtCurrency(chart.max)}</span>
				</div>
			{:else}
				<p class="muted empty">Not enough data points to chart this holding for the selected range.</p>
			{/if}
		</section>

		{#if history.lots && history.lots.length > 0}
			<section class="section">
				<h2>Historical Lots</h2>
				<p class="muted lots-note">
					Lot-level cost basis ingested from prior brokerage records. These supplement the live
					Position cost basis above; they are not used to compute current valuations.
				</p>
				<table class="data-table">
					<thead>
						<tr>
							<th>Source</th>
							<th>Open Date</th>
							<th class="num">Quantity</th>
							<th class="num">Cost / share</th>
							<th class="num">Cost total</th>
						</tr>
					</thead>
					<tbody>
						{#each history.lots as lot, i (i)}
							<tr>
								<td>{lot.raw_account_name}</td>
								<td>{lot.open_date}</td>
								<td class="num">{fmtQty(lot.quantity)}</td>
								<td class="num">{fmtCurrency(lot.cost_per_share)}</td>
								<td class="num">{fmtCurrency(lot.cost_total)}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</section>
		{/if}
	{/if}
</div>

<style>
	.page {
		max-width: 1000px;
		margin: 0 auto;
		padding: 24px;
		font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Helvetica Neue', sans-serif;
		color: #1d1d1f;
	}
	.header h1 {
		font-size: 32px;
		font-weight: 600;
		margin: 4px 0 4px 0;
	}
	.back {
		font-size: 13px;
		color: #007aff;
		text-decoration: none;
	}
	.back:hover {
		text-decoration: underline;
	}
	.sub {
		color: #6e6e73;
		margin: 0 0 24px 0;
		font-size: 14px;
	}

	.state {
		padding: 24px;
		text-align: center;
		color: #6e6e73;
	}
	.state.error {
		color: #d70015;
	}

	.section {
		background: #fff;
		border: 1px solid #e5e5e7;
		border-radius: 12px;
		padding: 20px 24px;
		margin-bottom: 16px;
	}
	.section h2 {
		font-size: 18px;
		font-weight: 600;
		margin: 0 0 12px 0;
	}

	.headline .value {
		font-size: 40px;
		font-weight: 600;
		font-feature-settings: 'tnum' 1;
	}
	.headline .meta {
		color: #6e6e73;
		font-size: 13px;
		margin-top: 4px;
	}

	.range-toggle {
		display: flex;
		gap: 6px;
		margin: 16px 0 8px;
	}
	.range-btn {
		font: inherit;
		font-size: 12px;
		padding: 4px 12px;
		border: 1px solid #d2d2d7;
		border-radius: 999px;
		background: #fff;
		color: #1d1d1f;
		cursor: pointer;
	}
	.range-btn.active {
		background: #1d1d1f;
		border-color: #1d1d1f;
		color: #fff;
	}

	.chart-meta {
		font-size: 13px;
		color: #6e6e73;
		margin-bottom: 4px;
	}
	.chart {
		width: 100%;
		height: auto;
		max-height: 260px;
		display: block;
	}
	.chart-axis {
		display: flex;
		justify-content: space-between;
		font-size: 11px;
		margin-top: 4px;
		padding: 0 8px;
		font-feature-settings: 'tnum' 1;
	}

	.muted {
		color: #6e6e73;
	}
	.empty {
		text-align: center;
		padding: 16px;
		font-style: italic;
	}
	.pos {
		color: #047a04;
	}
	.neg {
		color: #d70015;
	}

	.data-table {
		width: 100%;
		border-collapse: collapse;
		font-size: 14px;
	}
	.data-table thead th {
		text-align: left;
		font-weight: 500;
		color: #6e6e73;
		padding: 6px 8px;
		border-bottom: 1px solid #e5e5e7;
		font-size: 12px;
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}
	.data-table tbody td {
		padding: 8px;
		border-bottom: 1px solid #f0f0f2;
		font-feature-settings: 'tnum' 1;
	}
	.data-table .num {
		text-align: right;
		font-variant-numeric: tabular-nums;
	}

	.lots-note {
		font-size: 12px;
		margin: -4px 0 8px;
	}
</style>
