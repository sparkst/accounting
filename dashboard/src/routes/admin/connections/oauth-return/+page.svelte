<script lang="ts">
	import { onMount } from 'svelte';

	// OAuth-return is where Plaid redirects after an OAuth bank's auth flow.
	// Per Plaid's docs, the Link handler running in the original window
	// (opener) re-initializes with the original link_token plus
	// `receivedRedirectUri = window.location.href` to finish the flow.
	//
	// This page lives at https://books.sparkry.ai/admin/connections/oauth-return
	// (served via Cloudflare Tunnel behind Cloudflare Access).

	let message = $state('Completing Plaid authorization…');

	onMount(() => {
		// Tell the opener window (the connections page) we returned, so it can
		// finalize Link. If opened via target=_blank, opener exists; if Plaid
		// redirected the SAME window, we just navigate back to the connections page.
		try {
			if (window.opener && !window.opener.closed) {
				window.opener.postMessage(
					{ type: 'plaid_oauth_return', url: window.location.href },
					window.location.origin
				);
				message = 'Authorization complete. You can close this tab.';
			} else {
				message = 'Authorization complete. Redirecting…';
				setTimeout(() => {
					window.location.href = '/admin/connections';
				}, 1200);
			}
		} catch (e) {
			message = `Authorization complete. ${e instanceof Error ? e.message : ''}`;
		}
	});
</script>

<svelte:head>
	<title>Plaid OAuth Return</title>
</svelte:head>

<div class="wrap">
	<h1>Plaid</h1>
	<p>{message}</p>
</div>

<style>
	.wrap {
		font-family: -apple-system, system-ui, sans-serif;
		max-width: 480px;
		margin: 120px auto;
		text-align: center;
		color: #333;
	}
	h1 {
		font-size: 22px;
		margin-bottom: 12px;
	}
</style>
