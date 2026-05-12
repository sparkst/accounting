<script lang="ts">
	import { onMount } from 'svelte';
	import {
		fetchBrokerageAccounts,
		updateBrokerageAccountTags,
		type BrokerageAccount
	} from '$lib/api';
	import {
		brokerColor,
		brokerDisplayName,
		fmtCurrency,
		fmtCurrencyNoCents,
		applySort,
		matchesQuery,
		nextSortDir,
		sortIndicator,
		toggleSetMember,
		type SortDir
	} from '$lib/brokerage';

	// ── State ─────────────────────────────────────────────────────────────
	let accounts = $state<BrokerageAccount[] | null>(null);
	let loading = $state(true);
	// Initial-load error blanks the page; refetch errors keep prior data
	// visible and surface as inline banner (consistent with /holdings,
	// /transactions, and the main /brokerage summary page).
	let initialError = $state('');
	let refetchError = $state('');
	function setError(msg: string): void {
		if (accounts === null) initialError = msg;
		else refetchError = msg;
	}
	// Per-row tag-save error so the user sees feedback near the row, not
	// only via the global error banner that may be off-screen.
	let tagSaveError = $state<{ accountId: string; message: string } | null>(null);

	let acctQuery = $state('');
	let acctSortKey = $state<string | null>('market_value');
	let acctSortDir = $state<SortDir>('desc');
	let acctBrokerFilter = $state<Set<string>>(new Set());
	let acctTagInclude = $state<Set<string>>(new Set());
	let acctTagExclude = $state<Set<string>>(new Set());
	let editingTagsAccountId = $state<string | null>(null);
	let tagDraftInput = $state('');

	// ── Loading ───────────────────────────────────────────────────────────
	async function load(): Promise<void> {
		loading = true;
		try {
			accounts = await fetchBrokerageAccounts();
			initialError = '';
			refetchError = '';
		} catch (e) {
			setError(e instanceof Error ? e.message : String(e));
		} finally {
			loading = false;
		}
	}

	onMount(load);

	function toggleSort(
		key: string,
		currentKey: string | null,
		currentDir: SortDir
	): { key: string | null; dir: SortDir } {
		if (currentKey !== key) return { key, dir: 'asc' };
		const next = nextSortDir(currentDir);
		return { key: next ? key : null, dir: next };
	}

	function cycleTag(tag: string): void {
		if (acctTagInclude.has(tag)) {
			const next = new Set(acctTagInclude);
			next.delete(tag);
			acctTagInclude = next;
			const ex = new Set(acctTagExclude);
			ex.add(tag);
			acctTagExclude = ex;
		} else if (acctTagExclude.has(tag)) {
			const next = new Set(acctTagExclude);
			next.delete(tag);
			acctTagExclude = next;
		} else {
			const inc = new Set(acctTagInclude);
			inc.add(tag);
			acctTagInclude = inc;
		}
	}

	function tagState(tag: string): 'include' | 'exclude' | 'neutral' {
		if (acctTagInclude.has(tag)) return 'include';
		if (acctTagExclude.has(tag)) return 'exclude';
		return 'neutral';
	}

	// ── Derived ───────────────────────────────────────────────────────────
	let withSnapshots = $derived(
		(accounts ?? []).filter((a) => a.as_of !== null && !a.is_plan_wrapper)
	);

	let acctBrokerOptions = $derived(
		Array.from(new Set(withSnapshots.map((a) => a.broker))).sort()
	);

	let allTags = $derived.by(() => {
		const t = new Set<string>();
		for (const a of withSnapshots) for (const tag of a.tags ?? []) t.add(tag);
		return Array.from(t).sort();
	});

	let filteredAccounts = $derived.by(() => {
		const filtered = withSnapshots.filter((a) => {
			if (acctBrokerFilter.size > 0 && !acctBrokerFilter.has(a.broker)) return false;
			const tags = new Set(a.tags ?? []);
			if (acctTagInclude.size > 0) {
				for (const need of acctTagInclude) {
					if (!tags.has(need)) return false;
				}
			}
			if (acctTagExclude.size > 0) {
				for (const block of acctTagExclude) {
					if (tags.has(block)) return false;
				}
			}
			return matchesQuery(
				[a.broker, a.account_number_masked, a.account_name, a.account_type, a.entity],
				acctQuery
			);
		});
		return applySort(filtered, acctSortKey, acctSortDir);
	});

	let visibleTotal = $derived(
		filteredAccounts.reduce((sum, a) => sum + (a.market_value ?? 0), 0)
	);
