<script lang="ts">
	import type { Transaction, Entity, TaxCategory, Direction, TransactionUpdate } from '$lib/types';
	import { updateTransaction, confirmTransaction, extractReceipt, splitTransaction } from '$lib/api';
	import type { SplitLineItem } from '$lib/api';
	import { BUSINESS_CATEGORIES, PERSONAL_CATEGORIES, SUBCATEGORIES, entityBadgeClass } from '$lib/categories';

	interface Props {
		transaction: Transaction;
		focused?: boolean;
		selected?: boolean;
		onconfirmed?: (tx: Transaction) => void;
		onfocusrequest?: () => void;
		onselect?: (tx: Transaction) => void;
		onreject?: (tx: Transaction) => void;
		/** Callback signals that the card wants a specific keyboard action */
		onkeyaction?: (action: string) => void;
	}

	let { transaction, focused = false, selected = false, onconfirmed, onfocusrequest, onselect, onreject, onkeyaction }: Props = $props();

	// Initialise from the prop's current value. These are intentionally
	// local editing copies — the user can change entity/category before
	// confirming, so we want them decoupled from the parent prop.
	// eslint-disable-next-line svelte/reactivity
	let entity: string = $state(transaction.entity ?? '');
	// eslint-disable-next-line svelte/reactivity
	let taxCategory: string = $state(transaction.tax_category ?? '');
	// eslint-disable-next-line svelte/reactivity
	let taxSubcategory: string = $state(transaction.tax_subcategory ?? '');
	// eslint-disable-next-line svelte/reactivity
	let direction: string = $state(transaction.direction ?? '');
	let reasoningOpen = $state(false);
	let detailOpen = $state(false);
	let saving = $state(false);
	let error = $state('');
	// eslint-disable-next-line svelte/reactivity
	let notes = $state(transaction.notes ?? '');
	let extracting = $state(false);
	let pdfViewPath = $state<string | null>(null);

	// ── Split panel ──────────────────────────────────────────────────────────
	let splitOpen = $state(false);
	let splitRows = $state<{ amount: string; entity: string; tax_category: string; description: string }[]>([]);
	let splitSaving = $state(false);
	let splitError = $state('');

	const HOTEL_KEYWORDS = ['hotel', 'marriott', 'hilton', 'hyatt', 'holiday inn', 'hampton', 'courtyard', 'fairfield', 'residence inn', 'springhill', 'doubletree', 'embassy', 'westin', 'sheraton', 'radisson', 'best western', 'motel', 'lodge', 'inn', 'resort'];

	function isHotelTransaction(): boolean {
		const text = `${transaction.vendor ?? ''} ${transaction.description}`.toLowerCase();
		return HOTEL_KEYWORDS.some(k => text.includes(k));
	}

	function initSplitRows() {
		if (splitRows.length > 0) return;
		const amt = Math.abs(Number(transaction.amount) || 0);
		if (isHotelTransaction() && amt > 0) {
			const roomAmt = Math.round(amt * 0.80 * 100) / 100;
			const mealsAmt = Math.round((amt - roomAmt) * 100) / 100;
			splitRows = [
				{ amount: String(roomAmt), entity: entity, tax_category: 'TRAVEL', description: 'Room' },
				{ amount: String(mealsAmt), entity: entity, tax_category: 'MEALS', description: 'Meals' },
			];
		} else {
			splitRows = [
				{ amount: '', entity: entity, tax_category: taxCategory, description: '' },
				{ amount: '', entity: entity, tax_category: taxCategory, description: '' },
			];
		}
	}

	function toggleSplit() {
		splitOpen = !splitOpen;
		if (splitOpen) initSplitRows();
		splitError = '';
	}

	function addSplitRow() {
		splitRows = [...splitRows, { amount: '', entity: entity, tax_category: taxCategory, description: '' }];
	}

	function removeSplitRow(index: number) {
		splitRows = splitRows.filter((_, i) => i !== index);
	}

	let splitSum = $derived(
		splitRows.reduce((sum, r) => {
			const v = parseFloat(r.amount);
			return sum + (isNaN(v) ? 0 : v);
		}, 0)
	);

	let parentAbsAmount = $derived(Math.abs(Number(transaction.amount) || 0));

	let splitSumMatches = $derived(
		parentAbsAmount > 0 && Math.abs(splitSum - parentAbsAmount) < 0.005
	);

	let splitDelta = $derived(
		Math.round((parentAbsAmount - splitSum) * 100) / 100
	);

	async function applySplit() {
		if (!splitSumMatches || splitSaving) return;
		splitSaving = true;
		splitError = '';
		try {
			const sign = Number(transaction.amount) < 0 ? -1 : 1;
			const lineItems: SplitLineItem[] = splitRows.map(r => ({
				amount: parseFloat(r.amount) * sign,
				entity: r.entity || null,
				tax_category: r.tax_category || null,
				description: r.description || null,
			}));
			const result = await splitTransaction(transaction.id, lineItems);
			transaction = result.parent;
			splitOpen = false;
			splitRows = [];
			onconfirmed?.(result.parent);
		} catch (e) {
			splitError = e instanceof Error ? e.message : 'Split failed';
		} finally {
			splitSaving = false;
		}
	}

	// ── Amount editing ────────────────────────────────────────────────────────
	let amountEditing = $state(false);
	// eslint-disable-next-line svelte/reactivity
	let amountInput = $state(
		transaction.amount ? String(Math.abs(Number(transaction.amount))) : ''
	);
	// eslint-disable-next-line svelte/reactivity
	let amountSign = $state(
		transaction.amount && Number(transaction.amount) > 0 ? 'income' : 'expense'
	);
	let amountSaving = $state(false);
	let amountError = $state('');

	// ── Email viewer ──────────────────────────────────────────────────────────
	let emailViewMode = $state<'html' | 'text'>('html');
	let copyFeedback = $state<string | null>(null);


	let availableSubcategories = $derived(
		taxCategory ? (SUBCATEGORIES[taxCategory] ?? []) : []
	);

	function formatCurrency(amount: number | string): string {
		return new Intl.NumberFormat('en-US', {
			style: 'currency',
			currency: 'USD',
			minimumFractionDigits: 2
		}).format(Number(amount));
	}

	function formatDate(iso: string): string {
		return new Intl.DateTimeFormat('en-US', {
			month: 'short', day: 'numeric', year: 'numeric'
		}).format(new Date(iso));
	}

	function confidenceClass(score: number | null): string {
		if (score === null) return '';
		if (score >= 0.8) return 'confidence-high';
		if (score >= 0.5) return 'confidence-medium';
		return 'confidence-low';
	}

	function confidenceLabel(score: number | null): string {
		if (score === null) return '';
		return `${Math.round(score * 100)}%`;
	}

	// ── Email / attachment helpers ────────────────────────────────────────────

	function emailSubject(): string {
		return transaction.raw_data?.subject ?? '';
	}

	function emailFrom(): string {
		return transaction.raw_data?.from ?? '';
	}

	function emailDate(): string {
		const d = transaction.raw_data?.date;
		if (!d) return '';
		try {
			return new Intl.DateTimeFormat('en-US', {
				month: 'short', day: 'numeric', year: 'numeric',
				hour: 'numeric', minute: '2-digit'
			}).format(new Date(String(d)));
		} catch {
			return String(d);
		}
	}

	function emailHasHtml(): boolean {
		const html = transaction.raw_data?.body_html;
		return typeof html === 'string' && html.trim().length > 0;
	}

	function emailBodyText(): string {
		return transaction.raw_data?.body_text ?? '';
	}

	function emailBodyHtml(): string {
		return transaction.raw_data?.body_html ?? '';
	}

	function hasEmailContent(): boolean {
		if (!transaction.raw_data) return false;
		return emailHasHtml() || (emailBodyText() !== '' && emailBodyText() !== 'No plain text body available.');
	}

	function shouldShowHtml(): boolean {
		// Prefer HTML; fall back to text
		if (emailViewMode === 'html' && emailHasHtml()) return true;
		return false;
	}

	function filenameFromPath(path: string): string {
		return path.split('/').pop() ?? path;
	}

	function fileExtension(path: string): string {
		return (path.split('.').pop() ?? '').toLowerCase();
	}

	function fileTypeIcon(path: string): string {
		const ext = fileExtension(path);
		if (ext === 'pdf') return '📄';
		if (['jpg', 'jpeg', 'png', 'gif', 'webp', 'heic'].includes(ext)) return '🖼';
		if (['json'].includes(ext)) return '{ }';
		return '📎';
	}

	function isImage(path: string): boolean {
		return ['jpg', 'jpeg', 'png', 'gif', 'webp'].includes(fileExtension(path));
	}

	function isPdf(path: string): boolean {
		return fileExtension(path) === 'pdf';
	}

	function attachmentUrl(path: string): string {
		return `/api/attachments/serve?path=${encodeURIComponent(path)}`;
	}

	async function copyPath(path: string) {
		try {
			await navigator.clipboard.writeText(path);
			copyFeedback = path;
			setTimeout(() => { copyFeedback = null; }, 1800);
		} catch {
			copyFeedback = null;
		}
	}

	// ── Amount editing ────────────────────────────────────────────────────────

	function startEditAmount(e: MouseEvent) {
		e.stopPropagation();
		amountEditing = true;
		amountError = '';
	}

	async function commitAmount() {
		const raw = amountInput.trim().replace(/[$,]/g, '');
		if (raw === '') {
			amountEditing = false;
			return;
		}
		const parsed = parseFloat(raw);
		if (isNaN(parsed) || parsed < 0) {
			amountError = 'Enter a valid positive number';
			return;
		}
		const signed = amountSign === 'expense' ? -parsed : parsed;
		if (String(signed) === String(Number(transaction.amount))) {
			amountEditing = false;
			return;
		}
		amountSaving = true;
		amountError = '';
		try {
			const result = await updateTransaction(transaction.id, { amount: signed });
			// Reflect the updated amount back without removing the card
			transaction = result;
			amountInput = String(Math.abs(Number(result.amount)));
			amountSign = Number(result.amount) > 0 ? 'income' : 'expense';
			amountEditing = false;
		} catch (e) {
			amountError = e instanceof Error ? e.message : 'Failed to save amount';
		} finally {
			amountSaving = false;
		}
	}

	function handleAmountKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter') { e.preventDefault(); commitAmount(); }
		if (e.key === 'Escape') { amountEditing = false; amountError = ''; }
	}

	// ── Extract receipt via Claude CLI ────────────────────────────────────────

	function hasExtractableAttachment(): boolean {
		const exts = ['jpg', 'jpeg', 'png', 'gif', 'webp', 'pdf'];
		return (transaction.attachments ?? []).some(
			(p) => exts.includes(fileExtension(p))
		);
	}

	async function handleExtract() {
		extracting = true;
		error = '';
		try {
			const result = await extractReceipt(transaction.id);
			// Update the card with extracted data
			transaction = result.transaction;
			entity = transaction.entity ?? entity;
			taxCategory = transaction.tax_category ?? taxCategory;
			notes = transaction.notes ?? '';
			if (transaction.amount) {
				amountInput = String(Math.abs(Number(transaction.amount)));
				amountSign = Number(transaction.amount) > 0 ? 'income' : 'expense';
			}
		} catch (e) {
			error = e instanceof Error ? e.message : 'Extraction failed';
		} finally {
			extracting = false;
		}
	}

	// ── Direction change ─────────────────────────────────────────────────────

	async function handleDirectionChange() {
		if (!direction) return;
		try {
			const result = await updateTransaction(transaction.id, { direction: direction as Direction });
			transaction = result;
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to update direction';
		}
	}

	// ── Confirm / reject ──────────────────────────────────────────────────────

	async function handleConfirm() {
		if (saving) return;
		saving = true;
		error = '';
		try {
			const updates: TransactionUpdate = {};
			if (entity && entity !== transaction.entity)
				updates.entity = entity as Entity;
			if (taxCategory && taxCategory !== transaction.tax_category)
				updates.tax_category = taxCategory as TaxCategory;
			if (taxSubcategory !== (transaction.tax_subcategory ?? ''))
				updates.tax_subcategory = taxSubcategory || undefined;
			if (direction && direction !== (transaction.direction ?? ''))
				updates.direction = direction as Direction;
			if (notes.trim() && notes.trim() !== (transaction.notes ?? ''))
				updates.notes = notes.trim();

			let result: Transaction;
			if (Object.keys(updates).length > 0) {
				await updateTransaction(transaction.id, updates);
			}
			result = await confirmTransaction(transaction.id);
			onconfirmed?.(result);
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to confirm';
		} finally {
			saving = false;
		}
	}

	async function handleReject() {
		if (saving) return;
		saving = true;
		error = '';
		try {
			const result = await updateTransaction(transaction.id, { status: 'rejected' });
			onreject?.(result);
			onconfirmed?.(result);
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to reject';
		} finally {
			saving = false;
		}
	}

	// ── Public methods for parent to call ────────────────────────────────────

	export function setEntity(val: string) {
		entity = val;
	}

	export function focusEntityField() {
		const el = document.getElementById(`entity-${transaction.id}`);
		el?.focus();
	}

	export function focusCategoryField() {
		const el = document.getElementById(`cat-${transaction.id}`);
		el?.focus();
	}

	export function focusNotesField() {
		const el = document.getElementById(`notes-${transaction.id}`);
		el?.focus();
	}

	export function doConfirm() {
		handleConfirm();
	}

	export function doReject() {
		handleReject();
	}

	export function doToggleSplit() {
		toggleSplit();
	}

	function handleCardKeydown(e: KeyboardEvent) {
		if (!focused) return;
		if (e.key === 'y' && !e.ctrlKey && !e.metaKey && !e.altKey) {
			const tag = (e.target as HTMLElement)?.tagName;
			if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
			e.preventDefault();
			handleConfirm();
		}
	}
