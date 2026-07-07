# Ingestion & Classification Remediation — Design Spec

**Date:** 2026-07-07
**Author:** Travis Sparks (with Claude Code)
**Status:** Design → ready for implementation plan
**Branch:** `feat/remediation-and-features-2026-07`
**Scope:** REQ-FIX-ING-001..010 (`requirements/current.md` § Program 2026-07). Every fix traces to a verified defect with file:line evidence from the 2026-07-07 four-lens audit. No new features — correctness remediation of the ingest → classify → learn pipeline.

---

## 1. Verified defects

| REQ | Location | Defect |
|---|---|---|
| ING-001 | `src/adapters/bank_csv.py:572-583` | `session.begin_nested()` called imperatively, but the except handler calls `session.rollback()` — which rolls back the **outer** transaction, wiping every row flushed since the last `% 100` commit while `records_created` keeps counting them. One poisoned row silently destroys (up to) 99 good ones. |
| ING-002 | `src/adapters/gmail_n8n.py:680`, `src/utils/backfill_currency.py:119` | `float(abs(amount)) / best.amount` — `CurrencyAmount.amount` is `Decimal` (`src/utils/currency.py:37`); `float / Decimal` raises `TypeError`. Any receipt carrying **both** a USD amount and a detected foreign amount crashes `_process_file` on every run, permanently blocking that file. |
| ING-003 | `src/adapters/gmail_n8n.py:521-525` | Per-file `except` records the error but never calls `session.rollback()`. The session is left in a failed/dirty state (`_process_file` add/flushes before its own commit at :783), so every subsequent file in the batch fails too. |
| ING-004 | `src/api/routes/transactions.py:235-307, 925` | `_upsert_vendor_rule` on an existing rule only bumps `examples`/`confidence`/`last_matched`. It **never writes** `tax_category`/`direction`/`deductible_pct`. Confirming a human **correction** therefore *reinforces the old wrong rule* — the opposite of the learning loop's contract. |
| ING-005 | `src/classification/rules.py:52-58`, `src/models/vendor_rule.py:42` | `vendor_pattern` is compiled as a raw regex. Learned patterns are verbatim descriptions — metacharacters (`.` `$` `(` `+`) silently over-match (`"A.B Corp"` matches `"A9B Corporate"`); only `re.error` falls back to substring. |
| ING-006 | `src/adapters/bank_csv.py:527-529` | `source_id = filename:date:str(amount):desc` — (a) amount unquantized: a re-export rendering `10.5` as `10.50` changes the hash → duplicate row; (b) two legitimate identical same-day charges in one file collapse to one hash → second silently skipped. |
| ING-007 | `src/adapters/plaid_transactions.py:203-208, 338-357, 428-469` | `_existing_by_source_id` uses `.first()` with no `parent_id` filter — split children copy `parent.source_id` (`src/classification/splitter.py:228-241`), so modified/removed/pending-posted events can select and mutate a **child**, breaking the split-sum invariant. Split-parent events are skip+warn only (silent). Pending→posted transfer-recheck (:451-467) can flip a **human-rejected** row back to `needs_review`. First-sync supersede (:338-357) excludes `split_parent` rows but not their **children**. |
| ING-008 | `src/adapters/plaid_transactions.py:174-199`, `src/classification/engine.py:78-118` | `make_transaction` never reads `result.status`: a sign-veto (`_reconcile_sign` → `NEEDS_REVIEW`) with confidence ≥ 0.7 lands as `auto_classified`, and the veto's mismatch `review_reason` (set at :165) persists on the auto-classified row. No mirror veto exists for expense-on-inflow. |
| ING-009 | `src/classification/rules.py:64` | Rank key `(examples, confidence)` — a fat generic rule (`"amazon"`, 40 examples) always beats a precise one (`"amazon web services"`, 2 examples) for AWS charges. |
| ING-010 | `src/classification/engine.py:4,125-140`, `src/classification/llm_classifier.py:47-75,295`, `CLAUDE.md` | Tier 3 is Gemini (`gemini-2.5-flash-lite`) but docs/params say Claude: `classify(anthropic_api_key=…)` is fed `ANTHROPIC_API_KEY` by `src/utils/reclassify.py:189` and `src/api/routes/ingest.py:197` **into `genai.Client(api_key=…)`**, overriding `GEMINI_API_KEY` with a wrong-provider key. `_SYSTEM_PROMPT` omits `HEALTH_INSURANCE`, `WHOLESALE_INCOME`, `OTHER_EXPENSE`, `CAPITAL_CONTRIBUTION` — Gemini can never return them (decision locked: **keep Gemini**, fix docs + prompt). |

