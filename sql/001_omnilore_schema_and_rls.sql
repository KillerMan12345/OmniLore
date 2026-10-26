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

-- 0.0 防「同名提权」：fail closed，宁可初始化失败也不留一个看起来正常的提权角色。
--     背景（真实踩过的坑）：docker-compose 曾把 POSTGRES_USER 写成 omnilore_migrator，
--     initdb 会先把这个名字建成**可登录的超级用户**，下面的 `IF NOT EXISTS` 随即跳过创建。
--     结果是本应 NOLOGIN 的属主角色静默变成可登录超用户，而超级用户会绕过全部 RLS 策略
--     —— 这类失败「看起来一切正常」，比连不上危险得多。
--     正确配置：POSTGRES_USER 用 initdb 的默认 postgres（与 CI 的 integration-rls 一致），
--     omnilore_migrator 只由本文件按 NOLOGIN 创建。
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_roles
        WHERE rolname = 'omnilore_migrator' AND (rolsuper OR rolcanlogin)
    ) THEN
        RAISE EXCEPTION
            'omnilore_migrator 已存在且可登录或为超级用户：这是 POSTGRES_USER 与本文件角色同名导致的提权（超用户会绕过 RLS）。请把 compose/.env 的 POSTGRES_USER 改回 postgres，并重建数据卷。';
    END IF;
END
$$;

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

-- =============================================================================
-- 6. Command-Level Row Level Security (RLS) Policies (Section 7.3.1)
-- =============================================================================
-- 阅读提示（贡献者请先读这段）
-- ----------------------------
-- 下面 21 条行级安全策略，每一条上方都有 **4 行注释**，分别回答：
--   ① 这条策略保护什么（哪张表、什么读/写动作）；
--   ② 它依赖哪个权限维度 —— 口径与 `docs/FEATURE_STATUS.md` / `docs/THREAT_MODEL.md` 一致：
--      **只有 space + category 两维被真实 PostgreSQL 探针验证过**（2026-10-24，三条探针）；
--      `authz.current_user_id()`（user 维度）与 membership / profile 维度**没有探针覆盖**，
--      注释里会逐条注明「未验证」，不要把它们读成「已被证明」；
--   ③ 它防的具体攻击场景；
--   ④ 它**不覆盖**什么（边界必须写明，否则读者会以为策略比实际更强）。
--
-- 红线：**只加注释，不改策略逻辑。** 这 21 条策略是真实 PG 验证的基线；
-- 动逻辑会让「已验证」这句话失去依据，也让 `tools/verify_rls_pg.py` 的结论过期。
-- 由 `tests/test_w2_w3_roadmap.py::TestRlsPolicyComments` 自动守护（漏写注释会红）。
-- =============================================================================
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

-- [docs_select_policy] 保护 documents 的 SELECT：一行文档谁能读出来。
--   依赖维度：space + category（这两维是唯一被真实 PG 探针验证过的；membership / profile 未验证）。
--   防：跨空间越权读取 —— 未授权的 space 行在**命令层**就被过滤，而不是查出来再在应用层丢掉。
--   不覆盖：写入侧（见下面三条 insert/update/delete）；profile 硬性隔离由引擎层判定，不在此表达。
CREATE POLICY docs_select_policy ON documents
    FOR SELECT TO omnilore_gateway, omnilore_worker
    USING (authz.can_read_scope(space_id, category) OR authz.can_write_scope(space_id, category));

-- [docs_insert_policy] 保护 documents 的 INSERT：谁能新建文档行。
--   依赖维度：space + category（已被真实 PG 探针验证）。
--   防：把文档写进自己无写权限的空间/分类（WITH CHECK 在写入前拒绝，而不是写成功再回滚）。
--   不覆盖：同一文档的幂等性（由 `compute_idempotency_key` + 唯一约束保证）；series 指针正确性。
CREATE POLICY docs_insert_policy ON documents
    FOR INSERT TO omnilore_gateway, omnilore_worker
    WITH CHECK (authz.can_write_scope(space_id, category));

