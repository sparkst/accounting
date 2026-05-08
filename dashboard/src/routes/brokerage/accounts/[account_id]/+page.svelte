<script lang="ts">
	import { page } from '$app/state';
	import {
		fetchBrokerageAccountDetail,
		patchBrokerageAccount,
		type AccountDetailResponse
	} from '$lib/api';

	let detail = $state<AccountDetailResponse | null>(null);
	let loading = $state(true);
	let error = $state('');
	let notFound = $state(false);

	// Edit-form state.
	let editing = $state(false);
	let formName = $state('');
	let formBeneficiary = $state('');
	let formNotes = $state('');
	// Captured originals at form-open time, used to build a true partial PATCH.
	let origName = $state('');
	let origBeneficiary = $state('');
	let origNotes = $state('');
	let saving = $state(false);
	let saveError = $state('');
	let savedAt = $state<string | null>(null);
	let savedTimer: ReturnType<typeof setTimeout> | null = null;

	const accountId = $derived(page.params.account_id ?? '');

	function fmtCurrency(n: number | null | undefined): string {
		if (n === null || n === undefined) return '—';
		const sign = n < 0 ? '-' : '';
		return `${sign}$${Math.abs(n).toLocaleString('en-US', {
			minimumFractionDigits: 2,
			maximumFractionDigits: 2
		})}`;
	}

	function fmtQty(n: number | null | undefined): string {
		if (n === null || n === undefined) return '—';
		return n.toLocaleString('en-US', { maximumFractionDigits: 4 });
	}

	function fmtDate(iso: string | null | undefined): string {
		if (!iso) return '—';
		// ISO can be "YYYY-MM-DD" or full datetime. Take the date portion.
		return iso.slice(0, 10);
	}

	function fmtDateTime(iso: string | null | undefined): string {
		if (!iso) return '—';
		try {
			const d = new Date(iso);
			if (Number.isNaN(d.getTime())) return iso;
			return d.toLocaleString('en-US', { dateStyle: 'short', timeStyle: 'short' });
		} catch {
			return iso;
		}
	}

	function fmtBool(b: boolean | null | undefined): string {
		if (b === null || b === undefined) return '—';
		return b ? 'yes' : 'no';
	}

	function amountClass(n: number | null | undefined): string {
		if (n === null || n === undefined) return '';
		if (n > 0) return 'pos';
		if (n < 0) return 'neg';
		return '';
	}

	function truncate(s: string | null | undefined, n: number): string {
		if (!s) return '';
		return s.length > n ? `${s.slice(0, n)}…` : s;
	}

	const titleName = $derived.by(() => {
		if (!detail) return '';
		const a = detail.account;
		if (a.account_name && a.account_name.trim()) return a.account_name;
		// account_number_masked is e.g. "****1234" — take last 4 chars.
		const masked = a.account_number_masked ?? '';
		const last4 = masked.slice(-4);
		return last4 ? `Account ····${last4}` : 'Account';
	});

	async function load(): Promise<void> {
		if (!accountId) return;
		loading = true;
		error = '';
		notFound = false;
		try {
			detail = await fetchBrokerageAccountDetail(accountId);
		} catch (e) {
			const msg = e instanceof Error ? e.message : String(e);
			if (msg.includes('404')) {
				notFound = true;
			} else {
				error = msg;
			}
		} finally {
			loading = false;
		}
	}

	function startEdit(): void {
		if (!detail) return;
		formName = detail.account.account_name ?? '';
		formBeneficiary = detail.account.beneficiary ?? '';
		formNotes = detail.account.notes ?? '';
		// Capture originals so save() can diff and only send changed keys.
		origName = formName;
		origBeneficiary = formBeneficiary;
		origNotes = formNotes;
		saveError = '';
		editing = true;
	}

	function cancelEdit(): void {
		editing = false;
		saveError = '';
	}

	async function save(): Promise<void> {
		if (!detail) return;
		saving = true;
		saveError = '';
		try {
			// True PATCH semantics: only include keys that changed from the values
			// captured at form-open time. This avoids spurious updated_at bumps
			// when the user opens the form and clicks Save without editing.
			// Empty-string → null keeps the DB clean (no zero-length names).
			const coerce = (s: string): string | null => (s.trim() === '' ? null : s.trim());
			const patch: Record<string, string | null> = {};
			if (formName !== origName) patch.account_name = coerce(formName);
			if (formBeneficiary !== origBeneficiary) patch.beneficiary = coerce(formBeneficiary);
			if (formNotes !== origNotes) patch.notes = coerce(formNotes);
			if (Object.keys(patch).length > 0) {
				await patchBrokerageAccount(detail.account.id, patch);
			}
			// Reload the full detail so derived fields like updated_at refresh.
			await load();
			editing = false;
			savedAt = new Date().toLocaleTimeString('en-US', { timeStyle: 'medium' });
			if (savedTimer) clearTimeout(savedTimer);
			savedTimer = setTimeout(() => {
				savedAt = null;
			}, 3000);
		} catch (e) {
			saveError = e instanceof Error ? e.message : String(e);
		} finally {
			saving = false;
		}
	}

	$effect(() => {
		if (accountId) load();
	});
