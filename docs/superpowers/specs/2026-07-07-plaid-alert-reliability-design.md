# Plaid Balance-Sync Repair + Alert Delivery Reliability — Design Spec

**Date:** 2026-07-07
**Author:** Travis Sparks (with Claude Code)
**Status:** Design → ready for implementation plan
**Branch:** `feat/remediation-and-features-2026-07`
**Scope:** REQ-FIX-PLD-001..006, REQ-FIX-ALR-001..008, REQ-DHL-001..002 (`requirements/current.md` § Program 2026-07)
**Model policy:** no production LLM usage anywhere in this workstream.

---

## 1. Problem

`plaid-balance-sync.timer` has failed nightly since **2026-06-25**: `/accounts/balance/get` returns
`INVALID_PRODUCT` because the paid **Balance** product lapsed. `/transactions/sync` works on the
same tokens (Transactions product is active). Downstream effects: no `plaid_account_balance_snapshot`
rows since Jun-24 → milestone alerts have no prior-day baseline (REQ-BAL-005 mutes them) → the daily
pulse renders stale balances as if current. Compounding failures found in the 2026-07-07 audit:
webhook POSTs are fire-once (a transient n8n blip permanently loses an alert), the balance-sync exit
policy hides retryable failures (exit 0), two dead placeholder Items throw `INVALID_ACCESS_TOKEN`
nightly, unmapped Plaid accounts are silently skipped, and EA date-keyed rules miss firing after
downtime despite `Persistent=true`.

## 2. Fix 1 — switch to `/accounts/get` (REQ-FIX-PLD-001)

`src/adapters/plaid_balance.py` `_balance_request()` (~L339) currently builds
`AccountsBalanceGetRequest`. Replace with:

```python
def _accounts_request(access_token: str) -> Any:
    from plaid.model.accounts_get_request import AccountsGetRequest
    return AccountsGetRequest(access_token=access_token)
```

and the call site (~L197) becomes `client.accounts_get(_accounts_request(access_token))`.

**SDK request/response mapping — what differs from `/accounts/balance/get`:**

| Aspect | `/accounts/balance/get` | `/accounts/get` |
|---|---|---|
| Product entitlement | paid Balance (lapsed) | any active product (Transactions ✓) |
| Freshness | forces a live institution pull | **cached**, refreshed by Plaid's regular Transactions syncs (~daily) |
| Request options | `options.min_last_updated_datetime` supported | not supported — plain `AccountsGetRequest(access_token=...)` |
| Response shape | `resp.accounts[]` of `AccountBase` | identical `AccountBase` list |
| `balances.last_updated_datetime` | populated | typically `None` (Capital One-class only) — must not be relied on |

Every field `_build_snapshot()` (L86-121) consumes — `balances.current/available/iso_currency_code`,
`type`, `subtype`, `mask`, `name`, `to_dict()` — is identical. **Snapshot write path and schema
unchanged**; `Decimal(str(...))` boundary conversion, `raw_data=to_dict()`, and the
`UNIQUE(account_id, snapshot_date)` idempotency key stay as-is. `call_with_retry` (RATE_LIMIT etc.)
unchanged. Cached daily-granularity balances are sufficient: the alerting model is day-over-day.

