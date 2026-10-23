-- =============================================================================
-- OmniLore v0.3.0-alpha (internal generation: v2.3)
-- Bootstrap Seed Data: minimal space / membership / owner token
-- File: sql/002_omnilore_bootstrap_seed.sql
-- =============================================================================
--
-- WHY THIS FILE EXISTS
-- --------------------
-- `001_omnilore_schema_and_rls.sql` contains only DDL (there are zero INSERTs),
-- so a freshly initialised database has tables but no usable identity: every MCP
-- tool takes a mandatory `token`, yet nothing exists to create one -> first-run
-- success rate 0%.
--
-- This file provides the minimal data needed to make the very first request
-- succeed, and mirrors what `omnilore bootstrap` does in local/memory mode
-- (`omnilore/bootstrap.py`):
--
--   space_id  : sp_bootstrap_personal  (SpaceType.PERSONAL)
--   user_id   : 00000000-0000-4000-8000-000000000001
--   role      : ADMIN
--   profile   : OMNI
--   categories: 01_Work, 02_Life, 03_Family, 04_Health_Archive
--
-- IDEMPOTENCY
-- -----------
-- Every statement uses ON CONFLICT DO NOTHING, so this file is safe to re-apply.
-- It is mounted read-only into docker-entrypoint-initdb.d, which only runs on
-- first initialisation of the volume.
--
-- SECURITY
-- --------
-- The token here is a PLACEHOLDER. `access_tokens` stores only the SHA-256 of the
-- raw token; the seed below therefore ships the hash of a well-known placeholder
-- string so that an accidental deployment cannot silently expose a valid secret.
-- ALWAYS rotate it:
--
--   1) Generate a fresh token and its hash:
--        python -c "import secrets,hashlib; t='tok_'+secrets.token_urlsafe(32); \
--          print(t); print(hashlib.sha256(t.encode()).hexdigest())"
--      (or simply run `omnilore bootstrap`, which does exactly this)
--
--   2) Put that token in your MCP client env under OMNILORE_BOOTSTRAP_TOKEN and
--      replace TOKEN_HASH_PLACEHOLDER below (or run once with psql afterwards).
--
-- NOTE for pgcrypto users: if the extension is available you can keep the raw
-- token out of your shell history by replacing the hash literal with
--   digest('<raw-token>', 'sha256')::text   -- requires: CREATE EXTENSION pgcrypto;
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- 1. Owner (bootstrap) user
-- -----------------------------------------------------------------------------
INSERT INTO users (user_id, display_name, status)
VALUES (
    '00000000-0000-4000-8000-000000000001'::uuid,
    'OmniLore Bootstrap Owner',
    'ACTIVE'
)
ON CONFLICT (user_id) DO NOTHING;

-- -----------------------------------------------------------------------------
-- 2. Default personal space
--    Keeps the same defaults as `omnilore.bootstrap.DEFAULT_*` so that local
--    (memory) mode and PostgreSQL mode behave identically.
-- -----------------------------------------------------------------------------
INSERT INTO spaces (
    space_id,
    space_type,
    parent_space_id,
    org_tree_path,
    vault_rel_path,
    allowed_categories,
    external_llm_policy,
    default_ttl_days
)
VALUES (
    'sp_bootstrap_personal',
    'PERSONAL',
    NULL,
    NULL,
    'spaces/sp_bootstrap_personal',
    ARRAY['01_Work', '02_Life', '03_Family', '04_Health_Archive'],
    '{"01_Work": "ALLOW", "02_Life": "ALLOW", "03_Family": "REQUIRE_REVIEW", "04_Health_Archive": "DENY_UNLESS_APPROVED"}'::jsonb,
    NULL
)
ON CONFLICT (space_id) DO NOTHING;

-- -----------------------------------------------------------------------------
-- 3. ADMIN membership for the owner
-- -----------------------------------------------------------------------------
INSERT INTO space_memberships (
    user_id,
    space_id,
    role,
    allowed_categories,
    inherit_org_public_read,
    granted_by
)
VALUES (
    '00000000-0000-4000-8000-000000000001'::uuid,
    'sp_bootstrap_personal',
    'ADMIN',
    ARRAY['01_Work', '02_Life', '03_Family', '04_Health_Archive'],
    false,
    '00000000-0000-4000-8000-000000000001'::uuid
)
ON CONFLICT (user_id, space_id) DO NOTHING;

-- -----------------------------------------------------------------------------
-- 4. Owner access token (OMNI profile, 1 year validity)
--    IMPORTANT: token_hash MUST be the lowercase hex SHA-256 of your raw token.
--    The literal below is intentionally invalid — rotate it before use.
-- -----------------------------------------------------------------------------
INSERT INTO access_tokens (
    token_hash,
    user_id,
    profile_type,
    allowed_read_spaces,
    allowed_write_spaces,
    allowed_categories,
    issued_by,
    expires_at
)
VALUES (
    -- sha256 of "REPLACE_ME_ROTATE_THIS_TOKEN" is NOT used here on purpose; the
    -- placeholder below does not correspond to any real secret.
    '0000000000000000000000000000000000000000000000000000000000000000',
    '00000000-0000-4000-8000-000000000001'::uuid,
    'OMNI',
    ARRAY['sp_bootstrap_personal'],
    ARRAY['sp_bootstrap_personal'],
    ARRAY['01_Work', '02_Life', '03_Family', '04_Health_Archive'],
    '00000000-0000-4000-8000-000000000001'::uuid,
    now() + interval '365 days'
)
ON CONFLICT (token_hash) DO NOTHING;

COMMIT;

-- =============================================================================
-- HOW TO ROTATE THE SEED TOKEN (run after first boot)
-- =============================================================================
-- psql "$DATABASE_URL" <<'SQL'
--   INSERT INTO access_tokens (token_hash, user_id, profile_type,
--                              allowed_read_spaces, allowed_write_spaces,
--                              allowed_categories, issued_by, expires_at)
--   VALUES ('<sha256-hex-of-your-token>',
--           '00000000-0000-4000-8000-000000000001'::uuid, 'OMNI',
--           ARRAY['sp_bootstrap_personal'], ARRAY['sp_bootstrap_personal'],
--           ARRAY['01_Work','02_Life','03_Family','04_Health_Archive'],
--           '00000000-0000-4000-8000-000000000001'::uuid, now() + interval '365 days')
--   ON CONFLICT (token_hash) DO NOTHING;
--
--   DELETE FROM access_tokens
--    WHERE token_hash = '0000000000000000000000000000000000000000000000000000000000000000';
-- SQL
