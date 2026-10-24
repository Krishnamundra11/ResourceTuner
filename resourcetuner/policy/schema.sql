-- Policy Store schema for ResourceTuner (§6 Policy Store)
-- Tracks historical outcomes of tuning actions per (workload, condition, action) tuple.

CREATE TABLE IF NOT EXISTS policies (
    workload TEXT NOT NULL,
    condition TEXT NOT NULL,
    action_tool TEXT NOT NULL,
    action_target TEXT NOT NULL,
    action_param TEXT NOT NULL,  -- JSON string, e.g. '{"weight": 50}'
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    avg_improvement REAL,       -- EMA with alpha=0.3
    sample_count INTEGER NOT NULL DEFAULT 0,
    promoted INTEGER NOT NULL DEFAULT 0,  -- 1 if promoted, 0 otherwise
    last_outcome TEXT,          -- IMPROVED, UNCHANGED, DEGRADED, TIMEOUT
    last_updated REAL NOT NULL, -- Unix epoch timestamp
    PRIMARY KEY (workload, condition, action_tool, action_target, action_param)
);

CREATE INDEX IF NOT EXISTS idx_policies_lookup 
    ON policies (workload, condition, promoted);
