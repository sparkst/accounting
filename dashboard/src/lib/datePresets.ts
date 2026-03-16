/**
 * Financial date range presets for filtering transactions.
 */

interface DateRange {
	from: string; // YYYY-MM-DD
	to: string;
}

interface DatePreset {
	label: string;
	group: string;
	range: () => DateRange;
}

function fmt(d: Date): string {
	return d.toISOString().slice(0, 10);
}

function today(): Date {
	return new Date();
}

function startOfWeek(d: Date): Date {
	const day = d.getDay();
	const diff = d.getDate() - day + (day === 0 ? -6 : 1); // Monday start
	return new Date(d.getFullYear(), d.getMonth(), diff);
}

function quarterStart(year: number, q: number): Date {
	return new Date(year, (q - 1) * 3, 1);
}

function quarterEnd(year: number, q: number): Date {
	return new Date(year, q * 3, 0);
}

function currentQuarter(d: Date): number {
	return Math.floor(d.getMonth() / 3) + 1;
}

export const DATE_PRESETS: DatePreset[] = [
	// ── Standard ──
	{
		label: 'Today',
		group: 'Standard',
		range: () => {
			const d = fmt(today());
			return { from: d, to: d };
		}
	},
	{
		label: 'Yesterday',
		group: 'Standard',
		range: () => {
			const d = new Date();
			d.setDate(d.getDate() - 1);
			const s = fmt(d);
			return { from: s, to: s };
		}
	},
	{
		label: 'This Week',
		group: 'Standard',
		range: () => {
			const now = today();
			return { from: fmt(startOfWeek(now)), to: fmt(now) };
		}
	},
	{
		label: 'Last Week',
		group: 'Standard',
		range: () => {
			const now = today();
			const thisWeekStart = startOfWeek(now);
			const lastWeekEnd = new Date(thisWeekStart);
			lastWeekEnd.setDate(lastWeekEnd.getDate() - 1);
			const lastWeekStart = startOfWeek(lastWeekEnd);
			return { from: fmt(lastWeekStart), to: fmt(lastWeekEnd) };
		}
	},
	{
		label: 'This Month',
		group: 'Standard',
		range: () => {
			const now = today();
			return {
				from: fmt(new Date(now.getFullYear(), now.getMonth(), 1)),
				to: fmt(now)
			};
		}
	},
	{
		label: 'Last Month',
		group: 'Standard',
		range: () => {
			const now = today();
			const start = new Date(now.getFullYear(), now.getMonth() - 1, 1);
			const end = new Date(now.getFullYear(), now.getMonth(), 0);
			return { from: fmt(start), to: fmt(end) };
		}
	},
	{
		label: 'This Quarter',
		group: 'Standard',
		range: () => {
			const now = today();
			const q = currentQuarter(now);
			return {
				from: fmt(quarterStart(now.getFullYear(), q)),
				to: fmt(now)
			};
		}
	},
	{
		label: 'Last Quarter',
		group: 'Standard',
		range: () => {
			const now = today();
			let q = currentQuarter(now) - 1;
			let year = now.getFullYear();
			if (q === 0) { q = 4; year--; }
			return {
				from: fmt(quarterStart(year, q)),
				to: fmt(quarterEnd(year, q))
			};
		}
	},

	// ── Tax / Fiscal ──
	{
		label: 'YTD',
		group: 'Tax / Fiscal',
		range: () => {
			const now = today();
			return {
				from: `${now.getFullYear()}-01-01`,
				to: fmt(now)
			};
		}
	},
	{
		label: 'Last Year',
		group: 'Tax / Fiscal',
		range: () => {
			const year = today().getFullYear() - 1;
			return { from: `${year}-01-01`, to: `${year}-12-31` };
		}
	},
	{
		label: 'Tax Year 2025',
		group: 'Tax / Fiscal',
		range: () => ({ from: '2025-01-01', to: '2025-12-31' })
	},
	{
		label: 'Tax Year 2024',
		group: 'Tax / Fiscal',
		range: () => ({ from: '2024-01-01', to: '2024-12-31' })
	},

	// ── B&O Filing (WA State) ──
	{
		label: 'B&O: This Month (Sparkry)',
		group: 'B&O Filing',
		range: () => {
			const now = today();
			return {
				from: fmt(new Date(now.getFullYear(), now.getMonth(), 1)),
				to: fmt(new Date(now.getFullYear(), now.getMonth() + 1, 0))
			};
		}
	},
	{
		label: 'B&O: Last Month (Sparkry)',
		group: 'B&O Filing',
		range: () => {
			const now = today();
			return {
				from: fmt(new Date(now.getFullYear(), now.getMonth() - 1, 1)),
				to: fmt(new Date(now.getFullYear(), now.getMonth(), 0))
			};
		}
	},
	{
		label: 'B&O: This Quarter (BlackLine)',
		group: 'B&O Filing',
		range: () => {
			const now = today();
			const q = currentQuarter(now);
			return {
				from: fmt(quarterStart(now.getFullYear(), q)),
				to: fmt(quarterEnd(now.getFullYear(), q))
			};
		}
	},
	{
		label: 'B&O: Last Quarter (BlackLine)',
		group: 'B&O Filing',
		range: () => {
			const now = today();
			let q = currentQuarter(now) - 1;
			let year = now.getFullYear();
			if (q === 0) { q = 4; year--; }
			return {
				from: fmt(quarterStart(year, q)),
				to: fmt(quarterEnd(year, q))
			};
		}
	},
];
