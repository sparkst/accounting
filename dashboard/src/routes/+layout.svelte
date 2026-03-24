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

<a href="#main" class="sr-only" style="position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;">Skip to content</a>
<Nav />
<main id="main" class="main-content">
	{@render children()}
</main>

<style>
	.main-content {
		min-height: calc(100vh - 52px);
	}
</style>
