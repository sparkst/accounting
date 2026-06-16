# Balance Milestone Alerts — Design Spec

**Date:** 2026-06-14
**Author:** Travis Sparks (with Claude Code)
**Status:** Approved design (grilled to convergence) → ready for implementation plan
**Scope:** Replace per-account day-over-day **balance-drift** alerting with **milestone-crossing** alerts across **two systems** — `sparkry-crm` (Cloudflare wealth desk, personal accounts) and `accounting` (Hetzner box, business accounts). Route all alerts through the n8n `UT-Send Alert Message` severity stack; add a daily account-pulse digest.

---

## 1. Goal

Today the only balance alert is **REQ-PS-003 baseline-drift** in `sparkry-crm/src/lib/server/wealth/balance-drift.ts`: after every Plaid balance write it compares to the prior calendar day and emails Travis (via Resend) when the move exceeds `2% OR $100` (cash) / `15% OR $25k` (investment). This is **too noisy** — a large cash account trips the `$100` absolute floor on essentially any movement — and it does not match how Travis actually wants to be warned.

Replace it with **threshold milestones** that map to how the accounts are used:

- **Checking** — warn as the balance *falls through* low-water marks.
- **Savings / other cash** — warn if it drops below a minimum.
- **Credit cards** — warn as the balance *climbs through* ceilings.
- **Investments** — keep big-swing drift (it is the one genuinely useful drift signal).
- **Loan (mortgage)** — silent.

Deliver everything through the existing **n8n severity stack** (`info` / `sev2` / `sev3` → Telegram / Gmail, routing owned by n8n) instead of direct email, plus a **daily account-pulse digest**.

---

## 2. Motivation (from the grill)

1. **Too noisy / too many** — the `OR` trigger + `$100` floor fire constantly on large accounts.
2. **Mute specific accounts** — satisfied by type-driven rules (loan muted; investments kept on drift); no per-account override needed.

---

## 3. Scope — two systems, one model

The accounts Travis thinks of as "my accounts" live in **two separate codebases**. The same milestone model is implemented in both.

### 3.1 `sparkry-crm` (Cloudflare D1/Workers) — has alerting infra today
| Travis's name | Real account | Type | Latest balance |
|---|---|---|---|
| PenFed checking | Access America | checking | $541,835 |
| Chase checking | Sparks Checking | checking | $23,732 |
| (savings) | PenFed Money Market | savings | $6,316 |
| (savings) | PenFed Regular Savings | savings | $16,589 |
| credit | Atmos (BofA), Costco (Citi), Prime Visa (Chase) | credit | $9.3k / $1.0k / $0.7k |
| investment | E-Trade, Schwab Stocks, Schwab AMZN (RSU), Vanguard IRAs/529s | investment | up to $3.07M |
| loan | Chase Mortgage | loan | $41,328 |

### 3.2 `accounting` (Hetzner box, this repo) — **no balance alerting exists**
| Travis's name | Real account | Type | Latest balance |
|---|---|---|---|
| Sparkry | Sparkry checking | checking | $66,318 |
| BlackLine | Blackline checking | checking | $2,015 |
| (business cards) | Blue Business Plus, T. SPARKS | credit | $1.9k / $0.6k |

**Prerequisites for the box half (hard blockers):**
- **(P1)** Re-authenticate the disconnected Chase business Plaid item (`ITEM_LOGIN_REQUIRED`) — same root cause as the daily `plaid-transactions-sync` failure alert.
- **(P2)** Add a daily `plaid_balance_sync` systemd timer. `scripts/plaid_balance_sync.py` has no timer on the box today, so business-account balances are stale (snapshots last written 2026-06-07). Without fresh daily snapshots there is no prior-day baseline to cross.

---

## 4. Alert rules (type-driven, declared in code)

Rules key off `account_type` (D1) / `plaid_account_subtype` (box). There are no per-account special cases except the type-level loan mute.

| Type | Rule |
|---|---|
| **checking** | Downward crossing of milestones **[$10,000, $5,000, $1,000, $0]** |
| **savings / other depository** | Crossing **below $100** |
| **credit** | Upward crossing of **$10,000 and every +$10,000** ($10k, $20k, $30k, …) |
| **investment** | Drift: `\|Δ%\| ≥ 15% AND \|Δ$\| ≥ $25,000` (note: **AND**, tightened from the old `OR`) |
| **loan** | Muted — no alert |

Liability sign handling (credit/loan negation) and scale-2 quantization carry over from the existing drift code.

---

## 5. Re-fire logic (the crossing test)

A milestone fires when **yesterday's balance was on the safe side of level `L` and today's is not** — a day-over-day directional crossing:

- **Checking / savings (downward):** fire when `baseline > L` and `today ≤ L`.
- **Credit (upward):** fire when `baseline < L` and `today ≥ L`.

Where `baseline` = the **prior-calendar-day** snapshot value (the same baseline the drift code already computes; `null` prior day → never fire).

