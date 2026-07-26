/**
 * Unit tests for the wealth-link banner condition (P1-a1x/P1-r3e/P1-r3c).
 *
 * Run with: npm run test:unit  (node --test; no bundler, no browser)
 *
 * Lives outside src/ so svelte-check's project include never picks it up —
 * the dashboard has no TypeScript test-runner types installed.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { accountMapBanner } from '../src/lib/plaidAccountMap.ts';

const clean = {
	account_map_pushed: true,
	account_map_counts: {
		created: 2,
		reattached: 0,
		relinked: 0,
		already_mapped: 0,
		conflicts: 0,
		failed: 0
	}
};

test('a clean push shows the success banner', () => {
	const banner = accountMapBanner(clean, 'Vanguard');
	assert.equal(banner.ok, true);
	assert.match(banner.title, /Vanguard linked \(wealth-only\)/);
	assert.equal(banner.conflictMasks.length, 0);
});

test('conflicts fail the banner even when nothing hard-failed', () => {
	const banner = accountMapBanner(
		{
			account_map_pushed: false,
			account_map_counts: { created: 1, conflicts: 2, failed: 0 },
			account_map_conflict_masks: ['4321', '8899']
		},
		'Schwab'
	);
	assert.equal(banner.ok, false);
	assert.match(banner.title, /FAILED/);
	assert.match(banner.message, /Re-link/);
	assert.match(banner.countsSummary, /conflicts=2/);
	assert.deepEqual(banner.conflictMasks, ['4321', '8899']);
});

test('a nonzero failed count fails the banner', () => {
	const banner = accountMapBanner(
		{ account_map_pushed: false, account_map_counts: { created: 0, conflicts: 0, failed: 3 } },
		'Fidelity'
	);
	assert.equal(banner.ok, false);
	assert.match(banner.countsSummary, /failed=3/);
});

test('pushed=true is not enough when the counts disagree', () => {
	// Defense in depth: the banner never trusts the boolean alone.
	const banner = accountMapBanner(
		{ account_map_pushed: true, account_map_counts: { created: 1, conflicts: 1, failed: 0 } },
		'E*TRADE'
	);
	assert.equal(banner.ok, false);
});

test('a missing account_map_pushed is treated as failure, not success', () => {
	assert.equal(accountMapBanner({}, 'Vanguard').ok, false);
	assert.equal(accountMapBanner({ account_map_pushed: null }, 'Vanguard').ok, false);
});

test('failure copy survives a response with no conflict masks', () => {
	const banner = accountMapBanner(
		{ account_map_pushed: false, account_map_counts: { failed: 1 } },
		'PenFed'
	);
	assert.equal(banner.ok, false);
	assert.deepEqual(banner.conflictMasks, []);
});
