<script lang="ts">
	import { onMount } from 'svelte';
	import {
		fetchBrokerageRecentTransactions,
		fetchBrokerageRealizedGL,
		type BrokerageRecentTransaction,
		type BrokerageRealizedGL
	} from '$lib/api';
	import {
		brokerColor,
		brokerDisplayName,
		fmtCurrency,
		fmtQty,
		fmtSignedCurrency,
		fmtSignedCurrencyExact,
		amountClass,
		applySort,
		matchesQuery,
		nextSortDir,
		sortIndicator as sortInd,
		toggleSetMember,
		type SortDir
	} from '$lib/brokerage';

	// ── State ─────────────────────────────────────────────────────────────
	let txns = $state<BrokerageRecentTransaction[] | null>(null);
	let realizedGl = $state<BrokerageRealizedGL | null>(null);
	let days = $state(90);
	let loading = $state(true);
	// Initial-load error blanks the page; refetch error keeps the prior
	// good data on screen with an inline banner.
	let initialError = $state('');
	let refetchError = $state('');

	let txnQuery = $state('');
	let txnSortKey = $state<string | null>('trade_date');
	let txnSortDir = $state<SortDir>('desc');
	let txnBrokerFilter = $state<Set<string>>(new Set());

	async function load(isInitial = false): Promise<void> {
		if (isInitial) loading = true;
		refetchError = '';
		// `Promise.allSettled` so one slow/broken sub-call doesn't blank the
		// whole page — txns and realized G/L can degrade independently.
		const [tRes, glRes] = await Promise.allSettled([
			fetchBrokerageRecentTransactions(days),
			fetchBrokerageRealizedGL()
		]);
		const errors: string[] = [];
		if (tRes.status === 'fulfilled') txns = tRes.value;
		else errors.push(`transactions: ${tRes.reason?.message ?? tRes.reason}`);
		if (glRes.status === 'fulfilled') realizedGl = glRes.value;
		else errors.push(`realized G/L: ${glRes.reason?.message ?? glRes.reason}`);

		if (errors.length > 0) {
			const msg = errors.join('; ');
			if (isInitial && txns === null && realizedGl === null) initialError = msg;
			else refetchError = msg;
		} else {
			initialError = '';
		}
		if (isInitial) loading = false;
	}

	// Days-selector change refetches BOTH txns and realized G/L via
	// `load(false)` so the user can recover from a previous G/L failure
	// just by changing the day window — `reloadTxns` would otherwise leave
	// the realized G/L permanently in its broken state.
	async function reloadTxns(): Promise<void> {
		await load(false);
	}

	onMount(() => load(true));

	function toggleSort(key: string): void {
		if (txnSortKey !== key) {
			txnSortKey = key;
			txnSortDir = 'asc';
			return;
		}
		const next = nextSortDir(txnSortDir);
		txnSortKey = next ? key : null;
		txnSortDir = next;
	}

	function sortIndicator(key: string): string {
		return sortInd(key, txnSortKey, txnSortDir);
	}

	let txnBrokerOptions = $derived(
		Array.from(new Set((txns ?? []).map((t) => t.broker))).sort()
	);

	let filteredTxns = $derived.by(() => {
		const filtered = (txns ?? []).filter((t) => {
			if (txnBrokerFilter.size > 0 && !txnBrokerFilter.has(t.broker)) return false;
			return matchesQuery([t.symbol, t.action, t.account_number_masked, t.broker], txnQuery);
		});
		return applySort(filtered, txnSortKey, txnSortDir);
	});

	let netSum = $derived(filteredTxns.reduce((s, t) => s + (t.amount ?? 0), 0));

	let realizedYears = $derived(
		realizedGl
			? Object.keys(realizedGl.by_year)
					.map(Number)
					.sort((a, b) => b - a)
			: []
	);
</script>

