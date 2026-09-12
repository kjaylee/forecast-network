-- Shared, bounded official-source polling and immutable retained observations.
CREATE TABLE official_watch_sources (
 id TEXT PRIMARY KEY, url TEXT NOT NULL UNIQUE, kind TEXT NOT NULL CHECK(kind IN ('index','article')),
 parent_id TEXT REFERENCES official_watch_sources(id), pinned INTEGER NOT NULL DEFAULT 0 CHECK(pinned IN(0,1)), interval_ms INTEGER NOT NULL CHECK(interval_ms>=60000),
 etag TEXT, last_modified TEXT, checked_at INTEGER, next_poll INTEGER NOT NULL, lease_token TEXT, lease_until INTEGER NOT NULL DEFAULT 0,
 failure_count INTEGER NOT NULL DEFAULT 0, last_error TEXT, enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN(0,1))
);
CREATE INDEX official_watch_due ON official_watch_sources(enabled,next_poll,lease_until);
CREATE TABLE official_watch_bindings (
 forecast_id TEXT NOT NULL REFERENCES forecasts(id), source_id TEXT NOT NULL REFERENCES official_watch_sources(id),
 families TEXT NOT NULL CHECK(json_valid(families)), PRIMARY KEY(forecast_id,source_id)
);
CREATE TABLE official_source_observations (
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES official_watch_sources(id),
 url TEXT NOT NULL, content_hash TEXT NOT NULL CHECK(length(content_hash)=64),
 artifact_hash TEXT NOT NULL REFERENCES artifacts(hash), body TEXT NOT NULL CHECK(json_valid(body)),
 observed_at INTEGER NOT NULL, UNIQUE(source_id,url,content_hash)
);
CREATE TRIGGER official_observations_immutable BEFORE UPDATE ON official_source_observations
BEGIN SELECT RAISE(ABORT,'immutable_source_observation'); END;
CREATE TRIGGER official_observations_no_delete BEFORE DELETE ON official_source_observations
BEGIN SELECT RAISE(ABORT,'immutable_source_observation'); END;
CREATE TABLE official_source_reviews (
 id TEXT PRIMARY KEY, observation_id TEXT NOT NULL REFERENCES official_source_observations(id),
 forecast_id TEXT NOT NULL REFERENCES forecasts(id), specification_hash TEXT NOT NULL CHECK(length(specification_hash)=64),
 content_hash TEXT NOT NULL CHECK(length(content_hash)=64), policy TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','reviewed','complete','exhausted')),
 attempts INTEGER NOT NULL DEFAULT 0, next_attempt INTEGER NOT NULL, lease_token TEXT,
 lease_until INTEGER NOT NULL DEFAULT 0, result TEXT CHECK(result IS NULL OR json_valid(result)), last_error TEXT,
 UNIQUE(content_hash,specification_hash,policy)
);
CREATE INDEX official_review_due ON official_source_reviews(state,next_attempt,lease_until);
