# M0g evidence — REQ-WC-019 secret audit (2026-05-11)

## Result summary

| Surface | Provisioned | Required | Notes |
|---|---|---|---|
| Cloudflare Pages (`sparkry-crm`) | 9/9 | 9 | All REQ-WC-019 Pages secrets present |
| Cloudflare Worker (`sparkry-crm-cron`) | 8/10 | 10 | SENTRY_DSN + R2_BACKUP_WRITE_TOKEN missing — pre-cutover blockers, not team-lead blockers |
| Doppler `accounting/dev` | 4/4 | 4 | WEALTH_*, PLAID_FERNET_KEY |
| Doppler `accounting/prd` | 1/1 | 1 | PLAID_TOKEN_ENC_KEY_MIGRATION (migration script mirror) |

## Pages secrets (9/9)

```
✓ PLAID_CLIENT_ID
✓ PLAID_SANDBOX_SECRET
✓ PLAID_PRODUCTION_SECRET
✓ PLAID_ENV
✓ PLAID_TOKEN_ENC_KEY  (AES-GCM, distinct from PLAID_FERNET_KEY)
✓ TWELVE_DATA_API_KEY
✓ WEALTH_INTERNAL_KEY
✓ WEALTH_ALLOWED_EMAILS
✓ RESEND_API_KEY  (inherited from CRM)
```

Additional CRM-related Pages secrets present (not REQ-WC-019 scope): `ALLOWED_EMAILS`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `RESEND_WEBHOOK_SECRET`, `SENTRY_DSN`, `SESSION_SIGNING_KEY`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`.

## Cron Worker secrets (8/10)

```
✓ PLAID_CLIENT_ID
✓ PLAID_SANDBOX_SECRET
✓ PLAID_PRODUCTION_SECRET
✓ PLAID_ENV
✓ PLAID_TOKEN_ENC_KEY
✓ TWELVE_DATA_API_KEY
✓ WEALTH_INTERNAL_KEY
✓ RESEND_API_KEY
✗ SENTRY_DSN  (user action: retrieve from Sentry dashboard or copy from Pages value)
✗ R2_BACKUP_WRITE_TOKEN  (user action: generate CF API token with R2 WRITE-only permission scoped to sparkry-crm-backups/wealth/*)
```

## Doppler accounting/dev (4/4)

```
✓ WEALTH_API_BASE = https://internal.sparkry.ai
✓ WEALTH_INTERNAL_KEY  (matches Pages value for local importer)
✓ WEALTH_TARGET_DEFAULT = local  (flipped to cloud at cutover step 7i)
✓ PLAID_FERNET_KEY  (the legacy Fernet key, renamed from PLAID_TOKEN_ENC_KEY at M0c)
✓ Legacy PLAID_TOKEN_ENC_KEY absent  (correctly removed by M0c)
```

## Doppler accounting/prd (1/1)

```
✓ PLAID_TOKEN_ENC_KEY_MIGRATION  (AES-GCM mirror, read by migrate-from-sqlite.ts during cutover step 7d)
```

## Team-lead spawn impact

The two remaining cron Worker secrets are **pre-cutover blockers, not team-lead-spawn blockers**:

- **crm/d1-schema**: writes schema and migration scripts; does not need either secret.
- **crm/workers-plaid**: writes handler code; does not call SENTRY_DSN or R2_BACKUP_WRITE_TOKEN.
- **crm/workers-brokerage**: writes handler code including `handleR2Backup`; tests use mocked R2 binding, so `R2_BACKUP_WRITE_TOKEN` is not needed during development. The handler reads `env.R2_BACKUP_WRITE_TOKEN` at runtime only.
- **crm/frontend-brokerage**: pure UI work; no secrets.
- **crm/frontend-desk**: pure UI work; no secrets.
- **acct/local-migration**: local Python changes; uses `PLAID_FERNET_KEY` (provisioned).
- **acct/importer-cloud**: uses `WEALTH_INTERNAL_KEY` + `WEALTH_API_BASE` (both provisioned).

Both missing secrets MUST be provisioned BEFORE the pre-cutover checklist gate at runbook §7. The pre-cutover checklist already verifies their presence on the cron Worker.

## Pre-cutover blockers still pending

- SENTRY_DSN on `sparkry-crm-cron` (operator action: retrieve from Sentry dashboard or from the CRM Pages secrets via dashboard "reveal" UI; provision via `printf '%s' "$SENTRY_DSN" | wrangler secret put SENTRY_DSN --name sparkry-crm-cron`).
- R2_BACKUP_WRITE_TOKEN on `sparkry-crm-cron` (operator action: generate CF API token at dash.cloudflare.com/profile/api-tokens with permission "Workers R2 Storage:Edit" scoped to bucket `sparkry-crm-backups`, prefix `wealth/`; provision via `wrangler secret put`).
- R2_BACKUP_PRUNE_TOKEN on a SEPARATE prune-cron handler (per REQ-WC-018 two-token split). This handler doesn't exist yet — it will be created by crm/workers-brokerage as part of BR-T06. The prune token can be generated alongside the write token at the same time.