---

## 2. Design per REQ

### 2.1 ING-001 — bank_csv per-row savepoint (canonical pattern)

Adopt the canonical pattern from `src/adapters/xlsx_savings_plan.py:326`:

```python
# _process_row, replacing lines 572-583
if not self._dry_run:
    with session.begin_nested():        # ctx-mgr: rolls back ONLY this savepoint
        session.add(tx)
        session.flush()
    result.records_created += 1          # counted only after savepoint success
```

- Delete the imperative `begin_nested()` / bare `session.rollback()` / `% 100` interior commit. Single outer `session.commit()` in `run()` (make it unconditional when `not dry_run` — errors/skips may still have updated nothing, commit is a no-op then).
- Exceptions still propagate to `run()`'s per-row handler → `result.record_error(...)` (per-record isolation contract, `AdapterResult.errors`).
- `records_created` is now exact: increments happen strictly after a released savepoint.

### 2.2 ING-002 — Decimal-only exchange-rate math

`exchange_rate` column is `Numeric(18,8, asdecimal=True)` (`src/models/transaction.py:125`) — store `Decimal`, never `float`.

- `gmail_n8n.py:680` → `exchange_rate = (abs(amount) / best.amount).quantize(Decimal("1e-8"))` (both operands `Decimal`; the `best.amount > 0` guard stays).
- `backfill_currency.py:119` → `tx.exchange_rate = (abs(tx.amount) / best.amount).quantize(Decimal("1e-8"))` (`tx.amount` is already `Decimal` via `asdecimal=True`; the log line at :125 keeps its `float()` for `%.2f` formatting — display only).
- Sweep both files for any remaining `float(...)` used in *arithmetic* with `Decimal` (log-formatting casts are fine). `conversion.rate` / `conversion.usd_amount` are already `Decimal` and are stored as-is; the `Decimal(str(...))` wrap at `gmail_n8n.py:668` stays (harmless, boundary-defensive).

### 2.3 ING-003 — Gmail per-file rollback

```python
# run(), lines 521-525
for json_path in json_files:
    try:
        self._process_file(json_path, session, result)
    except Exception as exc:
        session.rollback()               # NEW: discard the poisoned partial file
        result.record_error(str(json_path), exc)
```

- `_process_file` commits per successful file (:783), so rollback discards exactly the failed file's partial `Transaction`/`IngestedFile` rows.
- **Decision:** no `FileStatus.ERROR` row is written on failure — failed files retry on the next run (transient OCR/API errors self-heal; a permanently bad file keeps surfacing in `result.errors`, which is the operator signal). Recorded here so the retry loop is intentional, not accidental.

### 2.4 ING-004 — Learning loop learns corrections

