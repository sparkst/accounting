<script lang="ts">
	import { page } from '$app/stores';
	import { onMount } from 'svelte';
	import { fetchHealth, fetchInvoices } from '$lib/api';

	let reviewCount = $state(0);
	let overdueCount = $state(0);
	let darkMode = $state(false);

	onMount(async () => {
		// Restore dark mode preference
		const stored = localStorage.getItem('theme');
		if (stored === 'dark' || (!stored && window.matchMedia('(prefers-color-scheme: dark)').matches)) {
			darkMode = true;
			document.documentElement.classList.add('dark');
		}

		try {
			const health = await fetchHealth();
			reviewCount = health.needs_review_count ?? 0;
		} catch {
			// health endpoint not critical for nav rendering
		}
		try {
			const allInvoices = await fetchInvoices();
			const today = new Date().toISOString().slice(0, 10);
			overdueCount = allInvoices.items.filter(
				(inv) =>
					inv.status === 'overdue' ||
					(inv.status === 'sent' && inv.due_date && inv.due_date < today)
			).length;
		} catch {
			// invoice endpoint not critical for nav rendering
		}
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
</script>

<header class="nav-shell">
	<div class="nav-inner container">
		<a href="/" class="nav-brand" aria-label="Accounting home">
			<span class="brand-mark">&#x25A3;</span>
			<span class="brand-name">Accounting</span>
		</a>

		<nav aria-label="Main navigation">
			<ul class="nav-links">
				<li>
					<a href="/" class="nav-link" aria-current={isActive('/') ? 'page' : undefined}>
						Dashboard
					</a>
				</li>
				<li>
					<a href="/review" class="nav-link" aria-current={isActive('/review') ? 'page' : undefined}>
						Review
						{#if reviewCount > 0}
							<span class="nav-badge" aria-label="{reviewCount} items need review">
								{reviewCount > 99 ? '99+' : reviewCount}
							</span>
						{/if}
					</a>
				</li>
				<li>
					<a
						href="/register"
						class="nav-link"
						aria-current={isActive('/register') ? 'page' : undefined}
					>
						Register
					</a>
				</li>
				<li>
					<a
						href="/invoices"
						class="nav-link"
						aria-current={isActive('/invoices') ? 'page' : undefined}
					>
						Invoices
						{#if overdueCount > 0}
							<span
								class="nav-badge nav-badge-red"
								aria-label="{overdueCount} overdue invoices"
							>
								{overdueCount > 99 ? '99+' : overdueCount}
							</span>
						{/if}
					</a>
				</li>
				<li>
					<a
						href="/financials"
						class="nav-link"
						aria-current={isActive('/financials') ? 'page' : undefined}
					>
						Financials
					</a>
				</li>
				<li>
					<a
						href="/tax"
						class="nav-link"
						aria-current={isActive('/tax') ? 'page' : undefined}
					>
						Tax
					</a>
				</li>
				<li>
					<a
						href="/health"
						class="nav-link"
						aria-current={isActive('/health') ? 'page' : undefined}
					>
						Health
					</a>
				</li>
				<li>
					<a
						href="/accounts"
						class="nav-link"
						aria-current={isActive('/accounts') ? 'page' : undefined}
					>
						Rules
					</a>
				</li>
				<li>
					<a
						href="/reconciliation"
						class="nav-link"
						aria-current={isActive('/reconciliation') ? 'page' : undefined}
					>
						Reconciliation
					</a>
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
