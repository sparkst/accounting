<script lang="ts">
	import { fetchTaxSummary, downloadExport } from '$lib/api';
	import type { TaxSummary } from '$lib/api';

	// ── Constants ─────────────────────────────────────────────────────────────

	const BNO_RATE = 0.015; // 1.5% B&O service/other activities rate
	const CURRENT_YEAR = new Date().getFullYear();

	type Entity = 'sparkry' | 'blackline';
	type FilingFrequency = 'monthly' | 'quarterly';

	// Entity filing frequencies per WA DOR requirements
	const ENTITY_FREQUENCY: Record<Entity, FilingFrequency> = {
		sparkry: 'monthly',
		blackline: 'quarterly',
	};

	// Quarters: Q1=Jan-Mar, Q2=Apr-Jun, Q3=Jul-Sep, Q4=Oct-Dec
	const QUARTERS = [
		{ label: 'Q1 (Jan–Mar)', value: 'Q1', months: ['01', '02', '03'] },
		{ label: 'Q2 (Apr–Jun)', value: 'Q2', months: ['04', '05', '06'] },
		{ label: 'Q3 (Jul–Sep)', value: 'Q3', months: ['07', '08', '09'] },
		{ label: 'Q4 (Oct–Dec)', value: 'Q4', months: ['10', '11', '12'] },
	];

	const MONTHS = [
		{ label: 'January', value: '01' },
		{ label: 'February', value: '02' },
		{ label: 'March', value: '03' },
		{ label: 'April', value: '04' },
		{ label: 'May', value: '05' },
		{ label: 'June', value: '06' },
		{ label: 'July', value: '07' },
		{ label: 'August', value: '08' },
		{ label: 'September', value: '09' },
		{ label: 'October', value: '10' },
		{ label: 'November', value: '11' },
		{ label: 'December', value: '12' },
	];

	// ── Step state ────────────────────────────────────────────────────────────

	// Step 1: Entity
	let selectedEntity = $state<Entity | ''>('');

	// Step 2: Period
	let selectedYear = $state<number>(CURRENT_YEAR);
	let selectedMonth = $state<string>('01');   // for monthly filers
	let selectedQuarter = $state<string>('Q1'); // for quarterly filers

	// Step 3+: Summary from API
	let loading = $state(false);
	let loadError = $state('');
	let summary = $state<TaxSummary | null>(null);

	// Step tracker (1=entity, 2=period, 3=summary)
	let step = $state(1);

	// Download / Mark as filed
	let downloading = $state(false);
	let downloadError = $state('');

	// ── Derived ───────────────────────────────────────────────────────────────

	let frequency = $derived<FilingFrequency | null>(
		selectedEntity ? ENTITY_FREQUENCY[selectedEntity] : null
	);

	/** The filing period key used for localStorage, e.g. "sparkry-2025-03" or "blackline-2025-Q2" */
	let periodKey = $derived(
		selectedEntity
			? `bno-filed:${selectedEntity}:${
				frequency === 'monthly'
					? `${selectedYear}-${selectedMonth}`
					: `${selectedYear}-${selectedQuarter}`
			}`
			: ''
	);

	/** Gross income from the tax summary (income categories only) */
	let grossReceipts = $derived<number>(
		summary
			? summary.line_items
				.filter((li) => li.is_income && !li.is_reimbursable)
				.reduce((sum, li) => sum + li.total, 0)
			: 0
	);

	let bnoTaxDue = $derived<number>(
		Math.round(grossReceipts * BNO_RATE * 100) / 100
	);

	// ── Filing history (localStorage) ─────────────────────────────────────────

	function getFilingHistory(): Record<string, string> {
		try {
			return JSON.parse(localStorage.getItem('bno-filing-history') ?? '{}');
		} catch {
			return {};
		}
	}

	function saveFilingHistory(history: Record<string, string>): void {
		localStorage.setItem('bno-filing-history', JSON.stringify(history));
	}

	let filedAt = $state<string | null>(null);

	function refreshFiledAt() {
		if (!periodKey) { filedAt = null; return; }
		const h = getFilingHistory();
		filedAt = h[periodKey] ?? null;
	}

	function markFiled() {
		if (!periodKey) return;
		const h = getFilingHistory();
		h[periodKey] = new Date().toISOString();
		saveFilingHistory(h);
		filedAt = h[periodKey];
	}

	function unmarkFiled() {
		if (!periodKey) return;
		const h = getFilingHistory();
		delete h[periodKey];
		saveFilingHistory(h);
		filedAt = null;
	}

	/** All filed periods for the current entity, for history display */
	let filingHistory = $state<{ period: string; filedAt: string }[]>([]);

	function refreshHistory() {
		if (!selectedEntity) { filingHistory = []; return; }
		const h = getFilingHistory();
		const prefix = `bno-filed:${selectedEntity}:`;
		filingHistory = Object.entries(h)
			.filter(([k]) => k.startsWith(prefix))
			.map(([k, v]) => ({ period: k.replace(prefix, ''), filedAt: v }))
			.sort((a, b) => b.period.localeCompare(a.period));
	}

	// ── Step transitions ──────────────────────────────────────────────────────

	function goToStep2() {
		if (!selectedEntity) return;
		step = 2;
		summary = null;
		loadError = '';
	}

	async function goToStep3() {
		if (!selectedEntity) return;
		loading = true;
		loadError = '';
		summary = null;
		step = 3;

		try {
			const s = await fetchTaxSummary(selectedEntity, selectedYear);
			summary = s;
		} catch (err) {
			loadError = err instanceof Error ? err.message : 'Failed to load summary';
		} finally {
			loading = false;
		}

		refreshFiledAt();
		refreshHistory();
	}

	async function handleDownload() {
		if (!selectedEntity || downloading) return;
		downloading = true;
		downloadError = '';
		const periodLabel = frequency === 'monthly'
			? `${selectedYear}-${selectedMonth}`
			: `${selectedYear}-${selectedQuarter}`;
		const filename = `bno_${selectedEntity}_${periodLabel}.csv`;
		try {
			await downloadExport('bno', selectedEntity, selectedYear, filename);
		} catch (err) {
			downloadError = err instanceof Error ? err.message : 'Download failed';
		} finally {
			downloading = false;
		}
	}

	// ── Formatting helpers ────────────────────────────────────────────────────

	function formatCurrency(n: number): string {
		return new Intl.NumberFormat('en-US', {
			style: 'currency',
			currency: 'USD',
			minimumFractionDigits: 2,
		}).format(n);
	}

	function formatDate(iso: string): string {
		try {
			return new Intl.DateTimeFormat('en-US', {
				month: 'short', day: 'numeric', year: 'numeric',
				hour: 'numeric', minute: '2-digit',
			}).format(new Date(iso));
		} catch {
			return iso;
		}
	}

	/** WA DOR account ID for each entity (from project knowledge) */
	function dorAccount(entity: string): string {
		if (entity === 'sparkry') return '605-965-107';
		if (entity === 'blackline') return '605-922-410';
		return '—';
	}

	/** Human-readable period label */
	let periodLabel = $derived(
		!selectedEntity
			? ''
			: frequency === 'monthly'
				? `${MONTHS.find((x) => x.value === selectedMonth)?.label ?? selectedMonth} ${selectedYear}`
				: `${QUARTERS.find((x) => x.value === selectedQuarter)?.label ?? selectedQuarter} ${selectedYear}`
	);
