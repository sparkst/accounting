# Accounting → Hetzner Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lift-and-shift the local Python accounting stack (FastAPI + SQLite + SvelteKit, no rewrite) onto the existing Hetzner box `ubuntu-4gb-nbg1-2`, public at `books.sparkry.ai` via Cloudflare Tunnel + Cloudflare Access, so Plaid gets a stable OAuth redirect and Chase can connect.

**Architecture:** `books.sparkry.ai` → CF Access (Google OAuth) → CF Tunnel (cloudflared, root, tunnel-token-only) → Caddy `127.0.0.1:9000` (`admin off`) → `/api/*` to uvicorn `127.0.0.1:8000`, `/*` to SvelteKit `vite preview 127.0.0.1:5173`. SQLite at `/home/travis/accounting/data/accounting.db` (owner travis, 600). All accounting services run as `travis` under systemd; the agentic-collab sandbox runs as a separate `collab` user. Backups go to Cloudflare R2.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy / SQLite (WAL), SvelteKit + Vite, Caddy, cloudflared, systemd units + timers, Doppler (`accounting/srv` config), Cloudflare Access + Tunnel + R2 + cron Worker, nftables + ufw + Hetzner Cloud Firewall.

**Source spec (authoritative):** `docs/superpowers/specs/2026-06-01-accounting-hetzner-migration-design.md`

---

## ⚠️ READ FIRST — load-bearing constraints (do not reorder)

1. **Security controls land in Part A (code) + Part B Phase 1/2 and are VERIFIED in Phase 2 BEFORE the Phase 3 tunnel exposes anything.** The four controls: (a) `API_KEY`/`INGEST_API_KEY` boot assertion incl. `API_KEY != INGEST_API_KEY`; (b) all `127.0.0.1` binds (8000/5173/9000); (c) nftables OUTPUT `skuid` rule blocking `collab`→loopback app ports (persisted); (d) ufw + Hetzner Cloud Firewall inbound-deny. **Phase 3 MUST NOT begin until the 5-point Phase-2 security gate is green.**
2. **All code tasks (Part A) are committed + pushed on the Mac FIRST, then rsync'd. There is NO hand-editing on the box.** If a code fix is discovered during box provisioning: fix + commit + push on the Mac, then re-rsync.
3. **PONR (point of no return) = the Phase-4 step-10 n8n ingest test (first Hetzner write); and again the first Phase-5 `--apply`.** Rollback is **binary on the PONR**: before PONR → re-enable the frozen read-only Mac; at/after PONR → **forward-recovery only, NEVER revert to the Mac.** A verified R2 baseline is taken BEFORE the first write (Phase-4 step 9).
4. **`INGEST_API_KEY != API_KEY`** is a permanent runtime invariant (boot assertion), not a one-time deploy check.
5. **Doppler runtime config is the NEW `accounting/srv`** — never `accounting/dev`, never `accounting/prd`.

---

## 📋 Fill these in before executing (box facts only the operator has)

The plan uses these placeholders. Collect once; substitute at execution time.

| Placeholder | Meaning | Value |
|---|---|---|
| `<BOX_TAILNET_HOST>` | Hetzner tailnet name (brief says `ubuntu`) | `ubuntu` |
| `<BOX_TAILNET_IP>` | Tailscale IP (brief says `100.103.3.121`) | `100.103.3.121` |
| `<BOX_PUBLIC_IP>` | Hetzner public IPv4 (for `nmap` verification) | _____ |
| `<TAILSCALE_SUBNET>` | Tailnet CIDR allowed inbound (e.g. `100.64.0.0/10`) | _____ |
| `<COLLAB_USER>` | OS user the agentic-collab sandbox runs as | `collab` |
| `<R2_BUCKET>` | Cloudflare R2 bucket name for backups | _____ |
| `<R2_ACCOUNT_ID>` / `<R2_ENDPOINT>` | R2 S3 endpoint | _____ |
| `<CF_ZT_ORG>` | Cloudflare Zero Trust org (team domain) | _____ |
| `<DOPPLER_SRV_TOKEN>` | Doppler **service token** scoped to `accounting/srv` | _____ |

---

## 🙋 Operator-only PAUSE points (executor STOPS and hands to Travis; never does these)

| When | Action |
|---|---|
| **NOW (pre-everything)** | Delete world-readable `/Users/travis/SGDrive/dev/accounting/.env`; **rotate** the live `RESEND_API_KEY` and `STRIPE_RESTRICTED_KEY` it contains (current exposure, independent of migration). |
| Before Phase 1 | Confirm CF zone + Zero Trust org (P-1/P-2); provide/confirm R2 bucket + `CLOUDFLARE_API_TOKEN`/`CLOUDFLARE_ACCOUNT_ID` (P-3); confirm `collab` is a separate user (P-4); confirm Syncthing roots exclude `data/` (P-5). |
| Phase 1 | Provide `<DOPPLER_SRV_TOKEN>`; set the real secret values in `accounting/srv`. |
| Phase 3 | Create CF Access application + the two service tokens (n8n ingest; uptime checker) in the Zero Trust dashboard. |
| **Phase 5** | Register `https://books.sparkry.ai/admin/connections/oauth-return` in the **production** Plaid app; **enter Chase credentials** in Plaid Link (final step). |

---

## File Structure

**Part A — code/config edited on the Mac (committed before transfer):**
- `pyproject.toml` — add `fpdf2`, `jinja2` deps (A1)
- `dashboard/vite.config.ts` — `preview.allowedHosts`/`host`/`port` (A2)
- `src/api/main.py` — CORS origin (A3); boot assertion (A5); health-router auth split (A6); ingest two-key registration (A7)
- `src/api/auth.py` — `require_ingest_api_key` (A7)
- `src/api/_startup_assert.py` — **new**, production-secret boot assertion helper (A5)
- `src/api/routes/health.py` — `/api/health/ping`, auth split, scrub `.env` string (A6)
- `src/adapters/gmail_n8n.py`, `src/adapters/deduction_email.py`, `src/api/routes/attachments.py` — env-configurable paths (A4)
- `src/api/routes/plaid.py`, `dashboard/src/routes/admin/connections/oauth-return/+page.svelte` — `redirect_uri` + comment (A8)
- `scripts/backup.sh` — extend to R2/flock/disk-check/etag/sentinel (A9)
- `scripts/backup_restore_test.py` — **new** (A10)
- `scripts/alert.py` — **new** (A11)
- `scripts/disk_check.sh` — **new** (A12)
- `scripts/plaid_transactions_sync.py` — `flock` around apply (A13)
- co-located `test_*.py` for each Python change

**Part B — created/configured on the box (no repo edits):** systemd unit files under `/etc/systemd/system/`, `/etc/accounting/doppler.env`, `Caddyfile`, cloudflared config, nftables/ufw rules, CF Access app + policies, CF cron Worker.

---

# PART A — Code tasks (Mac, TDD, committed + pushed before any box work)

> Run `pytest && ruff check src/ && mypy src/` green before committing each task. Final gate before transfer: all of Part A committed AND pushed.

---

### Task A1: Declare `fpdf2` + `jinja2` in pyproject.toml (REQ-HM-015)

**Files:**
- Modify: `pyproject.toml:6-16` (`[project.dependencies]`)
- Test: `src/invoicing/test_pdf_deps.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# src/invoicing/test_pdf_deps.py
"""REQ-HM-015: invoicing PDF/HTML deps must be DECLARED so a fresh
`pip install -e ".[dev]"` on the Hetzner box installs them."""
import tomllib
from pathlib import Path


def test_pyproject_declares_fpdf2_and_jinja2():
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    deps = tomllib.loads(pyproject.read_text())["project"]["dependencies"]
    joined = " ".join(deps)
    assert "fpdf2" in joined, "fpdf2 must be declared (pdf_renderer imports `from fpdf import FPDF`)"
    assert "jinja2" in joined, "jinja2 must be declared (render_html needs it)"


def test_render_html_jinja2_is_available():
    from src.invoicing import pdf_renderer
    assert pdf_renderer._JINJA2_AVAILABLE is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest src/invoicing/test_pdf_deps.py -v`
Expected: FAIL (`fpdf2`/`jinja2` not in deps; `_JINJA2_AVAILABLE` may be False).

- [ ] **Step 3: Add the deps**

In `pyproject.toml`, append to `dependencies` (after `"cryptography>=43.0.0",`):

```toml
    "fpdf2>=2.7",
    "jinja2>=3.1",
```

- [ ] **Step 4: Reinstall + run**

Run: `pip install -e ".[dev]" && pytest src/invoicing/test_pdf_deps.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/invoicing/test_pdf_deps.py
git commit -m "feat(deps): declare fpdf2 + jinja2 for invoicing on Hetzner (REQ-HM-015)"
```

