<script lang="ts">
	import { onMount } from 'svelte';
	import {
		fetchBankCsvConfigs,
		previewBankCsv,
		commitBankCsv,
		importBrokerageCsv
	} from '$lib/api';
	import type {
		BankCsvConfig,
		BankCsvPreview,
		BankCsvCommitResult,
		BrokerageCsvResult
	} from '$lib/api';

	// ── State ─────────────────────────────────────────────────────────────────

	type FileType = 'bank' | 'brokerage' | null;
	type Phase = 'idle' | 'preview' | 'importing' | 'done' | 'error';

	let dragOver = $state(false);
	let file = $state<File | null>(null);
	let fileType = $state<FileType>(null);
	let phase = $state<Phase>('idle');
	let errorMsg = $state('');

	// Bank CSV
	let configs = $state<BankCsvConfig[]>([]);
	let selectedBank = $state('');
	let preview = $state<BankCsvPreview | null>(null);
	let previewLoading = $state(false);
	let bankResult = $state<BankCsvCommitResult | null>(null);

	// Brokerage CSV
	let brokerageResult = $state<BrokerageCsvResult | null>(null);

	let fileInput: HTMLInputElement;

	// ── Lifecycle ─────────────────────────────────────────────────────────────

	onMount(() => {
		fetchBankCsvConfigs()
			.then((c) => (configs = c))
			.catch(() => {
				// Not fatal — user can still type a bank name or use auto-detect
			});
	});

	// ── File type detection ────────────────────────────────────────────────────

	function detectFileType(f: File): FileType {
		const name = f.name.toLowerCase();
		if (name.includes('brokerage') || name.includes('fidelity') || name.includes('vanguard') || name.includes('schwab') || name.includes('etrade')) {
			return 'brokerage';
		}
		// Default to bank CSV for everything else
		return 'bank';
	}

	// ── Handlers ──────────────────────────────────────────────────────────────

	function handleDragover(e: DragEvent) {
		e.preventDefault();
		dragOver = true;
	}

	function handleDragleave() {
		dragOver = false;
	}

	function handleDrop(e: DragEvent) {
		e.preventDefault();
		dragOver = false;
		const dropped = e.dataTransfer?.files?.[0];
		if (dropped) acceptFile(dropped);
	}

	function handleFileInput(e: Event) {
		const input = e.target as HTMLInputElement;
		const picked = input.files?.[0];
		if (picked) acceptFile(picked);
	}

	function acceptFile(f: File) {
		file = f;
		fileType = detectFileType(f);
		phase = 'idle';
		preview = null;
		bankResult = null;
		brokerageResult = null;
		errorMsg = '';

		// Auto-preview for bank CSVs
		if (fileType === 'bank') {
			runPreview();
		}
	}

	async function runPreview() {
		if (!file || fileType !== 'bank') return;
		previewLoading = true;
		preview = null;
		errorMsg = '';
		try {
			preview = await previewBankCsv(file, selectedBank || undefined);
			// Auto-select the detected config if none chosen yet
			if (!selectedBank && preview.detected_config) {
				selectedBank = preview.detected_config.bank_name;
			}
			phase = 'preview';
		} catch (err) {
			errorMsg = err instanceof Error ? err.message : String(err);
			phase = 'error';
		} finally {
			previewLoading = false;
		}
	}

	async function handleBankImport() {
		if (!file) return;
		phase = 'importing';
		errorMsg = '';
		try {
			bankResult = await commitBankCsv(file, selectedBank);
			phase = 'done';
		} catch (err) {
			errorMsg = err instanceof Error ? err.message : String(err);
			phase = 'error';
		}
	}

	async function handleBrokerageImport() {
		if (!file) return;
		phase = 'importing';
		errorMsg = '';
		try {
			brokerageResult = await importBrokerageCsv(file);
			phase = 'done';
		} catch (err) {
			errorMsg = err instanceof Error ? err.message : String(err);
			phase = 'error';
		}
	}

	function reset() {
		file = null;
		fileType = null;
		phase = 'idle';
		preview = null;
		bankResult = null;
		brokerageResult = null;
		errorMsg = '';
		selectedBank = '';
		if (fileInput) fileInput.value = '';
	}

	// Re-run preview when user explicitly changes the bank selector (not during auto-detect).
	let _prevBankSelection = '';
	$effect(() => {
		const bank = selectedBank;
		// Only fire when the user picks a different bank (not the initial auto-detect write).
		if (file && fileType === 'bank' && bank && bank !== _prevBankSelection && phase !== 'importing') {
			_prevBankSelection = bank;
			const t = setTimeout(() => runPreview(), 50);
			return () => clearTimeout(t);
		}
		_prevBankSelection = bank;
	});
