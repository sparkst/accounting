# Plaid Net-Worth Integration — Design Spec

**Date:** 2026-05-09
**Owner:** Travis Sparks (single-user system)
**Status:** Phase 1 ready to implement (advisor-reviewed, all P0/P1 findings addressed); Phase 2 deferred to a clean session.

---

## Why this spec exists

The accounting system already ingests bank/brokerage/card data via CSV/XLSX/PDF importers and Phase 3 brokerage features (live re-pricing via yfinance, historical price backfill, account balance snapshots). That work is canonical for tax-lot fidelity and cost-basis tracking.

This spec adds **Plaid as a parallel, automated feed** for daily balance snapshots and (in Phase 2) per-position holdings + dividend transactions. Plaid does not replace existing importers — it runs alongside as a reconciliation source and a way to keep net worth current without manual XLSX drops.

## Constraints

- **Plaid Item cap: 10.** One Item = one institution login. Each Item can hold many accounts. (NOTE: "unlimited API calls" was the contract framing; in practice `Balance` is **per-call billed** in production — not free under the Item cap. Daily polling on 10 Items ≈ 300 billed Balance calls/month. See "Plaid products enabled per Item" below.)
- **Single-user system.** Travis is the only end-user. No multi-tenancy.
- **Tailscale-only deployment.** Dashboard at `https://macbook.ancon-cliff.ts.net`. No public webhook ingress without a Cloudflare tunnel — both phases use polling, not webhooks.
- **Cannot connect via Plaid:** F&G annuity, GSK pension, NW Mutual (whole-life). Continue manual PDF/XLSX importers for these.

## Allocated slots (10 Items)

| # | Institution | Type | Phase 2 eligible (Investments)? |
|---|---|---|---|
| 1 | Vanguard #1 | brokerage | yes |
| 2 | Vanguard #2 | brokerage | yes |
| 3 | Schwab | brokerage | yes |
| 4 | Fidelity | brokerage | yes |
| 5 | E*TRADE | brokerage | yes |
| 6 | Franklin Templeton | brokerage | yes (verify Plaid coverage in sandbox first) |
| 7 | Chase | bank/depository | no |
| 8 | PenFed | bank/depository | no |
| 9 | Bank of America | card-only | no |
| 10 | Citibank | card-only | no |

**Cut from earlier draft:** Amex (cards over-represented), NW Mutual (no Plaid coverage).

Slots are reversible via `/item/remove` — disconnect frees the slot immediately.

## Existing system touchpoints

- **`Account` table** (`src/models/brokerage.py`): canonical account record. Plaid accounts must map to existing `Account` rows by (institution, mask) at link time.
- **`AccountBalanceSnapshot` table** (`src/models/history.py`): one row per account per day. Currently fed by XLSX historical import + Phase 3 forward-fill. Phase 1 leaves this table untouched — Plaid balances land in the sibling `plaid_account_balance_snapshot` table; reconciliation joins the two.
- **`expected_account` table** (`src/models/history.py`): manually-curated coverage list. Plaid-discovered accounts that don't match an `expected_account` should surface in the missing-accounts panel for explicit confirmation.
- **`/brokerage` page** computes account totals as `position × latest_yfinance_price`. Plaid Balance returns the **broker-reported** total. The two will disagree by small amounts (different price sources, intraday timing). Reconciliation = comparing them, not picking one.
- **Doppler** holds secrets. Plaid client_id and secret go there as `PLAID_CLIENT_ID` and `PLAID_SECRET` (sandbox + production environments separately).

---

# Phase 1 — Balance-only

**Goal:** Daily Plaid Balance pulls for all 10 Items, written alongside existing snapshots as a reconciliation source.

## Plaid products enabled per Item

Plaid's `Balance` product is **not** a guaranteed standalone primary product across institutions. For OAuth banks (Chase, BofA, Vanguard, Fidelity, Schwab) and many others, `/accounts/balance/get` requires that another primary product was included at link time, otherwise the API returns `PRODUCT_NOT_READY` or `PRODUCTS_NOT_SUPPORTED`. Sandbox does not always replicate this restriction — the failure typically only appears in production.

**Link-token configuration:**

```python
LinkTokenCreateRequest(
    products=[Products("transactions")],                           # primary product for link eligibility
    required_if_supported_products=[Products("balance")],          # request Balance where the institution supports it
    additional_consented_products=[Products("investments")],       # Phase 2 brokerages — included in consent now to avoid a re-link later
    ...
)
```

`required_if_supported_products` is the right knob: Balance is requested at every institution that supports it, without requiring institutions that don't (this is what causes the `PRODUCTS_NOT_SUPPORTED` failure when listed under `products`). Including `investments` in `additional_consented_products` at Phase 1 link time means Phase 2 doesn't need a re-consent flow on the same Items.

**What Phase 1 actually calls:** `/accounts/balance/get` only. We don't pull Transactions even though it's listed as the primary product — listing it grants consent and link-eligibility, calling it is what bills you. Plaid's Balance product is **per-call billed** (not "unlimited") — daily polling on 10 Items ≈ 300 billed calls/month. Confirm pricing tier with Plaid before flipping production.

## Schema additions

