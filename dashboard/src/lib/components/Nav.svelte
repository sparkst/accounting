<script lang="ts">
	import { page } from '$app/stores';
	import { onMount } from 'svelte';
	import { fetchHealth, fetchInvoices } from '$lib/api';

	let reviewCount = $state(0);
	let overdueCount = $state(0);
	let darkMode = $state(false);

	// Which dropdown group is open: 'transactions' | 'money' | 'system' | null
	let openGroup = $state<string | null>(null);

	onMount(() => {
		// Restore dark mode preference
		const stored = localStorage.getItem('theme');
		if (stored === 'dark' || (!stored && window.matchMedia('(prefers-color-scheme: dark)').matches)) {
			darkMode = true;
			document.documentElement.classList.add('dark');
		}

		// Fetch badge counts (fire-and-forget; errors are non-fatal)
		fetchHealth()
			.then((health) => {
				reviewCount = health.needs_review_count ?? 0;
			})
			.catch(() => {
				// health endpoint not critical for nav rendering
			});

		fetchInvoices()
			.then((allInvoices) => {
				const today = new Date().toISOString().slice(0, 10);
				overdueCount = allInvoices.items.filter(
					(inv) =>
						inv.status === 'overdue' ||
						(inv.status === 'sent' && inv.due_date && inv.due_date < today)
				).length;
			})
			.catch(() => {
				// invoice endpoint not critical for nav rendering
			});

		// Close dropdown when clicking outside nav
		function handleClickOutside(e: MouseEvent) {
			const nav = document.querySelector('.nav-shell');
			if (nav && !nav.contains(e.target as Node)) {
				openGroup = null;
			}
		}
		document.addEventListener('click', handleClickOutside);
		return () => document.removeEventListener('click', handleClickOutside);
	});

	function toggleDark() {
		darkMode = !darkMode;
		document.documentElement.classList.toggle('dark', darkMode);
		localStorage.setItem('theme', darkMode ? 'dark' : 'light');
	}

	function isActive(href: string): boolean {
		const path = $page.url.pathname;
		if (href === '/') return path === '/';
		return path.startsWith(href);
	}

	function isGroupActive(hrefs: string[]): boolean {
		return hrefs.some((href) => isActive(href));
	}

	function toggleGroup(group: string) {
		openGroup = openGroup === group ? null : group;
	}

	function closeGroup() {
		openGroup = null;
	}

	// Keyboard handler for dropdown triggers
	function handleTriggerKeydown(e: KeyboardEvent, group: string) {
		if (e.key === 'Enter' || e.key === ' ') {
			e.preventDefault();
			toggleGroup(group);
		} else if (e.key === 'ArrowDown') {
			e.preventDefault();
			openGroup = group;
			// Focus first item after paint
			requestAnimationFrame(() => {
				const menu = document.querySelector<HTMLElement>(`[data-menu="${group}"]`);
				const first = menu?.querySelector<HTMLElement>('[role="menuitem"]');
				first?.focus();
			});
		} else if (e.key === 'Escape') {
			closeGroup();
		}
	}

	// Keyboard handler for menu items
	function handleMenuKeydown(e: KeyboardEvent, group: string) {
		const menu = document.querySelector<HTMLElement>(`[data-menu="${group}"]`);
		if (!menu) return;
		const items = Array.from(menu.querySelectorAll<HTMLElement>('[role="menuitem"]'));
		const idx = items.indexOf(e.target as HTMLElement);

		if (e.key === 'ArrowDown') {
			e.preventDefault();
			items[(idx + 1) % items.length]?.focus();
		} else if (e.key === 'ArrowUp') {
			e.preventDefault();
			items[(idx - 1 + items.length) % items.length]?.focus();
		} else if (e.key === 'Escape') {
			e.preventDefault();
			closeGroup();
			// Return focus to trigger
			document.querySelector<HTMLElement>(`[data-trigger="${group}"]`)?.focus();
		} else if (e.key === 'Tab') {
			closeGroup();
		}
	}
</script>