</script>

<svelte:head>
	<title>B&O Filing Wizard</title>
</svelte:head>

<div class="container page-wrap">
	<div class="page-header">
		<h1 class="page-title">B&O Filing Wizard</h1>
		<p class="page-subtitle">WA Department of Revenue — Business &amp; Occupation Tax</p>
	</div>

	<!-- Step indicators -->
	<div class="steps-bar">
		<div class="step" class:step-active={step === 1} class:step-done={step > 1}>
			<span class="step-num">{step > 1 ? '✓' : '1'}</span>
			<span class="step-label">Entity</span>
		</div>
		<div class="step-connector" class:step-connector-done={step > 1}></div>
		<div class="step" class:step-active={step === 2} class:step-done={step > 2}>
			<span class="step-num">{step > 2 ? '✓' : '2'}</span>
			<span class="step-label">Period</span>
		</div>
		<div class="step-connector" class:step-connector-done={step > 2}></div>
		<div class="step" class:step-active={step === 3}>
			<span class="step-num">3</span>
			<span class="step-label">Summary</span>
		</div>
	</div>

	<!-- Step 1: Entity -->
	{#if step === 1}
		<div class="card wizard-card">
			<h2 class="wizard-step-title">Select entity</h2>
			<p class="wizard-hint">Sparkry files monthly; BlackLine files quarterly.</p>

			<div class="entity-grid">
				<button
					class="entity-option"
					class:entity-selected={selectedEntity === 'sparkry'}
					type="button"
					onclick={() => { selectedEntity = 'sparkry'; }}
				>
					<span class="entity-name">Sparkry AI LLC</span>
					<span class="entity-meta">Monthly · DOR {dorAccount('sparkry')}</span>
				</button>
				<button
					class="entity-option"
					class:entity-selected={selectedEntity === 'blackline'}
					type="button"
					onclick={() => { selectedEntity = 'blackline'; }}
				>
					<span class="entity-name">BlackLine MTB LLC</span>
					<span class="entity-meta">Quarterly · DOR {dorAccount('blackline')}</span>
				</button>
			</div>

			<div class="wizard-footer">
				<button
					class="btn btn-primary"
					type="button"
					onclick={goToStep2}
					disabled={!selectedEntity}
				>
					Continue
				</button>
			</div>
		</div>
	{/if}

	<!-- Step 2: Period -->
	{#if step === 2}
		<div class="card wizard-card">
			<h2 class="wizard-step-title">Select filing period</h2>
			<p class="wizard-hint">
				{selectedEntity === 'sparkry' ? 'Sparkry AI LLC — Monthly filer' : 'BlackLine MTB LLC — Quarterly filer'}
			</p>

			<div class="period-fields">
				<div class="field-group">
					<label class="field-label" for="bno-year">Tax year</label>
					<select id="bno-year" class="field-select" bind:value={selectedYear}>
						{#each [CURRENT_YEAR, CURRENT_YEAR - 1, CURRENT_YEAR - 2] as yr}
							<option value={yr}>{yr}</option>
						{/each}
					</select>
				</div>

				{#if frequency === 'monthly'}
					<div class="field-group">
						<label class="field-label" for="bno-month">Month</label>
						<select id="bno-month" class="field-select" bind:value={selectedMonth}>
							{#each MONTHS as m}
								<option value={m.value}>{m.label}</option>
							{/each}
						</select>
					</div>
				{:else}
					<div class="field-group">
						<label class="field-label" for="bno-quarter">Quarter</label>
						<select id="bno-quarter" class="field-select" bind:value={selectedQuarter}>
							{#each QUARTERS as q}
								<option value={q.value}>{q.label}</option>
							{/each}
						</select>
					</div>
				{/if}
			</div>

			<div class="wizard-footer">
				<button class="btn btn-ghost" type="button" onclick={() => { step = 1; }}>Back</button>
				<button class="btn btn-primary" type="button" onclick={goToStep3}>
					Load summary
				</button>
			</div>
		</div>
	{/if}

	<!-- Step 3: Summary -->
	{#if step === 3}
		<div class="card wizard-card">
			<div class="summary-header">
				<div>
					<h2 class="wizard-step-title" style="margin-bottom: 2px;">
						{selectedEntity === 'sparkry' ? 'Sparkry AI LLC' : 'BlackLine MTB LLC'}
					</h2>
					<p class="summary-period">{periodLabel}</p>
				</div>
				{#if filedAt}
					<span class="filed-badge">Filed {formatDate(filedAt)}</span>
				{/if}
			</div>

			{#if loading}
				<div class="loading-wrap">
					<span class="spinner" aria-label="Loading…"></span>
					<span class="loading-text">Loading tax summary…</span>
				</div>
			{:else if loadError}
				<p class="error-msg">{loadError}</p>
			{:else if summary}
				<!-- Gross receipts breakdown -->
				<div class="summary-section">
					<h3 class="summary-section-title">Gross Receipts</h3>
					<table class="summary-table">
						<thead>
							<tr>
								<th>Category</th>
								<th class="col-right">Amount</th>
							</tr>
						</thead>
						<tbody>
							{#each summary.line_items.filter((li) => li.is_income && !li.is_reimbursable) as li}
								<tr>
									<td>{li.tax_category.replace(/_/g, ' ')}</td>
									<td class="col-right">{formatCurrency(li.total)}</td>
								</tr>
							{:else}
								<tr>
									<td colspan="2" class="col-empty">No income transactions for this period.</td>
								</tr>
							{/each}
						</tbody>
						<tfoot>
							<tr class="summary-total-row">
								<td>Total Gross Receipts</td>
								<td class="col-right">{formatCurrency(grossReceipts)}</td>
							</tr>
						</tfoot>
					</table>
				</div>

				<!-- B&O tax calculation -->
				<div class="bno-calc-box">
					<div class="bno-calc-row">
						<span class="bno-calc-label">Gross Receipts</span>
						<span class="bno-calc-value">{formatCurrency(grossReceipts)}</span>
					</div>
					<div class="bno-calc-row">
						<span class="bno-calc-label">B&O Rate (Service &amp; Other)</span>
						<span class="bno-calc-value">{(BNO_RATE * 100).toFixed(1)}%</span>
					</div>
					<div class="bno-calc-divider"></div>
					<div class="bno-calc-row bno-total-row">
						<span class="bno-calc-label">B&O Tax Due</span>
						<span class="bno-total-value">{formatCurrency(bnoTaxDue)}</span>
					</div>
					<p class="bno-dor-note">
						DOR Account: <strong>{dorAccount(selectedEntity)}</strong>
						&nbsp;·&nbsp; File at <a href="https://dor.wa.gov" target="_blank" rel="noopener">dor.wa.gov</a>
					</p>
				</div>

				<!-- Readiness warning -->
				{#if summary.readiness.readiness_pct < 100}
					<div class="readiness-warn">
						<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
						{summary.readiness.readiness_pct.toFixed(0)}% of transactions confirmed —
						{summary.readiness.unconfirmed_count} unconfirmed may affect gross receipts total.
					</div>
				{/if}

				<!-- Actions -->
				<div class="action-row">
					<button
						class="btn btn-secondary"
						type="button"
						onclick={handleDownload}
						disabled={downloading}
						title="Download WA DOR upload format CSV"
					>
						{downloading ? 'Downloading…' : 'Download DOR Format'}
					</button>

					{#if filedAt}
						<button class="btn btn-ghost" type="button" onclick={unmarkFiled}>
							Unmark Filed
						</button>
					{:else}
						<button class="btn btn-primary" type="button" onclick={markFiled}>
							Mark as Filed
						</button>
					{/if}
				</div>

				{#if downloadError}
					<p class="error-msg" style="margin-top: 8px;">{downloadError}</p>
				{/if}
			{/if}

			<div class="wizard-footer" style="margin-top: 24px; border-top: 1px solid var(--border); padding-top: 16px;">
				<button class="btn btn-ghost" type="button" onclick={() => { step = 2; summary = null; }}>
					Back
				</button>
			</div>
		</div>

		<!-- Filing history -->
		{#if filingHistory.length > 0}
			<div class="card history-card">
				<h3 class="history-title">Filing History — {selectedEntity === 'sparkry' ? 'Sparkry AI LLC' : 'BlackLine MTB LLC'}</h3>
				<table class="history-table">
					<thead>
						<tr>
							<th>Period</th>
							<th>Filed</th>
						</tr>
					</thead>
					<tbody>
						{#each filingHistory as entry}
							<tr>
								<td class="history-period">{entry.period}</td>
								<td class="history-filed">{formatDate(entry.filedAt)}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}
	{/if}
</div>

<style>
	.page-wrap {
		padding-top: 32px;
		padding-bottom: 48px;
		max-width: 720px;
	}

	.page-header {
		margin-bottom: 28px;
	}

	.page-title {
		font-size: 1.6rem;
		font-weight: 700;
		margin: 0 0 4px;
		letter-spacing: -.5px;
	}

	.page-subtitle {
		font-size: .875rem;
		color: var(--text-muted);
		margin: 0;
	}

	/* ── Steps bar ─────────────────────────────────────────────────────────── */
	.steps-bar {
		display: flex;
		align-items: center;
		gap: 0;
		margin-bottom: 28px;
	}

	.step {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.step-num {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 26px;
		height: 26px;
		border-radius: 50%;
		font-size: .8rem;
		font-weight: 700;
		background: var(--gray-200);
		color: var(--text-muted);
		flex-shrink: 0;
		transition: background .15s, color .15s;
	}

	.step-label {
		font-size: .82rem;
		font-weight: 500;
		color: var(--text-muted);
		transition: color .15s;
	}

	.step-active .step-num {
		background: var(--gray-900);
		color: #fff;
	}

	.step-active .step-label {
		color: var(--text);
	}

	.step-done .step-num {
		background: var(--green-600);
		color: #fff;
	}

	.step-connector {
		flex: 1;
		height: 1px;
		background: var(--gray-200);
		margin: 0 12px;
		transition: background .15s;
	}

	.step-connector-done {
		background: var(--green-600);
	}

	/* ── Wizard card ───────────────────────────────────────────────────────── */
	.wizard-card {
		padding: 28px;
		margin-bottom: 20px;
	}

	.wizard-step-title {
		font-size: 1.1rem;
		font-weight: 600;
		margin: 0 0 6px;
	}

	.wizard-hint {
		font-size: .82rem;
		color: var(--text-muted);
		margin: 0 0 20px;
	}

	.wizard-footer {
		display: flex;
		align-items: center;
		gap: 10px;
		margin-top: 24px;
	}

	/* ── Entity selector ───────────────────────────────────────────────────── */
	.entity-grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 12px;
	}

	.entity-option {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 4px;
		padding: 16px 18px;
		border: 2px solid var(--border);
		border-radius: var(--radius);
		background: var(--surface);
		cursor: pointer;
		font-family: var(--font);
		text-align: left;
		transition: border-color .12s, background .12s;
	}

	.entity-option:hover {
		border-color: var(--gray-400);
		background: var(--gray-50);
	}

	.entity-selected {
		border-color: var(--gray-900) !important;
		background: var(--gray-50) !important;
	}

	.entity-name {
		font-size: .9rem;
		font-weight: 600;
		color: var(--text);
	}

	.entity-meta {
		font-size: .72rem;
		color: var(--text-muted);
	}

	/* ── Period fields ─────────────────────────────────────────────────────── */
	.period-fields {
		display: flex;
		gap: 16px;
		flex-wrap: wrap;
	}

	.field-group {
		display: flex;
		flex-direction: column;
		gap: 6px;
		flex: 1;
		min-width: 120px;
	}

	.field-label {
		font-size: .7rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: .06em;
		color: var(--text-muted);
	}

	.field-select {
		width: 100%;
	}

	/* ── Summary ───────────────────────────────────────────────────────────── */
	.summary-header {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: 12px;
		margin-bottom: 20px;
	}

	.summary-period {
		font-size: .875rem;
		color: var(--text-muted);
		margin: 0;
	}

	.filed-badge {
		display: inline-flex;
		align-items: center;
		padding: 4px 10px;
		background: var(--green-600);
		color: #fff;
		border-radius: 999px;
		font-size: .72rem;
		font-weight: 600;
		flex-shrink: 0;
		white-space: nowrap;
	}

	.summary-section {
		margin-bottom: 20px;
	}

	.summary-section-title {
		font-size: .8rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: .06em;
		color: var(--text-muted);
		margin: 0 0 10px;
	}

	.summary-table {
		width: 100%;
		border-collapse: collapse;
		font-size: .85rem;
	}

	.summary-table th {
		font-size: .7rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: .05em;
		color: var(--text-muted);
		padding: 6px 10px;
		border-bottom: 1px solid var(--border);
		text-align: left;
	}

	.summary-table td {
		padding: 8px 10px;
		border-bottom: 1px solid var(--border);
		color: var(--text);
	}

	.summary-table tfoot td {
		font-weight: 700;
		border-bottom: none;
		border-top: 2px solid var(--border);
	}

	.col-right {
		text-align: right;
		font-variant-numeric: tabular-nums;
	}

	.col-empty {
		color: var(--text-muted);
		font-style: italic;
	}

	.summary-total-row td {
		font-weight: 700;
	}

	/* ── B&O Calc box ──────────────────────────────────────────────────────── */
	.bno-calc-box {
		background: var(--gray-50);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		padding: 18px 20px;
		margin-bottom: 16px;
	}

	.bno-calc-row {
		display: flex;
		justify-content: space-between;
		align-items: center;
		font-size: .875rem;
		padding: 4px 0;
	}

	.bno-calc-label {
		color: var(--text-muted);
	}

	.bno-calc-value {
		font-variant-numeric: tabular-nums;
		font-weight: 500;
		color: var(--text);
	}

	.bno-calc-divider {
		height: 1px;
		background: var(--border);
		margin: 10px 0;
	}

	.bno-total-row {
		margin-top: 4px;
	}

	.bno-total-row .bno-calc-label {
		font-weight: 700;
		color: var(--text);
		font-size: .95rem;
	}

	.bno-total-value {
		font-size: 1.35rem;
		font-weight: 700;
		font-variant-numeric: tabular-nums;
		color: var(--text);
	}

	.bno-dor-note {
		font-size: .75rem;
		color: var(--text-muted);
		margin: 12px 0 0;
	}

	.bno-dor-note a {
		color: var(--gray-600);
	}

	/* ── Readiness warning ─────────────────────────────────────────────────── */
	.readiness-warn {
		display: flex;
		align-items: center;
		gap: 8px;
		font-size: .8rem;
		color: var(--amber-700, #92400e);
		background: var(--amber-50, #fffbeb);
		border: 1px solid var(--amber-200, #fde68a);
		border-radius: var(--radius-sm);
		padding: 10px 14px;
		margin-bottom: 16px;
	}

	/* ── Action row ────────────────────────────────────────────────────────── */
	.action-row {
		display: flex;
		gap: 10px;
		align-items: center;
		flex-wrap: wrap;
	}

	/* ── Loading ───────────────────────────────────────────────────────────── */
	.loading-wrap {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 24px 0;
		color: var(--text-muted);
	}

	.spinner {
		display: inline-block;
		width: 16px;
		height: 16px;
		border: 2px solid var(--gray-300);
		border-top-color: var(--gray-600);
		border-radius: 50%;
		animation: spin .8s linear infinite;
		flex-shrink: 0;
	}

	@keyframes spin { to { transform: rotate(360deg); } }

	.loading-text {
		font-size: .875rem;
	}

	.error-msg {
		color: var(--red-600);
		font-size: .85rem;
	}

	/* ── History card ──────────────────────────────────────────────────────── */
	.history-card {
		padding: 22px 28px;
	}

	.history-title {
		font-size: .85rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: .06em;
		color: var(--text-muted);
		margin: 0 0 14px;
	}

	.history-table {
		width: 100%;
		border-collapse: collapse;
		font-size: .85rem;
	}

	.history-table th {
		font-size: .7rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: .05em;
		color: var(--text-muted);
		padding: 6px 10px;
		border-bottom: 1px solid var(--border);
		text-align: left;
	}

	.history-table td {
		padding: 8px 10px;
		border-bottom: 1px solid var(--border);
	}

	.history-period {
		font-family: var(--font-mono);
		font-size: .8rem;
		font-weight: 600;
	}

	.history-filed {
		color: var(--text-muted);
		font-size: .8rem;
	}
</style>
