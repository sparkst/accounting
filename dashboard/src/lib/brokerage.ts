// Shared helpers for the brokerage redesign — used by /brokerage/* pages.
// Keep these alongside the .brokerage CSS in src/app.css; together they form
// the design system for this section of the dashboard.

const KNOWN_BROKERS = new Set([
	'etrade',
	'schwab',
	'vanguard',
	'fidelity',
	'fg_annuity',
	'gsk_pension',
	'nw_mutual',
	'franklin_templeton'
]);

const BROKER_DISPLAY_OVERRIDES: Record<string, string> = {
	etrade: 'E-Trade',
	fg_annuity: 'F&G Annuity',
	gsk_pension: 'GSK Pension',
	nw_mutual: 'NW Mutual',
	franklin_templeton: 'Franklin Templeton'
};

function normalizeBroker(broker: string): string {
	return broker.toLowerCase().replace(/[^a-z0-9]+/g, '_');
}

// Map broker name → CSS variable from the design's calm broker palette.
// Unknown brokers fall back to --ink-3 (a neutral hairline grey).
export function brokerColor(broker: string | null | undefined): string {
	if (!broker) return 'var(--ink-3)';
	const key = normalizeBroker(broker);
	return KNOWN_BROKERS.has(key) ? `var(--br-${key})` : 'var(--ink-3)';
}

// Map data keys → label as in the design (E-Trade, F&G Annuity, etc.).
// Unknown brokers get title-cased with underscores → spaces.
export function brokerDisplayName(broker: string): string {
	const key = normalizeBroker(broker);
	return (
		BROKER_DISPLAY_OVERRIDES[key] ??
		broker.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
	);
}

// Currency formatters used across the brokerage redesign. The DB stores
// expenses negative / income positive (see CLAUDE.md sign convention) but
// these all take pre-signed values and just format them.
export function fmtCurrency(n: number | null | undefined): string {
	if (n === null || n === undefined) return '—';
	const sign = n < 0 ? '-' : '';
	return `${sign}$${Math.abs(n).toLocaleString('en-US', {
		minimumFractionDigits: 2,
		maximumFractionDigits: 2
	})}`;
}

export function fmtCurrencyNoCents(n: number): string {
	const sign = n < 0 ? '-' : '';
	return `${sign}$${Math.abs(n).toLocaleString('en-US', { maximumFractionDigits: 0 })}`;
}

export function fmtCompactCurrency(n: number): string {
	// Sign handled separately so a negative balance renders as "−$1.23M" with
	// the dash before the dollar (not the broken "-$1.23M" that fell out of
	// `$${n / 1_000_000}` for negative inputs).
	const sign = n < 0 ? '−' : '';
	const abs = Math.abs(n);
	if (abs >= 1_000_000) return `${sign}$${(abs / 1_000_000).toFixed(2)}M`;
	if (abs >= 1_000) return `${sign}$${(abs / 1_000).toFixed(0)}K`;
	return `${sign}$${abs.toFixed(0)}`;
}

// Whole-dollar signed currency used for hero deltas (overview-level granularity
// where cents are noise). For tax-relevant displays (realized G/L cards), use
// `fmtSignedCurrencyExact` instead — that one preserves cents to match what a
// 1099-B would show.
export function fmtSignedCurrency(n: number): string {
	if (Object.is(n, -0)) return '$0';
	const sign = n >= 0 ? '+' : '−';
	return `${sign}$${Math.abs(n).toLocaleString('en-US', { maximumFractionDigits: 0 })}`;
}

export function fmtSignedCurrencyExact(n: number): string {
	if (Object.is(n, -0)) return '$0.00';
	const sign = n >= 0 ? '+' : '−';
	return `${sign}$${Math.abs(n).toLocaleString('en-US', {
		minimumFractionDigits: 2,
		maximumFractionDigits: 2
	})}`;
}

export function fmtSignedPct(n: number): string {
	const sign = n >= 0 ? '+' : '−';
	return `${sign}${Math.abs(n * 100).toFixed(1)}%`;
}

export function fmtPct(n: number): string {
	return `${(n * 100).toFixed(1)}%`;
}

export function fmtQty(n: number | null | undefined): string {
	if (n === null || n === undefined) return '—';
	return n.toLocaleString('en-US', { maximumFractionDigits: 4 });
}

export function amountClass(n: number | null | undefined): string {
	if (n === null || n === undefined) return '';
	return n >= 0 ? 'pos' : 'neg';
}

// ─── Shared sort / filter primitives used by /brokerage/* pages ──────
// Pure functions with no Svelte reactivity — kept here so accounts,
// holdings, and transactions pages don't duplicate them.

export type SortDir = 'asc' | 'desc' | null;

export function compareValues(a: unknown, b: unknown): number {
	const aNull = a === null || a === undefined;
	const bNull = b === null || b === undefined;
	if (aNull && bNull) return 0;
	if (aNull) return 1;
	if (bNull) return -1;
	if (typeof a === 'number' && typeof b === 'number') return a - b;
	return String(a).localeCompare(String(b), undefined, { numeric: true });
}

export function applySort<T>(rows: T[], key: string | null, dir: SortDir): T[] {
	if (!key || !dir) return rows;
	const sorted = [...rows].sort((a, b) =>
		compareValues((a as Record<string, unknown>)[key], (b as Record<string, unknown>)[key])
	);
	return dir === 'desc' ? sorted.reverse() : sorted;
}

export function nextSortDir(current: SortDir): SortDir {
	if (current === 'asc') return 'desc';
	if (current === 'desc') return null;
	return 'asc';
}

export function matchesQuery(haystack: (string | null | undefined)[], needle: string): boolean {
	if (!needle) return true;
	const q = needle.toLowerCase();
	return haystack.some((h) => h !== null && h !== undefined && h.toLowerCase().includes(q));
}

export function toggleSetMember<T>(set: Set<T>, value: T): Set<T> {
	const next = new Set(set);
	if (next.has(value)) next.delete(value);
	else next.add(value);
	return next;
}

export function sortIndicator(
	key: string,
	currentKey: string | null,
	currentDir: SortDir
): string {
	if (currentKey !== key || !currentDir) return '';
	return currentDir === 'asc' ? ' ▲' : ' ▼';
}
