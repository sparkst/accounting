# Wealth → Cloudflare Migration — Design Spec

**Date:** 2026-05-10
**Owner:** Travis Sparks
**Status:** Planning bundle ready for fresh-context `/qpipeline thorough` execution.
**Sibling:** `docs/superpowers/plans/2026-05-10-wealth-cloudflare-migration-runbook.md`
**Cost target:** Cloudflare free tier for Pages + most Worker handlers. Workers Paid ($5/mo) is **REQUIRED from day one** for: (a) the R2 backup cron (15-minute wall-clock for paginated 50,000+ row `historical_price` exports), (b) the Twelve Data ingest cron (8 req/min pacing × 50 symbols = 6.25 minutes wall-clock per run, which exceeds the 30-second Workers Free limit), and (c) the Plaid sync cron (network latency × N Items exceeds 30-second Workers Free wall-clock with 5+ active Items). The $5/mo Workers Paid cost covers all three crons and the on-demand `/wealth/api/brokerage/quotes` endpoint (REQ-WC-013a, runs on request) and is the operational baseline — not an escape hatch. Workers Paid is also the documented fallback if: (d) the 10 ms CPU/request budget cannot be met on request handlers after index tuning. Supabase is acknowledged as a fallback platform but not selected.

---

## Why this spec exists

The brokerage + Plaid feature set currently lives inside the local cash-basis accounting app on Travis's MacBook (FastAPI + SQLite + SvelteKit, served via launchd + Caddy over Tailscale at `macbook.ancon-cliff.ts.net`). That's fine for the personal cash-basis register, which is single-user, single-machine, and never needs to be public. The brokerage piece is different:

1. **Plaid OAuth banks need a publicly reachable redirect URI.** The current spec mitigates this with a Cloudflare tunnel exposing the local app's `/admin/connections/oauth-return` SvelteKit route. The public CNAME (the actual Plaid redirect URI) is retrieved via `cloudflared tunnel list` before deregistration from Plaid — do not use a placeholder. That works, but it adds a second auth boundary (Tailscale for everything else, public CF tunnel for one path) and adds operational fragility.
2. **The CRM at `https://internal.sparkry.ai` already lives on Cloudflare Pages + D1 + Workers** with Cloudflare Access + Google OAuth. Adding a sibling app under the same domain gives us shared auth and zero new infrastructure.
3. **The brokerage UI is fundamentally read-mostly observational** (net-worth charts, holdings, reconciliation, Plaid status). It does not need to be next to the transaction register. Co-locating it with the CRM aligns better — both are "browser tools Travis uses" rather than "tax-of-record databases on Travis's laptop."

Travis keeps the local accounting app for cash-basis books, P&L, tax exports. The brokerage feature + Plaid integration migrate to a new sibling app at `https://internal.sparkry.ai/wealth`.

## Hard constraints

- **CRM and Wealth share authorization and hosting only.** Same Cloudflare Access zone (covering `internal.sparkry.ai`), same Pages project. They MUST NOT cross-import code, MUST NOT share UI components beyond the layout shell, MUST NOT cross-reference each other in navigation, and MUST NOT share data models. The CRM does not know about brokerage; Wealth does not know about customers/work-orders/invoices.
- **They can look different.** Distinct typography, palette, and component vocabulary are fine and encouraged — Wealth is a Travis-only operator tool; CRM is for Amy.
- **Travis-only access on `/wealth/*`.** Cloudflare Access at the zone level admits both Travis and Amy (the existing CRM allowlist), but the in-app guard on `/wealth/*` (page + API) compares the `Cf-Access-Authenticated-User-Email` header against a `WEALTH_ALLOWED_EMAILS` Workers secret (default: `travis@sparkry.com` only). Amy hitting `/wealth/` gets a 404 from the app even though Cloudflare Access let her into the zone. A 404 (not 403) avoids revealing that the `/wealth/` feature exists to non-authorized users.
- **Operator routes are obfuscated.** Plaid management lives at `/wealth/desk/*`, not `/admin/*`, not `/wealth/admin/*`, and not `/wealth/console/*` (`console` is too admin-signaling). `desk` is non-semantic and unlikely to be probed by scanners targeting admin URLs. The path is auth-gated regardless; the URL is just neutral.
- **The local personal accounting register stays local.** Transactions, classification, invoicing, tax exports, reconciliation, weekly P&L — none of those move. Only the brokerage + Plaid scope migrates. **Exception:** the Plaid stale-Item alert moves entirely to Workers (REQ-WC-007); the section in the local weekly P&L email is stripped during cutover so there is no duplicate alert.
- **Re-do Plaid OAuth registration against the new public URL.** The Cloudflare tunnel for OAuth-return is decommissioned; the new redirect URI is `https://internal.sparkry.ai/wealth/desk/connections/oauth-return`. Both sandbox AND production must be re-registered; a screenshot of each dashboard's allowed-redirect-URIs list is captured as cutover evidence.

## Scope

### What migrates to Cloudflare

| Component | From | To |
|---|---|---|
| Brokerage data models (Account, BrokerageTransaction, PositionSnapshot, RealizedGainLoss) | `src/models/brokerage.py` (SQLite) | D1 tables (TypeScript Drizzle schema) |
| History models (HistoricalPrice, AccountBalanceSnapshot, ExpectedAccount, CostBasisLot, AccountTag) | `src/models/history.py` | D1 tables |
| Plaid models (PlaidItem, PlaidAccountBalanceSnapshot) | `src/models/plaid.py` | D1 tables |
| AuditEvent (with entity_id/entity_type extension) | `src/models/audit_event.py` | D1 tables (scoped to wealth-app events) |
| IngestionLog (per-source ingestion bookkeeping) | `src/models/ingestion_log.py` | D1 tables (wealth-app sources only) |
| Brokerage API routes — all 13 (see Routing section for full list) | `src/api/routes/brokerage.py` (FastAPI) | Workers routes under `/wealth/api/brokerage/*` |
| Plaid API routes (`/api/plaid/*`) | `src/api/routes/plaid.py` (FastAPI) | Workers routes under `/wealth/desk/api/plaid/*` |
| Plaid daily Balance sync | `com.sparkry.plaid-balance-sync.plist` (launchd) | Workers Cron Trigger |
| Brokerage dashboard pages (`/brokerage/*`, `/brokerage/accounts/<id>`) | SvelteKit @ macbook.ancon-cliff.ts.net | SvelteKit on Pages, mounted at `/wealth/*` |
| Plaid operator UI (formerly `/admin/connections` on local dashboard) | local dashboard | `/wealth/desk/connections` |
| Historical price backfill | `scripts/backfill_historical_prices.py` (yfinance) | Workers scheduled job + Twelve Data REST API |
| Daily yfinance price backfill | `com.sparkry.accounting-prices-daily.plist` (launchd) | Workers Cron Trigger (REQ-WC-013) |

### What stays local

- Cash-basis register (Transaction table) — tax-of-record stays on Travis's machine
- Classification engine + VendorRule
- Invoicing
- Tax exports (FreeTaxUSA, B&O)
- Reconciliation engine
- Weekly P&L report
- All Gmail/Stripe/Shopify/bank-CSV adapters
- The dashboard at `macbook.ancon-cliff.ts.net` (minus the brokerage sub-pages, which get removed)

### Out of scope for this migration

- Multi-tenancy (still single user)
- Mobile app
- Real-time / webhooks (still polling)
- Replacement of the Phase 4 PDF/XLSX importers (they stay as local Python scripts that POST to the Cloudflare API)

---

## Architectural decisions

Each decision below was passed through a simulated team — Travis-persona, skeptic, PE, strategic-advisor — until consensus. Disagreement points are noted under the decision.

### A0. Deployment topology

SvelteKit on Cloudflare Pages handles HTTP routes via the SvelteKit adapter (Pages Functions). The cron Worker (`sparkry-crm-cron`) is a SEPARATE deployment with its own bundle, secrets surface, and entry point (`src/worker.ts`). These two deployments are distinct: Pages Functions for HTTP request handling; the cron Worker for scheduled handlers. Secrets must be provisioned separately for each (see REQ-WC-019 cron Worker secrets section). Cross-referencing A1 (Workers) and A2 (Pages) in context: when the spec says "Workers route handler," it refers to Pages Functions invoked by the SvelteKit adapter. When it says "cron handler," it refers to the separate `sparkry-crm-cron` Worker.

### A1. Backend: Cloudflare Workers (TypeScript)

The current Python FastAPI backend gets rewritten in TypeScript on Workers. Reasons:
- Matches CRM stack — one runtime, one deploy pipeline
- D1 binding is native to Workers; running Python on Workers is feature-limited
- Cron Triggers are first-class on Workers

**Skeptic objection:** "That's a lot of rewrite for a recently-shipped Plaid module." Counter: the Plaid module is ~600 LOC; brokerage routes are ~700 LOC. Total surface is small. The Python tests are reference behavior; we port them to Vitest. The Python originals stay in the repo until the cutover is verified.

**Alternative considered:** keep Python backend somewhere else (Fly.io, Railway) and proxy through Workers. Rejected — adds a second deploy target, defeats the "shared hosting" requirement, and the redirect URI hop adds latency to OAuth.

### A2. Frontend: SvelteKit on Cloudflare Pages (mounted at `/wealth`)

The CRM is SvelteKit on Pages. Wealth is the same project — same Pages deployment, same `wrangler.toml`. The two apps are sibling SvelteKit route groups:

- `/(crm)/...` — the existing CRM routes
- `/(wealth)/wealth/...` — new wealth routes

This is the standard SvelteKit group-route pattern. Auth middleware in `hooks.server.ts` applies to both groups. Pages are bundled per route, so the wealth bundle does not load CRM code and vice versa.

**Constraint:** the two groups MUST NOT import from each other. Lint rule (`eslint-plugin-import` `no-restricted-paths`) enforces this in CI.

### A3. Database: Cloudflare D1

D1 is SQLite-compatible. The current schema ports near-1:1 — same tables, same column types (with `INTEGER`/`TEXT`/`REAL` mapping), same CHECK constraints, same UNIQUE indexes. The migration script is a `sqlite3 .dump` filtered to brokerage/plaid/history/audit tables, then a `wrangler d1 execute` import.

**Decimal precision question:** D1 supports `NUMERIC` but it's stored as REAL or TEXT depending on input. The current schema uses `Numeric(18, 4)` for Plaid balances and `Numeric(18, 8)` for quantities/prices. In D1, we store these as TEXT (canonical decimal strings) and convert in TypeScript via `decimal.js`. This preserves the same "Decimal at the JSON boundary" invariant from the Python CLAUDE.md.

**Decimal canonical-string serialization.** All `Numeric(p,s)` columns use a fixed scale: prices and quantities use scale 8 (e.g., `Numeric(18,8)`); Plaid balances use scale 4 (e.g., `Numeric(18,4)`); monetary amounts use scale 2 (e.g., `Numeric(12,2)` and `Numeric(14,2)`). The canonical serialization rules are:
- Python side (migration script, importers): `str(value.quantize(Decimal('0.' + '0'*s)))` where `s` is the column's scale. Python's default rounding mode is `ROUND_HALF_EVEN`. `-0` MUST be normalized to `0` before stringifying.
- TypeScript side (Workers handlers, migration script): at the top of the canonical-decimal helper module: `const D = Decimal.clone({ rounding: Decimal.ROUND_HALF_EVEN })` to create a module-local class. Use `D` for all wealth decimal operations. `Decimal.set()` is FORBIDDEN in the wealth module — it mutates shared global state that affects CRM code paths running in the same isolate. Then `new D(text).toFixed(s)` with `-0` normalized to `0`. A KAT fixture with a `.5`-ending value at the rounding boundary MUST include the value `2.445` quantized to scale 2: under HALF_EVEN = `2.44`, under HALF_UP = `2.45` — this makes the rounding-mode distinction mechanically observable in the KAT output. An ESLint rule (custom or `no-restricted-imports`) within `src/lib/server/wealth/` and `src/routes/(wealth)/` MUST flag any import of `decimal.js` that is not the cloned `D` class from `$lib/server/wealth/decimal.ts`. Direct `new Decimal(...)` calls from a top-level import are flagged.
- The migration script asserts that the TypeScript output string matches the Python quantized string for every fixture row in the KAT.
- The rollback script parses TEXT via `new Decimal(text)` and asserts round-trip equality through Python's `Decimal(str_value)` in the KAT.

**Alternative considered:** integer cents (like the CRM does for invoice amounts). Rejected — brokerage quantities have 8-decimal precision (fractional shares), which doesn't fit the cents model.

### A4. Auth: in-app Google OAuth + HMAC session cookie (shared with CRM)

**Reconciliation (M0h sub-team 2026-05-11, 4/4 consensus):** The original draft of this section assumed Cloudflare Access was deployed at the zone level for `internal.sparkry.ai`. Live inspection during M0h showed CF Access has ZERO applications and ZERO policies configured for this account — the CRM authenticates entirely in-app via Google OAuth + an HMAC-signed session cookie. `Cf-Access-*` headers and the JWKS endpoint are not in play. This section is rewritten to ratify the live CRM auth pattern; the original CF Access design is moved to **TF-002** (deferred follow-up; can be layered on later as defense-in-depth without removing the in-app guard).

**Live CRM auth boundary (the pattern Wealth reuses):**
- `/api/auth/callback` (CRM): initiates Google OAuth, validates Google `userInfo.email` against the `ALLOWED_EMAILS` Pages secret (comma-separated list including Travis + Amy), and on match sets an HMAC-signed `session` cookie using `SESSION_SIGNING_KEY` (Pages secret). Sliding 7-day expiry.
- `hooks.server.ts` (CRM): public paths (`/api/auth`, `/api/webhooks`, `/api/health`) bypass auth. For every other path, the handler reads the `session` cookie, validates its HMAC signature with `SESSION_SIGNING_KEY`, populates `event.locals.user`, and refreshes the cookie with a new 7-day expiry. Missing or invalid cookie → 302 redirect to `/api/auth/callback?action=login`.

**Wealth's additional gate.** Wealth routes (`/wealth/*` pages and `/wealth/api/brokerage/*` + `/wealth/desk/api/*` endpoints) ride the same session cookie. After the CRM-level `ALLOWED_EMAILS` check passes, `hooks.server.ts` performs a second per-path check:

```typescript
if (event.url.pathname.startsWith('/wealth/')) {
  if (!isAllowedEmail(event.locals.user.email, event.platform.env.WEALTH_ALLOWED_EMAILS)) {
    throw error(404);  // 404, not 403 — see below
  }
}
```

