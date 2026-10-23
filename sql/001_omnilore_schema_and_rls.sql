-- =============================================================================
-- OmniLore v2.3: Complete Production PostgreSQL Schema & Row-Level Security
-- Target: PostgreSQL 15+ with pgvector and ltree extensions
-- Covers Phase 0 (Security/RLS), Phase 1 (Personal MVP), Phase 2 (Family/Health/Lint),
-- and Phase 3 (Enterprise Multi-Level Org Tree Inheritance & Redacted Promotion)
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- 0. Extensions & Least-Privilege Role Separation (Section 7.4)
-- -----------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "vector";
CREATE EXTENSION IF NOT EXISTS "ltree";

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'omnilore_migrator') THEN
        CREATE ROLE omnilore_migrator NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'omnilore_gateway') THEN
        CREATE ROLE omnilore_gateway NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'omnilore_worker') THEN
        CREATE ROLE omnilore_worker NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'omnilore_auth_reader') THEN
        CREATE ROLE omnilore_auth_reader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'backup_operator') THEN
        CREATE ROLE backup_operator NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
    END IF;
END
$$;

-- -----------------------------------------------------------------------------
-- 1. Helper Functions for Safe GUC Context Parsing (Section 7.3 & 8.1)
-- -----------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS authz;

CREATE OR REPLACE FUNCTION authz.can_read_scope(target_space_id TEXT, target_category TEXT)
RETURNS BOOLEAN
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = pg_catalog, public
AS $$
    SELECT
        NULLIF(current_setting('app.allowed_read_spaces', true), '') IS NOT NULL
        AND target_space_id = ANY(string_to_array(current_setting('app.allowed_read_spaces', true), ','))
        AND (
            NULLIF(current_setting('app.allowed_read_scopes', true), '') IS NOT NULL
            AND (
                (target_space_id || ':' || target_category) = ANY(string_to_array(current_setting('app.allowed_read_scopes', true), ','))
                OR (target_space_id || ':*') = ANY(string_to_array(current_setting('app.allowed_read_scopes', true), ','))
            )
        );
$$;

CREATE OR REPLACE FUNCTION authz.can_write_scope(target_space_id TEXT, target_category TEXT)
RETURNS BOOLEAN
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = pg_catalog, public
AS $$
    SELECT
        NULLIF(current_setting('app.allowed_write_spaces', true), '') IS NOT NULL
        AND target_space_id = ANY(string_to_array(current_setting('app.allowed_write_spaces', true), ','))
        AND (
            NULLIF(current_setting('app.allowed_write_scopes', true), '') IS NOT NULL
            AND (
                (target_space_id || ':' || target_category) = ANY(string_to_array(current_setting('app.allowed_write_scopes', true), ','))
                OR (target_space_id || ':*') = ANY(string_to_array(current_setting('app.allowed_write_scopes', true), ','))
            )
        );
$$;

CREATE OR REPLACE FUNCTION authz.current_user_id()
RETURNS UUID
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = pg_catalog, public
AS $$
    SELECT NULLIF(current_setting('app.current_user_id', true), '')::UUID;
$$;

