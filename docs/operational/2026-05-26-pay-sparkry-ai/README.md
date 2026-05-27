# pay.sparkry.ai — Operations Runbook

## Manual revoke

If Stripe-side fraud is detected on an invoice that hasn't been voided yet:

```bash
cd /Users/travis/SGDrive/dev/sparkry-crm
npx wrangler d1 execute sparkry-crm-prod --remote \
  --command "UPDATE payment_link SET revoked_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE slug = 'XXXXXXXX';"
```

## Rotate expired Stripe link

If a Stripe Payment Link has expired and the customer cannot pay:
1. Detect: query for links where `expires_at` is in the past or Worker returns 410 for a slug
2. Create a new Stripe Payment Link in the CRM (re-open the invoice and use the Stripe dashboard)
3. Update the redirect target:
```bash
npx wrangler d1 execute sparkry-crm-prod --remote \
  --command "UPDATE payment_link SET target_url = '<new_stripe_url>', expires_at = NULL WHERE slug = 'XXXXXXXX';"
```
4. The slug is unchanged — the existing email link now redirects to the new Stripe link.
5. Optionally re-send the invoice email so the customer has fresh context.

## Slug enumeration incident

Rate limiting is enforced **in-Worker** via the Workers Rate Limiting binding (REQ-PAY-072) —
60 requests per 10 seconds per IP. This runs before D1 reads so floods don't burn D1 budget.
The binding is configured in `wrangler.pay.toml` under `[[ratelimits]]` (free tier, no CF Pro required).

To view rate-limit metrics: Cloudflare dashboard → Workers & Pages → sparkry-pay → Metrics tab.
For structured log analysis: Workers → sparkry-pay → Logs (look for `status: 429` entries).

If a sustained enumeration attack is observed:
1. Tighten the in-Worker rate limit by editing `wrangler.pay.toml` → `[ratelimits.simple]` → lower `limit` or use `period = 10` (already set). Redeploy: `pnpm build:pay-worker && npx wrangler deploy --config wrangler.pay.toml`.
2. Query click logs to check if any valid slug was hit from that IP bucket:
```bash
npx wrangler d1 execute sparkry-crm-prod --remote \
  --command "SELECT slug, invoice_id, click_count, last_clicked_at FROM payment_link WHERE last_clicked_at > strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-24 hours') ORDER BY last_clicked_at DESC LIMIT 50;"
```
3. If a valid slug was hit, consider manually revoking the affected slug (see Manual revoke section) and notifying the customer.

## Cookie domain audit

Verify no CRM session cookie leaks to pay.sparkry.ai:
```bash
curl -sv https://pay.sparkry.ai/healthz 2>&1 | grep -i 'set-cookie'
```
Expected: empty output (no Set-Cookie header). If any cookie appears, audit `hooks.server.ts` immediately — the CRM cookie MUST NOT use `domain: '.sparkry.ai'`.

## D1 outage / redirect failures spiking in Sentry

If D1 read failures cause the Worker to return 500s at scale:
1. Check https://www.cloudflarestatus.com for D1 incidents
2. If D1 is down for > 5 minutes: deploy a stub Worker that serves a static 503 page:
   - Stub response: HTTP 503 with body "Payment links temporarily unavailable. Please contact billing@sparkry.ai."
   - This is better than confusing 500 errors for customers
3. When D1 recovers, undeploy the stub and re-deploy the real Worker

## Top-clicked links (analytics)

```bash
npx wrangler d1 execute sparkry-crm-prod --remote \
  --command "SELECT slug, invoice_id, click_count, last_clicked_at FROM payment_link ORDER BY click_count DESC LIMIT 20;"
```

## Dead-link audit

```bash
npx wrangler d1 execute sparkry-crm-prod --remote \
  --command "SELECT COUNT(*) FROM payment_link WHERE click_count = 0 AND created_at < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-30 days');"
```

## Data retention (GDPR/CCPA)

The Worker logs IP-bucket (/24 for IPv4, /48 for IPv6) per click. IP-bucket data may be classified as personal data under GDPR Article 4 in EU member-state interpretations.

**Before enabling for Cardinal Health invoices:** Flag for legal review — Cardinal Health invoices go to corporate contacts globally.

**90-day retention procedure** (run periodically, e.g. monthly via cron or manually):
```bash
npx wrangler d1 execute sparkry-crm-prod --remote \
  --command "UPDATE payment_link SET last_clicked_at = NULL WHERE last_clicked_at < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-90 days');"
```
This nulls out the click timestamp for old records, removing the time-correlated IP-to-invoice linkage while preserving aggregate `click_count` for analytics.

Note: `click_count` is an aggregate and does not constitute personal data by itself.

## Rollback (kill switch)

1. Cloudflare Pages → sparkry-crm → Environment variables → set `PAY_SHORT_LINKS_ENABLED=false`. Redeploy.
2. New invoice sends revert to long Stripe URLs. Existing emails still work as long as the Worker is up.
3. If the Worker itself must come down, prefer deploying a stub Worker that 302s every path to a static "contact billing@sparkry.ai" page over `npx wrangler delete --name sparkry-pay` (the latter strands all sent emails).

## Schema rollback limitation

D1 does NOT support DROP COLUMN. The `short_url_card` and `short_url_ach` columns on `invoices` cannot be removed without a full table rebuild (CREATE new table, INSERT SELECT, DROP old, RENAME). The `payment_link` table can be dropped: `DROP TABLE payment_link`. Prefer feature-flag disable over schema rollback.

## Future schema changes

To apply a new migration to the production database:
```bash
npx wrangler d1 migrations apply sparkry-crm-prod --remote
```

## Worker updates

To deploy a new version of the pay Worker:
```bash
pnpm build:pay-worker && npx wrangler deploy --config wrangler.pay.toml
```

## DNS + custom domain setup (REQ-PAY-073)

To add the custom domain after deploying the Worker (or if the domain mapping needs to be re-added):

1. Cloudflare dashboard → Workers & Pages → sparkry-pay → Settings → Triggers → Custom Domains → Add → enter `pay.sparkry.ai`
2. Cloudflare creates the CNAME record automatically (CF-internal target, NOT to workers.dev). Do not create the DNS record manually.
3. Cert provisioning takes < 5 minutes (Universal SSL).
4. Verify:
```bash
curl -sI https://pay.sparkry.ai/healthz
```
Expected: HTTP/2 200 with `content-type: text/plain` and no `Set-Cookie` header.

To verify no CRM session cookie leaks to `pay.sparkry.ai` (cookie domain audit):
```bash
curl -sv https://pay.sparkry.ai/healthz 2>&1 | grep -i 'set-cookie'
```
Expected: empty output.

## Sentry

DSN is the shared CRM DSN. Events are tagged `service: sparkry-pay` via `initialScope` in the `withSentry` init. Filter in Sentry on `tags.service = "sparkry-pay"` to isolate pay Worker events from CRM events. Set up an alert rule: `tags.service = sparkry-pay AND level = error` → immediate email/PagerDuty notification.
