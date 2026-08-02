# Alerting consolidation — everything through the n8n severity webhook

**Date:** 2026-08-02
**Status:** repo side implemented on `fix/alert-noise`; box cutover pending deploy
**Companion fixes in the same PR:** stale-sweep replay fix (REQ-FIX-ALR-007), Hetzner Caddyfile restoration (root cause of the 2026-07-30..08-02 alert storm)

## 1. Why now — the 14-day alert-volume audit (2026-07-19 → 2026-08-02)

### Resend OnFailure emails (channel c — the noise)

Actual emails sent = dedup sentinels in `data/.alerts` on the box:

| Source unit | OnFailure trips | Emails sent | Root cause |
|---|---|---|---|
| `accounting-uptime-check.service` | 996 | 91 | Caddy dead since the 2026-07-29 23:07 UTC reboot: the rsynced `Caddyfile` was still the retired MacBook config (`macbook.ancon-cliff.ts.net` → auto-HTTPS → bind :80/:443 → permission denied as `travis`). Probe failed every 5 min; hourly dedup still emits ~24 emails/day. Fixed in this PR (repo `Caddyfile` replaced with the Hetzner spec config); deploy = rsync + `systemctl restart caddy`. |
| `accounting-api.service` | 7 | 6 | Deploy restarts: `doppler run` exits 255 when its child is stopped → unit marked failed. Already fixed live 2026-07-27 via `deploy/overrides/accounting-api.service.d` (`SuccessExitStatus=255`); all 7 trips predate the fix. |
| `caddy.service` | 6 | 1 | Same Caddyfile incident (5 rapid restarts at boot, StartLimit collapsed them into one email). |
| `plaid-transactions-sync.service` | 3 | 3 | Documented behavior: job exits non-zero if ANY Item errors (`ITEM_LOGIN_REQUIRED` pages daily until re-auth). |
| `plaid-balance-sync.service` | 3 | 2 | Same Plaid Item-error semantics. |
| `plaid-investments-sync.service` | 2 | 1 | Same. |
| `weekly-pl-report.service` | 1 | 1 | One-off. |
| `accounting-monthly-close.service` | 1 | 1 | One-off. |
| `accounting-dashboard.service` | 1 | 1 | One-off. |
| **Total** | **1,022** | **107** | ~85% of trips and emails trace to the dead Caddy. |

### n8n severity webhook (channel a — healthy)

`alert_dispatch` rows, last 14 days, all `status='sent'`: `balance_pulse` 15 (daily digest, by design), `run_marker` 15 (internal, no send), `tax_bo` 3 (day-3/10/17/25 reminder cadence, by design), `autoconfirm_digest` 2, `invoice_sweep` 1, `monthly_close` 1, `wbr_weekly` 1. **No volume problem here.**

### Sweep replay bug (fixed in this PR — REQ-FIX-ALR-007)

`src/alerts/sweep.py::sweep_failed_rows` re-POSTed stored `payload_json` VERBATIM for any `status='failed'` row in a 7-day lookback. On 2026-08-02 a `balance_pulse` row flipped to `failed` was replayed twice, re-delivering a 12-hour-old digest as if current. Fix: callers declare point-in-time digest types via `same_day_only_types`; the sweep replays those only on the same `occurrence_date` and flips older rows to terminal `status='superseded'`. `balance_pulse` is so declared; durable date-keyed types (`tax_bo`, `balance_milestone`, `policy_drift`, …) keep the 7-day window. Tests: `src/alerts/test_sweep.py`, `src/balance_alerts/test_dispatcher.py`.

## 2. Emitter inventory → target channel

Three channels today: (a) n8n severity webhook, (b) EA webhook (unprovisioned), (c) Resend email via `accounting-alert@.service`.

| Emitter | Current channel | Target | Change required |
|---|---|---|---|
| Balance milestones + drift (`src/balance_alerts/`, `accounting-balance-alerts.timer`) | (a) `N8N_SEVERITY_WEBHOOK_URL` | unchanged | none |
| Daily account pulse (`src/balance_alerts/digest.py`) | (a) | unchanged | none (sweep-staleness fix shipped in this PR) |
| Freshness sentinel (`scripts/freshness_sentinel.py`, `src/monitoring/sentinel.py`) | (a) | unchanged | none |
| Policy drift (`scripts/policy_drift_dispatch.py`) | (a) | unchanged | none |
| AR-chaser draft notifications (`src/ar/chaser.py`) | (a) | unchanged | none |
| **systemd OnFailure (all ~20 units)** | (c) `accounting-alert@.service` → Resend email | **(a)** via `accounting-alert-webhook@.service` → `scripts/alert_webhook.py` | **implemented in this PR** (repo only; cutover at deploy). sev2 = serving stack (`accounting-api`, `accounting-dashboard`, `caddy`, `cloudflared`, `accounting-uptime-check`); sev3 = every batch timer. |
| **EA B&O / invoice reminders** (`src/alerts/`, `scripts/alerts_dispatch.py`, `accounting-ea-alerts.timer` — DRY-RUN today) | (b) `N8N_ALERTS_WEBHOOK_URL` (never provisioned) | **(a)** as `type=info` | code change (next PR): map `Alert` → UT contract (`type/title/message/alert_key`) and post via `src.balance_alerts.webhook.post_payload`. Retires channel (b) before it ever ships — no new n8n workflow, no new Doppler keys. Sweep note: stored email-shaped `payload_json` rows are not valid UT payloads; on cutover, flip any `status='failed'` EA rows to `superseded` (same mechanism as REQ-FIX-ALR-007) so the sweep never replays a legacy-shape payload at the new target. |
| Report/content emails (WBR weekly, monthly close, autoconfirm digest — `delivery_channel='resend_email'`) | Resend (content, not alerts) | unchanged | out of scope — these are reports a human reads in email; only the *alerting* Resend path retires. |