`WEALTH_ALLOWED_EMAILS` defaults to `travis@sparkry.com` only (set in M0f). Amy is authenticated at the CRM level but the wealth gate 404s her — a 404 (not 403) avoids revealing the existence of the `/wealth/` feature to authenticated-but-not-authorized users. Every Worker route handler in `(wealth)` also independently checks `WEALTH_ALLOWED_EMAILS` (defense in depth: even if `hooks.server.ts` is bypassed, the route fails closed).

**Internal-ingest endpoint exception.** `/wealth/api/internal/*` (consumed only by the local Python importers) is added to `PUBLIC_PATHS` in `hooks.server.ts` (bypasses the session-cookie check). The route handler enforces both:
- HTTP method MUST be POST; any other method returns 405 (defense against accidental browser hits or scanner probes).
- `X-Internal-Key` header MUST `crypto.subtle.timingSafeEqual` the `WEALTH_INTERNAL_KEY` Pages secret. Missing/wrong header returns 401.

This matches the pattern in the original spec (X-Internal-Key + rate limit) but without relying on CF Access bypass-route configuration. Pages preview URLs (`sparkry-crm.pages.dev/wealth/*`) are covered by the same hooks.server.ts guard — no separate CF Access policy needed.

**KATs (replace the CF Access KATs):**
- Unauthenticated request to `/wealth/networth` → 302 to `/api/auth/callback?action=login` (CRM redirect pattern).
- Authenticated session cookie for `amycsparks@gmail.com` hitting `/wealth/networth` → 404 (Amy is allowlisted at CRM level via `ALLOWED_EMAILS` but rejected at wealth level via `WEALTH_ALLOWED_EMAILS`).
- Authenticated session cookie for `travis@sparkry.com` hitting `/wealth/networth` → 200.
- Forged `session` cookie with valid email but wrong HMAC signature → 302 to login (cookie deleted by `hooks.server.ts`).
- POST to `/wealth/api/internal/ingest/brokerage-csv` with valid `X-Internal-Key` → 200.
- GET to `/wealth/api/internal/ingest/brokerage-csv` → 405 (POST-only).
- POST to `/wealth/api/internal/ingest/brokerage-csv` with wrong `X-Internal-Key` → 401.
- Same as above against `sparkry-crm.pages.dev` preview URL → identical responses (cookie-level guard covers preview).

**Why CF Access is deferred to TF-002:** the in-app guard is already the authoritative auth boundary for the CRM and has been live for ~2 weeks. Adding CF Access on top would introduce a second auth boundary with non-obvious precedence (debugging trap: a 401 could come from either CF Access or the cookie middleware). The Wealth migration's scope is "migrate brokerage to Cloudflare" — not "redesign the CRM's auth boundary." If a third-party access need or stricter trust boundary emerges later, CF Access can be layered on (TF-002).

**Tracked follow-ups from sub-team:**
- **TF-006:** `SESSION_SIGNING_KEY` rotation procedure (Skeptic flag: today there is no rotation story; a leak would compromise both CRM and Wealth simultaneously).
- **TF-007:** Session-cookie HMAC validator pen-test (timing-side-channel, replay, expiry edge cases).
- **TF-002 (existing):** Layer CF Access on top of in-app auth as defense-in-depth, when operational maturity warrants.

### A5. Plaid OAuth-return URL

Old: the Cloudflare tunnel CNAME for `plaid-oauth-return` (run `cloudflared tunnel list` to retrieve the actual subdomain before deregistering from Plaid — do not fill in a placeholder).

New: `https://internal.sparkry.ai/wealth/desk/connections/oauth-return`.

This URL must be registered in the Plaid dashboard (sandbox AND production) before any link flow runs. The old tunnel URL is removed from the allowed-redirects list only after rollback risk is closed (step 9a post-soak). The old `cloudflared` tunnel is decommissioned in step 9b.

### A6. Plaid daily Balance sync: Workers Cron Trigger (UTC scheduling)

`com.sparkry.plaid-balance-sync.plist` → Workers `scheduled()` handler. **Workers Cron Triggers use UTC exclusively** — local-time scheduling is not supported. We pick a UTC time that is far from any DST boundary so the run hour does not jump seasonally:

- **`cron: "7 10 * * *"`** = 10:07 UTC daily
- = 02:07 PST (UTC-8) in winter / 03:07 PDT (UTC-7) in summer
- Both are off-peak; the one-hour seasonal shift is acceptable

REQ-WC-006 and the runbook capture the literal UTC cron expression so no agent re-derives it. Same three-layer error isolation (per-item / per-account / per-row try/catch). Idempotency on `UNIQUE(account_id, snapshot_date)` carries over.

**D1 doesn't have SAVEPOINT in the same Python-SQLite way.** Per-row error isolation uses individual `db.prepare(...).bind(...).run()` calls. The Plaid sync handler MUST NOT use `db.batch()` anywhere. Every INSERT (data rows AND IngestionLog) is an independent `db.prepare(...).bind(...).run()` call. Per-row error isolation is achieved through the per-row try/catch pattern. `db.batch()` is reserved for other scenarios in other handlers where genuinely-atomic multi-statement operations are needed (e.g., paired writes that must succeed together). A PL-T03 test covers 5 accounts where account 3 has a duplicate-key violation: accounts 1, 2, 4, 5 are written and account 3 is recorded in IngestionLog as an error; a separate PL-T03 variant simulates a D1 batch failure mid-Item and asserts previously-committed per-row data is intact and IngestionLog records the partial failure. Functionally equivalent for the daily sync's idempotency contract.

### A7. Historical price source: pluggable HTTP API (default Twelve Data)

yfinance is Python-only and won't run on Workers. Three real options:

| Provider | Free tier | Notes |
|---|---|---|
| Alpha Vantage | 25 req/day | Cheap to start; daily backfill across 50 symbols would burn the quota; **needs paid plan ($50/mo) for backfill** |
| Twelve Data | 800 req/day, 8/min | Sufficient for daily incremental EOD; backfill is rate-limited but doable overnight |
| Polygon.io | 5 req/min unlimited daily | Free tier OK for daily incremental; need basic plan ($29/mo) for historical |

**Decision: Twelve Data free tier for incremental EOD; full backfill is a one-time job that can take a few nights to complete.** If quota becomes a problem, swap to Polygon basic. The price-source layer in the new backend is an interface with the API call behind it; replacing providers is a single-file change.

**Alternative considered:** keep yfinance running locally as a daily script that POSTs to the Workers `/internal/prices/upsert` endpoint. Rejected because it keeps an operational dependency on Travis's MacBook — defeating part of the migration's purpose. May reconsider if Twelve Data quota proves insufficient.

### A8. Encryption: Web Crypto AES-GCM (replaces Fernet)

`cryptography.fernet` → Web Crypto `AES-GCM` with a 256-bit key stored in Workers Secrets as `PLAID_TOKEN_ENC_KEY` (raw URL-safe base64 of 32 random bytes, generated via `openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n'`). Using URL-safe base64 ensures the decode path and KAT fixture are consistent. **The implementation decodes the base64-encoded `PLAID_TOKEN_ENC_KEY` in the Workers runtime via `Uint8Array.from(atob(key.replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0))` to normalize URL-safe base64 characters (`-` and `_`) to standard base64 before decoding. BEFORE calling `crypto.subtle.importKey`, assert decoded byte length: `if (rawBytes.byteLength !== 32) throw new Error('PLAID_TOKEN_ENC_KEY must decode to exactly 32 bytes')`. PL-T01 KAT asserts: (a) importKey succeeds only with a 32-byte raw key; (b) a key that decodes to 16 bytes throws the assertion before importKey is called; (c) a fixture key containing at least one `-` or `_` character decodes correctly. Base64 strings with padding `=` characters are acceptable inputs.**

**Note on PLAID_FERNET_KEY base64 format:** `PLAID_FERNET_KEY` uses URL-safe base64 (RFC 4648 §5; may contain `-` and `_`, not `+` and `/`). The npm `fernet` package handles URL-safe base64 internally if passed the raw key string. Do NOT pass it through `atob()` directly. D1-T03 KAT fixture key MUST contain at least one `-` or `_` character to exercise this path. 

**Stored payload format — exact bytes, no manual tag handling.** `SubtleCrypto.encrypt(AES-GCM)` returns a single ArrayBuffer of `(plaintext_len + 16)` bytes where the 16-byte GCM authentication tag is already appended to the ciphertext by the API. The implementation MUST NOT extract and re-append the tag; doing so would double the tag and corrupt every payload. The stored format is:

```
base64( [1-byte version tag: 0x01] || [12-byte random IV] || [SubtleCrypto.encrypt output, which IS ciphertext||tag] )
```

The 1-byte version tag `0x01` is prepended to every blob so a future reader can dispatch across format versions unambiguously. Length assertions and round-trip KATs must account for this extra byte: `stored_len == base64_encoded_length(1 + 12 + plaintext_len + 16)`. The TS port's crypto module includes a fixed-input known-answer test against a hardcoded key + IV + plaintext so the wire format is locked.

**AAD binding.** The internal `plaid_item.id` UUID (UTF-8 bytes) MUST be passed as Additional Authenticated Data (AAD) to both `crypto.subtle.encrypt` and `crypto.subtle.decrypt`. This uses the internal UUID (assigned at row creation, never controlled by Plaid), NOT the Plaid-issued `item_id` — an attacker who can write the `access_token_encrypted` column cannot swap a token to a different internal row because the internal UUID is not Plaid-controlled. This binds each ciphertext to its specific PlaidItem row. A PL-T01 test encrypts with internal UUID `A` and attempts to decrypt with internal UUID `B`; this MUST throw a decryption error. The decrypt MUST throw on an unrecognized version tag (no fall-through to legacy handling).

**IV uniqueness.** A PL-T01 test asserts that calling `encrypt(plaintext)` twice produces different ciphertexts (proving the IV is randomized per call via `crypto.getRandomValues(new Uint8Array(12))`). At this scale, collision probability is negligible (birthday bound: ~2^48 encryptions for 1-in-2^32 collision chance with 12-byte random IVs).

**Key rotation** — comma-separated keys in `PLAID_TOKEN_ENC_KEY` (active first, fallbacks for decryption only). Same MultiFernet pattern as the local Python; implemented manually because there is no `MultiFernet` for Web Crypto. **Rotation completeness:** fallback keys remain in the comma-separated list until all rows have been re-encrypted with the current key. A key-rotation script `scripts/rotate-plaid-enc-key.ts` walks D1 PlaidItem rows, decrypts each `access_token_encrypted` with the previous key (last entry in the comma-separated list), re-encrypts with the new key (first entry), updates the row, and emits one AuditEvent row per rotation. After all rows are updated, remove the old key from the comma-separated list. Without this re-encryption walk, compromised keys cannot be retired and the fallback list grows indefinitely. This script is tracked in TF-004 if not implemented at migration time.

**Fernet→AES-GCM migration of existing access_tokens.** The Plaid Phase 1 spec is in production locally with PlaidItem rows whose `access_token_encrypted` is Fernet ciphertext. Fernet and AES-GCM are not interoperable — the formats differ entirely (`[version][timestamp][IV][ciphertext][HMAC]` vs `[IV][ciphertext+tag]`).

**Key naming disambiguation:** during migration, two distinct symmetric keys exist:
- `PLAID_FERNET_KEY` (Doppler-only, the old Fernet key, renamed at M0c for unambiguous lookup)
- `PLAID_TOKEN_ENC_KEY` (Workers Pages-only, the new AES-GCM key, set at M0c)

Same name was used historically; the rename prevents the migration script from reading the wrong key by accident.

The migration script (D1-T03 in the runbook), a TypeScript file in sparkry-crm run via `npx tsx`, does:
1. Read every PlaidItem row from local SQLite via `better-sqlite3`.
2. Decrypt the Fernet ciphertext using the npm `fernet` package + `PLAID_FERNET_KEY` from Doppler. A known-answer test against a fixture produced by Python's `cryptography.fernet` proves byte-format interoperability.
3. Re-encrypt the plaintext under the new AES-GCM key via Web Crypto `crypto.subtle.encrypt` + `PLAID_TOKEN_ENC_KEY_MIGRATION` from sparkry-crm Doppler (mirror set at M0c). **Do NOT read `PLAID_TOKEN_ENC_KEY` from Workers Pages during migration** — the migration-only copy is `PLAID_TOKEN_ENC_KEY_MIGRATION` in sparkry-crm Doppler, distinct from the runtime Workers secret name. Two pre-migration assertions in the script enforce the correct context — BOTH must pass before any row is processed:
- `if (!process.env.PLAID_FERNET_KEY) throw new Error('PLAID_FERNET_KEY not present — wrong Doppler context (need accounting chained)')`
- `if (!process.env.PLAID_TOKEN_ENC_KEY_MIGRATION) throw new Error('PLAID_TOKEN_ENC_KEY_MIGRATION not present — wrong Doppler context (need sparkry-crm chained)')`
4. Emit INSERT statements with the new ciphertext; upload via `wrangler d1 execute --remote`.

Token plaintext is touched in memory for the duration of the migration; the Node process exits immediately afterwards.

`secure_delete=ON` doesn't exist on D1 (not exposed). The REVOKED sentinel still applies — when an Item disconnects, we overwrite `access_token_encrypted = "REVOKED"`. D1's underlying storage layer is Cloudflare's responsibility; the threat model "DB file exfiltrated → ciphertext recoverable from freed pages" does not apply to D1 the same way it applied to a SQLite file on a backed-up Mac.

### A9-pre. Dedup hash (source_row_hash) stability Python↔TypeScript

The `compute_brokerage_row_hash` function in `src/adapters/brokerage_csv_helpers.py` produces a per-row SHA-256 hex digest used as `source_row_hash` for deduplication. The TypeScript port MUST produce byte-identical output for the same input row. Requirements:

- The TypeScript port is mandatory: `src/lib/server/wealth/brokerage-row-hash.ts` must mirror the Python logic precisely (same field concatenation order, same decimal-quantize rules before stringifying, same UTF-8 encoding).
- BR-T03 includes a known-answer test: take a fixture CSV row, compute the hash in Python, compute in TypeScript, assert the hex digests are identical. **The KAT MUST document the two-level framing explicitly for all three hash functions**, using the general form `SHA256(UTF-8 of f'{len(source_type)}:{source_type}:<framed>')` where `<framed>` is `'|'.join(f'{len(p)}:{p}' for p in [broker, account_number, ...])` (per `src/utils/dedup.py:36`). The three hash variants and their correct outer prefixes are:
  - `compute_brokerage_row_hash`: `SHA256(f'12:brokerage_row:' + framed)` — where `12 = len('brokerage_row')`
  - `compute_position_row_hash`: `SHA256(f'18:brokerage_position:' + framed)` — where `18 = len('brokerage_position')`
  - `compute_realized_lot_hash`: `SHA256(f'17:brokerage_realized:' + framed)` — where `17 = len('brokerage_realized')`

  The KAT fixture file MUST include the intermediate `framed` string AND the final SHA-256 hex digest for each of the three variants, allowing TypeScript implementers to verify each layer independently. BR-T03 KAT MUST include fixtures for all three hash functions with their correct outer prefixes.
