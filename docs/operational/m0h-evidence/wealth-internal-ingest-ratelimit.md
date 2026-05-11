# M0h evidence — WAF rate-limit rule deployed

**Date deployed:** 2026-05-11
**Operator:** Travis (manually via Cloudflare dashboard)
**Cloudflare zone:** `sparkry.ai` (Free plan, 1 rate-limiting rule slot)
**Rule slot:** 1/1 used (Free-plan maximum consumed)

## Rule

| Field | Value |
|---|---|
| Name | `wealth-internal-ingest-ratelimit` |
| Match against | Hostname (visible in the deployed rules list under sparkry.ai → Security → Security rules) |
| Hostname predicate | `internal.sparkry.ai` (equals) |
| URI Path predicate | starts with `/wealth/api/internal/` |
| Characteristic | IP |
| Threshold | 5 requests per 10-second window |
| Action | Block |
| Block duration | 10 seconds |
| Status | Active |
| Order | 1 of 1 (Rate limiting rules section) |

## Free-plan deviation from spec

Spec REQ-WC-002 / runbook M0h originally specified "5 requests per minute per IP." Cloudflare Free plan **locks the rate-limit sampling period to 10 seconds** — `1 minute` is paid-tier-only. We translated the spec intent as follows:

- Spec: 5 req / 60 sec
- Deployed: 5 req / 10 sec (effective ~30 req/min ceiling)

Chose 5/10s rather than 1/10s because the local Python importers fire 4 sequential POSTs per ingest run (`brokerage-csv`, `xlsx-snapshot`, `historical-prices`, `cost-basis-lot`) typically inside 2-3 seconds total. A 1/10s threshold would trip on the second POST. 5/10s gives the importer comfortable headroom while still blocking credential-stuffing bursts (an attacker needing >30 wrong-key attempts per minute is clearly not the importer).

If the deviation becomes a problem (e.g., legitimate use bursts hit the limit), the documented escape hatches are:

1. Upgrade `sparkry.ai` zone to Cloudflare Pro ($20/mo) — 5 rate-limit slots + 1-minute periods unlocked.
2. Add path-specific exemption logic in the Workers handler (record a token bucket per IP in WEALTH_KV and reject inside the route instead).

## Coexisting Custom rules (skip actions) — verified no interference

The Security rules page also shows two pre-existing Custom rules:

| Order | Name | Action | Match |
|---|---|---|---|
| 1 | Allow n8n traffic | Skip | Hostname (n8n-specific) |
| 2 | Allow OAuth2 Redirects | Skip | URI Path (OAuth-return paths) |

Neither rule targets `/wealth/api/internal/*`, so neither bypasses the new rate-limit rule. Confirmed by inspecting the rules list 2026-05-11 with the new rule deployed and active.

## Page state evidence

The Cloudflare dashboard view (`/4f7b1caeb29d300883d65e3b0875675f/sparkry.ai/security/security-rules`) at 2026-05-11 ~15:00 PT confirmed:

```
Rate limiting rules    1/1 used    ⚠ Create rule (greyed out, slot exhausted)

Order  Name                              Match against  Action  CSR  Events last 24h
1      wealth-internal-ingest-ratelimit  Hostname...    Block   -    0
```

## Pre-cutover checklist verification

At cutover time, a fresh curl smoke test against `https://internal.sparkry.ai/wealth/api/internal/ingest/brokerage-csv` issuing 6 POSTs within 10 seconds should observe the 6th request blocked with HTTP 429 from Cloudflare (not the 401 the route would return for missing X-Internal-Key). Run after Workers handlers are live (step 7g).
