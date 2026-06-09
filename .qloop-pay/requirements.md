# Requirements — pay.sparkry.ai Short-Link Redirect

## User ask (verbatim)

> "Let's use pay.sparkry.ai and cloudflare. Plan out the requirements and design for this and then /qreview the output. Fix all P0-P1 issues and significant P2-3s. /qloop doing clean context /qreview until no new P0-1 found."

> Earlier framing: "For our online invoicing capability with our internal.sparkry.ai CRM (not our accounting system directly) we send out links to stripe using stripe's domain name. ... I want an engineering analysis of which would be best to ensure our emails don't appear as SPAM because we're sending to from the wrong domain."

## Functional goals

1. **Replace long Stripe Payment Link URLs in invoice emails with branded `https://pay.sparkry.ai/<slug>` short URLs** that 302-redirect to the underlying Stripe checkout.
2. **Improve DMARC/sender-domain alignment** — emails sent from `@sparkry.ai` should contain visible URLs on the same eTLD+1.
3. **Maintain idempotency** — re-sending an invoice reuses the same short URL; the customer can pay using whichever email copy they have.
4. **Support revocation** — voiding an invoice in the CRM invalidates its short URLs; customers see a friendly "canceled" page, not a broken redirect to a dead Stripe link.
5. **Track clicks for operational visibility** (counter + last-clicked timestamp), without blocking the user-facing redirect on the click write.
6. **Be deployable end-to-end on the existing Cloudflare + D1 stack** the CRM already uses. No new SaaS dependency, no new $$ cost beyond what's already paid.

## Non-functional constraints

1. **The redirect endpoint MUST NOT be auth-gated.** Recipients of invoice emails are external customers without Google-OAuth identity.
2. **The redirect endpoint MUST NOT be host-shared with `internal.sparkry.ai`** in a way that allows auth-bypass leakage. Structural separation, not configurational.
3. **Open-redirect resistance** — a compromise of the CRM session that lets an attacker mint short URLs must NOT yield a generic open redirect. Allowlist Stripe checkout hosts and ONLY those.
4. **No customer PII in slugs, logs, or response headers.**
5. **No JavaScript, cookies, or trackers on the redirect surface** — this keeps the response audit-simple and immune to client-side attacks.
6. **The implementation must follow the existing sparkry-crm patterns** for D1, Drizzle, Sentry, Vitest+Miniflare, wrangler deploys, and migrations. See `sparkry-crm/CLAUDE.md` for these patterns.
7. **The artifact is two files together** — a design spec under `accounting/docs/superpowers/specs/` and an implementation plan under `accounting/docs/superpowers/plans/`. Both are concatenated as the review artifact. Reviewers should look at the whole bundle: spec correctness, plan-fidelity-to-spec, plan executability, and combined production-readiness.

## Review lenses

The qloop will spawn reviewers covering at minimum these lenses. Each operates clean-context.

- **Security** — open redirect, slug enumeration, header injection, cache poisoning, auth-bypass via host confusion, secrets handling, host-cookie boundary
- **Financial correctness** — even though no $ math is performed in this Worker, link integrity matters: wrong customer pays wrong invoice, leak across customers, double-charge from re-mint, race between send and void
- **Code quality** — TypeScript strictness, DRY, YAGNI, error-handling boundaries, naming, idiomatic SvelteKit / Cloudflare Workers patterns, match to existing sparkry-crm conventions
- **Test coverage** — REQ-ID traceability, Miniflare D1 patterns, integration vs unit boundary, regression-pinning of past bugs (see "Critical Patterns" section of sparkry-crm/CLAUDE.md for examples like the `ACCOUNT_SELECT` regression)
- **Operational readiness** — deploy plan, rollback, runbook completeness, WAF rule structure, monitoring, evidence captures

## Convergence criteria

- **Converged**: Zero P0 findings AND zero P1 findings AND combined (P2+P3) at or below threshold.
- **Min rounds**: 2 (qloop default; enforced).
- **Max rounds**: 5 (default).
- **Fix-ALL**: every finding at every priority must be addressed with FIXED status or ESCALATED with justification.

## Out of scope

- Implementation itself — this is a planning artifact review. The actual code changes happen after plan approval. The plan IS the artifact; the code is downstream.
- Migrating already-sent emails to short URLs — those emails are already in customers' inboxes.
- Short links for non-invoice use cases (newsletter CTAs, social bio links) — Short.io covers that today.
