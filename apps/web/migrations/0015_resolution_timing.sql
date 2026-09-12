-- Ordinary resolution evidence with uncertain participation timing stays on hold.
-- Metadata is not an automatic outcome/cutoff decision and never changes receipts.
CREATE TABLE resolution_timing_reviews (
 forecast_id TEXT NOT NULL REFERENCES forecasts(id),
 specification_hash TEXT NOT NULL CHECK(length(specification_hash)=64),
 resolution_hash TEXT NOT NULL CHECK(length(resolution_hash)=64),
 reason TEXT NOT NULL,
 last_receipt_at INTEGER NOT NULL,
 candidate_cutoff_at INTEGER,
 proof_hash TEXT NOT NULL CHECK(length(proof_hash)=64),
 body TEXT NOT NULL,
 created_at INTEGER NOT NULL,
 PRIMARY KEY(forecast_id,resolution_hash)
);
CREATE TRIGGER resolution_timing_reviews_immutable BEFORE UPDATE ON resolution_timing_reviews
BEGIN SELECT RAISE(ABORT,'immutable_resolution_timing_review'); END;
CREATE TRIGGER resolution_timing_reviews_no_delete BEFORE DELETE ON resolution_timing_reviews
BEGIN SELECT RAISE(ABORT,'immutable_resolution_timing_review'); END;
CREATE TRIGGER resolution_timing_review_validate BEFORE INSERT ON resolution_timing_reviews
BEGIN SELECT RAISE(ABORT,'resolution_timing_review_mismatch') WHERE NOT EXISTS(
 SELECT 1 FROM forecasts f WHERE f.id=NEW.forecast_id AND f.specification_hash=NEW.specification_hash
 AND json_extract(NEW.body,'$.forecastId')=NEW.forecast_id
 AND json_extract(NEW.body,'$.specificationHash')=NEW.specification_hash
 AND json_extract(NEW.body,'$.resolutionHash')=NEW.resolution_hash
 AND json_extract(NEW.body,'$.lastReceiptAt')=NEW.last_receipt_at); END;
CREATE VIEW unresolved_resolution_timing_reviews AS
 SELECT r.* FROM resolution_timing_reviews r WHERE NOT EXISTS(
  SELECT 1 FROM forecast_eligibility_decisions d JOIN forecast_eligibility_completions c ON c.decision_id=d.id
  WHERE d.forecast_id=r.forecast_id AND d.specification_hash=r.specification_hash);
DROP VIEW forecast_resolution_blockers;
CREATE VIEW forecast_resolution_blockers AS
 SELECT f.id AS forecast_id FROM forecasts f WHERE
 EXISTS(SELECT 1 FROM forecast_eligibility_decisions d WHERE d.forecast_id=f.id
  AND NOT EXISTS(SELECT 1 FROM forecast_eligibility_completions c WHERE c.decision_id=d.id))
 OR EXISTS(SELECT 1 FROM forecast_timing_reviews t WHERE t.forecast_id=f.id AND t.specification_hash=f.specification_hash
  AND NOT EXISTS(SELECT 1 FROM forecast_eligibility_decisions d JOIN forecast_eligibility_completions c ON c.decision_id=d.id
   WHERE d.id=t.trigger_hash AND d.forecast_id=f.id AND d.specification_hash=t.specification_hash))
 OR (json_extract(f.snapshot,'$.schema_version') IS NOT 2 AND (
  EXISTS(SELECT 1 FROM active_participation_holds h WHERE h.forecast_id=f.id AND NOT EXISTS(
   SELECT 1 FROM forecast_eligibility_decisions d JOIN forecast_eligibility_completions c ON c.decision_id=d.id
   WHERE d.forecast_id=f.id AND d.specification_hash=h.specification_hash
    AND EXISTS(SELECT 1 FROM json_each(d.body,'$.evidence') ev WHERE json_extract(ev.value,'$.url')=h.evidence_url)))
  OR EXISTS(SELECT 1 FROM official_source_reviews r WHERE r.forecast_id=f.id AND r.specification_hash=f.specification_hash
   AND NOT (r.state='complete' AND json_extract(r.result,'$.accepted') IS 0 AND json_extract(r.result,'$.dismissible') IS 1)
   AND NOT EXISTS(SELECT 1 FROM forecast_eligibility_decisions d JOIN forecast_eligibility_completions c ON c.decision_id=d.id
    WHERE d.forecast_id=f.id AND json_extract(r.result,'$.accepted') IS 1
     AND json_extract(r.result,'$.trigger')=json(d.body)))
 ))
 UNION SELECT r.forecast_id FROM unresolved_resolution_timing_reviews r;
-- Unlike the historical early-upgrade exception, this guard also covers ordinary
-- replacement resolutions on v2 forecasts. PAUSED remains available for recovery.
CREATE TRIGGER resolution_timing_state_guard BEFORE UPDATE OF state,snapshot ON forecasts
BEGIN SELECT RAISE(ABORT,'resolution_timing_review') WHERE
 NEW.state IN ('PROPOSED','CHALLENGE','FINALIZED','ARCHIVED')
 AND EXISTS(SELECT 1 FROM unresolved_resolution_timing_reviews r
 WHERE r.forecast_id=NEW.id AND r.specification_hash=NEW.specification_hash); END;
CREATE TRIGGER resolution_timing_intake_insert BEFORE INSERT ON user_forecasts
BEGIN SELECT RAISE(ABORT,'resolution_timing_review') WHERE EXISTS(
 SELECT 1 FROM unresolved_resolution_timing_reviews WHERE forecast_id=NEW.forecast_id); END;
CREATE TRIGGER resolution_timing_intake_update BEFORE UPDATE ON user_forecasts
BEGIN SELECT RAISE(ABORT,'resolution_timing_review') WHERE EXISTS(
 SELECT 1 FROM unresolved_resolution_timing_reviews WHERE forecast_id=NEW.forecast_id); END;
CREATE TRIGGER resolution_timing_market_intake BEFORE INSERT ON market_fills
BEGIN SELECT RAISE(ABORT,'resolution_timing_review') WHERE EXISTS(
 SELECT 1 FROM unresolved_resolution_timing_reviews WHERE forecast_id=NEW.forecast_id); END;
