# Wealth → Cloudflare Migration — Execution Runbook

**Companion to:** `docs/superpowers/specs/2026-05-10-wealth-cloudflare-migration.md`
**Designed for:** a fresh-context `/qpipeline thorough` session.
**Workspaces:** local accounting repo (`/Users/travis/SGDrive/dev/accounting`) and CRM repo (`/Users/travis/SGDrive/dev/sparkry-crm`). Parallel work happens in git worktrees off each repo.

---

## 0. Bootstrap (fresh-context session begins here)

A fresh `/qpipeline thorough` session reads:

1. `docs/superpowers/specs/2026-05-10-wealth-cloudflare-migration.md` — the spec (architectural decisions, requirements, anti-hallucination protocol)
2. This runbook — the task graph
3. The recent Plaid Phase 1 spec at `docs/superpowers/specs/2026-05-09-plaid-net-worth-integration.md` — for behavioral reference when porting
4. `/Users/travis/SGDrive/dev/sparkry-crm/CLAUDE.md` — the CRM repo's conventions
5. `requirements/current.md` — pulls in REQ-WC-001 through REQ-WC-019 after they're appended (Task M0)

The session boots with:

```bash
PIPELINE_DRIVER=/Users/travis/.claude/plugins/cache/sparkry-claude-skills/ai-review-toolkit/1.0.0/tools/pipeline-driver.py
python "$PIPELINE_DRIVER" init \
  --preset thorough \
  --artifact docs/superpowers/specs/2026-05-10-wealth-cloudflare-migration.md \
  --requirements requirements/current.md \
  --max-rounds 5
```

Then follows the standard /qpipeline gates, but the `execute` phase runs the parallel task graph from Section 3 of this runbook rather than a flat task list.

---

## 1. Sub-team decision protocol (replaces user gates during execute)

The user's directive: "If you need a decision from me ask a team of agents acting as me and a skeptic and PE and strategic advisor until you get consensus."

The fresh-context session MUST use this pattern instead of stopping for a user answer. For any decision encountered during execution that is not explicitly settled in the spec:

1. Spawn 4 sub-agents in parallel:
   - **TRAVIS-PERSONA**: prioritizes simplicity, single-user pragmatism, avoiding rabbit holes.
   - **SKEPTIC**: hunts for hidden assumptions, looks for failure modes, demands evidence.
   - **PE (principal engineer)**: enforces patterns from CLAUDE.md, demands invariants are preserved, refuses shortcuts.
   - **STRATEGIC-ADVISOR**: weighs reversibility, time-to-value, optionality.
2. Each receives the same question + the same context. Each returns a recommendation + rationale.
3. The orchestrator synthesizes: if 4/4 agree → proceed; if 3/4 agree → proceed with the dissenter's concern documented; if 2/2 split → re-run with the explicit tiebreaker question "what evidence would change your mind?" until one side concedes; if still split → escalate to user with the divergence.

This is the rule. Do not ask the user a question that the sub-team could answer. The user explicitly does not want gate prompts during execution.

---

## 2. Worktree decomposition

Two source repos, seven worktrees, dependency order documented per worktree. All work merges back to the respective repo's main branch via PR.

### Local accounting repo (`/Users/travis/SGDrive/dev/accounting`)

Worktree creation:
```bash
cd /Users/travis/SGDrive/dev/accounting
git worktree add ../accounting-wt-local-migration feat/wealth-migration-local
git worktree add ../accounting-wt-importer-cloud feat/wealth-importer-cloud-mode
```

| Worktree | Branch | Scope |
|---|---|---|
| **acct/local-migration** | `feat/wealth-migration-local` | AuditEvent CHECK rollback migration (relax back to Transaction-mode only); remove brokerage_router + plaid_router from main.py; remove `dashboard/src/routes/brokerage/*` and `dashboard/src/routes/admin/*`; `git rm com.sparkry.plaid-balance-sync.plist` from the REPO copy (build-phase removal). The `~/Library/LaunchAgents/` copy is removed separately by LM-T05 post-cutover — the repo copy and the LaunchAgents copy are distinct. Remove `_check_plaid_stale_items` from `scripts/weekly-pl-report.py` (REQ-WC-007; see LM-T04). Document the local-side cleanup in CLAUDE.md. |
| **acct/importer-cloud** | `feat/wealth-importer-cloud-mode` | Add `--target {local|cloud}` flag to: `src/adapters/brokerage_csv.py`, `xlsx_savings_plan.py`, `vanguard_csv.py`, `fg_pdf.py`, `nw_mutual_xlsx.py`, `gsk_pdf.py`, `ft_pdf.py`; cloud target POSTs to `https://internal.sparkry.ai/wealth/api/internal/ingest/*` with `X-Internal-Key` from Doppler; tests for each adapter cover both modes. |

### CRM repo (`/Users/travis/SGDrive/dev/sparkry-crm`)

Worktree creation:
```bash
cd /Users/travis/SGDrive/dev/sparkry-crm
git worktree add ../sparkry-crm-wt-d1-schema feat/wealth-d1-schema
git worktree add ../sparkry-crm-wt-workers-plaid feat/wealth-workers-plaid
git worktree add ../sparkry-crm-wt-workers-brokerage feat/wealth-workers-brokerage
git worktree add ../sparkry-crm-wt-frontend-brokerage feat/wealth-frontend-brokerage
git worktree add ../sparkry-crm-wt-frontend-desk feat/wealth-frontend-desk
```

| Worktree | Branch | Scope |
|---|---|---|
| **crm/d1-schema** | `feat/wealth-d1-schema` | Drizzle schema for **13 tables**: account, brokerage_transaction, position_snapshot, realized_gain_loss, historical_price, account_balance_snapshot, expected_account, cost_basis_lot, account_tag, plaid_item, plaid_account_balance_snapshot, audit_events (entity-mode only), **ingestion_log**; migration files; `scripts/migrate-from-sqlite.ts` (one-shot dump-load with Fernet→AES-GCM re-encryption); `scripts/rollback-from-d1.ts`; **`scripts/setup-staging-d1.ts`** to create `sparkry-crm-staging` D1. Owns ALL `worker.ts` cron registrations (sole writer to that file across worktrees). **Blocks all other crm worktrees.** |
| **crm/workers-plaid** | `feat/wealth-workers-plaid` | Workers handlers under `src/routes/(wealth)/wealth/desk/api/plaid/*`; Plaid SDK wrapper; AES-GCM crypto helpers; **exports** `handlePlaidSync(env, ctx)` from `src/lib/server/wealth/plaid-balance-sync.ts` AND `handlePlaidStaleAlert(env, ctx)` from `src/lib/server/wealth/plaid-stale-alert.ts` (separate module). The workers-plaid PR also includes the matching edits to `src/worker.ts` that replace the d1-schema-authored stubs with real imports for these two handlers — `src/worker.ts` is "owned" by d1-schema initially (which writes the stubs at D1-T06) but transferred to workers-plaid at merge time for the Plaid-handler lines. Depends on crm/d1-schema. |
| **crm/workers-brokerage** | `feat/wealth-workers-brokerage` | Workers handlers under `src/routes/(wealth)/wealth/api/brokerage/*` (all 13 routes incl. top-holdings/recent-transactions/data-integrity); reconciliation summary; internal-ingest endpoints (`/wealth/api/internal/ingest/*`) with `X-Internal-Key` auth; **exports** `handleTwelveDataIngest(env, ctx)` and `handleR2Backup(env, ctx)` from `src/lib/server/wealth/`. The workers-brokerage PR also includes the matching edits to `worker.ts` that replace the d1-schema-authored stubs for these two handlers. Depends on crm/d1-schema. |
| **crm/frontend-brokerage** | `feat/wealth-frontend-brokerage` | `src/routes/(wealth)/wealth/+page.svelte` (Wealth dashboard); `(wealth)/wealth/networth`, `holdings`, `accounts`, `accounts/[id]`, `missing-accounts`; ESLint `no-restricted-paths` rule isolating `(crm)` and `(wealth)`; Svelte 5 runes enforced via custom ESLint rule (`no-svelte4-syntax`). Depends on crm/workers-brokerage. |
| **crm/frontend-desk** | `feat/wealth-frontend-desk` | `src/routes/(wealth)/wealth/desk/+page.svelte`, `desk/connections`, `desk/connections/oauth-return`, `desk/reconciliation`, `desk/import`; Plaid Link integration; OAuth-return postMessage handler. Depends on crm/workers-plaid. |

---

## 3. Task graph

Tasks are grouped by worktree. Within a worktree, tasks run sequentially (because they share file state). Across worktrees, tasks run in parallel as soon as their dependencies are satisfied.

The fresh `/qpipeline` orchestrator spawns one **team-lead sub-agent per worktree**. Each team-lead is responsible for: TDD execution, internal /qreview to convergence on its scope, anti-hallucination validation review, merge back to its branch, opening a PR.

### Task M0 — Requirements append + secrets + golden output (orchestrator, ~30 min)

M0 is a bundle of orchestrator-only prerequisites that run BEFORE any team-lead spawns. Sequential:

**M0-pre — Sentinel smoke-test.** Before any other M0 work, the orchestrator proves it can execute the HARD HALT mechanism (Section 7). It emits "REPLY: SMOKE-TEST PHRASE" as the only output for that turn and waits for the user. When the user replies, the orchestrator verifies the exact-match behavior (anything other than "SMOKE-TEST PHRASE" → restate; exact match → proceed). This is one user interaction. Skipping this smoke-test is forbidden — the cutover gate at Section 7 depends on the same mechanism working.

**M0-doppler-context — Doppler project/config disambiguation.** All M0 doppler commands use explicit `--project` and `--config` flags to avoid ambiguity:

| Context | Doppler project | Doppler config |
|---|---|---|
| Local Python importers (accounting repo) | `accounting` | `dev` (or `prd` for production secrets) |
| sparkry-crm TypeScript scripts (migration-only secrets + CRM off-Cloudflare backup vault) | `accounting` | `prd` |

**Convention reconciliation note.** The original runbook draft assumed a dedicated `sparkry-crm` Doppler project. After M0a discovery (M0a sub-team consensus 2026-05-11), this was reconciled with the existing convention in `sparkry-crm/CLAUDE.md` which already designates `Doppler project accounting, config prd` as the off-Cloudflare backup vault for CRM secrets. Migration-only secrets (`PLAID_TOKEN_ENC_KEY_MIGRATION`) live in `accounting/prd` alongside CRM secrets like `RESEND_WEBHOOK_SECRET`. Names already disambiguate (`PLAID_FERNET_KEY` in `accounting/dev` vs `PLAID_TOKEN_ENC_KEY_MIGRATION` in `accounting/prd`); cross-config separation is a secondary defense.

Every doppler command in M0 uses `doppler secrets ... --project <project> --config <config>` explicitly. Do not rely on the currently-active doppler context.

**M0-prereqs — Verify tooling.** Before any M0 step that runs TypeScript scripts in the sparkry-crm repo: `cd /Users/travis/SGDrive/dev/sparkry-crm && npm install && npx tsx --version` — must succeed. Confirm `tsx` is pinned in `devDependencies` in `package.json` (e.g., `"tsx": "^4.0.0"`). Run a dry-run of `migrate-from-sqlite.ts` against a fixture SQLite file before opening the cutover window to confirm the compile and runtime paths resolve.

**M0a — Requirements append + sparkry-crm Doppler config verification.** In `accounting` main: append REQ-WC-001..019 from the spec into `requirements/current.md`. Single commit. **Reconciled (M0a sub-team 2026-05-11):** the CRM convention in `sparkry-crm/CLAUDE.md` designates `Doppler project accounting, config prd` as the off-Cloudflare backup vault — no dedicated `sparkry-crm` Doppler project exists or needs to be created. The M0-doppler-context table above is updated accordingly; all subsequent M0c/D1-T03/cutover/rollback commands use `--project accounting --config prd` for migration-only secrets.

**M0b — Generate WEALTH_INTERNAL_KEY + Doppler companions.** Use the stdin pipe pattern (no temp files):
```bash
KEY=$(openssl rand -base64 32 | tr -d '\n')
[ ${#KEY} -eq 44 ] || { echo "Key length mismatch — base64 encoding failed"; exit 1 }
printf '%s' "$KEY" | wrangler pages secret put WEALTH_INTERNAL_KEY --project-name sparkry-crm
printf '%s' "$KEY" | doppler secrets set WEALTH_INTERNAL_KEY --project accounting --config dev
doppler secrets set WEALTH_API_BASE=https://internal.sparkry.ai --project accounting --config dev
doppler secrets set WEALTH_TARGET_DEFAULT=local --project accounting --config dev
unset KEY
```
The `local` default keeps importers writing to SQLite until cutover step 7i flips it to `cloud`.
- Idempotency guard: `if wrangler pages secret list --project-name sparkry-crm | grep -q WEALTH_INTERNAL_KEY; then echo "M0b already complete; skipping"; exit 0; fi` — run this before the block above.
- Verify Workers side: `wrangler pages secret list --project-name sparkry-crm | grep WEALTH_INTERNAL_KEY`.
- Verify Doppler side: `doppler secrets --only-names --project accounting --config dev | grep ^WEALTH_` — expect 3 entries.

**M0c — Generate new AES-GCM key + rename old Fernet key for disambiguation.**

1. Generate and provision the new AES-GCM key (URL-safe base64, stdin pipe, no temp files):
```bash
TEK=$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n')
[ ${#TEK} -eq 44 ] || { echo "Key length mismatch — base64 encoding failed"; exit 1 }
printf '%s' "$TEK" | wrangler pages secret put PLAID_TOKEN_ENC_KEY --project-name sparkry-crm
printf '%s' "$TEK" | doppler secrets set PLAID_TOKEN_ENC_KEY_MIGRATION --project accounting --config prd
unset TEK
```
2. Verify the mirror is present before proceeding: `doppler secrets --only-names --project accounting --config prd | grep PLAID_TOKEN_ENC_KEY_MIGRATION`.
3. **Rename the existing Fernet key in Doppler (accounting config)** to avoid same-name ambiguity. Order is critical to eliminate the no-key gap window: (a) read the old key into an env var; (b) set the new name first; (c) verify it is present; (d) THEN unset the old name:
```bash
OLD=$(doppler secrets get PLAID_TOKEN_ENC_KEY --plain --project accounting --config dev)
printf '%s' "$OLD" | doppler secrets set PLAID_FERNET_KEY --project accounting --config dev
doppler secrets --only-names --project accounting --config dev | grep PLAID_FERNET_KEY   # verify present before unsetting old
doppler secrets unset PLAID_TOKEN_ENC_KEY --project accounting --config dev
unset OLD
```
4. Verify: `doppler secrets --only-names --project accounting --config dev | grep PLAID_` shows `PLAID_FERNET_KEY` (and existing `PLAID_*` API credentials), no longer `PLAID_TOKEN_ENC_KEY`.
- **Split idempotency guards (run BEFORE the block above, both independent):**
  - AES-GCM provisioning guard: `if doppler secrets --only-names --project accounting --config prd | grep -q PLAID_TOKEN_ENC_KEY_MIGRATION; then echo "M0c AES-GCM step already complete; skipping provisioning block"; fi` — if PLAID_TOKEN_ENC_KEY_MIGRATION is already present, skip only the AES-GCM generation and provisioning steps (steps 1 and 2 above).
  - Fernet rename guard: `if doppler secrets --only-names --project accounting --config dev | grep -q PLAID_FERNET_KEY && ! doppler secrets --only-names --project accounting --config dev | grep -q "^PLAID_TOKEN_ENC_KEY$"; then echo "M0c Fernet rename already complete; skipping rename block"; fi` — if `PLAID_FERNET_KEY` is present AND `PLAID_TOKEN_ENC_KEY` is absent in accounting Doppler, skip only the Fernet rename steps (step 3 above). The two guards are INDEPENDENT — partial completion of M0c (e.g., AES-GCM provisioned but Fernet not yet renamed) is handled correctly.
