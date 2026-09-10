-- LifeOS Agent — PostgreSQL DDL
-- Target: Neon Serverless PostgreSQL with pgvector >= 0.7.0
-- Apply with:  python -m app.init_db
--
-- Idempotent: safe to re-run against an existing branch.
--
-- NOTE: comments here must use `--`. PostgreSQL has no `#` comment, and this
-- file is sent to the server verbatim as one multi-statement script, so a `#`
-- header would be a syntax error on line 1.

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- 1. Users (self-managed JWT identity)
--    Deliberately NOT under RLS: signup and login must read/write this table
--    before any tenant context exists.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email VARCHAR(255) UNIQUE NOT NULL,
    hashed_password VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- 2. Documents
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS documents (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    filename VARCHAR(255) NOT NULL,
    category VARCHAR(50) DEFAULT 'General',
    status VARCHAR(30) DEFAULT 'processing', -- 'processing', 'ready', 'needs_review'
    raw_text TEXT,
    metadata JSONB DEFAULT '{}'::jsonb,
    has_actionable_deadline BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- 3. Document chunks (vector storage)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS document_chunks (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    filename VARCHAR(255) NOT NULL,
    page_number INTEGER DEFAULT 1,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    embedding vector(2048) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- 4. Actionable tasks
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tasks (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    document_id UUID REFERENCES documents(id) ON DELETE CASCADE,
    title VARCHAR(255) NOT NULL,
    due_date DATE,
    status VARCHAR(20) DEFAULT 'pending', -- 'pending', 'completed'
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- 5. Audit logs (observability and tool-execution traces)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_logs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    action_type VARCHAR(50) NOT NULL, -- 'tool_call', 'document_upload', 'extraction', ...
    tool_name VARCHAR(50),
    input_payload JSONB,
    output_payload JSONB,
    execution_time_ms INTEGER,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_documents_user_id ON documents(user_id);
CREATE INDEX IF NOT EXISTS idx_tasks_user_id ON tasks(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_user_id ON audit_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_document_chunks_user_id ON document_chunks(user_id);
CREATE INDEX IF NOT EXISTS idx_document_chunks_document_id ON document_chunks(document_id);

-- Supports query_structured_data() deadline filtering/ordering.
CREATE INDEX IF NOT EXISTS idx_documents_deadline
    ON documents ((metadata ->> 'action_deadline'));
CREATE INDEX IF NOT EXISTS idx_documents_category ON documents(category);

-- Vector index.
--
-- NOTE: pgvector caps HNSW at 2,000 dimensions for the `vector` type, so
--   CREATE INDEX ... USING hnsw (embedding vector_cosine_ops)
-- on a vector(2048) column fails outright with
--   "column cannot have more than 2000 dimensions for hnsw index".
-- halfvec permits up to 4,000 dimensions, so the index is built on the
-- halfvec cast and every query orders by the identical expression.
CREATE INDEX IF NOT EXISTS idx_document_chunks_vector ON document_chunks
    USING hnsw ((CAST(embedding AS halfvec(2048))) halfvec_cosine_ops);

-- ---------------------------------------------------------------------------
-- Row-Level Security
-- ---------------------------------------------------------------------------
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_logs ENABLE ROW LEVEL SECURITY;

-- A table's owner bypasses its own RLS policies by default, and on Neon the
-- application connects as the owner. Without FORCE, every policy below would be
-- silently inert.
ALTER TABLE documents FORCE ROW LEVEL SECURITY;
ALTER TABLE document_chunks FORCE ROW LEVEL SECURITY;
ALTER TABLE tasks FORCE ROW LEVEL SECURITY;
ALTER TABLE audit_logs FORCE ROW LEVEL SECURITY;

-- The tenant id is set per request transaction:
--   SELECT set_config('app.current_user_id', :user_id, true);
-- When the setting is absent, current_setting(..., true) returns NULL, the
-- comparison yields NULL, and no rows are visible or writable (fail closed).

DROP POLICY IF EXISTS tenant_isolation_documents ON documents;
CREATE POLICY tenant_isolation_documents ON documents
    USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
    WITH CHECK (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);

DROP POLICY IF EXISTS tenant_isolation_chunks ON document_chunks;
CREATE POLICY tenant_isolation_chunks ON document_chunks
    USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
    WITH CHECK (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);

DROP POLICY IF EXISTS tenant_isolation_tasks ON tasks;
CREATE POLICY tenant_isolation_tasks ON tasks
    USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
    WITH CHECK (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);

DROP POLICY IF EXISTS tenant_isolation_audit_logs ON audit_logs;
CREATE POLICY tenant_isolation_audit_logs ON audit_logs
    USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
    WITH CHECK (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