> **Addendum (discovered during execution via a clean-venv `pytest --co` sweep — REQ-HM-011).** The fresh worktree venv (which faithfully simulates the box's `pip install`) surfaced a cascade of dependency/tooling gaps that the Mac masked. All fixed:
> - **Undeclared runtime deps** (imported under `src/` but absent from `pyproject.toml`; present in the Mac venv only): `stripe` (income ingestion + invoices), `openpyxl` (xlsx importers), `chardet` (bank CSV), `icalendar` (Fascinate calendar invoicing), `resend` (invoice email + A11 alert), and **`alembic`** (the migration engine — REQ-HM-004's cutover gate runs `alembic current == head`; without it migrations can't run on the box). Declared + guarded by `src/test_deps_declared.py`.
> - **Tool-version drift:** a fresh install pulled `anthropic 0.105` (Mac 0.84 — its changed messages API broke tier-3 classification), `mypy 2.x` (Mac 1.19), `ruff 0.15.15` (Mac 0.15.6). Pinned `anthropic>=0.84,<0.85`, `ruff~=0.15.6`, `mypy~=1.19`, `pytest~=9.0` so the box reproduces the Mac (lift-and-shift; bump deliberately).
> - **Pre-existing pytest blocker:** `scripts/test_ingest_brokerage.py` imports a non-existent `VanguardCsvAdapter` (fails identically on the Mac); its collection error otherwise hard-aborts the ENTIRE `pytest` run (zero tests). **Quarantined** via `addopts --ignore` (per owner decision) so the suite runs; the brokerage owner should fix the stale test.
> - **REQ-HM-011 gate, realistic interpretation (owner-approved):** the bare gates are NOT green on the Mac (92 ruff, 807 mypy errors — pre-existing debt across the untyped test suite). The box gate means: *box reproduces the Mac's tool behavior (pinned) + no NEW failures from migration code + the app imports/runs + `pytest` is fully green (now 2268 passed, was 0-runnable)*. Do NOT block cutover on the pre-existing lint/type debt.

> **🔄 Provider/backup deviations (owner-approved, decided during execution — supersede the spec where they conflict):**
> - **Tier-3 classifier: Anthropic → Google Gemini.** Owner had no Anthropic key; chose Gemini. `src/classification/llm_classifier.py` now uses `google-genai` (model **`gemini-2.5-flash-lite`**, sync; cheapest/fast, batch rejected — would need async rearchitecture of the per-record flow). Dep `anthropic` removed, **`google-genai>=2.8,<3`** added. Key **`GEMINI_API_KEY`** in `srv` (sourced from `openclaw`'s paid key). Supersedes §7 "tier-3 uses the Anthropic SDK" + REQ-HM-002's `ANTHROPIC_API_KEY`.
> - **R2 backup: `aws s3api` → `wrangler r2 object`.** No S3 access-key/secret. Uses a Cloudflare **API token** (`CLOUDFLARE_API_TOKEN`, R2 Storage Read+Write, in `accounting/prd`→mirrored to `srv`) + `CLOUDFLARE_ACCOUNT_ID` + `R2_BUCKET=sparkry-accounting-backups` (dedicated bucket, created). `backup.sh`/`backup_restore_test.py` rewritten: date-keyed objects (`daily/accounting-<YYYY-MM-DD>.db`) + a `.meta.json` row-count **sidecar** (replaces R2 object-metadata) + **readback-sha verify** (replaces etag) + rolling 15-day delete (no object-listing needed). `wrangler` installed globally on the box. Supersedes REQ-HM-006's etag/S3 wording + REQ-HM-002's `R2_BACKUP_WRITE_TOKEN`/`AWS_*`/`R2_ENDPOINT` (the prd `R2_BACKUP_WRITE_TOKEN` was the wrong credential — wealth-scoped, write-only).
> - **R2 precondition gate (REQ-HM-006): PASSED** — bucket exists; `wrangler r2 object put/get/verify/delete` round-trip succeeds both from the Mac and from the box via `doppler run` + the `srv` token.

---

### Task A2: vite preview host/port/allowedHosts for books.sparkry.ai (REQ-HM-017)

**Files:**
- Modify: `dashboard/vite.config.ts:14-16`
- Test: `dashboard/src/lib/test_vite_config.test.ts` (create) — or the inline node check in Step 2

- [ ] **Step 1: Write the failing test**

```typescript
// dashboard/src/lib/test_vite_config.test.ts
import { readFileSync } from 'node:fs';
import { describe, it, expect } from 'vitest';

describe('vite preview config (REQ-HM-017)', () => {
  const cfg = readFileSync(new URL('../../vite.config.ts', import.meta.url), 'utf8');
  it('allows books.sparkry.ai + the hetzner tailnet host', () => {
    expect(cfg).toContain('books.sparkry.ai');
    expect(cfg).toContain('ubuntu'); // <BOX_TAILNET_HOST>
  });
  it('pins preview host + port (belt-and-braces vs vite 0.0.0.0:4173 default)', () => {
    expect(cfg).toMatch(/host:\s*['"]127\.0\.0\.1['"]/);
    expect(cfg).toMatch(/port:\s*5173/);
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd dashboard && npx vitest run src/lib/test_vite_config.test.ts`
Expected: FAIL (host/port/books.sparkry.ai absent). If vitest is not configured, instead verify by `grep -c 'books.sparkry.ai' vite.config.ts` returning `0`.

- [ ] **Step 3: Edit the config**

Replace the `preview` block in `dashboard/vite.config.ts`:

```typescript
	preview: {
		host: '127.0.0.1',
		port: 5173,
		allowedHosts: ['books.sparkry.ai', 'ubuntu', 'localhost', '127.0.0.1']
	}
```

(Substitute `<BOX_TAILNET_HOST>` for `'ubuntu'` if different. The explicit `host`/`port` are belt-and-braces; the systemd `ExecStart` also passes `--host 127.0.0.1 --port 5173` because vite preview defaults to `0.0.0.0:4173`.)

- [ ] **Step 4: Run to verify it passes**

Run: `cd dashboard && npx vitest run src/lib/test_vite_config.test.ts` (or re-grep)
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add dashboard/vite.config.ts dashboard/src/lib/test_vite_config.test.ts
git commit -m "feat(dashboard): vite preview host/port/allowedHosts for books.sparkry.ai (REQ-HM-017)"
```

---

### Task A3: Add production origin to CORS (REQ-HM-017)

**Files:**
- Modify: `src/api/main.py:138-147`
- Test: `src/api/test_cors.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# src/api/test_cors.py
"""REQ-HM-017: the production origin must be in CORS allow_origins."""
from fastapi.testclient import TestClient

from src.api.main import app


def test_books_origin_allowed():
    client = TestClient(app)
    resp = client.options(
        "/api/health/ping",
        headers={
            "Origin": "https://books.sparkry.ai",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.headers.get("access-control-allow-origin") == "https://books.sparkry.ai"
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest src/api/test_cors.py -v`
Expected: FAIL (origin not echoed). *(If `/api/health/ping` does not exist yet because A6 runs later, temporarily target `/api/health`; reconcile once A6 lands. Recommended: do A6 before A3, or use `/api/health`.)*

- [ ] **Step 3: Add the origin**

In `src/api/main.py`, extend `allow_origins`:

```python
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "https://books.sparkry.ai",
    ],
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest src/api/test_cors.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/api/main.py src/api/test_cors.py
git commit -m "feat(api): allow books.sparkry.ai CORS origin (REQ-HM-017)"
```

---

### Task A4: Env-configurable runtime paths (REQ-HM-018)

Make three Mac-hardcoded path sets env-driven so they resolve to Hetzner-local dirs.

**Files:**
- Modify: `src/adapters/gmail_n8n.py:477-485` (`_DEFAULT_DIRS` + constructor) → `GMAIL_N8N_DIRS`
- Modify: `src/adapters/deduction_email.py:194-200` → `DEDUCTION_DIR`
- Modify: `src/api/routes/attachments.py:29-35` (`_ALLOWED_ROOTS`, `_RECEIPTS_ROOT`) → `ATTACHMENT_ROOTS`, `RECEIPTS_ROOT`
- Test: `src/adapters/test_env_paths.py`, `src/api/test_attachment_roots.py` (create)

- [ ] **Step 1: Write the failing tests**

```python
# src/adapters/test_env_paths.py
"""REQ-HM-018: ingestion dirs read from env (Hetzner-local), default to Mac paths."""
from pathlib import Path

from src.adapters.gmail_n8n import GmailN8nAdapter
from src.adapters.deduction_email import DeductionEmailAdapter


def test_gmail_dirs_from_env(monkeypatch):
    monkeypatch.setenv("GMAIL_N8N_DIRS", "/home/travis/accounting/data/inbox:/home/travis/accounting/data/review")
    a = GmailN8nAdapter()
    assert Path("/home/travis/accounting/data/inbox") in a.source_dirs
    assert Path("/home/travis/accounting/data/review") in a.source_dirs


def test_gmail_dirs_default_when_unset(monkeypatch):
    monkeypatch.delenv("GMAIL_N8N_DIRS", raising=False)
    a = GmailN8nAdapter()
    assert any("SGDrive" in str(p) for p in a.source_dirs)


def test_deduction_dir_from_env(monkeypatch):
    monkeypatch.setenv("DEDUCTION_DIR", "/home/travis/accounting/data/deductions")
    a = DeductionEmailAdapter()
    assert Path("/home/travis/accounting/data/deductions") in a.source_dirs
```

```python
# src/api/test_attachment_roots.py
"""REQ-HM-018: attachment roots + receipts root read from env."""
import importlib
from pathlib import Path


def test_roots_from_env(monkeypatch):
    monkeypatch.setenv("ATTACHMENT_ROOTS", "/home/travis/accounting/data")
    monkeypatch.setenv("RECEIPTS_ROOT", "/home/travis/accounting/data/receipts")
    import src.api.routes.attachments as att
    importlib.reload(att)
    assert Path("/home/travis/accounting/data") in att._ALLOWED_ROOTS
    assert att._RECEIPTS_ROOT == Path("/home/travis/accounting/data/receipts")
    importlib.reload(att)  # restore for other tests
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest src/adapters/test_env_paths.py src/api/test_attachment_roots.py -v`
Expected: FAIL (env vars ignored).

- [ ] **Step 3: Implement env-driven defaults**

`src/adapters/gmail_n8n.py` — in `__init__`, before building `self.source_dirs`, add `import os` at top and resolve the default from env:

```python
        env_dirs = os.environ.get("GMAIL_N8N_DIRS")
        default_dirs = (
            [d for d in env_dirs.split(os.pathsep) if d]
            if env_dirs
            else list(self._DEFAULT_DIRS)
        )
        self.source_dirs = [Path(d) for d in (source_dirs or default_dirs)]
```

`src/adapters/deduction_email.py` — identical pattern with `DEDUCTION_DIR`:

```python
        env_dirs = os.environ.get("DEDUCTION_DIR")
        default_dirs = (
            [d for d in env_dirs.split(os.pathsep) if d]
            if env_dirs
            else list(self._DEFAULT_DIRS)
        )
        self.source_dirs = [Path(d) for d in (source_dirs or default_dirs)]
```

`src/api/routes/attachments.py` — replace the hardcoded module constants:

```python
import os

_ALLOWED_ROOTS = [
    Path(p)
    for p in (
        os.environ.get("ATTACHMENT_ROOTS")
        or "/Users/travis/SGDrive/LIVE_SYSTEM/accounting:/Users/travis/SGDrive/dev/accounting/data"
    ).split(os.pathsep)
    if p
]

_RECEIPTS_ROOT = Path(
    os.environ.get("RECEIPTS_ROOT", "/Users/travis/SGDrive/dev/accounting/data/receipts")
)
```

- [ ] **Step 4: Run to verify they pass**

Run: `pytest src/adapters/test_env_paths.py src/api/test_attachment_roots.py -v && ruff check src/ && mypy src/`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/adapters/gmail_n8n.py src/adapters/deduction_email.py src/api/routes/attachments.py src/adapters/test_env_paths.py src/api/test_attachment_roots.py
git commit -m "feat(adapters): env-configurable ingestion/attachment paths for Hetzner (REQ-HM-018)"
```

---

### Task A5: Mandatory production secret boot assertion (§7 — load-bearing)

Refuse to boot in production if `API_KEY` or `INGEST_API_KEY` is unset/weak, **or if they are equal**.

**Files:**
- Create: `src/api/_startup_assert.py`
- Modify: `src/api/main.py:50` (call inside `lifespan`)
- Test: `src/api/test_startup_assert.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# src/api/test_startup_assert.py
"""§7: production boot assertion for API_KEY / INGEST_API_KEY."""
import pytest

from src.api._startup_assert import assert_production_secrets

STRONG_A = "a" * 32
STRONG_B = "b" * 32


def _env(monkeypatch, **kw):
    for k in ("PLAID_ENV", "API_KEY", "INGEST_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    for k, v in kw.items():
        monkeypatch.setenv(k, v)


def test_missing_api_key_in_production_raises(monkeypatch):
    _env(monkeypatch, PLAID_ENV="production", INGEST_API_KEY=STRONG_B)
    with pytest.raises(RuntimeError, match="API_KEY"):
        assert_production_secrets()


def test_missing_ingest_key_in_production_raises(monkeypatch):
    _env(monkeypatch, PLAID_ENV="production", API_KEY=STRONG_A)
    with pytest.raises(RuntimeError, match="INGEST_API_KEY"):
        assert_production_secrets()


def test_weak_key_raises(monkeypatch):
    _env(monkeypatch, PLAID_ENV="production", API_KEY="short", INGEST_API_KEY=STRONG_B)
    with pytest.raises(RuntimeError, match="32"):
        assert_production_secrets()


def test_equal_keys_raise(monkeypatch):
    _env(monkeypatch, PLAID_ENV="production", API_KEY=STRONG_A, INGEST_API_KEY=STRONG_A)
    with pytest.raises(RuntimeError, match="must differ"):
        assert_production_secrets()


def test_two_distinct_strong_keys_ok(monkeypatch):
    _env(monkeypatch, PLAID_ENV="production", API_KEY=STRONG_A, INGEST_API_KEY=STRONG_B)
    assert_production_secrets()  # no raise


def test_non_production_is_permissive(monkeypatch):
    _env(monkeypatch)  # PLAID_ENV unset → not production
    assert_production_secrets()  # no raise
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest src/api/test_startup_assert.py -v`
Expected: FAIL (`src.api._startup_assert` does not exist).

- [ ] **Step 3: Implement the assertion**

```python
# src/api/_startup_assert.py
"""Production boot assertion (§7 Hetzner migration).

Refuses to start in production unless BOTH API_KEY and INGEST_API_KEY are set,
strong (>=32 chars), and DISTINCT. The equality check is a permanent runtime
invariant (not a one-time deploy check) so a future Doppler rotation that
collapses the two keys cannot silently re-merge the dashboard and ingest keys.
"""
from __future__ import annotations

import hmac
import os

_MIN_LEN = 32


def assert_production_secrets() -> None:
    if os.environ.get("PLAID_ENV") != "production":
        return

    api_key = os.environ.get("API_KEY") or ""
    ingest_key = os.environ.get("INGEST_API_KEY") or ""

    for name, value in (("API_KEY", api_key), ("INGEST_API_KEY", ingest_key)):
        if not value:
            raise RuntimeError(f"{name} must be set in production (got empty)")
        if len(value) < _MIN_LEN:
            raise RuntimeError(f"{name} must be >= {_MIN_LEN} chars in production")

    if hmac.compare_digest(api_key, ingest_key):
        raise RuntimeError("API_KEY and INGEST_API_KEY must differ (must differ)")
```

Then call it first thing in `lifespan` in `src/api/main.py`:

```python
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    from src.api._startup_assert import assert_production_secrets

    assert_production_secrets()
    logger.info("Starting accounting API — initialising database …")
    init_db()
    ...
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest src/api/test_startup_assert.py -v && mypy src/api/_startup_assert.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/api/_startup_assert.py src/api/main.py src/api/test_startup_assert.py
git commit -m "feat(api): production boot assertion for API_KEY/INGEST_API_KEY incl. inequality (§7)"
```

---

### Task A6: Minimal `/api/health/ping` + move rich health behind auth (§7)

**Files:**
- Modify: `src/api/routes/health.py` (add `/health/ping`; scrub `.env` string in `_SOURCE_CONFIG`)
- Modify: `src/api/main.py:154` (split health router: keep only `/ping` public; rich health + source-config behind `_auth`)
- Test: `src/api/routes/test_health_ping.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# src/api/routes/test_health_ping.py
"""§7: /api/health/ping is public+minimal; rich health + source-config require auth."""
from fastapi.testclient import TestClient


def _client(monkeypatch):
    monkeypatch.setenv("API_KEY", "k" * 32)
    monkeypatch.setenv("INGEST_API_KEY", "i" * 32)
    from src.api.main import app
    return TestClient(app)


def test_ping_is_public_and_minimal(monkeypatch):
    c = _client(monkeypatch)
    r = c.get("/api/health/ping")
    assert r.status_code == 200
    body = r.json()
    assert body == {"ok": True}
    for leak in ("error_detail", "institution_name", "llm_usage", "failure_log"):
        assert leak not in body


def test_rich_health_requires_auth(monkeypatch):
    c = _client(monkeypatch)
    assert c.get("/api/health").status_code == 401
    assert c.get("/api/health", headers={"X-Api-Key": "k" * 32}).status_code == 200


def test_source_config_requires_auth_and_scrubs_dotenv(monkeypatch):
    c = _client(monkeypatch)
    assert c.get("/api/health/source-config").status_code == 401
    r = c.get("/api/health/source-config", headers={"X-Api-Key": "k" * 32})
    assert r.status_code == 200
    assert ".env" not in r.text  # stale disclosure scrubbed
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest src/api/routes/test_health_ping.py -v`
Expected: FAIL (`/ping` 404; rich health is public 200; `.env` present).

- [ ] **Step 3: Implement**

In `src/api/routes/health.py`, add a separate router for the public ping and keep the rich endpoints on the existing `router`:

```python
ping_router = APIRouter(tags=["health"])


@ping_router.get("/health/ping")
def health_ping() -> dict[str, bool]:
    """Minimal readiness probe — no DB, no secrets. Used by the systemd
    ExecStartPost probe and the external CF uptime Worker."""
    return {"ok": True}
```

Scrub the stale `.env` references in `_SOURCE_CONFIG` (replace `"...in .env."` / `"...in .env"` notes with Doppler-neutral wording, e.g. `"Requires SHOPIFY_API_KEY and SHOPIFY_STORE_URL in the runtime config."`).

In `src/api/main.py`, change the health registration (line ~154):

```python
from src.api.routes.health import ping_router as health_ping_router
from src.api.routes.health import router as health_router

# Only the minimal ping is public (CF Access guards it at the edge).
app.include_router(health_ping_router, prefix="/api")

_auth = [Depends(require_api_key)]
# Rich health + source-config are dashboard diagnostics → behind API_KEY.
app.include_router(health_router, prefix="/api", dependencies=_auth)
```

(Keep the rest of the `_auth` router registrations unchanged. Verify `/api/health` and `/api/health/source-config` now resolve via `health_router` under `_auth`.)

- [ ] **Step 4: Run to verify it passes**

Run: `pytest src/api/routes/test_health_ping.py src/api/test_cors.py -v && mypy src/`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/api/routes/health.py src/api/main.py src/api/routes/test_health_ping.py
git commit -m "feat(api): public minimal /health/ping; rich health+source-config behind auth; scrub .env (§7)"
```

---

### Task A7: Distinct ingest key — `require_ingest_api_key` (§7)

**Files:**
- Modify: `src/api/auth.py` (add `require_ingest_api_key`)
- Modify: `src/api/main.py:163` (register `ingest_router` with the new dependency)
- Test: `src/api/test_ingest_auth.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# src/api/test_ingest_auth.py
"""§7: /api/ingest/* accepts INGEST_API_KEY only — NOT the browser API_KEY."""
from fastapi.testclient import TestClient


def _client(monkeypatch):
    monkeypatch.setenv("API_KEY", "k" * 32)
    monkeypatch.setenv("INGEST_API_KEY", "i" * 32)
    from src.api.main import app
    return TestClient(app)


def test_browser_key_rejected_on_ingest(monkeypatch):
    c = _client(monkeypatch)
    r = c.post("/api/ingest/run", headers={"X-Api-Key": "k" * 32}, json={})
    assert r.status_code == 401  # browser key must NOT drive ingest


def test_ingest_key_accepted_on_ingest(monkeypatch):
    c = _client(monkeypatch)
    r = c.post("/api/ingest/run", headers={"X-Api-Key": "i" * 32}, json={})
    assert r.status_code != 401  # 200/202/422 etc. — auth passed


def test_ingest_key_rejected_on_browser_route(monkeypatch):
    c = _client(monkeypatch)
    r = c.get("/api/transactions", headers={"X-Api-Key": "i" * 32})
    assert r.status_code == 401  # ingest key must NOT drive browser routes
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest src/api/test_ingest_auth.py -v`
Expected: FAIL (ingest still uses shared `_auth`/`API_KEY`).

- [ ] **Step 3: Implement**

Add to `src/api/auth.py` (mirrors `require_api_key`, reads `INGEST_API_KEY` only):

```python
_INGEST_KEY_HEADER = APIKeyHeader(name="X-Api-Key", auto_error=False)


def require_ingest_api_key(
    header_key: str | None = Security(_INGEST_KEY_HEADER),
) -> None:
    """Enforce INGEST_API_KEY (machine-to-machine) for /api/ingest/* ONLY.

    Checks INGEST_API_KEY exclusively — it does NOT also accept API_KEY, so a
    dashboard user who extracts the browser-baked VITE_API_KEY cannot drive
    ingest. Non-empty/strength is guaranteed by the lifespan() boot assertion;
    this dependency just compares.
    """
    expected = os.environ.get("INGEST_API_KEY")
    if not expected:
        return
    if not header_key or not hmac.compare_digest(header_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
```

In `src/api/main.py`, import it and change the ingest registration (line 163) — do NOT broaden `require_api_key`:

```python
from src.api.auth import require_api_key, require_ingest_api_key
...
app.include_router(ingest_router, prefix="/api", dependencies=[Depends(require_ingest_api_key)])
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest src/api/test_ingest_auth.py -v && ruff check src/ && mypy src/`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/api/auth.py src/api/main.py src/api/test_ingest_auth.py
git commit -m "feat(api): distinct require_ingest_api_key for /api/ingest/* (§7)"
```

> **REVISED during execution (owner-approved deviation from spec §7 literal acceptance).** The spec said "move the whole `ingest_router` to `INGEST_API_KEY`" with acceptance "`API_KEY` → 401 on `/api/ingest/run`". But the dashboard calls `ingest_router` endpoints with the BROWSER key: "Sync Now" → `POST /api/ingest/run` (`+page.svelte`) and the CSV import page → `POST /api/import/brokerage-csv` (`import/+page.svelte`). Applying the spec literally broke both live features. **Revised to per-route auth** (commits `…scope ingest key to /ingest/run (accept either key)…` + the circuit-breaker test-isolation fix): `/api/ingest/run` accepts EITHER `API_KEY` or `INGEST_API_KEY` (dashboard trigger + n8n both work; it's a no-body trigger, CF Access gates all callers) via a new `require_api_or_ingest_key`; `/api/import/brokerage-csv` and `/api/ingest/reclassify` stay browser-`API_KEY`-only (dashboard-only; n8n never calls them); the old `require_ingest_api_key` was removed. The A5 boot assertion still enforces `API_KEY != INGEST_API_KEY` (distinctness preserved). **Net auth contract:** brokerage-csv + reclassify reject the ingest key; ingest/run rejects unknown keys; n8n uses `INGEST_API_KEY` on `/ingest/run`. The Phase-4 step-10 n8n cutover header (`X-Api-Key: <INGEST_API_KEY>`) is unchanged and correct.

---

### Task A8: Plaid `redirect_uri` in create_link_token AND relink_item (REQ-HM-007)

**Files:**
- Modify: `src/api/routes/plaid.py` — `create_link_token` (req at line 279-289) + `relink_item` (req at 534-540)
- Modify: `dashboard/src/routes/admin/connections/oauth-return/+page.svelte:9-10` (stale comment)
- Test: `src/api/routes/test_plaid_redirect_uri.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# src/api/routes/test_plaid_redirect_uri.py
"""REQ-HM-007: both link-token paths send redirect_uri in production."""
from unittest.mock import MagicMock

import src.api.routes.plaid as plaid_mod


def _capture_request(monkeypatch):
    captured = {}
    fake_client = MagicMock()

    def _create(req):
        captured["req"] = req
        r = MagicMock()
        r.link_token = "link-sandbox-xyz"
        return r

    fake_client.link_token_create.side_effect = _create
    monkeypatch.setattr(plaid_mod, "_get_plaid_client", lambda: fake_client)
    monkeypatch.setenv("PLAID_ENV", "production")
    monkeypatch.setenv("PLAID_REDIRECT_URI", "https://books.sparkry.ai/admin/connections/oauth-return")
    return captured


def test_create_link_token_sends_redirect_uri(monkeypatch, db_session):
    captured = _capture_request(monkeypatch)
    plaid_mod.create_link_token(session=db_session)
    assert captured["req"].redirect_uri == "https://books.sparkry.ai/admin/connections/oauth-return"


def test_relink_sends_redirect_uri(monkeypatch, db_session, active_plaid_item):
    captured = _capture_request(monkeypatch)
    plaid_mod.relink_item(item_id=active_plaid_item.id, session=db_session)
    assert captured["req"].redirect_uri == "https://books.sparkry.ai/admin/connections/oauth-return"
```

*(Use the project's existing Plaid test fixtures for `db_session`/`active_plaid_item`; mirror `src/api/routes/test_plaid*.py`. If none exist for these, build a placeholder PlaidItem with a real `access_token_encrypted` via `encrypt_token`.)*

- [ ] **Step 2: Run to verify it fails**

Run: `pytest src/api/routes/test_plaid_redirect_uri.py -v`
Expected: FAIL (`redirect_uri` attribute absent on the request).

- [ ] **Step 3: Implement (both paths, env-gated)**

Add a module helper near the top of `src/api/routes/plaid.py`:

```python
def _plaid_redirect_uri() -> str | None:
    """REQ-HM-007: OAuth redirect for production banks (Chase). Sent only when
    in production or explicitly configured."""
    import os

    uri = os.environ.get("PLAID_REDIRECT_URI")
    if uri and (os.environ.get("PLAID_ENV") == "production" or uri):
        return uri
    return None
```

In `create_link_token`, build the request with the redirect when present:

```python
    req_kwargs: dict[str, Any] = dict(
        user=LinkTokenCreateRequestUser(client_user_id=placeholder.id),
        client_name="Travis Accounting",
        products=[Products("transactions")],
        additional_consented_products=[Products("investments")],
        country_codes=[CountryCode("US")],
        language="en",
    )
    _redirect = _plaid_redirect_uri()
    if _redirect:
        req_kwargs["redirect_uri"] = _redirect
    req = LinkTokenCreateRequest(**req_kwargs)
```

In `relink_item`, the same:

```python
    req_kwargs = dict(
        user=LinkTokenCreateRequestUser(client_user_id=item.id),
        client_name="Travis Accounting",
        country_codes=[CountryCode("US")],
        language="en",
        access_token=access_token,
    )
    _redirect = _plaid_redirect_uri()
    if _redirect:
        req_kwargs["redirect_uri"] = _redirect
    req = LinkTokenCreateRequest(**req_kwargs)
```

Fix the stale comment in `dashboard/src/routes/admin/connections/oauth-return/+page.svelte:9-10`:

```svelte
	// This page lives at https://books.sparkry.ai/admin/connections/oauth-return
	// (served via Cloudflare Tunnel behind Cloudflare Access).
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest src/api/routes/test_plaid_redirect_uri.py -v && mypy src/`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/api/routes/plaid.py dashboard/src/routes/admin/connections/oauth-return/+page.svelte src/api/routes/test_plaid_redirect_uri.py
git commit -m "feat(plaid): send redirect_uri in create_link_token + relink_item; fix oauth-return comment (REQ-HM-007)"
```

---

### Task A9: Extend `backup.sh` — R2 / flock / disk-check / etag / sentinel (REQ-HM-006)

**Files:**
- Modify: `scripts/backup.sh`
- Test: `scripts/test_backup_sh.py` (create — invokes the script in a sandbox with a stub R2 uploader and a temp DB)

- [ ] **Step 1: Write the failing test (bash smoke harness driven from pytest)**

```python
# scripts/test_backup_sh.py
"""REQ-HM-006: backup.sh disk-free gate, flock, integrity_check, sentinel.
The R2 upload + dead-man ping are stubbed via env hooks so the harness runs
offline; live R2/etag is verified in the box phase."""
import os
import sqlite3
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backup.sh"


def _make_db(p: Path):
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    con.execute("INSERT INTO t VALUES (1)")
    con.commit()
    con.close()


def _run(tmp_path, **env):
    db = tmp_path / "data" / "accounting.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    _make_db(db)
    e = {**os.environ, "REPO_ROOT_OVERRIDE": str(tmp_path), "R2_DISABLE": "1", **env}
    return subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=e)


def test_aborts_below_5gb(tmp_path):
    r = _run(tmp_path, DISK_FREE_GB_OVERRIDE="3")
    assert r.returncode != 0
    assert "5 GB" in (r.stdout + r.stderr) or "disk" in (r.stdout + r.stderr).lower()


def test_sentinel_created_then_removed(tmp_path):
    r = _run(tmp_path, DISK_FREE_GB_OVERRIDE="50")
    sentinel = tmp_path / "data" / ".backup.in-progress"
    assert not sentinel.exists()  # removed after successful run
    assert r.returncode == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest scripts/test_backup_sh.py -v`
Expected: FAIL (no disk gate / sentinel logic / overrides).

- [ ] **Step 3: Rewrite `scripts/backup.sh`**

```bash
#!/usr/bin/env bash
# backup.sh — consistent SQLite snapshot → integrity_check → versioned R2 upload.
# REQ-HM-006: disk-free gate, flock serialization, integrity BEFORE upload,
# etag verify, per-table row-count metadata, dead-man ping, in-progress sentinel.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${REPO_ROOT_OVERRIDE:-$(dirname "$SCRIPT_DIR")}"
DB_PATH="$REPO_ROOT/data/accounting.db"
LOCK="$REPO_ROOT/data/.backup.lock"
SENTINEL="$REPO_ROOT/data/.backup.in-progress"
TS="$(date -u '+%Y-%m-%dT%H%M%SZ')"
TMP_SNAP="$(mktemp /tmp/accounting-backup.XXXXXX.db)"
MIN_FREE_GB=5

cleanup() { rm -f "$TMP_SNAP"; }
trap cleanup EXIT

# ── Disk-free pre-check (abort+alert if < 5 GB) ──
free_gb="${DISK_FREE_GB_OVERRIDE:-$(df -BG --output=avail "$REPO_ROOT/data" | tail -1 | tr -dc 0-9)}"
if [[ "$free_gb" -lt "$MIN_FREE_GB" ]]; then
  echo "ERROR: only ${free_gb} GB free (< ${MIN_FREE_GB} GB) — aborting backup" >&2
  exit 1
fi

[[ -f "$DB_PATH" ]] || { echo "ERROR: db not found at $DB_PATH" >&2; exit 1; }

# ── flock: serialize against the --apply path + restore-test ──
exec 9>"$LOCK"
flock 9

touch "$SENTINEL"
remove_sentinel() { rm -f "$SENTINEL"; }

sqlite3 "$DB_PATH" "PRAGMA wal_checkpoint(TRUNCATE);"
sqlite3 "$DB_PATH" ".backup '$TMP_SNAP'"

# ── integrity BEFORE upload (never overwrite good with corrupt) ──
res="$(sqlite3 "$TMP_SNAP" 'PRAGMA integrity_check;' 2>&1)"
if [[ "$res" != "ok" ]]; then
  echo "ERROR: integrity_check failed: $res" >&2
  remove_sentinel
  exit 1
fi

# ── per-table row counts → object metadata ──
count() { sqlite3 "$TMP_SNAP" "SELECT count(*) FROM $1;" 2>/dev/null || echo 0; }
META="rows-transactions=$(count transactions),rows-audit_events=$(count audit_events),rows-invoices=$(count invoices),cutover-ts=${CUTOVER_TS:-},sha256=$(sha256sum "$TMP_SNAP" | cut -d' ' -f1)"

OBJECT_KEY="daily/accounting-${TS}.db"

if [[ "${R2_DISABLE:-0}" == "1" ]]; then
  echo "[backup] R2_DISABLE=1 — skipping upload (test mode). meta: $META"
else
  # Upload to R2 (aws s3 CLI w/ R2 endpoint, or rclone). Capture etag.
  upload_etag="$(aws s3api put-object \
      --endpoint-url "$R2_ENDPOINT" --bucket "$R2_BUCKET" --key "$OBJECT_KEY" \
      --body "$TMP_SNAP" --metadata "$META" --query ETag --output text)"
  local_md5="\"$(md5sum "$TMP_SNAP" | cut -d' ' -f1)\""
  if [[ "$upload_etag" != "$local_md5" ]]; then
    echo "ERROR: R2 etag mismatch (got $upload_etag, want $local_md5)" >&2
    remove_sentinel
    exit 1
  fi
  # Retention prune: daily 14d + weekly 8w (implement via lifecycle policy on the
  # bucket OR an aws s3 ls + delete pass keyed on object date — see Phase 4 step 12).
  # Dead-man ping on success.
  [[ -n "${HEALTHCHECK_PING_URL:-}" ]] && curl -fsS --max-time 10 "$HEALTHCHECK_PING_URL" >/dev/null || true
fi

remove_sentinel
echo "[backup] OK: $OBJECT_KEY"
```

*(`R2_ENDPOINT`/`R2_BUCKET`/`R2_BACKUP_WRITE_TOKEN`/`HEALTHCHECK_PING_URL` come from `accounting/srv` via `doppler run`. The `aws` CLI uses the R2 token as S3 creds; rclone is an acceptable substitute — keep the etag/md5 verify either way. The lifecycle retention can be a bucket lifecycle rule instead of in-script pruning; document the choice in Phase 4.)*

- [ ] **Step 4: Run to verify it passes**

Run: `pytest scripts/test_backup_sh.py -v`
Expected: PASS. (Live R2 etag verified in Phase 1 R2 dry-run + Phase 4 step 12.)

- [ ] **Step 5: Commit**

```bash
git add scripts/backup.sh scripts/test_backup_sh.py
git commit -m "feat(backup): disk-gate, flock, integrity-before-upload, R2 etag verify, sentinel (REQ-HM-006)"
```

---

### Task A10: `scripts/backup_restore_test.py` — weekly restore + row-count oracle (REQ-HM-006)

**Files:**
- Create: `scripts/backup_restore_test.py`
- Test: `scripts/test_backup_restore_test.py`

- [ ] **Step 1: Write the failing test**

```python
# scripts/test_backup_restore_test.py
"""REQ-HM-006: restore-test oracle — object-vs-own-metadata + monotonic + sentinel skip."""
import sqlite3
from pathlib import Path

import pytest

from scripts import backup_restore_test as brt


def _db_with_counts(p, tx, ae, inv):
    con = sqlite3.connect(p)
    for name, n in (("transactions", tx), ("audit_events", ae), ("invoices", inv)):
        con.execute(f"CREATE TABLE {name} (id INTEGER PRIMARY KEY)")
        con.executemany(f"INSERT INTO {name} (id) VALUES (?)", [(i,) for i in range(n)])
    con.commit()
    con.close()


def test_passes_when_counts_match_metadata(tmp_path):
    p = tmp_path / "snap.db"
    _db_with_counts(p, 10, 20, 3)
    meta = {"rows-transactions": 10, "rows-audit_events": 20, "rows-invoices": 3}
    assert brt.verify_object(p, meta, prior={"rows-transactions": 9}) is True


def test_fails_when_counts_mismatch_metadata(tmp_path):
    p = tmp_path / "snap.db"
    _db_with_counts(p, 10, 20, 3)
    meta = {"rows-transactions": 999, "rows-audit_events": 20, "rows-invoices": 3}
    assert brt.verify_object(p, meta, prior=None) is False


def test_fails_on_non_monotonic(tmp_path):
    p = tmp_path / "snap.db"
    _db_with_counts(p, 5, 20, 3)
    meta = {"rows-transactions": 5, "rows-audit_events": 20, "rows-invoices": 3}
    assert brt.verify_object(p, meta, prior={"rows-transactions": 10}) is False


def test_sentinel_causes_skip(tmp_path):
    (tmp_path / ".backup.in-progress").touch()
    assert brt.should_skip(tmp_path) is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest scripts/test_backup_restore_test.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# scripts/backup_restore_test.py
"""Weekly R2 restore + integrity test (REQ-HM-006).

Downloads the latest daily R2 object (>= 30 min old), runs PRAGMA
integrity_check, and a row-count oracle: the object's actual table counts must
equal its OWN recorded R2 metadata (object-to-self, not the moving live DB) AND
be monotonically >= the previous successful backup. Alerts on mismatch; skips
silently if a backup is in progress (sentinel)."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_TABLES = ("transactions", "audit_events", "invoices")


def should_skip(data_dir: Path) -> bool:
    return (data_dir / ".backup.in-progress").exists()


def _count(db: Path, table: str) -> int:
    con = sqlite3.connect(db)
    try:
        return int(con.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
    finally:
        con.close()


def _integrity_ok(db: Path) -> bool:
    con = sqlite3.connect(db)
    try:
        return con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        con.close()


def verify_object(db: Path, meta: dict, prior: dict | None) -> bool:
    if not _integrity_ok(db):
        return False
    for table in _TABLES:
        actual = _count(db, table)
        recorded = int(meta.get(f"rows-{table}", -1))
        if actual != recorded:
            return False
        if prior is not None and f"rows-{table}" in prior:
            if actual < int(prior[f"rows-{table}"]):
                return False  # non-monotonic shrink → suspicious
    return True


def main(argv: list[str] | None = None) -> int:
    # Box wiring (Phase 2): resolve repo root, skip on sentinel, download latest
    # daily object >= 30 min old + its metadata + prior backup's metadata from R2,
    # call verify_object(), and invoke scripts.alert on failure.
    repo_root = Path(__file__).resolve().parents[1]
    data_dir = repo_root / "data"
    if should_skip(data_dir):
        print("[restore-test] backup in progress — skipping (no alert)")
        return 0
    # R2 download + metadata fetch implemented against accounting/srv creds.
    # ... (download newest daily object to tmp; read its metadata; read prior) ...
    raise SystemExit("box-only: implement R2 download wiring in Phase 2")


if __name__ == "__main__":
    sys.exit(main())
```

*(The pure oracle (`verify_object`/`should_skip`/counts) is unit-tested here. The R2 download wiring is finalized + smoke-tested live against a real object in Phase 2; the `main()` stub raises so the timer is not enabled until the wiring lands.)*

- [ ] **Step 4: Run to verify it passes**

Run: `pytest scripts/test_backup_restore_test.py -v && mypy scripts/backup_restore_test.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/backup_restore_test.py scripts/test_backup_restore_test.py
git commit -m "feat(backup): restore-test row-count oracle + sentinel skip (REQ-HM-006)"
```

---

### Task A11: `scripts/alert.py` — Resend OnFailure handler (REQ-HM-014)

**Files:**
- Create: `scripts/alert.py`
- Test: `scripts/test_alert.py`

- [ ] **Step 1: Write the failing test**

```python
# scripts/test_alert.py
"""REQ-HM-014: one Resend email per failed unit per hour (dedup), no recursion."""
from pathlib import Path
from unittest.mock import MagicMock

from scripts import alert as alert_mod


def test_sends_once_then_dedups(monkeypatch, tmp_path):
    sent = []
    fake = MagicMock()
    fake.emails.send.side_effect = lambda payload: sent.append(payload) or {"id": "e1"}
    monkeypatch.setattr(alert_mod, "_resend_client", lambda: fake)
    monkeypatch.setattr(alert_mod, "_sentinel_dir", lambda: tmp_path)
    monkeypatch.setenv("ALERT_HOUR_OVERRIDE", "2026060114")

    rc1 = alert_mod.send_alert("accounting-api.service")
    rc2 = alert_mod.send_alert("accounting-api.service")  # same unit+hour → skip
    assert rc1 == 0 and rc2 == 0
    assert len(sent) == 1  # deduped


def test_distinct_units_both_send(monkeypatch, tmp_path):
    sent = []
    fake = MagicMock()
    fake.emails.send.side_effect = lambda payload: sent.append(payload) or {"id": "e"}
    monkeypatch.setattr(alert_mod, "_resend_client", lambda: fake)
    monkeypatch.setattr(alert_mod, "_sentinel_dir", lambda: tmp_path)
    monkeypatch.setenv("ALERT_HOUR_OVERRIDE", "2026060114")
    alert_mod.send_alert("accounting-api.service")
    alert_mod.send_alert("caddy.service")
    assert len(sent) == 2
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest scripts/test_alert.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# scripts/alert.py
"""systemd OnFailure handler → one Resend email naming the failed unit.

REQ-HM-014. Hourly per-unit dedup via /tmp sentinel; exits non-zero only on a
real send failure. This unit has NO OnFailure= itself (breaks alert recursion)."""
from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

_TO = "sparkst@gmail.com"
_FROM = "alerts@sparkry.ai"


def _resend_client():
    import resend

    resend.api_key = os.environ["RESEND_API_KEY"]
    return resend


def _sentinel_dir() -> Path:
    return Path("/tmp")


def _hour() -> str:
    return os.environ.get("ALERT_HOUR_OVERRIDE") or datetime.now(UTC).strftime("%Y%m%d%H")


def send_alert(unit: str) -> int:
    sentinel = _sentinel_dir() / f"alert-{unit}-{_hour()}.sent"
    if sentinel.exists():
        print(f"[alert] already sent for {unit} this hour — skipping")
        return 0
    try:
        client = _resend_client()
        client.emails.send(
            {
                "from": _FROM,
                "to": _TO,
                "subject": f"[accounting/hetzner] unit failed: {unit}",
                "text": f"systemd reported a failure for {unit} on the Hetzner box at {_hour()} UTC.",
            }
        )
    except Exception as exc:  # noqa: BLE001 — surface send failure to systemd
        print(f"[alert] send failed for {unit}: {exc}", file=sys.stderr)
        return 1
    sentinel.touch()
    print(f"[alert] sent for {unit}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: alert.py <unit-name>", file=sys.stderr)
        return 2
    return send_alert(argv[0])


if __name__ == "__main__":
    sys.exit(main())
```

*(`resend` is invoked via the Resend Python SDK already used elsewhere in the repo; confirm it is importable in the venv. If the repo uses an HTTP call instead of the SDK, mirror that. The systemd unit runs `doppler run -- .venv/bin/python3 -m scripts.alert "%i"`.)*

- [ ] **Step 4: Run to verify it passes**

Run: `pytest scripts/test_alert.py -v && mypy scripts/alert.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/alert.py scripts/test_alert.py
git commit -m "feat(monitoring): Resend OnFailure alert handler with hourly dedup (REQ-HM-014)"
```

---

### Task A12: `scripts/disk_check.sh` — external disk-free script (REQ-HM-014)

**Files:**
- Create: `scripts/disk_check.sh`
- Test: `scripts/test_disk_check.py`

- [ ] **Step 1: Write the failing test**

```python
# scripts/test_disk_check.py
"""REQ-HM-014: external script (NOT inline ExecStart) exits 1 below 5 GB."""
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "disk_check.sh"


def test_exits_0_when_ample(monkeypatch):
    r = subprocess.run(["sh", str(SCRIPT)], capture_output=True, text=True,
                       env={**os.environ, "DISK_FREE_GB_OVERRIDE": "50"})
    assert r.returncode == 0


def test_exits_1_when_constrained():
    r = subprocess.run(["sh", str(SCRIPT)], capture_output=True, text=True,
                       env={**os.environ, "DISK_FREE_GB_OVERRIDE": "3"})
    assert r.returncode == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest scripts/test_disk_check.py -v`
Expected: FAIL (script missing).

- [ ] **Step 3: Implement**

```sh
#!/bin/sh
# disk_check.sh — exit 1 if < 5 GB free on the accounting data dir (REQ-HM-014).
# External script because systemd's ExecStart C-tokenizer mangles inline shell.
DIR="${ACCOUNTING_DATA_DIR:-/home/travis/accounting/data}"
free="${DISK_FREE_GB_OVERRIDE:-$(df -BG --output=avail "$DIR" | tail -1 | tr -dc 0-9)}"
[ "$free" -ge 5 ] || { echo "disk low: ${free} GB free on $DIR" >&2; exit 1; }
echo "disk ok: ${free} GB free on $DIR"
```

Then `chmod +x scripts/disk_check.sh`.

- [ ] **Step 4: Run to verify it passes**

Run: `chmod +x scripts/disk_check.sh && pytest scripts/test_disk_check.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/disk_check.sh scripts/test_disk_check.py
git commit -m "feat(monitoring): external disk_check.sh for disk-pressure timer (REQ-HM-014)"
```

---

### Task A13: `flock` serialization in plaid_transactions_sync (--apply) (REQ-HM-009)

The `--apply` path must hold `data/.backup.lock` from BEFORE the write transaction until AFTER commit, so the Phase-5 pre-apply backup never snapshots a half-applied batch.

**Files:**
- Modify: `scripts/plaid_transactions_sync.py:52-55` (wrap the apply write)
- Test: `scripts/test_plaid_sync_flock.py`

- [ ] **Step 1: Write the failing test**

```python
# scripts/test_plaid_sync_flock.py
"""REQ-HM-009: during --apply the backup lock is held across the write."""
import fcntl
from pathlib import Path
from unittest.mock import MagicMock

import scripts.plaid_transactions_sync as sync_mod


def test_apply_holds_lock_across_write(monkeypatch, tmp_path):
    lock = tmp_path / ".backup.lock"
    monkeypatch.setattr(sync_mod, "_backup_lock_path", lambda: lock)
    monkeypatch.setattr(sync_mod, "init_db", lambda: None)
    monkeypatch.setattr(sync_mod, "make_plaid_client", lambda: MagicMock())

    held = {"during_write": None}

    def fake_sync_all_active(session, client, dry_run):
        # While the write runs, a non-blocking lock attempt must FAIL.
        with open(lock, "w") as probe:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held["during_write"] = False
                fcntl.flock(probe, fcntl.LOCK_UN)
            except BlockingIOError:
                held["during_write"] = True
        b = MagicMock()
        b.items = []
        b.total_added = b.total_reactivated = b.total_modified = 0
        b.total_removed = b.total_failed = b.total_superseded = 0
        return b

    monkeypatch.setattr(sync_mod, "sync_all_active", fake_sync_all_active)
    monkeypatch.setattr(sync_mod, "SessionLocal", lambda: MagicMock())

    sync_mod.main(["--apply"])
    assert held["during_write"] is True  # lock held during the write
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest scripts/test_plaid_sync_flock.py -v`
Expected: FAIL (no flock held; `_backup_lock_path` missing).

- [ ] **Step 3: Implement**

Add near the top of `scripts/plaid_transactions_sync.py`:

```python
import fcntl
from contextlib import contextmanager
from pathlib import Path


def _backup_lock_path() -> Path:
    return Path(__file__).resolve().parents[1] / "data" / ".backup.lock"


@contextmanager
def _backup_lock():
    """Hold data/.backup.lock EX across the entire apply write (acquire-before-begin,
    release-after-commit) so the pre-apply backup can't snapshot a partial batch."""
    path = _backup_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()
```

Wrap the write site (currently lines 52-55) so the lock is held only for `--apply`:

```python
    init_db()
    client = make_plaid_client()
    if args.apply:
        with _backup_lock(), SessionLocal() as session:
            batch = sync_all_active(session, client=client, dry_run=False)
    else:
        with SessionLocal() as session:
            batch = sync_all_active(session, client=client, dry_run=True)
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest scripts/test_plaid_sync_flock.py -v && ruff check scripts/ && mypy scripts/plaid_transactions_sync.py`
Expected: PASS.

- [ ] **Step 5: Commit + push all of Part A**

```bash
git add scripts/plaid_transactions_sync.py scripts/test_plaid_sync_flock.py
git commit -m "feat(plaid): hold backup flock across --apply write (REQ-HM-009)"
# Full Part A gate:
pytest && ruff check src/ scripts/ && mypy src/
git push -u origin feat/accounting-hetzner-migration
```

**Part A acceptance:** `pytest && ruff check src/ scripts/ && mypy src/` all green; branch pushed. No box work begins until this is true.

---

# PART B — Ops runbook (on the box; verification-gated, no repo edits)

> Each step is a command + expected output. BLOCKING gates are marked 🔒. The executor STOPS at operator PAUSE points.

---

## B-Phase 0 — Operator pre-flight (PAUSE) + box snapshot

- [ ] **OPERATOR (NOW):** delete `/Users/travis/SGDrive/dev/accounting/.env`; rotate `RESEND_API_KEY` + `STRIPE_RESTRICTED_KEY`. Confirm done.
- [ ] **OPERATOR:** confirm P-1 (CF DNS for `sparkry.ai`), P-2 (Zero Trust org + Google IdP), P-3 (R2 bucket + `CLOUDFLARE_API_TOKEN`/`CLOUDFLARE_ACCOUNT_ID`), P-4 (`collab` is a separate user), P-5 (Syncthing roots).
- [ ] Take a **Hetzner Cloud snapshot** of `ubuntu-4gb-nbg1-2` (full-box rollback baseline) before any change.
- [ ] SSH over Tailscale: `ssh travis@<BOX_TAILNET_HOST>`; record `free -m`, `systemctl status` of the collab sandbox + Syncthing (footprint for the §7 MemoryMax budget).

## B-Phase 1 — Provision app on the box

- [ ] `sudo timedatectl set-timezone UTC && timedatectl status` → `Time zone: UTC`.
- [ ] Doppler EnvironmentFile: `sudo mkdir -p /etc/accounting`; write `/etc/accounting/doppler.env` containing `DOPPLER_TOKEN=<DOPPLER_SRV_TOKEN>`; `sudo chown root:root /etc/accounting/doppler.env && sudo chmod 600 /etc/accounting/doppler.env`; verify `stat -c '%a %U' /etc/accounting/doppler.env` → `600 root`.
- [ ] **OPERATOR / executor:** create the Doppler `accounting/srv` config seeded from `accounting/dev` PLUS: `CLOUDFLARE_API_TOKEN` (R2 Storage Read+Write, mirrored from `accounting/prd` — NOT the wealth-scoped `R2_BACKUP_WRITE_TOKEN`, the wrong credential per the design note), `CLOUDFLARE_ACCOUNT_ID`, `R2_BUCKET`, `PLAID_ENV=production`, `PLAID_REDIRECT_URI=https://books.sparkry.ai/admin/connections/oauth-return`, `API_KEY` (`openssl rand -hex 32`), `INGEST_API_KEY` (`openssl rand -hex 32`, **distinct**), `VITE_API_KEY` (== `API_KEY`), `ANTHROPIC_API_KEY`, `PLAID_FERNET_KEY` (the key the migrated tokens were encrypted under), `GMAIL_N8N_DIRS`, `DEDUCTION_DIR`, `ATTACHMENT_ROOTS`, `RECEIPTS_ROOT`, `HEALTHCHECK_PING_URL`. Verify `openssl rand`-strength + `API_KEY != INGEST_API_KEY`.
- [ ] Pin + install Doppler CLI (versioned). Verify `doppler run -- env | grep DOPPLER_TOKEN` returns empty in the child (token stripped).
- [ ] `rsync -av --exclude='.env' --exclude='.venv' --exclude='node_modules' --exclude='data/' /Users/travis/SGDrive/dev/accounting/ travis@<BOX_TAILNET_HOST>:/home/travis/accounting/`
- [ ] On box: `cd /home/travis/accounting && python3.12 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"`.
- [ ] Create Hetzner-local dirs matching the path env vars: `mkdir -p data/inbox data/review data/deductions data/receipts reports`.
- [ ] 🔒 **Quality gates on the box:** `pytest && ruff check src/ scripts/ && mypy src/` all green (REQ-HM-011). (If any fail: fix on the Mac, commit, push, re-rsync — never hand-edit the box.)
- [ ] Build dashboard with the key baked in: `cd dashboard && doppler run --config srv -- npm run build`, then `chmod -R o-r build/`. 🔒 **Post-build grep (REQ-HM-002):** assert the compiled bundle contains the baked key and no un-substituted `import.meta.env.VITE_API_KEY`: `grep -rl "$(doppler run --config srv -- printenv VITE_API_KEY)" build/` returns ≥1 file. If empty → the build had no `VITE_API_KEY`; fix `accounting/srv` and rebuild.
- [ ] Install Caddy + cloudflared (both absent): per the pinned install steps. Do NOT create the tunnel yet (Phase 3).
- [ ] 🔒 **R2 precondition (REQ-HM-006):** dry-run write a throwaway object → GET → integrity-check (it's a tiny sqlite) → delete; confirm bucket + read+write token. Record bucket + token source.
- [ ] 🔒 **Syncthing gate (REQ-HM-016):** `syncthing cli config folders list` (or inspect config) — confirm NO share root is a parent of `/home/travis/accounting/data`. Assert no `.stfolder` / `.sync-conflict-*` under `data/`. If a share covers it: relocate repo to `/opt/accounting` OR add a Syncthing ignore.
- [ ] 🔒 **Collab gate (P-4):** confirm the agentic-collab sandbox runs as `<COLLAB_USER>` (not `travis`): `ps -o user= -p $(pgrep -f agentic-collab | head -1)` → `<COLLAB_USER>`.

## B-Phase 2 — systemd services, timers, security controls

> Author every unit, journald config, and the `reports/` dir. Then run the 🔒 5-point security gate. **Phase 3 cannot begin until that gate is green.**

- [ ] Write `/etc/systemd/system/accounting-api.service` (per spec §Phase 2): `User=travis`, `WorkingDirectory=/home/travis/accounting`, `EnvironmentFile=/etc/accounting/doppler.env`, `ExecStart=doppler run -- .venv/bin/uvicorn src.api.main:app --host 127.0.0.1 --port 8000 --log-level warning`, `Restart=always`, `RestartSec=10`, `StartLimitIntervalSec=120`, `StartLimitBurst=5`, `MemoryMax=512M`, `ProtectSystem=strict`, `ReadWritePaths=/home/travis/accounting/data`, `NoNewPrivileges=yes`, `PrivateTmp=yes`, `TimeoutStartSec=90`, `OnFailure=accounting-alert@%n.service`, `ExecStartPost=/bin/sh -c 'for i in $(seq 30); do curl -sf http://127.0.0.1:8000/api/health/ping && exit 0; sleep 2; done; exit 1'`.
- [ ] Write `accounting-dashboard.service`: `WorkingDirectory=/home/travis/accounting/dashboard`, `ExecStart=doppler run -- node node_modules/.bin/vite preview --host 127.0.0.1 --port 5173`, `After=accounting-api.service`, `MemoryMax=384M`, `RestartSec=10`, `StartLimit*`, `ProtectSystem=strict`, `ReadWritePaths=/home/travis/accounting/dashboard`, `NoNewPrivileges=yes`, `PrivateTmp=yes`, `OnFailure=…`.
- [ ] Write `caddy.service`: `ExecStart=caddy run --config /home/travis/accounting/Caddyfile`, `After=accounting-api.service accounting-dashboard.service`, `MemoryMax=64M`, `RestartSec=5`, `OnFailure=…`. (Caddyfile authored in Phase 3; listener `:9000`, `admin off`.)
- [ ] cloudflared installed in Phase 3 via `cloudflared service install` (root-owned unit). Drop-in `/etc/systemd/system/cloudflared.service.d/override.conf`: `Restart=always`, `RestartSec=5`, `StartLimit*`, `MemoryMax=128M`, `After=caddy.service network-online.target`, `OnFailure=accounting-alert@%n.service`. (Created Phase 3; listed here for the unit inventory.)
- [ ] Write `accounting-backup.timer` + `.service` (`OnCalendar=*-*-* 03:17:00 UTC`, `Persistent=true`, `ExecStart=doppler run -- scripts/backup.sh`, `OnFailure=…`).
- [ ] Write `accounting-backup-restore-test.timer` + `.service` (`OnCalendar=Sun *-*-* 07:00:00 UTC`, `Persistent=true`, `Conflicts=accounting-backup.service`, `ExecStart=doppler run -- .venv/bin/python3 scripts/backup_restore_test.py`, `OnFailure=…`). **Finalize the R2 download wiring in `backup_restore_test.py` `main()` here, smoke-test against a real object, commit+push+re-rsync, THEN enable the timer.**
- [ ] Write `accounting-disk-check.timer` + `.service` (`OnCalendar=*-*-* 00/6:00:00 UTC`, `ExecStart=/home/travis/accounting/scripts/disk_check.sh`, `OnFailure=…`).
- [ ] Write `weekly-pl-report.timer` + `.service` (`OnCalendar=Mon *-*-* 06:00:00 UTC`, `Persistent=true`, `ExecStart=doppler run -- .venv/bin/python3 scripts/weekly-pl-report.py`, `OnFailure=…`).
- [ ] Write `plaid-transactions-sync.timer` + `.service` (`doppler run --`, `Persistent=true`) — **install but DO NOT enable** (Phase 5).
- [ ] Write `accounting-alert@.service`: `EnvironmentFile=/etc/accounting/doppler.env`, `User=travis`, `WorkingDirectory=/home/travis/accounting`, `ExecStart=doppler run -- .venv/bin/python3 -m scripts.alert "%i"`, `StartLimitIntervalSec=300`, `StartLimitBurst=3`, and **NO `OnFailure=`** (breaks alert recursion).
- [ ] journald: create the **drop-in** `/etc/systemd/journald.conf.d/accounting.conf` (preserves package defaults across upgrades) with `SystemMaxUse=500M`, `SystemKeepFree=5G`, plus `RuntimeMaxUse=64M`, `RuntimeKeepFree=256M` (cap the in-RAM journal on the 4 GB box); `systemctl restart systemd-journald`.
- [ ] `systemctl daemon-reload`; `systemctl enable --now accounting-api accounting-dashboard` (caddy after Phase 3 Caddyfile). Enable timers EXCEPT `plaid-transactions-sync`.
- [ ] **nftables OUTPUT rule (§7):** use a **dedicated table** `inet accounting_collab` (NOT the shared `inet filter`) so the rule can never collide with ufw or fail2ban tables. Write `/etc/nftables.conf` as a non-flushing, idempotent file — do **NOT** use `nft list ruleset > /etc/nftables.conf` (that captures ufw/fail2ban's tables, and the default config's `flush ruleset` would wipe them at boot):
  ```
  #!/usr/sbin/nft -f
  # collab UID: verify `id -u <COLLAB_USER>` == the value below before loading.
  add table inet accounting_collab
  delete table inet accounting_collab
  table inet accounting_collab {
      chain output {
          type filter hook output priority 0; policy accept;
          oifname "lo" skuid <COLLAB_UID> tcp dport { 8000, 5173, 9000, 8384 } drop
      }
  }
  ```
  (8384 = Syncthing GUI, also a travis-owned loopback service.) Apply + persist: `nft -f /etc/nftables.conf && systemctl enable --now nftables`. `nft -f` runs as one atomic transaction — no open window. Verify ufw + fail2ban tables survive: `nft list tables` still shows `ip filter` / `inet f2b-table`.
- [ ] **Firewall (§7):** ufw + Hetzner Cloud Firewall deny inbound 8000/5173/9000 from all except `<TAILSCALE_SUBNET>`.

- [ ] 🔒 **PHASE-2 SECURITY GATE (all 5 must pass before Phase 3):**
  1. **Boot assertion (4 cases):** with `PLAID_ENV=production` + empty `API_KEY` → API refuses to boot (journal `RuntimeError`); + empty `INGEST_API_KEY` → refuses; + equal keys → refuses; + two distinct strong keys → boots. (`journalctl -u accounting-api` shows the RuntimeError in the fail cases.)
  2. **Loopback binds:** `ss -tlnp` shows 8000/5173/9000 on `127.0.0.1` only (never `0.0.0.0`).
  3. **collab denied:** `su -s /bin/sh <COLLAB_USER> -c "curl --max-time 3 -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/health/ping"` → connection error/timeout/non-200. Repeat 5173, 9000.
  4. **No public app ports:** from outside the tailnet, `nmap -p 8000,5173,9000 <BOX_PUBLIC_IP>` → filtered/closed.
  5. **nftables persisted:** `systemctl is-enabled nftables` → `enabled`; OUTPUT skuid rule present in `nft list ruleset` (ideally re-verified after a reboot).

## B-Phase 3 — Public access (Cloudflare Tunnel + Access)

- [ ] `cloudflared tunnel create books-accounting`; `cloudflared tunnel route dns books-accounting books.sparkry.ai`.
- [ ] Write `/home/travis/accounting/Caddyfile` exactly per spec §Phase 3 (`admin off`; `http://127.0.0.1:9000`; `/reports/*` file_server `browse off` rooted at `/home/travis/accounting`; `/api/*` → `127.0.0.1:8000`; `handle` → `127.0.0.1:5173` with `Cache-Control: no-store`). `systemctl enable --now caddy`. Install the cloudflared drop-in from Phase 2.
- [ ] cloudflared `config.yml` ingress: `books.sparkry.ai → http://127.0.0.1:9000`. `cloudflared service install`; apply the drop-in; `systemctl enable --now cloudflared`.
- [ ] **OPERATOR:** create CF Access application + policy on `books.sparkry.ai` (Google OAuth, allowed emails). Create the two **service-token** policies: one scoped to `/api/ingest/*` (n8n), one scoped to `/api/health/ping` (uptime checker). **No public bypass.**
- [ ] **External uptime monitor (REQ-HM-014):** deploy a CF cron Worker (every 5 min) that fetches `https://books.sparkry.ai/api/health/ping` with `Cf-Access-Client-Id`/`Cf-Access-Client-Secret`, pings a healthcheck.io dead-man on 200, emails on non-200.
- [ ] **Acceptance:**
  - `curl -I -H 'Host: books.sparkry.ai' http://127.0.0.1:9000/` → 200 (Caddy → vite via `allowedHosts`/`preview.host`).
  - Authenticated browser (or `curl` w/ service token) to `https://books.sparkry.ai/` → app after Access challenge.
  - `curl -s -o /dev/null -w '%{http_code}' https://books.sparkry.ai/admin/connections/oauth-return` → **302** (CF Access redirect — correct, not a failure).
  - `systemctl is-enabled accounting-cloudflared caddy accounting-api accounting-dashboard` → all `enabled`.

## B-Phase 4 — DB cutover (reversible until PONR)

- [ ] **Step 0 — baseline:** integrity-checked pre-cutover snapshot of the **Mac** DB → R2 (labeled), keep SGDrive copy. Record sha256.
- [ ] **Step 1 — unload DIRECT-DB writers on the Mac BY NAME FIRST:** `com.sparkry.plaid-transactions-sync` (if present), `com.sparkry.plaid-balance-sync`, `com.sparkry.accounting-prices-daily`, `com.sparkry.accounting-backup`, `com.sparkry.weekly-pl-report` — `launchctl bootout gui/$(id -u)/<label>` / unload each plist.
- [ ] **Step 2 — stop inbound writes:** unload Mac `com.sparkry.caddy-accounting` + `com.sparkry.accounting-api`; pause/disable the n8n workflow (or revoke its webhook). (Stripe is pull-only — no webhook re-point.)
- [ ] **Step 3 — confirm writers unloaded (per-label, NOT blanket grep):** for each writer label, `launchctl list | grep -F <label>` empty. Do NOT assert `grep com.sparkry` is empty (`com.sparkry.agentic-collab-proxy` stays loaded).
- [ ] **Step 4 — drain:** API + Caddy access logs show no new requests for ≥ 30 s; `lsof data/accounting.db` shows no writer.
- [ ] **Step 5:** `sqlite3 data/accounting.db "PRAGMA wal_checkpoint(TRUNCATE);"`; confirm `-wal` empty.
- [ ] **Step 6:** `sqlite3 data/accounting.db ".backup /tmp/cutover.db"`; record sha256 + per-table counts for the canonical set (`transactions`, `audit_events`, `invoices`, `invoice_line_items`, `customers`, `vendor_rules`, `plaid_item`).
- [ ] **Step 7:** `rsync /tmp/cutover.db` over Tailscale → `/home/travis/accounting/data/accounting.db`.
- [ ] 🔒 **Step 8 — 4-gate verify (all must pass or ABORT → re-enable Mac, no harm):** sha256(received) == source; `PRAGMA integrity_check == 'ok'`; per-table counts == Mac; `alembic current == head`. **Capture `CUTOVER_TS`** = `date -u +'%Y-%m-%d %H:%M:%S'` (space-separated, SQLite storage format — NOT ISO `T...Z`), into the runbook + the step-9 object metadata. Confirm box clock UTC.
- [ ] 🔒 **Step 9 — R2 baseline BEFORE any write:** services not yet serving → `sudo systemctl start accounting-backup.service` (self-contained). Confirm the object is etag-verified with `CUTOVER_TS` + per-table metadata. *(This is the clean offsite copy that exists at the instant of PONR.)*
- [ ] ⛔ **Step 10 — PONR: n8n ingest cutover (FIRST HETZNER WRITE).** Update the n8n HTTP node URL → `https://books.sparkry.ai/api/ingest/run`; add `X-Api-Key: <INGEST_API_KEY>` + `Cf-Access-Client-Id`/`Cf-Access-Client-Secret`; re-enable; send a test email; confirm `/api/ingest/run` → 200. **After this, rollback is forward-recovery only — never revert to the Mac.**
- [ ] **Step 11 — read smoke + start serving:** start remaining services; login via Access; load dashboard; read a few transactions (no writes).
- [ ] 🔒 **Step 12 — enable recurring backup timer:** `systemctl enable --now accounting-backup.timer`; verify the first scheduled backup lands in R2 (etag verified). Finalize retention (R2 lifecycle rule: daily 14d + weekly 8w, or in-script prune). BLOCKING before the Mac standby window may end.
- [ ] **Step 13 — docs (REQ-HM-013):** update `CLAUDE.md` Local Deployment section (Hetzner host, systemd units, R2 backup, `accounting/srv`, `books.sparkry.ai`; correct weekly-pl to "writes `reports/weekly-pl-latest.txt`"; remove stale launchd/Caddy/Tailscale-only text). Commit + push + re-rsync (or pull on box).
- [ ] **72-hour soak:** Mac DB stays frozen/read-only (launchd unloaded; do NOT auto-re-enable writers).
- [ ] **After soak (REQ-HM-012):** unload the eight enumerated accounting/wealth labels by name; per-label `launchctl list | grep -F <label>` empty; assert `com.sparkry.agentic-collab-proxy` STILL present. Retire SGDrive backup after R2 verified.

## B-Phase 5 — Plaid go-live (connect Chase)

- [ ] Confirm A8 `redirect_uri` code is deployed on the box (it shipped in Part A; verify present in `src/api/routes/plaid.py` on the box).
- [ ] **OPERATOR:** register `https://books.sparkry.ai/admin/connections/oauth-return` in the **production** Plaid app's Allowed Redirect URIs; confirm `transactions` product approved.
- [ ] **CF Access verify (REQ-HM-008):** complete a **sandbox** OAuth round-trip WITHOUT a bypass first (CF-Access-authenticated browser). Acceptance: a `PlaidItem` row with a real `item_id` appears. Add the GET-only scoped bypass for `/admin/connections/oauth-return*` ONLY if the redirect is blocked mid-flow; if added, confirm `oauth_state_id`/`oauth_token` are not logged at the edge.
- [ ] **OPERATOR:** connect Chase (Sparkry + BlackLine) via `https://books.sparkry.ai/admin/connections`; **operator enters Chase credentials**.
- [ ] Map accounts → entity + **unique** `payment_method`.
- [ ] 🔒 **DRY-RUN gate (REQ-HM-009):** run the transactions sync dry-run; operator reviews — superseded count sane vs confirmed `bank_csv` rows in range; `payment_method` labels match CSV history; covered_min/max per account verified. A non-zero superseded count requires explicit sign-off.
- [ ] **Serialize + pre-apply backup:** `systemctl stop accounting-backup.timer` (re-enable after) OR rely on the shared `data/.backup.lock`. Take an on-demand R2 backup to a **distinct immutable key** `pre-plaid-apply-<ts>.db`.
- [ ] ⛔ **`--apply`** (Plaid-write PONR): `doppler run -- python -m scripts.plaid_transactions_sync --apply`.
- [ ] `systemctl enable --now plaid-transactions-sync.timer`.

---

## Rollback / Disaster Recovery (spec §9)

- **PONR** = Phase-4 step-10 n8n ingest write; and first Phase-5 `--apply`. Verified R2 baseline (step 9 / `pre-plaid-apply-<ts>.db`) exists before each.
- **Decision is BINARY on the PONR — never on a write-count query.**
  - **Before PONR:** box DB byte-identical to migrated Mac snapshot (sha256 proven in step 8). Revert = re-enable the frozen read-only Mac. Clean.
  - **At/after PONR:** Mac is known-stale. **Forward-recovery ONLY** — (1) fix-forward on the box (then immediate on-demand R2 backup), or (2) restore the box from the newest good R2 object (`integrity_check` + row-count verify BEFORE restart). Do NOT reverse-sync to the Mac; the Mac stays a cold archival copy.
- **Diagnostic write-detector (non-gating, §9 step 2):** survey `transactions`/`invoices`/`plaid_item` on `MAX(created_at,updated_at) > CUTOVER_TS`, `customers` on `created_at`, `audit_events` on `changed_at` — all wrapped in `datetime()` for separator normalization. Known acceptable blind spot: `vendor_rules.last_matched` (re-derivable, non-load-bearing).
- **DR assets:** versioned R2 (daily 14d + weekly 8w) + immutable `pre-plaid-apply-<ts>.db`; weekly `accounting-backup-restore-test.timer`; pre-Phase-1 Hetzner Cloud snapshot; one-click resize on RAM pressure (§7 trigger: OOM in journal or sustained free RAM < 300 MB).

---

## Self-Review (run after writing; fix inline)

- **Spec coverage:** REQ-HM-001 (systemd/boot/ordering) → B-Phase 2; -002 (`accounting/srv` seeding) → B-Phase 1; -003 (`nmap`) → B-Phase 2 gate #4; -004 (4-gate cutover) → B-Phase 4 step 8; -005 (PONR/reversible) → B-Phase 4 + Rollback; -006 (backup/restore-test) → A9/A10 + B-Phase 2/4; -007 (redirect_uri) → A8; -008 (OAuth e2e) → B-Phase 5; -009 (dry-run gate + flock) → A13 + B-Phase 5; -010 (cohabit safely) → B-Phase 2 (nftables/perms/limits); -011 (quality gates + deps) → A1 + B-Phase 1; -012 (Mac unload per-label) → B-Phase 4; -013 (docs) → B-Phase 4 step 13; -014 (monitoring) → A11/A12 + B-Phase 2/3; -015 (fpdf2/jinja2) → A1; -016 (Syncthing) → B-Phase 1; -017 (vite/CORS) → A2/A3; -018 (env paths) → A4. **All 18 mapped.**
- **Placeholder scan:** box facts intentionally placeholdered in the fill-in table (operator choice). The `backup_restore_test.py` / `backup.sh` R2-wiring is explicitly finalized + smoke-tested in B-Phase 2 (flagged, not silent).
- **Type consistency:** `assert_production_secrets`, `require_ingest_api_key`, `health_ping`/`ping_router`, `_plaid_redirect_uri`, `_backup_lock`/`_backup_lock_path`, `verify_object`/`should_skip`, `send_alert` — names consistent between Part A definitions and Part B references.
