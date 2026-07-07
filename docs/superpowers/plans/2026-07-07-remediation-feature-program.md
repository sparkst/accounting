# Execution Plan — Remediation + Feature Program (2026-07)

Owner: Travis Sparks. Orchestrator: Fable 5 (this session — design + final review only).
Requirements: `requirements/current.md` § "Program 2026-07" (REQ-FIX-* + 10 feature groups).
Designs: `docs/superpowers/specs/2026-07-07-*-design.md` (6 specs).
Branch: `feat/remediation-and-features-2026-07` (all work; merge to `main` per workstream after review convergence).

## Model policy

| Role | Model | Rationale |
|---|---|---|
| Requirements, designs, final delivery review | **Fable 5** (this session + design subagents) | Locked: design-only usage |
| Complex implementation (WS4 wealth math, WS6 agent features, migrations) | **Opus 4.8** | Multi-file, high-stakes financial math |
| Standard implementation (WS1–WS3, WS5 report generators) | **Sonnet** | Well-specified fixes with tests |
| Mechanical work (test scaffolds, doc updates, n8n hygiene, config files) | **Haiku 4.5** | Cheap, fast, low ambiguity |
| Review lenses (financial-correctness, security, code-quality, test-coverage) | **Sonnet default, Opus for security + financial-correctness** | Per qreview/qloop tiering policy |
| Production runtime LLM (Tier-3 classifier, MCA narrative, VIS extraction) | **Gemini (default) / OpenAI (fallback)** | Locked: cheap live models only; core logic deterministic |

## Workstreams and order

Dependencies: WS1 unblocks live data (balance sync is down NOW) → first. WS2 has filing
deadlines → second. WS3/WS4 independent → parallel after WS2 merges. WS5 depends on WS1
(delivery-health data) + WS2 (corrected P&L math). WS6 depends on WS3 (learning-loop fix
must land before auto-confirm) and WS5 (email plumbing).

| WS | Scope (REQs) | Impl model | Est SP | Deploy surface |
|---|---|---|---|---|
| WS1 Plaid + alerts | FIX-PLD-001..006, FIX-ALR-001..008, DHL-001..002 | Sonnet (Opus for the dispatcher fallback logic) | 21 | Box: api + timers; Doppler srv/dev provisioning; live smoke on real sync |
| WS2 Tax + invoicing | FIX-TAX-001..007, FIX-INV-001..005, FIX-API-001..004 | Sonnet (Opus for the payout backfill migration) | 21 | Box: api; backfill run --apply with pre-snapshot |
| WS3 Ingestion + classification | FIX-ING-001..010 | Sonnet | 13 | Box: api + sync timers |
| WS4 Wealth + policy features | FIX-WLT-001..009, FIX-DAT-001..003, IPD, NWA, BBT | Opus 4.8 | 34 | Box: api + dashboard rebuild; price backfill |
| WS5 Reporting suite | WBR, TXF, SEL | Sonnet | 21 | Box: new timers + Resend |
| WS6 Agent features | MCA, ARC, VIS | Opus 4.8 | 34 | Box: timers; n8n callback wiring; shadow-mode only for VIS |
| WS7 n8n hygiene | FIX-N8N-001..002 | Haiku 4.5 | 5 | n8n API (tags/stickies/tests); no workflow logic changes |

## Per-workstream pipeline (mandatory)

Each WS runs `/qpipeline` preset `code` semantics with the repo's four lenses:

1. **TDD implementation** — failing tests referencing REQ-IDs first (repo rule), then impl.
   Delegated to the assigned model as subagents; financial code additionally constrained by
   CLAUDE.md critical patterns (Decimal(str()), savepoints, DRY-RUN, additive migrations).
2. **review-loop (`/qloop`)** — clean-context reviewers: financial-correctness-reviewer +
   security-reviewer + code-quality + test-coverage (+ migration-reviewer when a migration
   exists, + tax-export-validator for WS2). Fix ALL P0–P3; re-review to zero P0+P1. Min 2 rounds.
3. **test-gate** — `pytest` and `ruff check src/` fully green (ruff baseline zeroed 2026-07-07).
   `mypy src/` has a pre-existing baseline of 875 strict-mode errors in 61 files (measured
   2026-07-07, mypy 1.19); the enforceable gate is **zero NEW mypy errors in files a
   workstream touches** (compare per-file error counts against baseline). Full-baseline
   burn-down is explicitly out of program scope. Plus `npm run build` for WS4 dashboard.
4. **verify** — fresh-context verifier agent, acceptance per REQ, PASS required.
5. **deploy** — commit → push → rsync to box → restart affected units (pre-approved).
   DB-touching workstreams snapshot first: `sqlite3 data/accounting.db ".backup ..."` on box.