</script>

<svelte:window onkeydown={handleCardKeydown} />

<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
	class="card tx-card"
	class:card-focused={focused}
	class:card-selected={selected}
	data-id={transaction.id}
	role="region"
	aria-label="Transaction: {transaction.vendor ?? transaction.description}"
	onclick={() => onfocusrequest?.()}
>
	{#if onselect}
		<div class="tx-select-wrap">
			<input
				type="checkbox"
				checked={selected}
				onchange={() => onselect?.(transaction)}
				onclick={(e) => e.stopPropagation()}
				aria-label="Select transaction"
				class="tx-checkbox"
			/>
		</div>
	{/if}

	<div class="tx-header">
		<div class="tx-main">
			<span class="tx-date">{formatDate(transaction.date)}</span>
			<h3 class="tx-vendor truncate">
				{transaction.vendor ?? transaction.description}
				{#if entity}
					<span class={entityBadgeClass(entity)}>{entity === 'sparkry' ? 'Sparkry' : entity === 'blackline' ? 'BlackLine' : 'Personal'}</span>
				{/if}
			</h3>
			{#if transaction.vendor && transaction.description !== transaction.vendor}
				<p class="tx-desc truncate">{transaction.description}</p>
			{/if}
		</div>
		<div class="tx-right">
			<!-- Amount: click to edit -->
			{#if amountEditing}
				<!-- svelte-ignore a11y_click_events_have_key_events -->
				<div class="amount-edit-wrap" onclick={(e) => e.stopPropagation()}>
					<div class="amount-edit-row">
						<select
							class="amount-sign-select"
							bind:value={amountSign}
							aria-label="Income or expense"
						>
							<option value="expense">−</option>
							<option value="income">+</option>
						</select>
						<!-- svelte-ignore a11y_autofocus -->
						<input
							type="number"
							class="amount-input"
							bind:value={amountInput}
							onkeydown={handleAmountKeydown}
							onblur={commitAmount}
							placeholder="0.00"
							min="0"
							step="0.01"
							disabled={amountSaving}
							aria-label="Amount"
							autofocus
						/>
					</div>
					{#if amountError}
						<p class="amount-error">{amountError}</p>
					{/if}
				</div>
			{:else if !transaction.amount || Number(transaction.amount) === 0}
				<!-- svelte-ignore a11y_click_events_have_key_events -->
				<button
					class="tx-amount tx-amount-missing amount-clickable"
					type="button"
					title="Click to enter amount"
					onclick={startEditAmount}
				>
					Amount missing — click to enter
				</button>
			{:else}
				<button
					class="tx-amount amount-clickable"
					class:amount-negative={Number(transaction.amount) < 0}
					class:amount-positive={Number(transaction.amount) > 0}
					type="button"
					title="Click to edit amount"
					onclick={startEditAmount}
				>
					{formatCurrency(transaction.amount)}
				</button>
			{/if}
			<div class="tx-badges">
				{#if transaction.confidence !== null}
					<span
						class="confidence-badge {confidenceClass(transaction.confidence)}"
						title="Classification confidence: how sure the system is about the entity and category assignment. 90%+ = high (auto-classified via known vendor rule), 70-89% = medium (pattern match), below 70% = low (needs human review)."
					>
						{confidenceLabel(transaction.confidence)}
					</span>
				{/if}
				<span class="status-pill status-{transaction.status}">
					{transaction.status.replace(/_/g, ' ')}
				</span>
			</div>
		</div>
	</div>

	<div class="tx-fields">
		<div class="field-group">
			<label class="field-label" for="entity-{transaction.id}">Entity</label>
			<select id="entity-{transaction.id}" bind:value={entity} class="field-select">
				<option value="">— unassigned —</option>
				<option value="sparkry">Sparkry AI LLC</option>
				<option value="blackline">BlackLine MTB LLC</option>
				<option value="personal">Personal</option>
			</select>
		</div>

		<div class="field-group">
			<label class="field-label" for="cat-{transaction.id}">Category</label>
			<select id="cat-{transaction.id}" bind:value={taxCategory} class="field-select">
				<option value="">— unassigned —</option>
				<optgroup label="Business (Schedule C / 1065)">
					{#each BUSINESS_CATEGORIES as cat}
						<option value={cat.value}>{cat.label}</option>
					{/each}
				</optgroup>
				<optgroup label="Personal (Schedule A)">
					{#each PERSONAL_CATEGORIES as cat}
						<option value={cat.value}>{cat.label}</option>
					{/each}
				</optgroup>
			</select>
		</div>

		{#if availableSubcategories.length > 0}
			<div class="field-group">
				<label class="field-label" for="subcat-{transaction.id}">Subcategory</label>
				<select id="subcat-{transaction.id}" bind:value={taxSubcategory} class="field-select">
					<option value="">— none —</option>
					{#each availableSubcategories as sub}
						<option value={sub.value}>{sub.label}</option>
					{/each}
				</select>
			</div>
		{/if}

		<div class="field-group">
			<label class="field-label" for="dir-{transaction.id}">Direction</label>
			<select id="dir-{transaction.id}" bind:value={direction} onchange={handleDirectionChange} class="field-select">
				<option value="">— unset —</option>
				<option value="income">Income</option>
				<option value="expense">Expense</option>
				<option value="reimbursable">Reimbursable</option>
				<option value="transfer" title="Transfer — excluded from P&L">Transfer</option>
			</select>
		</div>
	</div>

	<div class="tx-notes">
		<label class="field-label" for="notes-{transaction.id}">Notes <span class="optional-hint">(optional)</span></label>
		<textarea
			id="notes-{transaction.id}"
			class="notes-input"
			bind:value={notes}
			placeholder="Why did I classify it this way? Any context for later..."
			rows="2"
		></textarea>
	</div>

	{#if transaction.reasoning}
		<div class="tx-reasoning">
			<button
				class="reasoning-toggle"
				type="button"
				onclick={() => (reasoningOpen = !reasoningOpen)}
				aria-expanded={reasoningOpen}
			>
				<span class="reasoning-icon">{reasoningOpen ? '▾' : '▸'}</span>
				Why this classification?
			</button>
			{#if reasoningOpen}
				<p class="reasoning-text">{transaction.reasoning}</p>
			{/if}
		</div>
	{/if}

	{#if error}
		<p class="tx-error">{error}</p>
	{/if}

	<div class="tx-actions">
		{#if hasExtractableAttachment() && (!transaction.amount || transaction.description === 'Travis Sparks')}
			<button
				class="btn btn-secondary extract-btn"
				type="button"
				onclick={handleExtract}
				disabled={extracting || saving}
				title="Extract vendor/amount from attachment using Claude"
			>
				{extracting ? 'Extracting…' : 'Extract'}
			</button>
		{/if}
		<button
			class="btn btn-primary confirm-btn"
			type="button"
			onclick={handleConfirm}
			disabled={saving}
			title="Confirm (y)"
		>
			{saving ? 'Saving…' : 'Confirm'}
			{#if focused}
				<kbd class="key-hint">y</kbd>
			{/if}
		</button>
		<button
			class="btn btn-danger"
			type="button"
			onclick={handleReject}
			disabled={saving}
		>
			Reject
		</button>
		<button
			class="btn btn-ghost"
			type="button"
			onclick={toggleSplit}
			title="Split transaction (s)"
		>
			{splitOpen ? '▾ Close split' : '▸ Split'}
		</button>
		<span class="tx-source">{transaction.source.replace(/_/g, ' ')}</span>

		<!-- Detail toggle — only shown when there's email content or attachments -->
		{#if hasEmailContent() || (transaction.attachments && transaction.attachments.length > 0)}
			<button
				class="btn btn-ghost detail-toggle"
				type="button"
				onclick={(e) => { e.stopPropagation(); detailOpen = !detailOpen; }}
				aria-expanded={detailOpen}
				title={detailOpen ? 'Hide email / attachments' : 'Show email / attachments'}
			>
				{detailOpen ? '▾ Hide' : '▸ View email'}
			</button>
		{/if}
	</div>

	<!-- ── Split panel ─────────────────────────────────────────────────── -->
	{#if splitOpen}
		<!-- svelte-ignore a11y_click_events_have_key_events -->
		<div class="split-panel" onclick={(e) => e.stopPropagation()}>
			<div class="detail-divider"></div>
			<div class="split-header">
				<span class="split-label">Split Transaction</span>
				<span class="split-parent-amount">
					Parent: {formatCurrency(transaction.amount)}
				</span>
			</div>

			<div class="split-rows">
				{#each splitRows as row, i}
					<div class="split-row">
						<input
							type="number"
							class="split-amount-input"
							bind:value={row.amount}
							placeholder="0.00"
							step="0.01"
							min="0"
							aria-label="Split row {i + 1} amount"
						/>
						<select bind:value={row.entity} class="split-field-select" aria-label="Split row {i + 1} entity">
							<option value="">Entity…</option>
							<option value="sparkry">Sparkry</option>
							<option value="blackline">BlackLine</option>
							<option value="personal">Personal</option>
						</select>
						<select bind:value={row.tax_category} class="split-field-select" aria-label="Split row {i + 1} category">
							<option value="">Category…</option>
							<optgroup label="Business">
								{#each BUSINESS_CATEGORIES as cat}
									<option value={cat.value}>{cat.label}</option>
								{/each}
							</optgroup>
							<optgroup label="Personal">
								{#each PERSONAL_CATEGORIES as cat}
									<option value={cat.value}>{cat.label}</option>
								{/each}
							</optgroup>
						</select>
						<input
							type="text"
							class="split-desc-input"
							bind:value={row.description}
							placeholder="Description…"
							aria-label="Split row {i + 1} description"
						/>
						{#if splitRows.length > 2}
							<button class="btn btn-ghost split-remove-btn" type="button" onclick={() => removeSplitRow(i)} aria-label="Remove row" title="Remove row">&times;</button>
						{/if}
					</div>
				{/each}
			</div>

			<div class="split-footer">
				<button class="btn btn-ghost" type="button" onclick={addSplitRow}>+ Add Row</button>

				<div class="split-sum" class:split-sum-match={splitSumMatches} class:split-sum-mismatch={!splitSumMatches && splitSum > 0}>
					Sum: {formatCurrency(splitSum)}
					{#if splitSumMatches}
						<span class="split-check" title="Amounts match">&#10003;</span>
					{:else if splitSum > 0}
						<span class="split-delta" title="Delta from parent">
							({splitDelta > 0 ? '+' : ''}{formatCurrency(splitDelta)} remaining)
						</span>
					{/if}
				</div>

				<button
					class="btn btn-primary"
					type="button"
					onclick={applySplit}
					disabled={!splitSumMatches || splitSaving}
				>
					{splitSaving ? 'Splitting…' : 'Apply Split'}
				</button>
			</div>

			{#if splitError}
				<p class="tx-error" style="margin-top: 8px;">{splitError}</p>
			{/if}
		</div>
	{/if}

	<!-- ── Detail panel ─────────────────────────────────────────────────── -->
	{#if detailOpen}
		<!-- svelte-ignore a11y_click_events_have_key_events -->
		<div class="detail-panel" onclick={(e) => e.stopPropagation()}>
			<div class="detail-divider"></div>

			<!-- Attachments -->
			{#if transaction.attachments && transaction.attachments.length > 0}
				<div class="attachments-section">
					<p class="detail-section-label">Attachments</p>
					<!-- Inline previews for images and PDFs -->
					{#each transaction.attachments as path (path)}
						{#if isImage(path)}
							<div class="attachment-preview">
								<img
									src={attachmentUrl(path)}
									alt={filenameFromPath(path)}
									class="attachment-image"
									loading="lazy"
								/>
								<span class="attachment-preview-name">{filenameFromPath(path)}</span>
							</div>
						{:else if isPdf(path)}
							{#if pdfViewPath === path}
								<div class="attachment-preview">
									<iframe
										src={attachmentUrl(path)}
										title={filenameFromPath(path)}
										class="attachment-pdf"
									></iframe>
									<span class="attachment-preview-name">
										{filenameFromPath(path)}
										<button class="btn btn-ghost" type="button" onclick={(e) => { e.stopPropagation(); pdfViewPath = null; }} style="margin-left: 8px; font-size: .7rem;">Close</button>
									</span>
								</div>
							{/if}
						{/if}
					{/each}
					<!-- File list for all attachments -->
					<ul class="attachment-list">
						{#each transaction.attachments as path (path)}
							<li class="attachment-item">
								<span class="file-icon">{fileTypeIcon(path)}</span>
								<span class="file-name" title={path}>{filenameFromPath(path)}</span>
								{#if isPdf(path)}
									<button
										class="btn btn-ghost copy-btn"
										type="button"
										onclick={(e) => { e.stopPropagation(); pdfViewPath = pdfViewPath === path ? null : path; }}
									>{pdfViewPath === path ? 'Hide' : 'View PDF'}</button>
								{:else if isImage(path)}
									<a
										href={attachmentUrl(path)}
										target="_blank"
										rel="noopener"
										class="btn btn-ghost copy-btn"
									>Open</a>
								{/if}
								<button
									class="btn btn-ghost copy-btn"
									type="button"
									onclick={() => copyPath(path)}
									title="Copy full path to clipboard"
								>
									{copyFeedback === path ? '✓ Copied' : 'Copy path'}
								</button>
							</li>
						{/each}
					</ul>
				</div>
			{/if}

			<!-- Email content -->
			{#if hasEmailContent()}
				<div class="email-section">
					<!-- Email metadata -->
					<div class="email-meta">
						{#if emailSubject()}
							<p class="email-subject">{emailSubject()}</p>
						{/if}
						<div class="email-from-row">
							{#if emailFrom()}
								<span class="email-from">{emailFrom()}</span>
							{/if}
							{#if emailDate()}
								<span class="email-meta-date">{emailDate()}</span>
							{/if}
						</div>
					</div>

					<!-- View mode toggle -->
					{#if emailHasHtml() && emailBodyText() !== '' && emailBodyText() !== 'No plain text body available.'}
						<div class="email-view-toggle">
							<button
								class="toggle-pill"
								class:active={emailViewMode === 'html'}
								type="button"
								onclick={() => (emailViewMode = 'html')}
							>HTML</button>
							<button
								class="toggle-pill"
								class:active={emailViewMode === 'text'}
								type="button"
								onclick={() => (emailViewMode = 'text')}
							>Plain text</button>
						</div>
					{/if}

					<!-- Email body -->
					<div class="email-body-wrap">
						{#if shouldShowHtml()}
							<iframe
								class="email-iframe"
								title="Email content"
								srcdoc={emailBodyHtml()}
								sandbox="allow-same-origin"
								scrolling="auto"
							></iframe>
						{:else}
							<pre class="email-plain">{emailBodyText()}</pre>
						{/if}
					</div>
				</div>
			{/if}
		</div>
	{/if}
</div>

<style>
	.tx-card {
		padding: 20px 22px;
		transition: box-shadow .15s;
		position: relative;
	}

	.card-selected {
		border-color: var(--blue-500);
		background: rgba(59,130,246,.04);
	}

	.tx-select-wrap {
		position: absolute;
		top: 14px;
		left: 8px;
	}

	.tx-checkbox {
		width: 16px;
		height: 16px;
		cursor: pointer;
		accent-color: var(--blue-500);
	}

	.tx-header {
		display: flex;
		gap: 16px;
		align-items: flex-start;
		margin-bottom: 16px;
	}

	.tx-main {
		flex: 1;
		min-width: 0;
	}

	.tx-date {
		display: block;
		font-size: .75rem;
		color: var(--text-muted);
		margin-bottom: 3px;
	}

	.tx-vendor {
		font-size: 1.05rem;
		font-weight: 600;
		margin: 0;
	}

	.tx-desc {
		font-size: .8rem;
		color: var(--text-muted);
		margin-top: 2px;
	}

	.tx-right {
		display: flex;
		flex-direction: column;
		align-items: flex-end;
		gap: 6px;
		flex-shrink: 0;
	}

	.tx-amount {
		font-size: 1.35rem;
		font-weight: 700;
		font-variant-numeric: tabular-nums;
		letter-spacing: -.5px;
	}

	.tx-amount-missing {
		font-size: .8rem;
		font-weight: 500;
		color: var(--text-muted);
		font-style: italic;
		letter-spacing: 0;
	}

	/* Amount as a button (click to edit) */
	.amount-clickable {
		background: none;
		border: none;
		cursor: pointer;
		padding: 2px 4px;
		border-radius: var(--radius-sm);
		transition: background .12s;
		font-family: var(--font);
		text-align: right;
	}
	.amount-clickable:hover {
		background: var(--gray-100);
	}

	/* Amount edit inline widget */
	.amount-edit-wrap {
		display: flex;
		flex-direction: column;
		align-items: flex-end;
		gap: 4px;
	}
	.amount-edit-row {
		display: flex;
		align-items: center;
		gap: 4px;
	}
	.amount-sign-select {
		width: 48px;
		padding: 4px 6px;
		font-size: 1rem;
		font-weight: 700;
		text-align: center;
	}
	.amount-input {
		width: 110px;
		font-size: 1.1rem;
		font-weight: 600;
		font-variant-numeric: tabular-nums;
		text-align: right;
		padding: 4px 8px;
	}
	/* Hide spinner arrows */
	.amount-input::-webkit-outer-spin-button,
	.amount-input::-webkit-inner-spin-button {
		-webkit-appearance: none;
	}
	.amount-input[type=number] { -moz-appearance: textfield; }
	.amount-error {
		font-size: .72rem;
		color: var(--red-600);
		text-align: right;
	}

	.tx-badges {
		display: flex;
		gap: 5px;
		flex-wrap: wrap;
		justify-content: flex-end;
	}

	.tx-fields {
		display: flex;
		gap: 12px;
		margin-bottom: 14px;
		flex-wrap: wrap;
	}

	.field-group {
		display: flex;
		flex-direction: column;
		gap: 4px;
		flex: 1;
		min-width: 140px;
	}

	.field-label {
		font-size: .7rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: .06em;
		color: var(--text-muted);
	}

	.field-select {
		width: 100%;
	}

	.tx-notes {
		margin-bottom: 14px;
	}

	.notes-input {
		width: 100%;
		padding: 8px 10px;
		font-size: .82rem;
		font-family: var(--font);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		background: var(--surface);
		color: var(--text);
		resize: vertical;
		min-height: 38px;
		line-height: 1.4;
		transition: border-color .15s;
	}
	.notes-input::placeholder {
		color: var(--gray-400);
		font-style: italic;
	}
	.notes-input:focus {
		outline: none;
		border-color: var(--gray-500);
	}

	.optional-hint {
		font-weight: 400;
		font-size: .65rem;
		color: var(--gray-400);
		text-transform: none;
		letter-spacing: 0;
	}

	.tx-reasoning {
		margin-bottom: 14px;
	}

	.reasoning-toggle {
		background: none;
		border: none;
		cursor: pointer;
		font-size: .8rem;
		color: var(--text-muted);
		display: flex;
		align-items: center;
		gap: 4px;
		padding: 0;
		font-family: var(--font);
	}

	.reasoning-toggle:hover {
		color: var(--text);
	}

	.reasoning-icon {
		font-size: .75rem;
	}

	.reasoning-text {
		margin-top: 6px;
		font-size: .8rem;
		color: var(--text-muted);
		line-height: 1.5;
		background: var(--gray-50);
		border-radius: var(--radius-sm);
		padding: 8px 10px;
		border-left: 2px solid var(--gray-300);
	}

	.tx-error {
		font-size: .8rem;
		color: var(--red-600);
		margin-bottom: 10px;
	}

	.tx-actions {
		display: flex;
		align-items: center;
		gap: 8px;
		flex-wrap: wrap;
	}

	.extract-btn {
		min-width: 90px;
		font-size: .82rem;
	}

	.confirm-btn {
		min-width: 110px;
	}

	.key-hint {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 16px;
		height: 16px;
		background: rgba(255,255,255,.25);
		border-radius: 3px;
		font-size: .65rem;
		font-family: var(--font-mono);
		border: 1px solid rgba(255,255,255,.3);
	}

	.tx-source {
		margin-left: auto;
		font-size: .72rem;
		color: var(--text-muted);
		text-transform: uppercase;
		letter-spacing: .05em;
	}

	.detail-toggle {
		font-size: .78rem;
		padding: 4px 10px;
		color: var(--gray-600);
	}

	/* ── Split panel ──────────────────────────────────────────────────────── */
	.split-panel {
		margin-top: 4px;
	}

	.split-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		margin-bottom: 12px;
	}

	.split-label {
		font-size: .8rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: .04em;
		color: var(--text-muted);
	}

	.split-parent-amount {
		font-size: .85rem;
		font-weight: 600;
		font-variant-numeric: tabular-nums;
		color: var(--text);
		background: var(--gray-100);
		padding: 3px 10px;
		border-radius: var(--radius-sm);
	}

	.split-rows {
		display: flex;
		flex-direction: column;
		gap: 8px;
		margin-bottom: 12px;
	}

	.split-row {
		display: flex;
		gap: 6px;
		align-items: center;
		flex-wrap: wrap;
	}

	.split-amount-input {
		width: 100px;
		font-size: .85rem;
		font-variant-numeric: tabular-nums;
		text-align: right;
		padding: 5px 8px;
		font-family: var(--font);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		background: var(--surface);
		color: var(--text);
	}
	.split-amount-input::-webkit-outer-spin-button,
	.split-amount-input::-webkit-inner-spin-button {
		-webkit-appearance: none;
	}
	.split-amount-input[type=number] { -moz-appearance: textfield; }

	.split-field-select {
		min-width: 120px;
		font-size: .82rem;
		padding: 5px 8px;
	}

	.split-desc-input {
		flex: 1;
		min-width: 100px;
		font-size: .82rem;
		padding: 5px 8px;
		font-family: var(--font);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		background: var(--surface);
		color: var(--text);
	}

	.split-remove-btn {
		padding: 2px 8px;
		font-size: 1rem;
		color: var(--red-600);
		line-height: 1;
	}

	.split-footer {
		display: flex;
		align-items: center;
		gap: 12px;
		flex-wrap: wrap;
	}

	.split-sum {
		font-size: .85rem;
		font-weight: 600;
		font-variant-numeric: tabular-nums;
		display: flex;
		align-items: center;
		gap: 6px;
		margin-left: auto;
	}

	.split-sum-match {
		color: var(--green-600);
	}

	.split-sum-mismatch {
		color: var(--amber-600);
	}

	.split-check {
		color: var(--green-600);
		font-weight: 700;
	}

	.split-delta {
		font-size: .75rem;
		font-weight: 500;
		color: var(--red-600);
	}

	/* ── Detail panel ─────────────────────────────────────────────────────── */
	.detail-panel {
		margin-top: 4px;
	}

	.detail-divider {
		height: 1px;
		background: var(--border);
		margin: 16px 0;
	}

	/* Attachments */
	.detail-section-label {
		font-size: .7rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: .06em;
		color: var(--text-muted);
		margin-bottom: 8px;
	}

	.attachment-preview {
		margin-bottom: 12px;
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		overflow: hidden;
		background: var(--surface);
	}

	.attachment-image {
		display: block;
		max-width: 100%;
		max-height: 500px;
		object-fit: contain;
		margin: 0 auto;
		background: var(--gray-50);
	}

	.attachment-pdf {
		display: block;
		width: 100%;
		height: 500px;
		border: none;
	}

	.attachment-preview-name {
		display: block;
		font-size: .72rem;
		color: var(--text-muted);
		padding: 6px 10px;
		border-top: 1px solid var(--border);
		background: var(--gray-50);
	}

	.attachment-list {
		list-style: none;
		display: flex;
		flex-direction: column;
		gap: 6px;
		margin-bottom: 20px;
	}

	.attachment-item {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 7px 10px;
		background: var(--gray-50);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
	}

	.file-icon {
		font-size: .9rem;
		flex-shrink: 0;
		font-style: normal;
	}

	.file-name {
		font-size: .8rem;
		font-family: var(--font-mono);
		color: var(--gray-700);
		flex: 1;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		min-width: 0;
	}

	.copy-btn {
		font-size: .72rem;
		padding: 3px 8px;
		flex-shrink: 0;
		white-space: nowrap;
	}

	/* Email section */
	.email-section {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.email-meta {
		display: flex;
		flex-direction: column;
		gap: 4px;
		padding: 10px 12px;
		background: var(--gray-50);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
	}

	.email-subject {
		font-size: .9rem;
		font-weight: 600;
		color: var(--text);
		line-height: 1.3;
	}

	.email-from-row {
		display: flex;
		align-items: center;
		gap: 12px;
		flex-wrap: wrap;
	}

	.email-from {
		font-size: .78rem;
		color: var(--text-muted);
	}

	.email-meta-date {
		font-size: .75rem;
		color: var(--gray-400);
		margin-left: auto;
	}

	.email-view-toggle {
		display: flex;
		gap: 2px;
		align-self: flex-start;
	}

	.toggle-pill {
		padding: 3px 10px;
		border: 1px solid var(--border);
		border-radius: 999px;
		font-size: .72rem;
		font-weight: 500;
		background: transparent;
		color: var(--text-muted);
		cursor: pointer;
		font-family: var(--font);
		transition: background .1s, color .1s;
	}
	.toggle-pill:hover {
		background: var(--gray-100);
	}
	.toggle-pill.active {
		background: var(--gray-900);
		color: #fff;
		border-color: var(--gray-900);
	}

	.email-body-wrap {
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		overflow: hidden;
		background: var(--surface);
	}

	.email-iframe {
		width: 100%;
		min-height: 300px;
		max-height: 520px;
		border: none;
		display: block;
		background: var(--surface);
	}

	.email-plain {
		font-size: .78rem;
		font-family: var(--font-mono);
		color: var(--gray-700);
		white-space: pre-wrap;
		word-break: break-word;
		line-height: 1.6;
		padding: 14px 16px;
		max-height: 400px;
		overflow-y: auto;
		margin: 0;
	}
</style>