- The migration script reads this via `process.env.PLAID_TOKEN_ENC_KEY_MIGRATION` when invoked with the chained doppler run. **After step 7e row-count validation SUBSTANTIATES**, do NOT delete `PLAID_TOKEN_ENC_KEY_MIGRATION` — **retain it for the FULL rollback window** (30 calendar days after cutover step 7i OR explicit rollback-window-closed sign-off, whichever is later). The rollback script (`rollback-from-d1.ts`) needs the AES-GCM key to decrypt D1 ciphertexts before re-encrypting them back to Fernet format. Without `PLAID_TOKEN_ENC_KEY_MIGRATION`, rollback decryption fails. Both `PLAID_TOKEN_ENC_KEY_MIGRATION` and `PLAID_FERNET_KEY` are deleted together at step 9e (post-30-days). **M0c inter-step verification (run immediately after step 1 AES-GCM provisioning, BEFORE the Fernet rename block at step 3):**
```bash
wrangler pages secret list --project-name sparkry-crm | grep -q PLAID_TOKEN_ENC_KEY || { echo 'STOP: PLAID_TOKEN_ENC_KEY not set in Workers Pages — AES-GCM provisioning failed'; exit 1 }
```
This is a hard dependency gate. Do NOT proceed to step 3 (Fernet rename) if this check fails — the migration script would read the wrong context.
- **Failure-path cleanup:** if the migration script aborts BEFORE any `wrangler d1 execute --apply` call has been issued (script error or manual abort before any rows were uploaded), the orchestrator MUST immediately delete `PLAID_TOKEN_ENC_KEY_MIGRATION` from `accounting/prd` Doppler: `doppler secrets unset PLAID_TOKEN_ENC_KEY_MIGRATION --project accounting --config prd`. If any rows were already uploaded to D1 prod before the abort, RETAIN both `PLAID_TOKEN_ENC_KEY_MIGRATION` and `PLAID_FERNET_KEY` for the rollback window — rollback needs them to re-encrypt tokens back to Fernet format. Do not delete migration keys when a partial upload may have occurred. On successful migration (step 7e validated), `PLAID_TOKEN_ENC_KEY_MIGRATION` is retained for the full 30-day rollback window (not deleted at step 7e — see the retention note above).

Net effect after M0c: `PLAID_TOKEN_ENC_KEY` exists in Workers Pages (production runtime) AND in `accounting/prd` Doppler as `PLAID_TOKEN_ENC_KEY_MIGRATION` (migration-only). `PLAID_FERNET_KEY` exists ONLY in `accounting/dev` Doppler. The migration script reads `PLAID_FERNET_KEY` (decrypt) + `PLAID_TOKEN_ENC_KEY_MIGRATION` (re-encrypt). Workers production runtime reads `PLAID_TOKEN_ENC_KEY` (the Workers Pages binding). No same-name ambiguity in any context.

**M0d-pre — REMOVED.** The Plaid SDK bundle-size measurement has been moved to a self-verification sub-step inside PL-T02 in the crm/workers-plaid worktree. This is the correct placement because the bundle size can only be measured after the Plaid SDK is imported in that worktree's context. M0d-pre is not a separate M0 task. See the PL-T02 task description for the measurement procedure.

**M0d — Provision Twelve Data API key (parallel sub-track, blocks BR-T04 only).** Travis must sign up at twelvedata.com (free tier), retrieve the API key, and reply with **TWELVE-DATA-KEY-READY**. This STOP applies only to the Twelve Data provisioning sub-track — all other team-leads (crm/d1-schema, crm/workers-plaid, acct/local-migration, acct/importer-cloud) can spawn and begin work after M0a–M0c complete. The orchestrator spawns the BR team-lead with the instruction: "Block BR-T04 until TWELVE-DATA-KEY-READY reply received; proceed with BR-T01 through BR-T03 in the meantime." After reply: `wrangler pages secret put TWELVE_DATA_API_KEY --project-name sparkry-crm` (Pages) AND provision on the cron Worker as documented in M0e.

**M0e-kv — Create WEALTH_KV namespace.** Before provisioning other secrets, create the KV namespace that the rotation procedure depends on:
```bash
wrangler kv:namespace create WEALTH_KV                     # production namespace
wrangler kv:namespace create WEALTH_KV --preview           # preview/staging namespace
```
Capture both namespace IDs to `.qpipeline/kv-ids.txt`:
```bash
echo "WEALTH_KV_ID=<production-namespace-id>" >> .qpipeline/kv-ids.txt
echo "WEALTH_KV_PREVIEW_ID=<preview-namespace-id>" >> .qpipeline/kv-ids.txt
```
D1-T06 reads from this file when filling in the `id =` field of `wrangler.worker.toml`. Pre-cutover checklist verifies: both `wrangler pages deploy --dry-run` (Pages) AND `wrangler deploy --dry-run --config wrangler.worker.toml` (Cron Worker) show WEALTH_KV resolved with a real namespace ID (not the placeholder `<kv-namespace-id>`). If the namespace already exists (idempotency), run `wrangler kv:namespace list | grep WEALTH_KV` and capture the existing IDs into `.qpipeline/kv-ids.txt` without creating a new namespace.

