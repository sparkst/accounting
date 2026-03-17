<script lang="ts">
	import { page } from '$app/stores';
	import { onMount } from 'svelte';
	import { fetchHealth } from '$lib/api';

	let reviewCount = $state(0);

	onMount(async () => {
		try {
			const health = await fetchHealth();
			reviewCount = health.needs_review_count ?? 0;
		} catch {
			// health endpoint not critical for nav rendering
		}
	});

	function isActive(href: string): boolean {
		const path = $page.url.pathname;
		if (href === '/') return path === '/';
		return path.startsWith(href);
	}
</script>

<header class="nav-shell">
	<div class="nav-inner container">
		<a href="/" class="nav-brand" aria-label="Accounting — home">
			<span class="brand-mark">▣</span>
			<span class="brand-name">Accounting</span>
		</a>

		<nav aria-label="Main navigation">
			<ul class="nav-links">
				<li>
					<a
						href="/"
						class="nav-link"
						aria-current={isActive('/') ? 'page' : undefined}
					>
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
						href="/health"
						class="nav-link"
						aria-current={isActive('/health') ? 'page' : undefined}
					>
						Health
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
						href="/accounts"
						class="nav-link"
						aria-current={isActive('/accounts') ? 'page' : undefined}
					>
						Accounts
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
	</div>
</header>

<style>
	.nav-shell {
		position: sticky;
		top: 0;
		z-index: 100;
		background: rgba(249, 250, 251, 0.88);
		-webkit-backdrop-filter: saturate(180%) blur(12px);
		backdrop-filter: saturate(180%) blur(12px);
		border-bottom: 1px solid var(--border);
	}

	.nav-inner {
		display: flex;
		align-items: center;
		gap: 32px;
		height: 52px;
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
		font-size: .9rem;
		font-weight: 600;
		letter-spacing: -.2px;
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
		font-size: .875rem;
		font-weight: 500;
		color: var(--text-muted);
		transition: color .12s, background .12s;
	}

	.nav-link:hover {
		color: var(--text);
		background: var(--gray-100);
	}

	.nav-link[aria-current="page"] {
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
		font-size: .65rem;
		font-weight: 700;
		line-height: 1;
	}
</style>
