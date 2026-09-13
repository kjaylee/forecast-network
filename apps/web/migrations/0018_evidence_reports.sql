-- Community evidence reports: a signed-in forecaster points at an official announcement
-- that may already settle an open question. Supported publishers flow into the same
-- containment-before-review path as the automatic watcher; the first report whose
-- evidence is accepted for an early resolution earns a fixed, non-transferable reward.
CREATE TABLE evidence_reports (
    id TEXT PRIMARY KEY,
    forecast_id TEXT NOT NULL REFERENCES forecasts(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    url TEXT NOT NULL,
    article_id TEXT,
    observation_id TEXT,
    artifact_hash TEXT,
    status TEXT NOT NULL CHECK(status IN ('received','held','unrelated','rewarded','dismissed')),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(forecast_id,user_id,url)
);
CREATE INDEX evidence_reports_forecast ON evidence_reports(forecast_id,created_at);
CREATE INDEX evidence_reports_user ON evidence_reports(user_id,created_at);
CREATE TABLE point_evidence_rewards (
    id TEXT PRIMARY KEY,
    report_id TEXT NOT NULL UNIQUE REFERENCES evidence_reports(id),
    user_id TEXT NOT NULL REFERENCES point_accounts(user_id),
    forecast_id TEXT NOT NULL UNIQUE REFERENCES forecasts(id),
    amount INTEGER NOT NULL CHECK(typeof(amount)='integer' AND amount BETWEEN 1 AND 1000),
    available_after INTEGER NOT NULL CHECK(typeof(available_after)='integer'),
    committed_after INTEGER NOT NULL CHECK(typeof(committed_after)='integer'),
    created_at INTEGER NOT NULL
);
CREATE TRIGGER point_evidence_rewards_immutable BEFORE UPDATE ON point_evidence_rewards
BEGIN SELECT RAISE(ABORT,'immutable_reward'); END;
CREATE TRIGGER point_evidence_rewards_no_delete BEFORE DELETE ON point_evidence_rewards
BEGIN SELECT RAISE(ABORT,'immutable_reward'); END;
