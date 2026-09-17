-- Canonical approvals and published envelopes are immutable evidence.
-- Head changes and publication inserts share the existing D1 mutation-guard CAS.
CREATE TABLE risk_feed_bindings (
    binding_id TEXT PRIMARY KEY,
    feed_id TEXT NOT NULL,
    binding_json TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    approved_at INTEGER NOT NULL CHECK(approved_at >= 0)
);
CREATE INDEX risk_feed_bindings_feed ON risk_feed_bindings(feed_id, approved_at);
CREATE TRIGGER risk_feed_bindings_no_update BEFORE UPDATE ON risk_feed_bindings
BEGIN SELECT RAISE(ABORT, 'immutable risk feed binding'); END;
CREATE TRIGGER risk_feed_bindings_no_delete BEFORE DELETE ON risk_feed_bindings
BEGIN SELECT RAISE(ABORT, 'immutable risk feed binding'); END;

CREATE TABLE risk_feed_binding_revocations (
    binding_id TEXT PRIMARY KEY REFERENCES risk_feed_bindings(binding_id),
    revoked_by TEXT NOT NULL,
    revoked_at INTEGER NOT NULL CHECK(revoked_at >= 0),
    reason TEXT NOT NULL
);
CREATE TRIGGER risk_feed_binding_revocations_no_update BEFORE UPDATE ON risk_feed_binding_revocations
BEGIN SELECT RAISE(ABORT, 'immutable risk feed revocation'); END;
CREATE TRIGGER risk_feed_binding_revocations_no_delete BEFORE DELETE ON risk_feed_binding_revocations
BEGIN SELECT RAISE(ABORT, 'immutable risk feed revocation'); END;

CREATE TABLE risk_feed_heads (
    feed_id TEXT PRIMARY KEY,
    sequence INTEGER NOT NULL CHECK(sequence >= 0)
);
CREATE TABLE risk_feed_publications (
    feed_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK(sequence > 0),
    payload_hash TEXT NOT NULL,
    envelope_json TEXT NOT NULL,
    created_at INTEGER NOT NULL CHECK(created_at >= 0),
    PRIMARY KEY(feed_id, sequence)
);
CREATE TRIGGER risk_feed_publications_no_update BEFORE UPDATE ON risk_feed_publications
BEGIN SELECT RAISE(ABORT, 'immutable risk feed publication'); END;
CREATE TRIGGER risk_feed_publications_no_delete BEFORE DELETE ON risk_feed_publications
BEGIN SELECT RAISE(ABORT, 'immutable risk feed publication'); END;