All schema changes ship as **Alembic migrations**, never raw SQL. Phase 1 needs one revision (`add_plaid_item_and_balance_snapshot`); Phase 2 needs a second (`add_plaid_investments_staging`). Each must include a real `downgrade()` that drops the added tables/columns in reverse FK order, per the project's alembic-migration skill conventions.

`plaid_item.id` and FK columns referencing `account.id` use `String(36)` UUID to match the existing `Account` model (`src/models/brokerage.py:79` — `id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)`). Using `INTEGER` would break FK integrity at insert time.

```python
# Conceptual SQLAlchemy models (Alembic op.create_table calls in the actual revision)

class PlaidItem(Base):
    __tablename__ = "plaid_item"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    item_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)               # Plaid's item_id
    institution_id: Mapped[str] = mapped_column(String, nullable=False)                     # e.g., "ins_3"
    institution_name: Mapped[str] = mapped_column(String, nullable=False)                   # "Chase", "Vanguard", etc.
    access_token_encrypted: Mapped[str] = mapped_column(String, nullable=False)             # Fernet ciphertext, or "REVOKED" sentinel after disconnect
    cursor: Mapped[str | None] = mapped_column(String, nullable=True)                       # reserved (unused for Balance/Investments; only used if /transactions/sync is ever adopted)
    last_sync_at: Mapped[datetime | None]
    last_sync_status: Mapped[str | None]                                                    # 'ok' | 'error' | 'pending'
    last_error: Mapped[str | None]                                                          # error_code only, never full body
    last_investments_txn_date: Mapped[date | None]                                          # Phase 2: cursor-equivalent for investment_transactions offset window
    consent_expiration_at: Mapped[datetime | None]
    state_nonce: Mapped[str | None]                                                         # short-lived OAuth state, cleared after exchange
    state_nonce_expires_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default='active')           # 'active' | 'disconnected'

# Existing Account table gets two new columns (ADD COLUMN, idempotent)
# account.plaid_item_id   String(36) NULL  REFERENCES plaid_item(id)
# account.plaid_account_id String     NULL  -- Plaid's per-account id (opaque)

class PlaidAccountBalanceSnapshot(Base):
    """Sibling table — does NOT mix with the existing account_balance_snapshot."""
    __tablename__ = "plaid_account_balance_snapshot"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("account.id"), nullable=False)
    snapshot_date: Mapped[date] = mapped_column(nullable=False)
    plaid_account_type: Mapped[str] = mapped_column(String, nullable=False)                 # 'depository'|'credit'|'investment'|'loan' — drives sign convention
    plaid_account_subtype: Mapped[str | None] = mapped_column(String, nullable=True)        # 'checking'|'credit card'|'cd'|...
    current_balance: Mapped[Decimal] = mapped_column(Numeric, nullable=False)               # Plaid's balances.current AS-RETURNED (positive for credit cards = debt; net-worth queries must negate by type)
    available_balance: Mapped[Decimal | None]
    iso_currency_code: Mapped[str | None]
    pulled_at: Mapped[datetime] = mapped_column(nullable=False)
    raw_data: Mapped[dict] = mapped_column(JSON, nullable=False)
    __table_args__ = (UniqueConstraint("account_id", "snapshot_date"),)
```

**Sign convention for `plaid_account_balance_snapshot.current_balance`:** the column stores Plaid's value as-returned. Plaid returns `current` as a positive number for credit cards (amount owed = liability) and for loans (principal outstanding). Any net-worth aggregation must negate the value when `plaid_account_type IN ('credit', 'loan')`. This is documented here so the implementer doesn't normalize at write time and lose round-trippability with `raw_data`.

**Encryption:** access_token encrypted via Doppler-held key (`PLAID_TOKEN_ENC_KEY`) using `cryptography.fernet`. Tokens never logged. On `/item/remove`, `access_token_encrypted` is overwritten with the sentinel `"REVOKED"` so the ciphertext does not linger in SQLite freed pages or backups. Key rotation handled by `scripts/rotate_plaid_key.py` using `cryptography.fernet.MultiFernet` for the in-flight window (old key + new key, re-encrypt all rows in a transaction, then drop old key from Doppler).

## UI

New SvelteKit route: `/admin/connections`

- **List view:** all `plaid_item` rows. Per row: institution name, # of accounts mapped, last_sync_at, status badge, "Disconnect" button.
- **Add Item flow (with CSRF/state binding):**
  1. Click "Add connection" → backend `POST /api/plaid/link-token` (auth-required). Server generates a `state_nonce` (cryptographically random, 32 bytes), stores it on a placeholder `plaid_item` row (or in a short-lived session cache) with a 30-minute TTL matching Plaid's link_token TTL. Server calls `/link/token/create` with the products config above and returns `{link_token, state_nonce}` to the client.
  2. Frontend opens Plaid Link modal (Plaid Link Web JS — there is no Svelte binding; use the vanilla JS embed loaded from `cdn.plaid.com`; CSP must allow that origin). Pass the `state_nonce` through Plaid's `state` parameter.
  3. Plaid Link `onSuccess(public_token, metadata)` → `POST /api/plaid/exchange` (auth-required) with `{public_token, state_nonce}`. **Server rejects the call if `state_nonce` doesn't match a stored, unexpired nonce** — this prevents an attacker from posting a forged `public_token` to the exchange endpoint. On success, exchange the public_token for `access_token` + `item_id`, persist the `plaid_item` row (encrypting access_token), clear the `state_nonce`.
  4. Backend immediately calls `/accounts/get` to enumerate accounts under the Item.
  5. UI shows mapping screen: each Plaid account (name + mask + type) gets a dropdown to map to an existing `Account` row, or "Create new Account."
  6. On confirm, populate `account.plaid_item_id` and `account.plaid_account_id`. Write an `AuditEvent` per mapped account (see Operational completeness below).
  7. Plaid Link `onExit(err, metadata)` handler must surface Plaid's error object to the UI (don't silently swallow). Common cases: user cancels, link_token expired, institution unsupported.
