# Program 2026-07 — Spec-Set Review Index (review artifact)

This artifact is the complete design package for the 2026-07 remediation + feature
program. Reviewing this artifact means reading and reviewing ALL of the following
files as one body of work (paths relative to repo root `/Users/travis/dev2/accounting`):

1. `requirements/current.md` — ONLY the section starting at heading
   "# Program 2026-07: Remediation + Feature Program (RFP)" (83 REQ-FIX-*/feature REQs: 54 fixes + 29 features).
2. `docs/superpowers/specs/2026-07-07-plaid-alert-reliability-design.md`
3. `docs/superpowers/specs/2026-07-07-tax-invoicing-correctness-design.md`
4. `docs/superpowers/specs/2026-07-07-ingestion-classification-design.md`
5. `docs/superpowers/specs/2026-07-07-wealth-analytics-design.md`
6. `docs/superpowers/specs/2026-07-07-reporting-suite-design.md`
7. `docs/superpowers/specs/2026-07-07-agent-features-design.md`
8. `docs/superpowers/plans/2026-07-07-remediation-feature-program.md` — execution plan.

REQ-family → owning spec (cross-references from other specs are consumers, not owners):

| REQ family | Owning spec |
|---|---|
| REQ-FIX-PLD-*, REQ-FIX-ALR-*, REQ-DHL-* | plaid-alert-reliability |
| REQ-FIX-TAX-*, REQ-FIX-INV-*, REQ-FIX-API-* | tax-invoicing-correctness |
| REQ-FIX-ING-* | ingestion-classification |
| REQ-FIX-WLT-*, REQ-FIX-DAT-*, REQ-IPD-*, REQ-NWA-*, REQ-BBT-* | wealth-analytics |
| REQ-WBR-*, REQ-TXF-*, REQ-SEL-* | reporting-suite |
| REQ-MCA-*, REQ-ARC-*, REQ-VIS-* | agent-features |
| REQ-FIX-N8N-* | execution plan WS7 (design-light by decision — see scope note) |

**Scope note — REQ-FIX-N8N-001/002 are deliberately design-light and out of this package's
acceptance-testable-design review:** these two REQs (`requirements/current.md:665-666`) are
n8n workflow hygiene tasks (tags, purpose stickies, timezone, `errorWorkflow` wiring, naming-taxonomy
prefix fix, and extracted local vitest coverage for the alert-path code nodes). They are NOT
covered by any of the six design specs above — the design package intentionally has no n8n
spec — and are instead specified directly in the execution plan as **WS7 n8n hygiene**
(`docs/superpowers/plans/2026-07-07-remediation-feature-program.md`, workstream table row
"WS7 n8n hygiene"), delegated to the `n8n-workflow-engineering` skill at implementation time
rather than to a written design doc. This is an explicit scoping decision, not an omission:
reviewers should not expect to find N8N-001/002 acceptance criteria in items 2-7 above, and
should instead check WS7's plan row + the n8n-workflow-engineering skill's own conventions
when those REQs come up for review/verification. Every other REQ in the Program 2026-07
section is owned by exactly one of items 2-7 per the Requirements coverage lens below.

Review lenses (all findings must cite file + section):
- **Financial correctness**: sign conventions (expenses negative), Decimal discipline,
  cash-basis semantics, tax-form line mappings, B&O gross-receipts rules, dedup/idempotency
  semantics, reconciliation-vs-dedup distinction. Verify designs against the repo's
  CLAUDE.md critical rules and actual code where a design asserts code behavior.
- **Security**: secrets handling (Doppler only), webhook auth, new endpoints/callback
  surfaces (AR approval flow, n8n callbacks), PII flowing to LLM providers, Cloudflare
  Access token scoping.
- **Requirements coverage**: every REQ in the Program section is owned by exactly one spec
  with acceptance-testable design; cross-references from other specs permitted (consuming specs
  cite the owning spec's contract). Every spec section traces to a REQ; no orphans. Traceability
  table marks each REQ's owning spec.
- **Internal consistency**: cross-spec contracts agree (alert_dispatch schema changes used
  by multiple specs; pl_engine consumed by reporting; ING-004 learning-loop fix vs MCA
  auto-confirm no-self-reinforcement rule; confirmed_by width; migration collision/ordering
  across specs — several specs each propose Alembic migrations; systemd unit conventions;
  config file proliferation — flag conflicts and ordering).

Fixes should be applied to the underlying files listed above, not to this index.
