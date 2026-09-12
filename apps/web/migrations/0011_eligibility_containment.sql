-- Timing review is independent of a participation hold: releasing an intake
-- notice cannot make receipts submitted after a known result eligible to win.
CREATE TABLE forecast_timing_reviews (
 forecast_id TEXT NOT NULL REFERENCES forecasts(id),
 specification_hash TEXT NOT NULL CHECK(length(specification_hash)=64),
 trigger_hash TEXT NOT NULL REFERENCES artifacts(hash),
 event_at INTEGER NOT NULL,
 event_time_basis TEXT NOT NULL CHECK(event_time_basis IN ('published_instant','observed_upper_bound')),
 created_at INTEGER NOT NULL,
 PRIMARY KEY(forecast_id,trigger_hash)
);
CREATE TRIGGER forecast_timing_reviews_immutable BEFORE UPDATE ON forecast_timing_reviews
BEGIN SELECT RAISE(ABORT,'immutable_timing_review'); END;
CREATE TRIGGER forecast_timing_reviews_no_delete BEFORE DELETE ON forecast_timing_reviews
BEGIN SELECT RAISE(ABORT,'immutable_timing_review'); END;

-- A validated v2 upgrade already proves receipt eligibility and reuses retained
-- evidence. Its original intake hold intentionally remains in the audit trail.
CREATE VIEW forecast_resolution_blockers AS
SELECT f.id AS forecast_id FROM forecasts f WHERE
 EXISTS(SELECT 1 FROM forecast_timing_reviews t WHERE t.forecast_id=f.id AND t.specification_hash=f.specification_hash)
 OR (json_extract(f.snapshot,'$.schema_version') IS NOT 2 AND (
  EXISTS(SELECT 1 FROM active_participation_holds h WHERE h.forecast_id=f.id)
  OR EXISTS(SELECT 1 FROM official_source_reviews r WHERE r.forecast_id=f.id AND r.specification_hash=f.specification_hash
   AND NOT (r.state='complete' AND json_extract(r.result,'$.accepted') IS 0 AND json_extract(r.result,'$.dismissible') IS 1))
 ));

CREATE TRIGGER forecast_resolution_eligibility BEFORE UPDATE OF state,snapshot ON forecasts
BEGIN SELECT RAISE(ABORT,'forecast_eligibility_review') WHERE
 NEW.state IN ('LOCKED','RESOLVING','PROPOSED','CHALLENGE','FINALIZED','ARCHIVED')
 AND EXISTS(SELECT 1 FROM forecast_resolution_blockers WHERE forecast_id=NEW.id)
 AND (json_extract(NEW.snapshot,'$.schema_version') IS NOT 2
  OR EXISTS(SELECT 1 FROM forecast_timing_reviews WHERE forecast_id=NEW.id AND specification_hash=NEW.specification_hash)); END;

CREATE TRIGGER forecast_reputation_eligibility BEFORE INSERT ON reputation_scores
BEGIN SELECT RAISE(ABORT,'forecast_eligibility_review') WHERE EXISTS(
 SELECT 1 FROM forecast_resolution_blockers WHERE forecast_id=NEW.forecast_id); END;
CREATE TRIGGER forecast_points_eligibility BEFORE INSERT ON point_ledger
BEGIN SELECT RAISE(ABORT,'forecast_eligibility_review') WHERE NEW.kind='settlement' AND EXISTS(
 SELECT 1 FROM forecast_resolution_blockers WHERE forecast_id=NEW.forecast_id); END;
CREATE TRIGGER forecast_market_settlement_eligibility BEFORE INSERT ON market_settlements
BEGIN SELECT RAISE(ABORT,'forecast_eligibility_review') WHERE EXISTS(
 SELECT 1 FROM forecast_resolution_blockers WHERE forecast_id=NEW.forecast_id); END;
CREATE TRIGGER forecast_market_ledger_eligibility BEFORE INSERT ON market_account_ledger
BEGIN SELECT RAISE(ABORT,'forecast_eligibility_review') WHERE NEW.kind='market_settlement' AND EXISTS(
 SELECT 1 FROM forecast_resolution_blockers WHERE forecast_id=NEW.forecast_id); END;
CREATE TRIGGER forecast_market_closure_eligibility BEFORE INSERT ON market_closures
BEGIN SELECT RAISE(ABORT,'forecast_eligibility_review') WHERE EXISTS(
 SELECT 1 FROM forecast_resolution_blockers WHERE forecast_id=NEW.forecast_id); END;

-- Unbound legacy questions retain their existing admission policy. Once a watch
-- is bound, ordinary forecasts use the same health window as active markets.
CREATE VIEW forecast_intake_watch_checks AS
SELECT b.forecast_id,s.enabled,s.failure_count,s.checked_at,s.interval_ms,s.lease_until
FROM official_watch_bindings b JOIN official_watch_sources s
 ON (s.id=b.source_id OR (s.parent_id=b.source_id AND s.enabled=1));
CREATE VIEW forecast_intake_review_blockers AS
SELECT r.forecast_id FROM official_source_reviews r JOIN forecasts f ON f.id=r.forecast_id
WHERE r.specification_hash=f.specification_hash AND
 NOT (r.state='complete' AND json_extract(r.result,'$.accepted') IS 0 AND json_extract(r.result,'$.dismissible') IS 1)
UNION SELECT forecast_id FROM forecast_timing_reviews;

CREATE TRIGGER forecast_watch_health_insert BEFORE INSERT ON user_forecasts
BEGIN SELECT RAISE(ABORT,'participation_on_hold') WHERE
 EXISTS(SELECT 1 FROM forecast_intake_review_blockers WHERE forecast_id=NEW.forecast_id)
 OR EXISTS(SELECT 1 FROM forecast_intake_watch_checks WHERE forecast_id=NEW.forecast_id
  AND (enabled!=1 OR failure_count>0 OR checked_at IS NULL OR lease_until>NEW.submitted_at
   OR checked_at<NEW.submitted_at-interval_ms-60000)); END;
CREATE TRIGGER forecast_watch_health_update BEFORE UPDATE ON user_forecasts
BEGIN SELECT RAISE(ABORT,'participation_on_hold') WHERE
 EXISTS(SELECT 1 FROM forecast_intake_review_blockers WHERE forecast_id=NEW.forecast_id)
 OR EXISTS(SELECT 1 FROM forecast_intake_watch_checks WHERE forecast_id=NEW.forecast_id
  AND (enabled!=1 OR failure_count>0 OR checked_at IS NULL OR lease_until>NEW.submitted_at
   OR checked_at<NEW.submitted_at-interval_ms-60000)); END;
CREATE TRIGGER forecast_market_timing_intake BEFORE INSERT ON market_fills
BEGIN SELECT RAISE(ABORT,'participation_on_hold') WHERE EXISTS(
 SELECT 1 FROM forecast_intake_review_blockers WHERE forecast_id=NEW.forecast_id); END;
