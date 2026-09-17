-- Product KPI reads reuse immutable domain events; never create click telemetry.
CREATE INDEX events_product_participation_time ON events(created_at,forecast_id,revision)
 WHERE json_extract(event,'$.command_name')='submit_forecast';
CREATE INDEX users_product_created ON users(created_at,id);

-- Administrative exclusions are permanent overlays, including after restore.
-- No display names, wallet addresses, sessions or raw provider responses belong here.
CREATE TABLE product_analytics_exclusions (
 subject_kind TEXT NOT NULL CHECK(subject_kind IN ('user','forecast')),
 subject_id TEXT NOT NULL CHECK(length(subject_id) BETWEEN 1 AND 128),
 reason TEXT NOT NULL CHECK(reason IN ('staff','test','load','deleted')),
 recorded_at INTEGER NOT NULL CHECK(typeof(recorded_at)='integer' AND recorded_at>=0),
 evidence_hash TEXT NOT NULL CHECK(length(evidence_hash)=64),
 PRIMARY KEY(subject_kind,subject_id,reason)
);
CREATE TRIGGER product_analytics_exclusions_no_update BEFORE UPDATE ON product_analytics_exclusions
BEGIN SELECT RAISE(ABORT,'immutable_analytics_exclusion'); END;
CREATE TRIGGER product_analytics_exclusions_no_delete BEFORE DELETE ON product_analytics_exclusions
BEGIN SELECT RAISE(ABORT,'immutable_analytics_exclusion'); END;

-- Only an authenticated, evidence-backed account merge may create these links.
-- An IP address or shared device is not evidence that accounts are the same person.
CREATE TABLE product_analytics_identity_links (
 alias_user_id TEXT PRIMARY KEY CHECK(length(alias_user_id) BETWEEN 1 AND 128),
 canonical_user_id TEXT NOT NULL CHECK(length(canonical_user_id) BETWEEN 1 AND 128),
 recorded_at INTEGER NOT NULL CHECK(typeof(recorded_at)='integer' AND recorded_at>=0),
 evidence_hash TEXT NOT NULL CHECK(length(evidence_hash)=64),
 CHECK(alias_user_id!=canonical_user_id)
);
CREATE TRIGGER product_analytics_identity_no_update BEFORE UPDATE ON product_analytics_identity_links
BEGIN SELECT RAISE(ABORT,'immutable_analytics_identity'); END;
CREATE TRIGGER product_analytics_identity_no_delete BEFORE DELETE ON product_analytics_identity_links
BEGIN SELECT RAISE(ABORT,'immutable_analytics_identity'); END;

-- Append revisions when an unknown or estimated operation obtains a real receipt.
-- The aggregate counts only the latest admitted revision once per operation.
CREATE TABLE product_cost_receipts (
 source_kind TEXT NOT NULL CHECK(source_kind IN ('provider','chain')),
 operation_id TEXT NOT NULL CHECK(length(operation_id) BETWEEN 1 AND 256),
 revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>=1),
 population_kind TEXT NOT NULL CHECK(population_kind IN ('application','fixture','isolated-load')),
 forecast_id TEXT, user_id TEXT,
 occurred_at INTEGER NOT NULL CHECK(typeof(occurred_at)='integer' AND occurred_at>=0),
 recorded_at INTEGER NOT NULL CHECK(typeof(recorded_at)='integer' AND recorded_at>=occurred_at),
 status TEXT NOT NULL CHECK(status IN ('known','unknown','estimated')),
 amount_atomic INTEGER CHECK(amount_atomic IS NULL OR (typeof(amount_atomic)='integer' AND amount_atomic>=0)),
 unit TEXT NOT NULL CHECK((source_kind='provider' AND unit='USD_MICRO') OR (source_kind='chain' AND unit='DEVNET_LAMPORT')),
 evidence_hash TEXT CHECK(evidence_hash IS NULL OR length(evidence_hash)=64),
 PRIMARY KEY(source_kind,operation_id,revision),
 CHECK((status='unknown' AND amount_atomic IS NULL) OR (status IN ('known','estimated') AND amount_atomic IS NOT NULL)),
 CHECK(status!='known' OR evidence_hash IS NOT NULL)
);
CREATE INDEX product_cost_receipts_window ON product_cost_receipts(occurred_at,recorded_at);
CREATE TRIGGER product_cost_receipts_no_update BEFORE UPDATE ON product_cost_receipts
BEGIN SELECT RAISE(ABORT,'immutable_product_cost_receipt'); END;
CREATE TRIGGER product_cost_receipts_no_delete BEFORE DELETE ON product_cost_receipts
BEGIN SELECT RAISE(ABORT,'immutable_product_cost_receipt'); END;
CREATE TRIGGER product_cost_receipts_revision BEFORE INSERT ON product_cost_receipts
WHEN NEW.revision!=COALESCE((SELECT MAX(revision)+1 FROM product_cost_receipts
 WHERE source_kind=NEW.source_kind AND operation_id=NEW.operation_id),1)
 OR EXISTS(SELECT 1 FROM product_cost_receipts p WHERE p.source_kind=NEW.source_kind AND p.operation_id=NEW.operation_id
 AND (p.population_kind!=NEW.population_kind OR p.forecast_id IS NOT NEW.forecast_id OR p.user_id IS NOT NEW.user_id
 OR p.occurred_at!=NEW.occurred_at OR p.unit!=NEW.unit OR p.recorded_at>NEW.recorded_at))
BEGIN SELECT RAISE(ABORT,'product_cost_receipt_revision_mismatch'); END;
