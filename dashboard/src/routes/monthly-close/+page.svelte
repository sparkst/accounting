<script lang="ts">
	import { onMount } from 'svelte';

	// ── Types ──────────────────────────────────────────────────────────────────
	interface Step {
		id: string;
		title: string;
		description: string;
		href: string;
		linkLabel: string;
	}

	// ── Constants ──────────────────────────────────────────────────────────────
	const STEPS: Step[] = [
		{
			id: 'freshness',
			title: 'Check Data Freshness',
			description: 'Verify all data sources are up to date.',
			href: '/health',
			linkLabel: 'Open Health'
		},
		{
			id: 'categorize',
			title: 'Review Uncategorized',
			description: 'Categorize all pending transactions.',
			href: '/register?status=needs_review',
			linkLabel: 'Open Register'
		},
		{
			id: 'reconcile',
			title: 'Reconcile',
			description: 'Match payouts with bank deposits.',
			href: '/reconciliation',
			linkLabel: 'Open Reconciliation'
		},
		{
			id: 'verify',
			title: 'Verify P&L',
			description: 'Review income statement for the month.',
			href: '/financials',
			linkLabel: 'Open Financials'
		},
		{
			id: 'export',
			title: 'Export for Filing',
			description: 'Download tax exports if needed.',
			href: '/tax',
			linkLabel: 'Open Tax'
		}
	];

	// ── Month helpers ──────────────────────────────────────────────────────────
	function toMonthKey(year: number, month: number): string {
		return `${year}-${String(month).padStart(2, '0')}`;
	}

	function parseMonthKey(key: string): { year: number; month: number } {
		const [y, m] = key.split('-').map(Number);
		return { year: y, month: m };
	}

	function monthLabel(year: number, month: number): string {
		return new Date(year, month - 1, 1).toLocaleString('en-US', {
			month: 'long',
			year: 'numeric'
		});
	}

	function prevMonth(year: number, month: number): { year: number; month: number } {
		if (month === 1) return { year: year - 1, month: 12 };
		return { year, month: month - 1 };
	}

	function nextMonth(year: number, month: number): { year: number; month: number } {
		if (month === 12) return { year: year + 1, month: 1 };
		return { year, month: month + 1 };
	}

	// ── State ──────────────────────────────────────────────────────────────────
	const now = new Date();
	let selectedYear = $state(now.getFullYear());
	let selectedMonth = $state(now.getMonth() + 1); // 1-based

	// Map of stepId → completed, persisted to localStorage per month
	let checked = $state<Record<string, boolean>>({});

	// The localStorage key for the selected month
	let storageKey = $derived(`close-${toMonthKey(selectedYear, selectedMonth)}`);

	// Whether we're in the current month (can't go forward past today)
	let isCurrentMonth = $derived(
		selectedYear === now.getFullYear() && selectedMonth === now.getMonth() + 1
	);

	// Completed count
	let completedCount = $derived(STEPS.filter((s) => checked[s.id]).length);
	let progressPct = $derived(Math.round((completedCount / STEPS.length) * 100));
	let allDone = $derived(completedCount === STEPS.length);

	// ── Load / save ────────────────────────────────────────────────────────────
	function loadState(key: string) {
		try {
			const raw = localStorage.getItem(key);
			if (raw) {
				const parsed = JSON.parse(raw);
				if (parsed && typeof parsed === 'object') return parsed as Record<string, boolean>;
			}
		} catch {
			// ignore parse errors
		}
		return {};
	}

	function saveState(key: string, state: Record<string, boolean>) {
		try {
			localStorage.setItem(key, JSON.stringify(state));
		} catch {
			// ignore quota errors
		}
	}

	// Re-load state whenever the selected month changes
	$effect(() => {
		const key = storageKey;
		checked = loadState(key);
	});

	// Persist whenever checked changes
	$effect(() => {
		// Read all step values to track them as reactive dependencies
		const snapshot: Record<string, boolean> = {};
		for (const step of STEPS) {
			snapshot[step.id] = checked[step.id] ?? false;
		}
		saveState(storageKey, snapshot);
	});

	onMount(() => {
		checked = loadState(storageKey);
	});

	// ── Handlers ───────────────────────────────────────────────────────────────
	function toggleStep(id: string) {
		checked = { ...checked, [id]: !checked[id] };
	}

	function goToPrev() {
		const p = prevMonth(selectedYear, selectedMonth);
		selectedYear = p.year;
		selectedMonth = p.month;
	}

	function goToNext() {
		if (isCurrentMonth) return;
		const n = nextMonth(selectedYear, selectedMonth);
		selectedYear = n.year;
		selectedMonth = n.month;
	}

	function resetMonth() {
		checked = {};
	}