</script>

<svelte:head>
	<title>All Accounts · Wealth</title>
	<link rel="preconnect" href="https://fonts.googleapis.com" />
	<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin="anonymous" />
	<link
		href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap"
		rel="stylesheet"
	/>
</svelte:head>

<div class="page brokerage">
	<nav class="crumbs" aria-label="Breadcrumb">
		<a href="/">Money</a>
		<span class="sep">/</span>
		<a href="/wealth">Wealth</a>
		<span class="sep">/</span>
		<span>Accounts</span>
	</nav>

	<h1 class="sr-only">All accounts</h1>

	{#if loading && !accounts}
		<div class="state">Loading…</div>
	{:else if initialError}
		<div class="state error">⚠ {initialError}</div>
	{:else}
		<section class="section first">
			<div class="sec-head">
				<h2 class="sec-title">All accounts · {filteredAccounts.length} of {withSnapshots.length}</h2>
				<div class="table-controls">
					<input
						type="search"
						class="search-input"
						placeholder="Search accounts…"
						bind:value={acctQuery}
					/>
				</div>
			</div>

			<div class="hero-meta brokerage-hero-meta-spacer">
				Visible total: <b>{fmtCurrencyNoCents(visibleTotal)}</b>
			</div>

			{#if allTags.length > 0}
				<div class="filter-chips">
					<span class="chip-label">Tags:</span>
					{#each allTags as tag (tag)}
						{@const state = tagState(tag)}
						<button
							type="button"
							class="chip tag-chip"
							class:include={state === 'include'}
							class:exclude={state === 'exclude'}
							onclick={() => cycleTag(tag)}
							aria-label="{tag}: {state === 'include'
								? 'including'
								: state === 'exclude'
									? 'excluding'
									: 'not filtered'} (click to cycle)"
							title="Click to cycle: include → exclude → off"
						>
							{state === 'exclude' ? '−' : state === 'include' ? '+' : ''} {tag}
						</button>
					{/each}
					{#if acctTagInclude.size + acctTagExclude.size > 0}
						<button
							type="button"
							class="chip clear"
							onclick={() => {
								acctTagInclude = new Set();
								acctTagExclude = new Set();
							}}
						>
							Clear tags
						</button>
					{/if}
				</div>
			{/if}

			{#if acctBrokerOptions.length > 1}
				<div class="filter-chips">
					<span class="chip-label">Broker:</span>
					{#each acctBrokerOptions as broker (broker)}
						<button
							type="button"
							class="chip"
							class:active={acctBrokerFilter.has(broker)}
							onclick={() => (acctBrokerFilter = toggleSetMember(acctBrokerFilter, broker))}
						>
							{brokerDisplayName(broker)}
						</button>
					{/each}
					{#if acctBrokerFilter.size > 0}
						<button
							type="button"
							class="chip clear"
							onclick={() => (acctBrokerFilter = new Set())}
						>
							Clear
						</button>
					{/if}
				</div>
			{/if}

			<table class="data-table">
				<thead>
					<tr>
						{#each [{ key: 'broker', label: 'Broker' }, { key: 'account_number_masked', label: 'Account' }, { key: 'account_type', label: 'Type' }] as col (col.key)}
							<th
								class="sortable"
								onclick={() => {
									const r = toggleSort(col.key, acctSortKey, acctSortDir);
									acctSortKey = r.key;
									acctSortDir = r.dir;
								}}>{col.label}{sortIndicator(col.key, acctSortKey, acctSortDir)}</th
							>
						{/each}
						<th>Tags</th>
						{#each [{ key: 'entity', label: 'Entity' }, { key: 'as_of', label: 'As of' }] as col (col.key)}
							<th
								class="sortable"
								onclick={() => {
									const r = toggleSort(col.key, acctSortKey, acctSortDir);
									acctSortKey = r.key;
									acctSortDir = r.dir;
								}}>{col.label}{sortIndicator(col.key, acctSortKey, acctSortDir)}</th
							>
						{/each}
						<th
							class="num sortable"
							onclick={() => {
								const r = toggleSort('market_value', acctSortKey, acctSortDir);
								acctSortKey = r.key;
								acctSortDir = r.dir;
							}}>Market value{sortIndicator('market_value', acctSortKey, acctSortDir)}</th
						>
					</tr>
				</thead>
				<tbody>
					{#each filteredAccounts as a (a.account_id)}
						<tr class:wrapper={a.is_plan_wrapper}>
							<td>
								<span class="broker-cell">
									<span class="broker-dot" style:background={brokerColor(a.broker)}></span>
									{brokerDisplayName(a.broker)}
								</span>
							</td>
							<td>
								<a
									class="account-link"
									href={`/wealth/accounts/${a.account_id}`}
									title={a.account_name
										? `${a.account_name} (${a.account_number_masked})`
										: 'Open account detail'}
								>
									{#if a.account_name}
										{a.account_name}
									{:else}
										{a.account_number_masked}
									{/if}
								</a>
							</td>
							<td>{a.account_type}</td>
							<td class="tags-cell">
								{#if editingTagsAccountId === a.account_id}
									<div class="tags-edit">
										{#each a.tags ?? [] as tag (tag)}
											<span class="tag-pill editing">
												{tag}
												<button
													type="button"
													class="tag-remove"
													aria-label={`Remove ${tag}`}
													onclick={async () => {
														const next = (a.tags ?? []).filter((t) => t !== tag);
														try {
															await updateBrokerageAccountTags(a.account_id, next);
															a.tags = next;
															tagSaveError = null;
														} catch (e) {
															tagSaveError = {
																accountId: a.account_id,
																message: e instanceof Error ? e.message : String(e)
															};
														}
													}}>×</button
												>
											</span>
										{/each}
										<input
											type="text"
											class="tag-input"
											bind:value={tagDraftInput}
											placeholder="add tag…"
											onkeydown={async (e) => {
												if (e.key !== 'Enter') return;
												const draft = tagDraftInput.trim().toLowerCase();
												if (!draft) return;
												if ((a.tags ?? []).includes(draft)) return;
												const next = [...(a.tags ?? []), draft].sort();
												try {
													await updateBrokerageAccountTags(a.account_id, next);
													a.tags = next;
													tagDraftInput = '';
													tagSaveError = null;
												} catch (err) {
													tagSaveError = {
														accountId: a.account_id,
														message: err instanceof Error ? err.message : String(err)
													};
												}
											}}
										/>
										<button
											type="button"
											class="tag-edit-done"
											onclick={() => {
												editingTagsAccountId = null;
												tagDraftInput = '';
												tagSaveError = null;
											}}>Done</button
										>
										{#if tagSaveError && tagSaveError.accountId === a.account_id}
											<span class="tag-save-error" role="alert">
												⚠ Save failed: {tagSaveError.message}
											</span>
										{/if}
									</div>
								{:else}
									<button
										type="button"
										class="tags-display"
										onclick={() => {
											editingTagsAccountId = a.account_id;
											tagDraftInput = '';
										}}
										title="Click to edit tags"
									>
										{#each a.tags ?? [] as tag (tag)}
											<span class="tag-pill">{tag}</span>
										{/each}
										{#if (a.tags ?? []).length === 0}
											<span class="muted">+ add</span>
										{/if}
									</button>
								{/if}
							</td>
							<td>{a.entity}</td>
							<td>{a.as_of}</td>
							<td class="num">{fmtCurrency(a.market_value)}</td>
						</tr>
					{/each}
					{#if filteredAccounts.length === 0}
						<tr><td colspan="7" class="muted empty">No accounts match the current filter.</td></tr>
					{/if}
				</tbody>
			</table>
		</section>
	{/if}
</div>
