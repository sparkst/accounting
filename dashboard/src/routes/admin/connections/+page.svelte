<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import {
		plaidCreateLinkToken,
		plaidExchangePublicToken,
		plaidDisconnect,
		plaidRelink,
		plaidListItems,
		plaidSyncNow,
		plaidReconciliationSummary,
		plaidMapAccounts,
		type PlaidItemSummary,
		type PlaidReconciliationRow,
		type PlaidExchangeResponse,
		type PlaidAccountFromExchange
	} from '$lib/api';
	import { accountMapBanner } from '$lib/plaidAccountMap';

	const PLAID_LINK_SCRIPT_URL = 'https://cdn.plaid.com/link/v2/stable/link-initialize.js';

	let items = $state<PlaidItemSummary[]>([]);
	let reconciliation = $state<PlaidReconciliationRow[]>([]);
	let loading = $state(true);
	let errorMsg = $state('');
	let syncingItem = $state<string | null>(null);

	// Mapping flow state — populated after a successful exchange.
	let pendingExchange = $state<PlaidExchangeResponse | null>(null);
	let pendingInstitutionName = $state('');
	// plaid_account_id → 'skip' | 'create_new'.
	let mappingChoices = $state<Record<string, string>>({});

	let plaidLinkLoaded = $state(false);

	// REQ-PC-B5: per-link scope choice. 'register' feeds the cash-basis register
	// (with the account-mapping step); 'wealth' pushes balances/holdings to the
	// wealth D1 only and skips register mapping entirely. Wealth is the default:
	// the register's two feeds (Chase, Amex) are long-linked, so nearly every new
	// link is a wealth connection — a register default caused two accidental
	// register links (Schwab + Vanguard, 2026-07-27) that needed server-side repair.
	let newLinkScope = $state<'register' | 'wealth'>('wealth');

	// P1-a1x/P1-r3e/P1-r3c: a wealth link is only usable if its account-map push
	// to the wealth D1 landed cleanly — otherwise every balance/holding for the
	// new Item is skipped there. Derived rather than assumed.
	const wealthBanner = $derived(
		pendingExchange && pendingExchange.scope === 'wealth'
			? accountMapBanner(pendingExchange, pendingInstitutionName)
			: null
	);

	// State for in-flight Plaid Link sessions. The OAuth-return page postMessages
	// back here so we can finalize OAuth-bank flows by reopening Link with
	// receivedRedirectUri (Plaid's required signal after the bank redirect).
	let activeLinkToken: string | null = $state(null);
	let activeStateNonce: string | null = $state(null);
	let activeInstitutionFromExchange: { id: string; name: string } | null = $state(null);
	let activeIsRelinkItemId: string | null = $state(null);

	async function loadItems() {
		try {
			loading = true;
			[items, reconciliation] = await Promise.all([
				plaidListItems(),
				plaidReconciliationSummary().catch(() => [] as PlaidReconciliationRow[])
			]);
			errorMsg = '';
		} catch (e) {
			errorMsg = `Failed to load: ${e instanceof Error ? e.message : String(e)}`;
		} finally {
			loading = false;
		}
	}

	function loadPlaidLinkScript(): Promise<void> {
		if (plaidLinkLoaded) return Promise.resolve();
		return new Promise((resolve, reject) => {
			const existing = document.querySelector<HTMLScriptElement>(
				`script[src="${PLAID_LINK_SCRIPT_URL}"]`
			);
			if (existing) {
				plaidLinkLoaded = true;
				resolve();
				return;
			}
			const s = document.createElement('script');
			s.src = PLAID_LINK_SCRIPT_URL;
			s.async = true;
			s.onload = () => {
				plaidLinkLoaded = true;
				resolve();
			};
			s.onerror = () => reject(new Error('Failed to load Plaid Link script'));
			document.head.appendChild(s);
		});
	}

	async function startAddConnection() {
		errorMsg = '';
		try {
			await loadPlaidLinkScript();
			// link_token must be requested at click time — 30 min TTL.
			const { link_token, state_nonce } = await plaidCreateLinkToken(newLinkScope);
			activeLinkToken = link_token;
			activeStateNonce = state_nonce;
			activeIsRelinkItemId = null;
			openPlaidLink(link_token, state_nonce, null, null);
		} catch (e) {
			errorMsg = `${e instanceof Error ? e.message : String(e)}`;
		}
	}

	function openPlaidLink(
		token: string,
		stateNonce: string | null,
		receivedRedirectUri: string | null,
		relinkItemId: string | null
	) {
		// @ts-expect-error — Plaid is injected on window by the CDN script
		const handler = window.Plaid.create({
			token,
			receivedRedirectUri: receivedRedirectUri ?? undefined,
			onSuccess: async (
				public_token: string,
				metadata: { institution: { name: string; institution_id: string } }
			) => {
				if (relinkItemId) {
					// Update-mode link: Plaid keeps the same item_id, no exchange needed.
					activeLinkToken = null;
					activeStateNonce = null;
					activeIsRelinkItemId = null;
					await loadItems();
					return;
				}
				if (!stateNonce) {
					errorMsg = 'Internal error: state_nonce missing from link flow.';
					return;
				}
				try {
					const resp = await plaidExchangePublicToken({
						public_token,
						state_nonce: stateNonce,
						institution_id: metadata.institution.institution_id,
						institution_name: metadata.institution.name
					});
					pendingExchange = resp;
					pendingInstitutionName = metadata.institution.name;
					mappingChoices = {};
					for (const acct of resp.accounts) {
						mappingChoices[acct.account_id] = 'skip';
					}
					activeLinkToken = null;
					activeStateNonce = null;
				} catch (e) {
					errorMsg = `Exchange failed: ${e instanceof Error ? e.message : String(e)}`;
				}
			},
			onExit: (err: unknown) => {
				if (err) {
					errorMsg = `Plaid Link exited: ${JSON.stringify(err)}`;
				}
				activeLinkToken = null;
				activeStateNonce = null;
				activeIsRelinkItemId = null;
			}
		});
		handler.open();
	}

	// OAuth banks redirect to /admin/connections/oauth-return, which postMessage's
	// the redirect URL back here so we can reopen Plaid Link with
	// `receivedRedirectUri` to complete the flow. Without this, OAuth-bank links
	// silently stall after the bank redirect.
	function handleOAuthReturn(evt: MessageEvent) {
		if (evt.origin !== window.location.origin) return;
		const data = evt.data;
		if (!data || data.type !== 'plaid_oauth_return' || typeof data.url !== 'string') return;
		if (!activeLinkToken) {
			errorMsg = 'Got OAuth return but no active link session. Try again.';
			return;
		}
		openPlaidLink(activeLinkToken, activeStateNonce, data.url, activeIsRelinkItemId);
	}

	async function confirmMappings() {
		if (!pendingExchange) return;
		const mappings: Array<{
			plaid_account_id: string;
			create_new?: { broker: string; account_number: string; account_name: string; account_type: string };
		}> = [];
		for (const acct of pendingExchange.accounts) {
			const choice = mappingChoices[acct.account_id];
			if (choice === 'create_new') {
				mappings.push({
					plaid_account_id: acct.account_id,
					create_new: {
						broker: inferBroker(pendingInstitutionName),
						account_number: acct.mask || `plaid-${acct.account_id.slice(-6)}`,
						account_name: acct.name || acct.official_name || 'Plaid Account',
						account_type: inferAccountType(acct.type, acct.subtype)
					}
				});
			}
		}
		if (mappings.length === 0) {
			pendingExchange = null;
			return;
		}
		try {
			await plaidMapAccounts({
				item_id: pendingExchange.plaid_item_id,
				mappings
			});
			pendingExchange = null;
			await loadItems();
		} catch (e) {
			errorMsg = `Mapping failed: ${e instanceof Error ? e.message : String(e)}`;
		}
	}

	function inferBroker(institutionName: string): string {
		const name = institutionName.toLowerCase();
		if (name.includes('vanguard')) return 'vanguard';
		if (name.includes('schwab')) return 'schwab';
		if (name.includes('fidelity')) return 'fidelity';
		if (name.includes('etrade') || name.includes('e*trade')) return 'etrade';
		if (name.includes('franklin')) return 'franklin_templeton';
		if (name.includes('chase')) return 'chase';
		if (name.includes('amex') || name.includes('american express')) return 'amex';
		return 'other'; // valid generic fallback (broker enum includes 'other')
	}

	function inferAccountType(plaidType: string | null | undefined, subtype: string | null | undefined): string {
		const sub = (subtype ?? '').toLowerCase();
		switch (plaidType) {
			case 'depository':
				return sub === 'savings' ? 'savings' : 'checking';
			case 'credit':
				return 'credit_card';
			case 'brokerage':
			case 'investment':
				return 'taxable';
			default:
				return 'other';
		}
	}

	async function syncItem(itemId: string) {
		syncingItem = itemId;
		try {
			await plaidSyncNow(itemId);
			await loadItems();
		} catch (e) {
			errorMsg = `Sync failed: ${e instanceof Error ? e.message : String(e)}`;
		} finally {
			syncingItem = null;
		}
	}

	async function disconnectItem(item: PlaidItemSummary) {
		const ok = confirm(
			`Disconnect ${item.institution_name}? This calls Plaid /item/remove and frees the slot. Accounts stay in the system; they just lose the Plaid link.`
		);
		if (!ok) return;
		try {
			await plaidDisconnect(item.id);
			await loadItems();
		} catch (e) {
			errorMsg = `Disconnect failed: ${e instanceof Error ? e.message : String(e)}`;
		}
	}

	async function relinkItem(item: PlaidItemSummary) {
		try {
			await loadPlaidLinkScript();
			const { link_token } = await plaidRelink(item.id);
			activeLinkToken = link_token;
			activeStateNonce = null;
			activeIsRelinkItemId = item.id;
			openPlaidLink(link_token, null, null, item.id);
		} catch (e) {
			errorMsg = `${e instanceof Error ? e.message : String(e)}`;
		}
	}

	function statusBadgeClass(item: PlaidItemSummary): string {
		if (item.last_sync_status === 'ok') return 'badge-ok';
		if (item.last_sync_status === 'institution_down') return 'badge-warn';
		if (item.last_sync_status === 'error') return 'badge-error';
		return 'badge-neutral';
	}

	function statusLabel(item: PlaidItemSummary): string {
		if (!item.last_sync_at) return 'Never synced';
		if (item.last_sync_status === 'ok') return 'OK';
		if (item.last_sync_status === 'institution_down') return 'Institution down (will retry)';
		if (item.last_sync_status === 'error') return `Error: ${item.last_error ?? 'unknown'}`;
		return item.last_sync_status ?? '—';
	}

	function needsRelink(item: PlaidItemSummary): boolean {
		const terminalCodes = new Set([
			'ITEM_LOGIN_REQUIRED',
			'INVALID_CREDENTIALS',
			'ITEM_LOCKED',
			'INVALID_ACCESS_TOKEN'
		]);
		return item.last_sync_status === 'error' && terminalCodes.has(item.last_error ?? '');
	}

	function formatNum(n: number | null): string {
		if (n === null || n === undefined) return '—';
		return n.toLocaleString('en-US', {
			minimumFractionDigits: 2,
			maximumFractionDigits: 2
		});
	}

	onMount(() => {
		window.addEventListener('message', handleOAuthReturn);
		loadItems();
	});
	onDestroy(() => {
		window.removeEventListener('message', handleOAuthReturn);
	});