</script>

<div class="container page-shell">
	<!-- ── Header ──────────────────────────────────────────────────────────── -->
	<header class="page-header">
		<div>
			<h1>Monthly Close</h1>
			<p class="page-subtitle">Step-by-step checklist to close out each month.</p>
		</div>
	</header>

	<!-- ── Month selector ─────────────────────────────────────────────────── -->
	<div class="month-nav card">
		<button
			class="btn btn-ghost month-arrow"
			onclick={goToPrev}
			aria-label="Previous month"
			title="Previous month"
		>
			<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
				<polyline points="15 18 9 12 15 6"/>
			</svg>
		</button>

		<div class="month-label-wrap">
			<span class="month-label">{monthLabel(selectedYear, selectedMonth)}</span>
			{#if isCurrentMonth}
				<span class="current-badge">Current</span>
			{/if}
		</div>

		<button
			class="btn btn-ghost month-arrow"
			onclick={goToNext}
			disabled={isCurrentMonth}
			aria-label="Next month"
			title={isCurrentMonth ? 'Already at current month' : 'Next month'}
		>
			<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
				<polyline points="9 18 15 12 9 6"/>
			</svg>
		</button>
	</div>

	<!-- ── Progress bar ───────────────────────────────────────────────────── -->
	<div class="progress-section">
		<div class="progress-header">
			<span class="progress-label">
				{completedCount} of {STEPS.length} complete
			</span>
			<span class="progress-pct">{progressPct}%</span>
		</div>
		<div class="progress-track" role="progressbar" aria-valuenow={progressPct} aria-valuemin={0} aria-valuemax={100} aria-label="Close progress">
			<div
				class="progress-fill"
				class:progress-fill-done={allDone}
				style="width: {progressPct}%"
			></div>
		</div>
	</div>

	<!-- ── All-done banner ────────────────────────────────────────────────── -->
	{#if allDone}
		<div class="done-banner" role="status">
			<span class="done-icon" aria-hidden="true">
				<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
					<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/>
					<polyline points="22 4 12 14.01 9 11.01"/>
				</svg>
			</span>
			<div>
				<strong>{monthLabel(selectedYear, selectedMonth)} is closed.</strong>
				<span class="done-sub">All steps complete. Great work.</span>
			</div>
			<button class="btn btn-ghost btn-sm reset-btn" onclick={resetMonth}>
				Reset
			</button>
		</div>
	{/if}

	<!-- ── Checklist ──────────────────────────────────────────────────────── -->
	<ol class="checklist" aria-label="Monthly close steps">
		{#each STEPS as step, i (step.id)}
			{@const done = checked[step.id] ?? false}
			<li class="step-item card" class:step-done={done}>
				<div class="step-number" aria-hidden="true">{i + 1}</div>

				<button
					class="step-check"
					class:step-check-done={done}
					onclick={() => toggleStep(step.id)}
					aria-label={done ? `Mark "${step.title}" incomplete` : `Mark "${step.title}" complete`}
					aria-pressed={done}
				>
					{#if done}
						<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
							<polyline points="20 6 9 17 4 12"/>
						</svg>
					{/if}
				</button>

				<div class="step-content">
					<div class="step-title" class:step-title-done={done}>{step.title}</div>
					<div class="step-description">{step.description}</div>
				</div>

				<a
					href={step.href}
					class="btn btn-ghost btn-sm step-link"
					aria-label="{step.linkLabel} for {step.title}"
				>
					{step.linkLabel}
					<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
						<line x1="7" y1="17" x2="17" y2="7"/>
						<polyline points="7 7 17 7 17 17"/>
					</svg>
				</a>
			</li>
		{/each}
	</ol>
</div>

<style>
	.page-shell {
		padding-top: 32px;
		padding-bottom: 64px;
		max-width: 680px;
	}

	/* ── Header ─────────────────────────────────────────────────────────────── */
	.page-header {
		margin-bottom: 24px;
	}

	.page-subtitle {
		margin-top: 4px;
		color: var(--text-muted);
		font-size: 0.9rem;
	}

	/* ── Month navigator ─────────────────────────────────────────────────────── */
	.month-nav {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		padding: 12px 16px;
		margin-bottom: 20px;
	}

	.month-arrow {
		padding: 5px 10px;
		flex-shrink: 0;
	}

	.month-label-wrap {
		display: flex;
		align-items: center;
		gap: 8px;
		flex: 1;
		justify-content: center;
	}

	.month-label {
		font-size: 1rem;
		font-weight: 600;
		letter-spacing: -0.2px;
	}

	.current-badge {
		font-size: 0.7rem;
		font-weight: 600;
		padding: 2px 7px;
		border-radius: 999px;
		background: var(--blue-500);
		color: #fff;
	}

	/* ── Progress ────────────────────────────────────────────────────────────── */
	.progress-section {
		margin-bottom: 20px;
	}

	.progress-header {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
		margin-bottom: 6px;
	}

	.progress-label {
		font-size: 0.8rem;
		color: var(--text-muted);
	}

	.progress-pct {
		font-size: 0.8rem;
		font-weight: 600;
		font-variant-numeric: tabular-nums;
		color: var(--text-muted);
	}

	.progress-track {
		height: 6px;
		background: var(--gray-200);
		border-radius: 999px;
		overflow: hidden;
	}

	.progress-fill {
		height: 100%;
		background: var(--blue-500);
		border-radius: 999px;
		transition: width 0.3s ease, background 0.3s ease;
	}

	.progress-fill-done {
		background: var(--green-500);
	}

	/* ── Done banner ─────────────────────────────────────────────────────────── */
	.done-banner {
		display: flex;
		align-items: center;
		gap: 12px;
		padding: 14px 18px;
		background: var(--green-100);
		border: 1px solid var(--green-500);
		border-radius: var(--radius);
		color: var(--green-700);
		margin-bottom: 24px;
	}

	.done-icon {
		display: flex;
		align-items: center;
		flex-shrink: 0;
		color: var(--green-600);
	}

	.done-banner > div {
		flex: 1;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.done-sub {
		font-size: 0.8rem;
		opacity: 0.8;
	}

	.reset-btn {
		flex-shrink: 0;
		font-size: 0.78rem;
		padding: 4px 10px;
		color: var(--green-700);
		border-color: var(--green-500);
	}

	.reset-btn:hover:not(:disabled) {
		background: var(--green-100);
	}

	/* ── Checklist ───────────────────────────────────────────────────────────── */
	.checklist {
		list-style: none;
		display: flex;
		flex-direction: column;
		gap: 10px;
	}

	.step-item {
		display: flex;
		align-items: center;
		gap: 14px;
		padding: 16px 18px;
		transition: border-color 0.15s, opacity 0.15s;
	}

	.step-done {
		opacity: 0.6;
	}

	.step-number {
		font-size: 0.7rem;
		font-weight: 700;
		color: var(--text-muted);
		width: 16px;
		text-align: center;
		flex-shrink: 0;
	}

	/* ── Checkbox ────────────────────────────────────────────────────────────── */
	.step-check {
		width: 22px;
		height: 22px;
		border: 2px solid var(--gray-300);
		border-radius: 6px;
		background: transparent;
		cursor: pointer;
		display: flex;
		align-items: center;
		justify-content: center;
		flex-shrink: 0;
		transition: border-color 0.15s, background 0.15s;
		color: #fff;
	}

	.step-check:hover {
		border-color: var(--blue-500);
		background: var(--gray-100);
	}

	.step-check-done {
		border-color: var(--green-500);
		background: var(--green-500);
	}

	.step-check-done:hover {
		border-color: var(--green-600);
		background: var(--green-600);
	}

	/* ── Step body ───────────────────────────────────────────────────────────── */
	.step-content {
		flex: 1;
		min-width: 0;
	}

	.step-title {
		font-size: 0.9rem;
		font-weight: 600;
		color: var(--text);
		margin-bottom: 2px;
		transition: color 0.15s;
	}

	.step-title-done {
		color: var(--text-muted);
		text-decoration: line-through;
	}

	.step-description {
		font-size: 0.8rem;
		color: var(--text-muted);
	}

	/* ── Link button ─────────────────────────────────────────────────────────── */
	.step-link {
		flex-shrink: 0;
		font-size: 0.8rem;
		padding: 5px 12px;
		gap: 4px;
	}

	/* ── Responsive ──────────────────────────────────────────────────────────── */
	@media (max-width: 500px) {
		.step-item {
			flex-wrap: wrap;
			gap: 10px;
		}

		.step-number {
			display: none;
		}

		.step-link {
			margin-left: auto;
		}
	}
</style>
