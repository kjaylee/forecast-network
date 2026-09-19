-- A timing review whose reason makes the outcome indeterminable can be closed.
--
-- The review is a blocker, and a blocker is a boolean: five places read
-- forecast_resolution_blockers and each would otherwise have to learn the same
-- exception, which is five chances to leak a reward. Closing the review instead
-- of bypassing the guard turns the boolean false everywhere at once, and keeps
-- "nothing is rewarded on evidence that cannot be placed relative to
-- participation" literally true rather than true-except-where-we-remembered.
--
-- A closure records what the review determined: the retained evidence is
-- authentic but cannot be placed before or after participation, so the only
-- result it supports is INVALID. It does not settle anything by itself.
-- ResolutionTiming.check() still refuses every outcome other than the recorded
-- determination, so a closure can never licence a reward or reputation.
--
-- Deliberately narrow. Only publication_time_unknown is determinable this way:
-- the review proved the evidence authentic and the publication time absent, and
-- no amount of waiting changes that. The other review reasons are determinate
-- failures with their own handling -- evidence that may predate participation is
-- a real timing judgement for the eligibility path, and unavailable or
-- unverified evidence may yet be supplied -- so closing them is not this table's
-- business.
CREATE TABLE resolution_timing_closures (
 forecast_id TEXT NOT NULL REFERENCES forecasts(id),
 specification_hash TEXT NOT NULL CHECK(length(specification_hash)=64),
 review_proof_hash TEXT NOT NULL CHECK(length(review_proof_hash)=64),
 determination TEXT NOT NULL CHECK(determination='INVALID'),
 reason TEXT NOT NULL CHECK(reason='publication_time_unknown'),
 evidence_hash TEXT NOT NULL CHECK(length(evidence_hash)=64),
 proof_hash TEXT NOT NULL CHECK(length(proof_hash)=64),
 body TEXT NOT NULL,
 created_at INTEGER NOT NULL,
 PRIMARY KEY(forecast_id,specification_hash)
);
CREATE TRIGGER resolution_timing_closures_immutable BEFORE UPDATE ON resolution_timing_closures
BEGIN SELECT RAISE(ABORT,'immutable_resolution_timing_closure'); END;
CREATE TRIGGER resolution_timing_closures_no_delete BEFORE DELETE ON resolution_timing_closures
BEGIN SELECT RAISE(ABORT,'immutable_resolution_timing_closure'); END;
-- A closure is only ever the conclusion of the review it names. It has to match
-- that review's reason and proof, name an evidence item the review itself
-- recorded as publication_time_unknown, and be written before the forecast has
-- any finalized result -- so it cannot be created after the fact to unblock a
-- forecast that already rewarded someone.
CREATE TRIGGER resolution_timing_closure_validate BEFORE INSERT ON resolution_timing_closures
BEGIN SELECT RAISE(ABORT,'resolution_timing_closure_mismatch') WHERE NOT EXISTS(
 SELECT 1 FROM resolution_timing_reviews r JOIN forecasts f ON f.id=r.forecast_id
 WHERE r.forecast_id=NEW.forecast_id AND r.specification_hash=NEW.specification_hash
  AND r.reason=NEW.reason AND r.proof_hash=NEW.review_proof_hash
  AND f.specification_hash=NEW.specification_hash
  AND f.state IN ('OPEN','LOCKED','RESOLVING')
  AND EXISTS(SELECT 1 FROM json_each(json_extract(r.body,'$.evidence')) e
   WHERE json_extract(e.value,'$.contentHash')=NEW.evidence_hash
    AND json_extract(e.value,'$.reason')='publication_time_unknown')
  AND json_extract(NEW.body,'$.forecastId')=NEW.forecast_id
  AND json_extract(NEW.body,'$.specificationHash')=NEW.specification_hash
  AND json_extract(NEW.body,'$.reason')=NEW.reason
  AND json_extract(NEW.body,'$.reviewProofHash')=NEW.review_proof_hash
  AND json_extract(NEW.body,'$.determination')=NEW.determination
  AND json_extract(NEW.body,'$.evidenceHash')=NEW.evidence_hash); END;
-- The only definition that changes. forecast_resolution_blockers and
-- resolution_timing_state_guard both name this view, and SQLite resolves a view
-- at query time, so redefining it here opens every consumer at once instead of
-- leaving four of them to be found later.
DROP VIEW unresolved_resolution_timing_reviews;
CREATE VIEW unresolved_resolution_timing_reviews AS
 SELECT r.* FROM resolution_timing_reviews r WHERE NOT EXISTS(
  SELECT 1 FROM forecast_eligibility_decisions d JOIN forecast_eligibility_completions c ON c.decision_id=d.id
  WHERE d.forecast_id=r.forecast_id AND d.specification_hash=r.specification_hash)
 AND NOT EXISTS(
  SELECT 1 FROM resolution_timing_closures x
  WHERE x.forecast_id=r.forecast_id AND x.specification_hash=r.specification_hash);