- An idempotency test is required: POST the same brokerage row payload twice via `POST /wealth/api/internal/ingest/brokerage-csv`; the second call MUST return the existing row without inserting a duplicate.

### A9. PDF/XLSX importers stay local

Vanguard CSV / F&G PDF / NW Mutual XLSX / GSK PDF / Franklin Templeton PDF importers depend on `pdfplumber`, `openpyxl`, etc. Porting them to TS/Workers is a significant effort and they're rarely run (once per statement, manually).

**Decision: keep them local as Python scripts that POST to the new Workers API.** Each script gets a `--target {local|cloud}` flag; default switches to `cloud` once the migration is verified. The local SQLite path can stay in the script for emergency fallback. A new Workers endpoint `POST /wealth/api/internal/ingest/<source>` accepts the normalized rows and writes to D1.

This is documented and accepted scope. Eventual Workers-native port is a separate future milestone.

### A10. Data migration: cutover with backfill, not dual-write

**SQLite-to-D1 DDL normalization (non-trivial):** D1 rejects a number of constructs the local SQLite app uses. The migration script (D1-T03 in runbook) MUST normalize:

1. **CHECK constraints.** ALTER TABLE ADD CONSTRAINT (which Alembic emits) is not supported by D1; every CHECK must be inline in CREATE TABLE. **The migration uses Drizzle-authored CREATE TABLE statements for the destination schema — the SQLite `.dump` output's DDL is discarded entirely.** Only the INSERT rows are taken from the dump. The hardcoded enum value lists in the Drizzle migration (Broker, AccountType, etc.) must match the current Python enum membership at cutover time; D1-T01 verifies parity against the Python source at code-review time.
2. **PRAGMA statements** are stripped (`PRAGMA foreign_keys=ON`, `PRAGMA journal_mode=WAL`, etc.).
3. **NUMERIC columns** map to TEXT with canonical decimal-string serialization (preserves 18,8 precision unambiguously across Workers and Python clients).
4. **DATETIME columns** map to TEXT in ISO-8601 format.
5. **AUTOINCREMENT** is replaced with explicit `String(36)` UUIDs (already the project's convention).
6. **SQLite triggers** (e.g., the `prevent_transaction_delete` trigger from the local DB) are NOT migrated — D1 supports triggers but the wealth schema doesn't carry the Transaction table.

The migration script is tested against a fixture SQLite DB before the real cutover. The runbook D1-T03 owns this.

**One-shot cutover sequence (preceded by staging dry-run):**

Before the production cutover window, a staging dry-run must complete (step 6 in the runbook): migrate a fresh SQLite snapshot to `sparkry-crm-staging`, run a 13-endpoint smoke-test against the staging deployment (deploy workers to staging Pages environment or use `wrangler dev` with sparkry-crm-staging D1 binding; compare against M0j golden output), run rollback round-trip, and validate byte-identical decimal columns. Only then open the production cutover window.

1. **Pre-flight checks** (all must pass):
   a. `SELECT COUNT(*) FROM audit_events WHERE entity_id IS NOT NULL` returns 0 on local DB (REQ-WC-009 / A11 safety).
   b. Doppler has `WEALTH_API_BASE`, `WEALTH_INTERNAL_KEY`, `PLAID_*` secrets set.
   c. Workers Pages secrets (the 9 enumerated in REQ-WC-019) verified present via `wrangler pages secret list --project-name sparkry-crm`: `PLAID_CLIENT_ID`, `PLAID_SANDBOX_SECRET`, `PLAID_PRODUCTION_SECRET`, `PLAID_ENV`, `PLAID_TOKEN_ENC_KEY` (new AES-GCM), `TWELVE_DATA_API_KEY`, `WEALTH_INTERNAL_KEY`, `WEALTH_ALLOWED_EMAILS`, `RESEND_API_KEY` (inherited from CRM). Doppler-side (sparkry-crm config): `PLAID_TOKEN_ENC_KEY_MIGRATION` (mirror of the new AES-GCM key set at M0c, used by `migrate-from-sqlite.ts` — the migration script reads this via `process.env.PLAID_TOKEN_ENC_KEY_MIGRATION`). Doppler-side (accounting config): `PLAID_FERNET_KEY` (renamed at M0c, used for migration-time decrypt), `WEALTH_API_BASE`, `WEALTH_INTERNAL_KEY`, `WEALTH_TARGET_DEFAULT` — verified via `doppler secrets --only-names | grep -E '^(PLAID_FERNET_KEY|PLAID_TOKEN_ENC_KEY_MIGRATION|WEALTH_)'`.
   d. Plaid dashboards (sandbox AND production) have the new redirect URI listed — screenshots captured.
   e. Staging D1 (`sparkry-crm-staging`) has been migrated to via a dry-run cutover and the round-trip rollback worked.
2. **Local SQLite snapshot**: `sqlite3 data/accounting.db ".backup data/accounting.pre-cutover-$(date -u +%Y%m%dT%H%M%SZ).db"`. Verify file exists and `> 1 MB`.
3. Pause Plaid sync: `launchctl unload ~/Library/LaunchAgents/com.sparkry.plaid-balance-sync.plist`.
4. `wrangler d1 export sparkry-crm-prod --remote --output prod-pre-migration.sql` (D1-side backup). After running, verify the file is non-trivially sized: `[ $(stat -f%z prod-pre-migration.sql) -gt 100 ]` must pass before proceeding.
5. Run `npx tsx scripts/migrate-from-sqlite.ts --apply` (in the sparkry-crm repo) — this script:
   a. Reads brokerage/plaid/history/audit_events/ingestion_log rows from SQLite.
   b. **Re-encrypts every PlaidItem.access_token_encrypted from Fernet to AES-GCM** (see A8) using `PLAID_FERNET_KEY` from accounting Doppler (decrypt) and `PLAID_TOKEN_ENC_KEY_MIGRATION` from sparkry-crm Doppler (re-encrypt; this is the migration-only mirror of the AES-GCM key, set at M0c). The invocation chains both Doppler contexts without intermediate shell re-export: `doppler run --project accounting --config dev -- doppler run --project sparkry-crm --config <SPARKRY_CRM_CONFIG> -- npx tsx scripts/migrate-from-sqlite.ts ...` (no intermediate `sh -c` re-export; both doppler contexts inject env independently into the final Node process). The script asserts `PLAID_FERNET_KEY` is present before proceeding.
   c. Emits an INSERT-only SQL file (no DDL — Drizzle migrations already applied the schema).
   d. Uploads via `wrangler d1 execute --remote --file=...`.
   e. Returns row counts per table; the orchestrator validates them against pre-migration SQLite counts. **Row-count validation accounts for oversized-row exclusions:** D1 row count + count of rows in `migration-oversized-rows.json` MUST equal the SQLite row count per table. The validator MUST subtract oversized exclusions before asserting match. Validator prompt: "exact match per table OR exact match after subtracting oversized-row exclusions documented in migration-oversized-rows.json."
6. **Value-level spot-check**: for 9 rows per table — 5 random rows PLUS 4 targeted edge-case rows: (1) a value ending in `.50` (trailing-zero preservation test), (2) a value with maximum fractional places (e.g., `0.12345678`), (3) a value of `0.00`, (4) the row with the largest absolute value — across all 6 decimal-bearing tables: `position_snapshot`, `plaid_account_balance_snapshot`, `cost_basis_lot`, `historical_price`, `brokerage_transaction` (sample `amount`, `fees`, `commission`), and `realized_gain_loss` (sample `proceeds`, `cost_basis`) = 6 tables × 9 rows = 54 rows total. Compare the canonical decimal string from SQLite vs the round-tripped value from D1. All 54 must be byte-identical. Also assert no `-0` appears in any TEXT column (must be normalized to `0`). Note: step 7c uses `stat -f%z` which is macOS-only syntax; use `stat -c%s` on Linux.
7. Smoke-test from an authenticated browser session: `/wealth/`, `/wealth/networth`, `/wealth/desk/connections`, `GET /wealth/desk/api/plaid/reconciliation/summary`. All return the expected pre-cutover values.
8. Resume Plaid sync via the new Cron Trigger (enabled in wrangler.toml).
9. Wait for the soak window to close per REQ-WC-014 (3 consecutive successful plaid-balance-sync cron runs, ≥3 calendar days). Verify via IngestionLog query (see runbook step 7j for the exact `wrangler d1 execute` command).
10. Post-soak decommission (after the soak window from step 9 closes): delete launchd plists (LM-T05), `cloudflared tunnel delete plaid-oauth-return`, remove old tunnel URI from Plaid dashboards. Reference runbook step 9 for the full sequence including PLAID_FERNET_KEY and PLAID_TOKEN_ENC_KEY_MIGRATION deletion (30 calendar days after cutover OR explicit rollback-window-closed sign-off, whichever is later).

Dual-write was considered. Rejected because it requires writes to both stores during the soak, doubles the operational complexity, and the cutover risk is low (single user, brokerage data is read-mostly, full data dump fits comfortably under D1's 5 GB free-tier cap).

### A11. AuditEvent split

The local `audit_events` table has Transaction-mode events (existing) AND Plaid-mode events (the latter were ADDED by the recent Plaid Phase 1 migration but no actual rows of that mode were ever written — Plaid was never used in production locally because the Cloudflare tunnel was never stood up).

After the migration:

- **Local DB:** keeps Transaction-mode events. The CHECK constraint relaxes back to `transaction_id NOT NULL` (entity columns get dropped).
- **D1:** new `audit_events` table mirrors the entity-mode schema only. Tables in this DB only ever see wealth entities, so the XOR CHECK simplifies to "entity_id NOT NULL AND entity_type NOT NULL."

**Hard pre-condition for the local rollback**, enforced as the first cutover pre-flight check and a runbook LM-T01 prerequisite assertion:

```sql
SELECT COUNT(*) FROM audit_events WHERE entity_id IS NOT NULL;  -- must return 0
```

If non-zero, the rollback migration would refuse to run (the existing migration includes a "refuse to downgrade if entity_id IS NOT NULL" guard). At time of writing (2026-05-10), this count IS zero — Plaid was never actually linked locally. The runbook records the value as evidence; the validator agent SUBSTANTIATES it from the raw query output before any rollback step proceeds.

### A12. Local-app brokerage routes get removed (not just hidden)

After cutover, the brokerage SvelteKit pages on the local dashboard are deleted. The brokerage routes in the FastAPI app are deleted. The brokerage Python adapters stay (they're the local importers that POST to Cloudflare). The brokerage MODELS stay in the SQLite DB as a read-only archive (in case we need to compare or recover) — but no new writes happen against them.

A new health check on the local app asserts: `SELECT COUNT(*) FROM plaid_item` returns 0 (or all rows are status=disconnected). Surfaces any forgotten local-write path.

---

## Routing

### URL space

```
https://internal.sparkry.ai/                              — CRM dashboard (Amy's home)
https://internal.sparkry.ai/customers                     — CRM (existing)
https://internal.sparkry.ai/work-orders                   — CRM (existing)
https://internal.sparkry.ai/invoices                      — CRM (existing)

https://internal.sparkry.ai/wealth                        — Wealth dashboard (Travis's home)
https://internal.sparkry.ai/wealth/networth               — net-worth chart + benchmark
https://internal.sparkry.ai/wealth/holdings               — per-symbol view
https://internal.sparkry.ai/wealth/accounts               — account list
https://internal.sparkry.ai/wealth/accounts/<id>          — account detail
https://internal.sparkry.ai/wealth/missing-accounts       — coverage panel

https://internal.sparkry.ai/wealth/desk                   — operator landing
https://internal.sparkry.ai/wealth/desk/connections       — Plaid Items
https://internal.sparkry.ai/wealth/desk/connections/oauth-return — Plaid OAuth landing
https://internal.sparkry.ai/wealth/desk/reconciliation    — Plaid vs computed deltas
https://internal.sparkry.ai/wealth/desk/import            — CSV/PDF drop zone for local-importer POSTs (optional, see FD-T05; may be deferred)
```

### API routing (Workers)

```
POST  /wealth/desk/api/plaid/link-token
POST  /wealth/desk/api/plaid/exchange
POST  /wealth/desk/api/plaid/map-accounts
POST  /wealth/desk/api/plaid/disconnect/{item_id}
POST  /wealth/desk/api/plaid/relink/{item_id}
GET   /wealth/desk/api/plaid/items
POST  /wealth/desk/api/plaid/sync-now
GET   /wealth/desk/api/plaid/reconciliation/summary

# Public brokerage API — all 13 endpoints from the existing FastAPI router
GET   /wealth/api/brokerage/networth
GET   /wealth/api/brokerage/networth-history
GET   /wealth/api/brokerage/networth-history-benchmark
GET   /wealth/api/brokerage/accounts
GET   /wealth/api/brokerage/accounts/{id}/detail
PATCH /wealth/api/brokerage/accounts/{id}
PUT   /wealth/api/brokerage/accounts/{id}/tags
GET   /wealth/api/brokerage/holdings/{symbol}/history
GET   /wealth/api/brokerage/missing-accounts
GET   /wealth/api/brokerage/realized-gl
GET   /wealth/api/brokerage/top-holdings
GET   /wealth/api/brokerage/recent-transactions
GET   /wealth/api/brokerage/data-integrity

# Internal API consumed only by local Python importers + the operator-desk drop-zone
POST  /wealth/api/internal/ingest/brokerage-csv
POST  /wealth/api/internal/ingest/xlsx-snapshot
POST  /wealth/api/internal/ingest/historical-prices
POST  /wealth/api/internal/ingest/cost-basis-lot
```

**Auth layering:**
- All `/wealth/desk/api/*` and `/wealth/api/brokerage/*` endpoints require Cloudflare Access (browser SSO) AND in-app guard against `WEALTH_ALLOWED_EMAILS`.
- `/wealth/api/internal/*` endpoints are EXCLUDED from Cloudflare Access (the Python importer scripts cannot do browser SSO). They authenticate via `X-Internal-Key` header matching the `WEALTH_INTERNAL_KEY` Workers secret. The comparison MUST use `crypto.subtle.timingSafeEqual()` (encode both the header value and the secret as `Uint8Array` before comparison) to prevent timing-oracle attacks. **The implementation MUST length-check before calling `crypto.subtle.timingSafeEqual()`: if `incoming.byteLength !== secret.byteLength`, return 401 immediately without calling timingSafeEqual (length mismatch is not secret information; the secret length is fixed and known).** A Cloudflare rate-limiting rule on `/wealth/api/internal/*` enforces a maximum of **5 requests per 10 seconds per IP** (Free plan's 10-second sampling-period constraint, translated from the original "5 req/min" intent; full rationale in `docs/operational/m0h-evidence/wealth-internal-ingest-ratelimit.md` and runbook M0h); this rule is documented and verified in M0h. The bypass and method restriction are implemented via the Workers route handler (not CF Access policy, which does not support method filtering): the handler returns 405 for any non-POST request. **IP allowlisting was considered and rejected** — dynamic ISP IPs make a hardcoded allowlist operationally fragile; defense via authenticated `X-Internal-Key` + constant-time comparison + 5 req/min rate limit + POST-only restriction is sufficient for single-user scope. This exclusion is also configured as a CF Access bypass rule for `/wealth/api/internal/*` — verified as a pre-cutover check (M0h).

---

## Requirements

Each REQ-ID maps to acceptance criteria, non-goals, and test seeds. The fresh `/qpipeline` session writes failing tests against each REQ first (TDD), then implements.

### REQ-WC-001: Routing isolation between CRM and Wealth groups
- **Acceptance:** SvelteKit groups `(crm)` and `(wealth)` exist; ESLint rule `no-restricted-paths` blocks imports across groups; CI fails if either group transitively imports from the other; the rendered bundle for `/customers` does not include any module path matching `*/wealth/*`, and vice versa.
- **Non-goals:** runtime sandboxing; the two groups still run in the same Workers/Pages process.

### REQ-WC-002: Shared auth via CRM's in-app Google OAuth + Travis-only Wealth gate
- **Note on reconciliation:** the original draft assumed Cloudflare Access JWKS validation. Live inspection at M0h showed CF Access is not configured. See A4 for full rationale; sub-team 4/4 consensus to reuse the CRM's in-app Google OAuth + HMAC session cookie pattern. CF Access deferred to TF-002.
- **Acceptance:**
  - Every `/wealth/*` page and `/wealth/api/brokerage/*` + `/wealth/desk/api/*` endpoint requires a valid HMAC-signed `session` cookie (validated by `SESSION_SIGNING_KEY` in `hooks.server.ts` per the existing CRM pattern) AND the session's email claim matching the `WEALTH_ALLOWED_EMAILS` Pages secret (default `travis@sparkry.com` only).
  - Missing/invalid session cookie → 302 redirect to `/api/auth/callback?action=login` (same as the CRM's existing behavior).
  - Valid session cookie for an email NOT in `WEALTH_ALLOWED_EMAILS` → **404** (not 403, not 200). Explicit test: a session cookie for `amycsparks@gmail.com` hitting `/wealth/networth` returns 404.
  - `hooks.server.ts` and every Worker route handler in `(wealth)` MUST: (a) parse `WEALTH_ALLOWED_EMAILS` (split by comma, trim whitespace, lowercase); (b) if the resulting list is empty or the env var is missing, fail closed with 500 and log `WEALTH_ALLOWED_EMAILS misconfigured`; (c) compare the session's email claim (lowercased) against the parsed list. Vitest KAT: missing `WEALTH_ALLOWED_EMAILS` env → 500 (not 200, not 404).
  - Pages preview URLs (`sparkry-crm.pages.dev/wealth/*`) are covered by the SAME `hooks.server.ts` guard — no separate Access policy needed because the cookie-level guard runs on the same handler regardless of host.
  - `/wealth/api/internal/*` is added to `PUBLIC_PATHS` in `hooks.server.ts` (bypasses the session-cookie check). The route handler enforces: (a) HTTP method MUST be POST or returns 405; (b) `X-Internal-Key` header MUST `crypto.subtle.timingSafeEqual` `WEALTH_INTERNAL_KEY`; missing/wrong header returns 401.
  - Defense in depth: every Worker route handler in `(wealth)` independently re-checks `event.locals.user.email` against `WEALTH_ALLOWED_EMAILS` (do not trust the hooks layer alone — the route handler is the last line of defense).
- **Non-goals:** Cloudflare Access deployment (TF-002); browser-side session management beyond the cookie sliding-window pattern the CRM already implements.

### REQ-WC-003: D1 schema port preserves all CHECK + UNIQUE constraints
- **Acceptance:** for every table being migrated (**account, brokerage_transaction, position_snapshot, realized_gain_loss, historical_price, account_balance_snapshot, expected_account, cost_basis_lot, account_tag, plaid_item, plaid_account_balance_snapshot, audit_events, ingestion_log** — 13 migrated tables), every CHECK constraint and UNIQUE constraint in the SQLite schema has an equivalent in the D1 migration; a smoke test that violates each constraint asserts the D1 INSERT is rejected. CHECK constraints are declared inline in CREATE TABLE (not ALTER TABLE ADD CONSTRAINT, which D1 rejects). Enum value lists are hardcoded in the D1 migration file, NOT injected from any external source at migration time. **`live_quote` (REQ-WC-013a) is a NEW D1-only table** (not migrated from SQLite) — D1-T01 creates it via the same Drizzle schema file; it has no SQLite source, no rows in the migration spot-check, and is excluded from R2 backups per REQ-WC-018.
- **Non-goals:** preserving SQLite-specific pragmas; preserving WAL mode; preserving SQLite triggers (D1 wealth schema does not host the Transaction table so the `prevent_transaction_delete` trigger is irrelevant).

### REQ-WC-004: Decimal precision preserved end-to-end
- **Acceptance:** every monetary or quantity value from a Plaid API response or local CSV is converted to a canonical decimal string before insert, never to a JS `number`; D1 stores it as TEXT; the API response converts back to canonical strings; the frontend uses `decimal.js` (or equivalent) for display; tests assert no precision loss across `8.123456781234` quantity values.
- **Non-goals:** changing the precision contract; supporting currencies beyond USD.

### REQ-WC-005: Plaid Item lifecycle (port of REQ-025)
- **Acceptance:** all REQ-025 acceptance bullets from the Plaid Phase 1 spec pass against the new Workers backend; CSRF state nonce is `crypto.randomUUID()` (122-bit entropy) stored on a placeholder PlaidItem row in D1; the placeholder row carries `nonce_expires_at = now + 30 min`; the oauth-return page validates state against D1 BEFORE postMessage (returns 400 on miss/expired/already-consumed nonce); exchange clears the nonce; disconnect overwrites `access_token_encrypted` to `"REVOKED"` and nulls Account FKs. **Nonce consumption MUST be atomic:** use a single UPDATE statement `UPDATE plaid_item SET state_nonce = NULL, state_nonce_expires_at = NULL WHERE id = ? AND state_nonce = ? AND state_nonce_expires_at > datetime('now') RETURNING id` — a zero-rows result means the nonce was already consumed or expired (return 400); a non-zero result means this request wins the race. A SELECT then UPDATE pattern is NOT permitted (TOCTOU race). A concurrent-replay test MUST be included in FD-T02 verifying that replaying the oauth-return URL with a consumed nonce returns 400. **link_token is fetched on the Add-Connection click handler, NOT on page load** — a test asserts that advancing fake time by 31 minutes between page load and click triggers a fresh `/wealth/desk/api/plaid/link-token` call (Phase 1 Known Gap #9 carried forward). FD-T02 test: replay oauth-return URL with a consumed nonce → 400. Rate limit on `/wealth/desk/api/plaid/link-token`: maximum 10 calls per user per hour (D1-backed counter or Workers rate limiting). A cleanup cron (every 6 hours: cron expression `"0 */6 * * *"`) marks expired placeholder PlaidItem rows as `status='abandoned'` rather than deleting them (never-delete invariant): `UPDATE plaid_item SET status='abandoned' WHERE status='pending_oauth' AND state_nonce_expires_at < datetime('now') AND access_token_encrypted IS NULL`. The `plaid_item` status enum includes `'pending_oauth'` and `'abandoned'` as valid values (these must be added to the local Python `PLAID_ITEM_STATUSES` CHECK constraint via the LM-T0 Alembic migration pre-cutover — see REQ-WC-009 and the runbook LM-T0 task). A CHECK constraint enforces: if `status='abandoned'` then `access_token_encrypted IS NULL`. Register this cron in D1-T06. D1-T06's cleanup cron test (D1-T06 test seed) verifies that abandoned rows are marked, not deleted. **postMessage targetOrigin:** the oauth-return page MUST call `window.opener.postMessage(payload, 'https://internal.sparkry.ai')` with the exact literal targetOrigin (NEVER `'*'`). A Vitest/Playwright test MUST assert the `targetOrigin` argument is exactly `'https://internal.sparkry.ai'`.
- **Plaid Link popup mode:** Plaid Link MUST be opened in popup mode (NOT redirect mode) for the `window.opener.postMessage` pattern to work. The oauth-return page MUST check `if (!window.opener)` and display an error UI ('Please open this connection from the wealth app, not directly') rather than silently failing. FD-T02 additional tests: oauth-return page navigated to directly (no opener) → error UI shown; oauth-return opened by attacker popup (origin mismatch) → postMessage no-ops cleanly without error.
- **Non-goals:** Phase 2 Investments.

### REQ-WC-006: Plaid Balance daily sync via Workers Cron Trigger (port of REQ-026)
- **Acceptance:** Workers `scheduled()` handler is registered with cron expression **`"7 10 * * *"`** (10:07 UTC daily, = 02:07 PST / 03:07 PDT); pulls `/accounts/balance/get` for every active PlaidItem; writes `plaid_account_balance_snapshot` rows; classifies errors as retryable/terminal per the existing table; idempotent on UNIQUE(account_id, snapshot_date) at the snapshot level; writes one IngestionLog row per Item per run via `INSERT INTO ingestion_log (id, source, run_at, status, records_processed, records_failed, error_detail, retryable) VALUES (uuid(), 'plaid-balance-sync', now(), ...)` — IngestionLog rows use UUID PKs and have NO UNIQUE(source, run_date) constraint (idempotency is at the snapshot level, not IngestionLog); unmapped accounts upsert ExpectedAccount. The IngestionLog schema mirrors the Python source (`src/models/ingestion_log.py`): columns are `id` (UUID PK), `source`, `run_at` (timestamp), `status`, `records_processed`, `records_failed`, `error_detail`, `retryable`, `retried_at`, `resolved_at`. There is NO `run_date` column and NO `row_count` column in this schema. **REVOKED-row filtering:** the sync handler MUST query `WHERE access_token_encrypted != 'REVOKED' AND status = 'active'` before attempting AES-GCM decrypt; a REVOKED sentinel would produce a version-tag mismatch (0x52 vs expected 0x01) causing decrypt to throw — the WHERE filter prevents this propagation during normal sync. `error_detail` written to IngestionLog MUST contain only the Plaid `error_code` and `error_type` fields (e.g., `'ITEM_LOGIN_REQUIRED / INVALID_CREDENTIALS'`) — NOT the full Plaid error response body which may contain `item_id` and `institution_id`. **PL-T07 CPU benchmark** MUST verify the sync cron's per-Item-iteration CPU is ≤10ms in `wrangler dev --test-scheduled "7 10 * * *"` profiling; if exceeded, Workers Paid is required for the cron Worker. **Workers Paid ($5/mo) is REQUIRED from day one for the Plaid sync cron** — network latency × N Items will exceed 30 seconds wall-clock with 5 or more active Items, exceeding the Workers Free scheduled handler limit. The $5/mo Workers Paid cost target already covers the R2 backup cron and the Twelve Data ingest cron (REQ-WC-013, REQ-WC-018); the Plaid sync cron is included in the same $5/mo plan. REQ-WC-017's wall-clock budget for the Plaid sync cron is ≤ 30 seconds wall-clock on Workers Paid (network-I/O-bound, not CPU-bound). Cron-retry idempotency test (PL-T03): invoke the scheduled handler twice with the same date → exactly one snapshot row per account (UNIQUE constraint prevents duplicates); two IngestionLog rows are acceptable (one per invocation); PL-T03 MUST also include one REVOKED row in the fixture and assert it is skipped without error.
- **Non-goals:** sub-daily sync; webhooks; local-time scheduling (Workers cron is UTC only).

### REQ-WC-007: Stale-Item alerting (port of REQ-027) — Workers-only, replaces local
- **Acceptance:** a Workers scheduled handler bound to cron **`"0 14 * * MON"`** (14:00 UTC Mondays = 06:00 PST / 07:00 PDT) emails Travis (via Resend) a summary of any active PlaidItem with terminal-error `last_sync_status`. The alert recipient is hardcoded as the constant `travis@sparkry.com` in the handler (personal operational alert; not configurable via secret). `WEALTH_ALERT_EMAIL` is NOT introduced as a secret — the hardcoded constant is intentional and documented. PL-T06 KAT MUST verify that `handlePlaidStaleAlert` calls `resend.emails.send({ to: ['travis@sparkry.com'] })` with the literal constant; test asserts email still goes to `travis@sparkry.com` even if any `WEALTH_ALERT_EMAIL` env variable were set (defense against future misconfiguration). The handler writes an AuditEvent row with `changed_by='cron:stale-alert'`, `cf_scheduled_time=controller.scheduledTime`, `entity_type='plaid_item'` for each stale item detected. The handler also writes an IngestionLog row with `source='plaid-stale-alert'` recording success/failure of the Resend call. The `/wealth/desk` page surfaces Items not synced in >48h. **The local weekly P&L email's Plaid section is stripped during cutover** (LM-T04 in runbook removes the `_check_plaid_stale_items` call and the appended report section) — only ONE stale-Item alert exists post-cutover, and it's from Workers.
- **Non-goals:** SMS/push.

### REQ-WC-008: Reconciliation summary (port of REQ-028)
- **Acceptance:** `GET /wealth/desk/api/plaid/reconciliation/summary` returns the same delta logic as the Python version (`> 2%` pct OR `> $100` abs, strict; credit/loan negation; `null` when no positions priced — NOT 0). **Error response sanitization:** all `/wealth/desk/api/plaid/*` HTTP error responses MUST return only sanitized error shapes containing `error_code` and `error_type`. The full Plaid SDK error body MUST be logged server-side only (via Sentry withSentry), never in the HTTP response. Vitest KAT: mock Plaid SDK throwing an error that includes `item_id`; assert the HTTP response body does NOT contain `item_id`.
- **Non-goals:** changing the thresholds.

### REQ-WC-009: AuditEvent entity-mode in D1
- **Acceptance:** D1 `audit_events` table has `entity_id NOT NULL` and `entity_type NOT NULL`; no `transaction_id` column at all (D1 wealth DB never sees Transactions); every Plaid lifecycle action writes a row. The `changed_by` column is `String(64)` to accommodate actor strings like `'cron:twelve-data-ingest'` (21 chars) and `'human:<email>'`. Cron-initiated AuditEvent rows write `changed_by='cron:plaid-sync'`, `'cron:twelve-data-ingest'`, `'cron:r2-backup'`, `'cron:stale-alert'` per handler. Human-initiated rows write `changed_by='human:<email>'`. Cron-initiated rows include `cf_scheduled_time` (INTEGER, nullable, Unix epoch ms) populated from `controller.scheduledTime` (Cloudflare-verifiable timestamp, not app-clock); human-initiated rows set `cf_scheduled_time = NULL`. The `audit_events` table is append-only enforced by D1 triggers preventing DELETE and UPDATE (added in D1-T01). The `audit_events` table has no UNIQUE constraint — it is purely append-only. Additionally, D1 no-delete triggers are added in D1-T01 on the following tables (mirroring the `audit_events` pattern): `plaid_item`, `plaid_account_balance_snapshot`, `brokerage_transaction`, `realized_gain_loss`, `position_snapshot`, `cost_basis_lot`. Pattern: `CREATE TRIGGER <table>_no_delete BEFORE DELETE ON <table> BEGIN SELECT RAISE(ABORT, '<table> is append-only per the never-delete invariant'); END;`. The `plaid_item` no-delete trigger is: `CREATE TRIGGER plaid_item_no_delete BEFORE DELETE ON plaid_item BEGIN SELECT RAISE(ABORT, 'plaid_item is append-only per never-delete invariant'); END;`. No UPDATE trigger is added on `plaid_item` — UPDATE remains permitted (REVOKED overwrite + abandoned status update). For the five brokerage data tables (`plaid_account_balance_snapshot`, `brokerage_transaction`, `realized_gain_loss`, `position_snapshot`, `cost_basis_lot`), only DELETE triggers are added — no UPDATE trigger. D1-T05 includes DELETE-rejection constraint tests for each of these tables including `plaid_item`. The cleanup-cron UPDATE for `plaid_item` status='abandoned' is the documented exception to no-delete (it is an UPDATE, not a DELETE, and is explicitly permitted).

  **Pre-cutover local migration (LM-T0, non-destructive):** the local Python `AuditEvent` model's `changed_by` column MUST be widened from `String(8)` to `String(64)` AND a new nullable `cf_scheduled_time: Mapped[int | None] = mapped_column(BigInteger, nullable=True)` column added via an Alembic migration that runs BEFORE the cutover (not in LM-T01 post-soak). Without this pre-cutover widening, a rollback during the soak window would attempt to re-insert cron AuditEvent rows with 21-character `changed_by` values into a `String(8)` column, causing the rollback to fail. Similarly, the local Python `PLAID_ITEM_STATUSES` CHECK constraint must be widened from `('active', 'disconnected')` to `('active', 'disconnected', 'pending_oauth', 'abandoned')` in the same LM-T0 migration, because rollback would re-insert D1 rows with those status values. LM-T0 is non-destructive (no DROP TABLE, no deleted plists) and may run well before the cutover window opens.
- **Non-goals:** preserving the dual-mode CHECK from the local DB.

### REQ-WC-010: Brokerage read API parity (all 13 routes + benchmark allowlist)
- **Acceptance:** every brokerage endpoint listed in the Routing section (13 routes: networth, networth-history, networth-history-benchmark, accounts, accounts/{id}/detail, accounts/{id} PATCH, accounts/{id}/tags PUT, holdings/{symbol}/history, missing-accounts, realized-gl, top-holdings, recent-transactions, data-integrity) returns the same JSON shape as the current FastAPI version. There is NO bare `GET /accounts/{id}` — the per-account entry point is `/accounts/{id}/detail`. A contract-test suite compares responses from a sample dataset (loaded into D1) against the Python golden output captured pre-cutover via M0j in the runbook; shape diffs fail the test. `/wealth/api/brokerage/networth-history-benchmark` enforces a hardcoded benchmark allowlist `{SPY, VTI, QQQ, BND}`; `?benchmark=AAPL` returns 400 with the generic message "invalid benchmark symbol" — the valid options are NOT enumerated in the error response to avoid leaking the allowlist; `?benchmark=SPY` returns 200.
- **Non-goals:** changing API contracts.

### REQ-WC-011: Brokerage UI parity at `/wealth/*` (Svelte 5 runes only)
- **Acceptance:** every brokerage page in the current `dashboard/src/routes/brokerage/*` tree has a `/wealth/*` equivalent with feature parity (net-worth chart with benchmark overlay, per-symbol history, account detail dossier, account-tag filter chips, missing-accounts panel, three-state filter chips); visual differences are acceptable per the "they can look different" constraint. **All `(wealth)` route components use Svelte 5 runes syntax (`$props()`, `$state()`, `$derived()`, `$effect()`)** consistent with the CRM project per `sparkry-crm/CLAUDE.md`. Co-locating Svelte 4 reactive statements (`$:`, `export let`) in the `(wealth)` group is a lint violation. **Note:** the `/wealth/transactions` page is deferred to a post-cutover follow-up; the local `/brokerage/transactions` page (if present) is preserved during this migration to avoid a feature gap until the follow-up port lands.
- **Non-goals:** pixel-perfect port; the page redesign that shipped recently for the local brokerage view sets the design language, and the migration should preserve that but is allowed to refine it.

### REQ-WC-012: Local Python importers POST to Workers
- **Acceptance:** the following 7 importers gain a `--target cloud` mode (default `--target local`) that POSTs the normalized rows to `/wealth/api/internal/ingest/*`: brokerage CSV importer, XLSX savings-plan importer, Vanguard CSV importer, F&G PDF importer, NW Mutual XLSX importer, GSK PDF importer, and Franklin Templeton PDF importer. The cloud-mode tests use a Workers mock to verify the payload shape; the existing local-mode tests continue to pass unchanged. Dedup hash (source_row_hash) ported from `src/adapters/brokerage_csv_helpers.py` to TypeScript with byte-identical output (BR-T03 KAT required). The ingest endpoint accepts a maximum of **100 rows per POST**; returns 413 if exceeded. Importer adapters MUST batch their POSTs accordingly. If 100-row individual inserts cannot meet the 10 ms CPU budget on Workers Free, the documented escape hatch is Workers Paid (which removes the per-request CPU cap). Do NOT use `db.batch()` chunking — it violates per-record error isolation (one bad row rejects the entire chunk).
- **raw_data overflow handling:** if a row's `raw_data` JSON exceeds 900 KB, the ingest handler REJECTS the row with 422 and writes an IngestionLog error row — do NOT silently truncate (raw_data is part of the audit-trail invariant). Importers MUST NOT silently truncate; F&G/GSK PDF cases must be split into multiple rows by the local Python importer before POST.
- **Non-goals:** porting the importer parsing logic to TypeScript.

### REQ-WC-013: Historical-price ingestion via Workers + Twelve Data (quota-bounded)
- **Acceptance:** a Workers scheduled handler bound to cron **`"30 7 * * *"`** (07:30 UTC daily = 23:30 PST / 00:30 PDT next day) pulls EOD prices for every symbol present in `position_snapshot`; issues **one API call per symbol** (Twelve Data free tier does not support multi-symbol batch — batch is a paid-tier feature). Budget math: at 50 symbols, 50 calls/day (~6% of the 800/day free-tier cap). Rate-limited to 8 req/min (burst) AND **enforces a daily-request budget cap of 600** (down from 680) to provide a wider safety buffer below Twelve Data's 800/day limit; if the daily cap is hit, additional symbols defer to next run and a warning row goes into IngestionLog. **Wall-clock budget:** 50 symbols × 1 call each at 8 req/min = 6.25 minutes wall-clock per cron run. This exceeds the 30-second Workers Free wall-clock limit; accordingly, this cron handler REQUIRES Workers Paid ($5/mo) from day one. REQ-WC-017's wall-clock budget for this handler is ≤ 8 minutes wall-clock on Workers Paid. **Symbol validation:** before constructing the Twelve Data API URL, the cron handler MUST validate each symbol against the regex `^[A-Z0-9.^]{1,12}$` (standard ticker format). A symbol failing this regex is skipped with an IngestionLog warning row and is NOT fetched — defense against symbol-injection via the brokerage CSV importer. Before calling Twelve Data, the handler queries already-present `(symbol, date)` pairs for the target date and SKIPS API calls for those (avoids burning quota on already-populated rows). Inserts use `INSERT INTO historical_price ... ON CONFLICT(symbol, trade_date) DO NOTHING`. A second run for the same date makes zero Twelve Data API calls for already-populated symbols (BR-T04 idempotency test). A backfill endpoint (`POST /wealth/api/internal/prices/backfill?overwrite=true`, auth-required, X-Internal-Key gated) walks the historical window in chunks subject to the same caps; with `?overwrite=true` it uses `INSERT OR REPLACE` (or `ON CONFLICT DO UPDATE SET price=excluded.price`) to correct bad historical_price rows — this is the only way to correct a bad row. Without `?overwrite=true` it uses `ON CONFLICT DO NOTHING`. Both write to `historical_price` in D1 with `source='twelve_data'`. **Field mapping:** `historical_price.close` stores the Twelve Data `close` field (not `open`, `high`, `low`, or `adjusted_close`); other fields are discarded. The column is named `close` (NOT `close_price`) per `src/models/history.py:60`. Twelve Data returns bars newest-first; use `outputsize=1` for daily incremental, `outputsize=N` for backfill chunks. **Note on `outputsize`:** the Twelve Data free tier may limit `outputsize` to a small value (e.g., 30 or 100 data points). Verify `outputsize=5000` is supported on the free tier before implementing the backfill chunking strategy. The PL-T02 bundle-size sub-step adds a probe call to verify the actual free-tier limit; if limited, the backfill chunk size must be adjusted accordingly. **The daily-budget cap counter is shared** between the cron handler AND the backfill endpoint: both query the IngestionLog for the day's `source='twelve_data'` call count before issuing API calls. The source string written to IngestionLog by the cron handler MUST be `'twelve_data'` (underscore — NOT `'twelve-data'` with a hyphen); a Vitest assertion MUST verify the written source string matches the budget-counter query string exactly. This is a best-effort cap: concurrent invocations may overrun by up to two batch worths in the simultaneous-start case (cron + manual backfill firing within milliseconds of each other). The cap is set to 600 (down from 680) to provide a wider safety buffer below Twelve Data's 800/day limit. Single-user scope makes simultaneous-start rare. BR-T05 MUST include a test simulating simultaneous-start and assert total calls ≤ 800. The backfill endpoint returns 429 if the shared budget is exhausted. **Initial backfill scope:** backfill is scoped to **2 years** initially (50 symbols × ~504 trading days ≈ 25,200 calls ≈ 42 days at 600/day cap). Full 10-year backfill is tracked in TF-003 (pending Twelve Data Grow upgrade or alternative provider evaluation). **API key sanitization:** a URL-sanitization helper is used at every Twelve Data fetch call site to strip the `apikey` query parameter before any logging; response bodies logged are truncated to non-sensitive fields only. **Symbol-count tracking**: the daily run emits a metric of distinct symbols processed; if count > 600, an alert is appended to the Monday email.
- **Non-goals:** switching to a paid provider; multi-symbol batch (paid tier only). **Intraday live-quote refresh is in scope via the companion REQ-WC-013a below** (sharing the same 600/day budget counter).

### REQ-WC-013a: Live-quote refresh for in-app price freshness (companion to REQ-WC-013)

User intent (M0h discussion 2026-05-11): Travis wants intraday "as of" prices on the Wealth dashboard during market hours, while keeping daily EOD canonical for `historical_price`. This REQ shares Twelve Data's 600/day shared-budget counter with REQ-WC-013 (the EOD cron) — no separate budget, no separate provider.

- **Acceptance:**
  - **New D1 table `live_quote`:**
    - `symbol TEXT PRIMARY KEY`
    - `price TEXT NOT NULL` (canonical decimal string, scale 8 per REQ-WC-004)
    - `currency TEXT NOT NULL DEFAULT 'USD'`
    - `fetched_at INTEGER NOT NULL` (Unix epoch ms; this is the application clock, separate from any Twelve-Data response timestamp)
    - `source TEXT NOT NULL DEFAULT 'twelve_data'`
    - `is_stale INTEGER NOT NULL DEFAULT 0` (0/1; set to 1 when last fetch attempt fell back to the cached value due to budget exhaustion, market-closed, or upstream error)
    - **NOT in the append-only / no-delete trigger set** (REQ-WC-009): `live_quote` is a freshness cache, not audit-trail data. UPDATE and DELETE are permitted. The table is declared without a DELETE-rejection trigger and without an UPDATE-rejection trigger.
  - **New endpoint `GET /wealth/api/brokerage/quotes?symbols=AAPL,VTI,SPY`** (auth-gated like other wealth routes per REQ-WC-002):
    - Parse `symbols` query param (comma-separated, lowercased then uppercased after trim). Maximum **50 symbols per request** → 413 if exceeded.
    - For each symbol: validate against `^[A-Z0-9.^]{1,12}$` (same regex as REQ-WC-013). Invalid symbol → return 400 with sanitized message; do NOT call Twelve Data.
    - For each symbol: read `live_quote.fetched_at` from D1.
      - If `fetched_at > (now - 15 minutes)` → return cached row.
      - Else, check whether the US equity market is currently open (Mon-Fri, 09:30-16:00 America/New_York, accounting for DST; use `Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York' })`).
        - **Market closed:** read latest `historical_price.close` for the symbol (most recent `trade_date`); return that with `is_stale: false` (it IS the canonical price outside market hours). Do NOT fetch Twelve Data.
        - **Market open + budget available:** fetch Twelve Data `quote` endpoint (`https://api.twelvedata.com/quote?symbol=...&apikey=...`), upsert `live_quote` row with `is_stale: 0`, return.
        - **Market open + budget exhausted:** return previous cached `live_quote` (or `historical_price.close` if `live_quote` is empty for this symbol) with `is_stale: 1`. Do NOT call Twelve Data. Do NOT 5xx — degraded mode is the contract.
    - **Response shape (per symbol):** `{ symbol, price, currency, fetched_at, source, is_stale }`.
    - **Shared 600/day budget enforcement (CRITICAL):** the call-count counter queried by REQ-WC-013's EOD cron is the SAME counter. Both the cron and `/quotes` write IngestionLog rows with `source='twelve_data'`, and both check the daily total before issuing an API call. The EOD cron at 07:30 UTC always wins (it fires first relative to the next US market open at 13:30/14:30 UTC), so the cron's 50 symbols/day are guaranteed; `/quotes` consumes the remaining 550/day. Hot day for `/quotes`: 11 visible symbols × 4 windows/hour × 6.5 hours = 286 calls; comfortably below 550. Worst-day burst-cap is what the 600 daily ceiling enforces.
    - **IngestionLog batching:** `/quotes` writes ONE IngestionLog row per HTTP request (not per symbol), with `source='twelve_data'`, `records_processed = symbols_fetched_count`, `records_failed = symbols_failed_count`, `error_detail` set to a JSON blob of failed symbols if any. This keeps IngestionLog volume sane (one row per page load, not 50).
    - **API key sanitization:** same as REQ-WC-013 — the `apikey` query parameter MUST be stripped from any logged URL or response body; response bodies are truncated to non-sensitive fields. **`X-Internal-Key` and `apikey` MUST NOT appear in any IngestionLog `error_detail`, server log, or Sentry breadcrumb.**
    - **Defense in depth:** the `/quotes` route handler MUST independently check `WEALTH_ALLOWED_EMAILS` against `event.locals.user.email` (do NOT rely solely on the `hooks.server.ts` guard — same defense-in-depth pattern as the rest of REQ-WC-002).

- **Frontend (deferred to `crm/frontend-brokerage` worktree, FB-T0X):**
  - Wealth pages with prices (`networth`, `holdings`, `accounts/[id]`) call `/quotes?symbols=...` on mount and on `visibilitychange` when the tab regains focus.
  - Display "Prices as of HH:MM" timestamp computed from `min(fetched_at)` across the visible symbols, formatted in the user's locale.
  - Show a "stale" indicator (warning icon + tooltip) when `is_stale: true` for any displayed symbol, or when `max(fetched_at)` is > 30 minutes old.
  - DO NOT poll on a timer — page-load + visibilitychange covers user intent without burning quota on idle tabs.

- **KAT (added to crm/workers-brokerage as new task BR-T08):**
  - 5 symbols requested, all `fetched_at` within 15 min → zero Twelve Data calls; all 5 returned from `live_quote`. IngestionLog row count unchanged.
  - 5 symbols requested, all stale (or absent), market OPEN → 5 Twelve Data calls; all 5 rows upserted; one IngestionLog row written.
  - 51 symbols requested → 413.
  - Symbol `'; DROP TABLE--` requested → 400 (regex rejects); no DB writes, no Twelve Data call.
  - 5 symbols requested, market CLOSED → zero Twelve Data calls; returns latest `historical_price.close` for each; `is_stale: false` (closed-market EOD is canonical).
  - 5 symbols requested, market OPEN, daily budget already exhausted → zero Twelve Data calls; returns previous `live_quote` rows with `is_stale: true` (or `historical_price.close` if no `live_quote` row exists); HTTP 200 (NOT 429 — degraded mode contract).
  - Request without valid wealth session cookie → 302 to login (per REQ-WC-002).
  - Request with session cookie for `amycsparks@gmail.com` → 404 (per REQ-WC-002).

- **CPU + wall-clock budget (REQ-WC-017):** `/quotes` handler with a 10-symbol payload (all cache misses, market open) must complete in ≤ 8 ms CPU time and ≤ 2 seconds wall-clock end-to-end (10 calls × ~150ms latency at 8 req/min → about 1.5 sec). 50-symbol payload may exceed 8 ms CPU if all are cache misses; in practice cache-miss-rates trend low because the 15-min cache absorbs page-load bursts. Document the 50-symbol worst case at deploy time; if exceeded, Workers Paid is required.

- **Non-goals:** websocket streaming; sub-15-minute refresh interval (would consume budget faster than is sustainable); pre-market / after-hours pricing; cryptocurrency or forex symbols (regex would reject anyway); refresh on background timer (only page-load + visibilitychange).

### REQ-WC-014: Cutover migration is reversible within the soak window
- **Acceptance:** before cutover, a full local SQLite snapshot (`sqlite3 .backup`) AND a D1 snapshot (`wrangler d1 export`) are taken; the dump-and-load is scripted in **TypeScript** at `scripts/migrate-from-sqlite.ts` (in the sparkry-crm repo, invoked via `npx tsx`); a rollback script `scripts/rollback-from-d1.ts` re-loads SQLite from the D1 export; both are tested end-to-end against a copy of the live DB and a non-prod D1 database (`sparkry-crm-staging`) before the real cutover. Migration includes **Fernet→AES-GCM re-encryption** of PlaidItem access_tokens (A8). Value-level spot-check: 9 rows per table (5 random rows PLUS 4 targeted edge-case rows: a value ending in `.50` for trailing-zero preservation, a value with maximum fractional places such as `0.12345678`, a value of `0.00`, and the row with the largest absolute value) across 6 tables (`position_snapshot`, `plaid_account_balance_snapshot`, `cost_basis_lot`, `historical_price`, `brokerage_transaction`, `realized_gain_loss`) = 54 rows total — compared byte-by-byte SQLite vs D1. **Rollback fidelity note:** `rollback-from-d1.ts` exports the CURRENT D1 state (including any new rows written by the Workers cron after cutover), NOT just the pre-migration snapshot. This ensures post-cutover cron data is not lost. **Reversibility window = soak window duration** (3 consecutive cron successes, ≥3 calendar days). LM-T01 (drop Plaid tables in local SQLite) MUST run AFTER the soak window closes so rollback within the window has a working SQLite target. The LM team-lead's polling DEADLINE_EPOCH is set to **168 hours (7 days)** from cutover, matching the extended-soak protocol maximum. D1-T04 test: SQLite → migrate → fire cron once → rollback → assert all post-cutover D1 rows are reflected in SQLite.
- **Non-goals:** rollback beyond the soak window once new Plaid Items have been linked.

### REQ-WC-015: Local brokerage routes removed after cutover
- **Acceptance:** post-cutover, the local FastAPI app no longer mounts `brokerage_router` or `plaid_router`; `GET http://localhost:8000/api/brokerage/networth` returns 404; the local SvelteKit dashboard no longer has `/brokerage/*` routes; a smoke test asserts both 404s. The local launchd plist files (`com.sparkry.plaid-balance-sync.plist`, `com.sparkry.accounting-prices-daily.plist`) are deleted from `~/Library/LaunchAgents/` (not just unloaded); reboot of the laptop does not re-load them. The Cloudflare tunnel `plaid-oauth-return` is deleted (`cloudflared tunnel list` does not list it).
- **Non-goals:** deleting the Python source (kept as reference + emergency rollback).

### REQ-WC-016: No cross-references between CRM and Wealth in nav or layout
- **Acceptance:** the CRM dashboard does not link to or mention `/wealth/*`; the Wealth dashboard does not link to or mention `/customers`/`/work-orders`/`/invoices`; visual inspection test (grep) on the rendered HTML asserts neither group's bundle contains the other group's route strings.
- **Non-goals:** preventing the user from typing the URL directly.

### REQ-WC-017: Workers free-tier CPU budget enforced (with two-level benchmark)
- **Acceptance:** the hot reconciliation summary handler completes in ≤ 8 ms CPU time on the local `wrangler dev` profiler with a fixture DB containing 10 PlaidItems × 5 accounts × 20 positions × 1000 historical prices **AND** ≤ 250 ms wall-clock end-to-end on a one-time staging-deployment profiled request against `sparkry-crm-staging` D1 with the same fixture (measured via `curl --write-out '%{time_total}'`). Note: D1 network I/O does NOT count against CPU time — only actual compute (JavaScript execution) counts. The two-level test exists because `wrangler dev` uses local SQLite while production D1 requests have network round-trips that inflate wall-clock without affecting the CPU meter. Required indexes documented in the Drizzle schema:
  - `(account_id, snapshot_date DESC)` on `plaid_account_balance_snapshot` (Plaid sync table — column is `snapshot_date`)
  - `(account_id, symbol, as_of DESC)` on `position_snapshot` (column is `as_of`)
  - `(symbol, trade_date DESC)` on `historical_price`
  
  Note: the `account_balance_snapshot` history table (separate from `plaid_account_balance_snapshot`) uses `as_of`, not `snapshot_date` — do not conflate the two. CI benchmark fails if either limit exceeded. Workers Paid ($5/mo) is the documented escape hatch if CPU budget cannot be met on request handlers after index tuning.

  **Local `wrangler dev` benchmarking is a proxy, not the authoritative measure.** `wrangler dev` does not replicate production V8 CPU isolation. The authoritative CPU-budget verification is post-deploy observation in the Cloudflare dashboard (Workers → Deployments → CPU Time metric). Set conservative local thresholds: target ≤ 3 ms CPU in `wrangler dev` to provide headroom for production V8 overhead. The pre-cutover checklist includes a post-deploy CPU verification: "After staging deploy, fire each cron handler at least once via `wrangler dev --test-scheduled` AND inspect the Cloudflare dashboard CPU metric for the staging worker — both must be ≤ 8 ms (request handler) or ≤ 10 ms per cron iteration."

  CPU-budget acceptance for the ingest handlers: the `POST /wealth/api/internal/ingest/brokerage-csv` handler with a 100-row payload must complete within the 10 ms CPU budget (BR-T07 benchmark). If the 100-row benchmark exceeds 10 ms, the documented escape hatch is Workers Paid (do NOT use `db.batch()` chunking — it violates per-record error isolation). Cron handler wall-clock budgets: R2 backup ≤ 15 minutes wall-clock on Workers Paid (REQUIRED from day one); **Plaid sync ≤ 30 seconds wall-clock on Workers Paid (REQUIRED from day one** — network latency × N Items exceeds 30-second Workers Free limit with 5+ active Items; $5/mo Workers Paid covers all three crons); Twelve Data ingest ≤ 8 minutes wall-clock on Workers Paid (REQUIRED from day one — 50 symbols at 8 req/min = 6.25 minutes, exceeding Workers Free 30-second limit). Document in REQ-WC-017 test.
- **Non-goals:** premature optimization beyond the hot path; instrumenting every endpoint.

### REQ-WC-018: Daily D1 → R2 backup (NDJSON with FK-aware restore order)
- **Acceptance:** a Workers scheduled handler bound to cron **`"0 12 * * *"`** (12:00 UTC daily) runs on **Workers Paid** (required from day one — paginated export of 50,000+ `historical_price` rows requires the 15-minute scheduled wall-clock that only Workers Paid provides). The handler iterates every wealth D1 table (the 13 migrated tables; `live_quote` from REQ-WC-013a is EXCLUDED — it is a freshness cache that regenerates from upstream and has no value in a snapshot) via paginated `SELECT * FROM <table> LIMIT 5000 OFFSET ?` in a loop, writing NDJSON to R2 as sequential chunk objects at path `sparkry-crm-backups/wealth/daily/<table>/<YYYY-MM-DD>/<chunk-NNN>.ndjson`. **Plaid token exclusion:** for the `plaid_item` table, the `access_token_encrypted` column MUST be replaced with the literal sentinel string `'BACKUP_REDACTED'` in every NDJSON row. Live encrypted Plaid tokens are NEVER written to R2 backups — even though they are AES-GCM encrypted at rest in D1, double-redundant storage of secrets in a separate location is avoided. The restore script repopulates `access_token_encrypted` from a separately-secured manual export only when a full restore with live tokens is required. For most operational scenarios (data corruption, schema migration), restoring with `'BACKUP_REDACTED'` and triggering Plaid re-link is sufficient. If the cron exits before all chunks land (approaching wall-time limit), a `sparkry-crm-backups/wealth/daily/<table>/<YYYY-MM-DD>/_INCOMPLETE` marker is written. **Retention: 30 days, enforced by a SEPARATE prune cron** (NOT the backup handler) — see the two-token split below. The backup handler itself NEVER lists or deletes objects; pruning runs in a distinct scheduled handler with its own narrower secret. The companion restore script `scripts/restore-from-r2.ts --date YYYY-MM-DD` re-inserts in **explicit FK-parent-first order**:
  FK safety in D1 restore is enforced by inserting parent-table rows before child-table rows (this order is the safety mechanism; PRAGMA statements are not available in D1). Post-restore integrity check is a SELECT-based cross-table count comparison — for example, `SELECT COUNT(*) FROM brokerage_transaction WHERE account_id NOT IN (SELECT id FROM account)` must return 0. All such cross-table checks must return 0.
  Insert order:
  1. `plaid_item` (no inbound FKs to other wealth tables)
  2. `historical_price` (no FKs to other wealth tables)
  3. `account` (FK → `plaid_item.id`, nullable)
  4. `expected_account` (FK → `account.id` via `resolved_account_id`, nullable — must come AFTER account)
  5. `brokerage_transaction`, `position_snapshot`, `realized_gain_loss`, `account_tag`, `cost_basis_lot`, `account_balance_snapshot`, `plaid_account_balance_snapshot` (all FK → `account.id`)
  6. `ingestion_log` (no FK to wealth tables)
  7. `audit_events` (no FK to wealth tables)

  The backup Cloudflare API tokens are split into two to prevent a single token compromise from enabling backup deletion:
  - **`R2_BACKUP_WRITE_TOKEN`**: WRITE-only permission scoped to `sparkry-crm-backups/wealth/*`. Used by the backup cron handler for writing new NDJSON chunk objects. Stored in the cron Worker secrets (provisioned in M0e). A write-only token cannot delete or list objects.
  - **`R2_BACKUP_PRUNE_TOKEN`**: LIST + DELETE permission scoped to `sparkry-crm-backups/wealth/*` objects older than 30 days. Used only by a separate prune-cron handler (NOT the backup handler). Apply an R2 lifecycle policy scoped to `wealth/daily/` objects with a 30-day retention if Cloudflare supports it; otherwise a separate prune cron using `R2_BACKUP_PRUNE_TOKEN` walks date prefixes and deletes objects older than 30 days. The prune token is provisioned only on the prune-cron handler — the backup handler NEVER has access to `R2_BACKUP_PRUNE_TOKEN`.
  
  A separate restricted token with R2 READ permission (`R2_RESTORE_TOKEN`) is provisioned in the accounting Doppler config (NOT Workers — used only by `scripts/restore-from-r2.ts` invoked manually as `doppler run --project accounting --config dev -- npx tsx scripts/restore-from-r2.ts --date YYYY-MM-DD`). Pre-cutover checklist verifies: (a) `R2_BACKUP_WRITE_TOKEN` is listed in `wrangler secret list --name sparkry-crm-cron`; (b) `R2_BACKUP_PRUNE_TOKEN` is provisioned on the prune-cron handler secret surface; (c) `R2_RESTORE_TOKEN` is listed in `doppler secrets --only-names --project accounting --config dev`; (d) the `R2_BACKUP_WRITE_TOKEN` can write but NOT list or delete objects; (e) the `R2_BACKUP_PRUNE_TOKEN` can list and delete but NOT write objects; (f) the read token can list objects at the backup prefix (`wrangler r2 object list sparkry-crm-backups --prefix wealth/`).
  
  The restore script MUST bind every TEXT-typed column (including all decimal columns) as JavaScript strings to D1 prepared parameters — NEVER coerce to JS `number`. A restore-time assertion checks: no decimal column value parsed from NDJSON has type `number` in JavaScript (all must be strings). This preserves the canonical decimal string invariant through the restore path. Restore is tested in staging: load yesterday's NDJSON into a fresh `sparkry-crm-staging` D1, assert row counts per table match the source export, assert all SELECT-based FK cross-checks return 0 violations.
- **Non-goals:** point-in-time recovery beyond daily granularity; off-Cloudflare backups; SQL-dump format (NDJSON with documented restore order is functionally equivalent).

### REQ-WC-019: All Workers Pages Secrets enumerated and provisioned
- **Acceptance:** the following Pages Secrets are set via `wrangler pages secret put <NAME> --project-name sparkry-crm` BEFORE the cutover, verified via `wrangler pages secret list --project-name sparkry-crm`:
  1. `PLAID_CLIENT_ID`
  2. `PLAID_SANDBOX_SECRET`
  3. `PLAID_PRODUCTION_SECRET`
  4. `PLAID_ENV` (sandbox|production)
  5. `PLAID_TOKEN_ENC_KEY` (NEW AES-GCM key, distinct from the Fernet key used during migration)
  6. `TWELVE_DATA_API_KEY`
  7. `WEALTH_INTERNAL_KEY` (32-byte random, standard base64 per RFC 4648 §4 — generated via `openssl rand -base64 32`; standard base64 may include `+`, `/`, `=` which are safe in HTTP header values)
  8. `WEALTH_ALLOWED_EMAILS` (comma-separated, default `travis@sparkry.com`)
  9. `RESEND_API_KEY` — **already set on the CRM Pages project**; the wealth handlers inherit the binding via the shared Pages project; verification step asserts the key is present in the list (does not re-create it).

  **WEALTH_KV KV namespace binding** is declared in BOTH `wrangler.toml` (Pages) AND `wrangler.worker.toml` (Cron Worker). The namespace is created during M0e-kv (see runbook). Pre-cutover checklist verifies: `wrangler pages deploy --dry-run` (Pages) AND `wrangler deploy --dry-run --config wrangler.worker.toml` (Cron Worker) — both must show WEALTH_KV resolved with a real namespace ID (not the placeholder `<kv-namespace-id>`).

  **Cron Worker secrets (separate surface).** The cron Worker (`sparkry-crm-cron`) is a SEPARATE deployment from Pages and does NOT inherit Pages secrets. Every secret consumed by `handlePlaidSync`, `handlePlaidStaleAlert`, `handleTwelveDataIngest`, and `handleR2Backup` MUST ALSO be provisioned on the cron Worker via `wrangler secret put <NAME> --name sparkry-crm-cron`. Required secrets: `PLAID_CLIENT_ID`, `PLAID_SANDBOX_SECRET`, `PLAID_PRODUCTION_SECRET`, `PLAID_ENV`, `PLAID_TOKEN_ENC_KEY`, `TWELVE_DATA_API_KEY`, `WEALTH_INTERNAL_KEY`, `RESEND_API_KEY`, `SENTRY_DSN`, `R2_BACKUP_WRITE_TOKEN` (required by `handleR2Backup`; Cloudflare API token with R2 **WRITE-only** permission to `sparkry-crm-backups/wealth/*` — the backup handler MUST NOT have LIST or DELETE permission, per the two-token split in REQ-WC-018; pruning is done by the separate `R2_BACKUP_PRUNE_TOKEN` provisioned only on the prune-cron handler). Note: `RESEND_API_KEY` and `SENTRY_DSN` must be set here even if they already exist on the Pages project — the two deployments have separate secret surfaces. `SENTRY_DSN` is required because the existing `src/worker.ts` is wrapped in `withSentry`. `WEALTH_ALLOWED_EMAILS` is NOT included on the cron Worker — cron handlers have no user-facing auth context and do not read this secret. Pre-cutover checklist verifies via `wrangler secret list --name sparkry-crm-cron`.
  
  Cloudflare Access policy verified to bypass `/wealth/api/internal/*` paths (M0h).
  
  **Doppler-side secrets** (NOT Workers — used only by the local Python importers in the accounting repo):
  - `WEALTH_API_BASE=https://internal.sparkry.ai`
  - `WEALTH_INTERNAL_KEY` (same value as Workers side; mirrored for the importer scripts)
  - `WEALTH_TARGET_DEFAULT` (initially `local`, flipped to `cloud` at cutover step 7i)
  
  Verified via `doppler secrets --only-names | grep ^WEALTH_`.
- **WEALTH_INTERNAL_KEY rotation procedure:** Rotation order: Doppler (accounting config) → Workers Pages → Cron Worker. Two-key rotation window: during the ~30s propagation window, the Worker accepts BOTH the old and new keys. **Maximum two-key overlap window = 5 minutes from the moment the new key is set in Wrangler.** After 5 minutes, the implementation MUST stop accepting the old key; this is enforced via **Workers KV** (binding name `WEALTH_KV`). The rotation procedure writes a row to `WEALTH_KV` keyed `key_rotation:WEALTH_INTERNAL_KEY` with value `{previous_key, rotated_at_epoch_ms}` — the actual previous key value (NOT a hash), encrypted at rest by KV (same trust boundary as the active Workers Secret). The Worker handler reads BOTH the current key (from the `WEALTH_INTERNAL_KEY` secret) AND the previous key (from KV) during the 5-minute window and uses `crypto.subtle.timingSafeEqual()` for BOTH comparisons separately, then ORs the boolean results. The Worker checks `WEALTH_KV.get('key_rotation:WEALTH_INTERNAL_KEY')` once per request during the rotation window (result cached in `caches.default` for 60 seconds). After 5 minutes from `rotated_at_epoch_ms`, the old key is unconditionally rejected (401 returned without checking); KV entry expiry is set to 5 minutes at write time. `WEALTH_KV` binding MUST be declared in both `wrangler.toml` (Pages) AND `wrangler.worker.toml` (Cron Worker). IC-T02 test: present old key 6 minutes after rotation → 401. Steps: (1) generate a new key (`openssl rand -base64 32 | tr -d '\n'`); (2) write `{previous_key: <old_key>, rotated_at_epoch_ms: Date.now()}` to `WEALTH_KV` with 5-minute expiry; (3) set in Doppler (accounting config) via `printf '%s' "$NEW_KEY" | doppler secrets set WEALTH_INTERNAL_KEY --project accounting --config dev`; (4) set in Workers Pages via `printf '%s' "$NEW_KEY" | wrangler pages secret put WEALTH_INTERNAL_KEY --project-name sparkry-crm`; (5) set in Cron Worker via `printf '%s' "$NEW_KEY" | wrangler secret put WEALTH_INTERNAL_KEY --name sparkry-crm-cron`; (6) local importers MUST retry once on 401 with 30-second backoff (IC-T02 test: simulate 401 → retry → 200); (7) verify with a test POST to `/wealth/api/internal/ingest/brokerage-csv` using the new key. Workers MUST NOT log the `X-Internal-Key` value at any log level. Importers MUST NEVER log the value of the `X-Internal-Key` header at any log level.
- **Non-goals:** secret rotation automation (manual rotation documented but not scheduled).

---

## Anti-hallucination protocol

The user explicitly called out that AI agents tend to fabricate "passing" validations. Every test / verification / quality-gate output gets an independent review pass.

This section summarizes the pattern; the authoritative validator prompt and capture protocol live in runbook Section 5 only. Do not duplicate text here to avoid drift.

### Validation-review agent prompt template

See runbook Section 5 for the authoritative validator prompt (used verbatim by every team-lead). The spec does not duplicate the prompt text to avoid wording divergence between the two documents.

The /qpipeline runbook spawns this validator after EVERY test-gate, EVERY review round result, and EVERY task-completion claim. If a phase's validator returns REFUTED, the phase fails and the task re-enters the review-loop. If INCONCLUSIVE returns 3 times, the team-lead escalates to the orchestrator (which runs the sub-team protocol from the runbook Section 1).

---

## Security at rest

Five security controls protect data at rest, grouped into three layers. None depend on a single point of failure.

| Layer | Mechanism | Protects against |
|---|---|---|
| Platform | Cloudflare D1 encrypts all storage server-side (AES-256, managed keys). Workers Secrets encrypted at rest. R2 objects encrypted at rest. | Cloudflare infrastructure-level data theft (operationally unlikely; the dependency is on Cloudflare's own security posture). |
| Application | Plaid `access_token_encrypted` is **additionally** encrypted at the application layer via Web Crypto AES-GCM with `PLAID_TOKEN_ENC_KEY` (Workers Secret, 256-bit). | DB-only exfiltration without Workers Secrets access — attacker has ciphertext but no key. |
| Sentinel | On Item disconnect, `access_token_encrypted` is overwritten with literal `"REVOKED"` before status change. | Stale ciphertext lingering in subsequent backups/snapshots after a user-initiated disconnect. |
| Transport | HTTPS-only (Cloudflare default; no plaintext fallback). | Network-level interception. |
| Backup encryption | R2 backup objects are encrypted at rest by Cloudflare. Daily exports are auth-required to read. | Backup blob exfiltration alone. |
| Backup ciphertext exposure | R2 backups contain application-layer-encrypted Plaid token ciphertexts. Combined compromise of (a) R2 access AND (b) `PLAID_TOKEN_ENC_KEY` yields the same exposure as live D1. The `R2_BACKUP_WRITE_TOKEN` (WRITE-only to `sparkry-crm-backups/wealth/*`) cannot decrypt the ciphertexts — it never sees `PLAID_TOKEN_ENC_KEY`. Also note: live `access_token_encrypted` is REPLACED with `'BACKUP_REDACTED'` sentinel before R2 write, so the ciphertexts are not even present in the backup. Accepted risk consistent with the single-user scope. | Combined D1-storage + Workers-Secrets breach (same threat model as the main application layer). |

**Threat model accepted:** simultaneous compromise of (a) Cloudflare D1 storage AND (b) the `PLAID_TOKEN_ENC_KEY` Workers Secret would expose access tokens. This is not separately mitigated; it requires breaching Cloudflare's tenant isolation. For a single-user personal scope, this is an accepted risk consistent with the original Plaid Phase 1 spec.

**Out of scope for this migration:** customer-managed keys (CMK), HSM, key rotation automation, key-derivation from a passphrase. All of these are reasonable upgrades but each adds operational complexity not justified at the single-user level.

---

## Known risks / open questions

1. **Twelve Data quota.** 800 req/day free tier; daily-cap of 600 req/day enforced in REQ-WC-013 (wider safety buffer). Free tier is 1 call per symbol per day (batch is paid tier only). At 50 symbols: 50 calls/day for daily incremental. Initial backfill is scoped to 2 years (~25,200 calls ≈ 42 days at 600/day cap). Full 10-year backfill (~126,000 calls ≈ 210 days) is tracked in TF-003 pending Twelve Data Grow ($29/mo) upgrade or alternative provider evaluation. If the backfill timeline is unacceptable, swap to Polygon basic ($29/mo) for backfill only — single-file change in the price-source layer. **Twelve Data free tier `outputsize` limit:** the free tier may limit `outputsize` to a small value (e.g., 30 or 100 data points rather than 5000). Verify `outputsize=5000` is supported before implementing the backfill chunking strategy. The PL-T02 bundle-size sub-step includes a probe call to verify the actual free-tier limit; if limited, the backfill chunk size must be adjusted accordingly.
2. **D1 free-tier size limits.** 5 GB per database (free), 10 GB (paid). Current brokerage tables are ~30 MB in SQLite. Headroom for many years. Daily R2 backup retention 30 days; older objects pruned.
3. **Workers free-tier CPU time and wall-clock.** On Workers Free, BOTH request handlers and scheduled handlers have a **30-second wall-clock limit**. On Workers Paid, request handlers remain at 30 seconds wall-clock while scheduled handlers gain **15 minutes** wall-clock. **The 10 ms CPU limit applies only to Workers Free invocations; Workers Paid removes the per-request CPU cap.** REQ-WC-017 enforces ≤ 8 ms on the hot reconciliation handler with documented indexes. **Workers Paid ($5/mo) is REQUIRED from day one** for: (a) the R2 backup cron (15-minute wall-clock, see REQ-WC-018); (b) the Twelve Data ingest cron (6.25 minutes wall-clock for 50 symbols at 8 req/min, exceeds the 30-second Workers Free limit, see REQ-WC-013); (c) the Plaid sync cron (network latency × N Items exceeds 30-second wall-clock with 5+ active Items, see REQ-WC-006). The $5/mo Workers Paid cost target covers all three crons. Workers Paid is additionally the documented escape hatch if: (d) the CPU budget cannot be met on request handlers after index tuning.
4. **Workers Cron Trigger billing tier.** Cron Triggers are available on Workers Free. The free plan grants the same cron quota as paid; the constraint is per-request CPU, which the cron iterates per-Item so each iteration must stay within budget. The R2 backup and Twelve Data ingest crons explicitly require Workers Paid — see Known Risk #3.
5. **D1 transaction semantics.** D1's `db.batch([...])` is atomic per-batch, no nested SAVEPOINT. The "per-row savepoint" pattern from Python maps to per-row try/catch + per-Item batch. Slightly weaker isolation but functionally equivalent for the daily sync's idempotency contract.
6. **D1 region affinity.** Specify the primary region at D1-creation time to match Travis's location: `wrangler d1 create sparkry-crm-prod --location wnam` (West North America). Mismatched primary adds 100-200 ms per D1 write on synchronous handlers (especially exchange and sync-now). Verify with `wrangler d1 info`.
7. **Plaid sandbox vs production redirect URIs.** Both must be re-registered (manual in Plaid dashboard). Screenshots of each dashboard's allowed-redirect-URIs list are captured pre-cutover as evidence.
8. **Plaid Fernet→AES-GCM token migration.** Existing PlaidItem rows in the local SQLite are Fernet-encrypted; D1 needs AES-GCM. The migration script does a re-encryption pass (see A8) — touches plaintext in memory for the migration window, then the process exits.
9. **AuditEvent rollback on local DB.** Pre-condition: the query `SELECT COUNT(*) FROM audit_events WHERE entity_id IS NOT NULL` must return 0; verified pre-cutover and asserted in runbook LM-T01.
10. **Worker bundle size for Plaid SDK.** Official `plaid` npm package is ~1 MB compressed unminified. Worker bundle limit is 1 MB compressed. Verify with `wrangler deploy --dry-run` early; if it exceeds, fall back to direct `fetch()` calls against Plaid's REST API.
11. **Email send from Workers.** Stale-Item alerting uses Resend. The CRM already has `RESEND_API_KEY` in Workers Secrets. Verify a Workers-compatible Resend client exists or call Resend's REST API directly.
12. **Cloudflare Access policy scope.** The existing CRM Access policy must be reviewed to confirm `/wealth/*` is covered AND `/wealth/api/internal/*` has an explicit bypass rule. The internal endpoints rely on `X-Internal-Key` header auth at the application layer; an Access SSO redirect would break the Python importer scripts.
13. **Workers cold start on cron.** Free-tier crons may incur 400-1000 ms cold start. Acceptable for the 2 AM Plaid sync; not on the critical path of user-facing requests.
14. **`historical_price` table is empty at cutover.** Twelve Data backfill takes ~26 days at quota. The migration script INCLUDES the existing yfinance-populated `historical_price` rows from SQLite, so the chart continues to show the existing history from day 1 — Twelve Data only fills NEW data going forward. Documented separately so the team-lead doesn't assume a clean slate.
15. **Cron scheduling separation.** Five Workers crons are scheduled. To avoid handler overlap, the daily crons are deliberately spaced:
    - `30 7 * * *` (07:30 UTC) — Twelve Data price ingest (REQ-WC-013)
    - `7 10 * * *` (10:07 UTC) — Plaid balance sync (REQ-WC-006)
    - `0 12 * * *` (12:00 UTC) — D1 → R2 backup (REQ-WC-018)
    - `0 14 * * MON` (14:00 UTC Mondays) — Plaid stale-Item alert email (REQ-WC-007) — weekly; no daily collision risk
    - `0 */6 * * *` (every 6 hours) — PlaidItem abandoned-placeholder cleanup (REQ-WC-005)
    
    Backup intentionally runs AFTER price ingest completes so the backup includes the latest day's prices. ≥ 1.5h gap between consecutive daily crons. The Monday cron runs 2h after the last daily; on Mondays it fires alongside the daily backup but doesn't share state with it. The cleanup cron runs every 6 hours and is unlikely to overlap with daily crons except transiently.
16. **R2 backup format is NDJSON, not SQL dump.** Workers cannot natively produce `wrangler d1 export`-format SQL dumps (no wrangler CLI access from within the runtime). The backup handler walks every wealth D1 table with `SELECT *` and emits NDJSON. The restore script `scripts/restore-from-r2.ts` consumes the NDJSON and re-INSERTs. Functionally equivalent for restore; not human-runnable as SQL.

---

## Acceptance criteria for migration completion

1. `https://internal.sparkry.ai/wealth/` loads the Wealth dashboard with net-worth chart from D1 data.
2. `https://internal.sparkry.ai/wealth/desk/connections` shows existing Plaid Items (migrated from SQLite, tokens re-encrypted to AES-GCM); a fresh Add Connection flow successfully links a sandbox Item end-to-end.
3. Daily Cron Trigger has run successfully for 3 consecutive days; `plaid_account_balance_snapshot` rows are written for every mapped account.
4. Local `http://localhost:8000/api/brokerage/networth` returns 404; local `/brokerage/*` SvelteKit routes are gone.
5. `scripts/migrate-from-sqlite.ts` (sparkry-crm) was run, and `scripts/rollback-from-d1.ts` was successfully tested against a copy of the D1 data before cutover (the actual rollback wasn't needed).
6. Plaid dashboard's allowed-redirect-URIs list contains `https://internal.sparkry.ai/wealth/desk/connections/oauth-return` (in BOTH sandbox AND production) and NOT the old tunnel URL.
7. Cloudflare tunnel `plaid-oauth-return` is decommissioned (`cloudflared tunnel delete` ran).
8. Validation-review agent has independently SUBSTANTIATED every "all green" claim from the execution pipeline (no INCONCLUSIVE or REFUTED carry-over).
9. Travis can connect a real production Plaid Item (Chase, Vanguard, etc.) via OAuth and see balances flow through to the Wealth dashboard within 24 hours.

---

## Tracked follow-ups

Items explicitly deferred from this migration with concrete review dates. Each must be revisited at the specified date or sooner.

### TF-001: /wealth/transactions page port (target: cutover + 60 days)

The `/brokerage/transactions` page on the local dashboard is preserved during this migration (per Sub-team Q2 decision). A follow-up port to `/wealth/transactions` on Cloudflare is required. Until that follow-up lands, the local `/brokerage/transactions` page remains the canonical transactions view. Target completion: cutover date + 60 days. At that point, LM-T03's exclusion of `dashboard/src/routes/brokerage/transactions/` is revisited and the route is removed from the local dashboard.

### TF-002: Zone-level Travis-only Cloudflare Access policy (target: cutover + 30 days)

The current spec ships with in-app guard only (WEALTH_ALLOWED_EMAILS) for Travis-only access on `/wealth/*`. A second Cloudflare Access policy scoped specifically to `/wealth/*` and allowing only `travis@sparkry.com` would add defense-in-depth at the zone level, blocking Amy at the CDN before the app even sees the request. This was deferred per Sub-team Q4 decision (3/4 consensus: ship in-app guard first; zone-level policy as follow-up).

**30-day risk acceptance (residual risk):** during the 30-day gap between cutover and TF-002 landing, Amy could reach `/wealth/*` pages via the zone-level Access policy (which admits Amy). The in-app guard returns 404 (per inline decision #7 — 404 not 403 to avoid revealing feature existence), but Amy seeing a 404 on a wealth URL still reveals that a request was made. For a single-user personal scope, this is accepted risk. The zone-level policy is prioritized at the first 30-day review — "narrow later often becomes never" (TRAVIS-PERSONA dissent, recorded verbatim).

Target review date: cutover date + 30 days. At that review, add the second Access policy if the follow-up has not been blocked by other priorities.

### TF-003: Full 10-year historical price backfill (target: after Twelve Data Grow upgrade or alternative provider evaluation)

Initial backfill is scoped to 2 years of historical prices (~25,200 API calls ≈ 42 days at the 600/day cap). Full 10-year backfill requires ~126,000 calls ≈ 210 days at the free-tier cap, OR an upgrade to Twelve Data Grow ($29/mo) for higher quota + batch support. Evaluate at cutover + 60 days once the daily incremental pattern is validated.

### TF-004: PLAID_TOKEN_ENC_KEY rotation script (target: cutover + 30 days)

Implement `scripts/rotate-plaid-enc-key.ts` per A8 to walk D1 PlaidItem rows and re-encrypt with a new key. Without it, compromised keys cannot be retired and the fallback list grows indefinitely across rotations. This is a security-relevant follow-up aligned with TF-002's 30-day target.

### TF-005: Plaid REST endpoint mapping (conditional on M0d-pre bundle-size result)

M0d-pre measures the `plaid` npm SDK bundle size via `wrangler pages deploy --dry-run`. If the compressed bundle is >= 800 KB, the Plaid SDK is NOT used and this follow-up becomes active: define the direct Plaid REST endpoint mapping covering at minimum: `POST /link/token/create`, `POST /item/public_token/exchange`, `POST /accounts/balance/get`, `POST /item/remove` — all authenticated with `X-Plaid-Client-Id` and `X-Plaid-Secret` headers. Replace SDK references in PL-T02 with the REST adapter spec. Target date: same sprint as M0d-pre, since the decision gates PL-T02 implementation. If M0d-pre shows bundle < 800 KB, the SDK is used and TF-005 is closed without action.

### TF-006: SESSION_SIGNING_KEY rotation procedure (target: cutover + 30 days)

The Wealth migration's M0h sub-team reconciliation (2026-05-11) ratified the CRM's existing in-app Google OAuth + HMAC session cookie as the auth boundary for `/wealth/*` (replacing the original CF Access design). The Skeptic in that sub-team flagged: today there is no documented rotation story for `SESSION_SIGNING_KEY`. A leak of that Pages secret would compromise both CRM AND Wealth simultaneously (one signing key, two auth surfaces).

Implement `scripts/rotate-session-signing-key.ts` (sparkry-crm repo) following a two-key overlap pattern similar to REQ-WC-019's `WEALTH_INTERNAL_KEY` rotation: (a) generate new key; (b) write `{previous_key, rotated_at_epoch_ms}` to WEALTH_KV (or a parallel `SESSION_KV` if rotation surface should stay isolated from wealth) with a 30-minute expiry (long enough for active sessions to refresh organically); (c) update the Pages secret; (d) the `parseSessionCookie` helper validates the cookie HMAC against BOTH keys during the overlap window; (e) after 30 minutes, the old key is unconditionally rejected; existing cookies must be re-signed via the next request's sliding-window refresh.

Target date: cutover + 30 days. Defer-OK reason: ratified auth is unchanged from CRM's existing pattern (~2 weeks live without incident); rotation is a hygiene investment, not a vulnerability remediation.

### TF-008: Wealth data enrichment — dividends, earnings, news (target: cutover + 30-60 days)

User intent (M0h discussion 2026-05-11): Travis wants a richer "operator dashboard" view of his holdings — dividend dates, ex-div dates, upcoming earnings, splits, and news headlines per held symbol. Twelve Data Free covers EOD prices and intraday quotes but not these enrichments.

**Scope (post-cutover phase):**

1. **`yahoo-finance2` (npm) for dividends/earnings/splits.** Unofficial wrapper around Yahoo Finance public endpoints (~50 KB compressed bundle, runs on Workers). Wired through the same pluggable data-source adapter pattern that hosts Twelve Data today. New endpoints in `crm/workers-brokerage`:
   - `GET /wealth/api/brokerage/dividends?symbol=...` — recent + upcoming dividend payouts (ex-div date, pay date, amount).
   - `GET /wealth/api/brokerage/earnings?symbol=...` — upcoming earnings date + estimate.
   - `GET /wealth/api/brokerage/splits?symbol=...` — historical splits.
   - Cron at `"0 11 * * *"` (11:00 UTC daily) refreshes a `symbol_calendar` D1 table for every symbol in `position_snapshot` so the dashboard doesn't pay round-trip latency per page load.
   - **Reliability caveat:** `yahoo-finance2` is unofficial; Yahoo can rate-limit or break it without notice. Treat the source as best-effort; cache aggressively; fall back to a "data unavailable" UI rather than a 5xx.

2. **Tavily for news per held symbol.** Daily cron at `"0 13 * * *"` (13:00 UTC) iterates top-10 holdings by market value, queries Tavily for `"{company name} stock news"` with `time_range=day`, writes results to a new `news_item(id UUID PK, symbol, headline, url, source, published_at, fetched_at, summary)` D1 table. Free-tier budget (1,000 credits/month) covers 10 symbols × 30 days = 300/month, comfortable headroom. Dashboard renders a `/wealth/news` tab grouped by symbol.

3. **Pluggable data-source adapter architecture.** Introduce `src/lib/server/wealth/data-sources/` with the interface:
   ```typescript
   interface PriceSource { quote(symbol: string): Promise<Quote>; ... }
   interface DividendSource { dividends(symbol: string): Promise<Dividend[]>; ... }
   interface NewsSource { news(query: string, lookback: string): Promise<NewsItem[]>; ... }
   ```
   Sources: `twelve-data.ts` (existing, prices), `yahoo-finance2.ts` (dividends/earnings/splits), `tavily.ts` (news). Each source self-reports a `healthCheck()` and degrades gracefully. Adding a future source (Polygon, Alpha Vantage Premium, IEX) is a one-file change.

4. **D1 tables:**
   - `symbol_calendar(symbol PK, next_dividend_ex_date, next_dividend_pay_date, next_dividend_amount, next_earnings_date, next_earnings_estimate, fetched_at, source)` — UPDATE/DELETE permitted (refresh cache, like `live_quote`).
   - `news_item(id UUID PK, symbol, headline, url UNIQUE, source, published_at, fetched_at, summary)` — append-only with no-delete trigger; old items pruned by a separate cron (>180 days). The UNIQUE(url) constraint prevents duplicate insertions from idempotent cron re-runs.

**Why deferred (not in this migration):**
- Adds ~15-20 SP to a migration already at ~87 SP; would stretch the critical path by ~1 day.
- yahoo-finance2 reliability risk is best evaluated in a follow-up phase rather than as a cutover dependency.
- The pluggable adapter pattern is the right architecture but is deferrable because the current migration only needs one source (Twelve Data).

**Acceptance for TF-008 closure:**
- Both crons green for 3 consecutive days, writing to `symbol_calendar` and `news_item` for every active symbol.
- Dashboard `/wealth/news` tab renders the latest 50 items.
- Dashboard per-holding pages show "Next dividend: $X on YYYY-MM-DD" and "Earnings: YYYY-MM-DD" badges.
- Adapter pattern documented in `docs/architecture/wealth-data-sources.md`; adding a hypothetical 4th source (e.g., Polygon) requires zero changes outside that one new adapter file.

Target review date: cutover + 30 to 60 days. Defer further if Travis Phase 2 Plaid (transactions/investments) lands first and consumes the slot.

### TF-007: Session-cookie HMAC validator pen-test (target: cutover + 30 days)

Companion to TF-006 from the same M0h sub-team. The Skeptic flagged: the session-cookie validator in `parseSessionCookie` (`sparkry-crm/src/lib/server/auth`) is ~2 weeks old and unaudited. Acceptance: a pen-test pass covering timing-side-channel resistance (use `timingSafeEqual` not `===`), replay attack defense (cookie tied to user-agent fingerprint or a server-side nonce), expiry edge cases (expired-but-not-yet-deleted cookies, future-dated `iat`), and HMAC algorithm choice review (HS256 vs HS384 — favor HS256 with a 32-byte key per OWASP). Document findings in `docs/operational/session-cookie-pentest-2026-XX.md`. Block this follow-up only if a P0/P1 finding emerges; default outcome is "validated as-is."

Target date: cutover + 30 days, aligned with TF-006.

---

## Picking this up cold (fresh context)

Read this spec, then read `docs/superpowers/plans/2026-05-10-wealth-cloudflare-migration-runbook.md`. The runbook is the operational task graph — parallel agent assignments, worktree decomposition, merge order. Boot `/qpipeline thorough` against this spec; the pipeline's `plan` phase ratifies the runbook, `execute` phase runs the task graph, and the rest enforces TDD + review-loop + validation-review.
