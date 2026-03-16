<script lang="ts">
	import '../app.css';
	import Nav from '$lib/components/Nav.svelte';
	import favicon from '$lib/assets/favicon.svg';

	let { children } = $props();
</script>

<svelte:head>
	<title>Accounting</title>
	<link rel="icon" href={favicon} />
</svelte:head>

<Nav />
<main class="main-content">
	{@render children()}
</main>

<style>
	.main-content {
		min-height: calc(100vh - 52px);
	}
</style>
