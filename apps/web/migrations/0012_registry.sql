-- Immutable exact historical snapshots; delivery state is separate and lease fenced.
CREATE TABLE registry_intents (
 forecast_id TEXT NOT NULL REFERENCES forecasts(id), revision INTEGER NOT NULL CHECK(revision>=2),
 event_hash TEXT NOT NULL CHECK(length(event_hash)=64), snapshot TEXT NOT NULL,
 created_at INTEGER NOT NULL, PRIMARY KEY(forecast_id,revision),
 FOREIGN KEY(forecast_id,revision) REFERENCES events(forecast_id,revision)
);
CREATE TRIGGER registry_intents_immutable BEFORE UPDATE ON registry_intents
BEGIN SELECT RAISE(ABORT,'immutable_registry_intent'); END;
CREATE TRIGGER registry_intents_no_delete BEFORE DELETE ON registry_intents
BEGIN SELECT RAISE(ABORT,'immutable_registry_intent'); END;
CREATE TRIGGER registry_intents_bound BEFORE INSERT ON registry_intents
WHEN NOT EXISTS (SELECT 1 FROM events e WHERE e.forecast_id=NEW.forecast_id
 AND e.revision=NEW.revision AND e.hash=NEW.event_hash)
BEGIN SELECT RAISE(ABORT,'registry_event_mismatch'); END;
CREATE TABLE registry_delivery (
 forecast_id TEXT NOT NULL, revision INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','submitted','confirmed','blocked')),
 lease_token TEXT, lease_until INTEGER NOT NULL DEFAULT 0,
 retry_at INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
 signature TEXT, submitted_at INTEGER, confirmed_slot INTEGER, error_code TEXT,
 PRIMARY KEY(forecast_id,revision),
 FOREIGN KEY(forecast_id,revision) REFERENCES registry_intents(forecast_id,revision)
);
CREATE INDEX registry_delivery_due ON registry_delivery(status,retry_at,lease_until);
CREATE TRIGGER registry_intent_queue AFTER INSERT ON registry_intents
BEGIN INSERT INTO registry_delivery(forecast_id,revision) VALUES(NEW.forecast_id,NEW.revision); END;
CREATE TABLE registry_spend (
 day INTEGER PRIMARY KEY, reserved_lamports INTEGER NOT NULL CHECK(reserved_lamports>=0)
);
CREATE TABLE registry_forecasts (
 forecast_id TEXT PRIMARY KEY REFERENCES forecasts(id), enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
 confirmed_revision INTEGER, confirmed_event_hash TEXT, confirmed_state INTEGER,
 chain_deadline INTEGER, chain_time INTEGER, observed_at INTEGER
);
CREATE TRIGGER registry_finalization_guard BEFORE UPDATE ON forecasts
WHEN NEW.state='FINALIZED' AND OLD.state!='FINALIZED'
 AND EXISTS(SELECT 1 FROM registry_forecasts WHERE forecast_id=OLD.id AND enabled=1)
 AND NOT EXISTS(SELECT 1 FROM registry_forecasts r WHERE r.forecast_id=OLD.id AND r.enabled=1
 AND r.confirmed_revision=OLD.revision AND r.confirmed_state=6
 AND r.confirmed_event_hash=json_extract(OLD.snapshot,'$.audit_head_hash')
 AND r.chain_deadline>0 AND r.chain_time>=r.chain_deadline
 AND r.observed_at<=NEW.updated_at AND r.observed_at>=NEW.updated_at-60000)
BEGIN SELECT RAISE(ABORT,'registry_finalization_pending'); END;
CREATE TABLE registry_deployment (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), program_id TEXT NOT NULL, genesis_hash TEXT NOT NULL
);
CREATE TRIGGER registry_deployment_immutable BEFORE UPDATE ON registry_deployment
BEGIN SELECT RAISE(ABORT,'immutable_registry_deployment'); END;
CREATE TRIGGER registry_deployment_no_delete BEFORE DELETE ON registry_deployment
BEGIN SELECT RAISE(ABORT,'immutable_registry_deployment'); END;
