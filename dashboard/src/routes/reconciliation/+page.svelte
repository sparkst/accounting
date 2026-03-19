<script lang="ts">
	import { onMount } from 'svelte';

	// ── Types ─────────────────────────────────────────────────────────────────

	interface TransactionSlim {
		id: string;
		source: string;
		date: string;
		description: string;
		amount: number | null;
		payment_method: string | null;
		status: string;
		notes: string | null;
	}

	interface MatchedPair {
		payout: TransactionSlim;
		bank: TransactionSlim;
		confidence: number;
		date_diff_days: number;
		card_match: boolean;
	}

	interface UnmatchedOut {
		payouts: TransactionSlim[];
		banks: TransactionSlim[];
	}

	interface MonthlyTotal {
		month: string;
		payout_total: number;
		bank_total: number;
		discrepancy: number;
		flagged: boolean;
	}

	interface RunSummary {
		matched_count: number;
		unmatched_payout_count: number;
		unmatched_bank_count: number;
		flagged_months: number;
		monthly_totals: MonthlyTotal[];
	}

	// ── State ─────────────────────────────────────────────────────────────────

	let loading = $state(true);
	let running = $state(false);
	let error = $state('');
	let actionError = $state('');

	let summary = $state<RunSummary | null>(null);
	let matched = $state<MatchedPair[]>([]);
	let unmatched = $state<UnmatchedOut>({ payouts: [], banks: [] });

	// Manual match selection
	let selectedA = $state<string | null>(null);
	let selectedB = $state<string | null>(null);
	let linkingInProgress = $state(false);
	let linkSuccess = $state('');

	// View toggle
	let activeTab = $state<'matched' | 'unmatched' | 'monthly'>('matched');

	// ── Derived ───────────────────────────────────────────────────────────────

	let flaggedMonths = $derived(
		summary ? summary.monthly_totals.filter((m) => m.flagged) : []
	);

	let canLink = $derived(selectedA !== null && selectedB !== null && selectedA !== selectedB);

	// ── API helpers ───────────────────────────────────────────────────────────

	const BASE = '/api';

	async function apiGet<T>(path: string): Promise<T> {
		const res = await fetch(`${BASE}${path}`);
		if (!res.ok) {
			const text = await res.text().catch(() => res.statusText);
			throw new Error(`API ${res.status}: ${text}`);
		}
		return res.json() as Promise<T>;
	}

	async function apiPost<T>(path: string, body?: unknown): Promise<T> {
		const res = await fetch(`${BASE}${path}`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: body !== undefined ? JSON.stringify(body) : undefined
		});
		if (!res.ok) {
			const text = await res.text().catch(() => res.statusText);
			throw new Error(`API ${res.status}: ${text}`);
		}
		return res.json() as Promise<T>;
	}

	// ── Load ─────────────────────────────────────────────────────────────────

	async function load() {
		loading = true;
		error = '';
		try {
			const [sum, mat, unmat] = await Promise.all([
				apiPost<RunSummary>('/reconcile/run'),
				apiGet<MatchedPair[]>('/reconcile/matched'),
				apiGet<UnmatchedOut>('/reconcile/unmatched')
			]);
			summary = sum;
			matched = mat;
			unmatched = unmat;
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load reconciliation data';
		} finally {
			loading = false;
		}
	}

	async function runReconciliation() {
		running = true;
		actionError = '';
		try {
			const [sum, mat, unmat] = await Promise.all([
				apiPost<RunSummary>('/reconcile/run'),
				apiGet<MatchedPair[]>('/reconcile/matched'),
				apiGet<UnmatchedOut>('/reconcile/unmatched')
			]);
			summary = sum;
			matched = mat;
			unmatched = unmat;
		} catch (e) {
			actionError = e instanceof Error ? e.message : 'Run failed';
		} finally {
			running = false;
		}
	}

	async function linkSelected() {
		if (!selectedA || !selectedB) return;
		linkingInProgress = true;
		actionError = '';
		linkSuccess = '';
		try {
			await apiPost('/reconcile/manual-match', {
				transaction_id_a: selectedA,
				transaction_id_b: selectedB
			});
			linkSuccess = 'Linked successfully. Refreshing…';
			selectedA = null;
			selectedB = null;
			await load();
			linkSuccess = '';
		} catch (e) {
			actionError = e instanceof Error ? e.message : 'Link failed';
		} finally {
			linkingInProgress = false;
		}
	}

	function toggleSelectA(id: string) {
		selectedA = selectedA === id ? null : id;
	}

	function toggleSelectB(id: string) {
		selectedB = selectedB === id ? null : id;
	}

	// ── Formatters ────────────────────────────────────────────────────────────

	function fmtAmount(amount: number | null): string {
		if (amount === null) return '—';
		return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(amount);
	}

	function fmtDate(iso: string): string {
		const d = new Date(iso + 'T00:00:00');
		return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
	}

	function fmtMonth(ym: string): string {
		const [y, m] = ym.split('-');
		const d = new Date(Number(y), Number(m) - 1, 1);
		return d.toLocaleDateString('en-US', { month: 'long', year: 'numeric' });
	}

	function sourceLabel(source: string): string {
		const labels: Record<string, string> = {
			stripe: 'Stripe',
			shopify: 'Shopify',
			bank_csv: 'Bank CSV',
			gmail_n8n: 'Gmail',
			brokerage_csv: 'Brokerage',
			photo_receipt: 'Receipt',
			deduction_email: 'Deduction'
		};
		return labels[source] ?? source;
	}

	function confidenceLabel(conf: number): string {
		if (conf >= 0.9) return 'High';
		if (conf >= 0.7) return 'Medium';
		return 'Low';
	}

	function confidenceClass(conf: number): string {
		if (conf >= 0.9) return 'confidence-high';
		if (conf >= 0.7) return 'confidence-medium';
		return 'confidence-low';
	}

	onMount(() => {
		load();
	});
