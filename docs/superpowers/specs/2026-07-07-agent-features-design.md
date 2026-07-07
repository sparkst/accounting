# Agent Features Design — Monthly Close, AR Chaser, Vision Ingestion

**Date:** 2026-07-07 · **Branch:** `feat/remediation-and-features-2026-07` · **Status:** Draft for review
**Scope:** REQ-MCA-001..004, REQ-ARC-001..003, REQ-VIS-001..004 (`requirements/current.md`, Program 2026-07)
**Related:** REQ-FIX-ING-004 (learning loop), REQ-WBR-001 (AR aging feeds WBR), 2026-06-01 Hetzner migration design.

## 0. Runtime-LLM policy (locked)

Development happens with Claude agents; **no Anthropic model runs in production**. Any live model call
uses **Gemini by default, OpenAI as fallback**, cheap tiers, selected by env (Doppler, never `.env`).
Every core computation — reconciliation, anomaly math, reminder ladder, diff engine, promotion counters —
is deterministic and fully testable with the LLM absent or mocked. LLM output is additive narrative or
extraction-under-shadow only; it never gates a financial number. All LLM calls log to `llm_usage_log`
via the existing `estimate_cost_for_model` path (`src/models/llm_usage.py`), reusing the circuit-breaker
pattern from `src/classification/llm_classifier.py`.

| Env key | Config | Purpose |
|---|---|---|
| `GEMINI_API_KEY` (exists) | dev+srv | Gemini text + vision |
| `OPENAI_API_KEY` (new) | dev+srv | Vision fallback provider |
| `VISION_PROVIDER` (new, default `gemini`) | dev+srv | `gemini` \| `openai` |
| `CLOSE_NARRATIVE_LLM` (new, default `0`) | dev+srv | Enables REQ-MCA-004 narrative section |

## 1. REQ-MCA — Monthly close agent

### 1.1 Module layout — decision: `src/close/` package

`src/reports/close.py` was considered and rejected: close spans reconciliation, anomaly detection,
auto-confirm policy, and email rendering — a package keeps tests co-located per concern and lets the
auto-confirm helper be imported by adapters without dragging report code in.

```
src/close/
  __init__.py
  reconcile.py       # Plaid-vs-register tie-out (§1.2)          + test_reconcile.py
  anomalies.py       # new-vendor / outlier / missing-recurring   + test_anomalies.py
  autoconfirm.py     # eligibility + apply + undo (§2)            + test_autoconfirm.py
  report.py          # CloseReport dataclass assembly, evidence links
  email.py           # Resend HTML render (inline CSS, table layout — mirror invoicing/email_sender.py)
  narrative.py       # optional Gemini narrative (env-gated, §1.5) + test_narrative.py
scripts/monthly_close.py        # CLI: --month YYYY-MM (default prior month), DRY-RUN default, --apply sends
scripts/autoconfirm.py          # CLI: sweep | undo <tx-id> | digest (§2.3–2.5)
```

### 1.2 Reconciliation summary algorithm (deterministic)

Scope = prior calendar month `[first, last]` unless `--month` given. Per active `PlaidItem` (skip
`status=disconnected` per REQ-FIX-PLD-004), per mapped account (keyed by `payment_method` label —
the register has no account FK, per CLAUDE.md):

1. **Sync coverage:** distinct success days in `ingestion_log` (`source='plaid'` rows) within the month;
   list gap days. Gaps > 0 ⇒ ⚠️ flag.
2. **Register aggregate:** `COUNT(*)`, `SUM(amount)` of register rows with `source='plaid'` and that
   `payment_method`, excluding `status IN (rejected, split_parent)` (children counted, parents not —
   mirrors REQ-FIX-API-002).
3. **Balance tie-out (depository accounts):** `Δ = snapshot(last) − snapshot(first_prior)` from
   `plaid_account_balance_snapshot` vs register `SUM(amount)` for the month. `|Δ − Σ| > $0.01` ⇒
   discrepancy line with both numbers (credit-card accounts report flows only — no tie-out, stated in report).
