<script lang="ts">
	import { fmtCurrency, fmtPct } from '$lib/brokerage';
	import type { BrokeragePolicy } from '$lib/api';

	let { policy }: { policy: BrokeragePolicy } = $props();

	// Headroom badge: positive headroom (below glide) = ok; negative (above) = warn.
	const headroomOk = $derived(policy.headroom_pts >= 0);

	// Minimal inline SVG for the glide line vs the current point.
	const W = 320;
	const H = 80;
	const chart = $derived.by(() => {
		const pts = policy.glide_series;
		if (pts.length === 0) return { path: '', cx: 0, cy: 0 };
		const xs = pts.map((_, i) => (i / Math.max(1, pts.length - 1)) * W);
		const ys = pts.map((p) => p.glide_pct);
		const ymin = Math.min(...ys, policy.current_pct) - 2;
		const ymax = Math.max(...ys, policy.current_pct) + 2;
		const scaleY = (v: number) => H - ((v - ymin) / Math.max(0.001, ymax - ymin)) * H;
		const path = pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${xs[i].toFixed(1)},${scaleY(p.glide_pct).toFixed(1)}`).join(' ');
		// Current point plotted at "today" — approximate x by month index vs series.
		const nowIdx = pts.findIndex((p) => p.month >= policy.as_of.slice(0, 7));
		const cx = nowIdx < 0 ? 0 : (nowIdx / Math.max(1, pts.length - 1)) * W;
		return { path, cx, cy: scaleY(policy.current_pct) };
	});

	const sortedConcentration = $derived(
		[...policy.concentration].sort((a, b) => (b.embedded_gain ?? -Infinity) - (a.embedded_gain ?? -Infinity))
	);
</script>

<!-- REQ-IPD-001..003: concentration vs glide, intl/cash %, excise headroom. -->
<section class="panel">
	<div class="stat-grid">
		<div class="stat">
			<span class="label">AMZN+MSFT vs glide</span>
			<span class="big">{policy.current_pct.toFixed(1)}%</span>
			<span class="sub">glide {policy.glide_pct.toFixed(1)}%
				<span class="badge {headroomOk ? 'ok' : 'warn'}">
					{headroomOk ? '+' : ''}{policy.headroom_pts.toFixed(1)} pts
				</span>
			</span>
		</div>
		<div class="stat">
			<span class="label">International (% of equity)</span>
			<span class="big">{fmtPct(policy.international_pct_of_equity)}</span>
			<span class="sub">target {policy.international_target_pct}%</span>
		</div>
		<div class="stat">
			<span class="label">Cash</span>
			<span class="big">{fmtPct(policy.cash_pct)}</span>
			<span class="sub">{fmtCurrency(policy.cash_value)}</span>
		</div>
		<div class="stat">
			<span class="label">WA excise headroom ({policy.wa_tax_year})</span>
			<span class="big">{policy.excise_threshold_headroom === null ? '—' : fmtCurrency(policy.excise_threshold_headroom)}</span>
			<span class="sub">LT gains YTD {fmtCurrency(policy.realized_lt_gains_ytd)} · surcharge {policy.excise_surcharge_headroom === null ? '—' : fmtCurrency(policy.excise_surcharge_headroom)}</span>
		</div>
	</div>

	<div class="chart-wrap">
		<h4>Concentration glide (AMZN+MSFT → 35% by 2031-07)</h4>
		<svg viewBox="0 0 {W} {H}" preserveAspectRatio="none" class="glide">
			<path d={chart.path} fill="none" stroke="#1a73e8" stroke-width="1.5" />
			<circle cx={chart.cx} cy={chart.cy} r="3" fill={headroomOk ? '#137333' : '#c5221f'} />
		</svg>
		<div class="axis"><span>{policy.glide_series[0]?.month ?? ''}</span><span>{policy.glide_series.at(-1)?.month ?? ''}</span></div>
	</div>

	<div class="totals">
		Investable base <strong>{fmtCurrency(policy.investable_base)}</strong>
		· equity {fmtCurrency(policy.equity_base)}
	</div>

	<h4>Embedded gains (by holding)</h4>
	<table>
		<thead>
			<tr>
				<th>Symbol</th>
				<th class="num">Value</th>
				<th class="num">% of base</th>
				<th class="num">Cost basis</th>
				<th class="num">Embedded gain</th>
			</tr>
		</thead>
		<tbody>
			{#each sortedConcentration as c (c.symbol)}
				<tr>
					<td>{c.symbol}</td>
					<td class="num">{fmtCurrency(c.market_value)}</td>
					<td class="num">{fmtPct(c.pct)}</td>
					<td class="num">{c.basis_missing ? '⚠ missing' : fmtCurrency(c.cost_basis)}</td>
					<td class="num">{c.embedded_gain === null ? '—' : fmtCurrency(c.embedded_gain)}</td>
				</tr>
			{/each}
		</tbody>
	</table>

	{#if policy.warnings.length > 0}
		<ul class="warnings">
			{#each policy.warnings as w (w)}<li>⚠ {w}</li>{/each}
		</ul>
	{/if}
</section>

<style>
	.panel {
		border: 1px solid var(--border, #ddd);
		border-radius: 8px;
		padding: 1rem;
		margin: 1rem 0;
	}
	.stat-grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
		gap: 0.75rem;
	}
	.stat {
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
		padding: 0.5rem;
		border: 1px solid var(--border, #eee);
		border-radius: 6px;
	}
	.label {
		font-size: 0.75rem;
		color: #666;
	}
	.big {
		font-size: 1.4rem;
		font-variant-numeric: tabular-nums;
	}
	.sub {
		font-size: 0.75rem;
		color: #666;
	}
	.badge {
		font-size: 0.7rem;
		padding: 0.05rem 0.35rem;
		border-radius: 999px;
	}
	.badge.ok {
		background: #e6f4ea;
		color: #137333;
	}
	.badge.warn {
		background: #fce8e6;
		color: #c5221f;
	}
	.chart-wrap {
		margin: 1rem 0;
	}
	h4 {
		margin: 0.75rem 0 0.35rem;
		font-size: 0.85rem;
	}
	svg.glide {
		width: 100%;
		height: 80px;
		border: 1px solid var(--border, #eee);
		border-radius: 4px;
	}
	.axis {
		display: flex;
		justify-content: space-between;
		font-size: 0.7rem;
		color: #888;
	}
	.totals {
		font-size: 0.85rem;
		margin: 0.5rem 0;
	}
	table {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.85rem;
	}
	th,
	td {
		text-align: left;
		padding: 0.3rem 0.5rem;
		border-bottom: 1px solid var(--border, #eee);
	}
	.num {
		text-align: right;
		font-variant-numeric: tabular-nums;
	}
	.warnings {
		font-size: 0.8rem;
		color: #b06000;
		margin: 0.5rem 0 0;
		padding-left: 1rem;
	}
</style>
