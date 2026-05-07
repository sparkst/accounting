<script lang="ts">
	import { onMount } from 'svelte';
	import {
		fetchBrokerageNetWorth,
		fetchBrokerageAccounts,
		fetchBrokerageTopHoldings,
		fetchBrokerageRecentTransactions,
		fetchBrokerageRealizedGL,
		fetchBrokerageDataIntegrity
	} from '$lib/api';
	import type {
		BrokerageNetWorth,
		BrokerageAccount,
		BrokerageHolding,
		BrokerageRecentTransaction,
		BrokerageRealizedGL,
		BrokerageDataIntegrity
	} from '$lib/api';

	// ── State ─────────────────────────────────────────────────────────────
	let netWorth = $state<BrokerageNetWorth | null>(null);
	let accounts = $state<BrokerageAccount[] | null>(null);
	let topHoldings = $state<BrokerageHolding[] | null>(null);
	let recentTxns = $state<BrokerageRecentTransaction[] | null>(null);
	let realizedGl = $state<BrokerageRealizedGL | null>(null);
	let dataIntegrity = $state<BrokerageDataIntegrity | null>(null);

	let topN = $state(10);
	let recentDays = $state(14);

	let loading = $state(true);
	let error = $state('');

	let showRecent = $state(true);
	let showRealizedGL = $state(true);
	let showIntegrity = $state(false);

	// ── Per-table sort/filter/search state ────────────────────────────────
	type SortDir = 'asc' | 'desc' | null;

	let acctQuery = $state('');
	let acctSortKey = $state<string | null>('market_value');
	let acctSortDir = $state<SortDir>('desc');
	let acctBrokerFilter = $state<Set<string>>(new Set());

	let holdQuery = $state('');
	let holdSortKey = $state<string | null>('total_market_value');
	let holdSortDir = $state<SortDir>('desc');
	let holdCashFilter = $state<'all' | 'cash' | 'non-cash'>('all');

	let txnQuery = $state('');
	let txnSortKey = $state<string | null>('trade_date');
	let txnSortDir = $state<SortDir>('desc');
	let txnBrokerFilter = $state<Set<string>>(new Set());

	// ── Helpers ───────────────────────────────────────────────────────────
	function fmtCurrency(n: number | null | undefined): string {
		if (n === null || n === undefined) return '—';
		const sign = n < 0 ? '-' : '';
		return `${sign}$${Math.abs(n).toLocaleString('en-US', {
			minimumFractionDigits: 2,
			maximumFractionDigits: 2
		})}`;
	}

	function fmtPct(n: number): string {
		return `${(n * 100).toFixed(1)}%`;
	}

	function fmtQty(n: number | null | undefined): string {
		if (n === null || n === undefined) return '—';
		return n.toLocaleString('en-US', { maximumFractionDigits: 4 });
	}

	function amountClass(n: number | null | undefined): string {
		if (n === null || n === undefined) return '';
		return n >= 0 ? 'pos' : 'neg';
	}

	// ── Data loading ──────────────────────────────────────────────────────
	async function loadAll() {
		loading = true;
		error = '';
		try {
			const [nw, accts, holdings, txns, gl, integrity] = await Promise.all([
				fetchBrokerageNetWorth(),
				fetchBrokerageAccounts(),
				fetchBrokerageTopHoldings(topN),
				fetchBrokerageRecentTransactions(recentDays),
				fetchBrokerageRealizedGL(),
				fetchBrokerageDataIntegrity()
			]);
			netWorth = nw;
			accounts = accts;
			topHoldings = holdings;
			recentTxns = txns;
			realizedGl = gl;
			dataIntegrity = integrity;
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			loading = false;
		}
	}

	async function reloadHoldings() {
		try {
			topHoldings = await fetchBrokerageTopHoldings(topN);
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		}
	}

	async function reloadRecent() {
		try {
			recentTxns = await fetchBrokerageRecentTransactions(recentDays);
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		}
	}

	onMount(loadAll);

	// ── Derived ───────────────────────────────────────────────────────────
	let withSnapshots = $derived(
		(accounts ?? []).filter((a) => a.as_of !== null)
	);
	let awaitingSnapshots = $derived(
		(accounts ?? []).filter((a) => a.as_of === null)
	);
	let realizedYears = $derived(
		realizedGl
			? Object.keys(realizedGl.by_year)
					.map(Number)
					.sort((a, b) => b - a)
			: []
	);

	let hasIntegrityWarnings = $derived(
		dataIntegrity !== null &&
			(dataIntegrity.orphan_transactions > 0 ||
				dataIntegrity.orphan_snapshots > 0 ||
				dataIntegrity.stale_snapshot_accounts > 0 ||
				dataIntegrity.suspect_symbols > 0 ||
				dataIntegrity.duplicate_position_groups > 0 ||
				dataIntegrity.duplicate_transaction_groups > 0)
	);

	// ── Sort / filter / search helpers ────────────────────────────────────
	function compareValues(a: unknown, b: unknown): number {
		// Nulls/undefineds sort last regardless of direction
		const aNull = a === null || a === undefined;
		const bNull = b === null || b === undefined;
		if (aNull && bNull) return 0;
		if (aNull) return 1;
		if (bNull) return -1;
		if (typeof a === 'number' && typeof b === 'number') return a - b;
		return String(a).localeCompare(String(b), undefined, { numeric: true });
	}

	function applySort<T>(rows: T[], key: string | null, dir: SortDir): T[] {
		if (!key || !dir) return rows;
		const sorted = [...rows].sort((a, b) =>
			compareValues((a as Record<string, unknown>)[key], (b as Record<string, unknown>)[key])
		);
		return dir === 'desc' ? sorted.reverse() : sorted;
	}

	function matchesQuery(haystack: (string | null | undefined)[], needle: string): boolean {
		if (!needle) return true;
		const q = needle.toLowerCase();
		return haystack.some((h) => h !== null && h !== undefined && h.toLowerCase().includes(q));
	}

	function nextSortDir(current: SortDir): SortDir {
		// asc → desc → null → asc
		if (current === 'asc') return 'desc';
		if (current === 'desc') return null;
		return 'asc';
	}

	function toggleSort(
		key: string,
		currentKey: string | null,
		currentDir: SortDir
	): { key: string | null; dir: SortDir } {
		if (currentKey !== key) return { key, dir: 'asc' };
		const next = nextSortDir(currentDir);
		return { key: next ? key : null, dir: next };
	}

	function toggleSetMember<T>(set: Set<T>, value: T): Set<T> {
		const next = new Set(set);
		if (next.has(value)) next.delete(value);
		else next.add(value);
		return next;
	}

	function sortIndicator(key: string, currentKey: string | null, currentDir: SortDir): string {
		if (currentKey !== key || !currentDir) return '';
		return currentDir === 'asc' ? ' ▲' : ' ▼';
	}

	// ── Filtered + sorted accounts ────────────────────────────────────────
	let acctBrokerOptions = $derived(
		Array.from(new Set(withSnapshots.map((a) => a.broker))).sort()
	);
	let txnBrokerOptions = $derived(
		Array.from(new Set((recentTxns ?? []).map((t) => t.broker))).sort()
	);

	let filteredAccounts = $derived.by(() => {
		const filtered = withSnapshots.filter((a) => {
			if (acctBrokerFilter.size > 0 && !acctBrokerFilter.has(a.broker)) return false;
			return matchesQuery(
				[a.broker, a.account_number_masked, a.account_name, a.account_type, a.entity],
				acctQuery
			);
		});
		return applySort(filtered, acctSortKey, acctSortDir);
	});

	let acctVisibleTotal = $derived(
		filteredAccounts.reduce((sum, a) => sum + (a.market_value ?? 0), 0)
	);
	let acctAllTotal = $derived(
		withSnapshots.reduce((sum, a) => sum + (a.market_value ?? 0), 0)
	);
	let acctIsFiltered = $derived(
		acctQuery.trim() !== '' || acctBrokerFilter.size > 0
	);

	let filteredHoldings = $derived.by(() => {
		const filtered = (topHoldings ?? []).filter((h) => {
			if (holdCashFilter === 'cash' && !h.is_cash_sleeve) return false;
			if (holdCashFilter === 'non-cash' && h.is_cash_sleeve) return false;
			return matchesQuery([h.symbol, h.description], holdQuery);
		});
		return applySort(filtered, holdSortKey, holdSortDir);
	});

	let filteredTxns = $derived.by(() => {
		const filtered = (recentTxns ?? []).filter((t) => {
			if (txnBrokerFilter.size > 0 && !txnBrokerFilter.has(t.broker)) return false;
			return matchesQuery(
				[t.symbol, t.action, t.account_number_masked, t.broker],
				txnQuery
			);
		});
		return applySort(filtered, txnSortKey, txnSortDir);
	});
