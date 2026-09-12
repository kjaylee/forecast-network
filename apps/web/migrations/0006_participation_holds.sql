-- Administrative participation containment is separate from domain resolution.
-- Every transition is immutable; the latest audited revision defines the hold.
CREATE TABLE participation_hold_events (
    id TEXT PRIMARY KEY,
    forecast_id TEXT NOT NULL REFERENCES forecasts(id),
    revision INTEGER NOT NULL CHECK(revision>0),
    action TEXT NOT NULL CHECK(action IN ('hold','release')),
    hold_id TEXT NOT NULL,
    specification_hash TEXT NOT NULL CHECK(length(specification_hash)=64),
    reason TEXT NOT NULL CHECK(reason='known_outcome_review'),
    evidence_url TEXT NOT NULL,
    actor TEXT NOT NULL CHECK(actor='authenticated_admin'),
    request_key TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL CHECK(length(request_hash)=64),
    body TEXT NOT NULL CHECK(json_valid(body)),
    created_at INTEGER NOT NULL,
    UNIQUE(forecast_id,revision)
);
CREATE TRIGGER participation_hold_events_immutable BEFORE UPDATE ON participation_hold_events
BEGIN SELECT RAISE(ABORT,'immutable_participation_hold'); END;
CREATE TRIGGER participation_hold_events_no_delete BEFORE DELETE ON participation_hold_events
BEGIN SELECT RAISE(ABORT,'immutable_participation_hold'); END;
CREATE VIEW active_participation_holds AS
SELECT e.* FROM participation_hold_events e WHERE e.action='hold'
 AND NOT EXISTS(SELECT 1 FROM participation_hold_events newer
 WHERE newer.forecast_id=e.forecast_id AND newer.revision>e.revision);
-- These guards share the vote/snapshot/receipt/points atomic transaction, so a
-- vote that read OPEN before an operator hold cannot commit after the hold.
CREATE TRIGGER participation_hold_vote_insert BEFORE INSERT ON user_forecasts
BEGIN SELECT RAISE(ABORT,'participation_on_hold') WHERE EXISTS(
 SELECT 1 FROM active_participation_holds WHERE forecast_id=NEW.forecast_id); END;
CREATE TRIGGER participation_hold_vote_update BEFORE UPDATE ON user_forecasts
BEGIN SELECT RAISE(ABORT,'participation_on_hold') WHERE EXISTS(
 SELECT 1 FROM active_participation_holds WHERE forecast_id=NEW.forecast_id); END;
CREATE TRIGGER participation_hold_points BEFORE INSERT ON point_ledger
BEGIN SELECT RAISE(ABORT,'participation_on_hold') WHERE NEW.kind='reservation' AND EXISTS(
 SELECT 1 FROM active_participation_holds WHERE forecast_id=NEW.forecast_id); END;