-- [docs_update_policy] 保护 documents 的 UPDATE：谁能改已有文档行。
--   依赖维度：space + category（已被真实 PG 探针验证）。
--   防：把别人的文档行「改到自己空间」（USING 挡住读取旧行，WITH CHECK 挡住写入新空间值）。
--   不覆盖：字段级校验（`content_revision` 乐观锁、受保护 frontmatter 字段由应用层判定）。
CREATE POLICY docs_update_policy ON documents
    FOR UPDATE TO omnilore_gateway, omnilore_worker
    USING (authz.can_write_scope(space_id, category))
    WITH CHECK (authz.can_write_scope(space_id, category));

-- [docs_delete_policy] 保护 documents 的 DELETE：谁能删除文档行。
--   依赖维度：space + category（已被真实 PG 探针验证）。
--   防：跨空间删除他人文档（未授权行对 DELETE 不可见，因此删不掉）。
--   不覆盖：级联影响的可追溯性（删除留痕由 audit_events 承担，不是策略的职责）；
--           也不覆盖「软删除 vs 硬删除」的业务语义。
CREATE POLICY docs_delete_policy ON documents
    FOR DELETE TO omnilore_gateway, omnilore_worker
    USING (authz.can_write_scope(space_id, category));

-- [series_select_policy] 保护 document_series 的 SELECT：谁能看版本序列本身。
--   依赖维度：space + category（已被真实 PG 探针验证）。
--   防：通过 series 元数据侧信道推断其他空间的版本演进（版本号、当前指针）。
--   不覆盖：版本链中历史文档行的可见性（那由 docs_select_policy 决定）。
CREATE POLICY series_select_policy ON document_series
    FOR SELECT TO omnilore_gateway, omnilore_worker
    USING (authz.can_read_scope(space_id, category) OR authz.can_write_scope(space_id, category));

-- [series_insert_policy] 保护 document_series 的 INSERT：谁能新建版本序列。
--   依赖维度：space + category（已被真实 PG 探针验证）。
--   防：在无写权限的空间里凭空插入 series，制造后续版本切换的落点。
--   不覆盖：`(space_id, version_key)` 唯一性 —— 那由唯一约束保证，不是策略。
CREATE POLICY series_insert_policy ON document_series
    FOR INSERT TO omnilore_gateway, omnilore_worker
    WITH CHECK (authz.can_write_scope(space_id, category));

-- [series_update_policy] 保护 document_series 的 UPDATE：谁能推进版本指针。
--   依赖维度：space + category（已被真实 PG 探针验证）。
--   防：把别的空间的 series 指针指向自己空间的文档（越权「劫持」版本归属）。
--   不覆盖：两阶段切换的原子性（Outbox + 补偿负责，见 docs/THREAT_MODEL.md 第 3 节）。
CREATE POLICY series_update_policy ON document_series
    FOR UPDATE TO omnilore_gateway, omnilore_worker
    USING (authz.can_write_scope(space_id, category))
    WITH CHECK (authz.can_write_scope(space_id, category));

-- [chunks_select_policy] 保护 document_chunks 的 SELECT：检索候选集只能来自可读范围。
--   依赖维度：space + category（已被真实 PG 探针验证）。
--   防：向量/关键词检索跨空间召回他人片段 —— 这是「越权内容经检索泄露」的主要入口。
--   不覆盖：片段级脱敏；注意本策略**只认读权限**（不像 documents 那样 OR 写权限），
--           因此「有写权限但无读权限」的角色看不到 chunks，这是刻意的保守选择。
CREATE POLICY chunks_select_policy ON document_chunks
    FOR SELECT TO omnilore_gateway, omnilore_worker
    USING (authz.can_read_scope(space_id, category));

-- [chunks_insert_policy] 保护 document_chunks 的 INSERT：谁能写入检索片段（含向量）。
--   依赖维度：space + category（已被真实 PG 探针验证）。
--   防：把伪造片段塞进别人的空间，污染其检索结果（投毒）。
--   不覆盖：embedding 模型版本一致性（由 index_version / embedding_version 字段与重建流程负责）。
CREATE POLICY chunks_insert_policy ON document_chunks
    FOR INSERT TO omnilore_gateway, omnilore_worker
    WITH CHECK (authz.can_write_scope(space_id, category));