</script>

<svelte:head>
	<title>Brokerage · Accounting</title>
</svelte:head>

<div class="page">
	<header class="header">
		<h1>Brokerage</h1>
		<p class="sub">Phase 1 ingest visibility — net worth, holdings, transactions, realized G/L.</p>
	</header>

	{#if loading && !netWorth}
		<div class="state">Loading…</div>
	{:else if error}
		<div class="state error">⚠ {error}</div>
	{:else if netWorth}
		<!-- ── Net Worth ──────────────────────────────────────────────── -->
		<section class="section networth">
			<div class="headline">
				<div class="label">Net Worth</div>
				{#if acctIsFiltered}
					<div class="value">{fmtCurrency(acctVisibleTotal)}</div>
					<div class="meta filtered-meta">
						Visible: {filteredAccounts.length} of {withSnapshots.length} accounts
						<span class="separator">·</span>
						All accounts: <strong>{fmtCurrency(acctAllTotal)}</strong>
					</div>
				{:else}
					<div class="value">{fmtCurrency(netWorth.total)}</div>
					{#if netWorth.as_of_min && netWorth.as_of_max}
						<div class="meta">
							{netWorth.as_of_min} … {netWorth.as_of_max}
						</div>
					{/if}
				{/if}
			</div>

			<div class="brokers">
				{#each Object.entries(netWorth.by_broker) as [broker, val] (broker)}
					<div class="broker-card">
						<div class="broker-name">{broker}</div>
						<div class="broker-value">{fmtCurrency(val)}</div>
					</div>
				{/each}
			</div>

			{#if netWorth.plan_wrapper_excluded_count > 0 || netWorth.zero_snapshot_account_count > 0}
				<div class="caveats">
					{#if netWorth.plan_wrapper_excluded_count > 0}
						<span>
							{netWorth.plan_wrapper_excluded_count} plan-wrapper account(s) excluded — held positions are in their child BrokerageLink accounts.
						</span>
					{/if}
					{#if netWorth.zero_snapshot_account_count > 0}
						<span>
							{netWorth.zero_snapshot_account_count} account(s) have no snapshot data yet.
						</span>
					{/if}
				</div>
			{/if}
		</section>

		<!-- ── Accounts ──────────────────────────────────────────────── -->
		<section class="section">
			<div class="section-head">
				<h2>Accounts</h2>
				<div class="table-controls">
					<input
						type="search"
						class="search-input"
						placeholder="Search accounts…"
						bind:value={acctQuery}
					/>
				</div>
			</div>

			{#if acctBrokerOptions.length > 1}
				<div class="filter-chips">
					<span class="chip-label">Broker:</span>
					{#each acctBrokerOptions as broker (broker)}
						<button
							type="button"
							class="chip"
							class:active={acctBrokerFilter.has(broker)}
							onclick={() => (acctBrokerFilter = toggleSetMember(acctBrokerFilter, broker))}
						>
							{broker}
						</button>
					{/each}
					{#if acctBrokerFilter.size > 0}
						<button
							type="button"
							class="chip clear"
							onclick={() => (acctBrokerFilter = new Set())}
						>
							Clear
						</button>
					{/if}
				</div>
			{/if}

			<table class="data-table">
				<thead>
					<tr>
						{#each [{ key: 'broker', label: 'Broker' }, { key: 'account_number_masked', label: 'Account' }, { key: 'account_type', label: 'Type' }, { key: 'entity', label: 'Entity' }, { key: 'tax_sheltered', label: 'Tax-sheltered' }, { key: 'as_of', label: 'As of' }] as col (col.key)}
							<th class="sortable" onclick={() => {
								const r = toggleSort(col.key, acctSortKey, acctSortDir);
								acctSortKey = r.key;
								acctSortDir = r.dir;
							}}>{col.label}{sortIndicator(col.key, acctSortKey, acctSortDir)}</th>
						{/each}
						<th class="num sortable" onclick={() => {
							const r = toggleSort('market_value', acctSortKey, acctSortDir);
							acctSortKey = r.key;
							acctSortDir = r.dir;
						}}>Market value{sortIndicator('market_value', acctSortKey, acctSortDir)}</th>
					</tr>
				</thead>
				<tbody>
					{#each filteredAccounts as a (a.account_id)}
						<tr class:wrapper={a.is_plan_wrapper}>
							<td>{a.broker}</td>
							<td>
								{a.account_number_masked}
								{#if a.account_name}
									<span class="muted"> · {a.account_name}</span>
								{/if}
								{#if a.is_plan_wrapper}
									<span class="badge">wrapper</span>
								{/if}
							</td>
							<td>{a.account_type}</td>
							<td>{a.entity}</td>
							<td>{a.tax_sheltered ? 'Yes' : 'No'}</td>
							<td>{a.as_of}</td>
							<td class="num">{fmtCurrency(a.market_value)}</td>
						</tr>
					{/each}
					{#if filteredAccounts.length === 0}
						<tr><td colspan="7" class="muted empty">No accounts match the current filter.</td></tr>
					{/if}
				</tbody>
			</table>

			{#if awaitingSnapshots.length > 0}
				<div class="awaiting">
					<h3>Awaiting Snapshot Data</h3>
					<ul>
						{#each awaitingSnapshots as a (a.account_id)}
							<li>{a.broker} {a.account_number_masked} ({a.account_type}, {a.entity})</li>
						{/each}
					</ul>
				</div>
			{/if}
		</section>

		<!-- ── Top Holdings ──────────────────────────────────────────── -->
		<section class="section">
			<div class="section-head">
				<h2>Top Holdings</h2>
				<div class="table-controls">
					<input
						type="search"
						class="search-input"
						placeholder="Search holdings…"
						bind:value={holdQuery}
					/>
					<label class="control">
						Show top
						<select bind:value={topN} onchange={reloadHoldings}>
							<option value={5}>5</option>
							<option value={10}>10</option>
							<option value={25}>25</option>
							<option value={50}>50</option>
						</select>
					</label>
				</div>
			</div>

			<div class="filter-chips">
				<span class="chip-label">Type:</span>
				<button type="button" class="chip" class:active={holdCashFilter === 'all'} onclick={() => (holdCashFilter = 'all')}>All</button>
				<button type="button" class="chip" class:active={holdCashFilter === 'non-cash'} onclick={() => (holdCashFilter = 'non-cash')}>Non-cash</button>
				<button type="button" class="chip" class:active={holdCashFilter === 'cash'} onclick={() => (holdCashFilter = 'cash')}>Cash sleeves</button>
			</div>

			<table class="data-table">
				<thead>
					<tr>
						<th class="sortable" onclick={() => {
							const r = toggleSort('symbol', holdSortKey, holdSortDir);
							holdSortKey = r.key;
							holdSortDir = r.dir;
						}}>Symbol / Description{sortIndicator('symbol', holdSortKey, holdSortDir)}</th>
						<th class="num sortable" onclick={() => {
							const r = toggleSort('total_quantity', holdSortKey, holdSortDir);
							holdSortKey = r.key;
							holdSortDir = r.dir;
						}}>Quantity{sortIndicator('total_quantity', holdSortKey, holdSortDir)}</th>
						<th class="num sortable" onclick={() => {
							const r = toggleSort('total_market_value', holdSortKey, holdSortDir);
							holdSortKey = r.key;
							holdSortDir = r.dir;
						}}>Market value{sortIndicator('total_market_value', holdSortKey, holdSortDir)}</th>
						<th class="num sortable" onclick={() => {
							const r = toggleSort('pct_of_net_worth', holdSortKey, holdSortDir);
							holdSortKey = r.key;
							holdSortDir = r.dir;
						}}>% of net worth{sortIndicator('pct_of_net_worth', holdSortKey, holdSortDir)}</th>
						<th class="num sortable" onclick={() => {
							const r = toggleSort('account_count', holdSortKey, holdSortDir);
							holdSortKey = r.key;
							holdSortDir = r.dir;
						}}># accounts{sortIndicator('account_count', holdSortKey, holdSortDir)}</th>
					</tr>
				</thead>
				<tbody>
					{#each filteredHoldings as h, i (h.symbol ?? h.description ?? `idx-${i}`)}
						<tr class:cash={h.is_cash_sleeve}>
							<td>
								{#if h.is_cash_sleeve}
									<strong>Cash</strong>
								{:else if h.symbol}
									<strong>{h.symbol}</strong>
									{#if h.description}<span class="muted"> · {h.description}</span>{/if}
								{:else}
									<span class="muted">{h.description ?? '(unknown)'}</span>
								{/if}
							</td>
							<td class="num">{fmtQty(h.total_quantity)}</td>
							<td class="num">{fmtCurrency(h.total_market_value)}</td>
							<td class="num">{fmtPct(h.pct_of_net_worth)}</td>
							<td class="num">{h.account_count}</td>
						</tr>
					{/each}
					{#if filteredHoldings.length === 0}
						<tr><td colspan="5" class="muted empty">No holdings match the current filter.</td></tr>
					{/if}
				</tbody>
			</table>
		</section>

		<!-- ── Recent Transactions ───────────────────────────────────── -->
		<section class="section">
			<div class="section-head">
				<h2>
					<button class="toggle" onclick={() => (showRecent = !showRecent)} aria-expanded={showRecent}>
						{showRecent ? '▾' : '▸'} Recent Transactions
					</button>
				</h2>
				{#if showRecent}
					<label class="control">
						Days
						<select bind:value={recentDays} onchange={reloadRecent}>
							<option value={7}>7</option>
							<option value={14}>14</option>
							<option value={30}>30</option>
							<option value={90}>90</option>
						</select>
					</label>
				{/if}
			</div>
			{#if showRecent}
				{#if (recentTxns?.length ?? 0) === 0}
					<p class="muted">None in the selected window.</p>
				{:else}
					<div class="table-controls inline">
						<input
							type="search"
							class="search-input"
							placeholder="Search transactions…"
							bind:value={txnQuery}
						/>
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
									{broker}
								</button>
							{/each}
							{#if txnBrokerFilter.size > 0}
								<button
									type="button"
									class="chip clear"
									onclick={() => (txnBrokerFilter = new Set())}
								>
									Clear
								</button>
							{/if}
						</div>
					{/if}
					<table class="data-table compact">
						<thead>
							<tr>
								{#each [{ key: 'trade_date', label: 'Date' }, { key: 'broker', label: 'Broker' }, { key: 'account_number_masked', label: 'Account' }, { key: 'action', label: 'Action' }, { key: 'symbol', label: 'Symbol' }] as col (col.key)}
									<th class="sortable" onclick={() => {
										const r = toggleSort(col.key, txnSortKey, txnSortDir);
										txnSortKey = r.key;
										txnSortDir = r.dir;
									}}>{col.label}{sortIndicator(col.key, txnSortKey, txnSortDir)}</th>
								{/each}
								<th class="num sortable" onclick={() => {
									const r = toggleSort('quantity', txnSortKey, txnSortDir);
									txnSortKey = r.key;
									txnSortDir = r.dir;
								}}>Qty{sortIndicator('quantity', txnSortKey, txnSortDir)}</th>
								<th class="num sortable" onclick={() => {
									const r = toggleSort('amount', txnSortKey, txnSortDir);
									txnSortKey = r.key;
									txnSortDir = r.dir;
								}}>Amount{sortIndicator('amount', txnSortKey, txnSortDir)}</th>
							</tr>
						</thead>
						<tbody>
							{#each filteredTxns as t, i (`${t.trade_date}-${t.account_number_masked}-${t.action}-${i}`)}
								<tr>
									<td>{t.trade_date}</td>
									<td>{t.broker}</td>
									<td>{t.account_number_masked}</td>
									<td>{t.action}</td>
									<td>{t.symbol ?? ''}</td>
									<td class="num">{fmtQty(t.quantity)}</td>
									<td class="num {amountClass(t.amount)}">{fmtCurrency(t.amount)}</td>
								</tr>
							{/each}
							{#if filteredTxns.length === 0}
								<tr><td colspan="7" class="muted empty">No transactions match the current filter.</td></tr>
							{/if}
						</tbody>
					</table>
				{/if}
			{/if}
		</section>

		<!-- ── Realized G/L ─────────────────────────────────────────── -->
		<section class="section">
			<h2>
				<button
					class="toggle"
					onclick={() => (showRealizedGL = !showRealizedGL)}
					aria-expanded={showRealizedGL}
				>
					{showRealizedGL ? '▾' : '▸'} Realized G/L by Year
				</button>
			</h2>
			{#if showRealizedGL && realizedGl}
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
				<div class="footer-note">
					{#if realizedGl.wash_sales.lots === 0}
						<span class="muted"
							>No wash sales in ingested data (1099-B substantiation not yet ingested).</span
						>
					{:else}
						<strong>Wash sales:</strong>
						{realizedGl.wash_sales.lots} lots, total disallowed loss
						{fmtCurrency(realizedGl.wash_sales.total_disallowed_loss)}
					{/if}
				</div>
			{/if}
		</section>

		<!-- ── Data Integrity ───────────────────────────────────────── -->
		<section class="section integrity" class:has-warnings={hasIntegrityWarnings}>
			<h2>
				<button
					class="toggle"
					onclick={() => (showIntegrity = !showIntegrity)}
					aria-expanded={showIntegrity}
				>
					{showIntegrity ? '▾' : '▸'} Data Integrity
					{#if hasIntegrityWarnings}<span class="warn-dot" aria-label="warnings present">⚠</span>{/if}
				</button>
			</h2>
			{#if showIntegrity && dataIntegrity}
				<dl class="grid">
					<dt>Accounts</dt><dd>{dataIntegrity.accounts}</dd>
					<dt>Transactions</dt><dd>{dataIntegrity.transactions}</dd>
					<dt>Position snapshots</dt><dd>{dataIntegrity.position_snapshots}</dd>
					<dt>Realized lots</dt><dd>{dataIntegrity.realized_lots}</dd>
				</dl>
				{#if hasIntegrityWarnings}
					<ul class="warnings">
						{#if dataIntegrity.orphan_transactions > 0}
							<li>⚠ {dataIntegrity.orphan_transactions} orphan transaction(s)</li>
						{/if}
						{#if dataIntegrity.orphan_snapshots > 0}
							<li>⚠ {dataIntegrity.orphan_snapshots} orphan snapshot(s)</li>
						{/if}
						{#if dataIntegrity.stale_snapshot_accounts > 0}
							<li>⚠ {dataIntegrity.stale_snapshot_accounts} account(s) with stale snapshot data</li>
						{/if}
						{#if dataIntegrity.suspect_symbols > 0}
							<li>⚠ {dataIntegrity.suspect_symbols} suspect symbol row(s) (adapter-bug indicator)</li>
						{/if}
						{#if dataIntegrity.duplicate_position_groups > 0}
							<li>
								⚠ {dataIntegrity.duplicate_position_groups} duplicate position group(s) (adapter-bug
								indicator)
							</li>
						{/if}
						{#if dataIntegrity.duplicate_transaction_groups > 0}
							<li>
								⚠ {dataIntegrity.duplicate_transaction_groups} duplicate transaction group(s) (adapter-bug
								indicator)
							</li>
						{/if}
					</ul>
				{:else}
					<p class="muted">No data integrity warnings.</p>
				{/if}
			{/if}
		</section>
	{/if}
</div>

<style>
	.page {
		max-width: 1200px;
		margin: 0 auto;
		padding: 24px;
		font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Helvetica Neue', sans-serif;
		color: #1d1d1f;
	}

	.header h1 {
		font-size: 32px;
		font-weight: 600;
		margin: 0 0 4px 0;
	}
	.header .sub {
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
		background: #ffffff;
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

	.section-head {
		display: flex;
		justify-content: space-between;
		align-items: center;
		margin-bottom: 12px;
	}
	.section-head h2 {
		margin: 0;
	}

	.toggle {
		background: none;
		border: none;
		padding: 0;
		font: inherit;
		cursor: pointer;
		color: inherit;
		text-align: left;
	}

	.warn-dot {
		color: #ff9500;
		margin-left: 6px;
	}

	.networth .headline {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		margin-bottom: 16px;
	}
	.networth .label {
		font-size: 12px;
		text-transform: uppercase;
		color: #6e6e73;
		letter-spacing: 0.04em;
	}
	.networth .value {
		font-size: 40px;
		font-weight: 600;
		font-feature-settings: 'tnum' 1;
		margin-top: 4px;
	}
	.networth .meta {
		color: #6e6e73;
		font-size: 12px;
	}

	.brokers {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
		gap: 12px;
	}
	.broker-card {
		background: #f5f5f7;
		border-radius: 8px;
		padding: 12px 14px;
	}
	.broker-name {
		font-size: 12px;
		text-transform: capitalize;
		color: #6e6e73;
	}
	.broker-value {
		font-size: 20px;
		font-weight: 600;
		font-feature-settings: 'tnum' 1;
		margin-top: 2px;
	}

	.caveats {
		display: flex;
		flex-direction: column;
		gap: 4px;
		margin-top: 12px;
		font-size: 13px;
		color: #6e6e73;
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
	.data-table tr.wrapper {
		background: #fafafc;
	}
	.data-table tr.cash {
		background: #f9f9fb;
	}
	.data-table .num {
		text-align: right;
		font-variant-numeric: tabular-nums;
	}

	.data-table.compact tbody td {
		padding: 4px 8px;
	}

	.muted {
		color: #6e6e73;
	}
	.pos {
		color: #047a04;
	}
	.neg {
		color: #d70015;
	}

	.badge {
		display: inline-block;
		font-size: 11px;
		background: #fff5e6;
		color: #b35900;
		padding: 1px 6px;
		border-radius: 4px;
		margin-left: 6px;
	}

	.awaiting {
		margin-top: 16px;
		padding-top: 12px;
		border-top: 1px dashed #e5e5e7;
	}
	.awaiting h3 {
		font-size: 13px;
		font-weight: 600;
		margin: 0 0 6px;
		color: #6e6e73;
	}
	.awaiting ul {
		margin: 0;
		padding-left: 20px;
		font-size: 13px;
		color: #6e6e73;
	}

	.footer-note {
		margin-top: 8px;
		font-size: 13px;
	}

	.control {
		font-size: 13px;
		color: #6e6e73;
	}
	.control select {
		font: inherit;
		padding: 2px 6px;
		border-radius: 6px;
		border: 1px solid #d2d2d7;
		background: #fff;
		margin-left: 4px;
	}

	.integrity .grid {
		display: grid;
		grid-template-columns: max-content 1fr;
		gap: 4px 16px;
		margin: 0;
		font-size: 14px;
	}
	.integrity dt {
		color: #6e6e73;
	}
	.integrity dd {
		margin: 0;
		font-feature-settings: 'tnum' 1;
	}

	.warnings {
		margin: 12px 0 0;
		padding-left: 20px;
		color: #b35900;
		font-size: 13px;
	}

	/* Table controls (search + dropdowns) */
	.table-controls {
		display: flex;
		align-items: center;
		gap: 12px;
	}
	.table-controls.inline {
		margin-bottom: 8px;
	}
	.search-input {
		font: inherit;
		font-size: 13px;
		padding: 4px 10px;
		border: 1px solid #d2d2d7;
		border-radius: 6px;
		background: #fff;
		min-width: 200px;
	}
	.search-input:focus {
		outline: none;
		border-color: #007aff;
		box-shadow: 0 0 0 3px rgba(0, 122, 255, 0.15);
	}

	/* Filter chips */
	.filter-chips {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
		align-items: center;
		margin: 4px 0 12px;
	}
	.chip-label {
		font-size: 12px;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: #6e6e73;
		margin-right: 4px;
	}
	.chip {
		font: inherit;
		font-size: 12px;
		padding: 3px 10px;
		border: 1px solid #d2d2d7;
		border-radius: 999px;
		background: #fff;
		color: #1d1d1f;
		cursor: pointer;
		transition: background 0.1s, border-color 0.1s;
	}
	.chip:hover {
		background: #f5f5f7;
	}
	.chip.active {
		background: #007aff;
		border-color: #007aff;
		color: #fff;
	}
	.chip.clear {
		color: #d70015;
		border-color: #f0c0c4;
	}

	/* Sortable column headers */
	.data-table thead th.sortable {
		cursor: pointer;
		user-select: none;
	}
	.data-table thead th.sortable:hover {
		color: #1d1d1f;
	}
	.data-table tbody td.empty {
		text-align: center;
		padding: 16px;
		font-style: italic;
	}

	/* Filtered headline meta */
	.networth .meta.filtered-meta {
		font-size: 13px;
		color: #6e6e73;
		margin-top: 6px;
	}
	.networth .meta.filtered-meta strong {
		color: #1d1d1f;
		font-feature-settings: 'tnum' 1;
	}
	.networth .meta .separator {
		margin: 0 8px;
		color: #c7c7cc;
	}
</style>
