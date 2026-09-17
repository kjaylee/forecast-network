-- Operator-approved recurring episode templates; the tick publishes and binds the
-- next episode ahead of its start so overlapping windows hand off without a gap.
CREATE TABLE risk_feed_series_v2 (
    series_id TEXT PRIMARY KEY,
    feed_id TEXT NOT NULL,
    series_hash TEXT NOT NULL CHECK(length(series_hash)=64),
    series_json TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
    configured_by TEXT NOT NULL,
    updated_at INTEGER NOT NULL CHECK(updated_at >= 0)
);
CREATE TABLE risk_feed_series_log_v2 (
    series_id TEXT NOT NULL,
    target_start_ms INTEGER NOT NULL CHECK(target_start_ms >= 0),
    attempted_at INTEGER NOT NULL CHECK(attempted_at >= 0),
    outcome TEXT NOT NULL,
    detail TEXT NOT NULL,
    PRIMARY KEY(series_id, target_start_ms, attempted_at)
);
