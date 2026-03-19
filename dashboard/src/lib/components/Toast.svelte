<script lang="ts">
	interface Props {
		message: string;
		type?: 'info' | 'success' | 'error';
		undoLabel?: string;
		onundo?: () => void;
		ondismiss?: () => void;
		duration?: number;
	}

	let { message, type = 'info', undoLabel, onundo, ondismiss, duration = 5000 }: Props = $props();

	let visible = $state(true);
	let timer: ReturnType<typeof setTimeout> | undefined;

	function startTimer() {
		clearTimeout(timer);
		timer = setTimeout(() => {
			visible = false;
			ondismiss?.();
		}, duration);
	}

	startTimer();

	function handleUndo() {
		clearTimeout(timer);
		visible = false;
		onundo?.();
	}

	function handleDismiss() {
		clearTimeout(timer);
		visible = false;
		ondismiss?.();
	}
</script>

{#if visible}
	<div class="toast toast-{type}" role="alert">
		<span class="toast-message">{message}</span>
		{#if undoLabel && onundo}
			<button class="toast-undo" type="button" onclick={handleUndo}>{undoLabel}</button>
		{/if}
		<button class="toast-close" type="button" onclick={handleDismiss} aria-label="Dismiss">&times;</button>
	</div>
{/if}

<style>
	.toast {
		position: fixed;
		bottom: 24px;
		left: 50%;
		transform: translateX(-50%);
		display: flex;
		align-items: center;
		gap: 12px;
		padding: 10px 18px;
		background: var(--gray-900);
		color: #fff;
		border-radius: var(--radius);
		box-shadow: var(--shadow);
		font-size: .875rem;
		z-index: 9999;
		animation: toast-in .2s ease-out;
	}

	@keyframes toast-in {
		from { opacity: 0; transform: translateX(-50%) translateY(12px); }
		to   { opacity: 1; transform: translateX(-50%) translateY(0); }
	}

	.toast-error {
		background: var(--red-600);
	}

	.toast-success {
		background: var(--green-700);
	}

	.toast-message {
		flex: 1;
	}

	.toast-undo {
		background: rgba(255,255,255,.2);
		border: 1px solid rgba(255,255,255,.3);
		border-radius: var(--radius-sm);
		color: #fff;
		padding: 4px 12px;
		font-size: .8rem;
		font-weight: 600;
		cursor: pointer;
		font-family: var(--font);
		transition: background .12s;
	}
	.toast-undo:hover {
		background: rgba(255,255,255,.35);
	}

	.toast-close {
		background: none;
		border: none;
		color: rgba(255,255,255,.6);
		font-size: 1.1rem;
		cursor: pointer;
		padding: 0 2px;
		line-height: 1;
	}
	.toast-close:hover {
		color: #fff;
	}
</style>
