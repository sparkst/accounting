<script lang="ts">
	import { onMount } from 'svelte';
	import {
		fetchBrokeragePolicy,
		fetchBrokerageBoldBets,
		type BrokeragePolicy,
		type BrokerageBoldBets
	} from '$lib/api';
	import PolicyPanel from '$lib/components/PolicyPanel.svelte';
	import BoldBetsCard from '$lib/components/BoldBetsCard.svelte';

	let policy = $state<BrokeragePolicy | null>(null);
	let boldBets = $state<BrokerageBoldBets | null>(null);
	let loading = $state(true);
	let error = $state('');

	async function load(): Promise<void> {
		loading = true;
		error = '';
		const [p, b] = await Promise.allSettled([fetchBrokeragePolicy(), fetchBrokerageBoldBets()]);
		if (p.status === 'fulfilled') policy = p.value;
		else error = 'Failed to load policy';
		if (b.status === 'fulfilled') boldBets = b.value;
		loading = false;
	}

	onMount(load);
</script>

<svelte:head><title>Investment Policy — Wealth</title></svelte:head>

<main>
	<h1>Investment Policy</h1>
	{#if error}<p class="error">{error}</p>{/if}
	{#if loading}
		<p>Loading…</p>
	{:else}
		{#if policy}<PolicyPanel {policy} />{/if}
		{#if boldBets}<BoldBetsCard {boldBets} />{/if}
	{/if}
</main>

<style>
	main {
		max-width: 900px;
		margin: 0 auto;
		padding: 1rem;
	}
	h1 {
		font-size: 1.3rem;
	}
	.error {
		color: #c5221f;
	}
</style>