-- -----------------------------------------------------------------------------
-- 2. Core Identity, Space & Access Control Tables (Section 2 & 8)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    user_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    display_name    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'SUSPENDED', 'DEACTIVATED')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS spaces (
    space_id                TEXT PRIMARY KEY CHECK (space_id ~ '^[a-z0-9_]{3,64}$'),
    space_type              TEXT NOT NULL CHECK (space_type IN ('PERSONAL', 'FAMILY', 'ORG_PUBLIC', 'ORG_RESTRICTED')),
    parent_space_id         TEXT REFERENCES spaces(space_id) ON DELETE RESTRICT,
    org_tree_path           ltree,
    vault_rel_path          TEXT NOT NULL UNIQUE,
    allowed_categories      TEXT[] NOT NULL DEFAULT ARRAY['01_Work', '02_Life', '03_Family', '04_Health_Archive'],
    external_llm_policy     JSONB NOT NULL DEFAULT '{"01_Work": "ALLOW", "02_Life": "ALLOW", "03_Family": "REQUIRE_REVIEW", "04_Health_Archive": "DENY_UNLESS_APPROVED"}'::jsonb,
    default_ttl_days        INTEGER,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_spaces_org_tree_gist ON spaces USING GIST (org_tree_path);

CREATE TABLE IF NOT EXISTS space_memberships (
    membership_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                 UUID NOT NULL REFERENCES users(user_id) ON DELETE RESTRICT,
    space_id                TEXT NOT NULL REFERENCES spaces(space_id) ON DELETE RESTRICT,
    role                    TEXT NOT NULL CHECK (role IN ('READER', 'WRITER', 'ADMIN')),
    allowed_categories      TEXT[] NOT NULL,
    inherit_org_public_read BOOLEAN NOT NULL DEFAULT false,
    granted_by              UUID REFERENCES users(user_id),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at              TIMESTAMPTZ,
    UNIQUE (user_id, space_id)
);

CREATE TABLE IF NOT EXISTS access_tokens (
    token_id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    token_hash              TEXT NOT NULL UNIQUE CHECK (char_length(token_hash) = 64),
    user_id                 UUID NOT NULL REFERENCES users(user_id) ON DELETE RESTRICT,
    profile_type            TEXT NOT NULL CHECK (profile_type IN ('WORK', 'FAMILY', 'OMNI')),
    allowed_read_spaces     TEXT[] NOT NULL,
    allowed_write_spaces    TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    allowed_categories      TEXT[] NOT NULL,
    issued_by               UUID REFERENCES users(user_id),
    expires_at              TIMESTAMPTZ NOT NULL,
    revoked_at              TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- -----------------------------------------------------------------------------
-- 3. Assets, Document Series, Documents & Relations (Section 5 & 8)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS assets (
    asset_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sha256_hash         TEXT NOT NULL UNIQUE CHECK (char_length(sha256_hash) = 64),
    storage_rel_path    TEXT NOT NULL UNIQUE,
    mime_type           TEXT NOT NULL,
    byte_size           BIGINT NOT NULL CHECK (byte_size >= 0),
    created_by          UUID NOT NULL REFERENCES users(user_id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS document_series (
    series_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    space_id            TEXT NOT NULL REFERENCES spaces(space_id) ON DELETE RESTRICT,
    category            TEXT NOT NULL,
    version_key         TEXT NOT NULL CHECK (version_key ~ '^[a-zA-Z0-9_.\-]{3,128}$'),
    current_doc_id      UUID,
    revision_number     INTEGER NOT NULL DEFAULT 1 CHECK (revision_number >= 1),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (space_id, version_key)
);

CREATE TABLE IF NOT EXISTS documents (
    doc_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    space_id            TEXT NOT NULL REFERENCES spaces(space_id) ON DELETE RESTRICT,
    category            TEXT NOT NULL,
    series_id           UUID NOT NULL REFERENCES document_series(series_id) ON DELETE RESTRICT,
    version_label       TEXT NOT NULL DEFAULT 'v1.0',
    title               TEXT NOT NULL,
    summary             TEXT NOT NULL DEFAULT '',
    wiki_rel_path       TEXT NOT NULL,
    file_hash           TEXT NOT NULL CHECK (char_length(file_hash) = 64),
    content_revision    INTEGER NOT NULL DEFAULT 1 CHECK (content_revision >= 1),
    doc_status          TEXT NOT NULL CHECK (doc_status IN (
                            'PENDING_PUBLISH',
                            'PUBLISHED_CURRENT',
                            'SUPERSEDED',
                            'SUPPLEMENT_ACTIVE',
                            'ARCHIVED',
                            'REVERTED'
                        )),
    review_status       TEXT NOT NULL DEFAULT 'APPROVED' CHECK (review_status IN ('APPROVED', 'NEEDS_REVIEW', 'REJECTED')),
    is_current          BOOLEAN NOT NULL DEFAULT false,
    tags                TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    outbound_links      TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    ttl_expires_at      TIMESTAMPTZ,
    source_task_id      UUID,
    created_by          UUID NOT NULL REFERENCES users(user_id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE document_series
    ADD CONSTRAINT fk_document_series_current_doc
    FOREIGN KEY (current_doc_id) REFERENCES documents(doc_id) DEFERRABLE INITIALLY DEFERRED;

CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_one_current_per_series
    ON documents (series_id)
    WHERE is_current = true AND doc_status = 'PUBLISHED_CURRENT';

CREATE TABLE IF NOT EXISTS document_assets (
    doc_id              UUID NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    asset_id            UUID NOT NULL REFERENCES assets(asset_id) ON DELETE RESTRICT,
    space_id            TEXT NOT NULL REFERENCES spaces(space_id) ON DELETE RESTRICT,
    category            TEXT NOT NULL,
    original_filename   TEXT NOT NULL,
    source_locator      JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (doc_id, asset_id)
);

CREATE TABLE IF NOT EXISTS document_relations (
    relation_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    space_id            TEXT NOT NULL REFERENCES spaces(space_id) ON DELETE RESTRICT,
    category            TEXT NOT NULL,
    source_doc_id       UUID NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    target_doc_id       UUID NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    relation_type       TEXT NOT NULL CHECK (relation_type IN ('SUPPLEMENT', 'SUPERSEDES', 'PROMOTED_FROM', 'REFERENCES', 'SPLIT_FROM')),
    created_by          UUID NOT NULL REFERENCES users(user_id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_doc_id, target_doc_id, relation_type)
);

-- -----------------------------------------------------------------------------
-- 4. Denormalized Document Chunks for Fast Authorized Hybrid Search (Section 6)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS document_chunks (
    chunk_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    doc_id              UUID NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    series_id           UUID NOT NULL REFERENCES document_series(series_id) ON DELETE CASCADE,
    space_id            TEXT NOT NULL REFERENCES spaces(space_id) ON DELETE RESTRICT,
    category            TEXT NOT NULL,
    is_current          BOOLEAN NOT NULL DEFAULT false,
    chunk_index         INTEGER NOT NULL CHECK (chunk_index >= 0),
    heading_path        TEXT NOT NULL DEFAULT '',
    page_number         INTEGER,
    chunk_text          TEXT NOT NULL,
    code_symbols        TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    tsv                 tsvector GENERATED ALWAYS AS (to_tsvector('simple', coalesce(heading_path, '') || ' ' || chunk_text)) STORED,
    embedding           vector(1024) NOT NULL,
    embedding_provider  TEXT NOT NULL DEFAULT 'aliyun_model_studio',
    embedding_model     TEXT NOT NULL DEFAULT 'text-embedding-v4',
    embedding_version   TEXT NOT NULL DEFAULT 'v4-1024',
    index_version       INTEGER NOT NULL DEFAULT 1,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (doc_id, chunk_index, index_version)
);

CREATE INDEX IF NOT EXISTS idx_chunks_current_scope
    ON document_chunks (space_id, category, index_version)
    WHERE is_current = true;

CREATE INDEX IF NOT EXISTS idx_chunks_tsv
    ON document_chunks USING GIN (tsv)
    WHERE is_current = true;

CREATE INDEX IF NOT EXISTS idx_chunks_code_symbols
    ON document_chunks USING GIN (code_symbols)
    WHERE is_current = true;

CREATE INDEX IF NOT EXISTS idx_chunks_embedding_hnsw
    ON document_chunks USING hnsw (embedding vector_cosine_ops)
    WHERE is_current = true;

-- -----------------------------------------------------------------------------
-- 5. Ingestion Tasks, Outbox, Health Metrics, Audit & Model Telemetry
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ingestion_tasks (
    task_id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    idempotency_key         TEXT NOT NULL UNIQUE,
    user_id                 UUID NOT NULL REFERENCES users(user_id) ON DELETE RESTRICT,
    space_id                TEXT REFERENCES spaces(space_id) ON DELETE RESTRICT,
    category                TEXT,
    operation_type          TEXT NOT NULL CHECK (operation_type IN ('INGEST_FILE', 'COMMIT_NOTE', 'REPLACE_VERSION', 'SUPPLEMENT_DOC', 'REPROCESS')),
    task_state              TEXT NOT NULL CHECK (task_state IN (
                                'RECEIVED',
                                'PRECHECKING',
                                'ROUTING',
                                'NEEDS_REVIEW',
                                'STAGED',
                                'COMMITTING',
                                'COMMITTED',
                                'FAILED_RETRYABLE',
                                'FAILED_FINAL',
                                'CANCELLED',
                                'REVERTED'
                            )),
    canonical_file_hash     TEXT NOT NULL,
    staging_rel_path        TEXT,
    result_doc_id           UUID REFERENCES documents(doc_id),
    candidate_route         JSONB NOT NULL DEFAULT '{}'::jsonb,
    review_reason           TEXT,
    error_code              TEXT,
    error_detail            TEXT,
    retry_count             INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    worker_lease_owner      TEXT,
    worker_lease_expires_at TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS outbox_events (
    event_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id             UUID REFERENCES ingestion_tasks(task_id) ON DELETE CASCADE,
    space_id            TEXT NOT NULL REFERENCES spaces(space_id) ON DELETE RESTRICT,
    category            TEXT NOT NULL,
    event_type          TEXT NOT NULL CHECK (event_type IN (
                            'PUBLISH_STAGED_FILES',
                            'REBUILD_CHUNKS',
                            'CLEANUP_COMPENSATION',
                            'LEDGER_APPEND'
                        )),
    payload             JSONB NOT NULL,
    delivery_status     TEXT NOT NULL DEFAULT 'PENDING' CHECK (delivery_status IN ('PENDING', 'PROCESSING', 'COMPLETED', 'DEAD_LETTER')),
    retry_count         INTEGER NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at        TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS health_metrics (
    metric_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    space_id            TEXT NOT NULL REFERENCES spaces(space_id) ON DELETE RESTRICT,
    category            TEXT NOT NULL DEFAULT '04_Health_Archive' CHECK (category = '04_Health_Archive'),
    source_doc_id       UUID NOT NULL REFERENCES documents(doc_id) ON DELETE RESTRICT,
    subject_person      TEXT NOT NULL,
    metric_name         TEXT NOT NULL,
    raw_extracted_value TEXT NOT NULL,
    normalized_value    NUMERIC,
    unit                TEXT NOT NULL,
    reference_range     TEXT NOT NULL,
    measured_date       DATE NOT NULL,
    source_locator      JSONB NOT NULL DEFAULT '{}'::jsonb,
    verification_status TEXT NOT NULL DEFAULT 'NEEDS_CHECK' CHECK (verification_status IN ('NEEDS_CHECK', 'HUMAN_VERIFIED', 'REJECTED')),
    verified_by         UUID REFERENCES users(user_id),
    verified_at         TIMESTAMPTZ,
    revision_history    JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_events (
    audit_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_user_id       UUID NOT NULL REFERENCES users(user_id),
    space_id            TEXT REFERENCES spaces(space_id),
    category            TEXT,
    action_type         TEXT NOT NULL,
    target_object_type  TEXT NOT NULL,
    target_object_id    TEXT NOT NULL,
    before_summary      JSONB NOT NULL DEFAULT '{}'::jsonb,
    after_summary       JSONB NOT NULL DEFAULT '{}'::jsonb,
    request_id          TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS model_runs (
    run_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id             UUID REFERENCES ingestion_tasks(task_id) ON DELETE SET NULL,
    doc_id              UUID REFERENCES documents(doc_id) ON DELETE SET NULL,
    space_id            TEXT REFERENCES spaces(space_id),
    provider            TEXT NOT NULL,
    model_id            TEXT NOT NULL,
    task_type           TEXT NOT NULL,
    prompt_version      TEXT NOT NULL,
    schema_version      TEXT NOT NULL,
    input_tokens        INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    latency_ms          INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd  NUMERIC(10, 6) NOT NULL DEFAULT 0,
    status              TEXT NOT NULL CHECK (status IN ('SUCCESS', 'SCHEMA_INVALID', 'TIMEOUT', 'RATE_LIMITED', 'POLICY_BLOCKED', 'PROVIDER_ERROR')),
    error_code          TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- -----------------------------------------------------------------------------
-- 6. Command-Level Row Level Security (RLS) Policies (Section 7.3.1)
-- -----------------------------------------------------------------------------
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents FORCE ROW LEVEL SECURITY;

ALTER TABLE document_series ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_series FORCE ROW LEVEL SECURITY;

ALTER TABLE document_assets ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_assets FORCE ROW LEVEL SECURITY;

ALTER TABLE document_relations ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_relations FORCE ROW LEVEL SECURITY;

ALTER TABLE document_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_chunks FORCE ROW LEVEL SECURITY;

ALTER TABLE health_metrics ENABLE ROW LEVEL SECURITY;
ALTER TABLE health_metrics FORCE ROW LEVEL SECURITY;

ALTER TABLE ingestion_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE ingestion_tasks FORCE ROW LEVEL SECURITY;

CREATE POLICY docs_select_policy ON documents
    FOR SELECT TO omnilore_gateway, omnilore_worker
    USING (authz.can_read_scope(space_id, category) OR authz.can_write_scope(space_id, category));

CREATE POLICY docs_insert_policy ON documents
    FOR INSERT TO omnilore_gateway, omnilore_worker
    WITH CHECK (authz.can_write_scope(space_id, category));

CREATE POLICY docs_update_policy ON documents
    FOR UPDATE TO omnilore_gateway, omnilore_worker
    USING (authz.can_write_scope(space_id, category))
    WITH CHECK (authz.can_write_scope(space_id, category));

CREATE POLICY docs_delete_policy ON documents
    FOR DELETE TO omnilore_gateway, omnilore_worker
    USING (authz.can_write_scope(space_id, category));

CREATE POLICY series_select_policy ON document_series
    FOR SELECT TO omnilore_gateway, omnilore_worker
    USING (authz.can_read_scope(space_id, category) OR authz.can_write_scope(space_id, category));

CREATE POLICY series_insert_policy ON document_series
    FOR INSERT TO omnilore_gateway, omnilore_worker
    WITH CHECK (authz.can_write_scope(space_id, category));

CREATE POLICY series_update_policy ON document_series
    FOR UPDATE TO omnilore_gateway, omnilore_worker
    USING (authz.can_write_scope(space_id, category))
    WITH CHECK (authz.can_write_scope(space_id, category));

CREATE POLICY chunks_select_policy ON document_chunks
    FOR SELECT TO omnilore_gateway, omnilore_worker
    USING (authz.can_read_scope(space_id, category));

CREATE POLICY chunks_insert_policy ON document_chunks
    FOR INSERT TO omnilore_gateway, omnilore_worker
    WITH CHECK (authz.can_write_scope(space_id, category));

CREATE POLICY chunks_update_policy ON document_chunks
    FOR UPDATE TO omnilore_gateway, omnilore_worker
    USING (authz.can_write_scope(space_id, category))
    WITH CHECK (authz.can_write_scope(space_id, category));

CREATE POLICY chunks_delete_policy ON document_chunks
    FOR DELETE TO omnilore_gateway, omnilore_worker
    USING (authz.can_write_scope(space_id, category));

CREATE POLICY doc_assets_select_policy ON document_assets
    FOR SELECT TO omnilore_gateway, omnilore_worker
    USING (authz.can_read_scope(space_id, category));

CREATE POLICY doc_assets_write_policy ON document_assets
    FOR INSERT TO omnilore_gateway, omnilore_worker
    WITH CHECK (authz.can_write_scope(space_id, category));

CREATE POLICY doc_relations_select_policy ON document_relations
    FOR SELECT TO omnilore_gateway, omnilore_worker
    USING (authz.can_read_scope(space_id, category));

CREATE POLICY doc_relations_write_policy ON document_relations
    FOR INSERT TO omnilore_gateway, omnilore_worker
    WITH CHECK (authz.can_write_scope(space_id, category));

CREATE POLICY health_select_policy ON health_metrics
    FOR SELECT TO omnilore_gateway, omnilore_worker
    USING (category = '04_Health_Archive' AND authz.can_read_scope(space_id, '04_Health_Archive'));

CREATE POLICY health_insert_policy ON health_metrics
    FOR INSERT TO omnilore_gateway, omnilore_worker
    WITH CHECK (category = '04_Health_Archive' AND authz.can_write_scope(space_id, '04_Health_Archive'));

CREATE POLICY health_update_policy ON health_metrics
    FOR UPDATE TO omnilore_gateway, omnilore_worker
    USING (category = '04_Health_Archive' AND authz.can_write_scope(space_id, '04_Health_Archive'))
    WITH CHECK (category = '04_Health_Archive' AND authz.can_write_scope(space_id, '04_Health_Archive'));

CREATE POLICY tasks_select_policy ON ingestion_tasks
    FOR SELECT TO omnilore_gateway, omnilore_worker
    USING (
        user_id = authz.current_user_id()
        OR (space_id IS NOT NULL AND category IS NOT NULL AND authz.can_read_scope(space_id, category))
    );

CREATE POLICY tasks_insert_policy ON ingestion_tasks
    FOR INSERT TO omnilore_gateway, omnilore_worker
    WITH CHECK (
        user_id = authz.current_user_id()
        AND (space_id IS NULL OR (category IS NOT NULL AND authz.can_write_scope(space_id, category)))
    );

CREATE POLICY tasks_update_policy ON ingestion_tasks
    FOR UPDATE TO omnilore_gateway, omnilore_worker
    USING (
        user_id = authz.current_user_id()
        OR (space_id IS NOT NULL AND category IS NOT NULL AND authz.can_write_scope(space_id, category))
    )
    WITH CHECK (
        user_id = authz.current_user_id()
        OR (space_id IS NOT NULL AND category IS NOT NULL AND authz.can_write_scope(space_id, category))
    );

-- -----------------------------------------------------------------------------
-- 7. Least-Privilege Grants
-- -----------------------------------------------------------------------------
GRANT USAGE ON SCHEMA public, authz TO omnilore_gateway, omnilore_worker, omnilore_auth_reader, backup_operator;
GRANT SELECT ON users, spaces, space_memberships, access_tokens TO omnilore_auth_reader, omnilore_gateway;
GRANT SELECT, INSERT, UPDATE, DELETE ON documents, document_chunks TO omnilore_gateway, omnilore_worker;
GRANT SELECT, INSERT, UPDATE ON document_series, ingestion_tasks, health_metrics, outbox_events, space_memberships, access_tokens TO omnilore_gateway, omnilore_worker;
GRANT SELECT, INSERT ON assets, document_assets, document_relations, audit_events, model_runs TO omnilore_gateway, omnilore_worker;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO backup_operator;

COMMIT;