</script>

<svelte:head>
	<title>{titleName || 'Account'} · Brokerage</title>
</svelte:head>

<div class="page">
	<header class="header">
		<a class="back" href="/brokerage">‹ Back to brokerage</a>
		{#if detail}
			<h1>{titleName}</h1>
			<p class="sub">
				{detail.account.broker} · {detail.account.account_number_masked} · {detail.account.account_type}
			</p>
		{:else if notFound}
			<h1>Account not found</h1>
		{:else}
			<h1>Loading…</h1>
		{/if}
	</header>

	{#if loading && !detail && !notFound}
		<div class="state">Loading…</div>
	{:else if notFound}
		<div class="state error">
			Account <code>{accountId}</code> was not found.
		</div>
	{:else if error}
		<div class="state error">⚠ {error}</div>
	{:else if detail}
		{@const a = detail.account}

		<!-- ── Metadata + Edit ─────────────────────────────────────── -->
		<section class="section">
			<div class="section-head">
				<h2>Account Details</h2>
				{#if !editing}
					<button type="button" class="btn-secondary" onclick={startEdit}>Edit</button>
				{/if}
			</div>

			{#if savedAt}
				<div class="banner success">Saved at {savedAt}</div>
			{/if}

			{#if editing}
				<form
					class="edit-form"
					onsubmit={(e) => {
						e.preventDefault();
						void save();
					}}
				>
					<label class="field">
						<span class="field-label">Account name</span>
						<input
							type="text"
							class="field-input"
							maxlength="128"
							bind:value={formName}
							placeholder="e.g. Travis Trad IRA"
						/>
					</label>
					<label class="field">
						<span class="field-label">Beneficiary</span>
						<input
							type="text"
							class="field-input"
							maxlength="64"
							bind:value={formBeneficiary}
							placeholder="e.g. Amy Sparks"
						/>
					</label>
					<label class="field">
						<span class="field-label">Notes</span>
						<textarea
							class="field-input"
							rows="3"
							maxlength="4096"
							bind:value={formNotes}
							placeholder="Anything worth remembering about this account…"
						></textarea>
					</label>
					{#if saveError}
						<div class="banner error">⚠ {saveError}</div>
					{/if}
					<div class="form-actions">
						<button type="submit" class="btn-primary" disabled={saving}>
							{saving ? 'Saving…' : 'Save'}
						</button>
						<button
							type="button"
							class="btn-secondary"
							onclick={cancelEdit}
							disabled={saving}
						>
							Cancel
						</button>
					</div>
				</form>
			{:else}
				<dl class="meta-grid">
					<dt>Broker</dt><dd>{a.broker}</dd>
					<dt>Account number</dt><dd class="mono">{a.account_number_masked}</dd>
					<dt>Account name</dt><dd>{a.account_name ?? '—'}</dd>
					<dt>Type</dt><dd>{a.account_type}</dd>
					<dt>Entity</dt><dd>{a.entity}</dd>
					<dt>Tax sheltered</dt><dd>{fmtBool(a.tax_sheltered)}</dd>
					<dt>Beneficiary</dt><dd>{a.beneficiary ?? '—'}</dd>
					<dt>Notes</dt><dd class="multiline">{a.notes ?? '—'}</dd>
					<dt>Parent account</dt><dd class="mono">{a.parent_account_id ?? '—'}</dd>
					<dt>Plan wrapper</dt><dd>{fmtBool(a.is_plan_wrapper)}</dd>
					<dt>Tags</dt>
					<dd>
						{#if a.tags.length === 0}
							—
						{:else}
							{a.tags.join(', ')}
						{/if}
					</dd>
					<dt>Created</dt><dd>{fmtDateTime(a.created_at)}</dd>
					<dt>Updated</dt><dd>{fmtDateTime(a.updated_at)}</dd>
				</dl>
			{/if}
		</section>

		<!-- ── Latest Positions ────────────────────────────────────── -->
		<section class="section">
			<h2>Latest Positions <span class="muted small">(top 10)</span></h2>
			{#if detail.latest_position_snapshots.length === 0}
				<p class="muted empty">No position snapshots recorded for this account yet.</p>
			{:else}
				<table class="data-table">
					<thead>
						<tr>
							<th>As of</th>
							<th>Symbol</th>
							<th>Description</th>
							<th class="num">Quantity</th>
							<th class="num">Price</th>
							<th class="num">Market value</th>
						</tr>
					</thead>
					<tbody>
						{#each detail.latest_position_snapshots as p (p.id)}
							<tr>
								<td>{fmtDate(p.as_of)}</td>
								<td><strong>{p.symbol ?? '—'}</strong></td>
								<td class="muted">{p.description ?? '—'}</td>
								<td class="num">{fmtQty(p.quantity)}</td>
								<td class="num">{fmtCurrency(p.price)}</td>
								<td class="num">{fmtCurrency(p.market_value)}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			{/if}
		</section>

		<!-- ── Latest Balance Snapshots ────────────────────────────── -->
		<section class="section">
			<h2>Latest Balance Snapshots <span class="muted small">(top 10)</span></h2>
			{#if detail.latest_balance_snapshots.length === 0}
				<p class="muted empty">No balance snapshots recorded for this account yet.</p>
			{:else}
				<table class="data-table">
					<thead>
						<tr>
							<th>As of</th>
							<th>Raw account name</th>
							<th class="num">Balance</th>
							<th>Source</th>
						</tr>
					</thead>
					<tbody>
						{#each detail.latest_balance_snapshots as b (b.id)}
							<tr>
								<td>{fmtDate(b.as_of)}</td>
								<td>{b.raw_account_name}</td>
								<td class="num">{fmtCurrency(b.balance)}</td>
								<td class="muted">{b.source}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			{/if}
		</section>

		<!-- ── Transaction Summary ──────────────────────────────────── -->
		<section class="section">
			<h2>Transaction Summary</h2>
			<div class="two-col">
				<div>
					<h3 class="subhead">Counts by action</h3>
					{#if Object.keys(detail.transaction_count_by_action).length === 0}
						<p class="muted empty">No transactions recorded for this account.</p>
					{:else}
						<dl class="kv">
							{#each Object.entries(detail.transaction_count_by_action).sort((x, y) => y[1] - x[1]) as [action, n] (action)}
								<dt>{action}</dt>
								<dd class="num">{n.toLocaleString('en-US')}</dd>
							{/each}
						</dl>
					{/if}
				</div>
				<div>
					<h3 class="subhead">Realized G/L (lifetime)</h3>
					<dl class="kv">
						<dt>Short-term</dt>
						<dd class="num {amountClass(detail.realized_gl_summary.short_term)}">
							{fmtCurrency(detail.realized_gl_summary.short_term)}
						</dd>
						<dt>Long-term</dt>
						<dd class="num {amountClass(detail.realized_gl_summary.long_term)}">
							{fmtCurrency(detail.realized_gl_summary.long_term)}
						</dd>
						<dt>Total</dt>
						<dd class="num {amountClass(detail.realized_gl_summary.total)}">
							{fmtCurrency(detail.realized_gl_summary.total)}
						</dd>
						<dt>Lots</dt>
						<dd class="num">{detail.realized_gl_summary.lots.toLocaleString('en-US')}</dd>
					</dl>
				</div>
			</div>
		</section>

		<!-- ── Ingestion History ────────────────────────────────────── -->
		<section class="section">
			<h2>Ingestion History <span class="muted small">(5 most recent)</span></h2>
			{#if detail.ingestion_log_recent.length === 0}
				<p class="muted empty">No ingestion runs have touched this broker yet.</p>
			{:else}
				<table class="data-table">
					<thead>
						<tr>
							<th>Run at</th>
							<th>Source</th>
							<th>Status</th>
							<th class="num">Processed</th>
							<th class="num">Failed</th>
							<th>Error</th>
						</tr>
					</thead>
					<tbody>
						{#each detail.ingestion_log_recent as log (log.id)}
							<tr>
								<td>{fmtDateTime(log.run_at)}</td>
								<td class="mono">{log.source}</td>
								<td>{log.status}</td>
								<td class="num">{log.records_processed.toLocaleString('en-US')}</td>
								<td class="num {log.records_failed > 0 ? 'neg' : ''}">
									{log.records_failed.toLocaleString('en-US')}
								</td>
								<td class="muted small">{truncate(log.error_detail, 100) || '—'}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			{/if}
		</section>
	{/if}
</div>

<style>
	.page {
		max-width: 1000px;
		margin: 0 auto;
		padding: 24px;
		font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Helvetica Neue', sans-serif;
		color: #1d1d1f;
	}
	.header h1 {
		font-size: 32px;
		font-weight: 600;
		margin: 4px 0 4px 0;
	}
	.back {
		font-size: 13px;
		color: #007aff;
		text-decoration: none;
	}
	.back:hover {
		text-decoration: underline;
	}
	.sub {
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
		background: #fff;
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

	.subhead {
		font-size: 12px;
		font-weight: 500;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: #6e6e73;
		margin: 0 0 8px;
	}

	.banner {
		padding: 8px 12px;
		border-radius: 8px;
		font-size: 13px;
		margin-bottom: 12px;
	}
	.banner.success {
		background: #e8f5e9;
		color: #047a04;
		border: 1px solid #c8e6c9;
	}
	.banner.error {
		background: #fdecea;
		color: #d70015;
		border: 1px solid #f5c6c6;
	}

	.btn-primary {
		font: inherit;
		font-size: 13px;
		padding: 6px 16px;
		border: 1px solid #007aff;
		background: #007aff;
		color: #fff;
		border-radius: 6px;
		cursor: pointer;
	}
	.btn-primary:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}
	.btn-secondary {
		font: inherit;
		font-size: 13px;
		padding: 6px 16px;
		border: 1px solid #d2d2d7;
		background: #fff;
		color: #1d1d1f;
		border-radius: 6px;
		cursor: pointer;
	}
	.btn-secondary:hover {
		background: #f5f5f7;
	}

	.edit-form {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}
	.field {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}
	.field-label {
		font-size: 12px;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: #6e6e73;
	}
	.field-input {
		font: inherit;
		font-size: 14px;
		padding: 6px 10px;
		border: 1px solid #d2d2d7;
		border-radius: 6px;
		background: #fff;
		color: #1d1d1f;
		width: 100%;
		box-sizing: border-box;
		font-family: inherit;
	}
	.field-input:focus {
		outline: none;
		border-color: #007aff;
		box-shadow: 0 0 0 3px rgba(0, 122, 255, 0.15);
	}
	.form-actions {
		display: flex;
		gap: 8px;
		margin-top: 4px;
	}

	.meta-grid {
		display: grid;
		grid-template-columns: max-content 1fr;
		gap: 8px 24px;
		margin: 0;
		font-size: 14px;
	}
	.meta-grid dt {
		color: #6e6e73;
		font-size: 13px;
	}
	.meta-grid dd {
		margin: 0;
		color: #1d1d1f;
	}
	.meta-grid dd.multiline {
		white-space: pre-wrap;
	}

	.two-col {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 24px;
	}
	@media (max-width: 640px) {
		.two-col {
			grid-template-columns: 1fr;
		}
	}

	.kv {
		display: grid;
		grid-template-columns: 1fr max-content;
		gap: 4px 16px;
		margin: 0;
		font-size: 14px;
	}
	.kv dt {
		color: #6e6e73;
	}
	.kv dd {
		margin: 0;
		font-feature-settings: 'tnum' 1;
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
		vertical-align: top;
	}
	.data-table .num {
		text-align: right;
		font-variant-numeric: tabular-nums;
	}

	.muted {
		color: #6e6e73;
	}
	.small {
		font-size: 12px;
		font-weight: 400;
		text-transform: none;
		letter-spacing: 0;
	}
	.empty {
		text-align: center;
		padding: 16px;
		font-style: italic;
	}
	.pos {
		color: #047a04;
	}
	.neg {
		color: #d70015;
	}
	.mono {
		font-family: ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, monospace;
		font-size: 13px;
	}
	code {
		font-family: ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, monospace;
		background: #f5f5f7;
		padding: 1px 6px;
		border-radius: 4px;
		font-size: 13px;
	}
</style>