-- [chunks_update_policy] 保护 document_chunks 的 UPDATE：谁能改片段（含把 is_current 置否）。
--   依赖维度：space + category（已被真实 PG 探针验证）。
--   防：把别的空间的片段标记为「已过期」，使其从检索里消失（隐蔽的拒绝服务）。
--   不覆盖：蓝绿索引重建期间的一致性（重建走整表替换，不经过本策略）。
CREATE POLICY chunks_update_policy ON document_chunks
    FOR UPDATE TO omnilore_gateway, omnilore_worker
    USING (authz.can_write_scope(space_id, category))
    WITH CHECK (authz.can_write_scope(space_id, category));

-- [chunks_delete_policy] 保护 document_chunks 的 DELETE：谁能删除检索片段。
--   依赖维度：space + category（已被真实 PG 探针验证）。
--   防：跨空间删除片段，让对方的答案「无据可查」。
--   不覆盖：删除后的审计留痕（audit_events 负责）。
CREATE POLICY chunks_delete_policy ON document_chunks
    FOR DELETE TO omnilore_gateway, omnilore_worker
    USING (authz.can_write_scope(space_id, category));

-- [doc_assets_select_policy] 保护 document_assets 的 SELECT：谁能看「文档 ↔ 原件」的归属。
--   依赖维度：space + category（已被真实 PG 探针验证）。
--   防：通过归属关系反查他人原件文件名/定位信息（比正文更敏感的元数据）。
--   不覆盖：assets 表本身（CAS 资产按内容寻址、无空间列，其访问控制由网关层路径校验承担）。
CREATE POLICY doc_assets_select_policy ON document_assets
    FOR SELECT TO omnilore_gateway, omnilore_worker
    USING (authz.can_read_scope(space_id, category));

-- [doc_assets_write_policy] 保护 document_assets 的 INSERT：谁能建立新的归属关系。
--   依赖维度：space + category（已被真实 PG 探针验证）。
--   防：把别人的原件挂到自己文档下，制造「这条答案来自那份原件」的伪证据。
--   不覆盖：UPDATE / DELETE —— 本表**没有**对应策略，因此行级默认拒绝（归属关系一经写入即不可改）。
CREATE POLICY doc_assets_write_policy ON document_assets
    FOR INSERT TO omnilore_gateway, omnilore_worker
    WITH CHECK (authz.can_write_scope(space_id, category));

-- [doc_relations_select_policy] 保护 document_relations 的 SELECT：谁能看文档间关系。
--   依赖维度：space + category（已被真实 PG 探针验证）。
--   防：由 SUPERSEDES / SUPPLEMENT 关系边推断其他空间的文档结构。
--   不覆盖：关系两端文档本身的可读性（由 docs_select_policy 分别判定）。
CREATE POLICY doc_relations_select_policy ON document_relations
    FOR SELECT TO omnilore_gateway, omnilore_worker
    USING (authz.can_read_scope(space_id, category));

-- [doc_relations_write_policy] 保护 document_relations 的 INSERT：谁能新增关系边。
--   依赖维度：space + category（已被真实 PG 探针验证）。
--   防：伪造 SUPERSEDES 边，让旧版本看起来「已被取代」从而隐藏证据。
--   不覆盖：UPDATE / DELETE —— 本表同样**没有**对应策略，行级默认拒绝。
CREATE POLICY doc_relations_write_policy ON document_relations
    FOR INSERT TO omnilore_gateway, omnilore_worker
    WITH CHECK (authz.can_write_scope(space_id, category));

