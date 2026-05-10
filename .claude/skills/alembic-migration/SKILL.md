---
name: alembic-migration
description: Author and validate an Alembic migration that respects this project's audit-trail invariants — never drop transactions or audit_event, never drop raw_data/created_at/updated_at/confirmed_by, never run raw DELETE on protected tables, always provide a real downgrade. Use when the user asks to "create a migration", "alembic revision", "add a column to transactions", "schema change", or has just authored a migration and wants it validated.
user_invocable: true
---

# Alembic Migration

Walk a schema change from autogenerate to commit, with a static check that enforces the audit-trail rules from `CLAUDE.md`.

## When to invoke

- User asks to create a new migration ("add a column to transactions", "migrate the schema")
- User has just authored a migration and wants it validated before commit
- Reviewing a migration in a PR

## How to invoke

### Step 1 — Generate (when authoring)

If a migration file does not yet exist, ask the user what schema change they want, confirm the model edit, then run:

```bash
cd /Users/travis/SGDrive/dev/accounting && \
  source .venv/bin/activate && \
  doppler run -- alembic revision --autogenerate -m "<short imperative description>"
```

Show the generated file path.

### Step 2 — Check the migration

Run the static checker on the generated file (or any file the user points to):

```bash
cd /Users/travis/SGDrive/dev/accounting && \
  python3 .claude/skills/alembic-migration/check_migration.py src/db/alembic/versions/<file>.py
```

`python3` (system) is fine — the checker uses only the standard library, no venv required.

### Step 3 — Interpret the report

The checker prints a finding list per file. Severities:

| Severity | Trigger | Action |
|---|---|---|
| `P0` | Drops `raw_data`, `created_at`, `updated_at`, or `confirmed_by` from any table; `drop_table("transactions")`; raw `DELETE FROM` in `op.execute()`; the file does not parse | **Block.** Do not commit. Surface to the user; rewrite the migration. |
| `P1` | `downgrade()` is missing, empty, or just `pass` | **Block.** Either implement a real reverse or replace `pass` with `raise NotImplementedError("reason")` — silent no-op downgrades hide problems. |

Exit codes: `0` = clean, `1` = at least one P0/P1 finding, `2` = a path was not found.

### Step 4 — Present the result

Print the checker output inline. If clean, confirm "no audit-trail violations" and ask the user whether to apply (`alembic upgrade head`). If any P0/P1 fired, **stop and surface the findings** — never advise applying a migration that violates these invariants.

## What it doesn't check

The checker is intentionally narrow. Known gaps — read the migration manually for these:

- **Variable-arg drops**: `col = "raw_data"; batch_op.drop_column(col)` is invisible to the static check. Only string literals are inspected.
- **Table-context scoping**: a `drop_column("created_at")` outside a `batch_alter_table("transactions")` block still fires P0 — the checker is intentionally column-name-driven, not table-context-aware. False positives are possible if another (non-audit) table happens to use a protected column name; rare in this codebase.
- That a migration is reversible in spirit (only that `downgrade()` has a non-empty body, or that it is a merge migration)
- That the schema change matches the model edit
- Index naming conventions or constraint correctness
- Cross-migration compatibility (e.g., that `down_revision` is correct)

Merge migrations (where `down_revision` is a tuple/list of parent revisions) are exempted from the empty-downgrade check — their `pass` body is correct.

For everything else, read the migration alongside the model change and run `alembic upgrade head` against a tmp DB before applying to the live one.

## Why these rules

From `CLAUDE.md`:

> SQLite is the single source of truth … Never delete transactions … Every transaction preserves `raw_data` from original source … Full audit trail: `created_at`, `updated_at`, `confirmed_by`.

A migration that violates any of those breaks the trail. The checker's job is to catch it before it lands.