</script>

<div class="page">
	<div class="page-header">
		<h1 class="page-title">Import</h1>
		<p class="page-subtitle">Upload bank or brokerage CSV files to import transactions.</p>
	</div>

	<!-- Drop zone -->
	{#if !file}
		<div
			class="drop-zone"
			class:drag-over={dragOver}
			ondragover={handleDragover}
			ondragleave={handleDragleave}
			ondrop={handleDrop}
			onclick={() => fileInput.click()}
			onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInput.click(); } }}
			role="button"
			tabindex="0"
			aria-label="Upload CSV file"
		>
			<svg class="drop-icon" width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
				<polyline points="16 16 12 12 8 16"/>
				<line x1="12" y1="12" x2="12" y2="21"/>
				<path d="M20.39 18.39A5 5 0 0 0 18 9h-1.26A8 8 0 1 0 3 16.3"/>
			</svg>
			<p class="drop-label">Drag & drop a CSV file here</p>
			<p class="drop-sub">or click to browse</p>
			<p class="drop-hint">Supports: Bank CSV, Brokerage CSV</p>
		</div>
		<input
			bind:this={fileInput}
			type="file"
			accept=".csv,text/csv"
			class="sr-only"
			onchange={handleFileInput}
		/>
	{:else}
		<!-- File selected -->
		<div class="file-card">
			<div class="file-info">
				<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
					<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
					<polyline points="14 2 14 8 20 8"/>
				</svg>
				<span class="file-name">{file.name}</span>
				<span class="file-badge" class:badge-blue={fileType === 'bank'} class:badge-purple={fileType === 'brokerage'}>
					{fileType === 'bank' ? 'Bank CSV' : 'Brokerage CSV'}
				</span>
				<span class="file-size">{(file.size / 1024).toFixed(1)} KB</span>
			</div>
			{#if phase !== 'importing'}
				<button class="btn-ghost btn-sm" onclick={reset}>
					<svg width="14" height="14" viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
						<line x1="3" y1="3" x2="15" y2="15"/>
						<line x1="15" y1="3" x2="3" y2="15"/>
					</svg>
					Remove
				</button>
			{/if}
		</div>

		<!-- Bank CSV flow -->
		{#if fileType === 'bank'}
			<section class="section">
				<h2 class="section-title">Bank Configuration</h2>
				<div class="field-row">
					<label class="field-label" for="bank-select">Bank name</label>
					<div class="field-input-group">
						<select
							id="bank-select"
							class="select"
							bind:value={selectedBank}
							disabled={phase === 'importing'}
						>
							<option value="">Auto-detect</option>
							{#each configs as cfg}
								<option value={cfg.bank_name}>{cfg.label || cfg.bank_name}</option>
							{/each}
						</select>
						{#if previewLoading}
							<span class="spinner" aria-label="Loading preview"></span>
						{/if}
					</div>
				</div>
			</section>

			<!-- Preview table -->
			{#if preview && phase !== 'done'}
				<section class="section">
					<div class="preview-header">
						<h2 class="section-title">Preview</h2>
						<span class="preview-meta">{preview.row_count} rows detected</span>
					</div>

					{#if preview.detected_config}
						<div class="detected-banner">
							<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
							Auto-detected as <strong>{preview.detected_config.label || preview.detected_config.bank_name}</strong>
							— date: <code>{preview.detected_config.date_col}</code>,
							amount: <code>{preview.detected_config.amount_col}</code>,
							description: <code>{preview.detected_config.description_col}</code>
						</div>
					{/if}

					<div class="table-wrap">
						<table class="preview-table">
							<thead>
								<tr>
									{#each preview.headers as header}
										<th>{header}</th>
									{/each}
								</tr>
							</thead>
							<tbody>
								{#each preview.sample_rows as row}
									<tr>
										{#each preview.headers as header}
											<td>{row[header] ?? ''}</td>
										{/each}
									</tr>
								{/each}
							</tbody>
						</table>
					</div>
				</section>
			{/if}

			<!-- Import button for bank -->
			{#if phase !== 'done' && phase !== 'importing'}
				<div class="action-row">
					<button
						class="btn-primary"
						onclick={handleBankImport}
						disabled={previewLoading || !preview}
					>
						Import Transactions
					</button>
					{#if !preview && !previewLoading}
						<button class="btn-secondary" onclick={runPreview}>
							Preview CSV
						</button>
					{/if}
				</div>
			{/if}
		{/if}

		<!-- Brokerage CSV flow -->
		{#if fileType === 'brokerage'}
			<section class="section">
				<p class="brokerage-note">
					Brokerage CSV files are imported directly. No preview is available.
				</p>
				{#if phase !== 'done' && phase !== 'importing'}
					<div class="action-row">
						<button class="btn-primary" onclick={handleBrokerageImport}>
							Import Brokerage Transactions
						</button>
					</div>
				{/if}
			</section>
		{/if}

		<!-- Spinner while importing -->
		{#if phase === 'importing'}
			<div class="status-row">
				<span class="spinner spinner-lg" aria-label="Importing"></span>
				<span class="status-text">Importing…</span>
			</div>
		{/if}

		<!-- Results -->
		{#if phase === 'done'}
			{@const result = bankResult ?? brokerageResult}
			{#if result}
				<div class="results-card">
					<div class="results-title">
						<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="var(--green-500)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
						Import complete
					</div>
					<div class="results-grid">
						<div class="result-stat">
							<span class="result-num created">{result.created}</span>
							<span class="result-label">Created</span>
						</div>
						<div class="result-stat">
							<span class="result-num skipped">{result.skipped}</span>
							<span class="result-label">Skipped (duplicates)</span>
						</div>
						{#if result.errors.length > 0}
							<div class="result-stat">
								<span class="result-num errored">{result.errors.length}</span>
								<span class="result-label">Errors</span>
							</div>
						{/if}
					</div>
					{#if result.errors.length > 0}
						<details class="error-details">
							<summary>View {result.errors.length} error{result.errors.length > 1 ? 's' : ''}</summary>
							<ul class="error-list">
								{#each result.errors as err}
									<li>{err}</li>
								{/each}
							</ul>
						</details>
					{/if}
					<div class="results-actions">
						<button class="btn-secondary" onclick={reset}>Import another file</button>
						<a href="/register" class="btn-primary">View in Register</a>
					</div>
				</div>
			{/if}
		{/if}

		<!-- Error state -->
		{#if phase === 'error'}
			<div class="error-banner">
				<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
				{errorMsg}
			</div>
			<div class="action-row">
				<button class="btn-secondary" onclick={reset}>Start over</button>
			</div>
		{/if}
	{/if}
</div>

<style>
	.page {
		max-width: 860px;
		margin: 0 auto;
		padding: 32px 24px 80px;
	}

	.page-header {
		margin-bottom: 28px;
	}

	.page-title {
		font-size: 1.5rem;
		font-weight: 600;
		color: var(--text);
		letter-spacing: -0.3px;
	}

	.page-subtitle {
		margin-top: 4px;
		font-size: 0.875rem;
		color: var(--text-muted);
	}

	/* ── Drop zone ──────────────────────────────────────────────────────────── */

	.drop-zone {
		border: 2px dashed var(--border);
		border-radius: var(--radius-lg);
		padding: 56px 32px;
		text-align: center;
		cursor: pointer;
		transition: border-color 0.15s, background 0.15s;
		background: var(--surface);
	}

	.drop-zone:hover,
	.drop-zone:focus-visible {
		border-color: var(--blue-500);
		background: color-mix(in srgb, var(--blue-500) 4%, transparent);
		outline: none;
	}

	.drop-zone.drag-over {
		border-color: var(--blue-500);
		background: color-mix(in srgb, var(--blue-500) 8%, transparent);
	}

	.drop-icon {
		color: var(--text-muted);
		margin: 0 auto 16px;
		display: block;
	}

	.drop-label {
		font-size: 1rem;
		font-weight: 500;
		color: var(--text);
		margin-bottom: 4px;
	}

	.drop-sub {
		font-size: 0.875rem;
		color: var(--text-muted);
		margin-bottom: 12px;
	}

	.drop-hint {
		font-size: 0.75rem;
		color: var(--gray-400);
	}

	.sr-only {
		position: absolute;
		width: 1px;
		height: 1px;
		padding: 0;
		margin: -1px;
		overflow: hidden;
		clip: rect(0,0,0,0);
		white-space: nowrap;
		border-width: 0;
	}

	/* ── File card ──────────────────────────────────────────────────────────── */

	.file-card {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		padding: 12px 16px;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		margin-bottom: 20px;
	}

	.file-info {
		display: flex;
		align-items: center;
		gap: 10px;
		color: var(--text-muted);
		min-width: 0;
	}

	.file-name {
		font-size: 0.9rem;
		font-weight: 500;
		color: var(--text);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		max-width: 300px;
	}

	.file-badge {
		display: inline-flex;
		align-items: center;
		padding: 2px 8px;
		border-radius: 999px;
		font-size: 0.7rem;
		font-weight: 600;
		white-space: nowrap;
	}

	.badge-blue {
		background: color-mix(in srgb, var(--blue-500) 15%, transparent);
		color: var(--blue-600);
	}

	.badge-purple {
		background: color-mix(in srgb, #8b5cf6 15%, transparent);
		color: #7c3aed;
	}

	.file-size {
		font-size: 0.78rem;
		color: var(--gray-400);
		white-space: nowrap;
	}

	/* ── Sections ───────────────────────────────────────────────────────────── */

	.section {
		margin-bottom: 24px;
	}

	.section-title {
		font-size: 0.9rem;
		font-weight: 600;
		color: var(--text);
		margin-bottom: 12px;
	}

	.field-row {
		display: flex;
		align-items: center;
		gap: 12px;
	}

	.field-label {
		font-size: 0.875rem;
		color: var(--text-muted);
		white-space: nowrap;
		flex-shrink: 0;
		min-width: 80px;
	}

	.field-input-group {
		display: flex;
		align-items: center;
		gap: 10px;
	}

	.select {
		font-size: 0.875rem;
		font-family: inherit;
		padding: 6px 10px;
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		background: var(--surface);
		color: var(--text);
		min-width: 200px;
		cursor: pointer;
	}

	.select:focus {
		outline: 2px solid var(--focus);
		outline-offset: 2px;
	}

	.select:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	/* ── Preview ────────────────────────────────────────────────────────────── */

	.preview-header {
		display: flex;
		align-items: baseline;
		gap: 12px;
		margin-bottom: 10px;
	}

	.preview-meta {
		font-size: 0.8rem;
		color: var(--text-muted);
	}

	.detected-banner {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 8px 12px;
		background: color-mix(in srgb, var(--green-500) 10%, transparent);
		border: 1px solid color-mix(in srgb, var(--green-500) 30%, transparent);
		border-radius: var(--radius-sm);
		font-size: 0.8rem;
		color: var(--text);
		margin-bottom: 12px;
	}

	.detected-banner code {
		font-family: var(--font-mono);
		font-size: 0.75rem;
		background: color-mix(in srgb, var(--green-500) 15%, transparent);
		padding: 1px 5px;
		border-radius: 3px;
	}

	.table-wrap {
		overflow-x: auto;
		border: 1px solid var(--border);
		border-radius: var(--radius);
	}

	.preview-table {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.8rem;
	}

	.preview-table th {
		padding: 8px 12px;
		text-align: left;
		background: var(--gray-50);
		border-bottom: 1px solid var(--border);
		color: var(--text-muted);
		font-weight: 600;
		white-space: nowrap;
	}

	.preview-table td {
		padding: 7px 12px;
		border-bottom: 1px solid var(--border);
		color: var(--text);
		white-space: nowrap;
		max-width: 220px;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.preview-table tbody tr:last-child td {
		border-bottom: none;
	}

	.preview-table tbody tr:hover td {
		background: var(--gray-50);
	}

	/* ── Brokerage note ─────────────────────────────────────────────────────── */

	.brokerage-note {
		font-size: 0.875rem;
		color: var(--text-muted);
		margin-bottom: 16px;
	}

	/* ── Action row ─────────────────────────────────────────────────────────── */

	.action-row {
		display: flex;
		gap: 10px;
		margin-top: 8px;
	}

	/* ── Status / spinner ───────────────────────────────────────────────────── */

	.status-row {
		display: flex;
		align-items: center;
		gap: 12px;
		padding: 16px 0;
	}

	.status-text {
		font-size: 0.9rem;
		color: var(--text-muted);
	}

	.spinner {
		display: inline-block;
		width: 16px;
		height: 16px;
		border: 2px solid var(--border);
		border-top-color: var(--blue-500);
		border-radius: 50%;
		animation: spin 0.7s linear infinite;
		flex-shrink: 0;
	}

	.spinner-lg {
		width: 22px;
		height: 22px;
	}

	@keyframes spin {
		to { transform: rotate(360deg); }
	}

	/* ── Results card ───────────────────────────────────────────────────────── */

	.results-card {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		padding: 24px;
	}

	.results-title {
		display: flex;
		align-items: center;
		gap: 8px;
		font-size: 1rem;
		font-weight: 600;
		color: var(--text);
		margin-bottom: 20px;
	}

	.results-grid {
		display: flex;
		gap: 32px;
		margin-bottom: 20px;
	}

	.result-stat {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.result-num {
		font-size: 2rem;
		font-weight: 700;
		line-height: 1;
	}

	.result-num.created  { color: var(--green-500); }
	.result-num.skipped  { color: var(--text-muted); }
	.result-num.errored  { color: var(--red-500); }

	.result-label {
		font-size: 0.78rem;
		color: var(--text-muted);
	}

	.error-details {
		margin-bottom: 16px;
		font-size: 0.8rem;
	}

	.error-details summary {
		cursor: pointer;
		color: var(--red-600);
		font-weight: 500;
		padding: 4px 0;
	}

	.error-list {
		margin-top: 8px;
		padding-left: 20px;
		color: var(--text);
		line-height: 1.8;
	}

	.results-actions {
		display: flex;
		gap: 10px;
		margin-top: 4px;
	}

	/* ── Error banner ───────────────────────────────────────────────────────── */

	.error-banner {
		display: flex;
		align-items: flex-start;
		gap: 8px;
		padding: 12px 16px;
		background: var(--red-100);
		border: 1px solid color-mix(in srgb, var(--red-500) 30%, transparent);
		border-radius: var(--radius-sm);
		color: var(--red-700);
		font-size: 0.875rem;
		margin-bottom: 16px;
	}

	/* ── Buttons ────────────────────────────────────────────────────────────── */

	.btn-primary {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		padding: 8px 16px;
		background: var(--blue-500);
		color: #fff;
		border: none;
		border-radius: var(--radius-sm);
		font-size: 0.875rem;
		font-weight: 500;
		font-family: inherit;
		cursor: pointer;
		text-decoration: none;
		transition: background 0.12s;
	}

	.btn-primary:hover {
		background: var(--blue-600);
	}

	.btn-primary:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.btn-secondary {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		padding: 8px 16px;
		background: var(--surface);
		color: var(--text);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		font-size: 0.875rem;
		font-weight: 500;
		font-family: inherit;
		cursor: pointer;
		text-decoration: none;
		transition: background 0.12s;
	}

	.btn-secondary:hover {
		background: var(--gray-100);
	}

	.btn-ghost {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		padding: 5px 10px;
		background: transparent;
		color: var(--text-muted);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		font-size: 0.8rem;
		font-family: inherit;
		cursor: pointer;
		transition: color 0.12s, background 0.12s;
	}

	.btn-ghost:hover {
		color: var(--text);
		background: var(--gray-100);
	}

	.btn-sm {
		padding: 4px 8px;
		font-size: 0.75rem;
		flex-shrink: 0;
	}
</style>