- **Disconnect flow:** confirm dialog → `POST /api/plaid/disconnect/{item_id}` (auth-required) → calls `/item/remove`, **then overwrites `access_token_encrypted = "REVOKED"`** so the ciphertext does not linger in SQLite freed pages or `.wal` snapshots, sets `plaid_item.status='disconnected'`, nulls FK on associated `account` rows, writes an `AuditEvent`. Slot freed.
- **Re-link flow (ITEM_LOGIN_REQUIRED recovery):** when an Item enters `ITEM_LOGIN_REQUIRED`, the connections page surfaces a "Re-link" button. Click → `POST /api/plaid/relink/{item_id}` generates a Link token in **update mode** (pass the existing `access_token` to `/link/token/create`). User completes Plaid Link flow; Plaid keeps the same `item_id` and refreshes the credentials. No new `plaid_item` row created.

**OAuth (mandatory for Phase 1):** Chase, BofA, Vanguard, Fidelity, Schwab, and Citi all require OAuth — at least 7 of the 10 Items. Plaid's OAuth flow requires a `redirect_uri` that is **publicly reachable from Plaid's infrastructure**, not just from Travis's network. Tailscale-only domains are not reachable to Plaid; this is a guaranteed failure without a public path. There is **no fallback** — the link flow hard-fails if the redirect_uri is unreachable.

**Cloudflare tunnel is a Phase 1 prerequisite, not a fallback.** Set up a Cloudflare tunnel exposing only `https://accounting-plaid.<chosen-subdomain>/admin/connections/oauth-return` to the public internet. The rest of the dashboard stays Tailscale-only. Register that single URL in the Plaid dashboard's allowed redirect_uris before any link attempt. Treat tunnel setup as part of Phase 1 acceptance.

## Sync flow

New cron via launchd: `com.sparkry.plaid-balance-sync.plist`, daily at ~2am. Idempotent on double-run (manual trigger via `/api/plaid/sync-now` is safe).

**Three layers of error isolation:** outer per-Item try/except, inner per-account try/except, and per-row `session.begin_nested()` savepoint per the project canon (CLAUDE.md "Per-row savepoint for batch ingest"). One bad row never halts an account; one bad account never halts an Item; one bad Item never halts the batch.

**Retry classification:**

| Plaid `error_code` | retryable | Treatment |
|---|---|---|
| `RATE_LIMIT_EXCEEDED` | yes | Exponential backoff, 3 attempts (1s, 5s, 30s) |
| `INTERNAL_SERVER_ERROR`, `PLANNED_MAINTENANCE` | yes | Same backoff |
| `INSTITUTION_DOWN`, `INSTITUTION_NOT_RESPONDING` | yes | Same backoff; if still failing, mark `last_sync_status='institution_down'` (not 'error') |
| `ITEM_LOGIN_REQUIRED`, `INVALID_CREDENTIALS`, `ITEM_LOCKED` | **no — terminal** | Mark `last_sync_status='error'`, `last_error=<code>`, surface re-link prompt |
| `INVALID_ACCESS_TOKEN`, `ACCESS_NOT_GRANTED` | **no — terminal** | Same; user must re-link |
| `PRODUCT_NOT_READY` | yes (short window) | Wait 30s and retry once; if still failing, defer to next cron run |
| Any other | no | Fail-safe: mark error, log, continue |