-- [health_select_policy] 保护 health_metrics 的 SELECT：体检指标谁能读。
--   依赖维度：space + category（已被真实 PG 探针验证）；额外硬编码 category = '04_Health_Archive'。
--   防：健康数据被「顺手」读进工作/生活分类（分类常量写死在策略里，不靠调用方自觉）。
--   不覆盖：家庭成员之间的可见边界（目前同空间同分类即可读）、指标核验状态的语义。
CREATE POLICY health_select_policy ON health_metrics
    FOR SELECT TO omnilore_gateway, omnilore_worker
    USING (category = '04_Health_Archive' AND authz.can_read_scope(space_id, '04_Health_Archive'));

-- [health_insert_policy] 保护 health_metrics 的 INSERT：谁能写入体检指标。
--   依赖维度：space + category（已被真实 PG 探针验证）；category 必须等于 '04_Health_Archive'。
--   防：把健康指标写进别的分类，绕过「健康数据的专属保护」。
--   不覆盖：指标值的正确性（AI 提取结果一律 NEEDS_CHECK，须人工核验后才转 HUMAN_VERIFIED）。
CREATE POLICY health_insert_policy ON health_metrics
    FOR INSERT TO omnilore_gateway, omnilore_worker
    WITH CHECK (category = '04_Health_Archive' AND authz.can_write_scope(space_id, '04_Health_Archive'));

-- [health_update_policy] 保护 health_metrics 的 UPDATE：谁能改指标（含核验状态）。
--   依赖维度：space + category（已被真实 PG 探针验证）；category 必须等于 '04_Health_Archive'。
--   防：把 NEEDS_CHECK 静默改成 HUMAN_VERIFIED（伪造「已由人核验」），把归属空间改到别处。
--   不覆盖：revision_history 是否被如实追加（应用层职责，不是策略能保证的）。
CREATE POLICY health_update_policy ON health_metrics
    FOR UPDATE TO omnilore_gateway, omnilore_worker
    USING (category = '04_Health_Archive' AND authz.can_write_scope(space_id, '04_Health_Archive'))
    WITH CHECK (category = '04_Health_Archive' AND authz.can_write_scope(space_id, '04_Health_Archive'));

-- [tasks_select_policy] 保护 ingestion_tasks 的 SELECT：任务回执谁能看。
--   依赖维度：user（`authz.current_user_id()`）+ space + category —— **user 维度没有真实 PG 探针覆盖**，
--             space + category 两维已验证；本策略是两者取并集，任一侧通过即可读。
--   防：读到别人的入库任务（回执里含文件名、结果 doc_id、错误详情）；跨空间扫描在途任务。
--   不覆盖：任务回执中可能残留的原始文本（回执字段本身不做内容脱敏）。
CREATE POLICY tasks_select_policy ON ingestion_tasks
    FOR SELECT TO omnilore_gateway, omnilore_worker
    USING (
        user_id = authz.current_user_id()
        OR (space_id IS NOT NULL AND category IS NOT NULL AND authz.can_read_scope(space_id, category))
    );

-- [tasks_insert_policy] 保护 ingestion_tasks 的 INSERT：谁能创建任务。
--   依赖维度：user（无真实 PG 探针覆盖）+ space + category（已验证）；user 维度是**必须满足**的，
--             不是可选并集 —— 不能替别人建任务。
--   防：以他人身份创建任务（污染任务归属）、在无写权限的空间里排队入库。
--   不覆盖：幂等性（`compute_idempotency_key` 与 idempotency_key 唯一约束负责）。
CREATE POLICY tasks_insert_policy ON ingestion_tasks
    FOR INSERT TO omnilore_gateway, omnilore_worker
    WITH CHECK (
        user_id = authz.current_user_id()
        AND (space_id IS NULL OR (category IS NOT NULL AND authz.can_write_scope(space_id, category)))
    );

-- [tasks_update_policy] 保护 ingestion_tasks 的 UPDATE：谁能推进任务状态机。
--   依赖维度：user（无真实 PG 探针覆盖）+ space + category（已验证），两者取并集。
--   防：改写他人的任务状态（例如把失败任务改成 COMMITTED，制造「已成功入库」的假回执）。
--   不覆盖：状态迁移合法性（`validate_state_transition` 在引擎层判定；策略只判「谁」不判「能不能」）。
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