6. **smoke** — live validation with evidence (WS1: real sync run writes today's snapshots;
   WS2: export golden diff on prod data; WS5: DRY-RUN render vs live data; WS6: shadow diff).
7. Merge WS branch → `main`.

## Cross-workstream migration ledger (merge gate: `alembic heads` == 1)

Six of the seven workstreams each author their own Alembic revision, but all Alembic revisions
in this repo form **one linear chain** via `down_revision` (current tip pre-program:
`na_iul_01`). The workstream table above shows WS1–WS6 with some running in parallel (WS3/WS4
after WS2), but the migration chain cannot branch — this ledger pins one deterministic order,
names every migration up front (three were left unnamed in their originating spec and are
named here), and makes "exactly one `alembic heads` line" a merge gate for every workstream,
not just the last one.

| Order | WS | Revision id | Purpose | Spec § |
|---|---|---|---|---|
| 1 | WS1 | `pld05_expected_account_ignored_status` | `expected_account` CHECK gains `'ignored'` | plaid-alert §6 |
| 2 | WS1 | `alr01_alert_dispatch_payload` | `alert_dispatch.payload_json` + `delivery_channel` | plaid-alert §8 |
| 3 | WS2 | `tax001_shopify_payout_backfill` | Shopify payout reclass (data migration) | tax-invoicing §2.2 |
| 4 | WS2 | `inv002_payment_link_amount` | `invoice.payment_link_amount` column | tax-invoicing §6 |
| 5 | WS3 | `vr_isregex01_vendor_rule_is_regex` | `vendor_rules.is_regex` | ingestion §3 |
| 6 | WS4 | `wa2607a_adjclose_splits` | `historical_price.adj_close` + `stock_split` table | wealth §1.4 |
| 7 | WS4 | `wa2607b_account_alias` | `account_alias` table | wealth §4.2 |
| 8 | WS4 | `wa2607c_vanguard_ira_types` | Vanguard IRA type/tax_sheltered data fix | wealth §10 |
| 9 | WS6 | `mca01_confirmed_by_widen_ar_vision` | `confirmed_by` widen + `ar_reminder` + `vision_promotion` | agent-features §5 |

Rules (the Order column above is illustrative; this block governs the actual merge-time chain):
- The **first** revision of each workstream chains its `down_revision` onto the **actual head
  on `main` at the moment that workstream's PR merges** (not the head when it branched);
  **subsequent revisions within the same workstream** chain onto their in-branch predecessor
  (WS4: `wa2607a` → main head, `wa2607b` → `wa2607a`, `wa2607c` → `wa2607b`). Concretely:
  WS1 merges first (down_revision = `na_iul_01`); WS2's two migrations chain off WS1's tip;
  then WS3 and WS4 run in parallel in implementation but **not** in the chain — whichever of
  WS3/WS4 merges to `main` second is responsible for rebasing its `down_revision` onto the
  other's merged tip as the last step of its own pipeline (not a follow-up task, not left to
  whoever notices); WS6 merges last (after WS5, which ships no migration) and chains onto
  WS4's tip.
- No workstream ships an unnamed revision — `alembic revision -m "<message>"` must match this
  table's Purpose column; this ledger is the source of truth for revision ids, superseding any
  informal id mentioned in a design spec before this ledger existed.
- **Merge gate** (step 3 of the per-workstream pipeline, in addition to pytest/ruff/mypy):
  `alembic heads` must print exactly one line before a workstream's PR is merged to `main`. If
  a real unplanned branch is discovered at merge time (e.g. an out-of-band hotfix migration
  landed on `main` concurrently), the merging workstream ships an explicit
  `alembic merge heads` revision as an extra, visible step — two heads are never left on `main`.
- The existing `versions/4707b428aea1_merge_index_and_decimal_heads.py` merge migration is
  precedent for exactly this failure mode; this ledger exists so the program doesn't need a
  second one.

## Gates and decisions

- Travis pre-approved: deployments, commits, tool usage, code review (2026-07-07).
- Judgment calls route through `/qdecide`; `decline` → hard stop and escalate in final report.
- Irreversible/external actions beyond the pre-approval scope (e.g., emailing a customer)
  remain human-gated — the AR chaser is draft-only by design.
- VIS promotion to primary parser: qdecide-gated per institution, never in this program's scope.

## Final phase

Fable (this session) re-reviews the entire delivery fresh against the requirements lock:
per-REQ acceptance check, cross-workstream consistency (sign conventions, dedup semantics,
message formats), deploy evidence audit, then the closeout report + scorecard to Travis.

## Non-goals

- No D1/sparkry-crm code changes (parity fixture only reads its algorithm spec).
- No Plaid balance-history backfill (impossible upstream).
- No changes to invoice/customer-facing behavior beyond the payment-link fixes.
- Fable never deployed as a runtime dependency.
