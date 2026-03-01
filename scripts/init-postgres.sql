-- Obscura PostgreSQL Initialization Script
-- Automatically run when PostgreSQL container starts

-- Create schemas
CREATE SCHEMA IF NOT EXISTS events;
CREATE SCHEMA IF NOT EXISTS memory;
CREATE SCHEMA IF NOT EXISTS metadata;

-- Sessions table
CREATE TABLE IF NOT EXISTS events.sessions (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'running',
    active_agent TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    backend TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'live',
    project TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    message_count INTEGER NOT NULL DEFAULT 0,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- Events table
CREATE TABLE IF NOT EXISTS events.events (
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload JSONB NOT NULL,
    timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (session_id, seq),
    FOREIGN KEY (session_id) REFERENCES events.sessions(id) ON DELETE CASCADE
);

-- Memory key-value store (consolidated from SQLite files)
CREATE TABLE IF NOT EXISTS memory.kv_store (
    id SERIAL PRIMARY KEY,
    user_hash VARCHAR(16) NOT NULL,
    namespace TEXT NOT NULL,
    key TEXT NOT NULL,
    value JSONB NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    expires_at TIMESTAMP WITH TIME ZONE,
    UNIQUE(user_hash, namespace, key)
);

-- Migration metadata
CREATE TABLE IF NOT EXISTS metadata.migration_history (
    id SERIAL PRIMARY KEY,
    migration_name VARCHAR(255) NOT NULL UNIQUE,
    sqlite_db_path TEXT,
    row_count_before INTEGER,
    row_count_after INTEGER,
    applied_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    success BOOLEAN DEFAULT TRUE,
    notes TEXT
);

-- Indexes for events
CREATE INDEX IF NOT EXISTS idx_events_session ON events.events(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events.events(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events.events(kind);
CREATE INDEX IF NOT EXISTS idx_events_payload ON events.events USING GIN (payload);

-- Indexes for sessions
CREATE INDEX IF NOT EXISTS idx_sessions_backend ON events.sessions(backend);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON events.sessions(status);
CREATE INDEX IF NOT EXISTS idx_sessions_source ON events.sessions(source);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON events.sessions(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_metadata ON events.sessions USING GIN (metadata);

-- Indexes for memory
CREATE INDEX IF NOT EXISTS idx_memory_user ON memory.kv_store(user_hash);
CREATE INDEX IF NOT EXISTS idx_memory_namespace ON memory.kv_store(namespace);
CREATE INDEX IF NOT EXISTS idx_memory_key ON memory.kv_store(key);
CREATE INDEX IF NOT EXISTS idx_memory_user_ns ON memory.kv_store(user_hash, namespace);
CREATE INDEX IF NOT EXISTS idx_memory_expires ON memory.kv_store(expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memory_value ON memory.kv_store USING GIN (value);

-- Auto-update timestamp trigger
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER update_memory_updated_at
    BEFORE UPDATE ON memory.kv_store
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at();

-- Grant permissions
GRANT ALL ON ALL TABLES IN SCHEMA events TO obscura_user;
GRANT ALL ON ALL TABLES IN SCHEMA memory TO obscura_user;
GRANT ALL ON ALL TABLES IN SCHEMA metadata TO obscura_user;
GRANT ALL ON ALL SEQUENCES IN SCHEMA memory TO obscura_user;
GRANT ALL ON ALL SEQUENCES IN SCHEMA metadata TO obscura_user;

-- Record initialization
INSERT INTO metadata.migration_history (migration_name, notes)
VALUES ('init-postgres', 'Initial PostgreSQL schema creation')
ON CONFLICT (migration_name) DO NOTHING;
