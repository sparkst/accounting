# `pay.sparkry.ai` Short-Link Redirect — Design Spec

**Date:** 2026-05-26
**Owner:** Travis Sparks
**Status:** Planning bundle ready for `/qloop` review-to-convergence then execution.
**Sibling:** `docs/superpowers/plans/2026-05-26-pay-sparkry-ai-redirect.md`
**Repo touched:** `sparkry-crm` (new Worker + D1 migration + invoice send integration). Spec lives in the accounting repo per the wealth-migration convention.
**Cost target:** $0 incremental. Lives inside the existing `sparkry-crm-prod` D1 and is served from a new Cloudflare Worker on the free tier (Workers Free = 100k req/day). The CRM already has Workers Paid enabled for the wealth crons; the new Worker rides that subscription with negligible additional usage.

---

## Why this spec exists

The CRM at `internal.sparkry.ai` sends invoice emails from `@sparkry.ai` (via Resend) that contain Stripe Payment Link URLs of the form `https://buy.stripe.com/3cs5kE...`. Two problems:

1. **Email deliverability — DMARC alignment.** Modern inbox classifiers penalize mismatch between the visible link domain (`stripe.com`) and the From-header domain (`sparkry.ai`). Stripe's checkout host is widely whitelisted so this is not a hard fail, but it lowers a score that already has other ingredients (a small business sender, transactional content, dollar amounts). Co-hosting the link on a sender-aligned `sparkry.ai` subdomain removes one inbox-classifier penalty entirely (unconfirmed — see Research notes for validation action).
2. **Brand trust & future flexibility.** Recipients see a long opaque `buy.stripe.com/3cs5kE...` URL today. A `pay.sparkry.ai/Xxxxxxxx` link looks like it came from us, is easy to remember if quoted in a follow-up call, and decouples the email body from the payment-provider URL (so if we ever swap rails — add a self-hosted ACH option, change Stripe accounts, etc. — the customer-facing URL stays the same).

A third option of using a third-party shortener (Short.io with `l.sparkry.ai`) was considered and rejected in favor of an in-platform Worker: zero third-party dependency, native D1 storage, native click analytics, and no recurring SaaS cost. Short.io stays available for non-invoice marketing links (newsletter CTAs, social bio links) where its dashboard UX is the point.

## Hard constraints

- **Customer-facing URLs must not require auth.** This is the entire point — recipients of invoice emails are not in `ALLOWED_EMAILS` and never will be. The Worker MUST NOT inherit the CRM's Google-OAuth gate.
- **No new attack surface on `internal.sparkry.ai`.** The redirect Worker is a SEPARATE Workers deployment bound to `pay.sparkry.ai` only. Host-confusion or path-confusion cannot bridge into the CRM admin surface.
- **Open-redirect resistance.** The Worker MUST refuse to redirect to anything outside an allowlisted set of Stripe checkout hosts. A compromise of the CRM session that lets an attacker mint links must NOT yield a generic open redirect.
- **No customer PII in slugs or logs.** Slugs are random base62. Click logs record slug + UA + timestamp + IP-bucket — not the destination URL beyond rail, not the invoice ID in plaintext beyond the internal join.
- **Idempotency.** Re-sending an invoice email MUST reuse the same short URL (so a customer who saved the link from a prior email still pays the right invoice). Re-creating the underlying Stripe link (e.g., after expiry) MUST be able to update the redirect target without changing the slug.
- **Reversibility.** Voiding an invoice MUST mark its short links as revoked; subsequent clicks return a friendly "this payment request was canceled" page (HTTP 410) rather than silently redirecting to an orphaned Stripe link.
- **The CRM's existing payment-link columns stay.** No backfill of historical invoices. Old emails already in customers' inboxes keep using the long Stripe URLs. New sends use short URLs going forward.
- **No JavaScript on the redirect path.** The Worker responds with a 302 (or static HTML error page) — no client-side code, no cookies, no localStorage, no analytics beacons. This keeps the response trivially auditable, compatible with mail-client preview-rendering, and immune to script-based attacks.

## Scope

### In scope (this spec)

- New `payment_link` D1 table in the existing `sparkry-crm-prod` database
- New columns `short_url_card`, `short_url_ach` on `invoices` (for idempotency lookup at send time)
- New Cloudflare Worker `sparkry-pay` deployed at `pay.sparkry.ai`, serving:
  - `GET /:slug` → 302 to Stripe Payment Link OR static 410 (revoked/expired) OR static 404 (unknown slug)
  - `GET /healthz` → liveness check (200 OK, no D1 query)
  - `GET /robots.txt` → `User-agent: *\nDisallow: /` (block search engine indexing of slugs)
  - `GET /` (root) → 302 to `https://sparkry.ai` (marketing site; avoids a bare-domain 404)
- Server-side mint helper in `sparkry-crm` invoked during the invoice send flow
- Server-side revoke hook invoked during the invoice void flow
- Email template change in `src/lib/server/email.ts` to prefer short URLs
- Sentry instrumentation on the Worker
- Cloudflare WAF rate-limit rule on `pay.sparkry.ai` to deter slug enumeration
- DNS + custom-domain setup for `pay.sparkry.ai`
- Operations runbook (rollback, manual revoke, click-analytics query)

