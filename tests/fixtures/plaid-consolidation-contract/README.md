# Plaid consolidation cross-repo contract fixtures

Fixes P1-xct: the box's payload builders/chunkers (this repo) and the wealth
Worker endpoints that consume them (`sparkry-crm`) were verified by entirely
separate test suites with hand-written fixtures on each side, and nothing
pinned the two together — which is exactly how P0-a1c shipped (the chunker's
own test asserts `securities: []` on holdings-only chunks; the endpoint's own
test suite had zero cases with an empty `securities` array).

These JSON files are a **committed, literal slice of real
`chunk_holdings_payload()` output** (see
`src/adapters/test_plaid_investments.py::test_contract_fixture_matches_committed_json`,
which regenerates the payload and asserts it round-trips byte-identical to
these files — so a future change to the chunker that silently alters the
shape fails a test in THIS repo, not just a drift nobody notices until it
ships).

- `01-securities-chunk.json` — the first securities-only chunk
  (`chunk_holdings_payload` ships securities before any holdings), containing
  `sec_aapl_contract` (ticker AAPL).
- `02-holdings-chunk.json` — a later holdings-only chunk (`securities: []`)
  whose single holding references `sec_aapl_contract` by `security_id` — the
  exact shape P0-a1c's fix (D1 fallback to the persisted `plaid_security`
  row) targets.

**sparkry-crm** loads these exact two files (copied verbatim — the two repos
share no filesystem) as request bodies in
`tests/unit/ingest-plaid-holdings-contract.test.ts`, POSTs them in order, and
asserts the resulting `plaid_investment_holding` row has `ticker='AAPL'`,
`name='Apple Inc'`, `type='equity'` — not `null` / `sec_aapl_contract` /
`'other'` (the P0-a1c regression shape). If either side's fixture drifts from
the other, the two repos' tests are loading different JSON and a mismatch is
caught the moment either one regenerates its committed copy — the cheapest
structural guard against this whole class of defect.

To regenerate (only when `chunk_holdings_payload`'s output shape intentionally
changes): see the generation script embedded in
`test_contract_fixture_matches_committed_json`, then manually copy both files
into `sparkry-crm-plaidcons/tests/fixtures/plaid-consolidation-contract/` —
there is no automated sync between the two repos.

## A1 (balance push) golden row — P2-006

- `03-balance-chunk.json` — a committed, literal `{"snapshots": [...]}`
  payload built from the REAL box builder (`_fresh_balance_row` in
  `src/adapters/plaid_balance.py`), pinned by
  `src/adapters/test_plaid_balance.py::test_balance_contract_fixture_matches_committed_json`
  the same way `01`/`02` are pinned for A2. Two rows:
  - a plain USD **depository/checking** row where `current`/`available` are
    passed as Python floats (`1234.5` / `1000`) — exercises the
    ROUND_HALF_UP-to-2dp string-money path (`"1234.50"`/`"1000.00"`);
  - a **credit** row where `current` is passed as a pre-stringified value
    (`"500.005"`) — exercises the string-money quantize path (HALF_UP →
    `"500.01"`) AND the read-side liability-negation predicate, which keys on
    `plaid_account_type` ∈ `{credit, loan}` (`brokerage-summary.ts`).

  **sparkry-crm** loads this file verbatim (copied — the two repos share no
  filesystem) as the request body in
  `tests/unit/ingest-plaid-balance-contract.test.ts`, POSTs it, and asserts
  the resulting `plaid_account_balance_snapshot` rows match
  `current_balance`/`available_balance`/`plaid_account_type` field-by-field
  against the fixture — not just "some row exists". If either side's copy
  drifts, a mismatch is caught the moment either one regenerates.
