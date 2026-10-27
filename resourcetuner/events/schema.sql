-- Pipeline run/event store (dashboard observability + replay).
-- Append-only events; `seq` is a global monotonic cursor used as the SSE event id, so a client
-- can reconnect with Last-Event-ID and receive exactly the events it has not seen.

CREATE TABLE IF NOT EXISTS runs (
    run_id            TEXT PRIMARY KEY,
    kind              TEXT NOT NULL,          -- 'cli' | 'dashboard'
    scenario          TEXT,
    level             TEXT,
    cycles_requested  INTEGER NOT NULL DEFAULT 1,
    cycles_done       INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL,          -- requested|running|completed|failed|cancelled
    params            TEXT NOT NULL DEFAULT '{}',   -- JSON (no secrets)
    created_at        REAL NOT NULL,
    started_at        REAL,
    ended_at          REAL,
    error             TEXT
);

CREATE TABLE IF NOT EXISTS events (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,   -- global order == SSE event id
    run_id      TEXT NOT NULL,
    cycle       INTEGER,                              -- null for run-level events
    stage       TEXT NOT NULL,
    status      TEXT NOT NULL,                        -- waiting|running|completed|rejected|skipped|failed|evidence_unavailable
    ts          REAL NOT NULL,
    duration_ms INTEGER,
    summary     TEXT NOT NULL DEFAULT '',
    payload     TEXT NOT NULL DEFAULT '{}',           -- JSON (scrubbed of secrets)
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);
