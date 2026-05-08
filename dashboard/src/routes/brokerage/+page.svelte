<script lang="ts">
	import { onMount } from 'svelte';
	import {
		fetchBrokerageNetWorth,
		fetchBrokerageAccounts,
		fetchBrokerageTopHoldings,
		fetchBrokerageRecentTransactions,
		fetchBrokerageRealizedGL,
		fetchBrokerageDataIntegrity,
		fetchBrokerageNetWorthHistory,
		fetchBrokerageBenchmarkComparison,
		fetchBrokerageMissingAccounts,
		updateBrokerageAccountTags
	} from '$lib/api';
	import type {
		BrokerageNetWorth,
		BrokerageAccount,
		BrokerageHolding,
		BrokerageRecentTransaction,
		BrokerageRealizedGL,
		BrokerageDataIntegrity,
		BrokerageNetWorthHistoryPoint,
		BrokerageBenchmarkComparison,
		BrokerageMissingAccount
	} from '$lib/api';

	// ── State ─────────────────────────────────────────────────────────────
	let netWorth = $state<BrokerageNetWorth | null>(null);
	let accounts = $state<BrokerageAccount[] | null>(null);
	let topHoldings = $state<BrokerageHolding[] | null>(null);
	let recentTxns = $state<BrokerageRecentTransaction[] | null>(null);
	let realizedGl = $state<BrokerageRealizedGL | null>(null);
	let dataIntegrity = $state<BrokerageDataIntegrity | null>(null);
	let networthHistory = $state<BrokerageNetWorthHistoryPoint[] | null>(null);
	let hoverHistoryIdx = $state<number | null>(null);
	let benchmarkOn = $state(false);
	let benchmarkData = $state<BrokerageBenchmarkComparison | null>(null);
	let missingAccounts = $state<BrokerageMissingAccount[] | null>(null);

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
	// Tag filter state: tags whose presence is required (include) and tags
	// whose presence excludes (exclude). UI uses three states per chip:
	// neutral → include → exclude → neutral.
	let acctTagInclude = $state<Set<string>>(new Set());
	let acctTagExclude = $state<Set<string>>(new Set());
	// Editing state: which account row's tag chips are being edited.
	let editingTagsAccountId = $state<string | null>(null);
	let tagDraftInput = $state('');

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
			const [nw, accts, holdings, txns, gl, integrity, history, missing] = await Promise.all([
				fetchBrokerageNetWorth(),
				fetchBrokerageAccounts(),
				fetchBrokerageTopHoldings(topN),
				fetchBrokerageRecentTransactions(recentDays),
				fetchBrokerageRealizedGL(),
				fetchBrokerageDataIntegrity(),
				fetchBrokerageNetWorthHistory(),
				fetchBrokerageMissingAccounts()
			]);
			netWorth = nw;
			accounts = accts;
			topHoldings = holdings;
			recentTxns = txns;
			realizedGl = gl;
			dataIntegrity = integrity;
			networthHistory = history;
			missingAccounts = missing;
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

	async function toggleBenchmark() {
		benchmarkOn = !benchmarkOn;
		if (benchmarkOn && !benchmarkData) {
			try {
				benchmarkData = await fetchBrokerageBenchmarkComparison('SPY');
			} catch (e) {
				error = e instanceof Error ? e.message : String(e);
				benchmarkOn = false;
			}
		}
	}

	// Refetch the history series when the account filter changes. We pass the
	// visible account_ids (those passing every active filter) to the API so
	// the chart matches whatever the headline is summing.
	let _lastFilterKey = '';
	$effect(() => {
		const ids = filteredAccounts.map((a) => a.account_id).sort();
		const key = `${acctIsFiltered}|${ids.join(',')}`;
		if (key === _lastFilterKey) return;
		_lastFilterKey = key;
		if (!accounts) return; // initial load handled by loadAll()
		void refetchFilteredHistory(ids);
	});

	async function refetchFilteredHistory(ids: string[]): Promise<void> {
		try {
			if (!acctIsFiltered) {
				networthHistory = await fetchBrokerageNetWorthHistory({});
				return;
			}
			if (ids.length === 0) {
				networthHistory = []; // empty filter → chart will render at $0
				return;
			}
			// Route through the typed api.ts helper so we inherit its X-Api-Key
			// forwarding, Content-Type defaults, and in-flight-dedup abort logic.
			networthHistory = await fetchBrokerageNetWorthHistory({ accountIds: ids });
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		}
	}

	onMount(loadAll);

	// ── Derived ───────────────────────────────────────────────────────────
	// Plan-wrapper accounts (e.g. Fidelity Microsoft 401K PLAN) are logical
	// containers whose positions duplicate their BrokerageLink child. The DB
	// retains them for structural relationships and audit, but the dashboard
	// hides them so the accounts table does not show two rows for the same
	// underlying value. Net-worth math already excludes wrappers via
	// compute_net_worth's is_plan_wrapper guard.
	let withSnapshots = $derived(
		(accounts ?? []).filter((a) => a.as_of !== null && !a.is_plan_wrapper)
	);
	let awaitingSnapshots = $derived(
		(accounts ?? []).filter((a) => a.as_of === null && !a.is_plan_wrapper)
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
			const tags = new Set(a.tags ?? []);
			if (acctTagInclude.size > 0) {
				for (const need of acctTagInclude) {
					if (!tags.has(need)) return false;
				}
			}
			if (acctTagExclude.size > 0) {
				for (const block of acctTagExclude) {
					if (tags.has(block)) return false;
				}
			}
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
		acctQuery.trim() !== '' ||
			acctBrokerFilter.size > 0 ||
			acctTagInclude.size > 0 ||
			acctTagExclude.size > 0
	);

	// Tag universe: distinct tags from all accounts, sorted alphabetically.
	let allTags = $derived.by(() => {
		const t = new Set<string>();
		for (const a of withSnapshots) for (const tag of a.tags ?? []) t.add(tag);
		return Array.from(t).sort();
	});

	// Three-state cycle: neutral → include → exclude → neutral.
	//
	// Manual repro for this code path (no e2e harness exists in dashboard/):
	//   1. Open https://macbook.ancon-cliff.ts.net/brokerage
	//   2. Click any tag chip in the Accounts filter strip:
	//        a. First click → green "+ <tag>" (include)
	//        b. Second click → red "− <tag>" (exclude)
	//        c. Third click → neutral (no class, no symbol)
	//   3. Repeat — cycle should be stable.
	//
	// The reassign pattern below is intentional: Svelte 5 tracks reads on
	// the binding identity, so swapping in a fresh Set instance is the most
	// reliable way to trigger a re-render across all consumers (the chart
	// derived state, the filteredAccounts derived, tagState in the {#each}
	// loop). Direct `.add()/.delete()` mutations on the $state Set are NOT
	// reliably reactive in this version of Svelte for non-SvelteSet plain
	// Sets, so we keep the copy-and-reassign pattern.
	function cycleTag(tag: string): void {
		if (acctTagInclude.has(tag)) {
			const next = new Set(acctTagInclude);
			next.delete(tag);
			acctTagInclude = next;
			const ex = new Set(acctTagExclude);
			ex.add(tag);
			acctTagExclude = ex;
		} else if (acctTagExclude.has(tag)) {
			const next = new Set(acctTagExclude);
			next.delete(tag);
			acctTagExclude = next;
		} else {
			const inc = new Set(acctTagInclude);
			inc.add(tag);
			acctTagInclude = inc;
		}
	}

	function tagState(tag: string): 'include' | 'exclude' | 'neutral' {
		if (acctTagInclude.has(tag)) return 'include';
		if (acctTagExclude.has(tag)) return 'exclude';
		return 'neutral';
	}

	let filteredHoldings = $derived.by(() => {
		const filtered = (topHoldings ?? []).filter((h) => {
			if (holdCashFilter === 'cash' && !h.is_cash_sleeve) return false;
			if (holdCashFilter === 'non-cash' && h.is_cash_sleeve) return false;
			return matchesQuery([h.symbol, h.description], holdQuery);
		});
		return applySort(filtered, holdSortKey, holdSortDir);
	});

	// ── Net-worth history chart geometry ──────────────────────────────────
	const CHART_W = 720;
	const CHART_H = 200;
	const CHART_PAD_X = 40;
	const CHART_PAD_Y = 16;

	// Today's value to anchor the chart's right edge: prefer the filtered
	// visible total (so the chart matches the headline) when a filter is on,
	// otherwise the live whole-portfolio net worth from PositionSnapshot.
	let chartLiveValue = $derived(
		acctIsFiltered ? acctVisibleTotal : (netWorth?.total ?? 0)
	);

	function formatAxisDollars(v: number): string {
		if (Math.abs(v) >= 1e6) return `$${(v / 1e6).toFixed(1)}M`;
		if (Math.abs(v) >= 1e3) return `$${(v / 1e3).toFixed(0)}k`;
		return `$${v.toFixed(0)}`;
	}

	function pickAxisTicks(min: number, max: number, count: number): number[] {
		if (max <= min) return [min];
		const range = max - min;
		const step = range / (count - 1);
		return Array.from({ length: count }, (_, i) => min + step * i);
	}

	function pickDateTicks(points: { as_of: string }[], count: number): { idx: number; label: string }[] {
		if (points.length === 0) return [];
		const indices = Array.from({ length: count }, (_, i) =>
			Math.round((i / (count - 1)) * (points.length - 1))
		);
		return indices.map((idx) => {
			const d = new Date(points[idx].as_of);
			return { idx, label: `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}` };
		});
	}

	let historyChart = $derived.by(() => {
		// Empty-filter: show a flat $0 line so the user sees "no data matches"
		// without losing the chart frame entirely.
		if (acctIsFiltered && filteredAccounts.length === 0) {
			return {
				points: [],
				xs: [],
				ys: [],
				minV: 0,
				maxV: 0,
				linePath: `M ${CHART_PAD_X} ${CHART_H - CHART_PAD_Y} L ${CHART_W - CHART_PAD_X} ${CHART_H - CHART_PAD_Y}`,
				areaPath: '',
				benchPath: null,
				todayIdx: null,
				first: null,
				last: null,
				deltaPct: 0,
				dateTicks: [],
				dollarTicks: [0],
				yFor: () => CHART_H - CHART_PAD_Y,
				empty: true
			};
		}

		// Append a synthetic 'today' point using the live aggregate so the
		// chart's right edge matches the headline. Only append when we have
		// an actual live value AND the historical series doesn't already
		// include today.
		const baseHistory = networthHistory ?? [];
		const todayIso = new Date().toISOString().slice(0, 10);
		const lastHistoricalIso = baseHistory[baseHistory.length - 1]?.as_of;
		const points = [...baseHistory];
		let todayIdx: number | null = null;
		if (chartLiveValue > 0 && lastHistoricalIso !== todayIso) {
			points.push({
				as_of: todayIso,
				balance_total: chartLiveValue,
				account_count: filteredAccounts.length || (accounts ?? []).length
			});
			todayIdx = points.length - 1;
		}

		if (points.length < 2) return null;

		const values = points.map((p) => p.balance_total);
		let benchValues: (number | null)[] = [];
		if (benchmarkOn && benchmarkData?.series.length) {
			// Align benchmark to the historical (non-today) points only; the
			// today point gets benchmark=null since SPY isn't in the bench
			// series for today.
			benchValues = points.map((p) => {
				const match = benchmarkData!.series.find((b) => b.as_of === p.as_of);
				return match ? match.benchmark_value : null;
			});
		}
		const allNonNull = [
			...values,
			...benchValues.filter((v): v is number => v !== null)
		];
		const minV = Math.min(...allNonNull);
		const maxV = Math.max(...allNonNull);
		const range = maxV - minV || 1;
		const innerW = CHART_W - CHART_PAD_X * 2;
		const innerH = CHART_H - CHART_PAD_Y * 2;
		const xs = points.map((_, i) =>
			points.length === 1 ? CHART_PAD_X + innerW / 2 : CHART_PAD_X + (i / (points.length - 1)) * innerW
		);
		const yFor = (v: number) => CHART_PAD_Y + innerH - ((v - minV) / range) * innerH;
		const ys = values.map(yFor);
		const linePath = xs.map((x, i) => `${i === 0 ? 'M' : 'L'} ${x.toFixed(1)} ${ys[i].toFixed(1)}`).join(' ');
		const areaPath = `${linePath} L ${xs[xs.length - 1].toFixed(1)} ${(CHART_PAD_Y + innerH).toFixed(1)} L ${xs[0].toFixed(1)} ${(CHART_PAD_Y + innerH).toFixed(1)} Z`;

		let benchPath: string | null = null;
		if (benchValues.some((v) => v !== null)) {
			const segments: string[] = [];
			let pen = 'M';
			benchValues.forEach((v, i) => {
				if (v === null) {
					pen = 'M';
					return;
				}
				segments.push(`${pen} ${xs[i].toFixed(1)} ${yFor(v).toFixed(1)}`);
				pen = 'L';
			});
			benchPath = segments.join(' ') || null;
		}

		return {
			points,
			xs,
			ys,
			minV,
			maxV,
			linePath,
			areaPath,
			benchPath,
			todayIdx,
			first: points[0],
			last: points[points.length - 1],
			deltaPct: (points[points.length - 1].balance_total - points[0].balance_total) / points[0].balance_total,
			dateTicks: pickDateTicks(points, 5),
			dollarTicks: pickAxisTicks(minV, maxV, 5),
			yFor,
			empty: false
		};
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

		<!-- ── Net-worth history ─────────────────────────────────────── -->
		{#if historyChart}
			<section class="section">
				<div class="section-head">
					<h2>Net Worth Over Time</h2>
					<div class="history-summary">
						{#if historyChart.first && historyChart.last}
							<span class="muted">{historyChart.first.as_of} → {historyChart.last.as_of}</span>
							<span class="separator">·</span>
							<span class={historyChart.deltaPct >= 0 ? 'pos' : 'neg'}>
								{historyChart.deltaPct >= 0 ? '+' : ''}{(historyChart.deltaPct * 100).toFixed(1)}%
							</span>
						{:else}
							<span class="muted">No accounts match the current filter</span>
						{/if}
						{#if benchmarkOn && benchmarkData?.benchmark_pct !== null && benchmarkData?.benchmark_pct !== undefined}
							<span class="separator">·</span>
							<span class="muted">SPY:</span>
							<span class={benchmarkData.benchmark_pct >= 0 ? 'pos' : 'neg'}>
								{benchmarkData.benchmark_pct >= 0 ? '+' : ''}{(benchmarkData.benchmark_pct * 100).toFixed(1)}%
							</span>
						{/if}
					</div>
					<button
						type="button"
						class="chip"
						class:active={benchmarkOn}
						onclick={toggleBenchmark}
					>
						{benchmarkOn ? '✓ S&P 500' : 'Compare to S&P 500'}
					</button>
				</div>
				<svg class="history-chart" viewBox={`0 0 ${CHART_W} ${CHART_H + 30}`} preserveAspectRatio="xMidYMid meet" role="img" aria-label="Net worth over time">
					<defs>
						<linearGradient id="history-fill" x1="0" y1="0" x2="0" y2="1">
							<stop offset="0%" stop-color="#007aff" stop-opacity="0.18" />
							<stop offset="100%" stop-color="#007aff" stop-opacity="0" />
						</linearGradient>
					</defs>
					<!-- Y-axis dollar grid lines.
					     Key by index, not value: pickAxisTicks can yield duplicate
					     values when min == max (single-point series, flatlined
					     filter). Duplicate keys throw each_key_duplicate, which
					     halts subsequent reactive updates and is the root cause
					     of the tag-chip click-bug — the filter handler succeeds,
					     but the chart re-derive throws on re-render and Svelte
					     stops processing the batch, so the chip class never
					     updates. -->
					{#each historyChart.dollarTicks as tick, i (i)}
						{@const y = historyChart.yFor(tick)}
						<line x1={CHART_PAD_X} y1={y} x2={CHART_W - CHART_PAD_X} y2={y} stroke="#f0f0f2" stroke-width="1" />
						<text x={CHART_PAD_X - 4} y={y + 3} text-anchor="end" font-size="10" fill="#6e6e73" font-family="-apple-system,Helvetica">{formatAxisDollars(tick)}</text>
					{/each}
					<path d={historyChart.areaPath} fill="url(#history-fill)" />
					<path d={historyChart.linePath} fill="none" stroke="#007aff" stroke-width="2" />
					{#if benchmarkOn && historyChart.benchPath}
						<path d={historyChart.benchPath} fill="none" stroke="#ff9500" stroke-width="2" stroke-dasharray="4 4" />
					{/if}
					{#each historyChart.points as p, i (p.as_of)}
						{@const isToday = historyChart.todayIdx === i}
						<circle
							cx={historyChart.xs[i]}
							cy={historyChart.ys[i]}
							r={hoverHistoryIdx === i ? 6 : isToday ? 5 : 3}
							fill={isToday ? '#047a04' : '#007aff'}
							stroke={isToday ? '#fff' : 'none'}
							stroke-width={isToday ? 1.5 : 0}
							class="history-dot"
							onmouseenter={() => (hoverHistoryIdx = i)}
							onmouseleave={() => (hoverHistoryIdx = null)}
						/>
					{/each}
					{#if hoverHistoryIdx !== null && !historyChart.empty}
						{@const p = historyChart.points[hoverHistoryIdx]}
						{@const cx = historyChart.xs[hoverHistoryIdx]}
						{@const cy = historyChart.ys[hoverHistoryIdx]}
						{@const isToday = historyChart.todayIdx === hoverHistoryIdx}
						<line x1={cx} y1={CHART_PAD_Y} x2={cx} y2={CHART_H - CHART_PAD_Y} stroke="#c7c7cc" stroke-dasharray="2 3" />
						<g transform={`translate(${Math.min(cx + 8, CHART_W - 150)} ${Math.max(cy - 32, 0)})`}>
							<rect x="0" y="0" width="140" height={isToday ? 50 : 40} rx="6" fill="#1d1d1f" />
							<text x="8" y="16" fill="#fff" font-size="11">{p.as_of}{isToday ? ' · Live' : ''}</text>
							<text x="8" y="32" fill="#fff" font-size="13" font-weight="600">{fmtCurrency(p.balance_total)}</text>
							{#if isToday}<text x="8" y="46" fill="#a7d99e" font-size="10">live PositionSnapshot total</text>{/if}
						</g>
					{/if}
					<!-- X-axis date ticks.
					     Key by index: pickDateTicks can yield duplicate `idx`
					     values when points.length is small (Math.round on a
					     short series collapses multiple slots to the same
					     index). Same root cause as dollarTicks above. -->
					{#each historyChart.dateTicks as tick, i (i)}
						{@const x = historyChart.xs[tick.idx] ?? CHART_PAD_X}
						<text x={x} y={CHART_H - 2} text-anchor="middle" font-size="10" fill="#6e6e73" font-family="-apple-system,Helvetica">{tick.label}</text>
					{/each}
				</svg>
			</section>
		{/if}

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

			{#if allTags.length > 0}
				<div class="filter-chips">
					<span class="chip-label">Tags:</span>
					{#each allTags as tag (tag)}
						{@const state = tagState(tag)}
						<button
							type="button"
							class="chip tag-chip"
							class:include={state === 'include'}
							class:exclude={state === 'exclude'}
							onclick={() => cycleTag(tag)}
							title="Click to cycle: include → exclude → off"
						>
							{state === 'exclude' ? '−' : state === 'include' ? '+' : ''} {tag}
						</button>
					{/each}
					{#if acctTagInclude.size + acctTagExclude.size > 0}
						<button
							type="button"
							class="chip clear"
							onclick={() => {
								acctTagInclude = new Set();
								acctTagExclude = new Set();
							}}
						>
							Clear tags
						</button>
					{/if}
				</div>
			{/if}

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
						{#each [{ key: 'broker', label: 'Broker' }, { key: 'account_number_masked', label: 'Account' }, { key: 'account_type', label: 'Type' }] as col (col.key)}
							<th class="sortable" onclick={() => {
								const r = toggleSort(col.key, acctSortKey, acctSortDir);
								acctSortKey = r.key;
								acctSortDir = r.dir;
							}}>{col.label}{sortIndicator(col.key, acctSortKey, acctSortDir)}</th>
						{/each}
						<th>Tags</th>
						{#each [{ key: 'entity', label: 'Entity' }, { key: 'as_of', label: 'As of' }] as col (col.key)}
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
								<a class="account-link" href={`/brokerage/accounts/${a.account_id}`} title="Open account detail">
									{a.account_number_masked}
									{#if a.account_name}
										<span class="muted"> · {a.account_name}</span>
									{/if}
								</a>
								{#if a.is_plan_wrapper}
									<span class="badge">wrapper</span>
								{/if}
							</td>
							<td>{a.account_type}</td>
							<td class="tags-cell">
								{#if editingTagsAccountId === a.account_id}
									<div class="tags-edit">
										{#each a.tags ?? [] as tag (tag)}
											<span class="tag-pill editing">
												{tag}
												<button
													type="button"
													class="tag-remove"
													aria-label={`Remove ${tag}`}
													onclick={async () => {
														const next = (a.tags ?? []).filter((t) => t !== tag);
														try {
															await updateBrokerageAccountTags(a.account_id, next);
															a.tags = next;
														} catch (e) {
															error = e instanceof Error ? e.message : String(e);
														}
													}}
												>×</button>
											</span>
										{/each}
										<input
											type="text"
											class="tag-input"
											bind:value={tagDraftInput}
											placeholder="add tag…"
											onkeydown={async (e) => {
												if (e.key !== 'Enter') return;
												const draft = tagDraftInput.trim().toLowerCase();
												if (!draft) return;
												if ((a.tags ?? []).includes(draft)) return;
												const next = [...(a.tags ?? []), draft].sort();
												try {
													await updateBrokerageAccountTags(a.account_id, next);
													a.tags = next;
													tagDraftInput = '';
												} catch (err) {
													error = err instanceof Error ? err.message : String(err);
												}
											}}
										/>
										<button
											type="button"
											class="tag-edit-done"
											onclick={() => {
												editingTagsAccountId = null;
												tagDraftInput = '';
											}}
										>Done</button>
									</div>
								{:else}
									<button
										type="button"
										class="tags-display"
										onclick={() => {
											editingTagsAccountId = a.account_id;
											tagDraftInput = '';
										}}
										title="Click to edit tags"
									>
										{#each a.tags ?? [] as tag (tag)}
											<span class="tag-pill">{tag}</span>
										{/each}
										{#if (a.tags ?? []).length === 0}
											<span class="muted">+ add</span>
										{/if}
									</button>
								{/if}
							</td>
							<td>{a.entity}</td>
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

		<!-- ── Missing Accounts ─────────────────────────────────────── -->
		{#if missingAccounts && missingAccounts.length > 0}
			<section class="section missing-accounts">
				<h2>
					Missing Accounts
					<span class="badge missing-badge">{missingAccounts.length}</span>
				</h2>
				<p class="muted missing-note">
					Accounts you've marked active that haven't reported a fresh balance in 60+ days
					(or never linked to a live account at all). Add a snapshot or close the account
					to clear it.
				</p>
				<table class="data-table">
					<thead>
						<tr>
							<th>Institution</th>
							<th>Account</th>
							<th>Last 4</th>
							<th>Source</th>
							<th class="num">Last seen</th>
						</tr>
					</thead>
					<tbody>
						{#each missingAccounts as m (m.id)}
							<tr>
								<td>{m.institution}</td>
								<td>{m.account_name}</td>
								<td>{m.last_4 ?? '—'}</td>
								<td>{m.source}</td>
								<td class="num">
									{#if m.last_seen_days_ago === null}
										<span class="muted">never</span>
									{:else}
										{m.last_seen_days_ago}d ago
									{/if}
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</section>
		{/if}

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

	/* Net-worth history chart */
	.history-summary {
		font-size: 13px;
		color: #6e6e73;
	}
	.history-summary .separator {
		margin: 0 8px;
		color: #c7c7cc;
	}
	.history-chart {
		width: 100%;
		height: auto;
		max-height: 240px;
		display: block;
		margin-top: 4px;
	}
	.history-dot {
		cursor: pointer;
		transition: r 0.1s;
	}
	/* Missing accounts panel */
	.missing-accounts h2 {
		display: flex;
		align-items: center;
		gap: 8px;
	}
	.missing-badge {
		background: #ff9500;
		color: #fff;
		font-size: 12px;
		padding: 2px 8px;
		border-radius: 999px;
	}
	.missing-note {
		font-size: 13px;
		margin: -6px 0 12px;
	}

	/* Tag chips: three-state (neutral / include / exclude) */
	.chip.tag-chip.include {
		background: #047a04;
		border-color: #047a04;
		color: #fff;
	}
	.chip.tag-chip.exclude {
		background: #d70015;
		border-color: #d70015;
		color: #fff;
	}

	/* Tag pills inline in account rows */
	.tags-cell {
		max-width: 240px;
	}
	.tags-display {
		background: none;
		border: 1px dashed transparent;
		border-radius: 6px;
		padding: 2px 4px;
		cursor: text;
		display: inline-flex;
		flex-wrap: wrap;
		gap: 4px;
		min-height: 22px;
		font: inherit;
		color: inherit;
		text-align: left;
	}
	.tags-display:hover {
		border-color: #d2d2d7;
		background: #fafafc;
	}
	.tag-pill {
		display: inline-block;
		font-size: 11px;
		padding: 1px 7px;
		background: #f0f0f2;
		color: #1d1d1f;
		border-radius: 999px;
		font-feature-settings: 'tnum' 0;
	}
	.tags-edit {
		display: flex;
		flex-wrap: wrap;
		gap: 4px;
		align-items: center;
	}
	.tag-pill.editing {
		display: inline-flex;
		align-items: center;
		gap: 4px;
	}
	.tag-remove {
		background: none;
		border: none;
		color: #6e6e73;
		cursor: pointer;
		font-size: 14px;
		padding: 0;
		line-height: 1;
	}
	.tag-input {
		font: inherit;
		font-size: 11px;
		padding: 1px 6px;
		border: 1px solid #d2d2d7;
		border-radius: 999px;
		background: #fff;
		min-width: 80px;
	}
	.tag-edit-done {
		font: inherit;
		font-size: 11px;
		padding: 1px 8px;
		border: 1px solid #1d1d1f;
		border-radius: 999px;
		background: #1d1d1f;
		color: #fff;
		cursor: pointer;
	}

	.account-link {
		color: inherit;
		text-decoration: none;
		cursor: pointer;
	}
	.account-link:hover {
		color: #007aff;
		text-decoration: underline;
	}
</style>
