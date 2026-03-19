<script lang="ts">
	import { onMount } from 'svelte';
	import { fetchHealth, triggerSourceIngest, fetchSourceConfig } from '$lib/api';
	import type { SourceConfigItem } from '$lib/api';
	import type { HealthResponse, SourceFreshness, TaxDeadline, FailureLogEntry, LLMUsage } from '$lib/types';

	// ── State ─────────────────────────────────────────────────────────────────
	let health: HealthResponse | null = $state(null);
	let loading = $state(true);
	let fetchError = $state('');
	let lastRefreshed = $state<Date | null>(null);
	let secondsSinceRefresh = $state(0);
	let syncingSource = $state<string | null>(null);
	let syncError = $state('');
	let sourceConfig = $state<SourceConfigItem[]>([]);

	// ── Derived ───────────────────────────────────────────────────────────────
	const FRESHNESS_ORDER: Record<string, number> = {
		never: 0,
		red: 1,
		amber: 2,
		green: 3
	};

	let sourcesSorted = $derived.by(() => {
		const h = health as HealthResponse | null;
		if (!h) return [] as SourceFreshness[];
		return [...h.source_freshness].sort(
			(a, b) => FRESHNESS_ORDER[a.freshness_status] - FRESHNESS_ORDER[b.freshness_status]
		);
	});

	// ── Helpers ───────────────────────────────────────────────────────────────
	function freshnessColor(status: SourceFreshness['freshness_status']): string {
		const map = { green: 'var(--green-600)', amber: 'var(--amber-600)', red: 'var(--red-600)', never: 'var(--gray-400)' };
		return map[status];
	}

	function freshnessLabel(status: SourceFreshness['freshness_status']): string {
		const map = { green: 'Fresh', amber: 'Stale', red: 'Very stale', never: 'Never synced' };
		return map[status];
	}

	function sourceLabel(source: string): string {
		const labels: Record<string, string> = {
			gmail_n8n: 'Gmail / n8n',
			stripe: 'Stripe',
			shopify: 'Shopify',
			brokerage_csv: 'Brokerage CSV',
			bank_csv: 'Bank CSV',
			photo_receipt: 'Photo Receipts',
			deduction_email: 'Deduction Email'
		};
		return labels[source] ?? source;
	}

	function fmtDatetime(iso: string | null): string {
		if (!iso) return '—';
		const d = new Date(iso + 'Z'); // API returns naive UTC
		return d.toLocaleString('en-US', {
			month: 'short',
			day: 'numeric',
			hour: 'numeric',
			minute: '2-digit',
			hour12: true
		});
	}

	function fmtDate(iso: string): string {
		const d = new Date(iso + 'T00:00:00');
		return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
	}

	function deadlineUrgency(days: number): string {
		if (days <= 7) return 'urgent';
		if (days <= 30) return 'soon';
		return 'upcoming';
	}

	function entityLabel(entity: string): string {
		const map: Record<string, string> = {
			sparkry: 'Sparkry AI',
			blackline: 'BlackLine MTB',
			personal: 'Personal',
			all: 'All entities'
		};
		return map[entity] ?? entity;
	}

	// ── Load ─────────────────────────────────────────────────────────────────
	async function load() {
		loading = true;
		fetchError = '';
		try {
			const [healthData, configData] = await Promise.all([
				fetchHealth(),
				fetchSourceConfig().catch(() => [] as SourceConfigItem[])
			]);
			health = healthData;
			sourceConfig = configData;
			lastRefreshed = new Date();
			secondsSinceRefresh = 0;
		} catch (e) {
			fetchError = e instanceof Error ? e.message : 'Failed to load health data';
		} finally {
			loading = false;
		}
	}

	function sourceConfigFor(source: string): SourceConfigItem | undefined {
		return sourceConfig.find(c => c.source === source);
	}

	async function syncSource(source: string) {
		syncingSource = source;
		syncError = '';
		try {
			await triggerSourceIngest(source);
			// Refresh health data after sync
			await load();
		} catch (e) {
			syncError = `Sync failed for ${sourceLabel(source)}: ${e instanceof Error ? e.message : 'Unknown error'}`;
		} finally {
			syncingSource = null;
		}
	}

	onMount(() => {
		load();

		// Update "last refreshed X seconds ago" counter
		const interval = setInterval(() => {
			if (lastRefreshed) {
				secondsSinceRefresh = Math.floor((Date.now() - lastRefreshed.getTime()) / 1000);
			}
		}, 1000);

		return () => clearInterval(interval);
	});

	function refreshLabel(): string {
		if (!lastRefreshed) return '';
		if (secondsSinceRefresh < 5) return 'Just now';
		if (secondsSinceRefresh < 60) return `${secondsSinceRefresh}s ago`;
		const mins = Math.floor(secondsSinceRefresh / 60);
		return `${mins}m ago`;
	}