**Freshness of the cached value (new, since `balances.last_updated_datetime` is typically `None`
per the table above and must not be relied on):** `snapshot_date` is always the **run date**
(unchanged — it's a daily job stamping "as of today's sync"), never derived from
`last_updated_datetime`; the cached balance's *actual* underlying freshness is instead surfaced
downstream by REQ-FIX-ALR-005's staleness marker (§10), which compares `snapshot_date` across
runs, not within one. This means a run can write a snapshot whose underlying institution value
is a day or two stale (Plaid hasn't re-pulled it yet) without that staleness being visible at
write time — it only becomes visible once the pulse/WBR notices consecutive snapshots holding
an identical value for longer than expected. Documented here so PLD-001's test (§12) and
REQ-FIX-ALR-005's staleness test both cover this interaction rather than assuming
`last_updated_datetime` carries the signal.

## 3. Fix 2 — exit-code policy unification (REQ-FIX-PLD-002)

`scripts/plaid_balance_sync.py` L77 exits 0 unless a failure is *terminal*
(`status == "error" and not retryable`) — a retryable `INSTITUTION_DOWN` or any
`accounts_failed > 0` run exits 0 and never trips OnFailure. Mirror
`scripts/plaid_transactions_sync.py` L113:

```python
has_failures = batch.total_failed > 0 or any(r.status != "ok" for r in batch.items)
return 1 if has_failures else 0
```

Idempotent double-runs stay exit-0 (IntegrityError collisions count as `accounts_processed`).

## 4. Fix 3 — dispatcher baseline fallback ≤7d (REQ-FIX-PLD-003)

`src/balance_alerts/rules.py` `compute_balance_alerts()` (L293-332) requires an exact
prior-calendar-day row; the Jun-25 gap means the first post-fix snapshot has no baseline and every
crossing during the outage is muted. Change the baseline query to *most recent snapshot strictly
before `latest.snapshot_date` and within 7 days*:

```python
baseline_row = session.scalars(
    select(Snap).where(
        Snap.account_id == account_id,
        Snap.snapshot_date < latest.snapshot_date,
        Snap.snapshot_date >= latest.snapshot_date - timedelta(days=7),
    ).order_by(Snap.snapshot_date.desc()).limit(1)
).first()
```

`BalanceAlert` gains `baseline_gap_days: int` (1 = normal); it is added to the n8n payload
(`build_payload`) always, and appended to `message` as `" (baseline N days old)"` only when > 1.
Gap > 7d → baseline None → no fire (REQ-BAL-005 null-baseline clause still governs beyond 7d).
`alert_key` unchanged, so dedup semantics are untouched.

## 5. Fix 4 — dead-item exclusion (REQ-FIX-PLD-004)

The two dead Items are abandoned-OAuth placeholder rows (`item_id LIKE 'placeholder_%'`,
`status='active'`, undecryptable token → nightly `INVALID_ACCESS_TOKEN` in the balance sync).
Two-part fix:

1. **Query parity:** `sync_all_active` in `plaid_balance.py` (L363) adopts the transactions-sync
   filter: `.filter(PlaidItem.status == "active", ~PlaidItem.item_id.like("placeholder_%"))`.
2. **Data fix (one-time, on the box):** a small audited script flips both rows to
   `status='disconnected'`, overwrites `access_token_encrypted` with `REVOKED_TOKEN_SENTINEL`, and
   records the reason in `last_error` — never deletes. They remain visible in
   `GET /api/plaid/reconciliation/summary` as disconnected (test asserts the endpoint does not
   filter them out).

## 6. Fix 5 — unmapped-account surfacing + ignore-list (REQ-FIX-PLD-005)

Today (`plaid_balance.py` L124-155, L319-325) unmapped accounts upsert an `ExpectedAccount`
(`status='unconfirmed'`, `source='plaid'`) and only a count reaches `ingestion_log`. Changes:

1. **Log detail:** `ItemSyncResult` collects `unmapped: list[str]` of `"name ·mask· subtype"`;
   `sync_one_item` appends them to `ingestion_log.error_detail` (e.g.
   `unmapped_skipped=2 [Chase Freedom ·4321· credit card; …]`).
2. **Pulse listing:** the daily pulse (§10) lists every `expected_account` row with
   `source='plaid' AND status='unconfirmed'` until mapped or ignored.
3. **Ignore-list mechanism — decision: reuse `expected_account` with a new status value
   `'ignored'`.** Rationale: unmapped Plaid accounts already land in this table with the right
   natural key (institution, name, last_4); a separate ignore table would duplicate that key plus
   join logic for one bit of state; a config file is invisible to the dashboard missing-accounts
   panel, unaudited, and drifts from the DB. Adding one CHECK-constraint value is additive and the
   existing `seed_expected_accounts confirm` walkthrough already mutates this status field — it
   gains an `i = ignore` choice. Migration, revision id `pld05_expected_account_ignored_status`
   (named up front per the cross-workstream migration ledger — plan §Migration ledger):
   batch-alter `ck_expected_account_status` to `('active','closed','unconfirmed','ignored')`.
   Downgrade: flip `ignored` rows back to `unconfirmed` (UPDATE, no deletes), then restore the
   old constraint. The missing-accounts panel and pulse both exclude `status IN ('closed','ignored')`.
   This migration precedes `alr01_alert_dispatch_payload` (§8) in the WS1 chain (ledger order 1
   then 2).

## 7. Fix 6 — webhook retry with backoff + jitter (REQ-FIX-ALR-001)

New shared helper `src/alerts/retry.py`:

```python
def post_with_retry(send: Callable[[], httpx.Response], *, attempts: int = 3,
                    base_delay: float = 1.0, sleep=time.sleep, rand=random.random) -> httpx.Response
```

Semantics: attempt → on `httpx.TransportError`/`httpx.TimeoutException` or a 5xx response, sleep
`base_delay * 2**n + rand() * base_delay` (full jitter on top of exponential), retry up to 3 total
attempts; **4xx returns immediately** (caller-side bug — retrying spams n8n); last failure
propagates (exception re-raised / 5xx response returned). Both clients route their single
`httpx.post` through it: `src/alerts/webhook.py::post_alert` (L73) and
`src/balance_alerts/webhook.py::post_payload` (L55). Error-string discipline unchanged (static
messages, never interpolate `exc`, secret only in `X-Webhook-Secret`). Tests inject fake
`sleep`/`rand` — no wall-clock waits.

## 8. Fix 7 — payload persistence + failed-row sweep (REQ-FIX-ALR-002)

**Migration** (additive, per the alembic-migration skill; `alert_dispatch` is not a protected table
but rows are never deleted):

- `alr01_alert_dispatch_payload`: `ALTER TABLE alert_dispatch ADD COLUMN payload_json TEXT NULL`
  and `ALTER TABLE alert_dispatch ADD COLUMN delivery_channel TEXT NULL` (values:
  `n8n_webhook` | `resend_email`). Downgrade: batch-mode drop of both columns — real downgrade,
  no row loss on protected tables (none touched).

**Channel discriminator (closes the program-wide coupling gap):** `alert_dispatch` is now shared
by every alert/report emitter added in this program (EA/balance dispatchers, the WBR/TXF/SEL
report ledger — reporting spec §2, the auto-confirm digest and monthly-close and AR-chaser
emails — agent-features spec, and the policy-drift alert — wealth spec §11.4). Retry only
applies to the webhook channel; Resend emails are regenerated fresh by their own timer, not
replayed stale. Every write path sets `delivery_channel` explicitly (no inferring it from
whether `payload_json` happens to be NULL): **every** `n8n_webhook` emitter — including
`policy_drift_dispatch.py` (wealth §11.4) and the AR-chaser Telegram draft-notification POST
(agent-features §3.3) — MUST persist `payload_json`; every `resend_email` emitter (reports,
autoconfirm digest, monthly close, the AR-chaser reminder-email send) MUST leave `payload_json`
NULL and rely on the channel filter, not a NULL-payload accident, to stay out of the sweep.
The AR chaser is dual-channel: its approval-request webhook rows are `n8n_webhook`
(persisted + swept per agent-features §3.3); only its reminder-email rows are `resend_email`.

**Write path:** `_record` in both `src/alerts/dispatcher.py` and
`src/balance_alerts/dispatcher.py` (and `digest._record_pulse`, and the new
`policy_drift_dispatch.py`) stores `delivery_channel="n8n_webhook"`,
`payload_json=json.dumps(payload)` — the exact dict handed to `httpx.post`. Resend-backed
emitters (`src/reports/report_email.py`, autoconfirm digest, monthly-close, the AR-chaser
reminder-email send) record `delivery_channel="resend_email"`, `payload_json=NULL`; the
AR-chaser's Telegram draft-notification POST records `delivery_channel="n8n_webhook"` with
its payload persisted (agent-features §3.3 owns that contract).

**Sweep:** at the top of each `--apply` dispatch run, before computing today's alerts:
select rows `(delivery_channel='n8n_webhook' OR delivery_channel IS NULL) AND status='failed'
AND occurrence_date >= (today - 7d).isoformat() AND payload_json IS NOT NULL`, re-POST via the
same retrying client, and on success update the row in place → `status='sent'`, `http_status`,
`error_detail=None`. The explicit `IS NULL` arm makes the query match the stated intent:
pre-migration failed rows (`delivery_channel` NULL, legacy — every pre-migration emitter was
webhook-only) participate in the sweep, gated on `payload_json IS NOT NULL`, so they are
skipped until a payload exists for them (test asserts a NULL-channel + non-NULL-payload row IS
swept). Per-row
isolation: one raising re-POST never halts the sweep or the main run. DRY-RUN performs and
prints the sweep query but neither POSTs nor writes.

## 9. Fix 8 — EA allowlist env-config (REQ-FIX-ALR-003) + catch-up (REQ-FIX-ALR-004)

**Allowlists:** `src/alerts/webhook.py` L21-22 hardcodes `ALLOWED_TO/ALLOWED_FROM`. Replace with
env-read, comma-separated `ALERT_ALLOWED_TO` / `ALERT_ALLOWED_FROM`, each defaulting to the current
literal (`ea-alerts@sparkry.com` / `Travis@sparkry.com`), parsed at call time (not import time) so
tests and Doppler both work. Provision `N8N_ALERTS_WEBHOOK_URL`, `N8N_ALERTS_WEBHOOK_SECRET`,
`ALERT_FROM_EMAIL`, `ALERT_TO_EMAIL` in **`accounting/srv` and `accounting/dev`** via Doppler
(never `.env`) — this unblocks the `accounting-ea-alerts.timer` DRY-RUN→apply cutover.

**Catch-up (missed-day evaluation):** date-keyed EA rules (`_sparkry_monthly_alert` day-3/10/17/25,
last-day-of-month sweep) fire only when `today` matches; a down box on that day loses the reminder
forever despite `Persistent=true`. Design: a **run-marker ledger row** — after each successful
`--apply` run, `dispatch_alerts` writes `AlertDispatch(alert_key='ea:run', occurrence_date=today,
alert_type='run_marker', entity='all', status='sent')`. On start, `last_run = MAX(occurrence_date)
WHERE alert_key='ea:run'`; evaluate every day `d` in `(last_run, today]`, capped at 14 days, calling
the existing per-day compute with `today=d` (each day's alerts carry `occurrence_date=d`, so the
`UNIQUE(alert_key, occurrence_date)` ledger dedups naturally). No marker → evaluate today only.
DRY-RUN writes no marker. No schema change — reuses the existing table.

## 10. Fix 9 — pulse staleness + delivery-health block (REQ-FIX-ALR-005, REQ-DHL-001..002)

`src/balance_alerts/digest.py`: `PulseLine` gains `snapshot_date: date`; a line is **stale** when
`snapshot_date < today - 1 day`. Exact rendering (diff-first, tight — anomalies get ink, healthy
state collapses):

```
Checking
  Sparkry Checking — $66,318.42
  BlackLine Checking — $2,015.10 (as of 2026-07-03) ⏳
Credit
  Blue Business Plus — $1,912.55

3 accounts · 0 flagged · 1 stale

Delivery
  sync: amex ✓0d · chase ⏳3d
  y'day: 2 sent · 1 failed · 0 skipped
  unmapped: Chase Freedom ·4321· credit card
  gap: BlackLine Checking 3d
```

- Healthy collapse: when every item synced <24h, yesterday had 0 failed, and there are no unmapped
  accounts or gaps, the whole block is one line: `Delivery ✓ syncs<24h · 0 failed · 0 unmapped`.
- **Derivation (REQ-DHL-001):** per-item last-success age from the latest
  `ingestion_log` row per source (`plaid_balance:%` / `plaid_tx:%`, `status='success'|'partial_failure'`);
  yesterday's sent/failed/skipped from `alert_dispatch` grouped by status where
  `occurrence_date = yesterday` (run markers excluded); unmapped names from
  `expected_account (source='plaid', status='unconfirmed')`; gap days = `today - snapshot_date`
  per account when ≥ 2.
- **REQ-DHL-002:** each audited silent-failure mode maps to a line above — missed snapshot day →
  `gap:`/stale marker; failed POST → `y'day: … failed` (plus §8 sweep retries it); unmapped skip →
  `unmapped:`; dead item → its `sync:` line ages visibly (disconnected items are excluded from
  `sync:` per §5, so no permanent ⏳ noise). All appear on the next pulse, i.e. within 24h.

## 11. Fix 10 — OnFailure enrichment (REQ-FIX-ALR-006), systemd ordering (REQ-FIX-ALR-007), $0 strict (REQ-FIX-ALR-008)

- **`scripts/alert.py`:** body gains (a) the failing unit's last ~15 journal lines via
  `journalctl -u <unit> -n 15 --no-pager -o cat` (subprocess, 10s timeout, best-effort — on any
  failure the basic email still sends, exit code unchanged), and (b) for dispatcher units
  (`accounting-balance-alerts`, `accounting-ea-alerts`) the subjects of `alert_dispatch` rows with
  `status='failed'` from the last 2 days (read-only SELECT). Ops prerequisite: `travis` joins the
  `systemd-journal` group (deploy step; documented in the unit comment).
- **`deploy/accounting-balance-alerts.service`:** add `After=plaid-balance-sync.service` to
  `[Unit]` — **ordering only** (no `Wants=`/`Requires=`), so a `Persistent=true` boot catch-up runs
  the 04:00 sync before the 14:00 dispatcher evaluates, and a sync failure never blocks alerting.
- **$0 strict crossing:** `_checking_alerts` (rules.py L129-131) fires `baseline > L and
  current <= L`; at `L=0` an exact $0.00 balance alerts as an overdraft, contradicting REQ-BAL-001
  "<$0". Special-case: for `level == 0` the crossing test is `baseline >= 0 and current < 0`
  (strict). Non-zero milestones keep `<=` (unchanged behavior, alert keys stable).

## 12. Test strategy (TDD — failing test with REQ-ID first; co-located `test_*.py`)

| REQ | Test (file · approach) |
|---|---|
| PLD-001 | `src/adapters/test_plaid_balance.py`: `_accounts_request` returns `AccountsGetRequest` with the token; mock client asserts `accounts_get` called, `accounts_balance_get` never; snapshot row fields unchanged vs golden fixture. **New case:** mock `/accounts/get` response with `balances.last_updated_datetime=None` and a value unchanged from the prior day's cached balance → assert `snapshot_date` is still stamped as the run date (not derived from `last_updated_datetime`), the snapshot writes normally, and a follow-up REQ-FIX-ALR-005 digest test (`src/balance_alerts/test_digest.py`) confirms two identical consecutive daily snapshots do NOT themselves trigger a false staleness marker (staleness is snapshot-recency-based, not value-based) — pinning the freshness-regression boundary this switch introduces. |
| PLD-002 | `scripts/test_plaid_balance_sync.py`: batch with retryable `institution_down` item → exit 1; `accounts_failed>0` → exit 1; all-ok double-run → exit 0. |
| PLD-003 | `src/balance_alerts/test_rules.py`: gap-3d baseline found, `baseline_gap_days=3`, message note appended; gap-8d → no alert; gap-1d payload carries `"1"`, no note. |
| PLD-004 | adapter test: placeholder item excluded from rotation; API test: reconciliation summary includes disconnected items. |
| PLD-005 | adapter test: unmapped account writes name·mask·subtype into `error_detail`; digest test: unconfirmed listed, `ignored` not; migration test (`src/db/`): CHECK accepts `ignored`, downgrade flips rows and preserves count. |
| ALR-001 | `src/alerts/test_retry.py`: fake sleep — 5xx→5xx→200 succeeds with 2 sleeps and jittered exponential delays; 4xx returns after 1 attempt; timeout×3 raises. |
| ALR-002 | dispatcher tests both modules: failed row with payload re-POSTs and flips to `sent`; NULL-payload and >7d rows skipped; a `resend_email`-channel failed row (report/digest/close/AR-chaser reminder-email) with a payload is NOT swept (channel filter, regression for the cross-emitter coupling gap); a `policy_drift` webhook failure with payload IS swept; an AR-chaser Telegram draft-notification (`n8n_webhook`) failed row with payload IS swept (agent-features §3.3 contract); a legacy NULL-channel row with a payload IS swept; sweep failure isolates; DRY-RUN writes nothing. |
| ALR-003 | webhook test: env-overridden allowlists honored; defaults match literals; non-allowlisted → `failed` without POST. |
| ALR-004 | dispatcher test: marker at D-3 → days D-2..D evaluated, month-end sweep for the missed day fires once; 20-day gap capped at 14; no marker → today only. |
| ALR-005 | digest test: 3-day-old snapshot renders `(as of …) ⏳`, footer `1 stale`; fresh renders bare. |
| ALR-006 | `scripts/test_alert.py`: monkeypatched journalctl output + failed-ledger titles land in body; journalctl raising still sends and exits 0. |
| ALR-007 | deploy-file assertion test (read unit file, assert `After=plaid-balance-sync.service`, no `Requires=`). |
| ALR-008 | rules test: baseline $50 → current $0.00: below-$1k fires, $0 does NOT; current −$0.01 fires sev2. |
| DHL-001/002 | digest test with seeded `ingestion_log`+`alert_dispatch`+`expected_account` fixtures: exact block text golden-matched; healthy collapse single-line; each of the four silent-failure fixtures produces its line. |

Gates: `pytest && ruff check src/ && mypy src/`. Constraints honored throughout: `Decimal(str())`
at boundaries, per-row `begin_nested()` savepoints, DRY-RUN defaults with `--apply` opt-in,
no row deletion anywhere, additive migration with a real downgrade, secrets via Doppler only.

## 13. Deploy + live smoke (Hetzner, per CLAUDE.md — never hand-edit on the box)

1. Doppler: add the four ALR-003 keys to `accounting/srv` + `accounting/dev`; verify
   `N8N_SEVERITY_WEBHOOK_URL/SECRET` present in `srv`.
2. Mac: merge to main → push → rsync to the box; `ssh root@ubuntu` → `alembic upgrade head`
   (backup runs first: `scripts/backup.sh` takes `data/.backup.lock`).
3. Install updated units: copy `deploy/accounting-balance-alerts.service` (adds `After=`),
   `usermod -aG systemd-journal travis`, `systemctl daemon-reload`.
4. One-time data fix: run the dead-item disconnect script (`--apply`); confirm reconciliation
   endpoint still lists both as disconnected.
5. **Live smoke (REQ-FIX-PLD-006):** `doppler run -- env -u DOPPLER_TOKEN python -m
   scripts.plaid_balance_sync` (DRY-RUN, expect 4 mapped accounts processed, exit 0) → re-run
   `--apply` → verify 4 `plaid_account_balance_snapshot` rows dated today → run
   `scripts.balance_alerts_dispatch --digest` (DRY-RUN) and confirm the pulse renders all four
   without stale markers and the Delivery block is the healthy single line → next timer runs
   observed via `journalctl -u plaid-balance-sync -u accounting-balance-alerts`.
6. Runbook note: the **Jun-25 → fix balance-history gap is permanent** — Plaid cannot backfill
   balances; document in the migration runbook alongside the ≤7d baseline-fallback behavior.

## 14. REQ traceability

| REQ | Spec § | Touches |
|---|---|---|
| REQ-FIX-PLD-001 | §2 | `src/adapters/plaid_balance.py` |
| REQ-FIX-PLD-002 | §3 | `scripts/plaid_balance_sync.py` |
| REQ-FIX-PLD-003 | §4 | `src/balance_alerts/rules.py`, `webhook.py` |
| REQ-FIX-PLD-004 | §5 | `plaid_balance.py`, one-time box script, reconciliation test |
| REQ-FIX-PLD-005 | §6 | `plaid_balance.py`, `expected_account` migration, `seed_expected_accounts`, digest |
| REQ-FIX-PLD-006 | §13.5-6 | runbook + live smoke |
| REQ-FIX-ALR-001 | §7 | `src/alerts/retry.py` (new), both webhook clients |
| REQ-FIX-ALR-002 | §8 | `alr01` migration (`payload_json` + `delivery_channel`), both dispatchers, digest, `policy_drift_dispatch.py` (wealth §11.4), reporting/agent-features Resend emitters (channel='resend_email', excluded from sweep) |
| REQ-FIX-ALR-003 | §9 | `src/alerts/webhook.py`, Doppler `srv`+`dev` |
| REQ-FIX-ALR-004 | §9 | `src/alerts/dispatcher.py` |
| REQ-FIX-ALR-005 | §10 | `src/balance_alerts/digest.py` |
| REQ-FIX-ALR-006 | §11 | `scripts/alert.py`, deploy step |
| REQ-FIX-ALR-007 | §11 | `deploy/accounting-balance-alerts.service` |
| REQ-FIX-ALR-008 | §11 | `src/balance_alerts/rules.py` |
| REQ-DHL-001 | §10 | `digest.py` (delivery-health block) |
| REQ-DHL-002 | §10 | same block — 24h visibility of all four silent-failure modes |

## 15. Out of scope

WBR scorecard (REQ-WBR-*), AR chaser (REQ-ARC-*), tax/invoicing/ingestion/wealth fixes
(REQ-FIX-TAX/INV/ING/WLT/API/DAT/N8N-*), unifying box + CRM pulses, per-account threshold overrides,
and any Plaid Balance product re-purchase.