```python
# scripts/plaid_balance_sync.py (sketch)
def sync_one_item(item: PlaidItem, run_log: IngestionLog) -> None:
    item_log = IngestionLog(
        source=f"plaid_balance:{item.institution_name}",
        started_at=utcnow(),
        run_id=run_log.id,
    )
    db.add(item_log)
    db.commit()
    try:
        access_token = decrypt(item.access_token_encrypted)
        resp = call_with_retry(
            lambda: plaid_client.accounts_balance_get(access_token=access_token),
            classify=classify_plaid_error,
        )
        accounts_processed = 0
        accounts_failed = 0
        accounts_skipped_unmapped = 0
        for plaid_account in resp.accounts:
            try:
                with db.begin_nested():            # per-row savepoint
                    account = db.query(Account).filter_by(
                        plaid_item_id=item.id,
                        plaid_account_id=plaid_account.account_id,
                    ).first()
                    if not account:
                        # surface in missing-accounts panel via ExpectedAccount with status='unconfirmed'
                        upsert_expected_account_from_plaid(item, plaid_account)
                        accounts_skipped_unmapped += 1
                        continue
                    snap = PlaidAccountBalanceSnapshot(
                        account_id=account.id,
                        snapshot_date=date.today(),
                        plaid_account_type=plaid_account.type,
                        plaid_account_subtype=plaid_account.subtype,
                        current_balance=Decimal(str(plaid_account.balances.current))
                            if plaid_account.balances.current is not None else None,
                        available_balance=(Decimal(str(plaid_account.balances.available))
                                           if plaid_account.balances.available is not None else None),
                        iso_currency_code=plaid_account.balances.iso_currency_code,
                        pulled_at=utcnow(),
                        raw_data=plaid_account.to_dict(),
                    )
                    if snap.iso_currency_code and snap.iso_currency_code != 'USD':
                        log.warning("non-USD account skipped", extra={...})
                        accounts_skipped_unmapped += 1
                        continue
                    if snap.current_balance is None:
                        accounts_failed += 1
                        continue
                    # On double-run, UNIQUE(account_id, snapshot_date) raises IntegrityError;
                    # the savepoint absorbs it and we move on.
                    db.add(snap)
                accounts_processed += 1
            except IntegrityError:
                # idempotent: today's snapshot already exists for this account
                accounts_processed += 1
                continue
            except Exception:
                accounts_failed += 1
                log.exception("per-account failure", extra={"item": item.id, "plaid_account": plaid_account.account_id})
        item.last_sync_at = utcnow()
        item.last_sync_status = 'ok'
        item.last_error = None
        item_log.status = 'ok'
        item_log.records_processed = accounts_processed
        item_log.records_failed = accounts_failed
        item_log.notes = f"unmapped_skipped={accounts_skipped_unmapped}"
    except TerminalPlaidError as e:
        item.last_sync_status = 'error'
        item.last_error = e.error_code
        item_log.status = 'error'
        item_log.retryable = False
        item_log.error_detail = e.error_code
    except RetryablePlaidError as e:
        item.last_sync_status = 'error'
        item.last_error = e.error_code
        item_log.status = 'error'
        item_log.retryable = True
        item_log.error_detail = e.error_code
    finally:
        item_log.completed_at = utcnow()
        db.commit()

# Driver
run_log = IngestionLog(source='plaid_balance', started_at=utcnow())
db.add(run_log); db.commit()
for item in db.query(PlaidItem).filter_by(status='active'):
    sync_one_item(item, run_log)
run_log.status = 'ok'
run_log.completed_at = utcnow()
db.commit()
```

**Stale-Item alerting (no webhooks):** the existing weekly P&L email (`com.sparkry.weekly-pl-report.plist`) gains a section listing any active `plaid_item` rows where `last_sync_status='error'` and `last_error IN ('ITEM_LOGIN_REQUIRED', 'INVALID_CREDENTIALS', 'ITEM_LOCKED')`. The Health Dashboard staleness check (REQ-015) reads `plaid_item.last_sync_at` and surfaces a warning if any active Item hasn't synced in >48h. Without this, balances silently go stale and the user has to remember to check `/admin/connections`.

**AuditEvent integration.** The existing `AuditEvent` table has a non-nullable `transaction_id` FK (`src/models/audit_event.py`), so it can't directly host Plaid lifecycle events. Phase 1 ships an Alembic revision that **adds nullable `entity_id` (TEXT) and `entity_type` (TEXT) columns** to `audit_event` and relaxes `transaction_id` to nullable, with a CHECK constraint that exactly one of `(transaction_id, entity_id)` is non-null. AuditEvents written for: Plaid Item connected, account mapped, account unmapped, Item disconnected, Item re-linked. Field-level old/new values per the existing audit pattern.

## API endpoints (backend)

