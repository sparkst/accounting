# Balance Milestone Alerts — Execution Plan

**Spec:** `docs/superpowers/specs/2026-06-14-balance-milestone-alerts-design.md`
**REQs:** REQ-BAL-001..010 (`requirements/current.md`)
**Date:** 2026-06-14

Two-system feature. This plan sequences the **accounting box** half (this repo, fully
testable now) first, then the **sparkry-crm** half, then the cross-system deploy which is
gated on a human prerequisite (Chase re-auth).

---

## Track A — accounting box (`src/balance_alerts/`, this repo)

> Mirrors the existing `src/alerts/` pattern: pure rule functions + webhook + dispatcher +
> CLI. Reuses the `alert_dispatch` ledger for dedup. DRY-RUN default. No new migration.

| # | Task | REQ | Dep | SP |
|---|------|-----|-----|----|
| A1 | `rules.py`: pure milestone-crossing engine. `compute_balance_alerts(today, session)` reads latest + prior-day `plaid_account_balance_snapshot` per account, applies type-driven rules, returns `BalanceAlert` objects (carries `severity`). | BAL-001..006 | — | 5 |
| A2 | Co-located `test_rules.py`: TDD-first. Crossing up/down, each milestone, severity map, AND-drift, loan mute, null-baseline, liability negation, no-recross-same-day, dedup-key shape. | BAL-001..006 | A1 | 5 |
| A3 | `webhook.py`: `post_balance_alert()` → POST `{type, title, message, source, account, balance, level}` to `N8N_SEVERITY_WEBHOOK_URL` w/ secret header; HTTPS-only; DRY-RUN builds payload, no network. | BAL-007 | A1 | 3 |
| A4 | `test_webhook.py`: payload shape, severity passthrough, https guard, dry-run no-call, secret header present + never logged. | BAL-007 | A3 | 3 |
| A5 | `dispatcher.py`: compute → filter already-sent (`alert_dispatch`) → POST → record. Per-alert error isolation. Reuse `AlertDispatch`. | BAL-006,010 | A1,A3 | 3 |
| A6 | `test_dispatcher.py`: dedup (no double-send), error isolation (one POST raises, rest proceed), dry-run writes nothing. | BAL-006,010 | A5 | 3 |
| A7 | `digest.py` + test: build the daily `info` account-pulse (all monitored accounts, balance, breach flag) → single POST. | BAL-008 | A1 | 3 |
| A8 | `scripts/balance_alerts_dispatch.py`: CLI, DRY-RUN default, `--apply`, `--date`, `--digest`. Mirror `scripts/alerts_dispatch.py`. | BAL-010 | A5,A7 | 2 |
| A9 | Prereq ops: add `plaid-balance-sync.timer/.service` units (daily) so business balances are fresh. **Chase re-auth is human-only (flag, don't block build).** | BAL-009 | — | 2 |

## Track B — sparkry-crm (personal accounts)

| # | Task | REQ | Dep | SP |
|---|------|-----|-----|----|
| B1 | Rewrite `src/lib/server/wealth/balance-drift.ts` → milestone engine (checking/savings/credit) keyed by `account_type`; keep investment drift switched `OR→AND` at 15%/$25k. | BAL-001..005 | A1 (shared rule constants) | 5 |
| B2 | Replace `sendAlertEmail(...)` with severity-tagged n8n POST; dedup key `milestone:{account}:{level}:{utcDate}` in `WEALTH_KV`. | BAL-006,007 | B1 | 3 |
| B3 | Update/extend `*.test.ts` for milestone behavior + AND-drift + n8n routing (Miniflare). | BAL-001..007 | B1,B2 | 5 |
| B4 | Daily pulse digest from the nightly cron Worker (`info`, 14:00 UTC). | BAL-008 | B1 | 3 |

## Track C — cross-system

| # | Task | REQ | Dep | SP |
|---|------|-----|-----|----|
| C1 | Resolve n8n webhook entrypoint for `UT-Send Alert Message`; provision its URL+secret in `accounting/srv` and the Worker secrets. | BAL-007 | — | 2 |
| C2 | **DEPLOY GATE** — user confirms prod deploy (Cloudflare Worker + Hetzner box). | all | A*,B*,C1 | — |
| C3 | Validate: trigger a known crossing in each system; confirm Telegram receipt at correct severity; confirm daily pulse. | all | C2 | 3 |

---

## Execution order (this pipeline run)
1. **Track A** (TDD → review-loop → test-gate → verify) — fully autonomous in this repo.
2. **Track B** — sparkry-crm implementation + review-loop.
3. **Track C** — gate at C2 (deploy), then C3 validate.

## Convergence gate
Review-loop runs to **zero P0 + zero P1** across all 4 lenses (security, financial-correctness,
code-quality, test-coverage) before test-gate. No phase skipped.
