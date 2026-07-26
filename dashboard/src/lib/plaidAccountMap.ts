/**
 * P1-a1x/P1-r3e/P1-r3c: banner state for a wealth-scope Plaid link.
 *
 * A wealth-only link does no register account mapping, so the ONLY thing that
 * makes the connection usable is the box's account-map push to the wealth D1.
 * If that push didn't fully land, every balance and holding for the new Item
 * is silently skipped on the D1 side until an operator intervenes — so the
 * page must never show an unconditional "linked" banner.
 *
 * Kept dependency-free so it can be unit-tested without a browser or a
 * bundler (see dashboard/tests/plaidAccountMap.test.mjs).
 */

/** Counts returned by the D1 `ingest/plaid-account-map` endpoint, relayed
 *  verbatim by the box on the exchange response. */
export interface AccountMapCounts {
	created?: number;
	reattached?: number;
	relinked?: number;
	already_mapped?: number;
	conflicts?: number;
	failed?: number;
}

/** The subset of the exchange response this helper reads. */
export interface AccountMapOutcome {
	account_map_pushed?: boolean | null;
	account_map_counts?: AccountMapCounts | null;
	account_map_conflict_masks?: string[] | null;
}

export interface AccountMapBanner {
	ok: boolean;
	/** Short headline, e.g. "Vanguard linked (wealth-only)". */
	title: string;
	/** Body copy — on failure, tells the operator exactly what to do next. */
	message: string;
	/** "created=1 conflicts=2 failed=0" — only populated on failure. */
	countsSummary: string;
	/** Masks of accounts D1 refused to map; only populated on failure. */
	conflictMasks: string[];
}

function summarizeCounts(counts: AccountMapCounts): string {
	return (
		[
			['created', counts.created],
			['reattached', counts.reattached],
			['relinked', counts.relinked],
			['already_mapped', counts.already_mapped],
			['conflicts', counts.conflicts],
			['failed', counts.failed]
		] as const
	)
		.filter(([, v]) => typeof v === 'number')
		.map(([k, v]) => `${k}=${v}`)
		.join(' ');
}

/**
 * Success requires ALL THREE: the box reported the push succeeded, zero hard
 * failures, and zero conflicts. A conflict means D1 found two candidate rows
 * and refused to guess — the account stays unmapped, which is a failure for
 * this banner's purpose even though nothing errored.
 *
 * A missing `account_map_pushed` (an older backend, or a response that never
 * carried the field) is NOT treated as success — unknown is not OK here.
 */
export function accountMapBanner(
	outcome: AccountMapOutcome,
	institutionName: string
): AccountMapBanner {
	const counts: AccountMapCounts = outcome.account_map_counts ?? {};
	const failed = counts.failed ?? 0;
	const conflicts = counts.conflicts ?? 0;
	const ok = outcome.account_map_pushed === true && failed === 0 && conflicts === 0;

	if (ok) {
		return {
			ok: true,
			title: `${institutionName} linked (wealth-only)`,
			message:
				'Balances and holdings feed the wealth dashboard only. The account mapping was ' +
				'pushed to the wealth database, so no register account mapping is needed here.',
			countsSummary: '',
			conflictMasks: []
		};
	}

	const conflictMasks = outcome.account_map_conflict_masks ?? [];
	return {
		ok: false,
		title: `${institutionName} linked — but the wealth account mapping FAILED`,
		message:
			'The connection exists, but its accounts could not be mapped in the wealth database. ' +
			'Until this is fixed, every balance and holding for this connection will be skipped. ' +
			'Use "Re-link" on this connection to retry the mapping push; if conflicts are listed ' +
			'below, retire the duplicate wealth account rows for those masks first.',
		countsSummary: summarizeCounts(counts),
		conflictMasks
	};
}