4. **Unmatched listing:** (a) `pending` Plaid rows older than 7 days (stuck pending→posted reconcile);
   (b) `needs_review` backlog count + oldest date, per entity; (c) Stripe/Shopify payout transfers in-month
   with no matching bank deposit (existing reconciliation-pair query in `src/utils/`); (d) unmapped-account
   names from `ingestion_log` detail (REQ-FIX-PLD-005 output).

All amounts `Decimal(str(...))` at every boundary; sums quantized to cents before comparison.

### 1.3 Anomaly scan (deterministic)

Vendor key = `description` lower-cased, whitespace-collapsed, digits/reference-suffixes stripped
(pure function `normalize_vendor()`, exhaustively tested).

- **New vendors:** vendor keys first seen in the close month (no earlier register row, no matching
  `VendorRule` via `lookup_vendor_rule`) with `abs(amount) ≥ $25`. Listed with count + total.
- **Amount outliers (z-score per vendor):** for vendors with ≥ 5 historical rows in the prior 12 months,
  flag close-month rows where `|amount − μ| / σ ≥ 3` **and** `|amount − μ| ≥ $50` (σ floor $1 to avoid
  div-by-zero on constant subscriptions). μ/σ computed on `abs(amount)` with Decimal→float only inside
  the statistic, never persisted.
- **Missing expected recurring — source (decision): derived from history + config overrides.**
  A vendor is "expected recurring" when it has ≥ 3 charges in the prior 6 months whose median interval
  is 25–35 days (monthly cadence). If no charge lands in the close month, it is flagged with last-seen
  date and typical amount. `config/close_recurring.yaml` (checked in) supports `ignore:` (suppress a
  vendor) and `require:` (force-track a vendor with `expected_day`, `amount_hint`) — no new table;
  history stays the source of truth, config only overrides.

### 1.4 Close report email + timer

One email (Resend — email channel per the delivery-split decision), sections in order: header KPIs
(rows ingested, auto-confirmed, needs-review depth), reconciliation table (§1.2), anomalies (§1.3),
auto-confirm month summary (§2), data-hygiene callouts (e.g. the $50 Fidelity TOD per REQ-FIX-DAT-002).
Tight layout: one line per finding, each with an **evidence link** to the dashboard:
`https://books.sparkry.ai/?status=needs_review&entity=<e>`, `/transactions?vendor=<key>&month=<m>`,
`/wealth/accounts/<id>`. Sender uses the single `sparkry.ai` constant (REQ-FIX-API-004).
Recipient: `ALERT_TO_EMAIL`.

Systemd (deploy/, cloned from `accounting-balance-alerts.*` hardening): `accounting-monthly-close.timer`
`OnCalendar=*-*-01 15:00:00 UTC`, `Persistent=true`, `OnFailure=accounting-alert@%p.service`;
service runs `doppler run -- env -u DOPPLER_TOKEN … python -m scripts.monthly_close --apply`.
Ship with the timer installed but the unit initially run DRY-RUN by hand; flip `--apply` after one clean cycle.

### 1.5 Narrative (REQ-MCA-004)

`narrative.py` renders an optional 5-sentence summary from the already-computed `CloseReport` dataclass
(numbers only, no raw transactions in the prompt). Gemini `gemini-2.5-flash-lite`, `temperature=0`,
reusing the `_CircuitState` breaker and `_write_usage_log` pattern. Gated on `CLOSE_NARRATIVE_LLM=1`;
any failure ⇒ section omitted, report still sends. Never alters deterministic sections.

## 2. REQ-MCA-002/003 — Auto-confirm

### 2.1 Rule (exact)

A transaction is auto-confirmed iff **all** hold:
`result.tier_used == 1` · matched `VendorRule.confidence ≥ 0.90` · `result.status == AUTO_CLASSIFIED`
(sign-reconciliation veto or any needs_review routing disqualifies) · `tx.amount is not None` ·
`tx.parent_id is None` · `tx.status == auto_classified` · entity, tax_category, direction all set.
Tier-2/3 results **never** auto-confirm regardless of confidence.