<header class="nav-shell">
	<div class="nav-inner container">
		<a href="/" class="nav-brand" aria-label="Accounting home">
			<span class="brand-mark">&#x25A3;</span>
			<span class="brand-name">Accounting</span>
		</a>

		<nav aria-label="Main navigation">
			<ul class="nav-links">
				<!-- Dashboard (top-level) -->
				<li>
					<a href="/" class="nav-link" aria-current={isActive('/') ? 'page' : undefined}>
						Dashboard
					</a>
				</li>

				<!-- Transactions dropdown: Register, Review -->
				<li class="nav-group">
					<button
						class="nav-link nav-group-trigger"
						class:nav-group-active={isGroupActive(['/register', '/review'])}
						aria-haspopup="menu"
						aria-expanded={openGroup === 'transactions'}
						data-trigger="transactions"
						onclick={() => toggleGroup('transactions')}
						onkeydown={(e) => handleTriggerKeydown(e, 'transactions')}
					>
						Transactions
						{#if reviewCount > 0}
							<span class="nav-badge" aria-label="{reviewCount} items need review">
								{reviewCount > 99 ? '99+' : reviewCount}
							</span>
						{/if}
						<span class="nav-chevron" aria-hidden="true">
							<svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
								<polyline points="2,3 5,7 8,3"/>
							</svg>
						</span>
					</button>
					{#if openGroup === 'transactions'}
						<ul class="nav-dropdown" role="menu" data-menu="transactions" aria-label="Transactions">
							<li role="none">
								<a
									href="/register"
									class="nav-dropdown-item"
									role="menuitem"
									aria-current={isActive('/register') ? 'page' : undefined}
									onclick={closeGroup}
									onkeydown={(e) => handleMenuKeydown(e, 'transactions')}
								>
									Register
								</a>
							</li>
							<li role="none">
								<a
									href="/review"
									class="nav-dropdown-item"
									role="menuitem"
									aria-current={isActive('/review') ? 'page' : undefined}
									onclick={closeGroup}
									onkeydown={(e) => handleMenuKeydown(e, 'transactions')}
								>
									Review
									{#if reviewCount > 0}
										<span class="nav-badge" aria-hidden="true">
											{reviewCount > 99 ? '99+' : reviewCount}
										</span>
									{/if}
								</a>
							</li>
						</ul>
					{/if}
				</li>

				<!-- Money dropdown: Invoices, Financials, Cash Flow, Tax -->
				<li class="nav-group">
					<button
						class="nav-link nav-group-trigger"
						class:nav-group-active={isGroupActive(['/invoices', '/financials', '/cashflow', '/tax'])}
						aria-haspopup="menu"
						aria-expanded={openGroup === 'money'}
						data-trigger="money"
						onclick={() => toggleGroup('money')}
						onkeydown={(e) => handleTriggerKeydown(e, 'money')}
					>
						Money
						{#if overdueCount > 0}
							<span class="nav-badge nav-badge-red" aria-label="{overdueCount} overdue invoices">
								{overdueCount > 99 ? '99+' : overdueCount}
							</span>
						{/if}
						<span class="nav-chevron" aria-hidden="true">
							<svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
								<polyline points="2,3 5,7 8,3"/>
							</svg>
						</span>
					</button>
					{#if openGroup === 'money'}
						<ul class="nav-dropdown" role="menu" data-menu="money" aria-label="Money">
							<li role="none">
								<a
									href="/invoices"
									class="nav-dropdown-item"
									role="menuitem"
									aria-current={isActive('/invoices') ? 'page' : undefined}
									onclick={closeGroup}
									onkeydown={(e) => handleMenuKeydown(e, 'money')}
								>
									Invoices
									{#if overdueCount > 0}
										<span class="nav-badge nav-badge-red" aria-hidden="true">
											{overdueCount > 99 ? '99+' : overdueCount}
										</span>
									{/if}
								</a>
							</li>
							<li role="none">
								<a
									href="/financials"
									class="nav-dropdown-item"
									role="menuitem"
									aria-current={isActive('/financials') ? 'page' : undefined}
									onclick={closeGroup}
									onkeydown={(e) => handleMenuKeydown(e, 'money')}
								>
									Financials
								</a>
							</li>
							<li role="none">
								<a
									href="/cashflow"
									class="nav-dropdown-item"
									role="menuitem"
									aria-current={isActive('/cashflow') ? 'page' : undefined}
									onclick={closeGroup}
									onkeydown={(e) => handleMenuKeydown(e, 'money')}
								>
									Cash Flow
								</a>
							</li>
							<li role="none">
								<a
									href="/tax"
									class="nav-dropdown-item"
									role="menuitem"
									aria-current={isActive('/tax') ? 'page' : undefined}
									onclick={closeGroup}
									onkeydown={(e) => handleMenuKeydown(e, 'money')}
								>
									Tax
								</a>
							</li>
						</ul>
					{/if}
				</li>

				<!-- System dropdown: Health, Rules, Reconciliation -->
				<li class="nav-group">
					<button
						class="nav-link nav-group-trigger"
						class:nav-group-active={isGroupActive(['/health', '/accounts', '/reconciliation'])}
						aria-haspopup="menu"
						aria-expanded={openGroup === 'system'}
						data-trigger="system"
						onclick={() => toggleGroup('system')}
						onkeydown={(e) => handleTriggerKeydown(e, 'system')}
					>
						System
						<span class="nav-chevron" aria-hidden="true">
							<svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
								<polyline points="2,3 5,7 8,3"/>
							</svg>
						</span>
					</button>
					{#if openGroup === 'system'}
						<ul class="nav-dropdown" role="menu" data-menu="system" aria-label="System">
							<li role="none">
								<a
									href="/health"
									class="nav-dropdown-item"
									role="menuitem"
									aria-current={isActive('/health') ? 'page' : undefined}
									onclick={closeGroup}
									onkeydown={(e) => handleMenuKeydown(e, 'system')}
								>
									Health
								</a>
							</li>
							<li role="none">
								<a
									href="/accounts"
									class="nav-dropdown-item"
									role="menuitem"
									aria-current={isActive('/accounts') ? 'page' : undefined}
									onclick={closeGroup}
									onkeydown={(e) => handleMenuKeydown(e, 'system')}
								>
									Rules
								</a>
							</li>
							<li role="none">
								<a
									href="/reconciliation"
									class="nav-dropdown-item"
									role="menuitem"
									aria-current={isActive('/reconciliation') ? 'page' : undefined}
									onclick={closeGroup}
									onkeydown={(e) => handleMenuKeydown(e, 'system')}
								>
									Reconciliation
								</a>
							</li>
						</ul>
					{/if}
				</li>
			</ul>
		</nav>

		<button
			class="dark-toggle"
			onclick={toggleDark}
			aria-label={darkMode ? 'Switch to light mode' : 'Switch to dark mode'}
			title={darkMode ? 'Light mode' : 'Dark mode'}
		>
			{#if darkMode}
				<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>
			{:else}
				<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
			{/if}
		</button>
	</div>
</header>

<style>
	.nav-shell {
		position: sticky;
		top: 0;
		z-index: 100;
		background: var(--surface);
		border-bottom: 1px solid var(--border);
	}

	.nav-inner {
		display: flex;
		align-items: center;
		gap: 32px;
		height: var(--nav-height);
	}

	.nav-brand {
		display: flex;
		align-items: center;
		gap: 8px;
		text-decoration: none;
		color: var(--text);
		flex-shrink: 0;
	}

	.brand-mark {
		font-size: 1.1rem;
		color: var(--gray-600);
	}

	.brand-name {
		font-size: 0.9rem;
		font-weight: 600;
		letter-spacing: -0.2px;
	}

	nav {
		flex: 1;
	}

	.nav-links {
		display: flex;
		list-style: none;
		gap: 2px;
	}

	.nav-link {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		padding: 5px 12px;
		border-radius: var(--radius-sm);
		text-decoration: none;
		font-size: 0.875rem;
		font-weight: 500;
		color: var(--text-muted);
		transition:
			color 0.12s,
			background 0.12s;
	}

	.nav-link:hover {
		color: var(--text);
		background: var(--gray-100);
	}

	.nav-link[aria-current='page'] {
		color: var(--text);
		background: var(--gray-200);
	}

	/* Dropdown trigger button resets */
	.nav-group-trigger {
		border: none;
		background: transparent;
		cursor: pointer;
		font-family: inherit;
	}

	.nav-group-trigger[aria-expanded='true'] {
		color: var(--text);
		background: var(--gray-100);
	}

	.nav-group-active {
		color: var(--text) !important;
		background: var(--gray-200) !important;
	}

	/* Chevron rotates when open */
	.nav-chevron {
		display: inline-flex;
		align-items: center;
		opacity: 0.5;
		transition: transform 0.15s;
	}

	.nav-group-trigger[aria-expanded='true'] .nav-chevron {
		transform: rotate(180deg);
	}

	/* Dropdown group positioning */
	.nav-group {
		position: relative;
	}

	.nav-dropdown {
		position: absolute;
		top: calc(100% + 6px);
		left: 0;
		min-width: 160px;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		box-shadow: var(--shadow);
		list-style: none;
		padding: 4px;
		z-index: 200;
	}

	.nav-dropdown-item {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 7px 12px;
		border-radius: var(--radius-sm);
		text-decoration: none;
		font-size: 0.875rem;
		font-weight: 500;
		color: var(--text-muted);
		transition:
			color 0.12s,
			background 0.12s;
		width: 100%;
	}

	.nav-dropdown-item:hover,
	.nav-dropdown-item:focus {
		color: var(--text);
		background: var(--gray-100);
		outline: none;
	}

	.nav-dropdown-item[aria-current='page'] {
		color: var(--text);
		background: var(--gray-200);
	}

	.nav-badge {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-width: 18px;
		height: 18px;
		padding: 0 5px;
		background: var(--amber-500);
		color: #fff;
		border-radius: 999px;
		font-size: 0.65rem;
		font-weight: 700;
		line-height: 1;
	}

	.nav-badge-red {
		background: var(--red-500);
	}

	.dark-toggle {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 32px;
		height: 32px;
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		background: transparent;
		color: var(--text-muted);
		cursor: pointer;
		flex-shrink: 0;
		transition: color .12s, background .12s;
	}

	.dark-toggle:hover {
		color: var(--text);
		background: var(--gray-100);
	}
</style>