**Dedup:** one alert per `(account_id, level, UTC-day)`. This delivers the agreed behavior exactly:
- No same-day re-dip spam (intra-day login-refresh writes can't re-fire a level already fired today).
- A level only re-fires after a genuine **recovery** (balance back above `L`) **on a later day** — because the crossing test requires `baseline` to be on the safe side, which only happens after recovery.

No new durable state machine is required beyond the per-`(account, level, day)` dedup key.

---

## 6. Severity mapping → n8n `type`

Each fired crossing POSTs to the n8n `UT-Send Alert Message` stack with a `type` field. **n8n owns channel routing** (`info` → Telegram quiet; `sev3` → Telegram; `sev2` → Telegram + Gmail). Severity ordering is standard: **sev2 is more urgent than sev3**.

| Crossing | `type` |
|---|---|
| Checking falls below **$10k**, **$5k** | `info` |
| Checking falls below **$1k** | `sev3` |
| Checking falls below **$0** (overdraft) | `sev2` |
| Savings/other cash below **$100** | `sev3` |
| Credit reaches **$10k** | `info` |
| Credit reaches **$20k, $30k, …** | `sev3` |
| Investment drift (15% AND $25k) | `sev3` |
| Mortgage | — (muted) |

**No direct email.** The existing `balance-drift.ts` `sendAlertEmail(...)` (Resend) call is removed; both systems POST severity-tagged payloads to n8n, which handles Telegram + Gmail.

---

## 7. Daily account-pulse digest

- **Content:** *Full account pulse* — every monitored account with its current balance and a flag on anything currently in a breached state (below a cash floor / above a credit ceiling / drift-flagged).
- **Timing:** **~07:00 PT (14:00 UTC)**, near the existing EA-alerts run.
- **Severity:** `info` (Telegram only).
- **Default aggregation:** each system emits its own pulse (box pulse = business accounts; wealth pulse = personal accounts). Unifying into a single message is a possible follow-up, not required for v1.

---

## 8. Delivery contract (n8n)

POST to the n8n webhook that fronts `UT-Send Alert Message`. Minimum payload:

```json
{
  "type": "sev2|sev3|info",
  "title": "Sparkry checking below $1,000",
  "message": "Sparkry checking fell from $1,240.50 to $812.30, crossing the $1,000 floor.",
  "source": "accounting|wealth",
  "account": "Sparkry checking",
  "balance": "812.30",
  "level": "1000"
}
```

- The Switch node routes on `type.toLowerCase()` (`info` / `sev2` / `sev3` / `sev1` / `jarvis`).
- **Open implementation item:** confirm the n8n *webhook* entrypoint for `UT-Send Alert Message` is reachable from both the Cloudflare Worker and the Hetzner box (the box already holds `N8N_ALERTS_WEBHOOK_URL/SECRET` for EA alerts — verify whether the same or a distinct webhook fronts this stack), and that the secret is provisioned in `accounting/srv` and the Worker's secrets.

---

## 9. Implementation footprint

### 9.1 `sparkry-crm` (personal accounts)
- Rewrite `src/lib/server/wealth/balance-drift.ts`:
  - Add the milestone engine (checking/savings/credit) keyed by `account_type`.
  - Keep investment drift but switch `OR` → `AND` at `15% / $25k`.
  - Replace `sendAlertEmail(...)` with an n8n webhook POST carrying `type` (severity).
  - Dedup key `milestone:{account_id}:{level}:{utcDate}` (reuse `WEALTH_KV`).
- Add the daily pulse (extend the existing nightly/cron Worker; emit `info` digest at 14:00 UTC).

### 9.2 `accounting` (business accounts, this repo)
- Resolve **P1** (Chase re-auth) and **P2** (daily `plaid_balance_sync` timer) first.
- New milestone-alert module (mirror the `src/alerts/` pattern: pure rule functions + a dispatcher that POSTs to n8n, DRY-RUN by default, `--apply` to send).
- Daily pulse for business accounts via a systemd timer (alongside `accounting-ea-alerts.timer`).

### 9.3 Shared
- Single declarative rule config (same constants in both repos): checking `[10000,5000,1000,0]`, savings floor `100`, credit step `10000`, investment drift `{pct:15, abs:25000}`, loan `muted`, and the severity map of §6.

---

## 10. REQ-IDs (proposed — to add to `requirements/current.md`)

| REQ | Acceptance |
|---|---|
| REQ-BAL-001 | Checking accounts alert on downward crossing of [$10k,$5k,$1k,$0]; severity per §6. |
| REQ-BAL-002 | Savings/other depository alert on crossing below $100 (`sev3`). |
| REQ-BAL-003 | Credit accounts alert on upward crossing of $10k and every +$10k (`info` at $10k, `sev3` above). |
| REQ-BAL-004 | Investment accounts keep drift, tightened to `15% AND $25k`; loan accounts are muted. |
| REQ-BAL-005 | A milestone fires only on a day-over-day directional crossing vs the prior-day baseline; null baseline never fires. |
| REQ-BAL-006 | Dedup: one alert per `(account_id, level, UTC-day)`; re-fire requires recovery on a later day. |
| REQ-BAL-007 | Alerts POST severity-tagged payloads to the n8n `UT-Send Alert Message` stack; no direct email send. |
| REQ-BAL-008 | Daily `info` account-pulse digest at 14:00 UTC lists every monitored account, balance, and breach flags. |
| REQ-BAL-009 | Box prerequisites met: Chase business item re-authed; daily `plaid_balance_sync` timer installed and writing snapshots. |
| REQ-BAL-010 | DRY-RUN default for the box dispatcher; `--apply` opts into POSTing; per-alert error isolation. |

---

## 11. Out of scope (v1)

- Unifying the two daily pulses into one message.
- A dashboard UI to edit thresholds (config stays in code; type-driven).
- Per-account threshold overrides / mute list (type rules cover all current accounts).
- Changing the investment drift model beyond the `OR → AND` tightening.