### Out of scope

- A short-link UI for non-invoice marketing links (Short.io covers that today)
- Per-customer link-click analytics dashboards (raw query against `payment_link` is enough for now)
- Geographic / device targeting
- Custom 404/410 pages with logos (text-only static pages are fine for v1; branding can come in v1.1)
- Link previews / OpenGraph tags (recipients click from email; no social sharing expected)
- Migrating sent-but-unpaid invoices to short URLs (those emails are already delivered; minting new links would not change what's in the customer's inbox)

---

## Architectural decisions

### A1. Deployment topology — separate Worker, shared D1

`sparkry-pay` is a NEW Cloudflare Worker (own `wrangler.pay.toml`, own `main = src/pay-worker.ts`). It binds the SAME D1 database as the CRM Pages app (`sparkry-crm-prod`). It is NOT a Pages Function and NOT mounted under `internal.sparkry.ai/*`.

Reasons:

- **Auth isolation.** The CRM's `hooks.server.ts` runs Google-OAuth + session-cookie verification on every path on `internal.sparkry.ai`. Mounting the redirect on the same host requires either (a) adding a `PUBLIC_PATHS` exception for `/pay/*` (auth bypass via path config — easy to break in a future refactor) or (b) deploying a second route group with auth-skipping middleware (still on the same host, still one config change away from leaking). A separate Worker on a separate host means the auth bypass is structural, not configurational.
- **Blast radius.** A bug in the redirect Worker cannot crash the CRM (and vice versa). They deploy independently.
- **Audit clarity.** Reading `wrangler.pay.toml` makes the entire public surface of the Worker obvious. There is no "and then this exception in hooks.server.ts" footnote.
- **D1 sharing is safe.** D1 bindings are scoped to whatever Worker/Pages project lists them; a single database can be bound to multiple deployments. The pay Worker only reads `payment_link` and updates click columns — it cannot reach `customers`, `invoices` row contents beyond the join column it explicitly selects, etc., except by writing SQL that does so. Since we control the SQL surface, this is fine.

**Skeptic objection:** "Two Workers is more to maintain." Counter: the pay Worker is ~150 LOC. The marginal maintenance is near-zero, and the structural auth isolation is worth more than one shared deploy.

### A2. Slug strategy — 8-char base62 from `crypto.getRandomValues`

Slug format: `^[A-Za-z0-9]{8}$`. Generated via `crypto.getRandomValues(new Uint8Array(8))` then mapped onto the base62 alphabet `0-9A-Za-z`. Stored as the primary key of `payment_link`.

Keyspace: 62^8 = 2.18 × 10^14. Combined with the WAF rate limit (A6) and the fact that there are at most a few hundred active links at any time, the probability of an attacker guessing a valid live slug is on the order of (active_links / 2.18×10^14) per request — effectively zero.

**Collision handling.** On INSERT, if the slug already exists (UNIQUE PK constraint trips), regenerate and retry. Max 5 attempts; raise on exhaustion. With 62^8 keyspace and active-link counts in the hundreds-to-thousands range, expected attempts is ≈1.

**Why not a URL-safe shortcode of the invoice ID.** The invoice ID is a UUID — sequential mapping of UUID → slug would either be enumerable or require crypto, both worse than just generating a random slug.

**Why not 6 chars.** 62^6 = 5.7 × 10^10. Still huge in absolute terms but a smaller margin against enumeration, and the 2-char savings in URL length is not user-visible.

**Why not 10 chars.** Marginal entropy gain, marginal UX cost. 8 is the industry standard (Bitly, YouTube, GitHub).

### A3. URL allowlist — only Stripe checkout hosts

The Worker MUST verify the redirect target matches:

```
^https://(buy|checkout)\.stripe\.com/[A-Za-z0-9_./?=&-]+$
```

**Note:** `%` is intentionally excluded from the allowed character class. Stripe Payment Link URLs do not use percent-encoding in practice, and including `%` would allow percent-encoded CRLF (`%0d%0a`) to pass the check — a header-injection bypass. The path requires at least one character (bare host with trailing slash is rejected).

This is enforced at TWO points:

1. **At mint time** in the CRM (`mintShortLink()` helper): rejects with a thrown error before the row is written.
2. **At redirect time** in the Worker (`isAllowedTarget()`): rejects with a 500 + Sentry alert if a row's `target_url` somehow drifts off-allowlist (defense in depth against a DB tamper or migration mistake).

If Stripe ever changes their checkout host, the allowlist regex is updated in one constant (`STRIPE_CHECKOUT_HOST_RE`) in a shared utility, with a unit test fixture.

**Why both layers.** Single-layer enforcement at mint is sufficient under the threat model where the DB is trusted. Re-checking at redirect time means a future bug (or admin migration script) that writes a non-Stripe URL into `payment_link.target_url` does not silently turn the Worker into a generic open-redirect.

### A4. Storage — new `payment_link` table + columns on `invoices`

