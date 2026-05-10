# PLAN — Option 2: Read-only Brokerage API endpoints

**Project:** 005-brokerage-api
**Predecessor:** Option 1 shipped — `src/reports/brokerage_summary.py` exposes pure functions with TypedDict contracts.
**Goal:** Wire those functions to `/api/brokerage/*` endpoints so the dashboard (Option 3) and curl/browser can query them.

---

## Acceptance criteria

1. New router at `src/api/routes/brokerage.py` exposes:
   - `GET /api/brokerage/networth` → `compute_net_worth` output as JSON.
   - `GET /api/brokerage/accounts` → `get_account_summary` output. Includes plan-wrapper accounts with `is_plan_wrapper` flag.
   - `GET /api/brokerage/top-holdings?n=10` → `get_top_holdings` (denominator from compute_net_worth).
   - `GET /api/brokerage/recent-transactions?days=14` → `get_recent_transactions`.
   - `GET /api/brokerage/realized-gl` → `get_realized_gl_summary`.
   - `GET /api/brokerage/data-integrity` → `compute_data_integrity` output (audit endpoint).
2. All endpoints behind the existing `_auth` dependency pattern (router include in `src/api/main.py` with `dependencies=_auth`).
3. Pydantic response models for each endpoint — mirror the TypedDicts so OpenAPI docs are correct. Decimal serialized as `str` (JSON-safe, matches existing convention).
4. Read-only: no endpoint mutates the DB. Tests assert session is clean after each request.
5. Tests in `src/api/test_brokerage_routes.py` cover:
   - Each endpoint with seeded fixture DB.
   - Empty DB → empty containers, no crash.
   - 401 without API key.
   - Query params validated (e.g. `n=-1` → 422).
6. Quality gates: `pytest src/`, `ruff check`, `mypy` clean on new files.
7. Live smoke: `curl http://localhost:8000/api/brokerage/networth -H "X-API-Key: $API_KEY"` returns a JSON object with the live net worth total.

## Out of scope

- Caching / response compression — defer until perf requires it.
- POST/PATCH/DELETE — Option 2 is read-only.
- Per-account drill-down (`/api/brokerage/accounts/{id}`) — defer to Option 3 if dashboard needs it.
- WebSocket / SSE for live updates.

---

## Tasks (TDD — RED first)

### TASK-01 — Test fixture builder (reuse from Option 1)
**File:** `src/api/test_brokerage_routes.py`
**SP:** 1

Use FastAPI `TestClient`. Build a session fixture matching the canonical Option 1 fixture (or import `_seed_fixture` from `src.reports.test_brokerage_summary`). Override `get_db` dependency to yield the test session.

Set up: client with `dependencies` overridden to skip auth for in-test requests OR pass the test API_KEY header. Existing pattern in `src/api/test_*.py` files.

### TASK-02 — Pydantic response models
**File:** `src/api/routes/brokerage.py` (top of file)
**SP:** 1

Define `NetWorthResponse`, `AccountSummaryRow`, `TopHoldingRow`, `RecentTransactionRow`, `RealizedGLByYear`, `RealizedGLSummaryResponse`, `DataIntegrityResponse`. All Decimals serialized as strings (use `field_serializer` or `model_config json_encoders`).

### TASK-03..08 — Six route handlers (failing tests first)
**SP:** 4 total

For each route:
- Write a failing test asserting response shape and key values against the seeded fixture.
- Implement the handler: `Session = Depends(get_db)`, call the pure function from `src.reports.brokerage_summary`, wrap in Pydantic model, return.

### TASK-09 — Wire router into main.py
**SP:** 0.5

Add `app.include_router(brokerage_router, prefix="/api", dependencies=_auth)` to the existing list.

### TASK-10 — Auth + 401 test
**SP:** 0.5

Test that a request without `X-API-Key` header returns 401. Mirror existing pattern.

### TASK-11 — Live smoke
**SP:** 0.5

After service restart, curl each endpoint with the live API key. Capture output to `reports/brokerage-api-smoke.txt`.

---

## SP rollup

Total: ~7 SP.

## Quality gates

- `pytest src/api/test_brokerage_routes.py` — green.
- `pytest src/` — full baseline still green.
- `ruff check src/api/routes/brokerage.py src/api/test_brokerage_routes.py` — clean.
- `mypy src/api/routes/brokerage.py` — clean.
- API restart: `launchctl unload com.sparkry.accounting-api.plist && launchctl load com.sparkry.accounting-api.plist`.

## Open questions for review consensus

1. **Decimal serialization**: project convention. Need to check what `transactions.py` does — likely `str` to avoid float precision loss. Confirm.
2. **Response envelope**: do existing endpoints return `{"data": ..., "meta": ...}` or just the data? Match precedent.
3. **Pagination**: top-holdings and recent-transactions could be paginated (limit/offset) or not. Probably not needed at this scale.