n8n side (NO workflow changes needed): the target is the existing `WH-Severity / Send Alert` (`wwY1EQulQ90yrKqs`) wrapping `UT-Send Alert Message` (`zH9sxbqM2aw8UVXE`) — contract per `n8n-render/docs/sending-alerts.md`. Plain text in `title`/`message` (endpoint handles encoding), `200 = accepted`, unknown `type` downgrades to `info`.

## 3. Doppler keys (`accounting/srv`)

| Key | Status | Notes |
|---|---|---|
| `N8N_SEVERITY_WEBHOOK_URL` | present | already used by balance alerts + sentinel |
| `N8N_SEVERITY_WEBHOOK_SECRET` | present | source of truth per `sending-alerts.md`; mirrored in `jarvis/dev`, `claude-code/dev` — never mint fresh |
| `N8N_ALERTS_WEBHOOK_URL` / `N8N_ALERTS_WEBHOOK_SECRET` | never provisioned | **do not provision** — EA reminders retarget to the severity webhook (§2); delete the keys from `accounting/dev` after the EA cutover PR |
| `ALERT_FROM_EMAIL` / `ALERT_TO_EMAIL` | never provisioned | obsolete under this plan |
| `RESEND_API_KEY` | present | **keep** — still used by report/content emails (WBR, monthly close, autoconfirm digest) |

## 4. The `accounting-alert@.service` replacement (implemented, repo-only)

- `scripts/alert_webhook.py` (+ `scripts/test_alert_webhook.py`): same contract as `scripts/alert.py` — hourly per-unit dedup sentinel (namespace `alert-webhook-*`, independent of the email path's `alert-*` so both channels keep their own budget during transition), exit non-zero only on real send failure, journal-tail + failed-ledger body enrichment reused from `scripts/alert.py` (including `_redact`).
- `deploy/accounting-alert-webhook@.service`: mirrors the box's `accounting-alert@.service` hardening + anti-storm guards (`StartLimitIntervalSec=300`/`Burst=3`, **no `OnFailure=` of its own**), `ExecStart` → `scripts.alert_webhook "%i"`.
- **Nothing changes until deployed**: no unit references the new template yet.

## 5. Cutover (deploy session, ~20 min)

1. rsync repo → box; `systemctl daemon-reload`.
2. Install the template: `cp deploy/accounting-alert-webhook@.service /etc/systemd/system/` + `daemon-reload`.
3. Smoke test WITHOUT touching any real unit: `systemctl start accounting-alert-webhook@smoke-test.service` → expect one sev3 Telegram message within ~10 s.
4. Flip ONE canary unit's `OnFailure=` (suggest `accounting-disk-check.service`) to `accounting-alert-webhook@%n.service`; force a failure; confirm Telegram delivery + hourly dedup.
5. Flip the remaining units (drop-ins or sed over `/etc/systemd/system/*.service`), `daemon-reload`.
6. Leave `accounting-alert@.service` installed but unreferenced for 2 weeks (instant rollback), then remove.

## 6. Rollback

Any step: point `OnFailure=` back to `accounting-alert@%n.service` + `daemon-reload` — the Resend path stays fully intact on the box until the 2-week soak ends. The two dedup namespaces never collide, so flapping between channels cannot suppress an alert.

## 7. Test plan

- **Repo (this PR, all green):** `scripts/test_alert_webhook.py` (dedup, sev2/sev3 mapping, failure exit codes, sentinel namespace), `src/alerts/test_sweep.py` + `src/balance_alerts/test_dispatcher.py` (REQ-FIX-ALR-007), `scripts/test_deploy_units.py` (unit-file lint), `caddy validate` on the new Caddyfile.
- **Box, after cutover:** §5 steps 3–4 (smoke unit + canary failure), then one induced real failure per severity class (a timer job → sev3; stop caddy for one probe cycle → sev2). Confirm: exactly one Telegram message per unit per hour; no Resend email for the flipped units; `UT-Send Alert Message` executions show no error status.
- **Regression watch (1 week):** daily `ls data/.alerts | grep alert-webhook-` count vs Telegram messages; `journalctl -u 'accounting-alert-webhook@*'` for non-zero exits.

## 8. Residual noise policy (post-consolidation)

- `ITEM_LOGIN_REQUIRED` still pages daily per failing Plaid Item (by design — it needs a human re-auth). Under the webhook path it becomes a sev3 Telegram ping instead of an email; acceptable. A future refinement can dedupe by Item error state rather than by hour.
- A sustained serving-stack outage pages sev2 once per hour per unit (uptime-check). If that is still too chatty, add an escalation sentinel (first fire = sev2, repeats within 6 h = suppressed) in `scripts/alert_webhook.py` — deliberately NOT included in this PR to keep cutover behavior identical to the email path.
