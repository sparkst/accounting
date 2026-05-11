# M0h evidence — Plaid redirect URI registered (sandbox + production)

**Date:** 2026-05-11
**Registered URI:** `https://internal.sparkry.ai/wealth/desk/connections/oauth-return`
**Registered by:** Travis (manually via Plaid dashboard)
**Verification method:** API call to `POST /link/token/create` against both environments — Plaid validates the `redirect_uri` request param against the allowed-URIs allowlist server-side; a success response (returns a `link_token`) proves the URI is registered, an `INVALID_REDIRECT_URI` error proves it is not.

## Sandbox verification

```
POST https://sandbox.plaid.com/link/token/create
Body: {
  "client_id": "<PLAID_CLIENT_ID from doppler accounting/dev>",
  "secret": "<PLAID_SANDBOX_SECRET from doppler accounting/dev>",
  "user": {"client_user_id": "travis-m0h-verify"},
  "client_name": "M0h verification",
  "products": ["transactions"],
  "country_codes": ["US"],
  "language": "en",
  "redirect_uri": "https://internal.sparkry.ai/wealth/desk/connections/oauth-return"
}

→ STATUS: OK (response contained link_token; no error_code or error_message)
```

## Production verification

```
POST https://production.plaid.com/link/token/create
Body: same as sandbox except secret=$PLAID_PRODUCTION_SECRET

→ STATUS: OK (response contained link_token; no error_code or error_message)
```

## What "production OK" means

Plaid production redirect URI registrations sometimes go through a manual review period of up to 24 hours. The production link/token/create response returning `OK` (not pending) means the URI is **active**, not pending review. Step 7h in the cutover sequence only re-confirms; no further action needed at cutover.

## Old tunnel URI

The legacy `cloudflared` tunnel redirect URI (the public CNAME for `plaid-oauth-return`, retrievable via `cloudflared tunnel list`) is **kept in Plaid's allowed-URIs list during the soak window** for rollback safety. It is removed at step 9b post-soak per REQ-WC-015.

## Re-verification at cutover

Step 7h of the runbook should re-run this verification (both sandbox + production POST) immediately before flipping `PLAID_ENV=production` on Workers Pages. If either environment fails, halt cutover.