**M0e — Move existing Plaid secrets to Pages AND provision Cron Worker secrets.** For each of `PLAID_CLIENT_ID`, `PLAID_SANDBOX_SECRET`, `PLAID_PRODUCTION_SECRET`, `PLAID_ENV=sandbox`: check idempotency first (`wrangler pages secret list --project-name sparkry-crm | grep -q <NAME> && echo "already set; skipping" || wrangler pages secret put <NAME> --project-name sparkry-crm`). Then provision each of those same secrets on the cron Worker (separate secret surface): `wrangler secret list --name sparkry-crm-cron | grep -q <NAME> && echo "already set; skipping" || printf '%s' "$VALUE" | wrangler secret put <NAME> --name sparkry-crm-cron`. Also provision on the cron Worker: `PLAID_TOKEN_ENC_KEY` (value from M0c), `TWELVE_DATA_API_KEY` (value from M0d), `WEALTH_INTERNAL_KEY` (value from M0b), `RESEND_API_KEY` (must be explicitly set on cron Worker even if already on Pages — separate secret surface), `SENTRY_DSN` (existing; the cron Worker's `src/worker.ts` is wrapped in `withSentry` — this secret must be verified present on the cron Worker), **`R2_BACKUP_WRITE_TOKEN`** (the Cloudflare API token with R2 **WRITE-only** permission to `sparkry-crm-backups/wealth/*` — required by `handleR2Backup`; the backup handler MUST NOT have LIST or DELETE permission per the two-token split in REQ-WC-018; generate via Cloudflare API Tokens dashboard before M0e). Also provision `R2_BACKUP_PRUNE_TOKEN` on the SEPARATE prune-cron handler's secret surface (LIST+DELETE only) — NOT on the backup handler. Document the "re-entry from M0e" recovery path: if interrupted mid-step, re-run the idempotency-guarded commands; already-set secrets are skipped.

**M0f — Set WEALTH_ALLOWED_EMAILS.** Idempotency guard: `if wrangler pages secret list --project-name sparkry-crm | grep -q WEALTH_ALLOWED_EMAILS; then echo "M0f already complete; skipping"; else wrangler pages secret put WEALTH_ALLOWED_EMAILS --project-name sparkry-crm; fi` — value `travis@sparkry.com` (comma-separated for future additions).

**M0g — Verify all secrets present (REQ-WC-019).** Capture `wrangler pages secret list --project-name sparkry-crm` to a file. The orchestrator runs the anti-hallucination validator on this output asserting **all 9** Workers Pages secrets are listed (the 8 new + the inherited `RESEND_API_KEY` that the CRM already has on this project). Also capture `wrangler secret list --name sparkry-crm-cron` and assert all required Cron Worker secrets are listed: `PLAID_CLIENT_ID`, `PLAID_SANDBOX_SECRET`, `PLAID_PRODUCTION_SECRET`, `PLAID_ENV`, `PLAID_TOKEN_ENC_KEY`, `TWELVE_DATA_API_KEY`, `WEALTH_INTERNAL_KEY`, `RESEND_API_KEY`, `SENTRY_DSN`, **`R2_BACKUP_WRITE_TOKEN`** (per REQ-WC-019 Cron Worker secrets section). If `RESEND_API_KEY` is missing from either deployment, halt — REQ-WC-007 stale-Item alert cannot work without it. If `SENTRY_DSN` is missing from the cron Worker, halt — the cron Worker's `withSentry` wrapper requires it.

**M0h — Cloudflare Access policy audit + Plaid redirect URI registration + CF Service Token.** The orchestrator opens the Cloudflare Access dashboard via Chrome MCP, captures a screenshot of the current `internal.sparkry.ai` policy, confirms (a) Travis and Amy are allowlisted at zone level, (b) a CF Access policy covering `sparkry-crm.pages.dev` (or `*.pages.dev`) exists or is created now to block preview URL access — add to pre-cutover checklist: "Confirm `https://sparkry-crm.pages.dev/wealth/` returns a CF Access login prompt (not a 200)," (c) implements the bypass for `/wealth/api/internal/*` via a Workers route handler that returns 405 for any non-POST request (CF Access bypass UI does not support method filtering; the handler enforces POST-only AND validates X-Internal-Key; **IP allowlisting is NOT used** — see auth layering in spec), (d) tightens the Cloudflare rate-limiting rule on `/wealth/api/internal/*` to 5 requests per minute per IP (the CRM's existing default rate-limit is 20 req/min; tighten to 5 for this endpoint only), (e) verifies the new rules are saved. Validator SUBSTANTIATES from screenshot.

**M0h — CF Access Service Token:** The CF Access Service Token for the smoke-test curl is NOT created at M0h. It is created immediately before step 7g (step 7g-pre), used during steps 7g and 7k, and revoked immediately after step 7k completes (step 9f). This tight lifetime (cutover window only) limits exposure time. Do NOT store the token in Doppler long-term — keep only in environment variables for the cutover window duration. M0h does NOT include a Service Token creation step. Steps 7g and 7k use `CF_SMOKE_TOKEN_ID` and `CF_SMOKE_TOKEN_SECRET` as environment variables set at step 7g-pre time.

**Also during M0h — register both Plaid redirect URIs now (not at step 7h):** register `https://internal.sparkry.ai/wealth/desk/connections/oauth-return` in BOTH the Plaid sandbox dashboard AND the Plaid production dashboard. Sandbox changes are instant; production changes may require manual review with up to 24 hours lag. Completing both during M0 ensures both URIs are active by cutover time. Screenshot both dashboards showing the new URI in the allowed-redirect-URIs list. Step 7h (in the cutover sequence) then only CONFIRMS the existing registrations are active — it does NOT add them.

**M0i — D1 region check.** `wrangler d1 info sparkry-crm-prod` — if the primary region is not `wnam` (West North America), the orchestrator runs the sub-team protocol to decide: migrate D1 (expensive) or accept the latency. Documented either way.

**M0j — Capture Python golden output for BR-T01.** In `accounting` repo: start the local FastAPI and verify it is running before the curl loop:
```bash
doppler run --project accounting --config dev -- uvicorn src.api.main:app --port 8000 &
sleep 5
curl -fsS http://localhost:8000/health > /dev/null || { echo "API failed to start"; exit 1 }
```
Then hit every brokerage GET endpoint AND the Plaid routes (`/api/plaid/items`, `/api/plaid/reconciliation/summary`) with a curl loop against the live SQLite DB, save responses to `tests/fixtures/brokerage-golden/<endpoint-slug>.json`. After the curl loop, kill the background uvicorn process: `kill %1` (or `pkill -f "uvicorn src.api.main:app --port 8000"`). Commit the captured fixtures. The orchestrator forwards the file paths to the crm/workers-brokerage team-lead (for brokerage routes) and the crm/workers-plaid team-lead (for Plaid routes, stored in `tests/fixtures/plaid-golden/<endpoint-slug>.json`). **Plaid golden capture note:** M0j captures Plaid golden with `PLAID_ENV=sandbox`. Step 7g comparison for Plaid endpoints is schema-only (response shape + immutable fields: `item_id`, `institution_id`, `status`) — NOT value-level diff, because values differ between sandbox and production. The immutable field list is explicitly documented in this M0j capture file's header comment. For brokerage endpoints: full JSON-shape + value diff against the golden output. **Step 7g staleness note:** M0j golden output is captured at M0 time. Step 7g comparison may show data-staleness diffs unrelated to migration correctness (new brokerage rows ingested between M0 and step 7d). Step 7g comparison MUST use schema-only assertion for both brokerage and Plaid endpoints (response shape + immutable fields). Value-level diffs in brokerage endpoints from data added after M0j are NOT a comparison failure.

**M0k — Capture local SQLite snapshot.** `sqlite3 /Users/travis/SGDrive/dev/accounting/data/accounting.db ".backup /Users/travis/SGDrive/dev/accounting/data/accounting.pre-wealth-migration-$(date -u +%Y%m%dT%H%M%SZ).db"`. Verify file `> 1 MB`. This is the rollback safety net for the whole migration.

**M0l — Verify AuditEvent rollback pre-condition.** Run `sqlite3 /Users/travis/SGDrive/dev/accounting/data/accounting.db "SELECT COUNT(*) FROM audit_events WHERE entity_id IS NOT NULL"`. Validator SUBSTANTIATES the captured output shows `0`. If not 0, HALT — the LM-T01 rollback would fail.

M0 blocks all team-lead worktrees.

### Worktree: crm/d1-schema

Tasks run sequentially. TDD discipline is honestly scoped — D1-T01 is "write the schema correctly" (no red-green; tested via D1-T05); D1-T05 is the proper TDD task.

| Task | Description | Test seed |
|---|---|---|
| D1-T00 | Create staging D1: `wrangler d1 create sparkry-crm-staging --location wnam`. Capture the database_id. Uncomment the staging block in `wrangler.toml` / `wrangler.worker.toml` and fill in the id. Required for D1-T03/D1-T04 to be testable end-to-end before prod. Verify the `wrangler d1 export sparkry-crm-staging --remote --output staging-test.sql` command works against staging before prod cutover (confirms the command syntax works end-to-end — database name as positional argument, NOT `--database` flag; `--remote --output` flag order is verified here). Also verify: `wrangler d1 execute sparkry-crm-staging --remote --command "SELECT 1 AS test"` returns a row with `test=1`. Capture wrangler version: `wrangler --version` output recorded in `.qpipeline/wrangler-version.txt` for pre-cutover parity check. | `wrangler d1 list` shows both prod and staging; `wrangler d1 info sparkry-crm-staging` reports primary region `wnam`; `staging-test.sql` is non-trivially sized after a test data insert; `wrangler d1 execute sparkry-crm-staging --remote --command "SELECT 1 AS test"` returns `test=1`; wrangler version captured. |
| D1-T01 | (REQ-WC-003, REQ-WC-009) Add Drizzle schema (`src/lib/server/db/schema-wealth.ts`) covering all **13 wealth tables** (incl. `ingestion_log`). CHECK + UNIQUE constraints inline. Required indexes per REQ-WC-017 also inline. Every D1 wealth table that has a `raw_data` column in the Python source mirrors it as TEXT (JSON-stringified, nullable=false). D1 has a 1 MB row limit; if a row's `raw_data` exceeds 900 KB (rare for large F&G/GSK PDF cases), the ingest handler REJECTS the row with 422 and writes an IngestionLog error row — do NOT silently truncate (raw_data is part of the audit-trail invariant). There is NO `raw_data_truncated` flag column. UNIQUE(account_id, snapshot_date) on `plaid_account_balance_snapshot` is intentional and matches the Python source — Plaid `account_id` is globally unique per Item, so `item_id` need not be part of the constraint. `historical_price` keeps the composite PK `(symbol, trade_date)` — no UUID surrogate needed (matches Python source, simplest, no surrogate). **`ingestion_log` schema mirrors the Python source (`src/models/ingestion_log.py`)**: columns are `id` (UUID PK), `source`, `run_at` (timestamp), `status`, `records_processed`, `records_failed`, `error_detail`, `retryable`, `retried_at`, `resolved_at`. There is NO `run_date` column, NO `row_count` column, and NO UNIQUE constraint on this table — idempotency is enforced at the snapshot level. **`audit_events` table has `entity_id NOT NULL` and `entity_type NOT NULL` (no `transaction_id` column). `changed_by` column is `String(64)` to accommodate actor strings like `'cron:twelve-data-ingest'` (21 chars) and `'human:<email>'`. `cf_scheduled_time` column is `INTEGER nullable` (Unix epoch ms); NULL for human-initiated rows, populated from `controller.scheduledTime` for cron-initiated rows.** Include D1 triggers on `audit_events` preventing DELETE and UPDATE — both triggers written out explicitly:
```sql
CREATE TRIGGER audit_events_no_delete BEFORE DELETE ON audit_events BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
CREATE TRIGGER audit_events_no_update BEFORE UPDATE ON audit_events BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
```
For the 5 brokerage data tables (`plaid_account_balance_snapshot`, `brokerage_transaction`, `realized_gain_loss`, `position_snapshot`, `cost_basis_lot`) and `plaid_item`, only DELETE triggers are added — no UPDATE trigger is added on those tables (brokerage data rows may have metadata corrected; `plaid_item` explicitly requires UPDATE for REVOKED overwrite and abandoned status). The `plaid_item` DELETE trigger is:
```sql
CREATE TRIGGER plaid_item_no_delete BEFORE DELETE ON plaid_item BEGIN SELECT RAISE(ABORT, 'plaid_item is append-only per never-delete invariant'); END;
```
D1-T01 includes explicit column-name disambiguation: `plaid_account_balance_snapshot.snapshot_date` (TEXT ISO-8601), `account_balance_snapshot.as_of` (TEXT ISO-8601), `position_snapshot.as_of` (TEXT ISO-8601) — do not conflate these. **`plaid_item.state_nonce_expires_at` is stored as ISO-8601 TEXT** (e.g., `'2026-05-10T10:37:00Z'`); the cleanup cron uses `< datetime('now')` to compare (NOT `< strftime('%s','now')` which compares ISO text against epoch integers and never matches). **Credit/loan balance sign convention:** `current_balance` and `available_balance` are stored as raw Plaid values (positive for credit accounts); negation to negative happens at query time only in the reconciliation handler. D1-T01 schema comment documents this: "current_balance stores raw Plaid value; sign normalization for credit/loan happens at query time." **Explicit decimal-column scale table** (D1-T01 must document this per-column for D1-T05 precision KATs):
  - Scale 8 (prices, quantities): `historical_price.close` (per `src/models/history.py:60` — NOT `close_price`), `position_snapshot.quantity`, `position_snapshot.avg_cost_basis` (per `src/models/brokerage.py:251` — NOT `cost_basis_per_unit`), `cost_basis_lot.quantity`, `cost_basis_lot.cost_per_share` (per `src/models/history.py:203` — NOT `cost_basis_per_unit`)
  - Scale 4 (Plaid balances): `plaid_account_balance_snapshot.current_balance`, `plaid_account_balance_snapshot.available_balance`
  - Scale 2 (monetary): `brokerage_transaction.amount`, `brokerage_transaction.fees`, `brokerage_transaction.commission`, `realized_gain_loss.proceeds`, `realized_gain_loss.cost_basis`, `realized_gain_loss.gain_loss`, `cost_basis_lot.cost_total` (Numeric(14,2)), `cost_basis_lot.wash_sale_adj` (Numeric(14,2), nullable), `account_balance_snapshot.balance` (Numeric(14,2))
  **Not TDD** — schema is hand-authored; correctness asserted by D1-T05. | `drizzle-kit check` passes. |
| D1-T02 | Generate `migrations/000X_wealth_init.sql`. Hand-verify CHECK + UNIQUE clauses (Drizzle sometimes drops these silently — see existing CRM migration for an example to compare against). Hand-edit if needed. | `wrangler d1 execute sparkry-crm-staging --remote --file=migrations/000X_wealth_init.sql` applies cleanly (database name is a positional argument, NOT a `--database` flag); smoke-INSERT one row in every table. |
| D1-T03 | (REQ-WC-014) Write `scripts/migrate-from-sqlite.ts` (TypeScript, in sparkry-crm; invoked by chaining both Doppler contexts WITHOUT intermediate shell re-export: `doppler run --project accounting --config dev -- doppler run --project accounting --config prd -- npx tsx scripts/migrate-from-sqlite.ts ...`; both Doppler contexts inject env independently into the final Node process). Verify via `ps -e -o command \| grep tsx` during a dry-run that no env value appears on the command line. Takes `--sqlite-path`, `--target {staging\|prod}`. Reads SQLite via `better-sqlite3`. Dumps brokerage/plaid/history/audit_events/ingestion_log rows. DDL is replaced by the canonical Drizzle migration; only INSERT data taken from dump. Converts NUMERIC→TEXT canonical-decimal. **Re-encrypts every PlaidItem.access_token_encrypted from Fernet to AES-GCM** using: (a) the npm `fernet` package (pinned version, e.g. `"fernet": "0.4.0"` in package.json) + `process.env.PLAID_FERNET_KEY` (from accounting Doppler) for decrypt, (b) Web Crypto `crypto.subtle.encrypt` + `process.env.PLAID_TOKEN_ENC_KEY_MIGRATION` (from `accounting/prd` Doppler mirror per M0c) for encrypt. **PLAID_FERNET_KEY is URL-safe base64 (RFC 4648 §5); decode with `Buffer.from(key.replace(/-/g, '+').replace(/_/g, '/'), 'base64')` — NOT `atob()`. The npm `fernet` package handles URL-safe base64 internally if passed the raw key string; the KAT fixture key MUST contain at least one `-` or `_` character.** Add a key-format validation: `if (!/^[A-Za-z0-9_-]+=*$/.test(fernetKey)) throw new Error('PLAID_FERNET_KEY is not URL-safe base64')` before use. **Per-column scale dispatch (CRITICAL):** the migration script MUST import the per-column scale table from D1-T01 and apply the correct `quantize` scale per column when emitting INSERT TEXT values. Do NOT apply a uniform quantize across all numeric columns. For example: `brokerage_transaction.amount` (scale 2) emits `'1234.56'`, NOT `'1234.56000000'`; `historical_price.close` (scale 8) emits `'0.12345678'`, NOT `'0.12'`. The scale-dispatch map must reference the exact column names from D1-T01: `historical_price.close` (scale 8), `position_snapshot.avg_cost_basis` (scale 8), `cost_basis_lot.cost_per_share` (scale 8), `account_balance_snapshot.balance` (scale 2), etc. **Pre-migration assertions (BOTH must pass before any row is processed):** `if (!process.env.PLAID_FERNET_KEY) throw new Error('PLAID_FERNET_KEY not present — wrong Doppler context (need accounting chained)')` AND `if (!process.env.PLAID_TOKEN_ENC_KEY_MIGRATION) throw new Error('PLAID_TOKEN_ENC_KEY_MIGRATION not present — wrong Doppler context (need accounting/prd chained)')`. **Pre-migration row validation:** compute `Buffer.byteLength(JSON.stringify(row.raw_data))` for every row with `raw_data`; if > 900 KB, log to stderr AND write to `migration-oversized-rows.json` report file; do NOT include in the INSERT batch. **INSERT idempotency:** use `INSERT OR IGNORE INTO plaid_item ...` (or `ON CONFLICT(id) DO NOTHING`) so partial-upload re-runs skip already-inserted rows. Add a pre-condition check: if any `plaid_item` rows already exist in the D1 target, log a warning and skip those specific rows. Document the re-run sequence for partial-upload recovery. Writes a D1-loadable INSERT-only .sql file using parameterized `db.prepare().bind()` calls (NOT raw SQL string interpolation — prevents SQL injection via raw_data containing quotes). Insert rows one at a time (decrypt → re-encrypt → emit INSERT → null reference) rather than batching all PlaidItem rows in memory. Migration script MUST NOT log any string matching the Plaid token format `access-[a-z0-9]+-[a-z0-9]+` at any log level. **Post-migration AES-GCM decrypt verification (mandatory before step 7e):** after `wrangler d1 execute --remote` completes, for each `plaid_item` row in D1, attempt decrypt of `access_token_encrypted` using `PLAID_TOKEN_ENC_KEY_MIGRATION`; assert decryption succeeds AND the plaintext matches the Plaid access-token format regex `^access-(sandbox|production|development)-[a-f0-9]{32}$`. Halt immediately if any token fails to decrypt — do NOT proceed to step 7e row-count validation with corrupted tokens. Run `ulimit -c 0` before invoking `npx tsx` to disable core dumps. **Required known-answer test:** check in `tests/fixtures/fernet-kat.json` containing `{ fernet_key (URL-safe base64, MUST contain at least one '-' or '_'), encrypted_token (Python cryptography.fernet output for known plaintext "access-token-fixture-abc123"), expected_plaintext }`; D1-T03's KAT reads this file to prove npm-fernet interoperability with the canonical format. Fixture row includes `raw_data` containing `"name": "O'Brien & Co."` to verify parameterized binding handles quotes correctly. | Unit test against a fixture SQLite DB with 5 rows per table including Fernet-encrypted PlaidItem rows (token plaintexts known); KAT reads `tests/fixtures/fernet-kat.json` and asserts npm-fernet decrypts the Python-produced fixture (fixture key contains `-` or `_`); migration log scan asserts no Plaid token format appears in log output; output .sql applies to staging D1 with row-count match AND post-load AES-GCM decrypt (using PLAID_TOKEN_ENC_KEY_MIGRATION) recovers the original token plaintexts; fixture row with 950KB raw_data appears in `migration-oversized-rows.json` report and NOT in the INSERT output; re-run of a partial-upload skips already-inserted rows without error. **D1-T05 KAT verifying byte-identity of post-migration TEXT against fresh-ingest TEXT for each column scale type:** fixture rows with a scale-2 value (`1234.56`), a scale-4 value (`100.5000`), and a scale-8 value (`0.12345678`) — after migration, assert the TEXT stored in D1 is byte-identical to what a fresh ingest path would produce using the same scale-dispatch table. |
| D1-T04 | Write `scripts/rollback-from-d1.ts`: before proceeding, queries D1 for `ingestion_log` rows with `run_at > cutover_timestamp` and emits a diff report of "D1-only rows that will be lost if you rollback to SQLite" (rows written by Workers cron after cutover that have no SQLite counterpart); the operator must confirm with `--apply` to proceed despite data-loss. Exports the CURRENT D1 state (not just the pre-migration snapshot — includes any new rows written by Workers cron after cutover); re-encrypts AES-GCM tokens back to Fernet using the original local key; dumps to SQLite-compatible format. LM-T01 MUST NOT run until the soak window closes (3 consecutive cron successes) so the SQLite target is intact for any within-window rollback. `--apply` is required to execute the rollback (dry-run default). **Rollback KAT through Python consumption:** after rollback re-encrypts AES-GCM → Fernet, the KAT runs a Python subprocess to verify the round-trip at the Python consumption boundary: `doppler run --project accounting --config dev -- python3 -c "from cryptography.fernet import Fernet; import os, sys; print(Fernet(os.environ['PLAID_FERNET_KEY']).decrypt(sys.stdin.read().encode()).decode())"` with the rolled-back ciphertext piped in; assert output equals the original plaintext. | Round-trip test: SQLite → migrate to staging D1 → fire cron once → rollback → assert all post-cutover D1 rows (including cron-written rows) are reflected in SQLite AND token plaintexts match after decrypt via Python Fernet subprocess; assert dry-run without `--apply` prints the diff report but does not modify SQLite. |
| D1-T05 | (REQ-WC-003, REQ-WC-004) Constraint-violation tests: each CHECK and UNIQUE in the new D1 schema gets a Vitest that violates it and asserts the D1 batch returns the expected error. **Per-column decimal precision KAT (REQ-WC-004):** using the explicit scale table from D1-T01, for EVERY Numeric column, assert the round-trip canonical string matches Python's `str(value.quantize(...))` for that column's scale. KAT MUST include at least: (a) an 8-decimal quantity row (e.g., `0.12345678`); (b) a 2-decimal monetary row (e.g., `1234.56`); (c) a `.50`-trailing-zero row (e.g., `10.50` — must round-trip as `'10.50'` not `'10.5'`); (d) a `0.00` row; (e) `-0` normalization — insert `-0.00`, assert retrieved as `'0.00'` (not `'-0.00'`). Also includes: raw_data round-trip test for a typical row; raw_data oversized test — POST a row with 1.2 MB raw_data → 422 (verifies the handler rejects, not truncates). Per-table UNIQUE constraint tests: insert a duplicate `(account_id, snapshot_date)` into `plaid_account_balance_snapshot` → rejected; insert duplicate `(account_id, source_row_hash)` into `position_snapshot` → rejected (matches actual Python source `UniqueConstraint('account_id', 'source_row_hash', name='uq_position_snapshot_dedup')`); insert duplicate `(symbol, trade_date)` into `historical_price` → rejected. **`state_nonce_expires_at` format test:** insert a `plaid_item` row with `state_nonce_expires_at` set to a past ISO-8601 TEXT timestamp; assert the cleanup cron query `< datetime('now')` matches the row and marks it `abandoned`. Also insert a future ISO-8601 timestamp and assert the query does NOT match it. Audit_events trigger tests: attempt UPDATE on an `audit_events` row → rejected with `'audit_events is append-only'`; attempt DELETE → rejected with same error. **This is the real TDD task** — failing tests written first against an empty schema (red), then schema added → green. | All constraint-violation tests fail on bare DB, pass after `wrangler d1 migrations apply`. |
| D1-T06 | `worker.ts` cron dispatcher: implements `export default { scheduled(controller, env, ctx) { ... } }`, registers **five** crons declared in spec — REQ-WC-006 `"7 10 * * *"` (Plaid balance sync), REQ-WC-013 `"30 7 * * *"` (Twelve Data prices), REQ-WC-018 `"0 12 * * *"` (D1→R2 backup), REQ-WC-007 `"0 14 * * MON"` (Plaid stale-Item Monday email), REQ-WC-005 `"0 */6 * * *"` (PlaidItem abandoned-placeholder cleanup) — and dispatches by `controller.cron` string-match, calling handlers with `(env, ctx)` to preserve `ctx.waitUntil()` semantics. **Register all five cron expressions in `wrangler.worker.toml` under `[triggers] crons = [...]` ONLY in the `crm/d1-schema` worktree** (the cron Worker config — NOT `wrangler.toml` which is the Pages config). `crm/workers-plaid` and `crm/workers-brokerage` do NOT touch `wrangler.worker.toml`. Add all required bindings to `wrangler.worker.toml`: `[[d1_databases]] binding = "DB" database_name = "sparkry-crm-prod" database_id = "<id>"` (production block) AND equivalent staging block with `database_name = "sparkry-crm-staging"`; `[[r2_buckets]] binding = "R2_BACKUPS" bucket_name = "sparkry-crm-backups"`; `[[kv_namespaces]] binding = "WEALTH_KV" id = "<kv-namespace-id>"`. Verify all bindings compile: `wrangler deploy --dry-run --config wrangler.worker.toml` must succeed. Add to pre-cutover checklist: `wrangler deploy --dry-run --config wrangler.worker.toml` passes with all bindings declared. Verify post-deploy with `wrangler deployments view` (or the Cloudflare dashboard Triggers tab) that all five cron expressions are live. **IMPORTANT — stub crons go live immediately on merge step 1:** cron triggers registered in `wrangler.worker.toml` are live on production immediately after the d1-schema merge (step 1). Stub firings between merge step 1 and merge step 3 are expected no-ops (`console.warn`). The 3-consecutive-success soak window in step 7j MUST start counting only after merge step 3 (workers-plaid AND workers-brokerage are both merged and deployed). Pre-cutover checklist MUST verify: `wrangler deployments view sparkry-crm-cron` shows the latest deploy includes real handler implementations (not `console.warn` stubs); verify by inspecting the deployed JS bundle or checking that the most recent `ingestion_log` rows have `status='success'` rather than absent. **Cross-worktree import strategy:** D1-T06 writes inline STUB implementations in `src/worker.ts` (the existing CRM cron entry point per `wrangler.worker.toml main = 'src/worker.ts'`) for all five handlers — `handlePlaidSync(env: Env, ctx: ExecutionContext)`, `handlePlaidStaleAlert(env: Env, ctx: ExecutionContext)`, `handleTwelveDataIngest(env: Env, ctx: ExecutionContext)`, `handleR2Backup(env: Env, ctx: ExecutionContext)`, `handlePlaidItemCleanup(env: Env, ctx: ExecutionContext)` — each body just `console.warn("STUB: <name>")`. D1-T06 ADDS to the existing `src/worker.ts` (does NOT create a new file at the project root). The merges of `feat/wealth-workers-plaid` and `feat/wealth-workers-brokerage` to main are each responsible for replacing the relevant stub bodies with real imports. Stubs are typed `(env, ctx)` to match the dispatcher's calling convention exactly. **Expand the Env interface in `src/worker.ts` to include all wealth secrets: `PLAID_CLIENT_ID`, `PLAID_SANDBOX_SECRET`, `PLAID_PRODUCTION_SECRET`, `PLAID_ENV`, `PLAID_TOKEN_ENC_KEY`, `TWELVE_DATA_API_KEY`, `WEALTH_INTERNAL_KEY`, `RESEND_API_KEY`, `SENTRY_DSN`, `R2_BACKUP_WRITE_TOKEN`. The existing CRM Env members stay. `wrangler deploy --dry-run` compiles TypeScript and catches missing Env members at build time.** **Import path:** because `src/worker.ts` is compiled by wrangler/esbuild directly (NOT through the SvelteKit Vite build), the `$lib` path alias is NOT available. Use relative imports: `import { handlePlaidSync } from "./lib/server/wealth/plaid-balance-sync"` (path relative to `src/worker.ts`). The workers-plaid PR description MUST include the `src/worker.ts` edits as part of the PR scope, even though d1-schema "owns" the file initially. | `wrangler dev --test-scheduled "7 10 * * *"` invokes the (stub) handler; D1-T06's tests assert dispatch happens; `wrangler deploy --dry-run` succeeds with the stub imports (proves the compile path resolves). Verify `wrangler deployments view` shows all five cron triggers. D1-T06 test seed includes a cleanup cron test: invoke `"0 */6 * * *"` handler with a fixture PlaidItem row in `pending_oauth` state older than 30 minutes and no access_token — asserts row status becomes `abandoned`, NOT deleted. Integration tests post-merge verify real handlers fire. |
| D1-T07 | Anti-hallucination: capture the raw `wrangler d1 execute` output for D1-T02 and D1-T05; validator asserts the output really shows the row inserts / constraint failures. SHA-256 hashed at capture. | Validator agent returns SUBSTANTIATED. |
| D1-T08 | Internal /qreview on this scope (schema-correctness + migration-script lenses). | Zero P0/P1. |

**Merge gate:** PR with `feat/wealth-d1-schema` → main when D1-T08 SUBSTANTIATED. Once merged, crm/workers-plaid and crm/workers-brokerage can start.

### Worktree: crm/workers-plaid

Depends on crm/d1-schema being merged.

| Task | Description | Test seed |
|---|---|---|
| PL-T01 | Port `src/utils/plaid_crypto.py` → `src/lib/server/wealth/crypto.ts`. Web Crypto AES-GCM; multi-key rotation pattern. | TS tests covering: round-trip, rotation window, wrong-key fails, missing-key throws typed error. Map every Python test 1:1. |
| PL-T02 | Port `src/adapters/plaid_client.py` → `src/lib/server/wealth/plaid-client.ts`. **Bundle-size self-verification (sub-step, run immediately after importing the Plaid SDK):** `wrangler pages deploy --dry-run --config wrangler.toml`; assert reported compressed bundle size < 1 MB. Decision threshold: if compressed bundle < 800 KB → use the official `plaid` npm SDK (PL-T02 stays as-is; TF-005 is closed without action); if >= 800 KB → use direct REST (`fetch()` against Plaid's REST API) and activate TF-005 (define the direct Plaid REST endpoint mapping: `POST /link/token/create`, `POST /item/public_token/exchange`, `POST /accounts/balance/get`, `POST /item/remove` with `X-Plaid-Client-Id` and `X-Plaid-Secret` headers; replace SDK references in PL-T02). The `npm pack plaid && gzip -c plaid-*.tgz | wc -c` approach MUST NOT be used — it measures the double-gzipped source tarball, not the actual Workers bundle size. ONE path is chosen (SDK or REST) — do not implement both. PL-T02 documents both branches in the task notes; the active branch is determined by the bundle-size measurement at this sub-step. The orchestrator records the decision (SDK vs REST) as a comment in the worktree scope table. | Error classification table tests; retry-with-backoff tests; client factory env tests; bundle-size assertion: `wrangler pages deploy --dry-run` output confirms compressed bundle < 1 MB. |
| PL-T03 | Port `src/adapters/plaid_balance.py` → `src/lib/server/wealth/plaid-balance-sync.ts`. Three-layer error isolation translated to per-row try/catch + per-Item batch. Writes to D1 `ingestion_log` table. **Additional test requirements:** (a) fixture MUST include one `REVOKED` row — assert it is skipped without error and does not appear in any error log; (b) `error_detail` written to IngestionLog MUST contain only Plaid `error_code` and `error_type` (e.g., `'ITEM_LOGIN_REQUIRED / INVALID_CREDENTIALS'`) — NOT the full Plaid error response JSON; assert `error_detail` does not contain raw Plaid response body; (c) assert one AuditEvent row is written per cron invocation with `changed_by='cron:plaid-sync'` and `cf_scheduled_time` populated from `controller.scheduledTime`. | Port every test function from `src/adapters/test_plaid_balance.py` to Vitest — validator asserts `grep -c "def test_" src/adapters/test_plaid_balance.py` matches the Vitest count; REVOKED-row skip test; error_detail sanitization test; AuditEvent row assertion. |
| PL-T04 | Workers route handlers for all 8 Plaid endpoints (`/wealth/desk/api/plaid/*`). (REQ-WC-005) Pydantic models → Zod schemas. State-nonce CSRF preserved. Printable-ASCII validator for institution_name preserved. **link_token fetch on click, not on page load — explicit failing-time test.** Auth guard tests cover REQ-WC-002 (Cf-Access-Authenticated-User-Email + JWT validation; forged-header → 401 test included). **Plaid error response sanitization:** all `/wealth/desk/api/plaid/*` HTTP error responses return only sanitized shapes containing `error_code` and `error_type`; full Plaid SDK error body is logged server-side only (via Sentry withSentry). Vitest KAT: mock Plaid SDK throwing error with `item_id` in the body; assert HTTP response body does NOT contain `item_id`. | Port every test function from `src/api/test_plaid_routes.py` to Vitest. Validator MUST assert explicit test count: run `grep -c "def test_" src/api/test_plaid_routes.py` and assert the Vitest suite count equals that number (exactly 30 per the effort section). Both the Python grep count and the Vitest count must match. KAT: mock Plaid error containing `item_id` → HTTP response body does not contain `item_id`. |
| PL-T05 | Export `handlePlaidSync(env, ctx)` from `src/lib/server/wealth/plaid-balance-sync.ts`. The workers-plaid PR ALSO edits `worker.ts` to replace the D1-T06-authored stub for `"7 10 * * *"` with a real import + call to `handlePlaidSync(env, ctx)`. The PR description must list `worker.ts` among modified files. | Local test via `wrangler dev --test-scheduled "7 10 * * *"`; handler invokes sync and writes snapshot rows + ingestion_log row. `wrangler deploy --dry-run` succeeds on the merged worker.ts. |
| PL-T06 | Resend integration for stale-Item Monday email. Export `handlePlaidStaleAlert(env, ctx)` from `src/lib/server/wealth/plaid-stale-alert.ts` (separate module from `plaid-balance-sync.ts`). PR also edits `src/worker.ts` to replace the D1-T06 stub for `"0 14 * * MON"` with `import { handlePlaidStaleAlert } from "./lib/server/wealth/plaid-stale-alert"` and the real call. **Required assertions:** `assert emailPayload.to === 'travis@sparkry.com'` (locks the hardcoded recipient against regression). KAT verifies `handlePlaidStaleAlert` calls `resend.emails.send({ to: ['travis@sparkry.com'] })` with literal constant. Defense test: set `process.env.WEALTH_ALERT_EMAIL = 'attacker@evil.com'` before invoking the handler; assert email still goes to `travis@sparkry.com` (proves the constant is not configurable via env). Assert one AuditEvent row is written per cron invocation with `changed_by='cron:stale-alert'`, `cf_scheduled_time` populated from `controller.scheduledTime`, and `entity_type='plaid_item'` for each stale item. Assert one IngestionLog row written with `source='plaid-stale-alert'` recording success/failure of the Resend call. | Mock Resend client; assert email payload contains the right Items; assert `emailPayload.to === 'travis@sparkry.com'`; defense test: `WEALTH_ALERT_EMAIL='attacker@evil.com'` → email still sent to `travis@sparkry.com`; assert AuditEvent row written with `changed_by='cron:stale-alert'` and non-null `cf_scheduled_time`; assert IngestionLog row written with `source='plaid-stale-alert'`. |
| PL-T07 | Anti-hallucination: every test run captures full Vitest output with SHA-256; validator agent asserts no `0 collected`, no `skipped` masquerading as passing, no truncation in load-bearing section. | Validator returns SUBSTANTIATED on every test-gate. |
| PL-T08 | Internal /qreview to convergence on this scope alone (security + financial-correctness + code-quality + test-coverage lenses, restricted to the Plaid module). | Zero P0/P1 across all 4 lenses. |

**Merge gate:** PR opened after PL-T08; team-lead drives the merge once review is clean.

### Worktree: crm/workers-brokerage

Depends on crm/d1-schema being merged. Parallel with crm/workers-plaid.

| Task | Description | Test seed |
|---|---|---|
| BR-T01 | Port `src/api/routes/brokerage.py` endpoints (**all 13 routes** incl. top-holdings/recent-transactions/data-integrity; benchmark allowlist preserved as hardcoded constant) to Workers handlers under `src/routes/(wealth)/wealth/api/brokerage/*`. The orchestrator copies the golden JSON files to a path accessible from the sparkry-crm worktree: `cp -r tests/fixtures/brokerage-golden ~/sparkry-crm-wt-workers-brokerage/tests/fixtures/brokerage-golden-from-accounting/`. The spawn message to the BR team-lead specifies the exact path: "Read golden output from `tests/fixtures/brokerage-golden-from-accounting/*.json`." | Contract tests against the M0j Python golden output files at `tests/fixtures/brokerage-golden-from-accounting/*.json`; assert the TypeScript implementation matches the JSON shape exactly per endpoint. |
| BR-T02 | (REQ-WC-008) Reconciliation summary handler: port the `> 2%` OR `> $100` threshold logic (strict), credit/loan negation, `null` when no positions priced. The exact source is `src/api/routes/plaid.py` function `reconciliation_summary` (NOT `src/api/routes/brokerage.py`, NOT `src/utils/reconciliation.py` which is the Stripe payout-vs-deposit pairing module — verify before porting: `grep -n 'def reconciliation_summary' src/api/routes/plaid.py`). The `plaid_signed` value is computed as `-snap.current_balance` for credit/loan account types; `available_balance` is NOT negated. Credit and loan account `current_balance` values are sign-flipped before comparison. **D1 sign convention:** `current_balance` and `available_balance` are stored in D1 as raw Plaid values (positive for credit accounts); sign normalization to negative happens at query time only, not at write time. Test seed MUST include: (1) a fixture credit-account row stored in D1 with `current_balance=+1500.00` (raw Plaid value); (2) reconciliation summary query returns `plaid_signed=-1500.00` for that account. This two-step assertion guards against accidental write-time normalization. | Port the 5 parametrized boundary tests + the "no priced positions" regression test from Python; assert credit/loan sign-flip behavior on `current_balance` only (not `available_balance`); add fixture row + assertion for the sign-flip boundary; assert D1 stores raw Plaid `current_balance=+1500.00` and query returns `plaid_signed=-1500.00`. |
| BR-T03 | Internal-ingest endpoints with `X-Internal-Key` header auth (REQ-WC-012). Endpoints: `POST /wealth/api/internal/ingest/brokerage-csv`, `xlsx-snapshot`, `historical-prices`, `cost-basis-lot`. Writes to D1 `ingestion_log`. Constant-time comparison via `crypto.subtle.timingSafeEqual()` for `X-Internal-Key`. **Implementation MUST length-check before calling `timingSafeEqual()`: if `incoming.byteLength !== secret.byteLength`, return 401 immediately (do not call timingSafeEqual on mismatched-length buffers — throws TypeError).** Dedup hash (source_row_hash) test: known-answer test asserting Python and TypeScript produce identical hex digests for the same fixture CSV row (REQ-WC-012). **Two-level framing MUST be documented in the KAT for all three hash functions**, using the general form `SHA256(UTF-8 of f'{len(source_type)}:{source_type}:<framed>')` per `src/utils/dedup.py:36`, where `<framed>` = `'|'.join(f'{len(p)}:{p}' for p in [broker, account_number, ...])`. The three hash variants and their correct outer prefixes are:
- `compute_brokerage_row_hash`: outer prefix `12:brokerage_row:` (where `12 = len('brokerage_row')`)
- `compute_position_row_hash`: outer prefix `18:brokerage_position:` (where `18 = len('brokerage_position')`)
- `compute_realized_lot_hash`: outer prefix `17:brokerage_realized:` (where `17 = len('brokerage_realized')`)

The KAT fixture file MUST include the intermediate `framed` string AND the final SHA-256 hex digest for each of the three variants, allowing TypeScript implementers to verify each layer independently. KAT fixture MUST include three variants: (a) standard row with `row_index=0`; (b) same-day duplicate row with `row_index=1` (assert hash differs from variant a); (c) synthetic row with `synthetic_suffix='div_partner'` (assert hash differs from the same row without suffix). All three Python-vs-TypeScript hex digests must be byte-identical. Idempotency test: POST same payload twice → second call returns existing row, no duplicate. Max **100 rows** per POST; returns 413 if exceeded (101-row payload → 413). Off-by-one-byte header → 401 (constant-time semantics verified by timing test). | Tests for: header missing → 401; header wrong → 401; off-by-one byte → 401; header of length 0 → 401 (not 500); header of completely different length → 401 (not TypeError); payload shape mismatch → 422; payload > 100 rows → 413; happy path inserts rows and writes IngestionLog row; duplicate row → returns existing; three KAT hash variants produce identical Python/TypeScript hex digests; KAT fixture includes intermediate framed string for two-layer verification. |
| BR-T04 | Export `handleTwelveDataIngest(env, ctx)` from `src/lib/server/wealth/twelve-data-ingest.ts`. Pull EOD prices for every symbol in `position_snapshot` using **one API call per symbol** (Twelve Data free tier; batch is paid-tier only). This cron handler runs on Workers Paid (15-minute wall-clock). Rate-limit 8 req/min AND daily-budget cap (default **600** — wider safety buffer below the 800/day limit than the previous 680). Symbol-count metric appended to IngestionLog. **Symbol validation:** validate each symbol against `^[A-Z0-9.^]{1,12}$` before building the Twelve Data URL; symbols failing this check are skipped with an IngestionLog warning row. A URL-sanitization helper MUST be used at every Twelve Data fetch call site (not only error paths) to strip the `apikey` query parameter before any logging — never log `TWELVE_DATA_API_KEY` in cleartext. Any response body that is logged is truncated to non-sensitive fields only. Workers `console.log` writes to Cloudflare dashboard logs — same sanitization applies. CI grep check: `grep -r 'TWELVE_DATA_API_KEY' src/ | grep -v 'process.env\|env\.'` returns empty (no hardcoded refs). The daily-budget cap counter is best-effort (both cron and backfill check the day's IngestionLog count for `source='twelve_data'` (underscore — NOT `'twelve-data'` with a hyphen) before issuing API calls; cap set to 600 for wider safety buffer; concurrent cron + manual backfill may overrun by up to two batch worths in the simultaneous-start case — single-user scope makes this rare); the backfill endpoint shares the same budget counter. `historical_price.close` stores the Twelve Data `close` field (not `open`, `high`, `low`, or `adjusted_close`); other fields are discarded. Twelve Data returns bars newest-first; use `outputsize=1` for daily incremental, `outputsize=N` for backfill chunks. **AuditEvent row:** assert one AuditEvent row is written per cron invocation with `changed_by='cron:twelve-data-ingest'` and `cf_scheduled_time` populated from `controller.scheduledTime`. PR also edits `src/worker.ts` to replace the D1-T06 stub for `"30 7 * * *"` with `import { handleTwelveDataIngest } from "./lib/server/wealth/twelve-data-ingest"` and the real call. | Mock fetch returning Twelve Data shape (per-symbol response, one call per symbol); assert pacing and budget cap (600 not 680 not 700); assert `historical_price` rows written with `source='twelve_data'` (underscore) and only the target date's `close` field (NOT `close_price`) is stored; assert mock 429 response → IngestionLog error row does NOT contain literal `TWELVE_DATA_API_KEY`; assert second run for same date makes zero API calls for already-populated symbols; assert URL-sanitization helper strips apikey from logged URLs at both success and error paths; BR-T04 fixture includes a per-symbol Twelve Data response and asserts only the target date's `close` is stored; assert AuditEvent row written with `changed_by='cron:twelve-data-ingest'` and non-null `cf_scheduled_time`; assert symbol failing regex → skipped with IngestionLog warning row; **assert source string written to IngestionLog is exactly `'twelve_data'` (underscore) — this string must match the budget-counter query string**. Cron dispatcher (D1-T06) calls this on `"30 7 * * *"`. |
| BR-T05 | One-shot backfill endpoint `POST /wealth/api/internal/prices/backfill` (auth: X-Internal-Key, rate-limited, daily-budget-aware; supports `?overwrite=true` for correcting bad historical_price rows via INSERT OR REPLACE). The daily-budget cap counter is shared with the cron handler (both query `ingestion_log` rows for `source='twelve_data'` (underscore); cap is 600). | Mock fetch; assert chunked walk-back; assert idempotency on UNIQUE(symbol, trade_date) without `?overwrite=true`; assert `?overwrite=true` updates existing rows; assert 429 returned when daily budget hit; BR-T05 test: simulate cron uses 595 calls → backfill request gets 429 after 5 more calls (total 600); BR-T05 MUST also include a simultaneous-start test: simulate cron + backfill firing within milliseconds of each other and assert total calls ≤ 800 (Twelve Data's hard limit). |
| BR-T06 | Export `handleR2Backup(env, ctx)` from `src/lib/server/wealth/r2-backup.ts` (REQ-WC-018). Runs on Workers Paid (15-minute wall-clock). Iterates every wealth D1 table via paginated `SELECT * FROM <table> LIMIT 5000 OFFSET ?` in a loop; writes sequential chunk objects to R2 at `sparkry-crm-backups/wealth/daily/<table>/<YYYY-MM-DD>/<chunk-NNN>.ndjson`. **Plaid token exclusion:** for the `plaid_item` table, the backup handler MUST replace `access_token_encrypted` with the literal sentinel string `'BACKUP_REDACTED'` in every NDJSON row before writing to R2. A KAT asserts: fixture `plaid_item` row with an actual AES-GCM ciphertext as `access_token_encrypted` — the NDJSON output contains `"access_token_encrypted":"BACKUP_REDACTED"` (not the ciphertext). If the cron exits before completion, writes an `_INCOMPLETE` marker. Retention 30 days (older objects pruned in same job by date prefix). **NDJSON decimal invariant:** the backup handler uses a `replacer` function in `JSON.stringify` asserting that TEXT-typed decimal columns are `typeof 'string'` before serialization; any non-string decimal column value throws before write. PR also edits `worker.ts` to replace the D1-T06 stub for `"0 12 * * *"` with the real import + call. **AuditEvent row:** assert one AuditEvent row is written per cron invocation with `changed_by='cron:r2-backup'` and `cf_scheduled_time` populated from `controller.scheduledTime`. | Local test: mock R2 binding; assert NDJSON written per table in chunks; assert `_INCOMPLETE` marker written when pagination not finished; old objects (date >30d ago) pruned; fixture D1 row with `quantity='0.12345678'` — assert NDJSON chunk contains `"0.12345678"` (quoted string, not bare number); fixture `plaid_item` row with AES-GCM ciphertext — assert NDJSON output contains `"access_token_encrypted":"BACKUP_REDACTED"` not the ciphertext; assert one AuditEvent row written with `changed_by='cron:r2-backup'` and non-null `cf_scheduled_time`. |
| BR-T07 | CPU-budget benchmark (REQ-WC-017): (a) reconciliation handler runs against fixture DB and asserts CPU time ≤ 8 ms; (b) `POST /wealth/api/internal/ingest/brokerage-csv` with a 100-row payload must complete within 10 ms CPU budget — if exceeded, the documented escape hatch is Workers Paid (which removes the per-request CPU cap). Do NOT switch to `db.batch(10)` chunking — it violates per-record error isolation. Both benchmarks run via `wrangler dev --test-scheduled` profiling. Fails CI if either limit exceeded without documented Workers Paid escalation. | Vitest benchmark + `wrangler dev` profiler output for both reconciliation handler and 100-row ingest handler. |
| BR-T08 | Anti-hallucination: contract-test outputs SHA-256 captured; validator asserts the JSON-diff against Python golden output is byte-zero per endpoint (not just claimed empty). | Validator returns SUBSTANTIATED. |
| BR-T09 | Internal /qreview to convergence on this scope. | Zero P0/P1 across 4 lenses. |

**Merge gate:** PR opened after BR-T09 (the internal /qreview-to-convergence task).

### Worktree: crm/frontend-brokerage

Depends on crm/workers-brokerage being merged. (The frontend can be skeletal-built earlier with mocked API responses if Travis wants to parallelize visual work; team-lead decides via sub-team protocol.)

| Task | Description | Test seed |
|---|---|---|
| FB-T01 | ESLint rule `no-restricted-paths` blocking cross-group imports between `src/routes/(crm)` and `src/routes/(wealth)` + custom rule rejecting Svelte 4 syntax (`$:`, `export let`) inside `(wealth)`. CI fails on violation. | Add intentional violating import in test, assert lint fails (red), remove import, assert pass (green). |
| FB-T02 | `(wealth)/wealth/+layout.svelte`: distinct typography + palette from CRM. No nav links to CRM. Svelte 5 runes. | Playwright snapshot; grep on rendered HTML for `/customers`/`/work-orders`/`/invoices` returns 0 matches. |
| FB-T03 | `(wealth)/wealth/+page.svelte` (Wealth dashboard with net-worth + benchmark). Reuse the redesign from the recent local-side brokerage refresh as the design language. | Renders 200; chart svg present; benchmark line visible. |
| FB-T04 | `(wealth)/wealth/networth`, `holdings`, `holdings/[symbol]`, `accounts`, `accounts/[id]`, `missing-accounts`. | Each page renders 200 with sample data from D1; key data points present. |
| FB-T05 | (REQ-WC-011) Three-state filter chips for account tags; PUT-to-API tag editing. Svelte 5 runes lint check (`no-svelte4-syntax`) passes on all `(wealth)` route files. | Playwright: click chip → URL state changes; PUT call observed. |
| FB-T06 | Top-holdings, recent-transactions, data-integrity panels added (REQ-WC-010 parity). | Renders 200; data from corresponding endpoints present. |
| FB-T07 | Anti-hallucination: capture screenshots + Playwright traces with SHA-256; validator asserts the screenshots really show the asserted UI (not blank/loading state). | Validator returns SUBSTANTIATED. |
| FB-T08 | Internal /qreview to convergence. | Zero P0/P1. |

### Worktree: crm/frontend-desk

Depends on crm/workers-plaid being merged.

| Task | Description | Test seed |
|---|---|---|
| FD-T01 | `(wealth)/wealth/desk/+page.svelte` operator landing page. NOT named "admin," "console," "ops," or other admin-signaling words. | Renders 200 under Cf-Access stub; URL is `/wealth/desk`. |
| FD-T02 | `desk/connections` Plaid Item list + Add Connection flow (Plaid Link CDN script via `<svelte:head>`, OAuth-return postMessage listener with origin guard, `link_token` re-fetched on click). Port the recently-shipped Svelte page verbatim with Svelte 4 → Svelte 5 syntax adaptation. **NOT TDD for the CDN load itself** (integration test); the postMessage origin guard and the click-time token fetch ARE unit-tested with red-green discipline. The `desk/connections` event listener MUST check `event.origin === 'https://internal.sparkry.ai'` and ignore any event from a different origin. Unit tests: postMessage from `evil.com` → ignored; from `internal.sparkry.ai` → processed. Replay oauth-return URL with consumed nonce → 400 (REQ-WC-005). **Concurrent-replay test (required):** simulate two requests consuming the same nonce simultaneously; assert exactly one succeeds (200) and the other returns 400 — proves the atomic `UPDATE ... RETURNING id` nonce consumption prevents TOCTOU race. **postMessage targetOrigin:** `window.opener.postMessage(payload, 'https://internal.sparkry.ai')` — a Playwright test MUST assert the `targetOrigin` argument is exactly `'https://internal.sparkry.ai'` (not `'*'`). **Content-Security-Policy:** configure in `_headers` (or `wrangler.toml` headers): `script-src 'self' cdn.plaid.com; frame-src 'self' cdn.plaid.com`. Add a Playwright test asserting the Plaid Link iframe loads without CSP violations in the browser console. **Plaid Link CDN SRI:** if Plaid publishes a stable SRI hash for `https://cdn.plaid.com/link/v2/stable/link-initialize.js`, pin it: `<script src='...' integrity='sha256-...' crossorigin='anonymous'>`. If Plaid does not publish SRI hashes (their current practice), document this in Known Risks as accepted (CDN compromise is the threat; the page is behind CF Access). | Vitest unit tests for guard + click-fetch (red-green); origin guard tests: evil.com → ignored, internal.sparkry.ai → processed; concurrent-replay test: two simultaneous requests with same nonce → one 200, one 400; targetOrigin assertion: `window.opener.postMessage` called with `'https://internal.sparkry.ai'` not `'*'`; Playwright integration test with Plaid mock observes exchange call; Playwright test asserts no CSP violations in browser console during Plaid Link load. |
| FD-T03 | `desk/connections/oauth-return/+page.svelte` (postMessage to opener with origin guard). The page calls `window.opener.postMessage(payload, 'https://internal.sparkry.ai')` with explicit targetOrigin (NEVER `'*'`). | Renders 200 standalone; postMessage observable via test harness with explicit targetOrigin assertion. |
| FD-T04 | `desk/reconciliation` table with threshold-flag highlighting. | Renders 200; rows with `exceeds_threshold=true` have `flagged` class. |
| FD-T05 | `desk/import` CSV/PDF drop zone that POSTs to `/wealth/api/internal/ingest/*` from the browser (operator drag-and-drops without firing up local Python). Optional polish; defer if running tight. | Drag-drop test; POST observed. |
| FD-T06 | Anti-hallucination: capture screenshots with SHA-256; validator asserts Add Connection flow really opened Plaid Link iframe (not just a non-functional button). | Validator returns SUBSTANTIATED. |
| FD-T07 | Internal /qreview to convergence. | Zero P0/P1. |

### Worktree: acct/local-migration

Independent of CRM worktrees (operates on accounting repo). Can start as soon as M0 is done.

| Task | Description | Test seed |
|---|---|---|
| LM-T0 | **PRE-CUTOVER, NON-DESTRUCTIVE Alembic migration** in `src/db/alembic/versions/wealth_pre_cutover_lm_t0_xxx.py`. **Owner: acct/local-migration team-lead's FIRST action upon spawn — run and verify this migration before entering the polling loop for `cutover-complete.flag`.** Runs before cutover (may run days or weeks before the cutover window opens). Does NOT drop any tables or columns. Does three things: (1) widens `audit_events.changed_by` from `String(8)` to `String(64)` to accommodate cron actor strings like `'cron:twelve-data-ingest'` (21 chars) and `'human:<email>'`; (2) adds nullable `cf_scheduled_time: Mapped[int \| None] = mapped_column(BigInteger, nullable=True)` column to `audit_events`; (3) widens the `plaid_item` status CHECK constraint from `('active', 'disconnected')` to `('active', 'disconnected', 'pending_oauth', 'abandoned')`. SQLite does not enforce VARCHAR length, but the model field defines what code generates and what rollback re-inserts — without these three changes, a rollback during the soak window would fail attempting to re-insert cloud-written rows. **This migration MUST run before the cutover window opens and before merge step 1.** After applying, record the revision ID: `alembic current 2>/dev/null | tee .qpipeline/lm-t0-revision.txt`. Pre-cutover checklist verifies `alembic current` shows LM-T0 as head. | Alembic upgrade + downgrade round-trip on a fixture DB: upgrade applies all three changes; downgrade reverts `changed_by` back to `String(8)`, removes `cf_scheduled_time`, and reverts `plaid_item` status CHECK back to `('active', 'disconnected')` — all without errors. Upgrade assertions: (a) `changed_by` column accepts 21-char actor strings via INSERT test; (b) `cf_scheduled_time` column is present and nullable via `sqlite3 data/accounting.db "PRAGMA table_info(audit_events)"` showing the column; (c) `plaid_item` status CHECK accepts `'pending_oauth'` and `'abandoned'` via dry-run INSERT inside a SAVEPOINT that is then ROLLBACK-ed; (d) `alembic current` shows LM-T0 as head; (e) `.qpipeline/lm-t0-revision.txt` is non-empty. |
| LM-T01 | **POST-SOAK, DESTRUCTIVE Alembic migration** in `src/db/alembic/versions/wealth_post_cutover_xxx.py`: drops `audit_events.entity_id` and `entity_type`; tightens `transaction_id` back to NOT NULL; drops Plaid tables; drops `account.plaid_item_id` and `plaid_account_id`. **Real downgrade required.** Migration runs the `SELECT COUNT(*) FROM audit_events WHERE entity_id IS NOT NULL` pre-check at start; if non-zero, raises and refuses to apply. LM-T01 MUST NOT run until `cutover-complete.flag` is present (post-soak). | Alembic upgrade + downgrade round-trip on a fixture DB; refuses to upgrade if any entity-mode AuditEvent or any Plaid Item rows exist. |
| LM-T02 | (REQ-WC-015) Remove `plaid_router` and `brokerage_router` from `src/api/main.py`. Delete `src/api/routes/plaid.py`, `src/api/routes/brokerage.py`. Smoke test: `GET /api/brokerage/networth` returns 404; `GET /api/plaid/items` returns 404. | Integration test asserts 404. |
| LM-T03 | Remove `dashboard/src/routes/brokerage/*` EXCEPT `dashboard/src/routes/brokerage/transactions/` (the `/brokerage/transactions` page is preserved during this migration — its cloud port is deferred to a follow-up; removing it now would create a feature gap). Also remove `dashboard/src/routes/admin/*`. Note: `dashboard/src/routes/admin/connections/` is at the dashboard's `admin/` path (sibling to `brokerage/`), not under `brokerage/`. The `rm -rf dashboard/src/routes/admin/*` command covers this — do not skip it. After removing routes, run `npm run build` for the local dashboard and assert it completes without errors before restarting. | REQUIRED hard red-green gate: `npm run build` completes without errors (exit code 0) AND `curl -fsS localhost:5173/brokerage/transactions | grep -q '<table'` returns 0 (non-empty table present). Both must pass — failure of either blocks LM-T03 completion. `curl localhost:5173/brokerage` returns 404 for all removed routes. |
| LM-T04 | **Remove the Plaid stale-Item section from `scripts/weekly-pl-report.py`** (REQ-WC-007 — the wealth Workers alert replaces it). Delete `_check_plaid_stale_items` function + the section-emission code. | Unit test: report output no longer mentions Plaid; pre-existing weekly P&L tests still pass. |
| LM-T05 | Unload AND delete `com.sparkry.plaid-balance-sync.plist` and `com.sparkry.accounting-prices-daily.plist`. `launchctl unload ...` + `rm ~/Library/LaunchAgents/com.sparkry.plaid-balance-sync.plist ~/Library/LaunchAgents/com.sparkry.accounting-prices-daily.plist`. Repo's copies of the plist files also deleted (if not already `git rm`-ed by the acct/local-migration branch). Update CLAUDE.md operational section. | `launchctl list \| grep plaid` returns empty; `launchctl list \| grep accounting-prices` returns empty; `ls ~/Library/LaunchAgents/com.sparkry.plaid-*.plist ~/Library/LaunchAgents/com.sparkry.accounting-prices-daily.plist 2>&1 \| grep -q 'No such file'` passes (both paths have `~/Library/LaunchAgents/` prefix); verify services are not in launchd disabled-jobs database: `launchctl print-disabled user/$(id -u) \| grep -E 'plaid\|accounting-prices'` returns empty; optional full reboot test post-soak. |
| LM-T06 | `cloudflared tunnel delete plaid-oauth-return`. Update `docs/operational/plaid-oauth-tunnel.md` to mark it decommissioned: add at the top of the file `> DECOMMISSIONED: <date>. Tunnel deleted in cutover step 9b. OAuth-return is now https://internal.sparkry.ai/wealth/desk/connections/oauth-return.` | `cloudflared tunnel list` does not show `plaid-oauth-return`; `docs/operational/plaid-oauth-tunnel.md` contains the decommission notice at the top. |
| LM-T07 | Anti-hallucination: capture every removal-confirmation command's output with SHA-256; validator SUBSTANTIATES. | Validator pass. |
| LM-T08 | Internal /qreview. | Zero P0/P1. |

**Order constraint:** LM tasks DO NOT run until cutover is verified successful. **Mechanism:** the orchestrator writes the flag file `.qpipeline/cutover-complete.flag` (fixed well-known path; no PROJECT_ID interpolation) after Section 4 step 7l (post-soak gate, after the 7j soak, then the 7k confirmatory smoke-test, then writing the flag in 7l). The flag write is the final action of the soak gate; LM tasks must not begin before it is present. **Exception: LM-T0 (the pre-cutover non-destructive Alembic migration) runs before the flag is present** — it is the LM team-lead's first action upon spawn, not gated by the flag. All other LM tasks (LM-T01 through LM-T08) are gated by the flag. The LM team-lead's polling loop (after LM-T0 completes) is:

```bash
until test -f ".qpipeline/cutover-complete.flag"; do
  # Max 168h total wait (soak window up to 7 days per extended-soak protocol).
  if [ "$(date +%s)" -gt "$DEADLINE_EPOCH" ]; then
    echo "LM-worktree timeout — escalating to orchestrator" >&2
    exit 2
  fi
  sleep 300
done
```

The orchestrator passes `DEADLINE_EPOCH` not as a shell env var but as a quoted constant embedded in the LM team-lead's instruction text at spawn time (e.g., "DEADLINE_EPOCH is 1747900800" — 168 hours / 7 days after cutover start, matching the REQ-WC-014 DEADLINE_EPOCH of 168 hours). Example spawn message: "The DEADLINE_EPOCH is `1747900800`. The flag file is at `.qpipeline/cutover-complete.flag`." This avoids shell interpolation issues. On timeout the team-lead returns to the orchestrator with status `timeout`; the orchestrator runs the sub-team protocol to decide whether to extend the soak or escalate to the user.

### Worktree: acct/importer-cloud

Independent. Can start whenever.

| Task | Description | Test seed |
|---|---|---|
| IC-T00 | Pre-condition check: verify Doppler has the keys (`doppler secrets --only-names | grep WEALTH`). These were generated in M0b — this task asserts they are present before cloud-mode adapter work begins. | `WEALTH_API_BASE`, `WEALTH_INTERNAL_KEY`, `WEALTH_TARGET_DEFAULT` all listed. |
| IC-T01 | Shared `_post_to_wealth(payload, source)` helper in `src/adapters/_shared/wealth_client.py`. Reads `WEALTH_API_BASE`, `WEALTH_INTERNAL_KEY`, AND `WEALTH_TARGET_DEFAULT` from Doppler. | Unit test: mock requests; assert payload shape + X-Internal-Key header. |
| IC-T02 | (REQ-WC-012, REQ-WC-015, REQ-WC-019) Add `--target {local|cloud}` to: `xlsx_savings_plan.py`, `brokerage_csv.py`, `vanguard_csv.py`, `fg_pdf.py`, `nw_mutual_xlsx.py`, `gsk_pdf.py`, `ft_pdf.py`. **Default behavior** = read `WEALTH_TARGET_DEFAULT` from env (Doppler) with fallback `local`. Setting `WEALTH_TARGET_DEFAULT=cloud` in Doppler post-cutover flips every adapter atomically; no code change needed. **WEALTH_INTERNAL_KEY rotation KV test:** mock the Workers KV `WEALTH_KV` with a `key_rotation:WEALTH_INTERNAL_KEY` entry where `rotated_at_epoch_ms` is 6 minutes ago; present the old key; assert the handler returns 401 (old key unconditionally rejected after 5 minutes). This test validates the Workers KV rotation mechanism from REQ-WC-019. | Each adapter: local-mode tests unchanged; new cloud-mode tests assert POST is made; integration test with Workers mock; KV rotation test: old key at t+6min → 401. |
| IC-T03 | Anti-hallucination: capture both --target=local and --target=cloud test outputs with SHA-256; validator SUBSTANTIATES that the cloud test really makes a network call (mock observes it AND the X-Internal-Key header was sent). | Validator pass. |
| IC-T04 | Internal /qreview. | Zero P0/P1. |

---

## 4. Merge order + cutover sequence

Once all worktrees finish their internal review-loops and have green PRs:

1. **Merge `feat/wealth-d1-schema`** to crm/main (this branch owns `worker.ts`'s cron registrations). **PREREQUISITE:** verify `alembic current` shows LM-T0 as head before running `wrangler d1 migrations apply` or merging `feat/wealth-d1-schema`. If LM-T0 has not been applied to the local accounting DB, halt — do not proceed with cutover. Run D1 migrations: `wrangler d1 migrations apply sparkry-crm-staging --remote` first, then `sparkry-crm-prod --remote`.
2. **Merge `feat/wealth-workers-plaid`** to crm/main. The PR's `worker.ts` edits replace the Plaid-handler stubs (two lines: `case "7 10 * * *"` and `case "0 14 * * MON"`) with real imports + calls. Merge conflict with workers-brokerage is impossible because the two PRs edit DIFFERENT case-branches within worker.ts's scheduled() handler.
3. **Merge `feat/wealth-workers-brokerage`** to crm/main. The PR's `worker.ts` edits replace the Twelve Data and R2 backup handler stubs (two lines: `case "30 7 * * *"` and `case "0 12 * * *"`). Wrangler auto-deploys after the merge. Smoke-test every endpoint via authenticated curl + the `X-Internal-Key` header for internal routes.
4. **Merge `feat/wealth-frontend-brokerage`** then **`feat/wealth-frontend-desk`** to crm/main. Pages auto-deploys. Visit `https://internal.sparkry.ai/wealth/` — confirm renders.
5. **Merge `feat/wealth-importer-cloud-mode`** to accounting/main. Doppler secrets in place (M0b set them). Run `launchctl unload com.sparkry.accounting-api.plist && launchctl load com.sparkry.accounting-api.plist` to restart the local API. Post-restart health check (mandatory): `sleep 5; curl -fsS http://localhost:8000/api/brokerage/networth > /dev/null || { echo "Local API brokerage degraded after restart — halt and investigate"; exit 1 }`. **WARNING: do NOT manually run any importer with `--target cloud` until step 7i has flipped `WEALTH_TARGET_DEFAULT=cloud`.** The cloud ingest endpoints exist but D1 prod data is not migrated yet; pre-step-7i `--target cloud` usage writes orphaned rows in D1 that lack SQLite counterparts and will appear as duplicates after migration.
6. **Staging dry-run cutover** (BEFORE production cutover):
   a. Take a fresh `sqlite3 data/accounting.db ".backup data/accounting.staging-dryrun-$(date -u +%Y%m%dT%H%M%SZ).db"` immediately before the staging dry-run; pass it as `--sqlite-path` input to the migration script. This ensures the test exercises near-cutover data distribution, not stale data.
   a-pre. Drop and re-apply Drizzle migrations on `sparkry-crm-staging` to ensure a clean slate before the migration script runs: `wrangler d1 execute sparkry-crm-staging --remote --command "<SQL to drop all wealth tables>"; wrangler d1 migrations apply sparkry-crm-staging --remote`.
   b. Migrate the fresh snapshot to `sparkry-crm-staging` via `npx tsx scripts/migrate-from-sqlite.ts --apply --target staging --sqlite-path <fresh-snapshot-path>`. After migration, run a row-count spot-check for each table before proceeding to 6c rollback.
   c. Run `rollback-from-d1.ts` to round-trip back to SQLite; assert byte-identical decimal columns.
   d. Validator SUBSTANTIATES the round-trip.
   e. Deploy workers to the `sparkry-crm-staging` Pages environment (or use `wrangler dev` with the `sparkry-crm-staging` D1 binding). Curl all 13 brokerage endpoints using the CF Service Token (`curl -H "CF-Access-Client-Id: $CF_SMOKE_TOKEN_ID" -H "CF-Access-Client-Secret: $CF_SMOKE_TOKEN_SECRET"`); compare against M0j golden output. Validator SUBSTANTIATES the diff is empty per endpoint.
7. **Production cutover window** (this is the HARD HALT — see Section 7):
   a. Local SQLite snapshot: `sqlite3 data/accounting.db ".backup data/accounting.pre-cutover-$(date -u +%Y%m%dT%H%M%SZ).db"`. Verify > 1 MB. (M0k already did one; this is a fresh second copy at the cutover boundary.)
   b. Before stopping services, verify no Plaid OAuth flow is in progress: `sqlite3 data/accounting.db "SELECT COUNT(*) FROM plaid_item WHERE status='pending_oauth' AND state_nonce_expires_at > datetime('now')"` must return 0. If non-zero, wait for the flow to complete or expire (30 min) before proceeding. **Symlink check:** `if [ -L ~/Library/LaunchAgents/com.sparkry.plaid-balance-sync.plist ]; then echo "WARNING: LaunchAgents plist is a symlink. If the repo copy was git-rm-ed by acct/local-migration, the symlink is now broken. Restore from pre-cutover commit before unloading."; fi`. Then unload local Plaid sync: `launchctl unload ~/Library/LaunchAgents/com.sparkry.plaid-balance-sync.plist`. Note: the repo copy of this plist may already be `git rm`-ed by acct/local-migration; the `~/Library/LaunchAgents/` copy is the one being unloaded here. After unloading, stop the cloudflared tunnel (do NOT delete — preserves rollback config): `sudo cloudflared service stop; sleep 2; if pgrep -x cloudflared > /dev/null; then pkill -x cloudflared; sleep 1; fi; pgrep -x cloudflared > /dev/null && { echo "WARNING: cloudflared still running"; exit 1 } || echo "tunnel stopped"`. The tunnel config stays for the rollback window; the tunnel connection is closed. The tunnel config is deleted in step 9b post-soak.
   c. D1 snapshot: `wrangler d1 export sparkry-crm-prod --remote --output prod-pre-migration.sql` (flag order: `--remote --output`, matching D1-T00's verified syntax; database name is a positional argument). Push to R2: `wrangler r2 object put sparkry-crm-backups/wealth-precutover-$(date -u +%Y%m%dT%H%M%SZ).sql --file prod-pre-migration.sql`. Verify: `wrangler r2 object list sparkry-crm-backups --prefix wealth-precutover` — the uploaded object must appear.
   c-pre. **Pre-migration row count capture:** for each of the 13 wealth tables, run `sqlite3 data/accounting.db "SELECT COUNT(*) FROM <table>"` and capture all results to `.qpipeline/pre-migration-counts.txt`. Run `shasum -a 256 .qpipeline/pre-migration-counts.txt` and record the hash. Validator SUBSTANTIATES the file is non-trivially populated (all 13 tables have counts listed, none are blank) before proceeding to step 7d. This file is used in step 7e row-count validation.
   d. Run the migration script from the **sparkry-crm** repo (TypeScript per D1-T03), chaining both Doppler contexts WITHOUT intermediate shell re-export: `doppler run --project accounting --config dev -- doppler run --project accounting --config prd -- npx tsx scripts/migrate-from-sqlite.ts --apply --target prod`. Verify via `ps -e -o command | grep tsx` during a dry-run that no env value appears on the command line. Reads local SQLite via `better-sqlite3`; decrypts Fernet tokens via npm `fernet` + `PLAID_FERNET_KEY` from accounting Doppler; re-encrypts via Web Crypto AES-GCM + `PLAID_TOKEN_ENC_KEY_MIGRATION` from `accounting/prd` Doppler (mirror set at M0c, NOT the runtime `PLAID_TOKEN_ENC_KEY` Workers Pages secret); emits INSERT-only SQL; uploads via `wrangler d1 execute --remote`. The script asserts both `PLAID_FERNET_KEY` and `PLAID_TOKEN_ENC_KEY_MIGRATION` are present before processing any rows. Records row counts per table.
   e. **Anti-hallucination row-count validation:** validator agent compares pre-migration SQLite row counts (captured before step 7d) vs post-load D1 row counts (queried after step 7d via `wrangler d1 execute "SELECT 'table_name', COUNT(*) FROM ..."`). SUBSTANTIATES exact match per table.
   f. **Value-level spot-check (54 rows total):** for each of the 6 decimal-bearing tables (`position_snapshot`, `plaid_account_balance_snapshot`, `cost_basis_lot`, `historical_price`, `brokerage_transaction`, `realized_gain_loss`), sample exactly 9 rows: 5 random rows PLUS 4 targeted edge-case rows — (1) a value ending in `.50` (trailing-zero preservation test), (2) a value with maximum fractional places (e.g., `0.12345678`), (3) a value of `0.00`, (4) the row with the largest absolute value. Compare canonical decimal-string TEXT from SQLite vs round-tripped value from D1. Validator SUBSTANTIATES byte-identity across all 6 tables × 9 rows = 54 rows total. Also assert no `-0` appears in any TEXT column (must be normalized to `0`).
   g. **13-endpoint schema-only golden comparison (immediately post-migration, before soak).** Curl all 13 brokerage endpoints AND the Plaid routes using the CF Service Token (`curl -H "CF-Access-Client-Id: $CF_SMOKE_TOKEN_ID" -H "CF-Access-Client-Secret: $CF_SMOKE_TOKEN_SECRET"`) provisioned in M0h. For ALL endpoints (brokerage and Plaid): **schema-only comparison** against M0j golden output — response shape, field names, types, and immutable fields (brokerage: `account_id`, `broker`, `account_type`; Plaid: `item_id`, `institution_id`, `status`) must match. Value differences from data added after M0j capture are expected and are NOT failure conditions. For Plaid endpoints (`/wealth/desk/connections`, `GET /wealth/desk/api/plaid/reconciliation/summary`, `/wealth/desk/api/plaid/items`): additionally, do NOT value-diff since PLAID_ENV was sandbox during M0j capture. The validator SUBSTANTIATES the schema diff is empty (no missing or extra fields, no type changes) per endpoint. Eyeball comparison is NOT an acceptable substitute. Migration-induced regressions must be caught here before the soak window starts. **NOTE: the Service Token (`CF_SMOKE_TOKEN_ID` / `CF_SMOKE_TOKEN_SECRET`) was created immediately before step 7g (not at M0h) per the Service Token lifetime restriction — see step 7g-pre below.**

   **7g-pre — Create CF Access Service Token immediately before step 7g.** Access dashboard → Service Auth → Service Tokens → Create. Name: `wealth-smoke-test`. Store Client ID as `CF_SMOKE_TOKEN_ID` and Secret as `CF_SMOKE_TOKEN_SECRET` in accounting Doppler (`--project accounting --config dev`). Verify the token can reach `https://internal.sparkry.ai/wealth/networth` before proceeding. The Service Token is valid only for the cutover window. It is revoked at step 9f (immediately after step 7k confirmatory smoke-test completes — see P1-19).
   h. **STOP** — Confirm that the Plaid redirect URI registered in M0h (`https://internal.sparkry.ai/wealth/desk/connections/oauth-return`) is active (not pending review) in BOTH the Plaid sandbox AND production dashboards. Log into dashboard.plaid.com and screenshot both, showing the URI in the allowed-redirect-URIs list with an active (not pending) status. Reply **REDIRECT-URIS-CONFIRMED** when done. The orchestrator MUST NOT proceed to step 7i until this reply is received. **Do NOT add the URI here — it was already registered at M0h.** Do NOT remove the old tunnel URL yet (rollback safety, removed in step 9b).
   i. Flip PLAID_ENV from sandbox to production on BOTH Pages and cron Worker (confirm PLAID_ENV is currently `sandbox` before this step): `printf 'production' | wrangler pages secret put PLAID_ENV --project-name sparkry-crm` AND `printf 'production' | wrangler secret put PLAID_ENV --name sparkry-crm-cron`. Then set `WEALTH_TARGET_DEFAULT=cloud` in Doppler (`doppler secrets set WEALTH_TARGET_DEFAULT=cloud --project accounting --config dev`). Importers now default to cloud target. **Record the cutover timestamp immediately after the env flip:** `CUTOVER_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ); mkdir -p .qpipeline; echo "$CUTOVER_TS" > .qpipeline/cutover-timestamp.txt`. Validator SUBSTANTIATES that `.qpipeline/cutover-timestamp.txt` exists and is non-empty. Step 7j's soak queries use `<cutover_timestamp>` placeholder — replace with the value from `.qpipeline/cutover-timestamp.txt`.
   j. **Soak window:** minimum 3 calendar days from cutover step 7i AND 3 consecutive successful `plaid-balance-sync` IngestionLog rows — whichever is later. Any cron run with `status='error'` resets the consecutive-success count to zero. If 3 consecutive successes have not occurred by day 3, extend soak 1 calendar day at a time (maximum 7 days total). The LM team-lead DEADLINE_EPOCH is set to 168 hours (7 days) from cutover start. **If cutover occurs after Thursday UTC, extend the soak minimum to include at least 1 business weekday cron run (Mon-Fri UTC) to validate against normal-weekday Plaid API behavior, not just weekend maintenance windows.** Confirm soak status with TWO assertions:
(1) `wrangler d1 execute sparkry-crm-prod --remote --command "SELECT DATE(run_at) AS sync_date, COUNT(*) FROM ingestion_log WHERE source='plaid-balance-sync' AND status='success' AND run_at > '<cutover_timestamp>' GROUP BY DATE(run_at) ORDER BY sync_date DESC LIMIT 3"` — assert result has exactly 3 rows (3 distinct calendar dates). Use the timestamp from `.qpipeline/cutover-timestamp.txt` (written at step 7i) in the format `YYYY-MM-DDTHH:MM:SSZ`.
(2) **Gap-day check:** `wrangler d1 execute sparkry-crm-prod --remote --command "SELECT COUNT(*) FROM ingestion_log WHERE source='plaid-balance-sync' AND status='error' AND run_at BETWEEN (SELECT MIN(run_at) FROM ingestion_log WHERE source='plaid-balance-sync' AND status='success' AND run_at > '<cutover_timestamp>') AND (SELECT MAX(run_at) FROM ingestion_log WHERE source='plaid-balance-sync' AND status='success' AND run_at > '<cutover_timestamp>')"` — this count MUST equal 0. A non-zero count means an error run occurred between the first and last success (gap day), which resets the consecutive-success counter. Soak is NOT satisfied if this check returns non-zero. Also confirm Travis can complete end-to-end Add Connection in production. Note: BOTH `PLAID_TOKEN_ENC_KEY_MIGRATION` and `PLAID_FERNET_KEY` are retained for the FULL rollback window — 30 calendar days after cutover step 7i OR explicit rollback-window-closed sign-off, whichever is later. Neither key is deleted at step 8 merge. Both are deleted together at step 9e. `PLAID_TOKEN_ENC_KEY_MIGRATION` retention is required because the rollback script must AES-GCM-decrypt D1 ciphertexts before re-encrypting to Fernet.

   **7k — Post-soak 13-endpoint confirmatory smoke-test (run AFTER the soak in 7j, BEFORE writing the flag in 7l):** curl all 13 brokerage endpoints (11 GET, PATCH `/accounts/{id}`, PUT `/accounts/{id}/tags`) via the cloud URL using the CF Service Token (`curl -H "CF-Access-Client-Id: $CF_SMOKE_TOKEN_ID" -H "CF-Access-Client-Secret: $CF_SMOKE_TOKEN_SECRET"`) and assert all return HTTP 200 with non-empty body (not a full golden diff — that happened in step 7g). For non-GET routes, send a no-op test payload (PATCH with current value, PUT with current tag list) and assert 200. All 13 must return 200 before writing the cutover-complete flag.
   **7l — Write cutover-complete flag (run AFTER 7k smoke-test passes):** `mkdir -p .qpipeline && touch .qpipeline/cutover-complete.flag`. Validator SUBSTANTIATES via `test -f .qpipeline/cutover-complete.flag && echo OK`. Only then proceed to the DECOMMISSION-APPROVED gate.

   **DECOMMISSION-APPROVED gate (STOP — runs BEFORE step 8):** The `feat/wealth-migration-local` branch contains a destructive Alembic migration (LM-T01 drops Plaid tables). This MUST NOT merge until confirmed safe. Reply **DECOMMISSION-APPROVED** to proceed. The orchestrator MUST NOT proceed to step 8 until this reply is received. Conditions to verify before the gate:
   - The cutover-complete flag is present: `test -f .qpipeline/cutover-complete.flag` (written at 7l).
   - The soak window is fully closed (7j complete with 3 consecutive successes, ≥3 calendar days).
   - End-to-end Add Connection in production succeeded per step 7j.
   - Rollback window has NOT been invoked (no rollback in progress).

8. **Merge `feat/wealth-migration-local`** to accounting/main. Local brokerage routes + Plaid routes deleted; AuditEvent schema rollback applied. Weekly P&L Plaid section stripped. NOTE: This merge runs AFTER the DECOMMISSION-APPROVED gate above — do not merge before the gate passes.
9. **Decommission** (post-soak):
   a. **STOP (already gated — DECOMMISSION-APPROVED received before step 8 above).** Remove the OLD tunnel URL from Plaid dashboard (sandbox AND production). Screenshot. Do NOT proceed if rollback is still possible (i.e., if the cutover-complete flag has not been written).
   b. `cloudflared tunnel delete plaid-oauth-return`. Verify with `cloudflared tunnel list`.
   c. Mark the local Plaid plist and yfinance plist as gone in `CLAUDE.md` operational section. Commit.
   d. Validator SUBSTANTIATES each decommission step from raw output.
   e. **Remove migration-only secrets (BOTH keys, same gate).** Both `PLAID_TOKEN_ENC_KEY_MIGRATION` and `PLAID_FERNET_KEY` are retained for the full rollback window. Delete BOTH at step 9e ONLY after **30 calendar days after cutover step 7i** OR explicit rollback-window-closed sign-off (whichever is later). Do NOT delete either key earlier — `PLAID_TOKEN_ENC_KEY_MIGRATION` is needed to AES-GCM decrypt D1 ciphertexts if rollback is required; `PLAID_FERNET_KEY` is needed to re-encrypt back to Fernet for the local Python app.
   Delete sequence:
   ```bash
   doppler secrets unset PLAID_TOKEN_ENC_KEY_MIGRATION --project accounting --config prd
   doppler secrets unset PLAID_FERNET_KEY --project accounting --config dev
   ```
   Verify both are gone:
   ```bash
   doppler secrets --only-names --project accounting --config prd | grep PLAID_TOKEN_ENC_KEY_MIGRATION  # must return empty
   doppler secrets --only-names --project accounting --config dev | grep PLAID_FERNET_KEY  # must return empty
   ```
   Validator SUBSTANTIATES from captured output of both verify commands.
   f. **Verify the `wealth-smoke-test` Cloudflare Access Service Token is already revoked.** The token was revoked immediately after step 7k completed (immediately after the confirmatory smoke-test, before writing the cutover-complete flag). Step 9f confirms the revocation is reflected in the Access dashboard: Access dashboard → Service Auth → Service Tokens → verify `wealth-smoke-test` is NOT listed as active. If still active (revocation did not happen at step 7k), revoke now via Access dashboard → Revoke. Capture the dashboard screenshot as evidence. If periodic smoke tests are needed post-cutover, provision a new narrower token with a short expiry at that time — do NOT reuse the cutover token.

---

## 4.5 Emergency rollback sequence

Use this sequence if the soak window reveals a critical defect requiring rollback to local SQLite. Prerequisites: (a) `feat/wealth-migration-local` (acct/local-migration) MUST NOT have been merged — if it was merged, rollback is not possible. (b) LM-T05 MUST NOT have run (the local LaunchAgents plists must still be present). (c) LM-T01 MUST NOT have run (the local SQLite Plaid tables must still be present).

0. **Pre-rollback safety gate:** Both AES-GCM and Fernet keys must be present, AND the local SQLite schema must be LM-T0-ready to accept rolled-back D1 rows:
   ```bash
   # Check 1: Both encryption keys must be present
   doppler secrets --only-names --project accounting --config dev | grep -q PLAID_FERNET_KEY || { echo "STOP: rollback impossible without PLAID_FERNET_KEY — Fernet re-encryption will fail"; exit 1 }
   doppler secrets --only-names --project accounting --config prd | grep -q PLAID_TOKEN_ENC_KEY_MIGRATION || { echo "STOP: rollback impossible without PLAID_TOKEN_ENC_KEY_MIGRATION — AES-GCM decrypt of D1 ciphertexts will fail"; exit 1 }
   
   # Check 2: LM-T0 must be applied — verify plaid_item CHECK accepts pending_oauth and abandoned
   sqlite3 data/accounting.db "PRAGMA table_info(plaid_item)"
   # Then run a dry-run INSERT to verify the CHECK constraint accepts the new status values:
   sqlite3 data/accounting.db "SAVEPOINT lm_t0_check; INSERT INTO plaid_item (id, status) VALUES ('check-id', 'abandoned'); ROLLBACK TO lm_t0_check; RELEASE lm_t0_check" || { echo "STOP: plaid_item CHECK rejects 'abandoned' — LM-T0 was not applied; rollback will fail re-inserting D1 rows"; exit 1 }
   ```
   Do NOT proceed if any check fails. All three checks verify: (a) AES-GCM decrypt key available; (b) Fernet re-encrypt key available; (c) local SQLite schema is LM-T0-compliant (will accept rolled-back D1 rows with pending_oauth/abandoned status).
1. Run `npx tsx scripts/rollback-from-d1.ts --apply` (from sparkry-crm repo; exports current D1 state including post-cutover cron rows, re-encrypts AES-GCM tokens back to Fernet using `PLAID_FERNET_KEY` from accounting Doppler, restores to local SQLite).
2. Flip `WEALTH_TARGET_DEFAULT=local` in accounting Doppler: `doppler secrets set WEALTH_TARGET_DEFAULT=local --project accounting --config dev`.
3. Flip `PLAID_ENV=sandbox` on both Pages and cron Worker: `printf 'sandbox' | wrangler pages secret put PLAID_ENV --project-name sparkry-crm` AND `printf 'sandbox' | wrangler secret put PLAID_ENV --name sparkry-crm-cron`.
4. **Pre-step-4 plist check:** `[ -f ~/Library/LaunchAgents/com.sparkry.plaid-balance-sync.plist ] || { echo "STOP: plist missing — restore from repo at pre-cutover commit before reloading"; exit 1 }`. Then reload: `launchctl load ~/Library/LaunchAgents/com.sparkry.plaid-balance-sync.plist`.
5. Restart cloudflared tunnel: `cloudflared service start` (or restart the tunnel process).
5b. Re-register the OLD cloudflared tunnel URL as an allowed redirect URI in BOTH the Plaid sandbox AND production dashboards. Screenshot both dashboards showing the old tunnel URL in the allowed-redirect-URIs list with an active status. Do NOT remove the new Cloudflare URL (`https://internal.sparkry.ai/wealth/desk/connections/oauth-return`) until the OLD tunnel URL is confirmed active in both dashboards.
6. Smoke-test local endpoints: `curl http://localhost:8000/api/plaid/items` and `curl http://localhost:8000/api/brokerage/networth` — both must return non-404.
7. Verify `PLAID_FERNET_KEY` is still present in accounting Doppler: `doppler secrets --only-names --project accounting --config dev | grep PLAID_FERNET_KEY`. It must be present — do NOT delete it until the 30-day retention gate passes.
8. Notify orchestrator of rollback completion; orchestrator runs sub-team protocol to diagnose the defect before re-attempting cutover.

---

## 5. Anti-hallucination validation-review (the meta-step)

Spawned by every team-lead after every claim of "X passes" / "X complete." The validator is a FRESH sub-agent with:

- The claim text.
- A **file path** to the captured command output (the validator opens the file itself via Read; output is never inlined into the validator's prompt, which would let an agent edit the inlined copy before passing).
- The **expected SHA-256** of that file at capture time (the validator re-hashes and compares).
- The git diff at the time of the claim.

### Capture protocol (independent capture agent, NOT the team-lead)

Self-hashing by the team-lead is not tamper-evident — a hallucinating agent could write fabricated output to the capture file and then hash the fabricated file. The hash matches but the output is fake. To close that gap, the capture is performed by a **separate sub-agent spawned by the orchestrator**, not by the team-lead.

Protocol:

1. The team-lead emits a `VERIFY_REQUEST` envelope containing: the worktree path, the verification command (e.g. `npx vitest run`), the expected outcome (e.g. "all tests pass"), and a stable task ID.
2. The orchestrator spawns a fresh `general-purpose` **capture agent** with that envelope. The capture agent's instructions are: (a) `cd` into the worktree, (b) run the command verbatim with `tee` to a fresh capture file under `.qpipeline/projects/${PROJECT_ID}/captures/${TASK_ID}.txt`, (c) compute its SHA-256, (d) return both path and hash to the orchestrator. The capture agent's system prompt explicitly forbids writing fabricated content to the capture file or skipping the command — but this is **prompt-level enforcement, not architectural sandboxing**. A general-purpose agent in the current Claude agent SDK inherits the full tool set; the runbook does not have a way to revoke the Write tool. The capture agent's compliance is assured by (i) its system prompt being narrow and audit-loggable, (ii) the validator agent independently re-running a quick sanity check (e.g. for a `vitest run` claim, the validator can re-run `vitest list` and confirm the test names asserted in the capture exist on disk; mismatch → REFUTED).
3. The orchestrator forwards `(path, hash)` to the validator agent, also fresh-context.
4. The validator reads the file from path, re-hashes, asserts match, then evaluates the claim against the verbatim content AND (for test-gate validations) runs the independent sanity check above.

The team-lead NEVER touches the capture file. The capture agent and validator agent are separate fresh-context agents, neither of which trusts the team-lead. **Honest threat model:** this pattern blocks the most common hallucination — a team-lead fabricating output in its own context — but does not block a colluding capture agent. Collusion across two fresh-context agents is materially harder than self-fabrication (each agent has independent instructions and no shared state), and the validator's re-run sanity check provides a third independent observation for test-gate claims. **Double-capture is REQUIRED (not optional) for the three highest-stakes validation points:** (1) post-migration D1 row counts (step 7e), (2) constraint-violation test gate (D1-T05), (3) staging rollback round-trip (step 6c). For these, the orchestrator spawns a SECOND independent capture agent and asserts hash match across both. For non-test claims (row counts, secret lists, deploy outputs), the validator independently re-runs the equivalent read-only command — NOT just re-hashes the capture file. For test-gate validations, the validator independently runs `npx vitest list` and compares against the captured test-name list; a name mismatch → REFUTED.

```bash
# Capture agent's bash (executed by the capture agent, not the team-lead):
mkdir -p .qpipeline/projects/${PROJECT_ID}/captures
CAPTURE=.qpipeline/projects/${PROJECT_ID}/captures/${TASK_ID}.txt
cd ${WORKTREE_PATH}
${VERIFICATION_COMMAND} 2>&1 | tee "${CAPTURE}"
shasum -a 256 "${CAPTURE}"
# Stdout from this shasum line is the orchestrator's source of truth for the hash.
```

### Validator prompt (used verbatim by every team-lead)

> You are a fresh-context validator. You have NO knowledge of how the work was done — only the artifacts produced.
>
> You are given:
> - A claim: "{CLAIM}"
> - A capture file path: {CAPTURE_PATH}
> - The expected SHA-256 at capture time: {EXPECTED_HASH}
> - The git diff at the time of the claim: {DIFF}
>
> You MUST:
> 1. Read the capture file from {CAPTURE_PATH}.
> 2. Compute its SHA-256 and compare to {EXPECTED_HASH}. If they differ → REFUTED (hash mismatch).
> 3. Otherwise check whether the raw output substantiates the claim, with line-numbered quotes.
>
> Reply with EXACTLY one of:
> - `SUBSTANTIATED` — quote the specific output lines (with line numbers) that prove the claim.
> - `INCONCLUSIVE` — what specific additional evidence would substantiate it?
> - `REFUTED` — quote the contradicting output lines, OR state the hash mismatch.
>
> Rules (you MUST follow):
> - DO NOT trust the claim text — re-derive every assertion from the output.
> - Quote output VERBATIM with line numbers (e.g. "L42: `0 collected`").
> - **Truncation in a load-bearing line is REFUTED, not INCONCLUSIVE.** If the claim asserts a specific count or status and the captured line is truncated with `...` in the place where the count would be, REFUTE — the claim is not provable from the partial line.
> - Truncation in a non-load-bearing section (e.g. compiler warnings before the test summary) is INCONCLUSIVE; request the un-truncated section.
> - `0 collected` / `no tests ran` where claim says "passing" → REFUTED.
> - `skipped` or `xfail` counts > 0 where claim says "all green" without accounting for them → REFUTED.
> - "File X added" claims where DIFF doesn't show file X → REFUTED.
> - "Coverage Y%" where report shows different number, or no report present → REFUTED.
> - "Screenshot attached at PATH" where PATH does not exist or is < 5 KB → REFUTED.

### When validation fails

- `REFUTED`: the team-lead marks the task as not-done, re-enters the review-loop with the validator's quote as the gap to close. Does NOT proceed to the next task.
- `INCONCLUSIVE`: the team-lead provides the requested additional evidence (re-run the command, capture more output, etc.) and re-submits. If 3 INCONCLUSIVE returns happen for the same task, the team-lead escalates to the orchestrator (and the orchestrator runs the sub-team decision protocol).
- `SUBSTANTIATED`: task is genuinely done. Team-lead advances.

### Convergence circuit-breaker

If a worktree's `/qreview` review-loop hits `--max-rounds 5` with open P0/P1 findings:

1. The team-lead halts internal work and emits a CIRCUIT_BREAKER report to the orchestrator with the unresolved findings.
2. The orchestrator runs the sub-team protocol (Section 1) on the specific findings, treating each as a "should this be re-scoped, deferred, or escalated?" question.
3. Each finding's resolution is one of: `re-scoped-to-P2-with-rationale`, `deferred-to-followup-spec`, `escalated-to-user-with-divergence-summary`.
4. The orchestrator emits a summary of resolutions; if any are `escalated-to-user`, the pre-cutover gate (Section 7) blocks until the user resolves them.

### Required validation points

Every entry below: executing agent uses the capture protocol; validator reads the file via path + verifies SHA-256.

| Validation point | What's validated | Required evidence in capture file |
|---|---|---|
| After every test-gate | Raw pytest/vitest output | Test names + PASSED/FAILED + total count + duration; no `0 collected`; no truncation in count line |
| After every `wrangler d1 execute` | D1 command output | Affected rows count + no error lines; full output, no `...` in result block |
| After every Cloudflare deploy | wrangler deploy output | Deployed URL + worker version + size + binding list |
| After every claim of "REQ-X covered" | git diff + grep result | A test function whose docstring or name references REQ-X (grep output captured) |
| After every screenshot/Playwright run | Screenshot file path + Playwright trace file path | Both files exist and are non-empty (> 5 KB); trace shows the asserted action sequence |
| After every cutover step | Command output + DB row counts | Counts match expectations byte-for-byte |
| After every secret-set | `wrangler pages secret list --project-name sparkry-crm` output | The expected secret name appears in the listing |
| After every Cloudflare Access policy change | Screenshot of the policy page | Allowlist + bypass rule both visible |

The orchestrator's pipeline records every validation as a row in `.qpipeline/projects/<id>/validations.jsonl` so the demo-phase summary can audit them.

---

## 6. Fresh-context bootstrap script

Drop this in the fresh session's terminal:

```bash
# Use test -s (non-zero-size check) NOT cat>/dev/null which silently accepts empty files.
for f in \
  docs/superpowers/specs/2026-05-10-wealth-cloudflare-migration.md \
  docs/superpowers/plans/2026-05-10-wealth-cloudflare-migration-runbook.md \
  docs/superpowers/specs/2026-05-09-plaid-net-worth-integration.md \
  ; do
    # Note: docs/superpowers/specs/2026-05-09-plaid-net-worth-integration.md is required
    # for behavioral reference when porting. If it is missing (e.g., in a fresh clone),
    # the team-lead may substitute the Plaid Phase 1 implementation files directly
    # (src/adapters/plaid_balance.py, src/api/routes/plaid.py, src/utils/plaid_crypto.py)
    # as equivalent behavioral reference — document this fallback if used.
    test -s "$f" || { echo "MISSING OR EMPTY: $f" >&2; exit 1; }
done

# M0 (orchestrator-only prerequisites) runs first — see Section 3 Task M0.
# It appends REQ-WC-001..019 and provisions all Workers Secrets.

# Init the pipeline.
PIPELINE_DRIVER=/Users/travis/.claude/plugins/cache/sparkry-claude-skills/ai-review-toolkit/1.0.0/tools/pipeline-driver.py
python "$PIPELINE_DRIVER" init \
  --preset thorough \
  --artifact docs/superpowers/specs/2026-05-10-wealth-cloudflare-migration.md \
  --requirements requirements/current.md \
  --max-rounds 5

# Set up worktrees (orchestrator creates these once; team-leads operate inside).
cd /Users/travis/SGDrive/dev/accounting
git worktree add ../accounting-wt-local-migration feat/wealth-migration-local
git worktree add ../accounting-wt-importer-cloud feat/wealth-importer-cloud-mode

cd /Users/travis/SGDrive/dev/sparkry-crm
git worktree add ../sparkry-crm-wt-d1-schema feat/wealth-d1-schema
git worktree add ../sparkry-crm-wt-workers-plaid feat/wealth-workers-plaid
git worktree add ../sparkry-crm-wt-workers-brokerage feat/wealth-workers-brokerage
git worktree add ../sparkry-crm-wt-frontend-brokerage feat/wealth-frontend-brokerage
git worktree add ../sparkry-crm-wt-frontend-desk feat/wealth-frontend-desk
```

### Team-lead agent spawning

**Important:** the existing `qralph:qralph-team-lead` agent in the installed plugin (6.12.1) is documented to spawn its own sub-agents as `subagent_type="general-purpose"`. The orchestrator MAY use either:

- `subagent_type="qralph:qralph-team-lead"` (preferred if it can be spawned as a top-level agent from the orchestrator — verify in a smoke test against the installed plugin version), or
- `subagent_type="general-purpose"` with a system prompt that contains the team-lead instructions inline (fallback that's known to work).

The orchestrator runs a one-shot smoke test before spawning the real team-leads: launch a trivial qralph team-lead, have it report back, confirm the agent type resolves. If it fails, fall back to `general-purpose`.

Each team-lead gets:

- The spec + runbook (read-only).
- Its worktree path.
- Its task subset (e.g., `D1-T00..D1-T08`).
- The validator prompt template (Section 5).
- The capture protocol (Section 5).
- A directive to spawn its own sub-team for decisions per Section 1.
- The required `WEALTH_*` and `PLAID_*` env values for local testing (`wrangler dev` reads them from `.dev.vars` which the team-lead writes from M0-provisioned secrets). **IMPORTANT:** `.dev.vars` MUST be in `sparkry-crm/.gitignore` BEFORE any team-lead writes it. The team-lead MUST verify `.dev.vars` is gitignored immediately after writing it: `git check-ignore -v .dev.vars` — if it is not ignored, halt and add it to `.gitignore` before proceeding. CI MUST include a lint step: `git check-ignore .dev.vars` confirms the file is gitignored.

Team-leads run in parallel where dependencies allow. The orchestrator polls each via SendMessage if it needs status, but otherwise lets them work to merge.

---

## 7. Pre-cutover gate (orchestrator-enforced HARD HALT)

**This gate is an EXCEPTION to the sub-team protocol from Section 1.** The cutover is the one irreversible step in the pipeline; the orchestrator MUST stop and wait for explicit human approval. The sub-team protocol does NOT apply here — no simulated persona can authorize cutover.

### Pre-cutover checklist (orchestrator verifies each box; validator SUBSTANTIATES each)

- [ ] LM-T0 pre-cutover Alembic migration applied to local SQLite (`changed_by` widened to `String(64)`, `cf_scheduled_time` column added to `audit_events`, `plaid_item` status CHECK widened to include `pending_oauth` and `abandoned`). Verified via `alembic current` showing LM-T0 as head. Also verify: `sqlite3 data/accounting.db "PRAGMA table_info(audit_events)"` lists `cf_scheduled_time` column (SQLite does not enforce VARCHAR length, but the column must be present for rollback compatibility).
- [ ] All 7 worktrees (d1-schema, workers-plaid, workers-brokerage, frontend-brokerage, frontend-desk, local-migration, importer-cloud) have green PRs **OPENED + APPROVED + READY-TO-MERGE** with zero P0/P1 across their internal review-loops. The `feat/wealth-migration-local` PR (acct/local-migration) is NOT merged here — its merge happens at step 8 post-soak.
- [ ] Every team-lead's validation-review log has zero REFUTED entries and zero open INCONCLUSIVE entries; the orchestrator audits `.qpipeline/projects/<id>/validations.jsonl` and SUBSTANTIATES.
- [ ] A fresh-context `/qreview` has been run over the master spec + every worktree's deliverables; SUBSTANTIATED recorded on each module's "ready" claim.
- [ ] Staging dry-run cutover completed successfully against `sparkry-crm-staging`; `scripts/rollback-from-d1.ts` round-trip verified byte-identical decimal columns. Add 13-endpoint smoke-test against staging (step 6e): deploy workers to staging Pages environment or use `wrangler dev` with sparkry-crm-staging D1 binding; run 13-endpoint curl smoke-test against staging, compare against M0j golden output.
- [ ] All 9 Workers Pages Secrets verified present via `wrangler pages secret list --project-name sparkry-crm`: `PLAID_CLIENT_ID`, `PLAID_SANDBOX_SECRET`, `PLAID_PRODUCTION_SECRET`, `PLAID_ENV`, `PLAID_TOKEN_ENC_KEY` (new AES-GCM), `TWELVE_DATA_API_KEY`, `WEALTH_INTERNAL_KEY`, `WEALTH_ALLOWED_EMAILS`, `RESEND_API_KEY` (inherited from CRM).
- [ ] Cloudflare Access policy screenshot shows zone allowlist + bypass rule for `/wealth/api/internal/*` (no IP allowlist — defense via X-Internal-Key + rate limit only).
- [ ] `.dev.vars` is present in `sparkry-crm/.gitignore`: `git check-ignore -v .dev.vars` returns a match. CI lint step `git check-ignore .dev.vars` passes.
- [ ] `wrangler deployments view sparkry-crm-cron` shows latest deploy includes real handler implementations (not `console.warn` stubs) for all 5 cron handlers. Verify by checking recent `ingestion_log` rows have `status='success'`.
- [ ] `wrangler deploy --dry-run --config wrangler.worker.toml` succeeds with all bindings declared (D1 prod + staging, R2_BACKUPS, WEALTH_KV).
- [ ] PL-T07 Plaid sync cron per-Item CPU benchmark passes (≤10ms via `wrangler dev --test-scheduled "7 10 * * *"` profiling); if not, Workers Paid is documented as required and cost target updated.
- [ ] Plaid dashboard screenshot (sandbox + production) shows the new redirect URI registered AND active (not pending review). The old tunnel URL still present (kept for rollback safety, removed in step 9b post-soak).
- [ ] Local SQLite pre-cutover snapshot exists at `data/accounting.pre-cutover-*.db` and is > 1 MB.
- [ ] D1 production pre-cutover snapshot exists in R2 at `sparkry-crm-backups/wealth-precutover-<ts>.sql`.
- [ ] Local AuditEvent count: `SELECT COUNT(*) FROM audit_events WHERE entity_id IS NOT NULL` returns 0 (verified twice — once at M0l and once now). If non-zero, BLOCK.
- [ ] JWT validation live-test: `curl -H 'Cf-Access-Authenticated-User-Email: travis@sparkry.com' https://internal.sparkry.ai/wealth/api/brokerage/networth` (no valid `Cf-Access-Jwt-Assertion` header) — response MUST be 401. Validator SUBSTANTIATES from captured curl output.
- [ ] R2 backup token live-test (two-token split per REQ-WC-018): verify `R2_BACKUP_WRITE_TOKEN` is listed in `wrangler secret list --name sparkry-crm-cron`; verify the token can WRITE a test object AND that LIST returns 403 / permission-denied AND DELETE returns 403 / permission-denied (confirms WRITE-only). Verify `R2_BACKUP_PRUNE_TOKEN` is provisioned on the separate prune-cron handler; verify it can LIST and DELETE but NOT WRITE. `R2_RESTORE_TOKEN` is listed in `doppler secrets --only-names --project accounting --config dev`.

### HARD HALT directive (enforced at the conversation layer, not the pipeline-driver)

The /qpipeline `pipeline-driver.py` does not natively support phrase-matching sentinel gates. The actual enforcement mechanism is at the orchestrator's CONVERSATION layer:

1. The orchestrator emits the halt prompt **as its only output for that turn** — no other tool calls, no other text.
2. The conversation harness naturally blocks until the user replies. There is no autonomous next turn.
3. When the user reply arrives, the orchestrator checks the verbatim message for the literal phrase `CUTOVER APPROVED`. If matched, it proceeds. If not matched (questions, "wait", "what if X", silence/no message), it restates the prompt and emits again — still as its only output for the new turn.
4. There is no timeout. The orchestrator will keep restating indefinitely until either the exact phrase arrives or the user manually intervenes to cancel the pipeline.

This is achievable WITHOUT pipeline-driver support because the orchestrator IS Claude in a conversation thread — the conversation harness's "wait for user input" semantics provide the gate. The pipeline-driver records the halt as an outstanding gate but otherwise doesn't enforce it; the orchestrator's behavior in conversation is what enforces it.

**M0-pre task:** The literal sentinel string is `SMOKE-TEST PHRASE` (not a placeholder — this is the exact string the user must reply with during the smoke-test). The real cutover gate uses a different literal string: `CUTOVER APPROVED`. Before any team-lead spawns, the orchestrator does a sentinel smoke-test on itself — emit "REPLY: SMOKE-TEST PHRASE" as the only turn output. When the user replies, check for the exact match. Pass the smoke-test before proceeding. This is the orchestrator proving to itself that it knows how to do this. This relies on the M0-pre sentinel mechanism defined in Section 3 Task M0, which must complete before any team-lead spawns. See Section 3 for the full protocol.

```
=== WEALTH MIGRATION CUTOVER REQUIRES YOUR APPROVAL ===

All pre-conditions verified. Ready to:
- Pause local Plaid sync.
- Migrate SQLite → D1 (one-shot, including Fernet→AES-GCM token re-encryption).
- Flip importer default to cloud.
- Smoke-test production /wealth/ endpoints.
- Begin soak window (minimum 3 calendar days, 3 consecutive successful cron runs).

Rollback is available until the soak window closes (3 consecutive cron successes, ≥3 calendar days) via:
  npx tsx scripts/rollback-from-d1.ts --apply

Reply with EXACTLY:  CUTOVER APPROVED
to begin the cutover sequence. Any other reply HALTS until you clarify.
```

After cutover, the demo phase final report lists every Acceptance Criterion from the spec with file-line evidence or a SUBSTANTIATED operational receipt.

---

## 8. What this runbook intentionally does NOT do

- Specify exact file paths inside the CRM repo beyond the `(wealth)` group convention — the team-leads pick paths consistent with the CRM's existing conventions.
- Lock in the historical-price provider — Twelve Data is the default but the spec explicitly allows pivoting to Polygon if quota fails.
- Detail the Drizzle schema field-by-field — D1-T01 generates that from the spec's data model section.
- Pin a UI component library — `(wealth)` can pick something different from CRM if it wants; constraint is "no shared components beyond the layout shell."
- Address Phase 2 Plaid Investments — that's a separate spec entirely.

The orchestrator and team-leads make those calls via the sub-team protocol when they come up.

---

## 9. Estimated effort (revised after round-1 review)

The original estimate underweighted the test corpus port. Revised:

- M0 orchestrator prerequisites: ~8 SP (**16 sub-tasks:** M0-pre, M0-doppler-context, M0-prereqs, M0a, M0b, M0c, M0d-pre, M0d, M0e, M0f, M0g, M0h, M0i, M0j, M0k, M0l; including interactive Twelve Data signup, Chrome MCP Cloudflare Access edit, CF Service Token creation, secret provisioning across 2 systems, golden output capture against local FastAPI, AuditEvent pre-check, region check, sentinel smoke-test). **Parallelism: M0a, M0b, M0c, M0d-pre, and M0h can run in parallel. M0d is blocked on Twelve Data signup (Travis manual action). M0e is blocked on M0b + M0c + M0d completing (provisions all three secrets onto the cron Worker). M0f is independent of M0d. M0g, M0i, M0j, M0k, and M0l run after M0e.** Effective wall-clock with parallelization: ~3 hours.
- crm/d1-schema: ~8 SP (schema + migration scripts + Fernet→AES-GCM re-encryption + worker.ts cron dispatcher + staging DB setup)
- crm/workers-plaid: ~18 SP (Python test corpus port — `test_plaid_routes.py` and `test_plaid_balance.py` together have exactly 45 test functions to port verbatim: 30 in `test_plaid_routes.py` + 15 in `test_plaid_balance.py`)
- crm/workers-brokerage: ~28 SP (13 endpoints; `test_brokerage_routes.py` has 96 test functions — this is the dominant effort; alternatively scope to 5 critical endpoints first and defer 60 tests to follow-up)
- crm/frontend-brokerage: ~10 SP (added 3 panels for top-holdings/recent-transactions/data-integrity)
- crm/frontend-desk: ~6 SP
- acct/local-migration: ~4 SP (added P&L-section strip + tunnel decommission)
- acct/importer-cloud: ~5 SP
- Cutover + decommission + post-cutover smoke: ~5 SP

**Revised total: ~87 SP.** Critical path with maximum parallelism: M0 → d1-schema → max(workers-plaid, workers-brokerage) → max(frontend-brokerage, frontend-desk) → cutover ≈ 3 + 8 + 28 + 10 + 5 = **54 SP serial**.

**Wall-clock estimate:** 4-5 working days with 7 parallel team-leads (5 CRM + 2 accounting), assuming each SP ≈ 30-60 minutes of agent work AND the validation-review meta-step adds ~15% overhead.

**Scope-trim option** (if 4-5 days is unacceptable): scope crm/workers-brokerage's BR-T01 to porting only the 5 most-used endpoints (networth, networth-history, accounts list, accounts/[id]/detail, reconciliation/summary) and defer the other 8 endpoints to a follow-up wealth migration phase.

**Trim-mode signal mechanism.** When trim mode is chosen, the orchestrator writes `.qpipeline/projects/${PROJECT_ID}/scope-trim.flag` containing a JSON list of deferred endpoint slugs. Every dependent team-lead reads this file at startup:

- **BR team-lead:** if file exists, scope BR-T01 to the non-deferred endpoints only. Skip the corresponding internal-ingest route for any deferred endpoint that doesn't have one (most deferred routes are GET endpoints with no ingest counterpart; this guard is for safety).
- **FB team-lead:** if file exists, skip the UI panels for deferred endpoints (the 3 panels in FB-T06 become conditional on `data-integrity`/`top-holdings`/`recent-transactions` NOT being in the deferred list).
- **LM team-lead:** if file exists, LM-T02 becomes a route-by-route edit: keep `brokerage_router` mounted but remove only the route handlers NOT in the deferred list. The deleted-files step (`rm src/api/routes/brokerage.py`) is skipped entirely in trim mode. The dashboard route deletion in LM-T03 similarly preserves deferred-endpoint UI pages.
- **IC (importer-cloud) team-lead:** if file exists, the cloud-mode tests for adapters whose corresponding `/wealth/api/internal/ingest/*` endpoint is deferred (i.e., BR-T03 didn't ship it yet) mock a 404 response and assert the adapter logs a warning and falls back to local mode for that source. The cutover step 7i (set `WEALTH_TARGET_DEFAULT=cloud` in Doppler) is deferred for those adapters until the follow-up phase ships their ingest endpoints; per-adapter `--target` overrides via CLI still work for already-shipped routes.

The flag file format:
```json
{
  "deferred_endpoints": ["top-holdings", "recent-transactions", "data-integrity",
                          "accounts/{id}/PATCH", "accounts/{id}/tags",
                          "holdings/{symbol}/history", "missing-accounts", "realized-gl"],
  "rationale": "scope-trim chosen for 2-3-day target; follow-up phase due within 2 weeks"
}
```

This preserves the "no regressions" promise — Travis can still hit deferred endpoints on the local dashboard until their cloud equivalents ship in the follow-up. Reduces BR scope ~28 → ~12 SP, FB scope ~10 → ~6 SP, LM scope unchanged but with selective preservation. Total critical path: ~38 SP / 2-3 days. Follow-up phase lands within 2 weeks of cutover.
