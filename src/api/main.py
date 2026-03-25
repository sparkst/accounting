"""FastAPI application entry point.

Binds to 127.0.0.1:8000 for local-first operation.
CORS is enabled for the SvelteKit dashboard at localhost:5173.

Startup sequence (lifespan):
  1. init_db() — create tables if missing.
  2. seed_vendor_rules() — populate known-vendor rules if table is empty.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.api.auth import require_api_key
from src.api.routes.attachments import router as attachments_router
from src.api.routes.csv_import import router as csv_import_router
from src.api.routes.health import router as health_router
from src.api.routes.ingest import router as ingest_router
from src.api.routes.invoices import router as invoices_router
from src.api.routes.reconciliation import router as reconciliation_router
from src.api.routes.tax_export import router as tax_export_router
from src.api.routes.tax_year_locks import router as tax_year_locks_router
from src.api.routes.transactions import router as transactions_router
from src.api.routes.vendor_rules import router as vendor_rules_router
from src.classification.seed_rules import seed_vendor_rules
from src.db.connection import SessionLocal, init_db
from src.invoicing.seed_customers import seed_customers

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan handler.

    Runs startup tasks before yielding, then teardown after.
    """
    logger.info("Starting accounting API — initialising database …")
    init_db()
    with SessionLocal() as session:
        inserted = seed_vendor_rules(session)
        if inserted:
            logger.info("Seeded %d vendor rules.", inserted)
        else:
            logger.debug("Vendor rules already seeded; skipping.")
    with SessionLocal() as session:
        counts = seed_customers(session)
        logger.info(
            "seed_customers: %d inserted, %d updated, %d invoices.",
            counts["customers_inserted"],
            counts["customers_updated"],
            counts["invoices_inserted"],
        )
    logger.info("Startup complete.")
    yield
    logger.info("Shutdown complete.")


app = FastAPI(
    title="Accounting System API",
    version="1.0.0",
    description="Cash-basis accounting API for Sparkry AI, BlackLine MTB, and Personal.",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Global exception handlers — prevent traceback leakage (S2-008, S2-009)
# ---------------------------------------------------------------------------


async def _global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch all unhandled exceptions; log with error_id, never expose traceback."""
    error_id = uuid4().hex[:8]
    logger.error("Unhandled error %s: %s", error_id, exc, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "error_id": error_id},
    )


async def _validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Return clean field-level 422 errors without exposing internals."""
    # Pydantic v2 may embed non-serializable objects (e.g. ValueError) in ctx.
    # Stringify those before building the JSON response.
    safe_errors = []
    for err in exc.errors():
        safe_err = {k: v for k, v in err.items() if k != "ctx"}
        if "ctx" in err:
            safe_err["ctx"] = {k: str(v) for k, v in err["ctx"].items()}
        safe_errors.append(safe_err)
    return JSONResponse(
        status_code=422,
        content={"error": "Validation error", "detail": safe_errors},
    )


async def _http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Return the HTTP exception detail as JSON, ensuring no traceback leaks.

    Preserves the {"detail": ...} format that clients and existing tests expect.
    Non-serializable detail values are coerced to strings.
    """
    detail = (
        exc.detail
        if isinstance(exc.detail, (str, int, float, bool, list, dict, type(None)))
        else str(exc.detail)
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": detail},
    )


app.add_exception_handler(Exception, _global_exception_handler)  # type: ignore[arg-type]
app.add_exception_handler(RequestValidationError, _validation_exception_handler)  # type: ignore[arg-type]
app.add_exception_handler(HTTPException, _http_exception_handler)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# CORS — allow the SvelteKit dashboard running at localhost:5173
# ---------------------------------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

# Health is always public (no auth dependency) so monitoring tools can reach it.
app.include_router(health_router, prefix="/api")

# All other routers require API key auth when API_KEY env var is set.
_auth = [Depends(require_api_key)]

app.include_router(attachments_router, prefix="/api", dependencies=_auth)
app.include_router(csv_import_router, prefix="/api", dependencies=_auth)
app.include_router(transactions_router, prefix="/api", dependencies=_auth)
app.include_router(ingest_router, prefix="/api", dependencies=_auth)
app.include_router(invoices_router, prefix="/api", dependencies=_auth)
app.include_router(reconciliation_router, prefix="/api", dependencies=_auth)
app.include_router(tax_export_router, prefix="/api", dependencies=_auth)
app.include_router(tax_year_locks_router, prefix="/api", dependencies=_auth)
app.include_router(vendor_rules_router, prefix="/api", dependencies=_auth)
