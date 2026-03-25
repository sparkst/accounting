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
	const ENTITIES = [
		{ value: 'sparkry',   label: 'Sparkry AI' },
		{ value: 'blackline', label: 'BlackLine MTB' },
		{ value: 'personal',  label: 'Personal' }
	] as const;
	type EntityValue = typeof ENTITIES[number]['value'];

	const CURRENT_YEAR = new Date().getFullYear();
	const YEARS: number[] = [];
	for (let y = 2024; y <= CURRENT_YEAR; y++) YEARS.push(y);

	// ── State ──────────────────────────────────────────────────────────────────
	let selectedEntity = $state<EntityValue>('sparkry');
	let selectedYear   = $state(CURRENT_YEAR - 1); // default to prior year (most recent closeable)

	// Map of stepId → completed, persisted to localStorage per entity+year
	let checked = $state<Record<string, boolean>>({});

	// The localStorage key for the selected entity+year
	let storageKey = $derived(`annual-close-${selectedEntity}-${selectedYear}`);

	// ── Steps ──────────────────────────────────────────────────────────────────
	let STEPS = $derived.by((): Step[] => [
		{
			id: 'monthly-closed',
			title: 'Verify All Months Closed',
			description: 'Confirm each month has been reconciled and closed via Monthly Close.',
			href: '/monthly-close',
			linkLabel: 'Open Monthly Close'
		},
		{
			id: 'review-1099',
			title: 'Review 1099 Documentation',
			description: `Verify all 1099 payers are documented and amounts match income records for ${selectedYear}.`,
			href: '/tax#tracking-1099',
			linkLabel: 'Open 1099 Tracking'
		},
		{
			id: 'categorize',
			title: 'Verify All Transactions Categorized',
			description: 'Confirm no transactions remain in needs_review status.',
			href: `/register?status=needs_review`,
			linkLabel: 'Open Register'
		},
		{
			id: 'pnl',
			title: 'Review P&L for Accuracy',
			description: `Verify income, expenses, and net profit for ${selectedYear} are correct.`,
			href: '/financials',
			linkLabel: 'Open Financials'
		},
		{
			id: 'tax-export',
			title: 'Run Tax Exports',
			description: `Download FreeTaxUSA${selectedEntity === 'blackline' ? ', TaxAct,' : ''} and B&O exports for ${selectedYear}.`,
			href: `/tax?year=${selectedYear}`,
			linkLabel: 'Open Tax'
		},
		{
			id: 'bno',
			title: 'File B&O Returns',
			description: 'Submit Washington State B&O tax returns via the DOR portal.',
			href: '/tax',
			linkLabel: 'Open Tax'
		},
		{
			id: 'est-tax',
			title: 'Record Estimated Tax Payments',
			description: `Log any estimated tax payments made for ${selectedYear} in the payment log.`,
			href: '/tax',
			linkLabel: 'Open Tax'
		}
	]);

	// ── Derived progress ───────────────────────────────────────────────────────
	let completedCount = $derived(STEPS.filter((s) => checked[s.id]).length);
	let progressPct    = $derived(Math.round((completedCount / STEPS.length) * 100));
	let allDone        = $derived(completedCount === STEPS.length);

	// ── Load / save ────────────────────────────────────────────────────────────
	function loadState(key: string): Record<string, boolean> {
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

	// Re-load checklist state whenever entity or year changes
	$effect(() => {
		const key = storageKey;
		checked = loadState(key);
	});

	// Persist whenever checked changes
	$effect(() => {
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

	function resetYear() {
		checked = {};
	}
</script>

<div class="container page-shell">
	<!-- ── Header ──────────────────────────────────────────────────────────── -->
	<header class="page-header">
		<div>
			<h1>Annual Close</h1>
			<p class="page-subtitle">Year-end checklist covering taxes, exports, and filing.</p>
		</div>
	</header>

	<!-- ── Controls ───────────────────────────────────────────────────────── -->
	<div class="controls-row card">
		<!-- Entity tabs -->
		<div class="entity-tabs" role="tablist" aria-label="Entity">
			{#each ENTITIES as ent (ent.value)}
				<button
					role="tab"
					aria-selected={selectedEntity === ent.value}
					class="entity-tab"
					class:active={selectedEntity === ent.value}
					onclick={() => { selectedEntity = ent.value; }}
				>
					{ent.label}
				</button>
			{/each}
		</div>

		<!-- Year selector -->
		<div class="year-wrap">
			<label class="year-label" for="year-select">Year</label>
			<select
				id="year-select"
				class="year-select"
				bind:value={selectedYear}
				aria-label="Tax year"
			>
				{#each YEARS as y (y)}
					<option value={y}>{y}</option>
				{/each}
			</select>
		</div>
	</div>

	<!-- ── Progress bar ───────────────────────────────────────────────────── -->
	<div class="progress-section">
		<div class="progress-header">
			<span class="progress-label">
				{completedCount} of {STEPS.length} complete
			</span>
			<span class="progress-pct">{progressPct}%</span>
		</div>
		<div
			class="progress-track"
			role="progressbar"
			aria-valuenow={progressPct}
			aria-valuemin={0}
			aria-valuemax={100}
			aria-label="Annual close progress"
		>
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
				<strong>{selectedYear} annual close complete.</strong>
				<span class="done-sub">All steps finished. Great work.</span>
			</div>
			<button class="btn btn-ghost btn-sm reset-btn" onclick={resetYear}>
				Reset
			</button>
		</div>
	{/if}

	<!-- ── Checklist ──────────────────────────────────────────────────────── -->
	<ol class="checklist" aria-label="Annual close steps">
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
		max-width: 720px;
	}

	/* ── Header ──────────────────────────────────────────────────────────────── */
	.page-header {
		margin-bottom: 24px;
	}

	.page-subtitle {
		margin-top: 4px;
		color: var(--text-muted);
		font-size: 0.9rem;
	}

	/* ── Controls row ────────────────────────────────────────────────────────── */
	.controls-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 16px;
		padding: 12px 16px;
		margin-bottom: 20px;
		flex-wrap: wrap;
	}

	/* ── Entity tabs ─────────────────────────────────────────────────────────── */
	.entity-tabs {
		display: flex;
		gap: 2px;
	}

	.entity-tab {
		padding: 5px 14px;
		background: none;
		border: 1px solid transparent;
		border-radius: var(--radius-sm);
		font-family: var(--font);
		font-size: 0.8rem;
		font-weight: 500;
		color: var(--text-muted);
		cursor: pointer;
		transition: color 0.12s, background 0.12s, border-color 0.12s;
		white-space: nowrap;
	}

	.entity-tab:hover {
		color: var(--text);
		background: var(--gray-100);
	}

	.entity-tab.active {
		color: var(--text);
		background: var(--gray-200);
		border-color: var(--gray-300);
	}

	/* ── Year selector ───────────────────────────────────────────────────────── */
	.year-wrap {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.year-label {
		font-size: 0.8rem;
		color: var(--text-muted);
		white-space: nowrap;
	}

	.year-select {
		min-width: 80px;
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
		.controls-row {
			flex-direction: column;
			align-items: flex-start;
		}

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