<svelte:head>
	<title>All Transactions · Wealth</title>
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
		<a href="/wealth">Wealth</a>
		<span class="sep">/</span>
		<span>Transactions</span>
	</nav>

	<h1 class="sr-only">All transactions</h1>

	{#if loading && !txns}
		<div class="state">Loading…</div>
	{:else if initialError}
		<div class="state error">⚠ {initialError}</div>
	{:else}
		<section class="section first">
			<div class="sec-head">
				<h2 class="sec-title">All transactions · {filteredTxns.length} of {txns?.length ?? 0}</h2>
				<div class="table-controls">
					<input
						type="search"
						class="search-input"
						placeholder="Search transactions…"
						bind:value={txnQuery}
					/>
					<label class="control">
						Days
						<select bind:value={days} onchange={reloadTxns}>
							<option value={30}>30</option>
							<option value={90}>90</option>
							<option value={180}>180</option>
							<option value={365}>365</option>
						</select>
					</label>
				</div>
			</div>

			{#if refetchError}
				<div class="inline-error" role="alert">
					⚠ Could not refresh: {refetchError}
					<button type="button" class="inline-error-retry" onclick={() => load(false)}>Retry</button>
				</div>
			{/if}

			<div class="activity-summary">
				Last {days} days · {filteredTxns.length} transactions · net cash flow (wealth)
				<b class={netSum >= 0 ? 'pos' : 'neg'}>{fmtSignedCurrencyExact(netSum)}</b>
			</div>

			{#if txnBrokerOptions.length > 1}
				<div class="filter-chips">
					<span class="chip-label">Broker:</span>
					{#each txnBrokerOptions as broker (broker)}
						<button
							type="button"
							class="chip"
							class:active={txnBrokerFilter.has(broker)}
							onclick={() => (txnBrokerFilter = toggleSetMember(txnBrokerFilter, broker))}
						>
							{brokerDisplayName(broker)}
						</button>
					{/each}
					{#if txnBrokerFilter.size > 0}
						<button type="button" class="chip clear" onclick={() => (txnBrokerFilter = new Set())}>
							Clear
						</button>
					{/if}
				</div>
			{/if}

			<table class="data-table compact">
				<thead>
					<tr>
						{#each [{ key: 'trade_date', label: 'Date' }, { key: 'broker', label: 'Broker' }, { key: 'account_number_masked', label: 'Account' }, { key: 'action', label: 'Action' }, { key: 'symbol', label: 'Symbol' }] as col (col.key)}
							<th class="sortable" onclick={() => toggleSort(col.key)}
								>{col.label}{sortIndicator(col.key)}</th
							>
						{/each}
						<th class="num sortable" onclick={() => toggleSort('quantity')}
							>Qty{sortIndicator('quantity')}</th
						>
						<th class="num sortable" onclick={() => toggleSort('amount')}
							>Amount{sortIndicator('amount')}</th
						>
					</tr>
				</thead>
				<tbody>
					{#each filteredTxns as t, i (`${t.trade_date}-${t.account_number_masked}-${t.action}-${i}`)}
						<tr>
							<td class="tx-date">{t.trade_date}</td>
							<td>
								<span class="broker-cell">
									<span class="broker-dot" style:background={brokerColor(t.broker)}></span>
									{brokerDisplayName(t.broker)}
								</span>
							</td>
							<td>{t.account_number_masked}</td>
							<td>{t.action}</td>
							<td>{t.symbol ?? ''}</td>
							<td class="num">{fmtQty(t.quantity)}</td>
							<td class="num {amountClass(t.amount)}">{fmtCurrency(t.amount)}</td>
						</tr>
					{/each}
					{#if filteredTxns.length === 0}
						<tr
							><td colspan="7" class="muted empty">No transactions match the current filter.</td></tr
						>
					{/if}
				</tbody>
			</table>
		</section>

		{#if realizedGl}
			<section class="section">
				<div class="sec-head">
					<h2 class="sec-title">Realized gains &amp; losses · all years</h2>
				</div>
				<table class="data-table">
					<thead>
						<tr>
							<th>Year</th>
							<th class="num">Short-term</th>
							<th class="num">Long-term</th>
							<th class="num">Unknown</th>
							<th class="num">Total</th>
							<th class="num">Lots</th>
						</tr>
					</thead>
					<tbody>
						{#each realizedYears as year (year)}
							{@const b = realizedGl.by_year[String(year)]}
							<tr>
								<td>{year}</td>
								<td class="num {amountClass(b.short_term)}">{fmtCurrency(b.short_term)}</td>
								<td class="num {amountClass(b.long_term)}">{fmtCurrency(b.long_term)}</td>
								<td class="num {amountClass(b.unknown)}">{fmtCurrency(b.unknown)}</td>
								<td class="num {amountClass(b.total)}">{fmtCurrency(b.total)}</td>
								<td class="num">{b.lots}</td>
							</tr>
						{/each}
					</tbody>
				</table>
				<p class="gl-note" style="margin-top:14px;">
					{#if realizedGl.wash_sales.lots === 0}
						<span class="ok">✓</span> No wash sales detected in ingested data.
						<span style="opacity:0.7;">1099-B substantiation not yet ingested.</span>
					{:else}
						<span class="warn">⚠</span> <strong>Wash sales:</strong>
						{realizedGl.wash_sales.lots} lots, total disallowed loss
						{fmtCurrency(realizedGl.wash_sales.total_disallowed_loss)}
					{/if}
				</p>
			</section>
		{/if}
	{/if}
</div>