</script>

<div class="container page-shell">
	<!-- ── Header ───────────────────────────────────────────────────────────── -->
	<header class="page-header">
		<div>
			<h1>Reconciliation</h1>
			<p class="page-subtitle">
				Match Stripe and Shopify payouts to bank deposits
			</p>
		</div>
		<div class="page-header-actions">
			<button class="btn btn-ghost" onclick={runReconciliation} disabled={running || loading}>
				{running ? 'Running…' : 'Re-run'}
			</button>
		</div>
	</header>

	{#if error}
		<div class="error-banner">
			<span>{error}</span>
			<button class="dismiss-btn" onclick={() => (error = '')}>✕</button>
		</div>
	{/if}

	{#if actionError}
		<div class="error-banner">
			<span>{actionError}</span>
			<button class="dismiss-btn" onclick={() => (actionError = '')}>✕</button>
		</div>
	{/if}

	{#if linkSuccess}
		<div class="success-banner">
			<span>{linkSuccess}</span>
		</div>
	{/if}

	{#if loading && !summary}
		<!-- Skeleton -->
		<div class="skeleton-grid">
			{#each Array(3) as _}
				<div class="card skeleton-card">
					<div class="skeleton" style="height: 14px; width: 40%; margin-bottom: 10px;"></div>
					<div class="skeleton" style="height: 28px; width: 60%;"></div>
				</div>
			{/each}
		</div>
	{:else if summary}

		<!-- ── Summary cards ─────────────────────────────────────────────────── -->
		<div class="summary-grid">
			<div class="card summary-card">
				<p class="summary-label">Matched pairs</p>
				<p class="summary-value summary-green">{summary.matched_count}</p>
			</div>
			<div class="card summary-card">
				<p class="summary-label">Unmatched payouts</p>
				<p class="summary-value {summary.unmatched_payout_count > 0 ? 'summary-amber' : ''}">
					{summary.unmatched_payout_count}
				</p>
			</div>
			<div class="card summary-card">
				<p class="summary-label">Unmatched bank</p>
				<p class="summary-value {summary.unmatched_bank_count > 0 ? 'summary-amber' : ''}">
					{summary.unmatched_bank_count}
				</p>
			</div>
			<div class="card summary-card">
				<p class="summary-label">Flagged months</p>
				<p class="summary-value {summary.flagged_months > 0 ? 'summary-red' : ''}">
					{summary.flagged_months}
				</p>
			</div>
		</div>

		<!-- ── Flagged months alert ──────────────────────────────────────────── -->
		{#if flaggedMonths.length > 0}
			<div class="flagged-alert card">
				<p class="flagged-title">Monthly total discrepancies detected</p>
				<ul class="flagged-list">
					{#each flaggedMonths as m}
						<li>
							<strong>{fmtMonth(m.month)}</strong> —
							payouts {fmtAmount(m.payout_total)}, bank {fmtAmount(m.bank_total)},
							difference <span class="text-red">{fmtAmount(m.discrepancy)}</span>
						</li>
					{/each}
				</ul>
			</div>
		{/if}

		<!-- ── Tabs ──────────────────────────────────────────────────────────── -->
		<div class="tabs">
			<button
				class="tab-btn {activeTab === 'matched' ? 'tab-active' : ''}"
				onclick={() => (activeTab = 'matched')}
			>
				Matched ({matched.length})
			</button>
			<button
				class="tab-btn {activeTab === 'unmatched' ? 'tab-active' : ''}"
				onclick={() => (activeTab = 'unmatched')}
			>
				Unmatched ({unmatched.payouts.length + unmatched.banks.length})
			</button>
			<button
				class="tab-btn {activeTab === 'monthly' ? 'tab-active' : ''}"
				onclick={() => (activeTab = 'monthly')}
			>
				Monthly totals ({summary.monthly_totals.length})
			</button>
		</div>

		<!-- ── Matched pairs ─────────────────────────────────────────────────── -->
		{#if activeTab === 'matched'}
			{#if matched.length === 0}
				<div class="card empty-state">
					<p class="icon">⟷</p>
					<p>No matched pairs yet. Run reconciliation after importing bank CSV data.</p>
				</div>
			{:else}
				<div class="card table-wrap">
					<table class="data-table">
						<thead>
							<tr>
								<th>Date</th>
								<th>Payout source</th>
								<th>Description</th>
								<th class="num-col">Amount</th>
								<th>Bank date</th>
								<th>Date diff</th>
								<th>Card</th>
								<th>Confidence</th>
							</tr>
						</thead>
						<tbody>
							{#each matched as pair (pair.payout.id + pair.bank.id)}
								<tr class="row-matched">
									<td class="td-date">{fmtDate(pair.payout.date)}</td>
									<td>
										<span class="source-pill">{sourceLabel(pair.payout.source)}</span>
									</td>
									<td class="td-desc truncate">{pair.payout.description}</td>
									<td class="num-col amount-positive">{fmtAmount(pair.payout.amount)}</td>
									<td class="td-date">{fmtDate(pair.bank.date)}</td>
									<td class="num-col">
										{#if pair.date_diff_days === 0}
											<span class="diff-same">same day</span>
										{:else}
											{pair.date_diff_days}d
										{/if}
									</td>
									<td>
										{#if pair.card_match}
											<span class="card-match-yes" title={pair.payout.payment_method ?? ''}>✓</span>
										{:else}
											<span class="card-match-no">—</span>
										{/if}
									</td>
									<td>
										<span class="confidence-badge {confidenceClass(pair.confidence)}">
											{confidenceLabel(pair.confidence)}
										</span>
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			{/if}
		{/if}

		<!-- ── Unmatched ─────────────────────────────────────────────────────── -->
		{#if activeTab === 'unmatched'}
			<div class="unmatched-layout">
				<!-- Unmatched payouts -->
				<section class="unmatched-section">
					<h2 class="section-title">Unmatched payouts ({unmatched.payouts.length})</h2>
					{#if unmatched.payouts.length === 0}
						<div class="card empty-state small-empty">
							<p>All payouts matched.</p>
						</div>
					{:else}
						<div class="card table-wrap">
							<table class="data-table">
								<thead>
									<tr>
										<th class="sel-col">Select A</th>
										<th>Date</th>
										<th>Source</th>
										<th>Description</th>
										<th class="num-col">Amount</th>
										<th>Card</th>
									</tr>
								</thead>
								<tbody>
									{#each unmatched.payouts as txn (txn.id)}
										<tr
											class="row-unmatched {selectedA === txn.id ? 'row-selected-a' : ''}"
											onclick={() => toggleSelectA(txn.id)}
										>
											<td class="sel-col">
												<input
													type="checkbox"
													checked={selectedA === txn.id}
													onchange={() => toggleSelectA(txn.id)}
													aria-label="Select as A"
												/>
											</td>
											<td class="td-date">{fmtDate(txn.date)}</td>
											<td>
												<span class="source-pill">{sourceLabel(txn.source)}</span>
											</td>
											<td class="td-desc truncate">{txn.description}</td>
											<td class="num-col amount-positive">{fmtAmount(txn.amount)}</td>
											<td class="td-card">{txn.payment_method ?? '—'}</td>
										</tr>
									{/each}
								</tbody>
							</table>
						</div>
					{/if}
				</section>

				<!-- Unmatched bank deposits -->
				<section class="unmatched-section">
					<h2 class="section-title">Unmatched bank deposits ({unmatched.banks.length})</h2>
					{#if unmatched.banks.length === 0}
						<div class="card empty-state small-empty">
							<p>All bank deposits matched.</p>
						</div>
					{:else}
						<div class="card table-wrap">
							<table class="data-table">
								<thead>
									<tr>
										<th class="sel-col">Select B</th>
										<th>Date</th>
										<th>Description</th>
										<th class="num-col">Amount</th>
										<th>Card</th>
									</tr>
								</thead>
								<tbody>
									{#each unmatched.banks as txn (txn.id)}
										<tr
											class="row-unmatched {selectedB === txn.id ? 'row-selected-b' : ''}"
											onclick={() => toggleSelectB(txn.id)}
										>
											<td class="sel-col">
												<input
													type="checkbox"
													checked={selectedB === txn.id}
													onchange={() => toggleSelectB(txn.id)}
													aria-label="Select as B"
												/>
											</td>
											<td class="td-date">{fmtDate(txn.date)}</td>
											<td class="td-desc truncate">{txn.description}</td>
											<td class="num-col amount-positive">{fmtAmount(txn.amount)}</td>
											<td class="td-card">{txn.payment_method ?? '—'}</td>
										</tr>
									{/each}
								</tbody>
							</table>
						</div>
					{/if}
				</section>
			</div>

			<!-- Link action bar -->
			{#if unmatched.payouts.length > 0 || unmatched.banks.length > 0}
				<div class="link-bar card">
					<div class="link-bar-info">
						{#if selectedA && selectedB}
							<p class="link-ready">
								Ready to link payout <code>{selectedA.slice(0, 8)}</code> with bank <code>{selectedB.slice(0, 8)}</code>
							</p>
						{:else if selectedA}
							<p class="link-hint">Now select a bank deposit (B) to link with the selected payout.</p>
						{:else if selectedB}
							<p class="link-hint">Now select a payout (A) to link with the selected bank deposit.</p>
						{:else}
							<p class="link-hint">Select one item from each list to manually link them.</p>
						{/if}
					</div>
					<button
						class="btn btn-primary"
						disabled={!canLink || linkingInProgress}
						onclick={linkSelected}
					>
						{linkingInProgress ? 'Linking…' : 'Link selected'}
					</button>
				</div>
			{/if}
		{/if}

		<!-- ── Monthly totals ────────────────────────────────────────────────── -->
		{#if activeTab === 'monthly'}
			{#if summary.monthly_totals.length === 0}
				<div class="card empty-state">
					<p class="icon">📅</p>
					<p>No transactions to compare yet.</p>
				</div>
			{:else}
				<div class="card table-wrap">
					<table class="data-table">
						<thead>
							<tr>
								<th>Month</th>
								<th class="num-col">Payout total</th>
								<th class="num-col">Bank total</th>
								<th class="num-col">Discrepancy</th>
								<th>Status</th>
							</tr>
						</thead>
						<tbody>
							{#each summary.monthly_totals as m (m.month)}
								<tr class="{m.flagged ? 'row-flagged' : ''}">
									<td class="td-month">{fmtMonth(m.month)}</td>
									<td class="num-col">{fmtAmount(m.payout_total)}</td>
									<td class="num-col">{fmtAmount(m.bank_total)}</td>
									<td class="num-col {m.flagged ? 'text-red' : 'text-muted'}">
										{fmtAmount(m.discrepancy)}
									</td>
									<td>
										{#if m.flagged}
											<span class="status-pill status-needs_review">Discrepancy</span>
										{:else}
											<span class="status-pill status-confirmed">Balanced</span>
										{/if}
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			{/if}
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

	/* ── Banners ───────────────────────────────────────────────────────────── */
	.error-banner {
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

	.success-banner {
		padding: 10px 16px;
		background: var(--green-100);
		border: 1px solid var(--green-500);
		border-radius: var(--radius-sm);
		color: var(--green-700);
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

	/* ── Skeleton ──────────────────────────────────────────────────────────── */
	.skeleton-grid {
		display: grid;
		grid-template-columns: repeat(3, 1fr);
		gap: 12px;
		margin-bottom: 32px;
	}

	.skeleton-card {
		padding: 20px;
	}

	/* ── Summary cards ─────────────────────────────────────────────────────── */
	.summary-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
		gap: 12px;
		margin-bottom: 24px;
	}

	.summary-card {
		padding: 18px 20px;
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.summary-label {
		font-size: 0.75rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--text-muted);
	}

	.summary-value {
		font-size: 2rem;
		font-weight: 700;
		font-variant-numeric: tabular-nums;
		color: var(--text);
	}

	.summary-green { color: var(--green-600); }
	.summary-amber { color: var(--amber-600); }
	.summary-red   { color: var(--red-600); }

	/* ── Flagged alert ─────────────────────────────────────────────────────── */
	.flagged-alert {
		padding: 16px 20px;
		margin-bottom: 20px;
		border-left: 4px solid var(--red-500);
	}

	.flagged-title {
		font-size: 0.875rem;
		font-weight: 600;
		color: var(--red-700);
		margin-bottom: 8px;
	}

	.flagged-list {
		list-style: none;
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.flagged-list li {
		font-size: 0.875rem;
		color: var(--text-muted);
	}

	.text-red   { color: var(--red-600); font-weight: 600; }
	.text-muted { color: var(--text-muted); }

	/* ── Tabs ──────────────────────────────────────────────────────────────── */
	.tabs {
		display: flex;
		gap: 4px;
		margin-bottom: 16px;
		border-bottom: 1px solid var(--border);
		padding-bottom: 0;
	}

	.tab-btn {
		padding: 8px 16px;
		font-size: 0.875rem;
		font-weight: 500;
		color: var(--text-muted);
		background: none;
		border: none;
		border-bottom: 2px solid transparent;
		cursor: pointer;
		transition: color 0.12s, border-color 0.12s;
		margin-bottom: -1px;
	}

	.tab-btn:hover {
		color: var(--text);
	}

	.tab-active {
		color: var(--text);
		border-bottom-color: var(--gray-900);
	}

	/* ── Table ─────────────────────────────────────────────────────────────── */
	.table-wrap {
		overflow-x: auto;
		margin-bottom: 20px;
	}

	.num-col {
		text-align: right;
		font-variant-numeric: tabular-nums;
	}

	.sel-col {
		width: 48px;
		text-align: center;
	}

	.td-date {
		white-space: nowrap;
		color: var(--text-muted);
		font-size: 0.8rem;
	}

	.td-desc {
		max-width: 260px;
	}

	.td-card {
		font-size: 0.8rem;
		color: var(--text-muted);
		white-space: nowrap;
	}

	.td-month {
		font-weight: 500;
	}

	/* ── Row states ────────────────────────────────────────────────────────── */
	.row-matched td {
		background: var(--green-100);
	}

	.row-unmatched {
		cursor: pointer;
	}

	.row-unmatched td {
		background: #fffbeb; /* amber-50 equivalent */
	}

	.row-unmatched:hover td {
		background: #fef3c7; /* amber-100 */
	}

	.row-selected-a td {
		background: #dbeafe !important; /* blue-100 */
		outline: 2px solid var(--blue-500);
		outline-offset: -1px;
	}

	.row-selected-b td {
		background: #d1fae5 !important; /* green-100 */
		outline: 2px solid var(--green-500);
		outline-offset: -1px;
	}

	.row-flagged td {
		background: #fff1f2; /* rose-50 */
	}

	/* ── Confidence badge ──────────────────────────────────────────────────── */
	/* (inherits global .confidence-badge, .confidence-high, etc.) */

	/* ── Source pill ───────────────────────────────────────────────────────── */
	.source-pill {
		display: inline-flex;
		align-items: center;
		padding: 2px 8px;
		border-radius: 999px;
		font-size: 0.7rem;
		font-weight: 600;
		background: var(--gray-100);
		color: var(--gray-600);
		white-space: nowrap;
	}

	/* ── Date diff ─────────────────────────────────────────────────────────── */
	.diff-same {
		font-size: 0.75rem;
		color: var(--green-600);
		font-weight: 600;
	}

	/* ── Card match ────────────────────────────────────────────────────────── */
	.card-match-yes {
		color: var(--green-600);
		font-weight: 700;
	}

	.card-match-no {
		color: var(--gray-400);
	}

	/* ── Unmatched layout ──────────────────────────────────────────────────── */
	.unmatched-layout {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 20px;
		margin-bottom: 16px;
	}

	@media (max-width: 800px) {
		.unmatched-layout {
			grid-template-columns: 1fr;
		}
	}

	.unmatched-section {
		display: flex;
		flex-direction: column;
		gap: 10px;
	}

	.section-title {
		font-size: 0.875rem;
		font-weight: 600;
		color: var(--text-muted);
		margin: 0;
	}

	/* ── Link bar ──────────────────────────────────────────────────────────── */
	.link-bar {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 16px;
		padding: 14px 18px;
		margin-bottom: 20px;
	}

	.link-bar-info {
		flex: 1;
	}

	.link-hint {
		font-size: 0.875rem;
		color: var(--text-muted);
	}

	.link-ready {
		font-size: 0.875rem;
		color: var(--text);
		font-weight: 500;
	}

	.link-ready code {
		font-family: var(--font-mono);
		background: var(--gray-100);
		padding: 1px 5px;
		border-radius: 4px;
		font-size: 0.8rem;
	}

	/* ── Empty states ──────────────────────────────────────────────────────── */
	.small-empty {
		padding: 24px;
		display: flex;
		align-items: center;
		justify-content: center;
	}

	.small-empty p {
		color: var(--text-muted);
		font-size: 0.875rem;
	}
</style>