`_upsert_vendor_rule(session, tx)` is redesigned around **agreement vs divergence** between the confirmed transaction and the rule that *actually matches it* (Tier-1 semantics post-2.5/2.9, not raw `vendor_pattern == description` equality — today's exact-equality lookup can miss the broad rule that misclassified the row in the first place).

Let `fields(x) = (tax_category, tax_subcategory, direction, deductible_pct)`. Matching rule = best Tier-1 match for `(tx.description, tx.entity)` (specificity-ranked, § 2.9); fall back to exact-literal `(pattern, entity)` lookup.

| Case | Rule state | Action on confirm |
|---|---|---|
| No matching rule | — | Create learned rule: `pattern = tx.description` (literal, `is_regex=False`), `fields ← tx`, `confidence=0.80`, `examples=1`. (Unchanged.) |
| **Agreeing** confirm (`fields(rule) == fields(tx)`) | any | `examples += 1`, `last_matched = now`; learned rules bump `confidence = min(0.95, 0.80 + examples·0.01)`. Human-seed rules keep 0.95. (Unchanged.) |
| **Divergent** confirm, matched rule is the *exact-literal* rule for this description (case-insensitive `pattern == description`, `is_regex=False`) | learned | Overwrite `fields(rule) ← fields(tx)`; **reset** `examples = 1`, `confidence = 0.80` (base); `last_matched = now`. The rule must re-earn trust. |
| same | human seed | Overwrite `fields(rule) ← fields(tx)`; `examples = 1`; keep `confidence = 0.95` (a human correcting a human rule is still fully trusted). |
| **Divergent** confirm, matched rule is *broader* (substring/regex, e.g. `"amazon"` matched `"amazon web services"`) | any | **Do not mutate the broad rule** (it may still be right for other descriptions). Create (or update, same divergence logic) the exact-literal rule for `(tx.description, tx.entity)` with `fields(tx)`, `confidence=0.80`, `examples=1`. Specificity ranking (§ 2.9) guarantees the new rule outranks the broad one for identical descriptions → *"a correction flips the classification of the next matching transaction"* holds. |
| Human changed **entity** | — | Rules are `(pattern, entity)`-scoped: lookup/create runs against the **new** `tx.entity`. The old-entity rule is left untouched (same vendor legitimately serves two entities — Critical Rules table). |

Signature stays `(session, tx)` — divergence is computed by field comparison, not the PATCH `changes` dict, so an edit-in-one-PATCH / confirm-in-a-later-PATCH sequence still learns. All mutations logged at INFO (vendor_rules has no audit table; out of scope to add one).

### 2.5 ING-005 — Literal-by-default vendor patterns (`is_regex`)

**Schema:** add `vendor_rules.is_regex BOOLEAN NOT NULL server_default='0'` (§ 3 migration).

**Matcher (`rules.py:50-58`) becomes flag-driven:**

```python
if rule.is_regex:
    try:
        m = re.search(pattern, description, re.IGNORECASE)
    except re.error:
        logger.warning("invalid regex vendor rule %s — skipped", rule.id)
        continue                          # never silently reinterpret
else:
    m = re.search(re.escape(pattern), description, re.IGNORECASE)  # literal, escape-at-match
```

- **Escape-on-write, refined to escape-at-match:** the REQ's intent (metacharacters never get regex semantics) is delivered by escaping the stored literal at match time instead of persisting `re.escape()` output. Rationale: (a) one storage format — dashboard shows the human-readable vendor string, not `Anthropic,\ PBC`; (b) **no lossy data migration** — un-escaping on downgrade would corrupt patterns that legitimately contain backslashes; (c) `re.search(re.escape(p), d, IGNORECASE)` *is* case-insensitive substring, identical semantics to the REQ's "matched literally (substring)". The match object also feeds specificity ranking (§ 2.9) uniformly.
- **Migration story for existing rows:** schema-only. All existing patterns get `is_regex=false` via the server default → they flip from raw-regex to literal matching. This is the *desired* behavior change: every existing learned rule is a verbatim description; any operator who genuinely wants regex re-flags via the rule editor. Write paths that accept `is_regex=true` (dashboard/API rule editor) must `re.compile()`-validate at write time → 422 on invalid.
- `_upsert_vendor_rule` always writes `is_regex=False`.

### 2.6 ING-006 — Quantized dedup key + occurrence counter

New canonical `source_id` in `_process_row` (`bank_csv.py:527-529`):

```python
amt_q = Decimal(str(row.amount)).quantize(Decimal("0.01")) if row.amount is not None else ""
tuple_key = (str(row.date), str(amt_q), normalized_desc)
n = occurrence_counter[tuple_key]          # per-file collections.Counter, 0-based
occurrence_counter[tuple_key] += 1
source_id = f"{self._filename}:{row.date}:{amt_q}:{normalized_desc}:{n}"
```

- **Quantize** (`Decimal(str())` at the boundary, then `.quantize()` — hash-payload-quantization pattern): `10.5` and `10.50` now hash identically.
- **Occurrence counter** — idempotent by construction: identical tuples are indistinguishable, so *any* row ordering of the same file content assigns the same multiset of suffixes `{0..k-1}`. Re-import ⇒ same k source_ids ⇒ all skipped. Two identical same-day charges get `:0` and `:1` ⇒ both import. Counter scope is per `run()` (per file — filename is embedded in the key).
- **Legacy-hash bridge (decision: permanent, no backfill):** existing DB rows carry old-format hashes (`unquantized:no-suffix`). Dedup check becomes: skip if `new_hash` exists **or** (`n == 0` and `legacy_hash` exists) — the legacy scheme collapsed duplicates, so only occurrence 0 can pre-exist. Inserts always write the new format. Existing rows are **never mutated** (dedup keys of history are load-bearing; the two extra indexed lookups are permanent and cheap). The `# S1-008` comment block is updated to document both formats. **Regression lock (accepted tech debt, tracked not silently carried):** because the dual-lookup is permanent rather than sunset via a one-time backfill, `test_bank_csv.py` pins the exact branchy skip predicate (`new_hash exists OR (n==0 AND legacy_hash exists)`) as an explicit named test — `test_ing006_legacy_bridge_is_permanent_dual_lookup` — so any future attempt to simplify or remove one branch fails loudly instead of silently reintroducing duplicate-import risk on old rows. A backfill/sunset (rewriting `source_hash` on existing `bank_csv` rows to the new quantized+occurrence format, since `source_hash` is derived, not audit data) is noted here as a candidate future REQ, not undertaken in this program.

### 2.7 ING-007 — Plaid vs split/rejected rows (decision table)

Code changes: (1) `_existing_by_source_id` adds `.filter(Transaction.parent_id.is_(None))` — a split **child** is never selected; (2) new helper `_flag_split_parent_for_review(session, parent, reason)`: sets `parent.review_reason = reason`, flips each non-rejected child to `needs_review` with `review_reason = reason` + status audit event (parent keeps structural `split_parent` status — it cannot become `needs_review` without destroying the split); (3) `supersede_csv_rows` adds `Transaction.parent_id.is_(None)`; (4) pending→posted transfer-recheck exempt-set gains `REJECTED`.

| Plaid event | Register row state (post-fix lookup: parents only) | Behavior |
|---|---|---|
| `added` (new id) | none | insert via `make_transaction` (unchanged) |
| `added` | active row exists | skip — idempotent (unchanged) |
| `added` | rejected, `review_reason="plaid_removed"` | reactivate → `needs_review` + audit (unchanged) |
| `added` | rejected by **human** | skip — human veto sticks (unchanged behavior, now pinned by test) |
| `added` | `split_parent` | skip + warn (unchanged) |
| `added` w/ `pending_transaction_id` | prior pending, active non-split | promote `source_id`/`source_hash`, `_apply_update` (audited), transfer-recheck (unchanged) |
| `added` w/ pending id | prior pending = `split_parent` | **NEW:** `_flag_split_parent_for_review(…, "plaid: posted txn arrived for split pending — re-verify split")`; posted txn skipped; parent/children amounts untouched |
| `added` w/ pending id | prior pending **rejected** (human or plaid) | promote `source_id`/`source_hash` (prevents duplicate insert), `_apply_update` fields (audited), **status never flips** — `REJECTED` added to the transfer-recheck exempt set at :451-457 |
| `modified` | active non-split | `_apply_update` (unchanged) |
| `modified` | `split_parent` | **NEW:** no field mutation; `_flag_split_parent_for_review(…, "plaid_modified: upstream txn changed after split — re-verify split")`; counted as flagged, not updated |
| `modified` | split **child** | unreachable — lookup excludes children (the ING-007 root bug) |
| `modified` | rejected | fields refresh (audited); status untouched (unchanged) |
| `modified` / `removed` | none | skip (unchanged) |
| `removed` | active non-split | status → rejected, `review_reason="plaid_removed"` + audit (unchanged) |
| `removed` | `split_parent` | **NEW:** never rejected (would orphan children); `_flag_split_parent_for_review(…, "plaid_removed: upstream txn removed after split — re-verify split")` |
| `removed` | already rejected | no-op guard (skip re-audit) |
| first-sync supersede | bank_csv **children of split parents** | **NEW:** excluded (`parent_id IS NULL`); parents already excluded via status filter |

### 2.8 ING-008 — Sign vetoes end-to-end

**Engine (`_reconcile_sign`):** add the mirror branch after the existing income-on-outflow veto — authoritative-sign source (`plaid`/`bank_csv`), `amount > 0`, `result.direction == Direction.EXPENSE` ⇒ `replace(result, status=NEEDS_REVIEW, review_reason="Sign/category mismatch: … inflow classified as expense — likely refund or misclassification; confirm.")`. **Asymmetric by design:** category/direction are *not* overridden (a positive-amount "expense" is usually a refund — the human picks refund-as-income vs category reversal); the income-on-outflow branch keeps its `OTHER_EXPENSE` override (an outflow can never be income, so the override is safe). `transfer`/`reimbursable` directions stay exempt in both branches.

**Plaid `make_transaction` (:174-199):**

```python
vetoed = result.status == TransactionStatus.NEEDS_REVIEW
needs_review = entity is None or is_transfer_category or vetoed \
               or result.confidence < _AUTO_THRESHOLD
...
if vetoed and result.review_reason:
    reasons.append(result.review_reason)       # veto text survives regardless of confidence
if tx.status == TransactionStatus.AUTO_CLASSIFIED.value:
    tx.review_reason = None                    # no stale mismatch/low-conf text on clean rows
```

The initial `tx.review_reason = result.review_reason` at :165 is removed — `review_reason` is set exclusively by the reasons block (needs_review) or cleared (auto_classified). `engine.apply_result` (:202) already writes `result.review_reason` + `result.status` faithfully for non-Plaid callers — no change there.

### 2.9 ING-009 — Specificity-first Tier-1 ranking

`lookup_vendor_rule` keeps the match object per rule and ranks:

```python
best_rule, best_m = max(matches, key=lambda rm: (len(rm[1].group(0)), rm[0].examples,
                                                 rm[0].confidence, rm[0].id))
```

`len(match.group(0))` = actual matched-text length (works uniformly for literal and regex rules): `"amazon web services"` (19) beats `"amazon"` (6) regardless of example counts; `examples` then `confidence` break ties; `id` makes ranking deterministic. This ordering is the substrate for the ING-004 broad-vs-specific correction design.

### 2.10 ING-010 — Gemini stays; docs, params, prompt fixed

- **Docs:** `CLAUDE.md` Architecture bullet "Tier 3 Claude API" → "Tier 3 Gemini API (`gemini-2.5-flash-lite`)". `engine.py` module docstring (:4) and `classify` docstring; `llm_classifier.py:295` "Claude's JSON response" → "Gemini's". The Gmail OCR "Claude CLI" references are *correct* (separate, real Claude CLI usage) — untouched.
- **Param rename:** `classify(anthropic_api_key=…)` → `llm_api_key`; update `src/utils/reclassify.py:85-192` and `src/api/routes/ingest.py:197` to read `GEMINI_API_KEY` (not `ANTHROPIC_API_KEY`). This also fixes the latent defect of an Anthropic key being injected into `genai.Client`, clobbering the correct env fallback. No back-compat shim — both call sites are in-repo (grep-verified).
- **Prompt:** `_SYSTEM_PROMPT` category list gains `HEALTH_INSURANCE` (Business), `WHOLESALE_INCOME` (Business income), and a new "Other" line: `OTHER_EXPENSE` (catch-all expense; refunds/uncategorizable) and `CAPITAL_CONTRIBUTION` (owner money into an entity — not income). `_parse_response` already validates via `TaxCategory(...)`, so no parser change.

---

## 3. Migration plan

One Alembic revision (id style follows `p4ext1enum0xt`): **`vr_isregex01_vendor_rule_is_regex`**

- `upgrade()`: `op.add_column("vendor_rules", sa.Column("is_regex", sa.Boolean(), nullable=False, server_default="0"))` inside `batch_alter_table` (SQLite). No data rewrite (§ 2.5 rationale).
- `downgrade()`: real — drop `is_regex` via batch mode. Literal patterns revert to raw-regex matching (pre-fix behavior); acceptable and documented.
- Audit invariants respected trivially: no touch of `transactions`/`audit_event`/`raw_data`/timestamps; no DELETEs. Run the `migration-reviewer` agent before commit (`/alembic-migration` skill).
- **No other REQ needs a migration.** ING-006's hash-format change is handled by the dual-hash read path — `source_hash` of existing rows is never rewritten. Rollout: local `pytest` gates → rsync to box → `alembic upgrade head` → services restart (standard Hetzner flow).

---

## 4. Test strategy (TDD — failing test with REQ-ID first)

| REQ | Test file | Key cases |
|---|---|---|
| ING-001 | `src/adapters/test_bank_csv.py` | 5-row batch with row 3 forced `IntegrityError` (monkeypatched flush / dup hash) → rows 1,2,4,5 persisted, `records_created == 4`, 1 error; dry-run writes nothing. |
| ING-002 | `src/adapters/test_gmail_n8n.py`; **new** `src/utils/test_backfill_currency.py` | Fixture receipt with USD total **and** `£` amount → ingests, `exchange_rate` is `Decimal`, quantized 8dp; backfill reference-rate path with `Decimal` tx.amount; regression: no `TypeError`. |
| ING-003 | `src/adapters/test_gmail_n8n.py` | Directory of 3 files, file 2 raises after `session.add` → session usable, files 1,3 committed, 1 error recorded; failed file leaves no partial `Transaction`/`IngestedFile` rows. |
| ING-004 | **new** `src/api/routes/test_transactions_learning.py` | Agreeing confirm → `examples+1`, fields unchanged; divergent confirm on exact-literal learned rule → fields overwritten, `confidence==0.80`, `examples==1`; divergent under broad rule → broad rule untouched, specific rule created, **next classification of same description returns corrected fields** (the flip test); entity-change creates parallel rule; human-seed divergence keeps 0.95. |
| ING-005 | **new** `src/classification/test_rules.py` | Literal rule `"A.B Corp"` does NOT match `"A9B Corporate"`, DOES match substring case-insensitively; `is_regex=True` rule uses regex; invalid regex with `is_regex=True` is skipped (never substring-fallback); learned-rule write path sets `is_regex=False`. |
| ING-006 | `src/adapters/test_bank_csv.py` | Same file re-imported with `10.5` re-rendered `10.50` → 0 new rows; two identical same-day rows → both import (`:0`,`:1`); full re-import → 0 new (counter idempotency incl. shuffled row order); pre-existing legacy-hash row (occurrence 0) → skipped, occurrence 1 → inserted; `test_ing006_legacy_bridge_is_permanent_dual_lookup` locks the two-branch skip predicate so the permanent dual-hash bridge (accepted tech debt, no backfill in this program) regresses loudly if simplified. |
| ING-007 | `src/adapters/test_plaid_transactions.py` | Lookup returns parent when a child shares `source_id`; `modified`/`removed` on split parent → children flipped `needs_review` + parent reason set + no amount mutation + audit rows; pending→posted where prior is human-rejected → status stays `rejected`, id promoted; first-sync supersede leaves split-parent children untouched; every decision-table row above gets an assertion. |
| ING-008 | `src/classification/test_engine.py`; `src/adapters/test_plaid_transactions.py` | Mirror veto: plaid inflow classified expense → `NEEDS_REVIEW`, direction/category preserved; income-on-outflow veto unchanged (regression); `make_transaction` with vetoed high-confidence result → `needs_review` + veto text; clean auto_classified row → `review_reason is None`. |
| ING-009 | **new** `src/classification/test_rules.py` | `"amazon"` (examples=40) vs `"amazon web services"` (examples=2) on an AWS description → specific wins; equal length falls back to examples, then confidence; deterministic on full ties. |
| ING-010 | `src/classification/test_llm_classifier.py` | Parametrized over `TaxCategory`: every enum value appears verbatim in `_SYSTEM_PROMPT`; `classify(llm_api_key=…)` signature; grep-style asserts that `engine.py`/`llm_classifier.py` docstrings say Gemini (cheap guard against doc rot). |

Gates before commit: `pytest && ruff check src/ && mypy src/`. Per-record isolation (ING-001/003) is tested explicitly per Critical Patterns.

---

## 5. Constraints honored

- **Never delete transactions** — ING-007 replaces silent skips with review-flags; rejection remains the only exclusion path; split parents are never rejected by automation.
- **`raw_data` preserved** — untouched everywhere; `_apply_update` continues to refresh (not drop) it, audited.
- **`Decimal(str())` at boundaries** — ING-002/006 exclusively; no float arithmetic on money or rates.
- **Hash quantization** — ING-006 quantizes to cents before stringifying (Critical Patterns).
- **DRY-RUN defaults** — bank_csv/gmail dry-run semantics unchanged; no new write paths without existing opt-in flags.
- **Audit trail** — every automated status flip (ING-007 flags, rejected-row field refreshes) emits `AuditEvent` rows via existing `_audit_*` helpers.

## 6. Traceability

| REQ | Impl files | Tests |
|---|---|---|
| ING-001 | `src/adapters/bank_csv.py` | `src/adapters/test_bank_csv.py` |
| ING-002 | `src/adapters/gmail_n8n.py`, `src/utils/backfill_currency.py` | `src/adapters/test_gmail_n8n.py`, `src/utils/test_backfill_currency.py` |
| ING-003 | `src/adapters/gmail_n8n.py` | `src/adapters/test_gmail_n8n.py` |
| ING-004 | `src/api/routes/transactions.py` | `src/api/routes/test_transactions_learning.py` |
| ING-005 | `src/classification/rules.py`, `src/models/vendor_rule.py`, `src/db/alembic/versions/vr_isregex01_*.py` | `src/classification/test_rules.py` |
| ING-006 | `src/adapters/bank_csv.py` | `src/adapters/test_bank_csv.py` |
| ING-007 | `src/adapters/plaid_transactions.py` | `src/adapters/test_plaid_transactions.py` |
| ING-008 | `src/classification/engine.py`, `src/adapters/plaid_transactions.py` | `src/classification/test_engine.py`, `src/adapters/test_plaid_transactions.py` |
| ING-009 | `src/classification/rules.py` | `src/classification/test_rules.py` |
| ING-010 | `src/classification/llm_classifier.py`, `src/classification/engine.py`, `src/utils/reclassify.py`, `src/api/routes/ingest.py`, `CLAUDE.md` | `src/classification/test_llm_classifier.py` |
