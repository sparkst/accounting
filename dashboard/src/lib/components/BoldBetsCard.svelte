<script lang="ts">
	import { fmtCurrency, fmtPct } from '$lib/brokerage';
	import type { BrokerageBoldBets } from '$lib/api';

	let { boldBets }: { boldBets: BrokerageBoldBets } = $props();
</script>

<!-- REQ-BBT-001..002: speculative sleeve + cap status. -->
<section class="card">
	<header class="card-head">
		<h3>Bold Bets</h3>
		{#if boldBets.over_cap}
			<span class="chip breach">Over cap — {fmtCurrency(boldBets.sleeve_value)} &gt; {fmtCurrency(boldBets.cap)}</span>
		{:else}
			<span class="chip ok">{fmtCurrency(boldBets.sleeve_value)} / {fmtCurrency(boldBets.cap)} cap</span>
		{/if}
	</header>

	{#if boldBets.over_cap}
		<p class="advice">
			Sleeve is above the ${boldBets.cap.toLocaleString()} cap. Consider housing
			quick-turnaround trades in the Roth (display/report only — no enforcement).
		</p>
	{/if}

	<div class="totals">
		<span>Value {fmtCurrency(boldBets.sleeve_value)}</span>
		<span>Unrealized {fmtCurrency(boldBets.sleeve_unrealized)}</span>
		<span>Realized {fmtCurrency(boldBets.sleeve_realized)}</span>
		<span>{fmtPct(boldBets.pct_of_investable)} of investable</span>
	</div>

	{#if boldBets.positions.length === 0}
		<p class="empty">No bold-bet positions (tag an account `bold-bet` or add symbols to config).</p>
	{:else}
		<table>
			<thead>
				<tr>
					<th>Symbol</th>
					<th class="num">Value</th>
					<th class="num">Cost</th>
					<th class="num">Unrealized</th>
					<th class="num">Realized</th>
					<th>Thesis / Exit</th>
				</tr>
			</thead>
			<tbody>
				{#each boldBets.positions as p (p.account_id + p.symbol)}
					<tr>
						<td>{p.symbol}</td>
						<td class="num">{fmtCurrency(p.market_value)}</td>
						<td class="num">{p.cost_basis === null ? '—' : fmtCurrency(p.cost_basis)}</td>
						<td class="num">{p.unrealized_gain === null ? '—' : fmtCurrency(p.unrealized_gain)}</td>
						<td class="num">{fmtCurrency(p.realized_gain)}</td>
						<td class="notes">
							{#if p.thesis}<div><strong>Thesis:</strong> {p.thesis}</div>{/if}
							{#if p.exit}<div><strong>Exit:</strong> {p.exit}</div>{/if}
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{/if}
</section>

<style>
	.card {
		border: 1px solid var(--border, #ddd);
		border-radius: 8px;
		padding: 1rem;
		margin: 1rem 0;
	}
	.card-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem;
	}
	h3 {
		margin: 0;
		font-size: 1rem;
	}
	.chip {
		font-size: 0.8rem;
		padding: 0.15rem 0.5rem;
		border-radius: 999px;
	}
	.chip.ok {
		background: #e6f4ea;
		color: #137333;
	}
	.chip.breach {
		background: #fce8e6;
		color: #c5221f;
	}
	.advice {
		font-size: 0.85rem;
		color: #b06000;
	}
	.totals {
		display: flex;
		flex-wrap: wrap;
		gap: 1rem;
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
	.notes {
		font-size: 0.78rem;
		color: #555;
	}
	.empty {
		font-size: 0.85rem;
		color: #777;
	}
</style>
