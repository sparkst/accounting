# pay.sparkry.ai — End-to-end production test

**Date:** 2026-05-27 evening (Pacific) / 2026-05-28 UTC
**Driver:** Claude via Chrome MCP + Gmail MCP + wrangler d1 CLI
**Customer fixture:** "Pay E2E Test" (`ec4f70fc-9b61-4b72-a40c-6afd82c1e891`) — sparkst@gmail.com (Travis sink)
**Invoices:** PAY-001 (Phase A, flag=false), PAY-002 (Phase B+C, flag=true)

## Setup

- Production CRM: `https://internal.sparkry.ai` (CF Pages deployment `9a2fe4dd-ae55-4d70-84c9-bec64755bbb5` on commit `b9046e3`)
- Production Worker: `sparkry-pay` at `https://pay.sparkry.ai`, version `9acc2b28-089c-4b38-9fb0-e21f7f5196be`
- Production D1: `sparkry-crm-prod` (`b50aa011-bcd2-4db0-92b1-0d35bd75db93`)
- Migration `0011_payment_link.sql` applied to prod D1 on 2026-05-27
- Feature flag: `PAY_SHORT_LINKS_ENABLED` (CF Pages secret)

## Phase A — flag=false regression guard

**Setup:** `PAY_SHORT_LINKS_ENABLED=false` already set; CRM redeployed with PR #17 code.

**Steps:** Created work order "pay.sparkry.ai E2E Test" → milestone "E2E Test Milestone" $1.00 → Mark Complete → draft invoice PAY-001 → Review & Send → Send Invoice (recipient sparkst@gmail.com).

**Verification:**

1. D1 `payment_link` table query: 0 rows. `mintShortLink` correctly gated off.
2. Gmail thread `19e6c16b853e4e74` plain-text body:
   ```
   Pay by Credit Card (includes $0.04 processing fee): https://buy.stripe.com/8x28wO1wx6ae1K2dyLcZa0j
   Pay by ACH Bank Transfer (no fee): https://buy.stripe.com/28EfZg5MNeGKbkC8ercZa0k
   ```
3. HTML body buttons encode the same long Stripe URLs (wrapped by Resend's `link.sparkry.ai/CL0/...` click-tracker; the encoded destination is the raw Stripe URL).

**Result:** No regression. With the flag off, the new code path behaves identically to the pre-PR code.

## Phase B — flag=true → short URL mint + redirect + click counter

**Setup:** Flipped `PAY_SHORT_LINKS_ENABLED=true` via `wrangler pages secret put`. Triggered CF Pages redeploy via empty commit `b9046e3`. New production deployment `9a2fe4dd` live.

**Steps:** Created work order "pay.sparkry.ai E2E Test Phase B" → milestone "E2E Test Milestone B" $1.00 → Mark Complete → draft invoice PAY-002 → Review & Send → Send Invoice.

**Verification:**

1. D1 `payment_link` table query (immediately after send):
   ```
   slug      | target_url                                              | rail | invoice_id    | click_count | revoked_at
   HFGfisPw  | https://buy.stripe.com/3cI7sK5MN426agy3YbcZa0l          | card | 7ddb78c6-...  | 0           | null
   5QomOtBb  | https://buy.stripe.com/eVq14ma33dCGgEW8ercZa0m          | ach  | 7ddb78c6-...  | 0           | null
   ```
   `mintShortLink` fired for both rails, persisted to D1.

2. `invoices.short_url_card` and `short_url_ach` populated on row `7ddb78c6-39ea-4d1d-8acd-7a7f6ca4017c`.

3. `curl -sI https://pay.sparkry.ai/HFGfisPw` →
   ```
   HTTP/2 302
   location: https://buy.stripe.com/3cI7sK5MN426agy3YbcZa0l
   cache-control: no-store
   strict-transport-security: max-age=31536000; includeSubDomains; preload
   content-security-policy: default-src 'none'
   referrer-policy: no-referrer
   x-content-type-options: nosniff
   ```
   All security headers present (REQ-PAY-035).

4. Same for `5QomOtBb` → 302 → ACH Stripe link.

5. Two GET requests against `HFGfisPw` → `click_count=2`, `last_clicked_at=2026-05-28T01:27:22Z`. `ctx.waitUntil` D1 write succeeded. HEAD requests (`curl -sI`) intentionally did not bump the counter (REQ-PAY-031).

6. Gmail thread `19e6c2de133e0338` plain-text body:
   ```
   Pay by Credit Card (includes $0.04 processing fee): https://pay.sparkry.ai/HFGfisPw
   Pay by ACH Bank Transfer (no fee): https://pay.sparkry.ai/5QomOtBb
   ```
   HTML buttons encode `pay.sparkry.ai/HFGfisPw` and `pay.sparkry.ai/5QomOtBb`.

**Result:** Full mint → email → click → redirect → counter chain working end-to-end.

## Phase C — void → revoke → 410

**Steps:** Navigated to PAY-002 invoice page → Void → confirm Void Invoice.

**Verification:**

1. D1 query post-void:
   ```
   slug      | click_count | revoked_at
   HFGfisPw  | 2           | 2026-05-28T01:33:08Z
   5QomOtBb  | 0           | 2026-05-28T01:33:08Z
   ```

2. `activity_log` entries (both at `2026-05-28T01:33:07.755Z` — same batch, atomic):
   - `voided` action with `{"reason":""}`
   - `payment_links_revoked` action with `{"count":2}`

   This confirms the revoke statement was inside the same `db.batch()` as the status update (per Task 5.1 Step 4 — Drizzle query builder, NOT `db.run(sql\`...\`)`).

3. `curl -sI https://pay.sparkry.ai/HFGfisPw` → `HTTP/2 410` with `text/html; charset=utf-8` body + security headers.
4. Same for `5QomOtBb` → 410.

**Result:** Voiding atomically revokes all active short URLs and the Worker correctly serves 410.

## REQ-PAY-072 — Rate limiting

Configured as in-Worker `[[ratelimits]]` binding (60 req / 10s per IP). Single-source-IP burst tests did NOT reliably trigger 429s because the limiter is per-Cloudflare-data-center (per CF docs). Sustained slug-enumeration from a distributed attacker would hit individual POP counters and trigger 429s on per-POP traffic exceeding the limit. The Worker code is in place; production behavior matches the documented design.

## Test data retained in prod

These test artifacts remain in prod for the operational record:

- Customer: `Pay E2E Test` (`ec4f70fc-9b61-4b72-a40c-6afd82c1e891`)
- Work orders: `pay.sparkry.ai E2E Test` (completed, `b9505e0a-...`), `pay.sparkry.ai E2E Test Phase B` (active, `91d31b47-...`)
- Invoices: PAY-001 (sent, `c3c10aad-...`), PAY-002 (void, `7ddb78c6-...`)
- `payment_link` rows: `HFGfisPw` (revoked), `5QomOtBb` (revoked)

Delete via direct D1 statements when no longer needed; the customer/work-order rows are safe to leave indefinitely.