Effect: `status=confirmed`, `confirmed_by=f"auto:rule:{rule.id}"`, `AuditEvent` rows for `status` and
`confirmed_by` with `changed_by=f"auto:rule:{rule.id}"` (fits `AuditEvent.changed_by` String(64)).

### 2.2 Plumbing changes

- `ClassificationResult` gains `rule_id: str | None = None`; `rules.lookup_vendor_rule` populates it
  from the winning rule. (Additive dataclass field — no call-site breakage.)
- `Transaction.confirmed_by` widens `String(8) → String(64)` (additive Alembic; SQLite doesn't enforce
  length but the model/D1 port must be honest). `ConfirmedBy` enum untouched — the column already holds
  free strings in entity-mode conventions.
- New `src/close/autoconfirm.py: auto_confirm_if_eligible(session, tx, result) -> bool`, called at the
  ingestion call sites after classification is applied: `plaid_transactions.make_transaction` (~line 199,
  after status resolution) and `api/routes/ingest.py` (~line 212, after `apply_result`). Not inside
  `apply_result` itself — that function is deliberately session-free.

### 2.3 No self-reinforcement (explicit, interacts with REQ-FIX-ING-004)

**Auto-confirm never touches `vendor_rules`.** `auto_confirm_if_eligible` does not call
`_upsert_vendor_rule`, does not bump `examples`, does not raise `confidence`. Only human confirms —
the `PATCH /transactions/{id}` flow (`transactions.py` ~925) — adjust rules, including the
REQ-FIX-ING-004 corrected-learning behavior (divergent human correction resets rule confidence to base,
which can drop a rule below 0.90 and thereby switch off auto-confirm for that vendor: the intended
brake). A human confirming an already-auto-confirmed row (confirmed_by starts `auto:rule:`) upgrades
`confirmed_by` to `human` and runs the normal learning loop. Test asserts a 100-cycle
auto-confirm loop leaves the rule row byte-identical.

### 2.4 Backlog sweep + weekly digest

`python -m scripts.autoconfirm sweep [--apply] [--limit N]` — iterates `status=auto_classified` rows
(~250 today), re-runs `lookup_vendor_rule` against current rules, applies §2.1. DRY-RUN default prints
a per-row table (id, date, vendor, amount, rule id, rule confidence) and a summary; `--apply` writes with
per-row `begin_nested()` isolation. Supersedes and deletes `scripts/auto-confirm-high-confidence.py`
(which confirmed on transaction confidence, not rule confidence — wrong per the locked decision).

`python -m scripts.autoconfirm digest [--apply]` — emails (Resend) all transactions with
`confirmed_by LIKE 'auto:rule:%'` confirmed in the trailing 7 days (via AuditEvent `changed_at`),
grouped by vendor with counts/totals and per-row undo command lines. Timer
`accounting-autoconfirm-digest.timer` Mon 14:10 UTC, same hardening/OnFailure as §1.4.

### 2.5 Undo

`python -m scripts.autoconfirm undo <transaction-id> [--apply]` — guards: row exists, `confirmed_by`
starts `auto:rule:`, tax year not locked. Reverts `status → needs_review` (resurfaces in the review
queue rather than silently back to auto_classified), `review_reason="auto-confirm undone by operator"`,
`confirmed_by → auto`, AuditEvents for all three fields with `changed_by=human`. Never deletes anything;
never touches the rule.

## 3. REQ-ARC — AR chaser (draft-for-approval)

### 3.1 State — decision: new table `ar_reminder` (additive Alembic)

`alert_dispatch` was considered and rejected: it is a fire-and-forget dedup ledger (UNIQUE key+date,
terminal statuses `sent|failed|dry_run`) with nowhere to hold a draft body, an approval state machine,
or an invoice FK. Repurposing it would overload EA-alert semantics that REQ-FIX-ALR-002 is about to
build retry logic on. New table:

```
ar_reminder: id (uuid PK) · invoice_id (FK invoices.id, indexed) · rung (int: 14|30|45)
  · status: drafted → pending_approval → approved → sent | dismissed | failed (retryable → pending_approval)
  · draft_subject, draft_body (Text) · approval_token (uuid, single-use) · approved_via (telegram|cli, nullable)
  · resend_message_id (nullable) · created_at, sent_at, updated_at
  · UNIQUE(invoice_id, rung)   ← exactly-once per invoice per rung
```

### 3.2 Ladder + drafts (no LLM, v1)

Daily job (`scripts/ar_chaser.py run`, DRY-RUN default; timer `accounting-ar-chaser.timer` 14:15 UTC):
for invoices with `status IN (sent, overdue)` and `paid_date IS NULL`, compute `days = today − sent_at`.
For each rung `r ∈ {14, 30, 45}` with `days ≥ r` and no `ar_reminder(invoice_id, r)` row, insert one
`drafted` row. Draft generation is template-based (`src/ar/templates.py`, three escalating tones:
friendly nudge / firm reminder / final notice referencing `late_fee_pct` when set), rendered with the
invoice/customer/line-item context reusing `_format_currency`/`_validate_email` from
`src/invoicing/email_sender.py`. Only the highest unsent rung is drafted per run (an invoice discovered
at day 46 gets one 45-day draft, not three). A paid/void invoice dismisses all open drafts.

### 3.3 Approval flow

**Primary (Telegram, via existing n8n `WH-Telegram / Bot Callback Router`):** on draft creation the job
POSTs `{type:"info", title:"AR reminder draft", message:<preview>, alert_key:"ar:<invoice>:<rung>",
callback:{approve_url, dismiss_url, token}}` through the severity-webhook client pattern
(`src/balance_alerts/webhook.py` — HTTPS-only, secret header, static error strings). n8n renders a
Telegram inline keyboard [Approve] [Dismiss]; the callback router POSTs back to
`POST /api/ar/reminders/{id}/approve|dismiss` with header `X-Webhook-Secret: $N8N_ALERTS_WEBHOOK_SECRET`
**and** body `{"token": <approval_token>}`. Edge auth: a new scoped Cloudflare Access service token
`books-ar-approve` limited to `/api/ar/*` (same pattern as `books-ingest`). The endpoint verifies
secret + single-use token + `status=pending_approval`, then sends via Resend, records
`resend_message_id`, `sent_at`, `status=sent`.

**Fallback (CLI):** `python -m scripts.ar_chaser approve <id>` / `dismiss <id>` / `list` — same
transition function, `approved_via=cli`, no token needed (local operator).

**Exactly-once:** the UNIQUE constraint blocks duplicate rungs; the `pending_approval→approved` transition
is guarded by a single UPDATE … WHERE status='pending_approval' (rowcount check) so a double-tap on
Telegram cannot double-send. Nothing ever sends without an explicit approval (REQ-ARC-001).

**Audit:** every transition writes an entity-mode `AuditEvent` (`entity_type="ar_reminder"`,
`entity_id=<reminder id>`, field `status`) — the open-string entity_type design in
`src/models/audit_event.py` permits this without migration. AR aging buckets
(current/14/30/45+) computed by the same module and exported for the WBR (REQ-ARC-003).

## 4. REQ-VIS — Vision statement ingestion (shadow mode)

### 4.1 Provider abstraction — `src/vision/`

