---
name: doppler-guard
description: Background knowledge — always prefix Python commands that read project secrets with `doppler run --`. Applies to pytest, uvicorn, alembic (when DATABASE_PATH is the live DB), and any script that hits Stripe/Resend/Shopify/Anthropic APIs. Claude-only; never user-invoked.
user_invocable: false
---

# Doppler Guard

This project uses **Doppler** for all secrets. There is no `.env` file. Any command that touches the API integrations or the live DB must run under `doppler run --`.

## Rule

When you are about to run any of the following, prepend `doppler run --` to the command line:

| Command | Reason |
|---|---|
| `pytest` (when tests hit Stripe/Shopify/Resend/Anthropic) | Adapters read API keys at import time |
| `uvicorn src.api.main:app ...` | API routes read `STRIPE_API_KEY`, `RESEND_API_KEY`, etc. |
| `python -m scripts.<anything that imports adapters>` | Same — adapters fail at import without secrets |
| `alembic upgrade head` against the live DB | If migrations run model code that imports adapters |

## Skip the prefix when

- The command is pure CLI tooling that doesn't import adapters: `ruff check`, `mypy`, `pytest src/utils/`, alembic against a tmp DB
- The script is self-contained (e.g. `scripts/backup.sh`)
- You're running a one-off Python `-c "from src.models...`" that doesn't load the adapter module

## Symptoms of forgetting

- `ModuleNotFoundError: No module named 'sqlalchemy'` (Doppler activates `.venv` indirectly via `doppler run`; without it, the wrong Python may be invoked)
- `KeyError: 'STRIPE_API_KEY'` at adapter import
- API endpoint returns 500 with auth-error trace from Stripe/Resend

## Reference

Doppler project: `accounting`. Default config: `dev`.

```bash
doppler setup --project accounting --config dev    # one-time
doppler run -- pytest                              # tests
doppler run -- uvicorn src.api.main:app --reload   # dev server
doppler run -- python -m scripts.ingest-brokerage /path/to/accounts/
```
