-- Accounting System — Reference DDL
--
-- This file documents the intended schema. The authoritative schema is created
-- by SQLAlchemy's Base.metadata.create_all() in src/db/connection.py.
-- This SQL is kept in sync for human reference, DBA review, and CI diffing.
--
-- SQLite quirks:
--   - No native UUID type → stored as TEXT(36)
--   - No native BOOLEAN → stored as INTEGER (0/1)
--   - No native DECIMAL → stored as NUMERIC (SQLAlchemy uses TEXT internally)
--   - JSON columns stored as TEXT by SQLite, parsed by SQLAlchemy

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;

-- ── transactions ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS transactions (
    id                  TEXT(36)       NOT NULL PRIMARY KEY,
    source              TEXT(32)       NOT NULL,
    source_id           TEXT(255),
    source_hash         TEXT(64)       NOT NULL UNIQUE,
    date                TEXT(10)       NOT NULL,
    description         TEXT           NOT NULL,
    amount              NUMERIC(12, 2),          -- NULL = amount unknown (needs review)
    currency            TEXT(3)        NOT NULL DEFAULT 'USD',
    entity              TEXT(16),
    direction           TEXT(16),
    tax_category        TEXT(32),
    tax_subcategory     TEXT(32),
    deductible_pct      REAL           NOT NULL DEFAULT 1.0,
    status              TEXT(24)       NOT NULL DEFAULT 'needs_review',
    confidence          REAL           NOT NULL DEFAULT 0.0,
    review_reason       TEXT,
    parent_id           TEXT(36)       REFERENCES transactions(id),
    reimbursement_link  TEXT(36)       REFERENCES transactions(id),
    attachments         TEXT,          -- JSON array
    raw_data            TEXT           NOT NULL,  -- JSON object
    created_at          TEXT           NOT NULL,
    updated_at          TEXT           NOT NULL,
    confirmed_by        TEXT(8)        NOT NULL DEFAULT 'auto',
    notes               TEXT
);

CREATE INDEX IF NOT EXISTS idx_transactions_date         ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_transactions_entity       ON transactions(entity);
CREATE INDEX IF NOT EXISTS idx_transactions_tax_category ON transactions(tax_category);
CREATE INDEX IF NOT EXISTS idx_transactions_status       ON transactions(status);
CREATE INDEX IF NOT EXISTS idx_transactions_source_hash  ON transactions(source_hash);

-- ── vendor_rules ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS vendor_rules (
    id               TEXT(36)  NOT NULL PRIMARY KEY,
    vendor_pattern   TEXT      NOT NULL,
    entity           TEXT(16)  NOT NULL,
    tax_category     TEXT(32)  NOT NULL,
    tax_subcategory  TEXT(32),
    direction        TEXT(16)  NOT NULL,
    deductible_pct   REAL      NOT NULL DEFAULT 1.0,
    confidence       REAL      NOT NULL DEFAULT 1.0,
    source           TEXT(8)   NOT NULL DEFAULT 'human',
    examples         INTEGER   NOT NULL DEFAULT 1,
    last_matched     TEXT,
    created_at       TEXT      NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_vendor_rules_entity ON vendor_rules(entity);

-- ── ingested_files ────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS ingested_files (
    id               TEXT(36)  NOT NULL PRIMARY KEY,
    file_path        TEXT      NOT NULL,
    file_hash        TEXT(64)  NOT NULL UNIQUE,
    adapter          TEXT(32)  NOT NULL,
    processed_at     TEXT      NOT NULL,
    status           TEXT(8)   NOT NULL DEFAULT 'success',
    transaction_ids  TEXT      NOT NULL  -- JSON array
);

CREATE INDEX IF NOT EXISTS idx_ingested_files_adapter   ON ingested_files(adapter);
CREATE INDEX IF NOT EXISTS idx_ingested_files_file_hash ON ingested_files(file_hash);

-- ── audit_events ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS audit_events (
    id              TEXT(36)  NOT NULL PRIMARY KEY,
    transaction_id  TEXT(36)  NOT NULL REFERENCES transactions(id),
    field_changed   TEXT(64)  NOT NULL,
    old_value       TEXT,
    new_value       TEXT,
    changed_by      TEXT(8)   NOT NULL,
    changed_at      TEXT      NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_events_transaction_id ON audit_events(transaction_id);
CREATE INDEX IF NOT EXISTS idx_audit_events_changed_at     ON audit_events(changed_at);

-- ── ingestion_log ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS ingestion_log (
    id                  TEXT(36)   NOT NULL PRIMARY KEY,
    source              TEXT(32)   NOT NULL,
    run_at              TEXT       NOT NULL,
    status              TEXT(16)   NOT NULL,
    records_processed   INTEGER    NOT NULL DEFAULT 0,
    records_failed      INTEGER    NOT NULL DEFAULT 0,
    error_detail        TEXT,
    retryable           INTEGER    NOT NULL DEFAULT 0,
    retried_at          TEXT,
    resolved_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_ingestion_log_source ON ingestion_log(source);
CREATE INDEX IF NOT EXISTS idx_ingestion_log_run_at ON ingestion_log(run_at);