```
src/vision/
  client.py     # VisionProvider protocol: extract(file_bytes, mime, schema, prompt) -> VisionExtraction
                # GeminiVisionProvider (default, gemini-2.5-flash, native PDF input)
                # OpenAIVisionProvider (fallback, gpt-4o-mini, PDF pages → images)
                # select_provider() reads VISION_PROVIDER; per-provider circuit breaker (clone of
                # llm_classifier._CircuitState); usage row → llm_usage_log via estimate_cost_for_model
                # (add gpt-4o-mini pricing prefix to _PRICING).
  schemas.py    # JSON Schemas per statement type (below) — passed as structured-output config
  extract.py    # institution → (statement_type, prompt, schema) registry for the 5 legacy adapters:
                # fg_pdf, gsk_pdf, nw_mutual_xlsx, ft_pdf, north_american_iul
  shadow.py     # shadow-run harness (§4.2)                     + test_shadow.py
  promote.py    # promotion ledger (§4.3)                        + test_promote.py
  diff.py       # pure field-level diff engine                   + test_diff.py
```

Statement-type schemas (all monetary fields strings, converted `Decimal(str(v)).quantize(cents)` at the
boundary; date fields ISO): **balances** `{institution, account_number_mask, as_of, balance}`;
**positions** `{account, as_of, positions:[{symbol, quantity, price, value}]}`;
**policy_values** `{policy_number, as_of, cash_value, surrender_value, death_benefit, premium_paid}`.
Extraction failures per file are caught, logged, and never halt a directory batch (per-file isolation,
mirroring the adapters' per-record rule).

### 4.2 Shadow harness (REQ-VIS-002)

`python -m src.vision.shadow run --institution <fg|gsk|nw_mutual|ft|na_iul> --file <path> [--provider …]`
1. Runs the legacy adapter with `dry_run=True`, capturing its would-write rows (existing `ImportResult`).
2. Runs vision extraction on the same file.
3. `diff.py` produces a field-level report: per field `match | mismatch(legacy=…, vision=…) |
   vision_only | legacy_only`; Decimals compared post-quantization, dates exactly.
4. Writes the full diff JSON (including the raw provider response for `raw_data`-style provenance,
   REQ-VIS-004) to `data/vision-shadow/<institution>/<ts>-<file-hash>.json` (gitignored) and one
   `IngestionLog` row `source="vision_shadow"` with `records_processed/records_failed` and the summary
   line (`n match / n mismatch / clean=bool / provider / cost`) in `error_detail`.
5. **Never writes register, brokerage, or history tables — enforced by construction (shadow.py opens no
   write path to them) and by test.**

### 4.3 Promotion ledger (REQ-VIS-003)

New table `vision_promotion` (additive): `institution (PK) · consecutive_clean (int) ·
last_cycle_at · last_report_path · promoted (bool, default false) · promoted_at · decision_ref (Text)`.
A shadow run that is **equal-or-better** (zero mismatches; `vision_only` extras allowed,
`legacy_only` misses not) increments `consecutive_clean`; any dirty run resets it to 0. At 3, the CLI
prints "eligible for promotion — run qdecide"; the flip is a manual `python -m src.vision.promote
<institution> --decision-ref <qdecide id>` which sets `promoted=true` and writes an entity-mode
AuditEvent (`entity_type="vision_promotion"`). Post-promotion the vision path becomes primary for that
institution's importer with the legacy parser retained as automatic fallback on extraction failure;
demotion is the same command with `--revoke`. (Wiring vision-as-primary into each importer ships with
the promotion of the first institution, behind the flag — shadow tooling itself carries no write risk.)

### 4.4 Security & cost

API keys only via Doppler (`GEMINI_API_KEY`, `OPENAI_API_KEY`); statement bytes go only to the two
configured providers, nowhere else (no intermediate upload services); every call logs a `llm_usage_log`
row (model, tokens, `cost_estimate`, duration) and shadow reports embed per-run cost. Raw provider JSON
preserved in the diff report file (and in `raw_data` when vision becomes primary post-promotion).

## 5. Schema changes (one additive Alembic migration, real downgrade)

1. `transactions.confirmed_by` `String(8) → String(64)` (widen only; comment updated to
   `auto | human | auto:rule:<id>`). No CHECK change.
2. New table `ar_reminder` (§3.1) + index on `invoice_id`, UNIQUE `(invoice_id, rung)`.
3. New table `vision_promotion` (§4.3).

No protected table dropped or deleted from; downgrade drops the two new tables and narrows the column.
Validated with the `alembic-migration` skill + `migration-reviewer` before commit.

## 6. Test strategy (fixtures + fake LLMs, fully deterministic)

- **Reconcile:** in-memory SQLite seeded with a fixture month (plaid rows, snapshots, ingestion_log);
  golden expected `CloseReport`; cases: gap day, $0.01-tolerance pass, discrepancy, stuck pending,
  credit-card no-tie-out. REQ-MCA-001.
- **Anomalies:** fixture register with known vendors; tests for `normalize_vendor`, z-score boundary
  (exactly 3σ / $50), σ-floor, recurring-cadence detection (25/35-day edges), yaml ignore/require.
  REQ-MCA-001.
- **Auto-confirm:** each §2.1 conjunct falsified individually (tier-2 match, 0.89 rule, veto’d result,
  null amount, split child); rule-row-unchanged invariant (§2.3); sweep DRY-RUN writes nothing; undo
  round-trip with AuditEvent assertions; PATCH-confirm on auto-confirmed row runs learning loop.
  REQ-MCA-002/003 + REQ-FIX-ING-004 interaction.
- **Narrative:** injected fake genai client (existing `_client` pattern); breaker-open ⇒ report sends
  without section; prompt contains aggregates only. REQ-MCA-004.
- **AR chaser:** frozen-clock ladder tests (13/14/44/46 days), one-rung-per-run, UNIQUE violation ⇒
  skip not crash, paid-invoice dismissal, single-use token, double-approve race (UPDATE rowcount),
  webhook client mocked httpx, CLI approve path, audit rows per transition. REQ-ARC-001..003.
- **Vision:** recorded fake provider responses as JSON fixtures per institution (no network);
  diff-engine truth table; Decimal quantization at boundary (`10.5` vs `10.50` matches); per-file
  isolation (poisoned file in batch); "never writes register" test (row counts before/after);
  promotion counter reset/increment/flip + revoke. REQ-VIS-001..004.
- Gates: `pytest && ruff check src/ && mypy src/`; every test docstring carries its REQ-ID (req-trace).

## 7. REQ traceability

| REQ | Design section | Primary code |
|---|---|---|
| REQ-MCA-001 | §1.1–1.4 | `src/close/{reconcile,anomalies,report,email}.py`, `scripts/monthly_close.py`, timer |
| REQ-MCA-002 | §2.1–2.4 | `src/close/autoconfirm.py`, `ClassificationResult.rule_id`, call sites, sweep CLI |
| REQ-MCA-003 | §2.4–2.5 | `scripts/autoconfirm.py digest|undo`, digest timer |
| REQ-MCA-004 | §1.5 | `src/close/narrative.py`, `CLOSE_NARRATIVE_LLM` |
| REQ-ARC-001 | §3.1–3.2 | `ar_reminder` table, `scripts/ar_chaser.py`, `src/ar/templates.py` |
| REQ-ARC-002 | §3.3 | `/api/ar/reminders/{id}/approve`, n8n callback, CLI fallback |
| REQ-ARC-003 | §3.2–3.3 | aging buckets → WBR, entity-mode AuditEvents |
| REQ-VIS-001 | §4.1 | `src/vision/{client,schemas,extract}.py` |
| REQ-VIS-002 | §4.2 | `src/vision/{shadow,diff}.py`, IngestionLog `vision_shadow` |
| REQ-VIS-003 | §4.3 | `vision_promotion` table, `src/vision/promote.py`, qdecide gate |
| REQ-VIS-004 | §4.2, §4.4 | raw JSON in diff report/`raw_data`, Doppler keys, `llm_usage_log` |

## 8. Out of scope

WBR scorecard body (REQ-WBR-*), tax forecaster (REQ-TXF-*), delivery-health pulse (REQ-DHL-*),
vision-as-primary importer wiring beyond the promotion flag (ships with first promotion), and any
Cloudflare/D1 port of these features.
