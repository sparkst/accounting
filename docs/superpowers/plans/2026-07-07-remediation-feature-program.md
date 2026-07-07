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
3. **test-gate** — `pytest && ruff check src/ && mypy src/` green (plus `npm run build` for WS4 dashboard).
4. **verify** — fresh-context verifier agent, acceptance per REQ, PASS required.
5. **deploy** — commit → push → rsync to box → restart affected units (pre-approved).
   DB-touching workstreams snapshot first: `sqlite3 data/accounting.db ".backup ..."` on box.
6. **smoke** — live validation with evidence (WS1: real sync run writes today's snapshots;
   WS2: export golden diff on prod data; WS5: DRY-RUN render vs live data; WS6: shadow diff).
7. Merge WS branch → `main`.

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