</script>

<div class="container page-shell">
	<!-- ── Header ──────────────────────────────────────────────────────────── -->
	<header class="page-header">
		<div>
			<h1>Health Dashboard</h1>
			<p class="page-subtitle">
				Source freshness, classification accuracy, and upcoming tax deadlines
			</p>
		</div>
		<div class="page-header-actions">
			{#if lastRefreshed && !loading}
				<span class="refreshed-label">Last refreshed {refreshLabel()}</span>
			{/if}
			<button class="btn btn-ghost" onclick={load} disabled={loading}>
				{loading ? 'Loading…' : 'Refresh'}
			</button>
		</div>
	</header>

	{#if loading && !health}
		<!-- Initial skeleton -->
		<div class="section-grid">
			{#each Array(4) as _}
				<div class="card skeleton-card">
					<div class="skeleton" style="height: 14px; width: 40%; margin-bottom: 10px;"></div>
					<div class="skeleton" style="height: 22px; width: 60%; margin-bottom: 8px;"></div>
					<div class="skeleton" style="height: 14px; width: 50%;"></div>
				</div>
			{/each}
		</div>
	{:else if fetchError}
		<div class="card error-card">
			<p class="error-msg">{fetchError}</p>
			<button class="btn btn-ghost" onclick={load}>Try again</button>
		</div>
	{:else if health}

		<!-- ── Sync error banner ─────────────────────────────────────────────── -->
		{#if syncError}
			<div class="sync-error-banner">
				<span>{syncError}</span>
				<button class="dismiss-btn" onclick={() => (syncError = '')}>✕</button>
			</div>
		{/if}

		<!-- ── Source Freshness ──────────────────────────────────────────────── -->
		<section class="dashboard-section">
			<h2 class="section-title">Source Freshness</h2>
			<div class="source-grid">
				{#each sourcesSorted as sf (sf.source)}
					<div class="card source-card">
						<div class="source-card-header">
							<div class="source-name-row">
								<span
									class="freshness-dot"
									style="background: {freshnessColor(sf.freshness_status)}"
									title={freshnessLabel(sf.freshness_status)}
								></span>
								<span class="source-name">{sourceLabel(sf.source)}</span>
							</div>
							<span class="freshness-badge freshness-{sf.freshness_status}">
								{freshnessLabel(sf.freshness_status)}
							</span>
						</div>

						<div class="source-meta">
							<div class="meta-row">
								<span class="meta-label">Last sync</span>
								<span class="meta-value">{fmtDatetime(sf.last_run_at)}</span>
							</div>
							<div class="meta-row">
								<span class="meta-label">Records</span>
								<span class="meta-value">{sf.records_processed.toLocaleString()}</span>
							</div>
							{#if sf.records_failed > 0}
								<div class="meta-row">
									<span class="meta-label">Failures</span>
									<span class="meta-value meta-value-red">{sf.records_failed}</span>
								</div>
							{/if}
							{#if sf.ingestion_status && sf.ingestion_status !== 'success'}
								<div class="meta-row">
									<span class="meta-label">Status</span>
									<span class="meta-value meta-value-red">{sf.ingestion_status.replace('_', ' ')}</span>
								</div>
							{/if}
						</div>

						{#if sf.last_error}
							<details class="error-details">
								<summary class="error-summary">Last error</summary>
								<pre class="error-pre">{sf.last_error}</pre>
							</details>
						{/if}

						{#if sourceConfigFor(sf.source)}
							{@const cfg = sourceConfigFor(sf.source)!}
							<div class="source-config-info">
								{#if cfg.mode === 'import_only'}
									<span class="config-badge config-import">Import Only</span>
								{:else if !cfg.configured}
									<span class="config-badge config-setup">Setup Required</span>
									{#if cfg.missing_env_vars.length > 0}
										<span class="config-missing">Missing: {cfg.missing_env_vars.join(', ')}</span>
									{/if}
								{/if}
							</div>
						{/if}

						<div class="source-actions">
							<button
								class="btn btn-ghost btn-sm"
								disabled={syncingSource !== null}
								onclick={() => syncSource(sf.source)}
							>
								{#if syncingSource === sf.source}
									<span class="spinner" aria-hidden="true"></span>
									Syncing…
								{:else}
									Re-sync
								{/if}
							</button>
						</div>
					</div>
				{/each}
			</div>
		</section>

		<!-- ── Classification Stats ─────────────────────────────────────────── -->
		<section class="dashboard-section">
			<h2 class="section-title">Classification Accuracy</h2>
			<div class="stats-layout">
				<div class="card stats-card">
					<div class="stat-row">
						<span class="stat-label">Total transactions</span>
						<span class="stat-value">{health.classification_stats.total.toLocaleString()}</span>
					</div>
					<div class="stat-row">
						<span class="stat-label">Auto-classified</span>
						<div class="stat-right">
							<span class="stat-value">{health.classification_stats.auto_classified.toLocaleString()}</span>
							<span class="stat-pct">{health.classification_stats.auto_confirmed_pct}%</span>
						</div>
					</div>
					<div class="stat-row">
						<span class="stat-label">Confirmed (human)</span>
						<div class="stat-right">
							<span class="stat-value">{health.classification_stats.confirmed.toLocaleString()}</span>
							<span class="stat-pct">{health.classification_stats.edited_pct}%</span>
						</div>
					</div>
					<div class="stat-row">
						<span class="stat-label">Rejected</span>
						<div class="stat-right">
							<span class="stat-value">{health.classification_stats.rejected.toLocaleString()}</span>
							<span class="stat-pct stat-pct-red">{health.classification_stats.rejected_pct}%</span>
						</div>
					</div>
					<div class="stat-divider"></div>
					<div class="stat-row stat-row-highlight">
						<span class="stat-label">Pending review</span>
						<div class="stat-right">
							<span class="stat-value stat-value-amber">
								{health.classification_stats.pending_count.toLocaleString()}
							</span>
							<span class="stat-pct stat-pct-amber">{health.classification_stats.pending_pct}%</span>
						</div>
					</div>

					{#if health.classification_stats.pending_count > 0}
						<div class="stats-cta">
							<a href="/review" class="btn btn-primary btn-sm">
								Review {health.classification_stats.pending_count} pending
							</a>
						</div>
					{:else}
						<div class="stats-cta">
							<span class="all-clear">All transactions reviewed</span>
						</div>
					{/if}
				</div>

				<!-- Progress bar breakdown -->
				<div class="card stats-card progress-card">
					<h3 class="progress-title">Status breakdown</h3>
					{#if health.classification_stats.total > 0}
						<div class="progress-bar-container">
							<div
								class="progress-segment progress-confirmed"
								style="width: {health.classification_stats.edited_pct}%"
								title="Confirmed: {health.classification_stats.confirmed}"
							></div>
							<div
								class="progress-segment progress-auto"
								style="width: {health.classification_stats.auto_confirmed_pct - health.classification_stats.edited_pct}%"
								title="Auto-classified: {health.classification_stats.auto_classified}"
							></div>
							<div
								class="progress-segment progress-pending"
								style="width: {health.classification_stats.pending_pct}%"
								title="Pending review: {health.classification_stats.pending_count}"
							></div>
							<div
								class="progress-segment progress-rejected"
								style="width: {health.classification_stats.rejected_pct}%"
								title="Rejected: {health.classification_stats.rejected}"
							></div>
						</div>
					{:else}
						<p class="no-data">No transactions yet.</p>
					{/if}

					<div class="legend">
						<div class="legend-item"><span class="legend-dot legend-confirmed"></span>Confirmed</div>
						<div class="legend-item"><span class="legend-dot legend-auto"></span>Auto-classified</div>
						<div class="legend-item"><span class="legend-dot legend-pending"></span>Pending review</div>
						<div class="legend-item"><span class="legend-dot legend-rejected"></span>Rejected</div>
					</div>
				</div>
			</div>
		</section>

		<!-- ── Claude API Usage ─────────────────────────────────────────────── -->
		<section class="dashboard-section">
			<h2 class="section-title">Claude API Usage</h2>
			<div class="card stats-card">
				{#if health.llm_usage}
					<div class="stat-row">
						<span class="stat-label">Calls this month</span>
						<span class="stat-value">{health.llm_usage.calls_this_month.toLocaleString()}</span>
					</div>
					<div class="stat-row">
						<span class="stat-label">Input tokens</span>
						<span class="stat-value">{health.llm_usage.total_input_tokens.toLocaleString()}</span>
					</div>
					<div class="stat-row">
						<span class="stat-label">Output tokens</span>
						<span class="stat-value">{health.llm_usage.total_output_tokens.toLocaleString()}</span>
					</div>
					<div class="stat-row">
						<span class="stat-label">Total tokens</span>
						<span class="stat-value">{health.llm_usage.total_tokens.toLocaleString()}</span>
					</div>
					<div class="stat-divider"></div>
					<div class="stat-row stat-row-highlight">
						<span class="stat-label">Estimated cost</span>
						<span class="stat-value">${health.llm_usage.estimated_cost_usd.toFixed(2)}</span>
					</div>
				{:else}
					<p class="no-data">0 calls this month, $0.00 estimated</p>
				{/if}
			</div>
		</section>

		<!-- ── Tax Deadlines ────────────────────────────────────────────────── -->
		<section class="dashboard-section">
			<h2 class="section-title">Upcoming Tax Deadlines</h2>
			{#if health.tax_deadlines.length === 0}
				<div class="card empty-deadlines">
					<p class="no-data">No deadlines in the next 180 days.</p>
				</div>
			{:else}
				<div class="deadlines-list">
					{#each health.tax_deadlines as deadline (deadline.label + deadline.due_date)}
						{@const urgency = deadlineUrgency(deadline.days_until_due)}
						<div class="card deadline-card deadline-{urgency}">
							<div class="deadline-left">
								<span class="deadline-dot deadline-dot-{urgency}"></span>
								<div>
									<p class="deadline-label">{deadline.label}</p>
									<p class="deadline-entity">{entityLabel(deadline.entity)}</p>
								</div>
							</div>
							<div class="deadline-right">
								<p class="deadline-date">{fmtDate(deadline.due_date)}</p>
								<p class="deadline-days deadline-days-{urgency}">
									{deadline.days_until_due === 0
										? 'Due today'
										: deadline.days_until_due === 1
											? 'Tomorrow'
											: `${deadline.days_until_due} days`}
								</p>
							</div>
						</div>
					{/each}
				</div>
			{/if}
		</section>

		<!-- ── Failure Log ───────────────────────────────────────────────────── -->
		{#if health.failure_log.length > 0}
			<section class="dashboard-section">
				<h2 class="section-title">Recent Failures</h2>
				<div class="card failure-table-wrap">
					<table class="data-table">
						<thead>
							<tr>
								<th>Source</th>
								<th>Time</th>
								<th>Status</th>
								<th>Records</th>
								<th>Error</th>
							</tr>
						</thead>
						<tbody>
							{#each health.failure_log as entry (entry.source + entry.run_at)}
								<tr>
									<td class="td-source">{sourceLabel(entry.source)}</td>
									<td class="td-time">{fmtDatetime(entry.run_at)}</td>
									<td>
										<span class="status-pill {entry.ingestion_status === 'failure' ? 'status-rejected' : 'status-needs_review'}">
											{entry.ingestion_status.replace('_', ' ')}
										</span>
									</td>
									<td class="td-records">
										{entry.records_processed} / <span class="text-red">{entry.records_failed} failed</span>
									</td>
									<td class="td-error">
										{#if entry.error_detail}
											<details>
												<summary class="error-toggle">Show error</summary>
												<pre class="error-pre error-pre-sm">{entry.error_detail}</pre>
											</details>
										{:else}
											<span class="no-data">—</span>
										{/if}
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			</section>
		{/if}

	{/if}
</div>

<style>
	.page-shell {
		padding-top: 32px;
		padding-bottom: 64px;
	}

	.page-header {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: 16px;
		margin-bottom: 32px;
	}

	.page-subtitle {
		margin-top: 4px;
		color: var(--text-muted);
		font-size: 0.9rem;
	}

	.page-header-actions {
		display: flex;
		gap: 12px;
		align-items: center;
		flex-shrink: 0;
	}

	.refreshed-label {
		font-size: 0.8rem;
		color: var(--text-muted);
	}

	/* ── Sections ──────────────────────────────────────────────────────────── */
	.dashboard-section {
		margin-bottom: 36px;
	}

	.section-title {
		margin-bottom: 14px;
		font-size: 1rem;
		font-weight: 600;
		color: var(--text);
	}

	/* ── Error / sync ──────────────────────────────────────────────────────── */
	.error-card {
		padding: 24px;
		display: flex;
		flex-direction: column;
		gap: 12px;
		align-items: flex-start;
	}

	.error-msg {
		color: var(--red-600);
		font-size: 0.875rem;
	}

	.sync-error-banner {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		padding: 10px 16px;
		background: var(--red-100);
		border: 1px solid var(--red-500);
		border-radius: var(--radius-sm);
		color: var(--red-700);
		font-size: 0.875rem;
		margin-bottom: 20px;
	}

	.dismiss-btn {
		background: none;
		border: none;
		cursor: pointer;
		color: var(--red-700);
		font-size: 0.9rem;
		padding: 0 4px;
	}

	/* ── Source grid ───────────────────────────────────────────────────────── */
	.section-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
		gap: 12px;
		margin-bottom: 36px;
	}

	.skeleton-card {
		padding: 18px 20px;
	}

	.source-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
		gap: 12px;
	}

	.source-card {
		padding: 16px 18px;
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.source-card-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
	}

	.source-name-row {
		display: flex;
		align-items: center;
		gap: 8px;
		min-width: 0;
	}

	.freshness-dot {
		flex-shrink: 0;
		width: 10px;
		height: 10px;
		border-radius: 50%;
	}

	.source-name {
		font-size: 0.875rem;
		font-weight: 600;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.freshness-badge {
		font-size: 0.7rem;
		font-weight: 600;
		padding: 2px 7px;
		border-radius: 999px;
		flex-shrink: 0;
		white-space: nowrap;
	}

	.freshness-green {
		background: var(--green-100);
		color: var(--green-700);
	}

	.freshness-amber {
		background: var(--amber-100);
		color: var(--amber-700);
	}

	.freshness-red {
		background: var(--red-100);
		color: var(--red-700);
	}

	.freshness-never {
		background: var(--gray-100);
		color: var(--gray-500);
	}

	.source-meta {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.meta-row {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
		gap: 8px;
		font-size: 0.8rem;
	}

	.meta-label {
		color: var(--text-muted);
		flex-shrink: 0;
	}

	.meta-value {
		color: var(--text);
		text-align: right;
		font-variant-numeric: tabular-nums;
	}

	.meta-value-red {
		color: var(--red-600);
	}

	.error-details {
		font-size: 0.75rem;
	}

	.error-summary {
		cursor: pointer;
		color: var(--red-600);
		font-size: 0.75rem;
	}

	.error-pre {
		margin-top: 6px;
		padding: 8px;
		background: var(--gray-100);
		border-radius: var(--radius-sm);
		font-size: 0.7rem;
		font-family: var(--font-mono);
		white-space: pre-wrap;
		word-break: break-word;
		max-height: 120px;
		overflow-y: auto;
		color: var(--red-700);
	}

	.source-actions {
		display: flex;
		gap: 8px;
	}

	.source-config-info {
		display: flex;
		align-items: center;
		gap: 6px;
		flex-wrap: wrap;
	}

	.config-badge {
		font-size: 0.68rem;
		font-weight: 600;
		padding: 2px 7px;
		border-radius: 999px;
		white-space: nowrap;
	}

	.config-import {
		background: var(--gray-100);
		color: var(--gray-600);
	}

	.config-setup {
		background: var(--amber-100);
		color: var(--amber-700);
	}

	.config-missing {
		font-size: 0.7rem;
		color: var(--amber-600);
	}

	.btn-sm {
		padding: 4px 12px;
		font-size: 0.8rem;
	}

	/* ── Spinner ───────────────────────────────────────────────────────────── */
	.spinner {
		display: inline-block;
		width: 12px;
		height: 12px;
		border: 2px solid var(--gray-300);
		border-top-color: var(--gray-700);
		border-radius: 50%;
		animation: spin 0.6s linear infinite;
	}

	@keyframes spin {
		to { transform: rotate(360deg); }
	}

	/* ── Classification stats ──────────────────────────────────────────────── */
	.stats-layout {
		display: grid;
		grid-template-columns: 320px 1fr;
		gap: 12px;
	}

	@media (max-width: 700px) {
		.stats-layout {
			grid-template-columns: 1fr;
		}
	}

	.stats-card {
		padding: 20px 22px;
	}

	.stat-row {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 12px;
		padding: 6px 0;
		border-bottom: 1px solid var(--border);
		font-size: 0.875rem;
	}

	.stat-row:last-of-type {
		border-bottom: none;
	}

	.stat-row-highlight {
		font-weight: 600;
	}

	.stat-label {
		color: var(--text-muted);
	}

	.stat-right {
		display: flex;
		align-items: baseline;
		gap: 8px;
	}

	.stat-value {
		font-variant-numeric: tabular-nums;
		font-weight: 600;
	}

	.stat-value-amber {
		color: var(--amber-600);
	}

	.stat-pct {
		font-size: 0.75rem;
		color: var(--text-muted);
		font-variant-numeric: tabular-nums;
	}

	.stat-pct-red { color: var(--red-600); }
	.stat-pct-amber { color: var(--amber-600); }

	.stat-divider {
		height: 1px;
		background: var(--border);
		margin: 8px 0;
	}

	.stats-cta {
		margin-top: 16px;
	}

	.all-clear {
		font-size: 0.8rem;
		color: var(--green-600);
	}

	/* ── Progress bar ──────────────────────────────────────────────────────── */
	.progress-card {
		display: flex;
		flex-direction: column;
		gap: 16px;
	}

	.progress-title {
		font-size: 0.875rem;
		font-weight: 600;
		color: var(--text-muted);
	}

	.progress-bar-container {
		display: flex;
		height: 14px;
		border-radius: 999px;
		overflow: hidden;
		background: var(--gray-100);
	}

	.progress-segment {
		min-width: 0;
		transition: width 0.4s ease;
	}

	.progress-confirmed { background: var(--green-500); }
	.progress-auto      { background: var(--blue-500); }
	.progress-pending   { background: var(--amber-500); }
	.progress-rejected  { background: var(--red-500); }

	.legend {
		display: flex;
		flex-wrap: wrap;
		gap: 12px;
	}

	.legend-item {
		display: flex;
		align-items: center;
		gap: 6px;
		font-size: 0.78rem;
		color: var(--text-muted);
	}

	.legend-dot {
		width: 10px;
		height: 10px;
		border-radius: 50%;
		flex-shrink: 0;
	}

	.legend-confirmed { background: var(--green-500); }
	.legend-auto      { background: var(--blue-500); }
	.legend-pending   { background: var(--amber-500); }
	.legend-rejected  { background: var(--red-500); }

	.no-data {
		color: var(--text-muted);
		font-size: 0.875rem;
	}

	/* ── Tax deadlines ─────────────────────────────────────────────────────── */
	.deadlines-list {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.deadline-card {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 16px;
		padding: 14px 18px;
	}

	.deadline-left {
		display: flex;
		align-items: flex-start;
		gap: 12px;
		min-width: 0;
	}

	.deadline-dot {
		flex-shrink: 0;
		width: 10px;
		height: 10px;
		border-radius: 50%;
		margin-top: 4px;
	}

	.deadline-dot-urgent  { background: var(--red-500); }
	.deadline-dot-soon    { background: var(--amber-500); }
	.deadline-dot-upcoming { background: var(--green-500); }

	.deadline-label {
		font-size: 0.875rem;
		font-weight: 500;
		color: var(--text);
	}

	.deadline-entity {
		font-size: 0.75rem;
		color: var(--text-muted);
		margin-top: 2px;
	}

	.deadline-right {
		text-align: right;
		flex-shrink: 0;
	}

	.deadline-date {
		font-size: 0.8rem;
		color: var(--text-muted);
	}

	.deadline-days {
		font-size: 0.875rem;
		font-weight: 600;
		margin-top: 2px;
	}

	.deadline-days-urgent  { color: var(--red-600); }
	.deadline-days-soon    { color: var(--amber-600); }
	.deadline-days-upcoming { color: var(--green-700); }

	.empty-deadlines {
		padding: 24px;
	}

	/* ── Failure table ─────────────────────────────────────────────────────── */
	.failure-table-wrap {
		overflow-x: auto;
	}

	.td-source { font-weight: 500; white-space: nowrap; }
	.td-time   { white-space: nowrap; color: var(--text-muted); font-size: 0.8rem; }
	.td-records { font-variant-numeric: tabular-nums; font-size: 0.8rem; }
	.td-error  { max-width: 320px; }

	.text-red { color: var(--red-600); }

	.error-toggle {
		cursor: pointer;
		color: var(--red-600);
		font-size: 0.75rem;
	}

	.error-pre-sm {
		margin-top: 4px;
		padding: 6px 8px;
		background: var(--gray-100);
		border-radius: var(--radius-sm);
		font-size: 0.68rem;
		font-family: var(--font-mono);
		white-space: pre-wrap;
		word-break: break-word;
		max-height: 80px;
		overflow-y: auto;
		color: var(--red-700);
	}
</style>
