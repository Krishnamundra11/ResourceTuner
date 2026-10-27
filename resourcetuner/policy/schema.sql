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

-- Append-only log of every individual outcome, one row per record_outcome() call. `policies`
-- above keeps only lifetime aggregates (counts, an EMA, the single last outcome) and cannot
-- support a real recent-vs-older trend; this table is what makes that possible (reported
-- finding: `recent_trend` was derived from last_outcome + the lifetime success_rate, never an
-- actual recent-vs-older comparison, because no per-event history existed to compare).
CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workload TEXT NOT NULL,
    condition TEXT NOT NULL,
    action_tool TEXT NOT NULL,
    action_target TEXT NOT NULL,
    action_param TEXT NOT NULL,     -- same JSON-string form as policies.action_param
    outcome TEXT NOT NULL,          -- IMPROVED, DEGRADED, UNCHANGED, TIMEOUT
    improvement_value REAL,         -- raw (non-EMA) value for this one outcome, if any
    ts REAL NOT NULL                -- Unix epoch timestamp
);

CREATE INDEX IF NOT EXISTS idx_outcomes_lookup
    ON outcomes (workload, condition, action_tool, action_target, action_param, ts);