D1 schema additions (one migration, applied via `wrangler d1 migrations apply sparkry-crm-prod --remote`):

```sql
CREATE TABLE payment_link (
  slug          TEXT PRIMARY KEY,
  target_url    TEXT NOT NULL,
  invoice_id    TEXT NOT NULL REFERENCES invoices(id) ON DELETE RESTRICT,
  rail          TEXT NOT NULL CHECK (rail IN ('card', 'ach')),
  created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  expires_at    TEXT,                 -- NULL = no expiry; otherwise ISO 8601 with Z suffix
  revoked_at    TEXT,                 -- NULL = active; set on invoice void
  last_clicked_at TEXT,               -- updated by Worker via ctx.waitUntil()
  click_count   INTEGER NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX ux_payment_link_invoice_rail
  ON payment_link (invoice_id, rail)
  WHERE revoked_at IS NULL;

CREATE INDEX ix_payment_link_invoice
  ON payment_link (invoice_id);

ALTER TABLE invoices ADD COLUMN short_url_card TEXT;
ALTER TABLE invoices ADD COLUMN short_url_ach  TEXT;
```

**Why the partial unique index.** It enforces "at most one active short link per (invoice, rail)" while allowing a revoked link to coexist with a newly-issued replacement (rare but possible if we re-issue Stripe links after expiry without re-using the slug).

**Why store `short_url_*` on `invoices` too.** Idempotency at send time: the send action looks up `invoices.short_url_card`; if non-null, reuses; if null, mints a new slug and writes both rows. Without the denormalized column, the send code would need to query `payment_link` by `(invoice_id, rail, NOT revoked)` which is fine but slower and less self-documenting at the read site (`email.ts`).

**`ON DELETE RESTRICT`.** Invoices are never hard-deleted in this system (status: void instead). The FK is belt-and-braces — if a future migration accidentally tries to DELETE FROM invoices, the FK trips rather than orphaning short links.

### A5. Click tracking — best-effort, non-blocking

On a successful redirect, the Worker calls `ctx.waitUntil()` with an UPDATE that bumps `click_count` and sets `last_clicked_at`. The 302 response is sent BEFORE the D1 write resolves. Tradeoffs:

- **Pro:** redirect latency is just D1 lookup (~5-15 ms) + response serialization. No second D1 round-trip on the user-facing path.
- **Pro:** D1 write failures don't break the user experience.
- **Con:** click counts can undercount if the Worker process is evicted before `waitUntil` resolves. Acceptable — these are coarse analytics, not financial records.
- **Con:** A burst of clicks from one user can race-condition the `click_count = click_count + 1`. Acceptable — we don't need exact counts.

The `last_clicked_at` and `click_count` columns are advisory. Authoritative payment status lives in Stripe + the `invoices.status` field.

**Bot-click filtering.** Email clients (Gmail, Outlook) frequently pre-fetch links for security scanning, inflating click counts. v1 logs all clicks — operator can interpret. v1.1 can filter by user-agent regex if needed.

### A6. Rate limiting + WAF

A Cloudflare WAF custom rule on the `pay.sparkry.ai` hostname:

- **Rate limit:** 60 requests per 10 seconds per IP. Generous for a real user (who'd visit once and maybe re-click) but blocks enumeration in seconds.
- **Action on excess:** managed challenge.
- **Bot Fight Mode:** enabled.
- **No path-based bypass.** All paths under `pay.sparkry.ai` are rate-limited including `/healthz` (healthcheck pinger should set a custom header that the rule allows, OR the rate is generous enough that internal monitoring fits within it).

Configured via the Cloudflare dashboard. Evidence (screenshot + rule export) saved at `docs/operational/2026-05-26-pay-sparkry-ai/`.

### A7. DNS + TLS

- DNS: Cloudflare creates the DNS record automatically when you add `pay.sparkry.ai` as a Custom Domain in Workers → sparkry-pay → Settings → Triggers → Custom Domains. No manual DNS entry is needed — CF creates a CNAME to a CF-internal target (NOT to `workers.dev`). Do not manually create a CNAME to a `workers.dev` hostname.
- TLS: Cloudflare Universal SSL (free) — automatic cert provisioning.
- HSTS: enabled at the zone level (existing setting for `sparkry.ai` already covers subdomains).

### A8. Observability — Sentry + console

The Worker uses `withSentry` from `@sentry/cloudflare` to wrap the fetch handler, matching the pattern in `src/worker.ts:122-126`. The Worker export is `export default withSentry((env: Env) => ({ dsn: env.SENTRY_DSN ?? '', tracesSampleRate: 1.0, initialScope: { tags: { service: 'sparkry-pay' } } }), { fetch: handle })`. SENTRY_DSN is set as a Worker secret (same DSN value as the CRM is fine). All thrown errors and 500 responses go to Sentry, tagged with `service: sparkry-pay` for filtering. 404s and 410s are NOT sent to Sentry (those are normal operational outcomes).

Beyond Sentry, the Worker emits a single structured `console.log` line per request shaped as:

```json
{"event":"redirect","slug":"Xxxxxxxx","status":302,"rail":"card","ua_hash":"<short>","ip_bucket":"<cidr/24>"}
```

Note: `invoice_id` is NOT included in the log. The hard constraint says 'not the invoice ID in plaintext beyond the internal join'. The `slug` alone is sufficient for analytics — the invoice can be retrieved via D1 join if needed for debugging.

These land in Cloudflare's tail logs and can be queried via `wrangler tail`. No raw IPs or full UAs stored — UA hashed to 8 hex chars, IP truncated to /24 (IPv4) or /48 (IPv6) for coarse aggregation.

---

## Requirements

REQ-IDs follow the project convention (`REQ-PAY-NNN`). Priority: P0 = must ship in v1; P1 = must ship in v1.1; P2 = backlog.

### Data model

- **REQ-PAY-001 (P0):** D1 migration creates the `payment_link` table with the schema in A4, idempotent (`CREATE TABLE IF NOT EXISTS`).
- **REQ-PAY-002 (P0):** Migration adds `short_url_card` and `short_url_ach` TEXT columns to `invoices`, nullable, no default. SvelteKit Drizzle schema (`src/lib/server/db/schema.ts`) updated to match.
- **REQ-PAY-003 (P0):** Partial unique index `ux_payment_link_invoice_rail` enforces single-active-link-per-rail-per-invoice.
- **REQ-PAY-004 (P0):** FK `payment_link.invoice_id → invoices(id)` with `ON DELETE RESTRICT`.

### Slug + URL utilities

- **REQ-PAY-010 (P0):** Slug generator emits 8-character base62 strings using `crypto.getRandomValues`. Unit-tested to (a) match `/^[A-Za-z0-9]{8}$/`, (b) produce 1,000 distinct values from 1,000 calls, (c) reject the all-zeros byte vector edge case (re-roll — if all 8 bytes are zero, call `getRandomValues` again). Implementation: after generating bytes, `if (bytes.every(b => b === 0)) crypto.getRandomValues(bytes)` re-rolls once. Test: mock `crypto.getRandomValues` to return all-zeros on first call, valid random on second; assert output is not `'00000000'`.
- **REQ-PAY-011 (P0):** `isAllowedTarget(url: string): boolean` returns true iff `url` matches `^https://(buy|checkout)\.stripe\.com/[A-Za-z0-9_./?=&-]+$` (note: `%` is excluded — prevents percent-encoded CRLF header injection; REQUIRES at least one path char — bare host rejected). Unit-tested with positive Stripe URLs, negative cases (http, other host, javascript:, data:, empty path, `%0d%0a` percent-encoded CRLF).
- **REQ-PAY-012 (P0):** `STRIPE_CHECKOUT_HOST_RE` is exported from a single utility module (`src/lib/server/pay/url.ts`) and consumed by both `mintShortLink` and the Worker — no duplication.

### Mint helper (CRM-side)

- **REQ-PAY-020 (P0):** `mintShortLink(db, invoiceId, rail, targetUrl)` returns `{ slug, shortUrl }`. Side effects: INSERT row into `payment_link`, UPDATE `invoices.short_url_<rail>`.
- **REQ-PAY-021 (P0):** Idempotency — `mintShortLink` queries `payment_link WHERE invoice_id = ? AND rail = ? AND revoked_at IS NULL` directly (the `payment_link` table is the single source of truth for idempotency; the denormalized `invoices.short_url_<rail>` column is a read-optimization cache, not the authority). If an active row exists, return the existing slug. If `targetUrl` differs from the stored `target_url`, UPDATE the row (slug unchanged) AND insert an activityLog entry `payment_link_target_updated` with `old_value=existing.target_url, new_value=targetUrl` to maintain audit trail.
- **REQ-PAY-022 (P0):** Allowlist enforcement — throws `InvalidPaymentTargetError` if `targetUrl` fails `isAllowedTarget()`. No DB write occurs.
- **REQ-PAY-023 (P0):** Slug collision retry — on UNIQUE PK violation, regenerate and retry up to 5 times. Raise `SlugMintExhaustedError` on exhaustion (Sentry-captured).
- **REQ-PAY-024 (P0):** Returned `shortUrl` is `https://pay.sparkry.ai/<slug>` — host is read from a config constant `PAY_DOMAIN_URL`, NOT hardcoded at every call site.

### Worker — redirect endpoint

- **REQ-PAY-030 (P0):** `GET /:slug` where slug matches `/^[A-Za-z0-9]{8}$/` → look up in `payment_link`.
- **REQ-PAY-031 (P0):** If row exists, `revoked_at` is null, `expires_at` is null or in the future, and `isAllowedTarget(row.target_url)` is true → respond 302 with `Location: <target_url>` and security headers (A8). Bump click counters via `ctx.waitUntil`.
- **REQ-PAY-032 (P0):** If row exists but is revoked or expired → respond 410 with a static HTML page ("This payment request has been canceled or has expired. Contact billing@sparkry.ai if you need help.") and `Cache-Control: no-store`.
- **REQ-PAY-033 (P0):** If row does not exist OR the slug fails the regex → respond 404 with a static HTML page ("Page not found.") and `Cache-Control: no-store`. The 404 page MUST NOT distinguish between "slug malformed", "slug well-formed but unknown", and "slug well-formed but revoked" — they all render identically except revoked/expired returns 410 instead.

Wait — REQ-PAY-032 says 410 for revoked. REQ-PAY-033 says 404 for unknown. This DOES leak existence (a 410 confirms the slug was real). Acceptable tradeoff: the friendly "this was canceled" UX is worth more than the negligible enumeration signal (per A6's WAF, attacker can't scale enumeration anyway).

- **REQ-PAY-034 (P0):** If `target_url` fails `isAllowedTarget()` at redirect time → respond 500, Sentry-captured, NO redirect emitted.
- **REQ-PAY-035 (P0):** Response headers on all paths include:
  - `Strict-Transport-Security: max-age=31536000; includeSubDomains; preload`
  - `Referrer-Policy: no-referrer`
  - `X-Content-Type-Options: nosniff`
  - `X-Frame-Options: DENY`
  - `Cache-Control: no-store` on 302/410/404 (cached redirects would survive revocation)
  - `Content-Security-Policy: default-src 'none'` on error pages (no script needed)
- **REQ-PAY-036 (P0):** `GET /healthz` → 200 with body `ok`, NO D1 query, NO Sentry init for this path.
- **REQ-PAY-037 (P0):** `GET /robots.txt` → 200 with body `User-agent: *\nDisallow: /\n`.
- **REQ-PAY-038 (P0):** `GET /` (root) → 302 to `https://sparkry.ai`. Avoids bare-host 404.
- **REQ-PAY-039 (P0):** All other paths/methods → 404 (or 405 for valid path with wrong method). No echo of request data into response body.

### Email integration

- **REQ-PAY-050 (P0):** `sendInvoiceEmail` in `src/lib/server/email.ts` reads `inv.short_url_card` / `inv.short_url_ach` and uses them in the email body where the long Stripe URLs were used previously. Falls back to `inv.payment_link_card_url` / `payment_link_ach_url` if the short fields are null (covers in-flight invoices created before the rollout).
- **REQ-PAY-051 (P0):** The invoice send flow (`src/routes/(crm)/invoices/[id]/+page.server.ts`) calls `mintShortLink` AFTER Stripe link creation succeeds and BEFORE the email send. Mint failures abort the send (revert `sending → draft`).
- **REQ-PAY-052 (P1):** The plain-text fallback body of the email also uses the short URLs.

### Revocation

- **REQ-PAY-060 (P0):** Both voiding an invoice AND undoing a send (status transition → `void` or `undoSend` → `draft`) MUST set `revoked_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')` on all `payment_link` rows for that invoice. The revoke SQL statement MUST be included in the same `db.batch([...])` call as the status update so revocation is atomic with the status change. An activityLog entry `payment_links_revoked` with `metadata: { count: N }` MUST be inserted after the revoke to maintain audit trail.
- **REQ-PAY-061 (P1):** Manual revoke via wrangler one-liner is documented in the operational runbook (covers the case where Stripe-side fraud is detected before invoice void).
- **REQ-PAY-062 (P2):** A future UI affordance ("revoke link without voiding invoice") is not in v1.

### Observability + ops

- **REQ-PAY-070 (P0):** Sentry initialized on the Worker. SENTRY_DSN is a Worker secret (separate set; same DSN value as the CRM is fine).
- **REQ-PAY-071 (P0):** Structured per-request log (A8) emitted via `console.log` on every redirect.
- **REQ-PAY-072 (P0):** WAF rate-limit rule (A6) configured before the Worker is wired into invoice sends.
- **REQ-PAY-073 (P0):** Operations runbook at `docs/operational/2026-05-26-pay-sparkry-ai/` covers:
  - DNS + custom-domain setup commands (`wrangler` invocation)
  - WAF rule export (JSON)
  - Manual revoke query
  - Click-analytics query (top-N links by clicks, dead-link audit)
  - Rollback procedure (point pay.sparkry.ai DNS at `sparkry.ai` apex; emails already sent keep working because the long Stripe URLs are still embedded as fallback for any future re-render — see REQ-PAY-080)

### Security regression tests

- **REQ-PAY-080 (P0):** Test that `mintShortLink` rejects `http://buy.stripe.com/...`, `https://evil.com/`, `https://buy.stripe.com.evil.com/...`, `javascript:alert(1)`, empty string, and ASCII-confusable hosts (e.g., `https://buy.stripe.com@evil.com/x`).
- **REQ-PAY-081 (P0):** Test that the Worker `isAllowedTarget` check fires when a `payment_link` row contains a non-Stripe target (manually inserted in the test) — response is 500, NOT 302.
- **REQ-PAY-082 (P0):** Test that revoked links return 410 and DO NOT bump `click_count`.
- **REQ-PAY-083 (P0):** Test that the Worker returns 404 for slug `aaaaaaaa` if it doesn't exist (no information leak via response shape diff between malformed and well-formed-but-unknown).
- **REQ-PAY-084 (P1):** Test that re-mint preserves the slug when `target_url` changes (Stripe link rotation case).
- **REQ-PAY-085 (P1):** Test that a concurrent send + void race resolves cleanly. In `mint.test.ts` or `invoice-pay-integration.test.ts`: (1) use `Promise.all([mintShortLink(...), revokeInvoiceShortLinks(...)])` for simultaneous execution; (2) assert the final state has no orphaned active short URLs (either both revoked, or one minted post-revoke); (3) test the reverse direction: mint then revoke should leave exactly one revoked row. Also test that `undoSend` (which calls `revokeInvoiceShortLinks`) results in all active short links being revoked.

### Test coverage gates

- **REQ-PAY-090 (P0):** Vitest unit tests for all P0 utilities (slug gen, URL allowlist, mint, revoke).
- **REQ-PAY-091 (P0):** Vitest + Miniflare D1 integration tests for the Worker (redirect, 410, 404, 500-on-bad-target, click counter increment).
- **REQ-PAY-092 (P0):** Each test references its REQ-ID in a comment or test name (project convention; see existing `tests/unit/frontend-desk.test.ts` — e.g., `describe("wealth gate (REQ-WC-002)", ...)`).
- **REQ-PAY-093 (P0):** `pnpm check` (svelte-check typecheck) and `pnpm lint` pass green before merge.

### Feature flag lifecycle

- **REQ-PAY-100 (P2):** Remove the `PAY_SHORT_LINKS_ENABLED` feature flag after 7 days of clean operation. **"Clean operation" criteria:** (a) zero P0/P1 Sentry events tagged `service: sparkry-pay` over the 7-day window; (b) at least 5 invoice emails sent successfully with short URLs (click counter > 0 for each). Criteria (a) and (b) are the ONLY exit gates. A manual best-effort check is also performed: Travis reviews the `billing@sparkry.ai` inbox for payment complaints and checks the Stripe Dashboard disputes tab — if nothing is flagged there, that check is satisfied. (Note: a `SELECT ... WHERE action LIKE '%dispute%' OR action LIKE '%support%'` query against `activity_log` is NOT a valid check because no CRM action values contain these strings; the inbox and Stripe Dashboard are the only available dispute signals.) When criteria (a) and (b) are met and the manual check is clear, remove the flag check from `+page.server.ts` and remove the env var from CF Pages settings.

---

## Data flow

### Send-an-invoice flow (new path)

```
User clicks "Review & Send"
   │
   ▼
Atomic claim: draft → sending  ──┐
   │                              │ (existing logic)
   ▼                              │
createPaymentLink(card)           │
createPaymentLink(ach)           ─┘
   │
   ▼  *** NEW ***
mintShortLink(db, invoiceId, 'card', cardUrl)   →  short_url_card
mintShortLink(db, invoiceId, 'ach',  achUrl)    →  short_url_ach
   │
   ▼
sendInvoiceEmail(...)  uses short_url_card / short_url_ach
   │
   ▼
sending → sent
```

If `mintShortLink` throws (e.g., bad URL, slug-mint exhaustion), the existing rollback (`sending → draft`) fires and the user sees an error toast.

### Customer-click flow

```
Customer clicks https://pay.sparkry.ai/Xxxxxxxx
   │
   ▼
DNS → CF Anycast → sparkry-pay Worker
   │
   ▼
slug matches /^[A-Za-z0-9]{8}$/ ?
   │ no  → 404 (static)
   ▼ yes
SELECT target_url, revoked_at, expires_at, invoice_id, rail
  FROM payment_link WHERE slug = ?
   │
   ▼
row found?
   │ no  → 404 (static)
   ▼ yes
revoked? expired?
   │ yes → 410 (static)
   ▼ no
isAllowedTarget(target_url)?
   │ no  → 500, Sentry alert
   ▼ yes
302 Location: target_url, security headers, Cache-Control: no-store
ctx.waitUntil(UPDATE payment_link SET click_count = ..., last_clicked_at = ...)
```

### Void-an-invoice flow (revocation hook)

```
User clicks "Void invoice"
   │
   ▼
Atomic claim: invoice → void
   │
   ▼  *** NEW ***
UPDATE payment_link SET revoked_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
  WHERE invoice_id = ? AND revoked_at IS NULL
   │
   ▼
Activity log entry: "Payment links revoked (N rows)"
```

---

## File layout (sparkry-crm)

```
src/
  lib/
    server/
      pay/
        url.ts             — STRIPE_CHECKOUT_HOST_RE, isAllowedTarget, PAY_DOMAIN_URL
        slug.ts            — generateSlug (base62 from crypto.getRandomValues)
        mint.ts            — mintShortLink, revokeInvoiceShortLinks, InvalidPaymentTargetError, SlugMintExhaustedError
      db/
        schema.ts          — MODIFIED: add short_url_card, short_url_ach; add payment_link table
      email.ts             — MODIFIED: prefer short URLs in HTML + plaintext bodies
  routes/
    (crm)/
      invoices/
        [id]/
          +page.server.ts  — MODIFIED: call mintShortLink in send action; revoke in void action
migrations/
  0011_payment_link.sql    — NEW: payment_link table + invoices columns
wrangler.pay.toml          — NEW: Worker config (D1 binding, custom domain, SENTRY_DSN secret)
src/pay-worker.ts          — NEW: Worker entry point
tests/
  unit/
    pay/
      url.test.ts          — REQ-PAY-011, REQ-PAY-080
      slug.test.ts         — REQ-PAY-010
      mint.test.ts         — REQ-PAY-020..024, REQ-PAY-084, REQ-PAY-085 (concurrent test)
      email.test.ts        — REQ-PAY-050, REQ-PAY-052
  integration/
    pay-worker.test.ts     — REQ-PAY-030..039, REQ-PAY-081, REQ-PAY-082, REQ-PAY-083 (Miniflare D1)
docs/operational/
  2026-05-26-pay-sparkry-ai/
    README.md              — runbook (REQ-PAY-073)
    waf-rule.json          — exported WAF rule
    dns-evidence.png       — screenshot of CF DNS panel post-setup
```

---

## Security model

### Threat: open redirect

**Mitigation:** double allowlist (mint-time + redirect-time) on `^https://(buy|checkout)\.stripe\.com/...$`.
**Residual risk:** Stripe themselves are compromised and host malicious content at `buy.stripe.com`. Out of our threat model.

### Threat: slug enumeration

**Mitigation:** 62^8 keyspace + WAF rate limit at 60/10s/IP + Bot Fight Mode.
**Residual risk:** distributed slow scan from many IPs. Probability of hitting any one of N active slugs in K requests is K·N / 62^8 — for N = 1000 active and K = 1M requests, that's 4.6 × 10^-6. Acceptable.

### Threat: tampered DB row (admin error, future migration)

**Mitigation:** redirect-time `isAllowedTarget` check (REQ-PAY-034) refuses to redirect even if the row's `target_url` was overwritten with a hostile value.
**Residual risk:** if attacker has DB write, they can also disable the check. But that's a different game.

### Threat: stolen short URL (forwarded email, screenshot, leaked from logs)

**Mitigation:** Stripe Payment Link itself has `restrictions: { completed_sessions: { limit: 1 } }` (existing config in `stripe.ts`) — after one successful payment, the link no longer works. Revocation can also be triggered on detection.
**Residual risk:** before the legitimate customer pays, a leaked link could be paid by an attacker who then claims they paid (this would be a strange attack — they're paying our invoice — but theoretically possible). The fix is on the Stripe side, not in this Worker.

### Threat: cache poisoning

**Mitigation:** `Cache-Control: no-store` on every response. Cloudflare's edge cache would otherwise cache the 302 for a configurable TTL, and a poisoned cache entry could outlive revocation.
**Residual risk:** none assuming the header is honored. Verified by integration test.

### Threat: response splitting / header injection via slug or target

**Mitigation:** Slug is regex-validated `^[A-Za-z0-9]{8}$` — no CR/LF possible. Target URL is allowlist-validated `^https://...` — no CR/LF possible (CR/LF wouldn't match the allowed-char class).
**Residual risk:** none in our control path.

### Threat: SSRF via the Worker fetching the target

**Mitigation:** the Worker NEVER fetches the target. It only emits `Location:` in a 302. The customer's browser is responsible for the GET to Stripe.

### Threat: CSRF on the redirect endpoint

**Not applicable.** GET, no side effects beyond the click counter, no authentication state, no cookies.

### Threat: cookie / session theft on `pay.sparkry.ai`

**Mitigation:** Worker NEVER sets a cookie. Worker NEVER reads a cookie. Domain-level separation from `internal.sparkry.ai` means session cookies for the CRM are never sent to `pay.sparkry.ai` (cookie domain is `internal.sparkry.ai`, not `.sparkry.ai`). VERIFY this is the case — see audit step in runbook.

**Important:** The CRM's `cookies.set()` in `hooks.server.ts` MUST NOT specify `domain: '.sparkry.ai'` — that would scope the cookie to all subdomains and leak the CRM session token to `pay.sparkry.ai`. The default (no domain attribute) correctly scopes to the exact host `internal.sparkry.ai`. See the verification step in the runbook: `curl -sv https://pay.sparkry.ai/healthz 2>&1 | grep -i 'set-cookie'` — must return empty. Also verify in browser DevTools: after logging into `internal.sparkry.ai`, check that no session cookie appears in requests to `pay.sparkry.ai/healthz`.

---

## Deployment plan

### M0 — D1 migration (sparkry-crm-staging first)

1. Apply migration `0011_payment_link.sql` to staging D1.
2. Run staging smoke test: insert a sample row, query by slug.
3. If clean, apply to production D1.

### M1 — Worker (staging deploy)

1. Create `wrangler.pay.toml`.
2. Write `src/pay-worker.ts`, tests, utilities.
3. `wrangler deploy --config wrangler.pay.toml --env staging`.
4. Hit staging Worker URL with a hand-crafted slug. Verify 302 / 410 / 404 paths.

### M2 — DNS + custom domain + WAF

1. Add `pay.sparkry.ai` custom domain to the Worker via wrangler (writes the DNS record automatically).
2. Wait for DNS + cert propagation (typically <5 min).
3. Add WAF rate-limit rule via dashboard. Export rule JSON to `docs/operational/...`.
4. Verify: `curl -I https://pay.sparkry.ai/healthz` → 200; `curl -I https://pay.sparkry.ai/aaaaaaaa` → 404.

### M3 — CRM integration (deploy behind a feature flag)

1. Land the mint + email changes in `sparkry-crm` (CRM is on production already; deploy proceeds via Pages CI).
2. Feature flag: env var `PAY_SHORT_LINKS_ENABLED=true` (set per environment). When false, `sendInvoiceEmail` uses the long Stripe URLs and `mintShortLink` is not called.
3. Deploy to staging Pages with flag ON. Send a test invoice to a sink address. Click the short URL. Confirm it lands on Stripe Checkout.
4. Deploy to production with flag OFF first (verify nothing changed).
5. Flip flag to ON in production secrets. Send a real test invoice (Travis to himself). Verify.
6. Remove flag after 7 days of clean operation (REQ-PAY-100, soft requirement).

### M4 — Operational evidence

1. Save WAF rule JSON + dashboard screenshot.
2. Save `wrangler deploy` output + custom-domain confirmation.
3. Save a successful end-to-end test trace (sent invoice → click → Stripe → paid → webhook → status update).
4. Update `sparkry-crm/CLAUDE.md` with a brief "Payment short links" subsection in the Key Patterns section.

---

## Rollback

**Scenario A — short URLs broken in production after enable.**

1. Set `PAY_SHORT_LINKS_ENABLED=false` in CF Pages secrets, redeploy.
2. All new sends use long Stripe URLs again.
3. Existing already-sent emails in customer inboxes contain whichever URL was active at send time — both keep working as long as the Worker is up. (Mark-paid still routes via the Stripe webhook.)

**Scenario B — `pay.sparkry.ai` Worker is unavailable.**

1. Already-sent emails that contain `pay.sparkry.ai/...` will 5xx until the Worker recovers.
2. Stripe links themselves are still valid — Travis can manually email customers the long URLs from the invoice detail page.
3. To accelerate recovery without a Worker fix: change the `pay.sparkry.ai` DNS to a Cloudflare Worker stub that 302s to a static "please contact billing@sparkry.ai" page.

**Scenario C — short URL leak / customer paid wrong thing.**

1. Worker is correct; the issue is upstream (CRM picked wrong invoice).
2. Manual revoke: `wrangler d1 execute sparkry-crm-prod --remote --command "UPDATE payment_link SET revoked_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE slug = 'Xxxxxxxx'"`
3. Reissue corrected invoice through normal flow (gets a new slug).

---

## Out of scope for v1 — explicit non-goals

- Customer-facing analytics (e.g., "this invoice was clicked 3 times")
- A/B testing different landing URLs
- Geolocation
- Authenticated short links (e.g., requiring a one-time password to follow)
- Bulk minting / API for third parties
- A retention policy for `payment_link` rows (we keep them all until the table is observably large enough to need pruning)
- Stripe Customer Portal / Subscription links (different URL pattern; allowlist would need updating — defer)
- Internationalized error pages (en-US only for v1)

---

## Research notes and open items

### DMARC deliverability claim
The primary business justification (§"Why this spec exists") claims that co-hosting links on `pay.sparkry.ai` removes an inbox-classifier penalty. This is non-obvious: major classifiers (Gmail, Outlook, ProofPoint) may follow 302 redirects to score the final destination rather than the visible URL. **Action:** Before declaring a deliverability win, send identical test invoices (one with long Stripe URL, one with short URL) through Google Postmaster Tools or MXToolbox. If classifiers follow the redirect, the brand-trust benefit remains but the DMARC-alignment benefit is reduced.

### GDPR/CCPA click analytics
The Worker logs IP-bucket (/24 for IPv4, /48 for IPv6) and UA-hash per click to D1. IP-bucket data is classified as personal data under GDPR Article 4 in many EU member-state interpretations. Cardinal Health invoices go to corporate accounts globally. **Action:** Assess whether this constitutes new personal data processing requiring a DPA review. Consider a retention policy (NULL out `last_clicked_at` and clear IP analytics after 90 days). Flag for legal review before enabling for Cardinal Health invoices. A GDPR-compliant data retention procedure is documented in the operations runbook at `docs/operational/2026-05-26-pay-sparkry-ai/README.md` (see "Data retention (GDPR/CCPA)" section).

### Cost at scale
Click tracking writes 1 D1 row update per successful redirect. At current volumes (<500 invoice sends/month × typical pre-fetch inflation), D1 write counts remain well within the Workers Paid 5M/month included allotment. The `$0 incremental` claim holds at current scale. At Workers Paid pricing ($0.001/100k writes beyond 5M/month included), even 10k sends/month with 5× pre-fetch = 50k writes — still $0. The 10k threshold is a point to re-evaluate architectural fit (Analytics Engine), not a cost breakeven. Actual cost first materializes above ~500k sends/month.