</script>

<svelte:head>
	<title>Plaid Connections — Admin</title>
</svelte:head>

<div class="page">
	<header>
		<h1>Plaid Connections</h1>
		<p class="subtitle">
			10 institution slots; reversible via /item/remove. OAuth banks require the Cloudflare
			tunnel to be live before linking.
		</p>
	</header>

	{#if errorMsg}
		<div class="error">{errorMsg}</div>
	{/if}

	<section class="action-row">
		<button onclick={startAddConnection} class="primary">+ Add connection</button>
		<select bind:value={newLinkScope} aria-label="Connection scope">
			<option value="wealth">Wealth-only (balances + holdings)</option>
			<option value="register">Register (transactions — Chase/Amex spending only)</option>
		</select>
		<span class="slot-count">{items.length} of 10 slots used</span>
	</section>

	{#if wealthBanner}
		<!-- REQ-PC-B5 / spec non-negotiable #2: wealth-scope links skip the
		     register account-mapping step entirely — no Account rows, no
		     payment_method stamps. Confirm-only; map-accounts also 409s a
		     wealth-scope Item server-side as defense in depth.
		     P1-a1x: the success banner is conditional on the D1 account-map push
		     having landed — a failed push means this connection produces nothing. -->
		<section class="mapping-card" class:map-failed={!wealthBanner.ok}>
			<h2>{wealthBanner.title}</h2>
			<p class="hint">{wealthBanner.message}</p>
			{#if !wealthBanner.ok}
				<p class="map-counts">{wealthBanner.countsSummary}</p>
				{#if wealthBanner.conflictMasks.length > 0}
					<p class="map-counts">
						Conflicting account masks: {wealthBanner.conflictMasks.join(', ')}
					</p>
				{/if}
			{/if}
			<div class="mapping-actions">
				<button onclick={() => (pendingExchange = null)} class="primary">
					{wealthBanner.ok ? 'Done' : 'Dismiss'}
				</button>
			</div>
		</section>
	{:else if pendingExchange}
		<section class="mapping-card">
			<h2>Map accounts from {pendingInstitutionName}</h2>
			<p class="hint">
				Pick "Create new Account" for each one you want to track. "Skip" leaves it in the
				missing-accounts panel for later triage.
			</p>
			<ul class="mapping-list">
				{#each pendingExchange.accounts as acct (acct.account_id)}
					<li>
						<div class="acct-info">
							<strong>{acct.name ?? acct.official_name ?? '(unnamed)'}</strong>
							<span class="mask">…{acct.mask ?? '????'}</span>
							<span class="type">{acct.type}/{acct.subtype ?? '?'}</span>
							{#if acct.balances?.current !== undefined && acct.balances?.current !== null}
								<span class="balance">${formatNum(acct.balances.current)}</span>
							{/if}
						</div>
						<select bind:value={mappingChoices[acct.account_id]}>
							<option value="skip">Skip (triage later)</option>
							<option value="create_new">Create new Account</option>
						</select>
					</li>
				{/each}
			</ul>
			<div class="mapping-actions">
				<button onclick={confirmMappings} class="primary">Confirm</button>
				<button onclick={() => (pendingExchange = null)} class="ghost">Cancel</button>
			</div>
		</section>
	{/if}

	{#if loading}
		<p class="loading">Loading…</p>
	{:else if items.length === 0}
		<p class="empty">No connections yet. Click "Add connection" to link your first institution.</p>
	{:else}
		<section class="items">
			<h2>Connected institutions</h2>
			<ul class="item-list">
				{#each items as item (item.id)}
					<li class="item-row">
						<div class="item-meta">
							<strong>{item.institution_name}</strong>
							<span class="badge {statusBadgeClass(item)}">{statusLabel(item)}</span>
							{#if item.scope === 'wealth'}
								<span class="badge badge-neutral">wealth</span>
							{/if}
							<span class="accts">{item.mapped_account_count} mapped account{item.mapped_account_count === 1 ? '' : 's'}</span>
							{#if item.last_sync_at}
								<time>{new Date(item.last_sync_at).toLocaleString()}</time>
							{/if}
						</div>
						<div class="item-actions">
							{#if needsRelink(item)}
								<button onclick={() => relinkItem(item)} class="primary">Re-link</button>
							{/if}
							<button
								onclick={() => syncItem(item.id)}
								disabled={syncingItem === item.id}
								class="ghost"
							>
								{syncingItem === item.id ? 'Syncing…' : 'Sync now'}
							</button>
							<button onclick={() => disconnectItem(item)} class="danger">Disconnect</button>
						</div>
					</li>
				{/each}
			</ul>
		</section>
	{/if}

	{#if reconciliation.length > 0}
		<section class="recon">
			<h2>Plaid vs. computed reconciliation</h2>
			<p class="hint">
				Delta &gt; 2% OR &gt; $100 is flagged. Credit/loan balances are negated before comparison.
			</p>
			<table>
				<thead>
					<tr>
						<th>Account</th>
						<th>Type</th>
						<th>As of</th>
						<th class="num">Plaid</th>
						<th class="num">Computed</th>
						<th class="num">Δ</th>
						<th class="num">Δ %</th>
						<th></th>
					</tr>
				</thead>
				<tbody>
					{#each reconciliation as row (row.account_id)}
						<tr class:flagged={row.exceeds_threshold}>
							<td>{row.account_name ?? row.account_id.slice(0, 8)}</td>
							<td>{row.plaid_account_type}</td>
							<td>{row.snapshot_date}</td>
							<td class="num">${formatNum(row.plaid_total)}</td>
							<td class="num">{row.computed_total === null ? '—' : `$${formatNum(row.computed_total)}`}</td>
							<td class="num">{row.delta === null ? '—' : `$${formatNum(row.delta)}`}</td>
							<td class="num">{row.delta_pct === null ? '—' : `${formatNum(row.delta_pct)}%`}</td>
							<td>{row.exceeds_threshold ? '⚠️' : '✓'}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</section>
	{/if}
</div>

<style>
	.page {
		max-width: 1100px;
		margin: 0 auto;
		padding: 24px 16px 80px;
		font-family: -apple-system, system-ui, sans-serif;
	}
	h1 {
		font-size: 28px;
		font-weight: 600;
		margin: 0 0 4px;
	}
	h2 {
		font-size: 18px;
		font-weight: 600;
		margin: 24px 0 8px;
	}
	.subtitle {
		color: #666;
		margin: 0 0 24px;
	}
	.hint {
		color: #777;
		font-size: 14px;
		margin: 0 0 12px;
	}
	.error {
		background: #fee;
		border: 1px solid #fcc;
		padding: 12px;
		border-radius: 8px;
		color: #900;
		margin-bottom: 16px;
	}
	.action-row {
		display: flex;
		gap: 12px;
		align-items: center;
		margin-bottom: 24px;
	}
	.slot-count {
		color: #888;
		font-size: 13px;
	}
	button {
		font: inherit;
		padding: 8px 14px;
		border-radius: 6px;
		border: 1px solid #ddd;
		background: #fff;
		cursor: pointer;
		transition: background 0.15s, border-color 0.15s;
	}
	button:hover {
		background: #f5f5f5;
	}
	button.primary {
		background: #007aff;
		border-color: #007aff;
		color: #fff;
	}
	button.primary:hover {
		background: #0066cc;
	}
	button.danger {
		color: #c00;
		border-color: #fcc;
	}
	button.danger:hover {
		background: #fee;
	}
	button.ghost {
		background: transparent;
	}
	button:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
	.mapping-card {
		background: #f9f9fb;
		border: 1px solid #e5e5ea;
		border-radius: 10px;
		padding: 16px;
		margin-bottom: 24px;
	}
	.mapping-card.map-failed {
		background: #fee;
		border-color: #fcc;
	}
	.mapping-card.map-failed h2 {
		color: #900;
	}
	.map-counts {
		font-family: SF Mono, Menlo, monospace;
		font-size: 13px;
		color: #900;
		margin: 0 0 8px;
	}
	.mapping-list {
		list-style: none;
		padding: 0;
		margin: 0 0 16px;
	}
	.mapping-list li {
		display: flex;
		justify-content: space-between;
		align-items: center;
		padding: 10px 0;
		border-bottom: 1px solid #eee;
	}
	.mapping-list li:last-child {
		border-bottom: none;
	}
	.acct-info {
		display: flex;
		gap: 10px;
		align-items: baseline;
		flex-wrap: wrap;
	}
	.mask {
		color: #888;
		font-size: 13px;
		font-family: SF Mono, Menlo, monospace;
	}
	.type {
		color: #666;
		font-size: 12px;
		background: #eee;
		padding: 1px 6px;
		border-radius: 4px;
	}
	.balance {
		color: #333;
		font-variant-numeric: tabular-nums;
	}
	.mapping-actions {
		display: flex;
		gap: 8px;
	}
	.items {
		margin-top: 24px;
	}
	.item-list {
		list-style: none;
		padding: 0;
		margin: 0;
	}
	.item-row {
		display: flex;
		justify-content: space-between;
		align-items: center;
		padding: 12px;
		border: 1px solid #e5e5ea;
		border-radius: 8px;
		margin-bottom: 8px;
		background: #fff;
	}
	.item-meta {
		display: flex;
		gap: 12px;
		align-items: center;
		flex-wrap: wrap;
	}
	.item-meta strong {
		font-size: 15px;
	}
	.badge {
		font-size: 12px;
		padding: 2px 8px;
		border-radius: 12px;
	}
	.badge-ok {
		background: #e6f7ee;
		color: #0a7c3a;
	}
	.badge-warn {
		background: #fff5e0;
		color: #a06600;
	}
	.badge-error {
		background: #fee;
		color: #c00;
	}
	.badge-neutral {
		background: #eee;
		color: #666;
	}
	.accts {
		color: #888;
		font-size: 13px;
	}
	time {
		color: #999;
		font-size: 12px;
	}
	.item-actions {
		display: flex;
		gap: 8px;
	}
	.recon {
		margin-top: 32px;
	}
	table {
		width: 100%;
		border-collapse: collapse;
		font-size: 14px;
	}
	thead th {
		text-align: left;
		font-weight: 600;
		padding: 8px;
		border-bottom: 2px solid #ddd;
		color: #555;
	}
	tbody td {
		padding: 8px;
		border-bottom: 1px solid #eee;
	}
	th.num,
	td.num {
		text-align: right;
		font-variant-numeric: tabular-nums;
	}
	tr.flagged {
		background: #fff8e0;
	}
	.empty,
	.loading {
		color: #888;
		padding: 24px;
		text-align: center;
	}
</style>
