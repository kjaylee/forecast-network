-- Operator-configured scheduled operation of a v2 feed. The admitted weight
-- reference is explicit configuration, never inferred from the newest artifact.
CREATE TABLE risk_feed_operations_v2 (
    feed_id TEXT PRIMARY KEY REFERENCES risk_feed_heads(feed_id),
    weight_set_hash TEXT NOT NULL CHECK(length(weight_set_hash)=64),
    weight_set_version TEXT NOT NULL,
    calibration_cohort_id TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
    configured_by TEXT NOT NULL,
    updated_at INTEGER NOT NULL CHECK(updated_at >= 0)
);
CREATE TABLE risk_feed_operation_log_v2 (
    feed_id TEXT NOT NULL,
    tick_at INTEGER NOT NULL CHECK(tick_at >= 0),
    outcome TEXT NOT NULL,
    detail TEXT NOT NULL,
    PRIMARY KEY(feed_id, tick_at)
);
