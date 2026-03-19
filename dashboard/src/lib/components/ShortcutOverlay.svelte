<script lang="ts">
	interface Props {
		onclose: () => void;
	}

	let { onclose }: Props = $props();

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === '?' || e.key === 'Escape') {
			e.preventDefault();
			onclose();
		}
	}

	const shortcuts = [
		{ section: 'Navigation', items: [
			{ key: 'j / ↓', desc: 'Next item' },
			{ key: 'k / ↑', desc: 'Previous item' },
		]},
		{ section: 'Actions', items: [
			{ key: 'y', desc: 'Confirm focused card' },
			{ key: 'e', desc: 'Edit (focus first field)' },
			{ key: 's', desc: 'Toggle split panel' },
			{ key: 'd', desc: 'Mark as duplicate (reject)' },
			{ key: 'r', desc: 'Reject with undo' },
			{ key: 'n', desc: 'Focus notes textarea' },
			{ key: 'c', desc: 'Focus category dropdown' },
		]},
		{ section: 'Entity Quick-Select', items: [
			{ key: '1', desc: 'Set entity to Sparkry AI LLC' },
			{ key: '2', desc: 'Set entity to BlackLine MTB LLC' },
			{ key: '3', desc: 'Set entity to Personal' },
		]},
		{ section: 'Batch', items: [
			{ key: 'x', desc: 'Toggle select on focused card' },
			{ key: 'Shift+click', desc: 'Range select' },
		]},
		{ section: 'Other', items: [
			{ key: '?', desc: 'Toggle this overlay' },
			{ key: 'Esc', desc: 'Close overlay / cancel' },
		]},
	];
</script>

<svelte:window onkeydown={handleKeydown} />

<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="overlay-backdrop" onclick={onclose}>
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div class="overlay-modal" onclick={(e) => e.stopPropagation()}>
		<h2 class="overlay-title">Keyboard Shortcuts</h2>
		{#each shortcuts as group}
			<div class="shortcut-section">
				<h3 class="section-label">{group.section}</h3>
				<dl class="shortcut-list">
					{#each group.items as item}
						<div class="shortcut-row">
							<dt class="shortcut-key"><kbd>{item.key}</kbd></dt>
							<dd class="shortcut-desc">{item.desc}</dd>
						</div>
					{/each}
				</dl>
			</div>
		{/each}
		<p class="overlay-hint">Press <kbd>?</kbd> or <kbd>Esc</kbd> to close</p>
	</div>
</div>

<style>
	.overlay-backdrop {
		position: fixed;
		inset: 0;
		background: rgba(0,0,0,.4);
		z-index: 9000;
		display: flex;
		align-items: center;
		justify-content: center;
		animation: fade-in .15s ease-out;
	}

	@keyframes fade-in {
		from { opacity: 0; }
		to   { opacity: 1; }
	}

	.overlay-modal {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: var(--radius-lg);
		box-shadow: var(--shadow);
		padding: 28px 32px;
		max-width: 480px;
		width: 90vw;
		max-height: 85vh;
		overflow-y: auto;
	}

	.overlay-title {
		font-size: 1.1rem;
		font-weight: 700;
		margin-bottom: 20px;
	}

	.shortcut-section {
		margin-bottom: 16px;
	}

	.section-label {
		font-size: .7rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: .06em;
		color: var(--text-muted);
		margin-bottom: 6px;
	}

	.shortcut-list {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.shortcut-row {
		display: flex;
		align-items: center;
		gap: 12px;
		padding: 3px 0;
	}

	.shortcut-key {
		min-width: 100px;
		text-align: right;
	}

	.shortcut-key kbd {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-width: 22px;
		height: 22px;
		padding: 0 6px;
		background: var(--gray-100);
		border: 1px solid var(--gray-300);
		border-radius: 4px;
		font-family: var(--font-mono);
		font-size: .72rem;
		color: var(--gray-700);
	}

	.shortcut-desc {
		font-size: .85rem;
		color: var(--text);
	}

	.overlay-hint {
		margin-top: 20px;
		text-align: center;
		font-size: .75rem;
		color: var(--text-muted);
	}

	.overlay-hint kbd {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-width: 18px;
		height: 18px;
		padding: 0 4px;
		background: var(--gray-100);
		border: 1px solid var(--gray-300);
		border-radius: 3px;
		font-family: var(--font-mono);
		font-size: .65rem;
		color: var(--gray-700);
	}
</style>
