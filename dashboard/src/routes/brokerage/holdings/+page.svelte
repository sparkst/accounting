<script lang="ts">
	import { onMount } from 'svelte';
	import { fetchBrokerageTopHoldings, type BrokerageHolding } from '$lib/api';
	import {
		fmtCurrency,
		fmtPct,
		fmtQty,
		fmtCurrencyNoCents,
		applySort,
		matchesQuery,
		nextSortDir,
		sortIndicator as sortInd,
		type SortDir
	} from '$lib/brokerage';

	// ── State ─────────────────────────────────────────────────────────────
	let holdings = $state<BrokerageHolding[] | null>(null);
	let topN = $state(50);
	let loading = $state(true);
	// `initialError` only blanks the page on first-load failure. A refetch
	// failure (e.g. switching the topN selector) sets `refetchError` so the
	// previous good table stays visible with an inline banner instead.
	let initialError = $state('');
	let refetchError = $state('');

	let holdQuery = $state('');
	let holdSortKey = $state<string | null>('total_market_value');
	let holdSortDir = $state<SortDir>('desc');
	let holdCashFilter = $state<'all' | 'cash' | 'non-cash'>('all');

	async function load(isInitial = false): Promise<void> {
		if (isInitial) loading = true;
		refetchError = '';
		try {
			holdings = await fetchBrokerageTopHoldings(topN);
			initialError = '';
		} catch (e) {
			const msg = e instanceof Error ? e.message : String(e);
			if (isInitial || holdings === null) initialError = msg;
			else refetchError = msg;
		} finally {
			if (isInitial) loading = false;
		}
	}

	onMount(() => load(true));

	function toggleSort(key: string): void {
		if (holdSortKey !== key) {
			holdSortKey = key;
			holdSortDir = 'asc';
			return;
		}
		const next = nextSortDir(holdSortDir);
		holdSortKey = next ? key : null;
		holdSortDir = next;
	}
	function sortIndicator(key: string): string {
		return sortInd(key, holdSortKey, holdSortDir);
	}

	let filteredHoldings = $derived.by(() => {
		const filtered = (holdings ?? []).filter((h) => {
			if (holdCashFilter === 'cash' && !h.is_cash_sleeve) return false;
			if (holdCashFilter === 'non-cash' && h.is_cash_sleeve) return false;
			return matchesQuery([h.symbol, h.description], holdQuery);
		});
		return applySort(filtered, holdSortKey, holdSortDir);
	});

	let visibleTotal = $derived(
		filteredHoldings.reduce((s, h) => s + h.total_market_value, 0)
	);
</script>

<svelte:head>
	<title>All Holdings · Brokerage</title>
	<link rel="preconnect" href="https://fonts.googleapis.com" />
	<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin="anonymous" />
	<link
		href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap"
		rel="stylesheet"
	/>
</svelte:head>

<div class="page brokerage">
	<nav class="crumbs" aria-label="Breadcrumb">
		<a href="/">Money</a>
		<span class="sep">/</span>
		<a href="/brokerage">Brokerage</a>
		<span class="sep">/</span>
		<span>Holdings</span>
	</nav>

	<h1 class="sr-only">All holdings</h1>

	{#if loading && !holdings}
		<div class="state">Loading…</div>
	{:else if initialError}
		<div class="state error">⚠ {initialError}</div>
	{:else}
		<section class="section first">
			<div class="sec-head">
				<h2 class="sec-title">All holdings · {filteredHoldings.length} of {holdings?.length ?? 0}</h2>
				<div class="table-controls">
					<input
						type="search"
						class="search-input"
						placeholder="Search holdings…"
						bind:value={holdQuery}
					/>
					<label class="control">
						Show top
						<select bind:value={topN} onchange={() => load(false)}>
							<option value={25}>25</option>
							<option value={50}>50</option>
							<option value={100}>100</option>
							<option value={250}>250</option>
						</select>
					</label>
				</div>
			</div>

			<div class="hero-meta brokerage-hero-meta-spacer">
				Visible total: <b>{fmtCurrencyNoCents(visibleTotal)}</b>
			</div>

			{#if refetchError}
				<div class="inline-error" role="alert">
					⚠ Could not refresh: {refetchError}
					<button type="button" class="inline-error-retry" onclick={() => load(false)}>Retry</button>
				</div>
			{/if}

			<div class="filter-chips">
				<span class="chip-label">Type:</span>
				<button
					type="button"
					class="chip"
					class:active={holdCashFilter === 'all'}
					onclick={() => (holdCashFilter = 'all')}>All</button
				>
				<button
					type="button"
					class="chip"
					class:active={holdCashFilter === 'non-cash'}
					onclick={() => (holdCashFilter = 'non-cash')}>Non-cash</button
				>
				<button
					type="button"
					class="chip"
					class:active={holdCashFilter === 'cash'}
					onclick={() => (holdCashFilter = 'cash')}>Cash sleeves</button
				>
			</div>

			<table class="data-table">
				<thead>
					<tr>
						<th class="sortable" onclick={() => toggleSort('symbol')}
							>Symbol / Description{sortIndicator('symbol')}</th
						>
						<th class="num sortable" onclick={() => toggleSort('total_quantity')}
							>Quantity{sortIndicator('total_quantity')}</th
						>
						<th class="num sortable" onclick={() => toggleSort('total_market_value')}
							>Market value{sortIndicator('total_market_value')}</th
						>
						<th class="num sortable" onclick={() => toggleSort('pct_of_net_worth')}
							>% of net worth{sortIndicator('pct_of_net_worth')}</th
						>
						<th class="num sortable" onclick={() => toggleSort('account_count')}
							># accounts{sortIndicator('account_count')}</th
						>
					</tr>
				</thead>
				<tbody>
					{#each filteredHoldings as h, i (h.symbol ?? h.description ?? `idx-${i}`)}
						<tr class:cash={h.is_cash_sleeve}>
							<td>
								{#if h.is_cash_sleeve}
									<strong>Cash</strong>
								{:else if h.symbol}
									<a class="account-link" href={`/brokerage/holdings/${h.symbol}`}
										><strong>{h.symbol}</strong></a
									>
									{#if h.description}<span class="muted"> · {h.description}</span>{/if}
								{:else}
									<span class="muted">{h.description ?? '(unknown)'}</span>
								{/if}
							</td>
							<td class="num">{fmtQty(h.total_quantity)}</td>
							<td class="num">{fmtCurrency(h.total_market_value)}</td>
							<td class="num">
								<div class="pct-row">
									<div class="pct-bar">
										<div
											style:width="{Math.min(100, h.pct_of_net_worth * 100).toFixed(2)}%"
										></div>
									</div>
									<span>{fmtPct(h.pct_of_net_worth)}</span>
								</div>
							</td>
							<td class="num">{h.account_count}</td>
						</tr>
					{/each}
					{#if filteredHoldings.length === 0}
						<tr
							><td colspan="5" class="muted empty">No holdings match the current filter.</td></tr
						>
					{/if}
				</tbody>
			</table>
		</section>
	{/if}
</div>
