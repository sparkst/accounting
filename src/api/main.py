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

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.routes.attachments import router as attachments_router
from src.api.routes.csv_import import router as csv_import_router
from src.api.routes.health import router as health_router
from src.api.routes.ingest import router as ingest_router
from src.api.routes.transactions import router as transactions_router
from src.classification.seed_rules import seed_vendor_rules
from src.db.connection import SessionLocal, init_db

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

app.include_router(attachments_router, prefix="/api")
app.include_router(csv_import_router, prefix="/api")
app.include_router(health_router, prefix="/api")
app.include_router(transactions_router, prefix="/api")
app.include_router(ingest_router, prefix="/api")
