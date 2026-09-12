-- This migration is the D1 and SQLite contract. Application writes use prepared SQL.
PRAGMA foreign_keys = ON;

CREATE TABLE users (
    id TEXT PRIMARY KEY, display_name TEXT NOT NULL CHECK(length(display_name) BETWEEN 1 AND 40),
    handle TEXT NOT NULL UNIQUE, recovery_hash TEXT NOT NULL UNIQUE, created_at INTEGER NOT NULL
);
CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
    created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL
);
CREATE INDEX sessions_expiry ON sessions(expires_at);
CREATE TABLE artifacts (
    hash TEXT PRIMARY KEY CHECK(length(hash)=64), kind TEXT NOT NULL,
    body TEXT NOT NULL CHECK(length(CAST(body AS BLOB))<=524288), media_type TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TRIGGER artifacts_immutable BEFORE UPDATE ON artifacts
BEGIN SELECT RAISE(ABORT, 'immutable_artifact'); END;
CREATE TABLE drafts (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), specification TEXT NOT NULL,
    assessment TEXT NOT NULL, ai_forecast TEXT, created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL, published_id TEXT
);
CREATE TABLE forecasts (
    id TEXT PRIMARY KEY, creator_id TEXT NOT NULL REFERENCES users(id), draft_id TEXT NOT NULL UNIQUE,
    snapshot TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>=0),
    state TEXT NOT NULL, category TEXT NOT NULL, title TEXT NOT NULL, question TEXT NOT NULL,
    normalized_question TEXT NOT NULL, specification_hash TEXT NOT NULL UNIQUE,
    open_at INTEGER NOT NULL, close_at INTEGER NOT NULL, created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL, challenge_until INTEGER, finalized_outcome TEXT,
    ai_forecast TEXT, share_count INTEGER NOT NULL DEFAULT 0, mutation_key TEXT NOT NULL,
    job_token TEXT, job_until INTEGER NOT NULL DEFAULT 0, retry_at INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0, job_error TEXT
);
CREATE INDEX forecasts_discovery ON forecasts(state,category,created_at DESC);
CREATE INDEX forecasts_jobs ON forecasts(state,retry_at,job_until,close_at);
CREATE INDEX forecasts_creator ON forecasts(creator_id,created_at DESC);
CREATE UNIQUE INDEX forecast_question_unique ON forecasts(normalized_question,close_at);
CREATE TABLE command_receipts (
    forecast_id TEXT NOT NULL REFERENCES forecasts(id), command_id TEXT NOT NULL,
    receipt TEXT NOT NULL, PRIMARY KEY(forecast_id,command_id)
);
CREATE TABLE operations (
    user_id TEXT NOT NULL, operation_key TEXT NOT NULL, request_hash TEXT NOT NULL,
    forecast_id TEXT REFERENCES forecasts(id), result TEXT NOT NULL, created_at INTEGER NOT NULL,
    PRIMARY KEY(user_id,operation_key)
);
CREATE TABLE events (
    forecast_id TEXT NOT NULL REFERENCES forecasts(id), revision INTEGER NOT NULL,
    hash TEXT NOT NULL UNIQUE, event TEXT NOT NULL, created_at INTEGER NOT NULL,
    PRIMARY KEY(forecast_id,revision)
);
CREATE TRIGGER events_immutable BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'immutable_event'); END;
-- A failed CAS must fail the whole D1 batch. Insert 0 to raise a CHECK failure.
CREATE TABLE mutation_guards (token TEXT PRIMARY KEY, valid INTEGER NOT NULL CHECK(valid=1));
CREATE TABLE user_forecasts (
    forecast_id TEXT NOT NULL REFERENCES forecasts(id), user_id TEXT NOT NULL REFERENCES users(id),
    outcome TEXT NOT NULL CHECK(outcome IN ('YES','NO')),
    confidence INTEGER NOT NULL CHECK(confidence BETWEEN 0 AND 100),
    yes_probability INTEGER NOT NULL CHECK(yes_probability BETWEEN 0 AND 100),
    submitted_at INTEGER NOT NULL, revision INTEGER NOT NULL, body TEXT NOT NULL,
    PRIMARY KEY(forecast_id,user_id)
);
CREATE INDEX user_forecasts_user ON user_forecasts(user_id,submitted_at DESC);
CREATE TABLE forecast_history (
    forecast_id TEXT NOT NULL REFERENCES forecasts(id), revision INTEGER NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id), body TEXT NOT NULL,
    crowd_probability REAL NOT NULL, participant_count INTEGER NOT NULL, created_at INTEGER NOT NULL,
    PRIMARY KEY(forecast_id,revision)
);
CREATE TABLE comments (
    id TEXT PRIMARY KEY, forecast_id TEXT NOT NULL REFERENCES forecasts(id),
    user_id TEXT NOT NULL REFERENCES users(id), body TEXT NOT NULL CHECK(length(body) BETWEEN 1 AND 2000),
    created_at INTEGER NOT NULL
);
CREATE INDEX comments_forecast ON comments(forecast_id,created_at);
CREATE TABLE follows (
    follower_id TEXT NOT NULL REFERENCES users(id), creator_id TEXT NOT NULL REFERENCES users(id),
    created_at INTEGER NOT NULL, PRIMARY KEY(follower_id,creator_id), CHECK(follower_id!=creator_id)
);
CREATE TABLE activity (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), forecast_id TEXT REFERENCES forecasts(id),
    kind TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, created_at INTEGER NOT NULL,
    read_at INTEGER
);
CREATE INDEX activity_user ON activity(user_id,created_at DESC);
CREATE TABLE reputation_scores (
    forecast_id TEXT NOT NULL REFERENCES forecasts(id), user_id TEXT NOT NULL REFERENCES users(id),
    category TEXT NOT NULL, outcome TEXT NOT NULL, probability INTEGER NOT NULL,
    correct INTEGER, brier_score REAL, created_at INTEGER NOT NULL,
    PRIMARY KEY(forecast_id,user_id)
);
CREATE INDEX reputation_user ON reputation_scores(user_id);
CREATE TABLE outbox (
    id TEXT PRIMARY KEY, forecast_id TEXT NOT NULL REFERENCES forecasts(id), kind TEXT NOT NULL,
    created_at INTEGER NOT NULL, processed_at INTEGER,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','processed','awaiting_adapter'))
);
CREATE TABLE rate_limits (
    scope TEXT NOT NULL, bucket INTEGER NOT NULL, count INTEGER NOT NULL CHECK(count>=0),
    expires_at INTEGER NOT NULL,
    PRIMARY KEY(scope,bucket)
);
CREATE TABLE ai_leases (
    owner TEXT PRIMARY KEY, token TEXT NOT NULL, expires_at INTEGER NOT NULL
);
CREATE TABLE share_receipts (
    forecast_id TEXT NOT NULL REFERENCES forecasts(id), actor TEXT NOT NULL, bucket INTEGER NOT NULL,
    PRIMARY KEY(forecast_id,actor,bucket)
);
