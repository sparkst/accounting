<script lang="ts">
	import { onMount, untrack } from 'svelte';
	import {
		fetchBrokerageNetWorth,
		fetchBrokerageAccounts,
		fetchBrokerageTopHoldings,
		fetchBrokerageRecentTransactions,
		fetchBrokerageRealizedGL,
		fetchBrokerageDataIntegrity,
		fetchBrokerageNetWorthHistory,
		fetchBrokerageBenchmarkComparison,
		fetchBrokerageMissingAccounts
	} from '$lib/api';
	import {
		brokerColor,
		brokerDisplayName,
		fmtCurrency,
		fmtPct,
		fmtQty,
		amountClass,
		fmtCompactCurrency,
		fmtCurrencyNoCents,
		fmtSignedCurrency,
		fmtSignedCurrencyExact,
		fmtSignedPct,
		applySort,
		matchesQuery,
		type SortDir
	} from '$lib/brokerage';
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

	// ── Compare-account picker state ──────────────────────────────────────
	// Selected account ids whose individual history series get overlaid on
	// the main net-worth chart in their broker's color.
	let compareIds = $state<Set<string>>(new Set());
	let compareOpen = $state(false);
	// Per-account history cached by account_id. We re-render the chart
	// whenever the cache or compareIds change.
	let compareCache = $state<Map<string, BrokerageNetWorthHistoryPoint[]>>(new Map());
	// Per-account fetch state so a load failure shows a clear "(failed)"
	// chip instead of being conflated with the "no historical data"
	// (today-only) sparse case.
	let compareErrors = $state<Set<string>>(new Set());
	// Bound via bind:this so we can focus the first interactive element
	// when the popover opens (a11y: keyboard users land inside the dialog
	// rather than staying on the trigger button).
	let comparePopEl = $state<HTMLElement | null>(null);
	// Bound to the inline error banner so we can scrollIntoView when it
	// appears — otherwise an error triggered by the chart's compare
	// toggle (which sits below the fold) would render at the top of the
	// page and the user would never see it.
	let inlineErrorEl = $state<HTMLElement | null>(null);

	let loading = $state(true);

	let showRecent = $state(true);
	let showRealizedGL = $state(true);
	let showIntegrity = $state(false);

	// ── Account filter state ──────────────────────────────────────────────
	// Only tag-chip filtering is exposed on the main summary page (it
	// drives both the visible-accounts list AND the chart's history series
	// via `filterKey`). Search, broker filter, sort, and tag editing live
	// on the dedicated /brokerage/accounts page.
	let acctTagInclude = $state<Set<string>>(new Set());
	let acctTagExclude = $state<Set<string>>(new Set());

	// Allocation segments derived from `by_broker`, sorted by value desc.
	type AllocSeg = { broker: string; value: number; pct: number; color: string; label: string };
	function allocationSegments(byBroker: Record<string, number>): AllocSeg[] {
		const total = Object.values(byBroker).reduce((s, v) => s + v, 0);
		if (total <= 0) return [];
		return Object.entries(byBroker)
			.map(([broker, value]) => ({
				broker,
				value,
				pct: value / total,
				color: brokerColor(broker),
				label: brokerDisplayName(broker)
			}))
			.sort((a, b) => b.value - a.value);
	}

	// Compute three deltas (30d / YTD / inception) from the history series.
	// Returns null when fewer than two points exist.
	type Delta = { period: string; abs: number; pct: number | null } | null;
	function findReferencePoint(
		points: { as_of: string; balance_total: number }[],
		predicate: (d: Date) => boolean
	): { as_of: string; balance_total: number } | null {
		// Pick the LAST historical point that satisfies the predicate (closest
		// to "today" while still meeting the cutoff). Returns null if none.
		for (let i = points.length - 1; i >= 0; i--) {
			if (predicate(new Date(points[i].as_of))) return points[i];
		}
		return null;
	}

	function computeDeltas(
		points: { as_of: string; balance_total: number }[],
		latest: { balance_total: number } | null
	): { d30: Delta; ytd: Delta; inception: Delta } {
		if (!latest || points.length < 2) {
			return { d30: null, ytd: null, inception: null };
		}
		const now = new Date();
		const cutoff30 = new Date(now);
		cutoff30.setDate(cutoff30.getDate() - 30);
		const yearStart = new Date(now.getFullYear(), 0, 1);

		// Historical points exclude the synthetic "today" — slice so the
		// reference can never be the same point as `latest`.
		const hist = points.slice(0, -1);
		const ref30 = findReferencePoint(hist, (d) => d <= cutoff30);
		const refYtd = findReferencePoint(hist, (d) => d <= yearStart) ?? hist[0];
		const refInception = hist[0];

		const v = latest.balance_total;
		// When `ref.balance_total === 0` the absolute delta is still
		// well-defined (e.g. portfolio went from $0 → $100k); only the
		// percentage is undefined. Return `pct: null` instead of dropping
		// the whole delta — the template renders "—" for null pct.
		const mk = (period: string, ref: { balance_total: number; as_of: string } | null): Delta => {
			if (!ref) return null;
			const abs = v - ref.balance_total;
			const pct = ref.balance_total === 0 ? null : abs / ref.balance_total;
			return { period, abs, pct };
		};
		return {
			d30: mk('30 days', ref30),
			ytd: mk('YTD', refYtd),
			inception: mk('All time', refInception)
		};
	}

	// ── Data loading ──────────────────────────────────────────────────────
	// `initialError` only blanks the page on first-load failure. Refetch
	// failures (toggling benchmark, switching recentDays, filter-driven
	// history refetch, etc.) write to a per-endpoint map so a successful
	// holdings refetch doesn't silently wipe a still-broken benchmark
	// error. The visible `refetchError` string is the joined union.
	let initialError = $state('');
	let refetchErrors = $state<Record<string, string>>({});
	let refetchError = $derived(Object.values(refetchErrors).join(' · '));

	function setOpError(op: string, msg: string): void {
		refetchErrors = { ...refetchErrors, [op]: msg };
	}
	function clearOpError(op: string): void {
		if (!(op in refetchErrors)) return;
		const next = { ...refetchErrors };
		delete next[op];
		refetchErrors = next;
	}

	async function loadAll() {
		loading = true;
		initialError = '';
		// Promise.allSettled so a single broken endpoint doesn't blank the
		// whole dashboard; degrade per-section instead.
		const [nwR, acctR, holdR, txR, glR, intR, histR, missR] = await Promise.allSettled([
			fetchBrokerageNetWorth(),
			fetchBrokerageAccounts(),
			fetchBrokerageTopHoldings(topN),
			fetchBrokerageRecentTransactions(recentDays),
			fetchBrokerageRealizedGL(),
			fetchBrokerageDataIntegrity(),
			fetchBrokerageNetWorthHistory(),
			fetchBrokerageMissingAccounts()
		]);
		const errs: string[] = [];
		if (nwR.status === 'fulfilled') netWorth = nwR.value;
		else errs.push(`net worth: ${nwR.reason?.message ?? nwR.reason}`);
		if (acctR.status === 'fulfilled') accounts = acctR.value;
		else errs.push(`accounts: ${acctR.reason?.message ?? acctR.reason}`);
		if (holdR.status === 'fulfilled') topHoldings = holdR.value;
		else errs.push(`holdings: ${holdR.reason?.message ?? holdR.reason}`);
		if (txR.status === 'fulfilled') recentTxns = txR.value;
		else errs.push(`transactions: ${txR.reason?.message ?? txR.reason}`);
		if (glR.status === 'fulfilled') realizedGl = glR.value;
		else errs.push(`realized G/L: ${glR.reason?.message ?? glR.reason}`);
		if (intR.status === 'fulfilled') dataIntegrity = intR.value;
		else errs.push(`integrity: ${intR.reason?.message ?? intR.reason}`);
		if (histR.status === 'fulfilled') networthHistory = histR.value;
		else errs.push(`history: ${histR.reason?.message ?? histR.reason}`);
		if (missR.status === 'fulfilled') missingAccounts = missR.value;
		else errs.push(`missing: ${missR.reason?.message ?? missR.reason}`);

		// Only block the entire page on a hard failure (every fetch failed
		// AND we have nothing rendered yet). Otherwise surface per-section.
		if (errs.length === 8 && !netWorth) {
			initialError = errs.join('; ');
		} else if (errs.length > 0) {
			setOpError('initial', errs.join('; '));
		} else {
			clearOpError('initial');
		}
		loading = false;
	}

	async function reloadHoldings() {
		try {
			topHoldings = await fetchBrokerageTopHoldings(topN);
			clearOpError('holdings');
		} catch (e) {
			setOpError('holdings', `holdings: ${e instanceof Error ? e.message : String(e)}`);
		}
	}

	async function reloadRecent() {
		try {
			recentTxns = await fetchBrokerageRecentTransactions(recentDays);
			clearOpError('transactions');
		} catch (e) {
			setOpError(
				'transactions',
				`transactions: ${e instanceof Error ? e.message : String(e)}`
			);
		}
	}

	async function toggleBenchmark() {
		benchmarkOn = !benchmarkOn;
		if (benchmarkOn && !benchmarkData) {
			try {
				benchmarkData = await fetchBrokerageBenchmarkComparison('SPY');
				clearOpError('benchmark');
			} catch (e) {
				setOpError(
					'benchmark',
					`S&P comparison: ${e instanceof Error ? e.message : String(e)}`
				);
				benchmarkOn = false;
			}
		}
	}

	// Refetch the history series when the account filter changes. We pass the
	// visible account_ids (those passing every active filter) to the API so
	// the chart matches whatever the headline is summing.
	//
	async function refetchFilteredHistory(ids: string[]): Promise<void> {
		try {
			if (!acctIsFiltered) {
				networthHistory = await fetchBrokerageNetWorthHistory({});
			} else if (ids.length === 0) {
				networthHistory = []; // empty filter → chart will render at $0
			} else {
				networthHistory = await fetchBrokerageNetWorthHistory({ accountIds: ids });
			}
			clearOpError('history');
		} catch (e) {
			setOpError('history', `history: ${e instanceof Error ? e.message : String(e)}`);
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

	// (Sort/filter/search helpers are imported from $lib/brokerage at the
	// top of this file. Only `toggleSort` is page-specific because it
	// closes over the per-table sort key/dir state.)

	// ── Filtered accounts (drives top-5 row-list AND chart history) ──────
	// Sorted by market value desc by default — main page exposes only the
	// tag-chip filter; full search/sort/edit live on /brokerage/accounts.
	let filteredAccounts = $derived.by(() => {
		const filtered = withSnapshots.filter((a) => {
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
			return true;
		});
		return applySort(filtered, 'market_value', 'desc');
	});

	let acctVisibleTotal = $derived(
		filteredAccounts.reduce((sum, a) => sum + (a.market_value ?? 0), 0)
	);
	let acctAllTotal = $derived(
		withSnapshots.reduce((sum, a) => sum + (a.market_value ?? 0), 0)
	);
	let acctIsFiltered = $derived(acctTagInclude.size > 0 || acctTagExclude.size > 0);

	// Refetch the chart's history series when the account filter changes.
	// `untrack` reads `filteredAccounts` lazily so the effect only re-runs
	// when `filterKey` (a coarse, normalized signature) actually changes —
	// not on every transient mutation of the array reference. Declared
	// after `filteredAccounts` to avoid use-before-declaration.
	let filterKey = $derived(
		`${acctIsFiltered}|${filteredAccounts
			.map((a) => a.account_id)
			.sort()
			.join(',')}`
	);
	$effect(() => {
		filterKey; // track normalized signature only
		if (!accounts) return; // initial load handled by loadAll()
		const ids = untrack(() => filteredAccounts.map((a) => a.account_id).sort());
		void refetchFilteredHistory(ids);
	});

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

	// Allocation segments + deltas derived from the same data the headline uses.
	// When a filter is active we recompute by_broker from the visible accounts so
	// the bar matches the headline number.
	let allocSegments = $derived.by(() => {
		if (!netWorth) return [];
		if (acctIsFiltered) {
			const byBroker: Record<string, number> = {};
			for (const a of filteredAccounts) {
				byBroker[a.broker] = (byBroker[a.broker] ?? 0) + (a.market_value ?? 0);
			}
			return allocationSegments(byBroker);
		}
		return allocationSegments(netWorth.by_broker);
	});

	// ── Compare-account picker (helpers + derived lines) ────────────────
	async function ensureAccountHistory(accountId: string): Promise<void> {
		if (compareCache.has(accountId)) return;
		try {
			const series = await fetchBrokerageNetWorthHistory({ accountIds: [accountId] });
			const next = new Map(compareCache);
			next.set(accountId, series);
			compareCache = next;
			if (compareErrors.has(accountId)) {
				const errs = new Set(compareErrors);
				errs.delete(accountId);
				compareErrors = errs;
			}
			clearOpError(`account-${accountId}`);
		} catch (e) {
			// Surface the failure on the per-account chip — distinct from the
			// "today only" sparse-data state. Don't let it silently render an
			// empty/missing line.
			const errs = new Set(compareErrors);
			errs.add(accountId);
			compareErrors = errs;
			setOpError(
				`account-${accountId}`,
				`account history: ${e instanceof Error ? e.message : String(e)}`
			);
		}
	}

	function toggleCompare(accountId: string): void {
		const next = new Set(compareIds);
		if (next.has(accountId)) next.delete(accountId);
		else next.add(accountId);
		compareIds = next;
		if (next.has(accountId)) void ensureAccountHistory(accountId);
	}

	function clearCompare(): void {
		compareIds = new Set();
		compareErrors = new Set();
	}

	// Close the compare popover when the user clicks outside it. The
	// `setTimeout(_, 0)` defers subscription past the current click that
	// opened the popover so the toggle's own bubbling click doesn't
	// immediately close us — works even if the toggle's `stopPropagation`
	// is removed in a future refactor. Cleanup is automatic via the
	// $effect return.
	$effect(() => {
		if (!compareOpen) return;
		let onDocClick: (() => void) | null = null;
		const t = setTimeout(() => {
			onDocClick = () => (compareOpen = false);
			document.addEventListener('click', onDocClick);
		}, 0);
		return () => {
			clearTimeout(t);
			if (onDocClick) document.removeEventListener('click', onDocClick);
		};
	});

	// Focus the first checkbox in the compare popover when it opens — so
	// keyboard users land inside the dialog rather than getting stuck on
	// the trigger button. The setTimeout defers past the same tick where
	// the popover element mounts.
	$effect(() => {
		if (!compareOpen || !comparePopEl) return;
		const t = setTimeout(() => {
			const first = comparePopEl?.querySelector<HTMLElement>('input[type=checkbox]');
			first?.focus();
		}, 0);
		return () => clearTimeout(t);
	});

	// Scroll the inline error banner into view whenever it transitions
	// from empty → present. The chart's compare toggle and the recent-
	// activity day-selector both sit below the fold; without this, an
	// error triggered there would render at the top of the page and the
	// user would never see it. Honour `prefers-reduced-motion` — users
	// who've opted out of OS-level animation should get an instant scroll
	// instead of the smooth one (browsers don't apply the preference to
	// `scrollIntoView` automatically).
	$effect(() => {
		if (!refetchError || !inlineErrorEl) return;
		const reducedMotion =
			typeof window !== 'undefined' &&
			window.matchMedia('(prefers-reduced-motion: reduce)').matches;
		inlineErrorEl.scrollIntoView({
			behavior: reducedMotion ? 'auto' : 'smooth',
			block: 'nearest'
		});
	});

	// Derived overlay lines for selected accounts. Each line aligns
	// per-account history points against the main chart's x-positions by
	// matching `as_of` strings — points without a match are skipped, and
	// $0 / unfunded points create a gap (so accounts that came online
	// later start partway across the timeline).
	//
	// `nonZeroPoints` lets the UI flag accounts whose backend history is
	// effectively just today's PositionSnapshot — historical XLSX rows
	// aren't yet linked to live account_ids, so per-account series are
	// sparse for most accounts. The chip shows "(today only)" in that
	// case rather than silently rendering an invisible single-dot line.
	type CompareLine = {
		id: string;
		broker: string;
		name: string;
		color: string;
		path: string;
		nonZeroPoints: number;
	};
	let compareLines = $derived.by(() => {
		if (!historyChart || historyChart.empty) return [] as CompareLine[];
		const lines: CompareLine[] = [];
		for (const id of compareIds) {
			const series = compareCache.get(id);
			if (!series || series.length === 0) continue;
			const acct = (accounts ?? []).find((a) => a.account_id === id);
			if (!acct) continue;
			const seriesByDate = new Map(series.map((p) => [p.as_of, p.balance_total]));
			const segments: string[] = [];
			let pen = 'M';
			let nonZero = 0;
			historyChart.points.forEach((p, i) => {
				const v = seriesByDate.get(p.as_of);
				if (v === undefined || v <= 0) {
					pen = 'M';
					return;
				}
				const x = historyChart.xs[i];
				const y = historyChart.yFor(v);
				segments.push(`${pen} ${x.toFixed(1)} ${y.toFixed(1)}`);
				pen = 'L';
				nonZero++;
			});
			lines.push({
				id,
				broker: acct.broker,
				name: acct.account_name ?? acct.account_number_masked,
				color: brokerColor(acct.broker),
				path: segments.join(' '),
				nonZeroPoints: nonZero
			});
		}
		return lines;
	});

	// True when at least one selected account has insufficient history
	// (≤1 non-zero point). Drives the "today only" notice below the chart.
	let compareSparse = $derived(compareLines.some((l) => l.nonZeroPoints <= 1));

	let deltas = $derived.by(() => {
		if (!historyChart || historyChart.empty || !historyChart.last) {
			return { d30: null, ytd: null, inception: null };
		}
		return computeDeltas(historyChart.points, historyChart.last);
	});

</script>

<svelte:head>
	<title>Brokerage · Accounting</title>
	<link rel="preconnect" href="https://fonts.googleapis.com" />
	<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin="anonymous" />
	<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet" />
</svelte:head>

<div class="page brokerage">
	<nav class="crumbs" aria-label="Breadcrumb">
		<a href="/">Money</a>
		<span class="sep">/</span>
		<span>Brokerage</span>
	</nav>

	<h1 class="sr-only">Brokerage</h1>

	{#if loading && !netWorth}
		<div class="state">Loading…</div>
	{:else if initialError}
		<div class="state error">⚠ {initialError}</div>
	{:else if netWorth}
		{#if refetchError}
			<div bind:this={inlineErrorEl} class="inline-error" role="alert">
				⚠ Could not refresh: {refetchError}
				<button type="button" class="inline-error-retry" onclick={() => (refetchErrors = {})}>Dismiss</button>
			</div>
		{/if}
		<!-- ── Hero: Net Worth ───────────────────────────────────────── -->
		<section class="section first hero">
			<div class="hero-head">
				<div>
					<div class="hero-label">Net worth{acctIsFiltered ? ' · filtered' : ''}</div>
					{#if acctIsFiltered}
						<div class="hero-num">{fmtCurrencyNoCents(acctVisibleTotal)}</div>
					{:else}
						<div class="hero-num">{fmtCurrencyNoCents(netWorth.total)}</div>
					{/if}
				</div>
				<div class="hero-deltas">
					{#if deltas.d30}
						<div class="delta">
							<span class="delta-period">Last 30d</span>
							<span class="delta-val {deltas.d30.abs >= 0 ? 'pos' : 'neg'}">
								{fmtSignedCurrency(deltas.d30.abs)}
								<span class="pct">{deltas.d30.pct === null ? '—' : fmtSignedPct(deltas.d30.pct)}</span>
							</span>
						</div>
					{/if}
					{#if deltas.ytd}
						<div class="delta">
							<span class="delta-period">YTD</span>
							<span class="delta-val {deltas.ytd.abs >= 0 ? 'pos' : 'neg'}">
								{fmtSignedCurrency(deltas.ytd.abs)}
								<span class="pct">{deltas.ytd.pct === null ? '—' : fmtSignedPct(deltas.ytd.pct)}</span>
							</span>
						</div>
					{/if}
					{#if deltas.inception}
						<div class="delta">
							<span class="delta-period">All time</span>
							<span class="delta-val {deltas.inception.abs >= 0 ? 'pos' : 'neg'}">
								{fmtSignedCurrency(deltas.inception.abs)}
								<span class="pct">{deltas.inception.pct === null ? '—' : fmtSignedPct(deltas.inception.pct)}</span>
							</span>
						</div>
					{/if}
				</div>
			</div>
			{#if acctIsFiltered}
				<div class="hero-meta">
					Showing {filteredAccounts.length} of {withSnapshots.length} accounts
					<span class="sep">·</span>
					All accounts: <b>{fmtCurrencyNoCents(acctAllTotal)}</b>
				</div>
			{:else if netWorth.as_of_min && netWorth.as_of_max}
				<div class="hero-meta">{netWorth.as_of_min} … {netWorth.as_of_max}</div>
			{/if}
		</section>

		<!-- ── Net-worth history (chart inside hero, design pattern) ─── -->
		{#if historyChart}
			<section class="section chart-section">
				<svg class="history-chart" viewBox={`0 0 ${CHART_W} ${CHART_H + 30}`} preserveAspectRatio="xMidYMid meet" role="img" aria-label="Net worth over time">
					<defs>
						<linearGradient id="history-fill" x1="0" y1="0" x2="0" y2="1">
							<stop offset="0%" stop-color="var(--accent)" stop-opacity="0.16" />
							<stop offset="100%" stop-color="var(--accent)" stop-opacity="0" />
						</linearGradient>
					</defs>
					<!-- Y-axis dollar gridlines.
					     Key by index (not value): pickAxisTicks can yield
					     duplicate values when min == max (e.g. flatlined
					     filter), and Svelte's each_key_duplicate halts
					     reactive updates — the same bug that surfaced as the
					     tag-chip click freeze. -->
					{#each historyChart.dollarTicks as tick, i (i)}
						{@const y = historyChart.yFor(tick)}
						<line x1={CHART_PAD_X} y1={y} x2={CHART_W - CHART_PAD_X} y2={y} stroke="var(--hairline-2)" stroke-width="1" />
						<text x={CHART_PAD_X - 4} y={y + 3} text-anchor="end" font-size="10" fill="var(--ink-3)" font-family="Inter,system-ui">{formatAxisDollars(tick)}</text>
					{/each}
					<path d={historyChart.areaPath} fill="url(#history-fill)" />
					{#if benchmarkOn && historyChart.benchPath}
						<path d={historyChart.benchPath} fill="none" stroke="var(--ink-3)" stroke-width="1.2" stroke-dasharray="3 3" opacity="0.6" />
					{/if}
					{#each compareLines as line (line.id)}
						{#if line.nonZeroPoints > 1}
							<path d={line.path} fill="none" stroke={line.color} stroke-width="1.4" stroke-linejoin="round" stroke-linecap="round" opacity="0.85" />
						{/if}
					{/each}
					<path d={historyChart.linePath} fill="none" stroke="var(--accent)" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round" />
					{#each historyChart.points as p, i (p.as_of)}
						{@const isToday = historyChart.todayIdx === i}
						{@const isHover = hoverHistoryIdx === i}
						<circle
							cx={historyChart.xs[i]}
							cy={historyChart.ys[i]}
							r={isHover ? 5 : isToday ? 3.5 : 0}
							fill={isToday ? 'var(--pos)' : 'var(--accent)'}
							stroke={isToday || isHover ? 'var(--bg)' : 'none'}
							stroke-width={isToday || isHover ? 1.5 : 0}
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
						<line x1={cx} y1={CHART_PAD_Y} x2={cx} y2={CHART_H - CHART_PAD_Y} stroke="var(--ink-3)" stroke-dasharray="2 3" opacity="0.55" />
						<g transform={`translate(${Math.min(cx + 8, CHART_W - 160)} ${Math.max(cy - 32, 0)})`}>
							<rect x="0" y="0" width="148" height={isToday ? 48 : 38} rx="6" fill="var(--ink)" />
							<text x="8" y="15" fill="var(--bg)" font-size="11" font-family="Inter,system-ui">{p.as_of}{isToday ? ' · Live' : ''}</text>
							<text x="8" y="30" fill="var(--bg)" font-size="13" font-weight="600" font-family="Inter,system-ui">{fmtCurrency(p.balance_total)}</text>
							{#if isToday}<text x="8" y="44" fill="var(--accent-soft)" font-size="10" font-family="Inter,system-ui">live total</text>{/if}
						</g>
					{/if}
					<!-- X-axis date ticks. Key by index — see dollarTicks note above. -->
					{#each historyChart.dateTicks as tick, i (i)}
						{@const x = historyChart.xs[tick.idx] ?? CHART_PAD_X}
						<text x={x} y={CHART_H - 2} text-anchor="middle" font-size="10" fill="var(--ink-3)" font-family="Inter,system-ui">{tick.label}</text>
					{/each}
				</svg>
				<div class="chart-controls">
					<span class="chart-summary">
						{#if historyChart.first && historyChart.last}
							{historyChart.first.as_of} → {historyChart.last.as_of}
						{:else}
							No accounts match filter
						{/if}
					</span>
					<button
						type="button"
						class="compare-toggle"
						class:active={benchmarkOn}
						onclick={toggleBenchmark}
					>
						<span class="dash-dot"></span>
						{#if benchmarkOn && benchmarkData?.benchmark_pct !== null && benchmarkData?.benchmark_pct !== undefined}
							S&P 500
							<span class="compare-pct {benchmarkData.benchmark_pct >= 0 ? 'pos' : 'neg'}">
								{fmtSignedPct(benchmarkData.benchmark_pct)} over period
							</span>
						{:else}
							Compare to S&P 500
						{/if}
					</button>
					<div class="acct-cmp">
						<button
							type="button"
							class="acct-cmp-btn"
							aria-haspopup="true"
							aria-expanded={compareOpen}
							onclick={(e) => {
								e.stopPropagation();
								compareOpen = !compareOpen;
							}}
						>
							+ Compare account
						</button>
						{#if compareOpen}
							<div
								bind:this={comparePopEl}
								class="acct-cmp-pop"
								role="dialog"
								aria-label="Select accounts to compare on the chart"
								onclick={(e) => e.stopPropagation()}
								onkeydown={(e) => {
									if (e.key === 'Escape') {
										compareOpen = false;
										return;
									}
									// Focus trap: keep Tab/Shift-Tab cycling inside the
									// dialog. A role="dialog" without a trap lets focus
									// escape into the underlying page while the modal is
									// still visible — WCAG 2.1 expects focus confinement.
									if (e.key !== 'Tab' || !comparePopEl) return;
									// Standard tabbable selector — broader than just
									// checkboxes so the trap stays correct if a Clear /
									// close button is added inside the popover later.
									const focusable = Array.from(
										comparePopEl.querySelectorAll<HTMLElement>(
											'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
										)
									);
									if (focusable.length === 0) return;
									const first = focusable[0];
									const last = focusable[focusable.length - 1];
									const active = document.activeElement as HTMLElement | null;
									if (e.shiftKey && active === first) {
										e.preventDefault();
										last.focus();
									} else if (!e.shiftKey && active === last) {
										e.preventDefault();
										first.focus();
									}
								}}
								tabindex="-1"
							>
								{#each withSnapshots as a (a.account_id)}
									<label>
										<input
											type="checkbox"
											checked={compareIds.has(a.account_id)}
											onchange={() => toggleCompare(a.account_id)}
										/>
										<span class="acct-cmp-row">
											<span class="dot" style:background={brokerColor(a.broker)}></span>
											<span class="name" title={a.account_name ?? a.account_number_masked}>
												{a.account_name ?? a.account_number_masked}
											</span>
										</span>
										<span class="val">{fmtCompactCurrency(a.market_value ?? 0)}</span>
									</label>
								{/each}
							</div>
						{/if}
					</div>
					{#if compareIds.size > 0}
						<span class="acct-cmp-chips">
							{#each Array.from(compareIds) as id (id)}
								{@const a = (accounts ?? []).find((x) => x.account_id === id)}
								{@const line = compareLines.find((l) => l.id === id)}
								{@const failed = compareErrors.has(id)}
								{@const sparse = !failed && (!line || line.nonZeroPoints <= 1)}
								{#if a}
									<span class="acct-chip" class:sparse class:failed>
										<span class="dot" style:background={brokerColor(a.broker)}></span>
										<span class="acct-chip-name">{a.account_name ?? a.account_number_masked}</span>
										{#if failed}
											<span class="acct-chip-note">failed</span>
										{:else if sparse}
											<span class="acct-chip-note">today only</span>
										{/if}
										<button
											type="button"
											class="x"
											aria-label="Remove {a.account_name ?? a.account_number_masked}"
											onclick={() => toggleCompare(id)}
										>×</button>
									</span>
								{/if}
							{/each}
							<button type="button" class="acct-cmp-clear" onclick={clearCompare}>Clear</button>
						</span>
					{/if}
				</div>
				{#if compareSparse || compareErrors.size > 0}
					<p class="compare-notice">
						{#if compareErrors.size > 0}
							⚠ Some accounts couldn't be loaded — chips marked "failed" can be
							removed and re-added to retry. {#if compareSparse}Other accounts have no
							historical balance data linked yet, so only "today only" appears.{/if}
						{:else}
							Per-account historical balance data isn't linked for the marked accounts —
							lines render only for accounts with two or more historical snapshots.
						{/if}
					</p>
				{/if}
			</section>
		{:else if !loading}
			<section class="section chart-section">
				<p class="muted" style="padding: 24px 0;">
					Not enough net-worth history yet to draw a chart — at least two
					historical snapshots are required. Once a second snapshot lands the
					chart will appear automatically.
				</p>
			</section>
		{/if}

		<!-- ── Allocation by broker ──────────────────────────────────── -->
		{#if allocSegments.length > 0}
			<section class="section">
				<div class="sec-head">
					<h2 class="sec-title">Allocation · by broker</h2>
				</div>
				<div class="alloc-bar" role="img" aria-label="Allocation by broker">
					{#each allocSegments as seg (seg.broker)}
						<div class="alloc-seg" style:width="{(seg.pct * 100).toFixed(2)}%" style:background={seg.color} title="{seg.label} {(seg.pct * 100).toFixed(1)}%"></div>
					{/each}
				</div>
				<div class="alloc-legend">
					{#each allocSegments as seg (seg.broker)}
						<div class="alloc-item">
							<span class="swatch" style:background={seg.color}></span>
							<span class="name">{seg.label}</span>
							<span class="val">{fmtCurrencyNoCents(seg.value)}</span>
							<span class="pct">{(seg.pct * 100).toFixed(1)}%</span>
						</div>
					{/each}
				</div>
			</section>
		{/if}

		<!-- ── Accounts (summary: top 5 + filter chips drive chart) ─── -->
		<section class="section">
			<div class="sec-head">
				<h2 class="sec-title">Accounts · {filteredAccounts.length} of {withSnapshots.length}</h2>
				<a href="/brokerage/accounts" class="sec-link">All accounts · filter, group, tag <span class="arrow">→</span></a>
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
							aria-label="{tag}: {state === 'include'
								? 'including'
								: state === 'exclude'
									? 'excluding'
									: 'not filtered'} (click to cycle)"
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

			<div class="accounts-list">
				{#each filteredAccounts.slice(0, 5) as a (a.account_id)}
					<a class="a-row" href={`/brokerage/accounts/${a.account_id}`} title={a.account_name ?? a.account_number_masked}>
						<span class="a-dot" style:background={brokerColor(a.broker)}></span>
						<div class="a-info">
							<div class="a-name">{a.account_name ?? a.account_number_masked}</div>
							<div class="a-meta">
								<span class="a-broker">{brokerDisplayName(a.broker)}</span>
								{#each a.tags ?? [] as tag (tag)}<span class="a-tag">{tag}</span>{/each}
							</div>
						</div>
						<div class="a-meta a-asof">As of {a.as_of}</div>
						<div class="a-val">{fmtCurrencyNoCents(a.market_value ?? 0)}</div>
					</a>
				{/each}
				{#if filteredAccounts.length > 5}
					{@const restTotal = filteredAccounts.slice(5).reduce((s, a) => s + (a.market_value ?? 0), 0)}
					<a href="/brokerage/accounts" class="a-more">
						{filteredAccounts.length - 5} more accounts · {fmtCurrencyNoCents(restTotal)} · show all <span class="arrow">→</span>
					</a>
				{:else if filteredAccounts.length === 0}
					<div class="empty-row">No accounts match the current filter.</div>
				{/if}
			</div>
		</section>

		<!-- ── Top Holdings (summary: top 5 row-list) ─────────────────── -->
		<section class="section">
			<div class="sec-head">
				<h2 class="sec-title">Top holdings</h2>
				<a href="/brokerage/holdings" class="sec-link">All holdings · search, sort <span class="arrow">→</span></a>
			</div>

			<div class="holdings-list">
				{#each (topHoldings ?? []).slice(0, 5) as h, i (h.symbol ?? h.description ?? `idx-${i}`)}
					{@const href = h.symbol && !h.is_cash_sleeve ? `/brokerage/holdings/${h.symbol}` : null}
					<svelte:element
						this={href ? 'a' : 'div'}
						href={href}
						class="h-row {h.is_cash_sleeve ? 'cash' : ''}"
						title={h.description ?? h.symbol}
					>
						<span class="h-sym">
							{#if h.is_cash_sleeve}Cash{:else}{h.symbol ?? '—'}{/if}
						</span>
						<span class="h-desc">
							{#if h.description}{h.description}{/if}
							{#if h.total_quantity}<span class="h-qty"> · {fmtQty(h.total_quantity)} sh</span>{/if}
							{#if h.account_count > 1}<span class="h-qty"> · {h.account_count} accounts</span>{/if}
						</span>
						<span class="h-val">{fmtCurrencyNoCents(h.total_market_value)}</span>
						<div class="h-bar"><div style:width="{Math.min(100, h.pct_of_net_worth * 100).toFixed(2)}%"></div></div>
						<span class="h-pct">{fmtPct(h.pct_of_net_worth)}</span>
					</svelte:element>
				{/each}
				{#if (topHoldings?.length ?? 0) === 0}
					<div class="empty-row">No holdings yet.</div>
				{/if}
			</div>
		</section>

		<!-- ── Recent activity (summary: last 10 in window) ─────────── -->
		<section class="section">
			<div class="sec-head">
				<h2 class="sec-title">Recent activity</h2>
				<a href="/brokerage/transactions" class="sec-link">All transactions · search, filter <span class="arrow">→</span></a>
			</div>
			{#if (recentTxns?.length ?? 0) === 0}
				<p class="muted">No transactions in the last {recentDays} days.</p>
			{:else}
				{@const recentNet = (recentTxns ?? []).reduce((s, t) => s + (t.amount ?? 0), 0)}
				<div class="activity-summary">
					Last {recentDays} days · {recentTxns?.length} transactions · net cash flow
					<b class={recentNet >= 0 ? 'pos' : 'neg'}>{fmtSignedCurrencyExact(recentNet)}</b>
				</div>
				<table class="data-table compact">
					<tbody>
						{#each (recentTxns ?? []).slice(0, 10) as t, i (`${t.trade_date}-${t.account_number_masked}-${t.action}-${i}`)}
							<tr>
								<td class="tx-date">{t.trade_date}</td>
								<td>
									<span class="broker-cell">
										<span class="broker-dot" style:background={brokerColor(t.broker)}></span>
										{brokerDisplayName(t.broker)}
									</span>
								</td>
								<td>{t.action} <span class="muted">····{t.account_number_masked.slice(-4)}</span></td>
								<td>{t.symbol ?? '—'}</td>
								<td class="num muted">{t.quantity ? fmtQty(t.quantity) : '—'}</td>
								<td class="num {amountClass(t.amount)}">{fmtCurrency(t.amount)}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			{/if}
		</section>

		<!-- ── Realized G/L ─────────────────────────────────────────── -->
		<section class="section">
			<div class="sec-head">
				<h2 class="sec-title">
					<button
						class="toggle"
						onclick={() => (showRealizedGL = !showRealizedGL)}
						aria-expanded={showRealizedGL}
					>
						{showRealizedGL ? '▾' : '▸'} Realized gains &amp; losses
					</button>
				</h2>
				<a href="/brokerage/transactions?view=realized-gl" class="sec-link">Lots, wash-sale checks, 1099-B <span class="arrow">→</span></a>
			</div>
			{#if showRealizedGL && realizedGl}
				<div class="gl-grid">
					{#each realizedYears.slice(0, 3) as year (year)}
						{@const b = realizedGl.by_year[String(year)]}
						{@const isYtd = year === new Date().getFullYear()}
						<div class="gl-card">
							<span class="gl-year">{year}{isYtd ? ' · YTD' : ''}</span>
							<span class="gl-total {amountClass(b.total)}">{fmtSignedCurrencyExact(b.total)}</span>
							<div class="gl-bd">
								<span>ST &nbsp;<b>{fmtSignedCurrencyExact(b.short_term)}</b></span>
								<span>LT &nbsp;<b>{fmtSignedCurrencyExact(b.long_term)}</b></span>
								{#if b.unknown !== 0}
									<span>? &nbsp;<b>{fmtSignedCurrencyExact(b.unknown)}</b></span>
								{/if}
								<span class="gl-lots">· {b.lots} lots</span>
							</div>
						</div>
					{/each}
				</div>
				{#if realizedYears.length > 3}
					<details class="gl-more">
						<summary>{realizedYears.length - 3} earlier year{realizedYears.length - 3 === 1 ? '' : 's'} →</summary>
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
								{#each realizedYears.slice(3) as year (year)}
									{@const b = realizedGl.by_year[String(year)]}
									<tr>
										<td>{year}</td>
										<td class="num {amountClass(b.short_term)}">{fmtSignedCurrencyExact(b.short_term)}</td>
										<td class="num {amountClass(b.long_term)}">{fmtSignedCurrencyExact(b.long_term)}</td>
										<td class="num {amountClass(b.unknown)}">{fmtSignedCurrencyExact(b.unknown)}</td>
										<td class="num {amountClass(b.total)}">{fmtSignedCurrencyExact(b.total)}</td>
										<td class="num">{b.lots}</td>
									</tr>
								{/each}
							</tbody>
						</table>
					</details>
				{/if}
				<p class="gl-note">
					{#if realizedGl.wash_sales.lots === 0}
						<span class="ok">✓</span> No wash sales detected in ingested data.
						<span style="opacity:0.7;">1099-B substantiation not yet ingested.</span>
					{:else}
						<span class="warn">⚠</span> <strong>Wash sales:</strong>
						{realizedGl.wash_sales.lots} lots, total disallowed loss
						{fmtCurrency(realizedGl.wash_sales.total_disallowed_loss)}
					{/if}
				</p>
			{/if}
		</section>

		<!-- ── Missing Accounts ─────────────────────────────────────── -->
		{#if missingAccounts && missingAccounts.length > 0}
			<section class="section missing-accounts">
				<div class="sec-head">
					<h2 class="sec-title">
						Missing accounts
						<span class="badge missing-badge">{missingAccounts.length}</span>
					</h2>
				</div>
				<p class="missing-note">
					Accounts marked active that haven't reported a fresh balance in 60+ days
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

		<!-- ── Data integrity (footer strip) ────────────────────────── -->
		<div class="integrity" class:has-warnings={hasIntegrityWarnings}>
			<div class="integrity-left">
				<span class="integrity-mark" class:warn={hasIntegrityWarnings} aria-hidden="true">
					{#if hasIntegrityWarnings}
						<svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><line x1="5" y1="2" x2="5" y2="6"/><circle cx="5" cy="8.2" r="0.6" fill="currentColor"/></svg>
					{:else}
						<svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="1.5,5.5 4,8 8.5,2.5"/></svg>
					{/if}
				</span>
				<div>
					{#if hasIntegrityWarnings}
						<b>Data integrity warnings present</b>
					{:else if dataIntegrity}
						<b>All sources reconciled</b> · {dataIntegrity.accounts} accounts · {dataIntegrity.transactions.toLocaleString()} transactions
					{:else}
						<b>Status unknown</b>
					{/if}
				</div>
			</div>
			<button
				type="button"
				class="sec-link"
				onclick={() => (showIntegrity = !showIntegrity)}
				aria-expanded={showIntegrity}
			>
				{showIntegrity ? 'Hide details' : 'Data integrity report'} <span class="arrow">→</span>
			</button>
		</div>
		{#if showIntegrity && dataIntegrity}
			<section class="section integrity-detail">
				<dl class="grid">
					<dt>Accounts</dt><dd>{dataIntegrity.accounts.toLocaleString()}</dd>
					<dt>Transactions</dt><dd>{dataIntegrity.transactions.toLocaleString()}</dd>
					<dt>Position snapshots</dt><dd>{dataIntegrity.position_snapshots.toLocaleString()}</dd>
					<dt>Realized lots</dt><dd>{dataIntegrity.realized_lots.toLocaleString()}</dd>
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
							<li>⚠ {dataIntegrity.duplicate_position_groups} duplicate position group(s) (adapter-bug indicator)</li>
						{/if}
						{#if dataIntegrity.duplicate_transaction_groups > 0}
							<li>⚠ {dataIntegrity.duplicate_transaction_groups} duplicate transaction group(s) (adapter-bug indicator)</li>
						{/if}
					</ul>
				{:else}
					<p class="muted">No data integrity warnings.</p>
				{/if}
			</section>
		{/if}
	{/if}
</div>

