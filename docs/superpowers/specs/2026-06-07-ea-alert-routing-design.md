# EA Alert Routing via n8n Webhook — Design Spec

**Date:** 2026-06-07
**Author:** Travis Sparks (with Claude Code)
**Status:** Approved design → ready for implementation plan
**Scope:** New `src/alerts/` module + daily dispatch job + n8n webhook contract

---

## 1. Goal

Send actionable email alerts to **ea-alerts@sparkry.com** (from **Travis@sparkry.com**)
for two recurring obligations Travis keeps missing:

1. **WA state tax (B&O) filings are due** — escalating reminders that begin a couple
   of days after each filing period ends and continue until the due date.
2. **Invoices need to be drafted / submitted** — a monthly "create this month's
   invoices" nudge, plus per-invoice daily reminders that run until the invoice is
   closed out.

Email is sent by an **n8n workflow** (Travis's n8n project owns it). The accounting
system's job is to decide *what* is due, dedupe so nothing sends twice, and POST a
fully-formed, email-ready payload to the n8n webhook. n8n is a thin relay.

---

## 2. Architecture — Push model

```
Hetzner box (Ubuntu)
┌─────────────────────────────────────────────────────────┐
│  systemd timer (daily ~07:00)                            │
│        │                                                 │
│        ▼                                                 │
│  scripts/alerts_dispatch.py  (--apply to send)          │
│        │                                                 │
│        ▼                                                 │
│  src/alerts/                                             │
│   rules.py     → compute active Alerts for `today`      │
│   dispatcher.py→ filter already-sent, POST, record      │
│   webhook.py   → httpx POST (DRY-RUN default)           │
│   models.py    → AlertDispatch ledger (dedup + audit)   │
│        │                                                 │
│        ▼  POST {N8N_ALERTS_WEBHOOK_URL}                  │
└────────┼─────────────────────────────────────────────────┘
         ▼
   n8n webhook  → validate X-Webhook-Secret
                → send email (Travis@sparkry.com → ea-alerts@sparkry.com)
                → return 200
```

**Why push (not poll):** business logic and the "have we already alerted on this?"
state both live in the accounting DB — a single source of truth that survives n8n
restarts and needs no round-trips. The Hetzner box already runs scheduled jobs, so a
daily timer is the natural fit.

**Delivery granularity:** **one email per distinct alert.** Each obligation gets its
own subject line and its own deep link so Amy can act on / forward each independently.
(A daily digest was the considered alternative; rejected to keep each item individually
actionable.)

---

## 3. Components (all new, isolated under `src/alerts/`)

| File | Responsibility | Depends on |
|------|----------------|------------|
| `src/alerts/rules.py` | Pure functions. Given `today` (+ a Session for invoices), return `list[Alert]`. No I/O, no network. | models (read-only) |
| `src/alerts/models.py` | `AlertDispatch` SQLAlchemy model (dedup + audit ledger). | `src/db` |
| `src/alerts/webhook.py` | Thin httpx client → n8n. DRY-RUN by default; `--apply` opts into real POST. | httpx, Doppler env |
| `src/alerts/dispatcher.py` | Orchestrates: compute → filter already-sent-today → POST each → record. Per-alert error isolation. | rules, models, webhook |
| `scripts/alerts_dispatch.py` | CLI entrypoint. DRY-RUN default; `--apply` to send; `--date YYYY-MM-DD` override for testing. | dispatcher |
| `com.sparkry.alerts-dispatch.{service,timer}` | systemd unit + timer (daily ~07:00) on Hetzner. A matching `.plist` is included for macOS parity. | scripts |

### 3.1 `Alert` dataclass (in-memory contract between rules and dispatcher)

```python
@dataclass(frozen=True)
class Alert:
    alert_key: str        # stable idempotency key, e.g. "tax:sparkry:bo:2026-05"
    occurrence_date: str  # ISO date this specific reminder fires (today)
    alert_type: str       # "tax_bo" | "invoice_sweep" | "invoice_draft"
    entity: str           # "sparkry" | "blackline" | "personal" | "all"
    subject: str          # email subject line
    body_text: str        # plain-text email body (rendered, ready to send)
    due_date: str | None  # ISO date the obligation is due (for context/sorting)
    action_url: str       # deep link the recipient clicks to act
    body_html: str | None = None  # optional HTML body; last because it has a default
```

`alert_key` is the *obligation* identity (one per period/invoice). The dedup key for a
single send is `(alert_key, occurrence_date)` — see §5.

---

## 4. Alert rules

### 4.1 WA state tax (B&O) — `alert_type = "tax_bo"`

Derived from the same obligations encoded in `_build_tax_deadlines()`
(`src/api/routes/health.py`). DOR account IDs:

- **Sparkry LLC** — monthly B&O, acct **605-965-107**, due the **25th** of the month
  following the filing period.
- **BlackLine MTB Apparel LLC** — quarterly B&O, acct **605-922-410**, due
  **4/30, 7/31, 10/31, 1/31**.

**Escalation schedule (weekly, begins a couple days after the period closes):**

| Obligation | Reminder fires on | Stops |
|------------|-------------------|-------|
| Sparkry monthly | 3rd, 10th, 17th, 25th of the month | after the 25th (due date) |
| BlackLine quarterly | ~3rd of the due month, then weekly, then the due date | after the due date |

Each `tax_bo` alert email contains, prominently:
- Entity name + **DOR account ID**
- **Filing period** (e.g. "May 2026" for Sparkry; "Q1 (Jan-Mar)" for BlackLine — ASCII
  hyphen in the rendered label for encoding robustness)
- **Due date**
- `action_url`

**Deep-link limitation (must be honored, not papered over):** My DOR
(`https://secure.dor.wa.gov/home/Login`) is an authenticated portal. There is **no
public URL that opens a specific period's return without an active logged-in session.**
Therefore:
- `action_url = "https://secure.dor.wa.gov/home/Login"`
- The account ID **and** exact filing period are placed in the email body so the
  recipient logs in and immediately knows which return to open.

If a working post-login deep-link pattern is later confirmed, it slots into `action_url`
with no other changes. This is the honest current best.

**No "filed" signal:** the DB has no record of a completed B&O filing, so tax reminders
run the fixed escalation schedule and stop at the due date. A future "mark B&O filed"
ack to silence reminders early is **out of scope for v1** (noted in §9).

### 4.2 Invoice monthly sweep — `alert_type = "invoice_sweep"`

- Fires **once, on the last calendar day of each month** (handles 28/29/30/31).
- Subject: "Time to create & submit {Month YYYY} invoices" (e.g. "Time to create &
  submit June 2026 invoices" — month is named in the subject so the EA can tell periods
  apart at a glance).
- Body lists the recurring billers as a checklist (Cardinal Health flat-rate, Fascinate
  calendar-based) so nothing is forgotten.
- `action_url` → invoicing area of the dashboard.
- `alert_key = "invoice:sweep:YYYY-MM"`, single occurrence (no escalation).

### 4.3 Invoice per-draft reminders — `alert_type = "invoice_draft"`

- Source: `Invoice` rows in **`draft`** status (`InvoiceStatus.DRAFT`).
- A reminder fires **daily** for a draft invoice once `today >= reminder_date`, where
  `reminder_date = due_date` (fallback `submitted_date`, fallback `service_period_end`).
- Reminders **stop automatically when the invoice is "closed out"** — i.e. its status
  leaves `draft` (→ `sent` / `paid` / `void`). Status is the self-terminating stop
  signal; no arbitrary cutoff date.
- Subject: "Draft invoice {invoice_number} still needs to be sent".
- Body: customer, entity, total, the reminder_date, days outstanding.
- `action_url` → that invoice's detail page in the dashboard.
- `alert_key = "invoice:draft:{invoice_id}"`; dedup key `(alert_key, occurrence_date)`
  makes the daily cadence idempotent within a day.

> **Interpretation note:** "their due date with daily reminders until it is closed out"
> is read as *draft invoices Travis hasn't sent yet*, reminding daily from the due date
> until the invoice is submitted. This is **submission** chasing, not customer
> **payment** chasing (a separate concern, out of scope here).

---

## 5. Dedup + audit — `AlertDispatch` table

Additive table only. Touches **no** protected table; honors all audit-trail invariants
(nothing dropped, nothing deleted).

```
alert_dispatch
  id               TEXT  PK (uuid)
  alert_key        TEXT  NOT NULL    -- obligation identity
  occurrence_date  TEXT  NOT NULL    -- ISO date of this specific send
  alert_type       TEXT  NOT NULL
  entity           TEXT  NOT NULL
  subject          TEXT  NOT NULL
  status           TEXT  NOT NULL    -- "sent" | "failed" | "dry_run"
  http_status      INT   NULL        -- n8n response code on send
  error_detail     TEXT  NULL
  created_at       TEXT  NOT NULL
  UNIQUE (alert_key, occurrence_date)   -- the dedup guarantee
```

**Dispatch algorithm (`dispatcher.py`):**
1. `alerts = compute_tax_alerts(today) + compute_invoice_alerts(today, session)`
2. For each alert, skip if a row with `(alert_key, occurrence_date)` already exists
   with `status="sent"` (idempotent: running the job twice on the same day sends
   nothing twice).
3. POST to n8n. On `2xx`, insert `AlertDispatch(status="sent", http_status=...)`.
4. On failure (network / non-2xx), insert/record `status="failed"` with `error_detail`,
   **continue to the next alert** (per-alert error isolation — one bad POST never blocks
   the batch). A failed alert is retried on the next daily run because no `"sent"` row
   exists for it.
5. DRY-RUN mode performs steps 1–2, logs what *would* send, records `status="dry_run"`
   (or records nothing — decided in plan), and makes **no** network call.

Alembic migration is additive (`CREATE TABLE alert_dispatch`) with a real `downgrade`
that drops only the new table. Validate with the `alembic-migration` skill.

---

## 6. Webhook contract — deliverable spec for the n8n project

> This section is the hand-off to Travis's n8n team. The accounting side targets exactly
> this contract; n8n implements the receiving workflow.

### 6.1 Endpoint
- **Method:** `POST`
- **URL:** value of Doppler key `N8N_ALERTS_WEBHOOK_URL` (n8n Webhook node, Production URL)
- **Content-Type:** `application/json`

### 6.2 Authentication
- Header **`X-Webhook-Secret`** = Doppler key `N8N_ALERTS_WEBHOOK_SECRET`.
- n8n **must reject** (HTTP 401) any request whose `X-Webhook-Secret` does not match.
  (Mirrors the existing `N8N_WEBHOOK_SECRET` convention used elsewhere in the stack.)
- The secret SHOULD be compared using a constant-time equality function (e.g. `crypto.timingSafeEqual` in Node.js) to prevent timing attacks.
- The secret SHOULD be at least 32 bytes of cryptographically random data.

### 6.3 Request payload (one POST per alert)

```json
{
  "from": "Travis@sparkry.com",
  "to": "ea-alerts@sparkry.com",
  "subject": "WA B&O due — Sparkry LLC (May 2026) by Jun 25",
  "body_text": "Sparkry LLC monthly B&O for May 2026 is due Jun 25, 2026.\nDOR account 605-965-107.\nFile at: https://secure.dor.wa.gov/home/Login",
  "body_html": "<p>…optional…</p>",
  "alert_type": "tax_bo",
  "entity": "sparkry",
  "due_date": "2026-06-25",
  "action_url": "https://secure.dor.wa.gov/home/Login",
  "alert_key": "tax:sparkry:bo:2026-05",
  "occurrence_date": "2026-06-10"
}
```

- `body_text` is **fully rendered and ready to send** — n8n does not template anything;
  it puts `body_text` (and `body_html` if present) straight into the email body and uses
  `from`/`to`/`subject` verbatim.
- `action_url` is also embedded in the body; it is provided as a top-level field in case
  n8n wants to render a button.

### 6.4 n8n workflow responsibilities
1. Validate `X-Webhook-Secret` → 401 on mismatch.
2. Send email **from `Travis@sparkry.com` to `to`** (currently always
   `ea-alerts@sparkry.com`) with the given `subject` and body.
3. Return **HTTP 200** on success (any 2xx accepted; non-2xx is treated as failure and
   retried by the accounting side next day).
4. Idempotency on n8n's side is **optional** — the accounting system already guarantees
   one send per `(alert_key, occurrence_date)`. n8n may log `alert_key` for traceability.
5. (Defense in depth) n8n SHOULD also hardcode/allowlist the recipient and reject unknown
   `to` values — the accounting side enforces an `ALLOWED_TO` set, but a second check in
   n8n prevents any misconfiguration from routing emails to unintended addresses.

### 6.5 Response
- **200** = accepted/sent. Body ignored by the accounting side.
- Any non-2xx (or timeout) = failure → accounting records `status="failed"` and retries
  on the next daily run.

---

## 7. Configuration (Doppler `accounting/dev` + `prd`)

| Key | Value / purpose |
|-----|-----------------|
| `N8N_ALERTS_WEBHOOK_URL` | Production URL of the n8n alerts webhook |
| `N8N_ALERTS_WEBHOOK_SECRET` | Shared secret sent as `X-Webhook-Secret` |
| `ALERT_FROM_EMAIL` | `Travis@sparkry.com` (overridable) |
| `ALERT_TO_EMAIL` | `ea-alerts@sparkry.com` (overridable) |

No `.env` files (project rule). The systemd unit invokes the script under
`doppler run --project accounting --config <env> --`.

---

## 8. Scheduling

- **Primary (Hetzner / Ubuntu):** `com.sparkry.alerts-dispatch.service` (oneshot,
  `doppler run -- python -m scripts.alerts_dispatch --apply`) +
  `com.sparkry.alerts-dispatch.timer` (`OnCalendar=*-*-* 07:00:00`, `Persistent=true`
  so a missed run while the box was down fires on next boot).
- **macOS parity:** a `com.sparkry.alerts-dispatch.plist` LaunchAgent is included for
  any local runs, matching the existing plist family.
- Idempotency (§5) means a catch-up run after downtime will not double-send for any day
  already covered.

---

## 9. Testing (TDD — failing tests first, each REQ-tagged)

| REQ-ID | Behavior |
|--------|----------|
| REQ-ALERT-001 | Sparkry monthly B&O alerts fire on the 3rd/10th/17th/25th and stop after the 25th |
| REQ-ALERT-002 | BlackLine quarterly B&O alerts fire weekly from ~3 days after quarter-end until the due date |
| REQ-ALERT-003 | tax_bo email body contains DOR account ID, filing period, due date, and login action_url |
| REQ-ALERT-004 | Invoice sweep fires exactly once on the last calendar day of the month (incl. Feb 28/29) |
| REQ-ALERT-005 | Draft invoice fires daily from reminder_date and stops the day status leaves `draft` |
| REQ-ALERT-006 | Dedup: running dispatch twice on the same day yields one `sent` row per `(alert_key, occurrence_date)` |
| REQ-ALERT-007 | DRY-RUN is the default; no network call without `--apply` |
| REQ-ALERT-008 | Per-alert error isolation: one failed POST records `failed` and does not block remaining alerts; failed alert retries next run |
| REQ-ALERT-009 | Webhook POST sends correct payload shape + `X-Webhook-Secret` header |
| REQ-ALERT-010 | Alembic migration creates `alert_dispatch` and downgrade drops only it (audit invariants preserved) |

Quality gates before commit: `pytest && ruff check src/ && mypy src/`.

---

## 10. Out of scope (v1)

- "Mark B&O filed" ack to silence tax reminders before the due date (no filing signal
  in the DB today). Tax reminders run the fixed escalation schedule and stop at due date.
- Customer **payment** chasing (overdue-invoice dunning to customers) — distinct from
  the **submission** reminders here.
- Multiple recipients / per-entity routing — `to` is currently always
  `ea-alerts@sparkry.com`; the payload already carries `to` so this is a config change later.
- A true authenticated DOR deep link (blocked on portal capability).

---

## 11. Open items to confirm at plan time

1. Confirm `N8N_ALERTS_WEBHOOK_URL` / `_SECRET` will be provisioned in Doppler before the
   `--apply` cutover (until then the job runs DRY-RUN safely).
2. Confirm the invoicing dashboard URLs used for invoice `action_url` (sweep landing page
   + per-invoice detail route).
3. Confirm whether DRY-RUN should persist `status="dry_run"` audit rows or stay
   side-effect-free (defaults to side-effect-free; decide in plan).