**All `/api/plaid/*` endpoints require `Depends(get_current_user)`** consistent with every other admin route in the project. Even though the dashboard is Tailscale-gated, in-network actors (other devices on the tailnet, the user's phone) must not be able to install or modify Items without authentication.

```
POST /api/plaid/link-token                    -> {link_token, state_nonce}                   (auth)
POST /api/plaid/exchange                      -> {item_id, accounts: [...]}                  (auth, validates state_nonce)
POST /api/plaid/map-accounts                  -> {} (writes plaid_account_id mappings)        (auth, writes AuditEvent)
POST /api/plaid/disconnect/{item_id}          -> {} (calls /item/remove, zeros token)         (auth, writes AuditEvent)
POST /api/plaid/relink/{item_id}              -> {link_token}                                (auth, update-mode link)
GET  /api/plaid/items                         -> [{item, accounts, last_sync_at, status}]    (auth)
POST /api/plaid/sync-now                      -> {} (manual trigger, calls the cron logic)   (auth, rate-limited 1/min/Item)
GET  /api/plaid/reconciliation/summary        -> [{account_id, snapshot_date, plaid_total, computed_total, delta, delta_pct, exceeds_threshold}]  (auth)
```

## Production approval

Plaid requires a production application before live institution data flows. Sandbox is unlimited and free. Production is per-Item per-product per-month. Apply when Phase 1 sandbox testing is green.

Submission needs: app description, end-user disclosure (single-user personal app), privacy policy URL (or attestation that the app is for the developer's personal use). Approval typically 1-3 days.

## What Phase 1 does NOT do

- No transactions ingestion (Plaid `Transactions` product).
- No per-symbol holdings (Plaid `Investments` product). The brokerage page still pulls positions from existing CSV/XLSX importers.
- No webhooks. No real-time. Daily batch only.
- No replacement of existing CSV/XLSX flows. Plaid runs alongside.

## Requirements (REQ-IDs)

These extend `requirements/current.md`. Each test references the REQ-ID per project TDD convention.

- **REQ-025 — Plaid Item lifecycle:** connect (link → exchange → persist with encrypted token), map accounts, disconnect (calls `/item/remove`, zeros encrypted token, frees slot), re-link in update mode for ITEM_LOGIN_REQUIRED. CSRF/state nonce validated on exchange.
- **REQ-026 — Plaid Balance daily sync:** for every active Item, pull `/accounts/balance/get`, write `plaid_account_balance_snapshot` rows for mapped accounts, write `IngestionLog` row per Item per run, classify Plaid errors as retryable/terminal, idempotent on double-run via per-row savepoint absorbing UNIQUE-constraint conflicts.
- **REQ-027 — Plaid stale-Item alerting:** weekly P&L email surfaces active Items with terminal-error `last_sync_status`; Health Dashboard surfaces Items not synced in >48h.
- **REQ-028 — Plaid balance reconciliation surface:** `GET /api/plaid/reconciliation/summary` returns per-account delta between `plaid_account_balance_snapshot.current_balance` and the existing computed `position × yfinance` total. Tolerance threshold flag: `exceeds_threshold = (abs(delta_pct) > 2.0) OR (abs(delta) > 100.00)`.
- **REQ-029 — Plaid AuditEvent extension:** `audit_event` table extended with nullable `entity_id`, `entity_type` columns and CHECK constraint enforcing exactly-one-of (`transaction_id`, `entity_id`). All Plaid lifecycle actions write AuditEvents.

## Tests

- `src/adapters/test_plaid_balance_sync.py` — covers REQ-026: successful sync, unmapped account skipped (with `ExpectedAccount` upsert), double-run idempotency, ApiException isolation per account and per Item, retryable vs terminal error classification, `last_sync_at`/`last_sync_status`/`last_error` correct after each branch, IngestionLog row written.
- `src/api/test_plaid_routes.py` — covers REQ-025, REQ-028: state nonce required on exchange, auth required on every endpoint, disconnect zeros encrypted token, reconciliation summary returns expected shape and threshold flag.
- Plaid sandbox JSON fixtures committed under `src/adapters/fixtures/plaid/` — captured from `plaid-python` sandbox responses. At minimum: depository, credit card, brokerage, error responses (ITEM_LOGIN_REQUIRED, RATE_LIMIT_EXCEEDED, INSTITUTION_DOWN). Tests use these fixtures via a Plaid client mock; no live API calls in CI.

## Acceptance criteria for Phase 1

1. All 10 Items connected via `/admin/connections` UI in sandbox; OAuth banks complete the link flow through the Cloudflare-tunnel-exposed `/admin/connections/oauth-return` URL.
2. Daily cron writes `plaid_account_balance_snapshot` rows for every mapped account, with `plaid_account_type` populated so net-worth aggregation can negate credit/loan balances.
3. Disconnect button removes the Item, overwrites the encrypted access_token with `"REVOKED"`, and frees the slot (verified by re-counting connected Items).
4. Errored Items surface in the UI with the Plaid `error_code`; ITEM_LOGIN_REQUIRED Items show a "Re-link" button that completes the update-mode flow without creating a new Item.
5. `GET /api/plaid/reconciliation/summary` returns per-account deltas; the tolerance threshold flag separates "drift within expected band" from "needs attention."
6. Weekly P&L email includes a Plaid stale-Item section.
7. CSRF/state nonce validation rejects forged `/api/plaid/exchange` calls (test asserts 4xx on missing or stale nonce).
8. All `/api/plaid/*` endpoints reject unauthenticated requests (test asserts 401 with no session).
9. Cloudflare tunnel exposing only `/admin/connections/oauth-return` is configured and registered in the Plaid dashboard.
10. Production application submitted and approved.

---

# Phase 2 — Investments (deferred, clean-session pickup)

**Trigger:** Phase 1 in production for ≥2 weeks with stable balance pulls and reconciliation queries running.

## Why a separate phase

Investments is a different cost tier (~2-3x Balance per-Item per-month in production), introduces transaction-level dedup against existing brokerage CSV ingestion, and has a real engineering question around which source becomes canonical for which fields. Worth doing only after Phase 1 plumbing is trusted.

## Goal

Add Plaid `Investments` product to the 6 brokerage Items (Vanguard ×2, Schwab, Fidelity, E*TRADE, Franklin Templeton). Pull holdings + investment_transactions daily. Land them in **staging tables** for reconciliation against existing `BrokeragePosition` and `BrokerageTransaction` rows. Surface reconciliation results in the dashboard.

## Plaid endpoints used

- `/investments/holdings/get` — per-symbol qty, cost basis, institution price. Single call, no pagination.
- `/investments/transactions/get` — buys, sells, dividends, cash, transfers, fees. **Offset-based pagination** (NOT cursor-based — that's `/transactions/sync` for the depository Transactions product, which we don't use). Loop with `offset` + `count` (max 500/page) until `len(fetched) >= response.total_investment_transactions`. Backfill window is "up to 24 months" but **varies by institution**; record the actual earliest transaction date per Item on first sync and surface in `/admin/connections` so per-institution coverage is visible.
- `/investments/refresh` (on-demand) — triggers a fresh extract. **Rate-limited to once per 30 minutes per Item** by Plaid; calling more frequently returns `PRODUCT_NOT_READY`. Server-side cooldown guard on the "Refresh now" button: track last refresh timestamp per Item, return 429 with countdown if within window, disable the UI button + show countdown.

## Schema additions

FK columns use `String(36)` to match the Account/PlaidItem UUID convention. Staging tables ship as a Phase 2 Alembic revision (`add_plaid_investments_staging`) with a real `downgrade()` dropping all three tables in reverse FK order.

```sql
-- Staging table for Plaid holdings (NOT the source of truth)
CREATE TABLE plaid_holdings_staging (
    id TEXT PRIMARY KEY,                                          -- UUID String(36)
    plaid_item_id TEXT NOT NULL REFERENCES plaid_item(id),
    plaid_account_id TEXT NOT NULL,
    snapshot_date DATE NOT NULL,
    security_id TEXT NOT NULL,        -- Plaid's security_id
    ticker_symbol TEXT,
    cusip TEXT,
    isin TEXT,
    name TEXT,
    quantity DECIMAL NOT NULL,
    cost_basis DECIMAL,                -- nullable; broker-reported, best-effort
    institution_price DECIMAL,
    institution_value DECIMAL,
    iso_currency_code TEXT,
    pulled_at DATETIME NOT NULL,
    UNIQUE(plaid_item_id, plaid_account_id, security_id, snapshot_date)
);

-- Staging table for Plaid investment transactions
CREATE TABLE plaid_investment_transactions_staging (
    id TEXT PRIMARY KEY,                                          -- UUID String(36)
    plaid_item_id TEXT NOT NULL REFERENCES plaid_item(id),
    plaid_account_id TEXT NOT NULL,
    investment_transaction_id TEXT NOT NULL UNIQUE,  -- Plaid's id, idempotent
    security_id TEXT,
    ticker_symbol TEXT,
    transaction_date DATE NOT NULL,
    transaction_type TEXT NOT NULL,    -- 'buy'|'sell'|'cash'|'transfer'|'fee'
    transaction_subtype TEXT,          -- 'dividend'|'interest'|... (rich detail)
    quantity DECIMAL,
    price DECIMAL,
    fees DECIMAL,
    amount DECIMAL NOT NULL,           -- signed; positive = inflow, negative = outflow
    iso_currency_code TEXT,
    raw_data JSON NOT NULL,            -- preserve full Plaid response
    pulled_at DATETIME NOT NULL,
    matched_brokerage_transaction_id TEXT REFERENCES brokerage_transaction(id),
    match_status TEXT NOT NULL DEFAULT 'unmatched'  -- 'unmatched'|'matched'|'plaid_only'|'csv_only'
);
-- amount sign convention: stored AS-RETURNED by Plaid, which is the OPPOSITE of this system's convention.
-- Plaid: positive = outflow (buy/fee/cash-out), negative = inflow (sell/dividend/cash-in).
-- This system's BrokerageTransaction: positive = inflow, negative = outflow.
-- Reconciliation must NEGATE Plaid's amount before comparing, OR compare on abs(amount) for magnitude only.
-- Do not normalize at write time — keep round-trippable with raw_data.

-- Reconciliation summary (materialized periodically, not source-of-truth)
CREATE TABLE plaid_reconciliation_log (
    id TEXT PRIMARY KEY,                                          -- UUID String(36)
    run_at DATETIME NOT NULL,
    plaid_account_id TEXT NOT NULL,
    snapshot_date DATE NOT NULL,
    plaid_total DECIMAL,
    computed_total DECIMAL,            -- existing position × yfinance
    delta DECIMAL,
    delta_pct DECIMAL,
    holdings_match_count INTEGER,
    holdings_plaid_only_count INTEGER,
    holdings_csv_only_count INTEGER,
    transactions_unmatched_count INTEGER,
    notes TEXT
);
```

## Sync flow

Extends the Phase 1 cron with a second pass for Investments-eligible Items:

```python
# scripts/plaid_investments_sync.py (sketch)
PAGE_SIZE = 500    # Plaid /investments/transactions/get max

for item in db.query(PlaidItem).filter_by(status='active'):
    if not item_has_investments_product(item):
        continue
    item_log = IngestionLog(source=f"plaid_investments:{item.institution_name}", started_at=utcnow())
    db.add(item_log); db.commit()
    try:
        access_token = decrypt(item.access_token_encrypted)

        # Holdings: full snapshot per call, no pagination needed.
        # Idempotent on UNIQUE(plaid_item_id, plaid_account_id, security_id, snapshot_date).
        h = call_with_retry(lambda: plaid_client.investments_holdings_get(access_token=access_token), classify_plaid_error)
        for holding in h.holdings:
            with db.begin_nested():
                upsert_plaid_holdings_staging(item, holding, snapshot_date=date.today())

        # Transactions: OFFSET-based pagination (NOT cursor — that's /transactions/sync).
        # Window goes back at most 730 days, but actual coverage varies by institution.
        # First-run for an Item: pull from 730d ago to today.
        # Subsequent runs: pull from item.last_investments_txn_date - 7d to today (7d overlap covers late-posting txns).
        if item.last_investments_txn_date is None:
            start_date = date.today() - timedelta(days=730)
        else:
            start_date = item.last_investments_txn_date - timedelta(days=7)
        end_date = date.today()

        offset = 0
        earliest_returned = None
        while True:
            t = call_with_retry(
                lambda: plaid_client.investments_transactions_get(
                    access_token=access_token,
                    start_date=start_date,
                    end_date=end_date,
                    options={'count': PAGE_SIZE, 'offset': offset},
                ),
                classify_plaid_error,
            )
            for txn in t.investment_transactions:
                with db.begin_nested():
                    # idempotent on investment_transaction_id UNIQUE
                    insert_or_skip_plaid_investment_transactions_staging(item, txn)
                if earliest_returned is None or txn.date < earliest_returned:
                    earliest_returned = txn.date
            offset += len(t.investment_transactions)
            if offset >= t.total_investment_transactions:
                break

        item.last_investments_txn_date = end_date
        # On first sync per Item, record actual backfill depth for /admin/connections display
        if item.first_investments_txn_date is None:
            item.first_investments_txn_date = earliest_returned
        item_log.status = 'ok'
    except Exception as e:
        item_log.status = 'error'
        item_log.error_detail = str(e)
        log.exception("plaid investments sync failed", extra={"item": item.id})
    finally:
        item_log.completed_at = utcnow()
        db.commit()

# Reconciliation pass (runs after sync). Writes plaid_reconciliation_log rows.
reconcile_holdings_against_brokerage_position()
reconcile_transactions_against_brokerage_transaction()
```

**Phase 2 Alembic addendum:** the Phase 2 revision must `op.add_column` `plaid_item.first_investments_txn_date DATE` in addition to the staging tables already specified (the column captures actual backfill depth for the `/admin/connections` UI).

## Dedup / matching strategy

**Holdings vs. `BrokeragePosition`:**
- Match on `(account_id, ticker_symbol or cusip, snapshot_date)`.
- Compare `quantity` and `institution_price`. Any delta > 0.01 share or > 0.5% price → flag.
- `cost_basis`: Plaid's value is best-effort. Treat your CSV-imported `CostBasisLot` as authoritative for tax purposes; Plaid's number lands in staging only.

**Transactions vs. `BrokerageTransaction`:**
- Match on `(account_id, transaction_date ±1 day, ticker_symbol, abs(amount) within 0.01, transaction_type bucket)`.
- Match status:
  - `matched`: exists in both. Plaid is informational, CSV stays canonical.
  - `plaid_only`: in Plaid, not in CSV. Likely means CSV import is stale — surface in dashboard "needs CSV import" panel.
  - `csv_only`: in CSV, not in Plaid. Often historical (older than Plaid's 24-month window) — expected for old data, flag if recent.
- Dividends are the highest-value match candidates: they often hit weeks before the next CSV statement. `plaid_only` dividends become an automatic "consider importing" prompt.

## What stays canonical where

| Field | Canonical source | Why |
|---|---|---|
| Tax-lot identity (FIFO, specific-lot) | Existing `CostBasisLot` (XLSX import) | Plaid doesn't expose lot-level granularity reliably |
| Realized G/L | Existing brokerage realized-G/L logic | Computed from authoritative cost basis |
| Per-symbol position quantity | Plaid `holdings` once trusted; falls back to CSV `BrokeragePosition` | Plaid is daily-fresh; CSV requires manual import |
| Account total | Plaid `Balance` (Phase 1) | Broker-reported, no derivation drift |
| Dividend history | Plaid `investment_transactions` once trusted | Daily-fresh; CSV captures only after statement import |
| Buy/sell history | CSV `BrokerageTransaction` for tax purposes | Lot identity matters; Plaid for "did anything new happen?" alerts |

## UI additions for Phase 2

- `/admin/connections` gains a per-Item "products enabled" badge showing `Balance + Investments` for brokerage Items.
- New `/admin/reconciliation` page or tab listing the latest `plaid_reconciliation_log` entries with drill-down to mismatch details.
- `/brokerage/accounts/{id}/detail` page gains a "Plaid says" panel: latest Plaid holdings + the most recent unmatched Plaid transactions.

## Acceptance criteria for Phase 2

1. Holdings staging populated daily for all 6 brokerage Items.
2. Investment transactions staging idempotent on `investment_transaction_id`; backfill of 24 months on first run.
3. Reconciliation pass writes a `plaid_reconciliation_log` row per (account, day) with non-null delta vs. computed.
4. `/admin/reconciliation` surfaces unmatched-Plaid-only transactions for triage.
5. Existing `/brokerage` page unchanged in canonical numbers — Plaid data only appears in Phase 2 UI additions.

## Out of scope for Phase 2

- Auto-promoting Plaid transactions into `BrokerageTransaction` (next phase if reconciliation proves clean).
- Webhooks / real-time. Still polling.
- Plaid `Liabilities` for cards (APR, statement balance, due date) — separate decision later.
- Replacing existing CSV/XLSX importers. They stay as the tax-lot-of-record path.

---

# Resolved decisions

1. **Storage:** Plaid balances land in a **sibling table** (`plaid_account_balance_snapshot`), NOT in the existing `account_balance_snapshot`. Existing read paths and importers stay untouched. Reconciliation joins the two by `(account_id, snapshot_date)`.
2. **Franklin Templeton:** verify Plaid coverage in sandbox at link time. If FT is not in Plaid's institution finder, the slot frees up and Amex (or another card) becomes a candidate.
3. **OAuth callback:** if `https://macbook.ancon-cliff.ts.net` is unreachable from Plaid's infra during sandbox testing, **host the OAuth-return path via Cloudflare tunnel**. The tunnel only needs to expose the single `/admin/connections/oauth-return` route; the rest of the dashboard stays Tailscale-only.
4. **Encryption-at-rest:** `cryptography.fernet` with a symmetric key held in Doppler as `PLAID_TOKEN_ENC_KEY`. Tokens encrypted at write time, decrypted only inside the sync worker. Tokens never logged.
5. **SDK:** official `plaid-python` package, pinned to a specific minor version in `pyproject.toml`. Renovate-style upgrades only after sandbox re-verification.

---

# Known gaps + accepted risks

These are intentionally NOT in scope but worth surfacing so a future session doesn't accidentally rediscover them as problems.

1. **Liabilities not in Phase 1 net worth.** Mortgage, HELOC, and similar liability accounts are excluded from the Phase 1 slot allocation. Net worth as displayed will be **overstated by the outstanding mortgage principal** — quantify before launch and add a clear caveat in the dashboard. If a PenFed HELOC exists, slot 8 (PenFed) covers it via the same Plaid Item; net-worth aggregation must use `plaid_account_type` to negate.
2. **529 / HSA accounts.** Vanguard Item slots may surface 529 accounts; Plaid's brokerage product covers them. Net-worth treatment same as brokerage. HSA depends on custodian — if held at Fidelity, covered by slot 4. Out of scope for separate handling in Phase 1.
3. **Franklin Templeton coverage uncertainty.** Verify against Plaid's **production** institution finder (`/institutions/search` or the Plaid dashboard institution lookup), not sandbox — sandbox can fabricate connections for any string and gives a false green signal. If FT is not in production, the slot frees up; reconsider Amex or another candidate.
4. **NW Mutual / F&G / GSK manual flows.** No Plaid coverage. Continue existing PDF/XLSX importers. Net worth includes these only when the manual import is run; no daily auto-refresh.
5. **Non-USD accounts.** All connected institutions are USD. The sync code asserts `iso_currency_code == 'USD'` and skips with a warning otherwise. Adding FX is a separate scope.
6. **Reconciliation noise on settlement days.** Plaid's broker-reported balance includes pending/unsettled trades that the computed `position × yfinance` total may not. Expect daily T+1/T+2 drift — the threshold (`>2%` or `>$100`) filters routine noise from real divergence. Surface deltas above the threshold; suppress (but log) those below.
7. **`raw_data` PII in backups.** Both `plaid_account_balance_snapshot` and Phase 2 staging tables store full Plaid responses including account names, masks, and holdings detail. SGDrive backup inherits Apple's at-rest encryption; this is acceptable for personal use but means the backup target should not be shared with anyone who shouldn't see institution data. Periodic pruning of `raw_data` for snapshots older than 90d is a reasonable hardening if the risk model tightens.
8. **No real-time / no webhooks.** Daily cron only. Stale-Item alerting via the weekly P&L email gives a 7-day-worst-case lag on noticing a broken connection — accepted because (a) ITEM_LOGIN_REQUIRED is rare for a personal-use Item, (b) the Health Dashboard staleness check (REQ-015) catches it within 48h on any dashboard visit.
9. **Plaid Link link_token TTL.** The token expires 30 minutes after creation. The connections page must re-fetch the token on the click handler (not on page load) to avoid silent failures when a user idles on the page.
10. **CSP for Plaid Link CDN.** SvelteKit's CSP must allow `https://cdn.plaid.com` for the Link JS embed. If a strict CSP is added later, this exception must be preserved.
11. **Single-key encryption.** `PLAID_TOKEN_ENC_KEY` in Doppler is the sole encryption key. Compromise of Doppler ≠ token exposure unless DB also leaks (need both). Compromise of DB alone ≠ token exposure unless Doppler also leaks. Both leaking simultaneously is the worst case and not separately mitigated. Acceptable for single-user personal scope.

---

# Picking this up cold

A future session reads this file and should be able to:

1. Confirm slot allocation is still current (check `plaid_item` rows; compare against the table above).
2. Run the existing Phase 1 cron and verify `last_sync_at` is recent.
3. Inspect `plaid_reconciliation_log` to see whether Phase 1 has been stable enough to start Phase 2.
4. Begin Phase 2 by adding the staging tables, extending the cron, and building the reconciliation pass per the schema and pseudocode in this spec.

The existing `2026-03-15-accounting-system-design.md` is the broader system spec; this doc is scoped specifically to the Plaid integration and assumes familiarity with the brokerage Phase 3 schema (`HistoricalPrice`, `AccountBalanceSnapshot`, `ExpectedAccount`, `CostBasisLot`, `AccountTag`).
