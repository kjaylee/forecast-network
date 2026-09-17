-- Additive v2 risk feed registry: typed [start,end) targets, reviewed mapping profiles.
-- v1 tables, publications and heads are untouched; v2 feeds use their own namespace
-- of feed identities and share the sequence head table by feed_id.
CREATE TABLE risk_feed_definitions_v2 (
    definition_hash TEXT PRIMARY KEY CHECK(length(definition_hash)=64),
    feed_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    approved_at INTEGER NOT NULL CHECK(approved_at >= 0)
);
CREATE INDEX risk_feed_definitions_v2_feed ON risk_feed_definitions_v2(feed_id, channel);
CREATE TRIGGER risk_feed_definitions_v2_no_update BEFORE UPDATE ON risk_feed_definitions_v2
BEGIN SELECT RAISE(ABORT, 'immutable risk definition'); END;
CREATE TRIGGER risk_feed_definitions_v2_no_delete BEFORE DELETE ON risk_feed_definitions_v2
BEGIN SELECT RAISE(ABORT, 'immutable risk definition'); END;

CREATE TABLE risk_feed_profiles_v2 (
    profile_hash TEXT PRIMARY KEY CHECK(length(profile_hash)=64),
    feed_id TEXT NOT NULL,
    profile_json TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    approved_at INTEGER NOT NULL CHECK(approved_at >= 0)
);
CREATE INDEX risk_feed_profiles_v2_feed ON risk_feed_profiles_v2(feed_id, approved_at);
CREATE TRIGGER risk_feed_profiles_v2_no_update BEFORE UPDATE ON risk_feed_profiles_v2
BEGIN SELECT RAISE(ABORT, 'immutable mapping profile'); END;
CREATE TRIGGER risk_feed_profiles_v2_no_delete BEFORE DELETE ON risk_feed_profiles_v2
BEGIN SELECT RAISE(ABORT, 'immutable mapping profile'); END;

CREATE TABLE risk_feed_bindings_v2 (
    binding_id TEXT PRIMARY KEY,
    feed_id TEXT NOT NULL,
    forecast_id TEXT NOT NULL REFERENCES forecasts(id),
    binding_json TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    approved_at INTEGER NOT NULL CHECK(approved_at >= 0)
);
CREATE INDEX risk_feed_bindings_v2_feed ON risk_feed_bindings_v2(feed_id, approved_at);
CREATE TRIGGER risk_feed_bindings_v2_no_update BEFORE UPDATE ON risk_feed_bindings_v2
BEGIN SELECT RAISE(ABORT, 'immutable risk feed binding'); END;
CREATE TRIGGER risk_feed_bindings_v2_no_delete BEFORE DELETE ON risk_feed_bindings_v2
BEGIN SELECT RAISE(ABORT, 'immutable risk feed binding'); END;

CREATE TABLE risk_feed_binding_revocations_v2 (
    binding_id TEXT PRIMARY KEY REFERENCES risk_feed_bindings_v2(binding_id),
    revoked_by TEXT NOT NULL,
    revoked_at INTEGER NOT NULL CHECK(revoked_at >= 0),
    reason TEXT NOT NULL
);
CREATE TRIGGER risk_feed_binding_revocations_v2_no_update BEFORE UPDATE ON risk_feed_binding_revocations_v2
BEGIN SELECT RAISE(ABORT, 'immutable risk feed revocation'); END;
CREATE TRIGGER risk_feed_binding_revocations_v2_no_delete BEFORE DELETE ON risk_feed_binding_revocations_v2
BEGIN SELECT RAISE(ABORT, 'immutable risk feed revocation'); END;

CREATE TABLE risk_feed_publications_v2 (
    feed_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK(sequence > 0),
    payload_hash TEXT NOT NULL,
    envelope_json TEXT NOT NULL,
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    PRIMARY KEY(feed_id, sequence)
);
CREATE TRIGGER risk_feed_publications_v2_no_update BEFORE UPDATE ON risk_feed_publications_v2
BEGIN SELECT RAISE(ABORT, 'immutable risk feed publication'); END;
CREATE TRIGGER risk_feed_publications_v2_no_delete BEFORE DELETE ON risk_feed_publications_v2
BEGIN SELECT RAISE(ABORT, 'immutable risk feed publication'); END;

-- Separate clock roles for operator refreshes: capture, evaluation and completion
-- times live in an immutable artifact keyed by the estimate they describe.
CREATE TABLE risk_prediction_clocks_v2 (
    estimate_artifact_hash TEXT PRIMARY KEY CHECK(length(estimate_artifact_hash)=64),
    forecast_id TEXT NOT NULL REFERENCES forecasts(id),
    clock_artifact_hash TEXT NOT NULL CHECK(length(clock_artifact_hash)=64),
    recorded_at INTEGER NOT NULL CHECK(recorded_at >= 0)
);
CREATE TRIGGER risk_prediction_clocks_v2_no_update BEFORE UPDATE ON risk_prediction_clocks_v2
BEGIN SELECT RAISE(ABORT, 'immutable prediction clock'); END;
CREATE TRIGGER risk_prediction_clocks_v2_no_delete BEFORE DELETE ON risk_prediction_clocks_v2
BEGIN SELECT RAISE(ABORT, 'immutable prediction clock'); END;
