-- Evidence cutoffs are append-only overlays; accepted commands and stakes remain
-- audit evidence. Equality with a precise publication timestamp is already late.
CREATE TABLE forecast_eligibility_decisions (
 id TEXT PRIMARY KEY REFERENCES artifacts(hash),
 forecast_id TEXT NOT NULL UNIQUE REFERENCES forecasts(id),
 specification_hash TEXT NOT NULL CHECK(length(specification_hash)=64),
 cutoff_at INTEGER NOT NULL,
 event_time_basis TEXT NOT NULL CHECK(event_time_basis IN ('published_instant','observed_upper_bound')),
 created_at INTEGER NOT NULL,
 body TEXT NOT NULL
);
CREATE TABLE forecast_receipt_eligibility (
 decision_id TEXT NOT NULL REFERENCES forecast_eligibility_decisions(id),
 forecast_id TEXT NOT NULL REFERENCES forecasts(id),
 user_id TEXT NOT NULL REFERENCES users(id),
 revision INTEGER NOT NULL CHECK(revision>0),
 receipt_hash TEXT NOT NULL CHECK(length(receipt_hash)=64),
 status TEXT NOT NULL CHECK(status IN ('eligible','void','review')),
 body TEXT NOT NULL,
 submitted_at INTEGER NOT NULL,
 PRIMARY KEY(decision_id,revision)
);
CREATE INDEX forecast_receipt_eligibility_user ON forecast_receipt_eligibility(forecast_id,user_id,revision);
CREATE TABLE forecast_eligibility_completions (
 decision_id TEXT PRIMARY KEY REFERENCES forecast_eligibility_decisions(id),
 created_at INTEGER NOT NULL
);
-- Mutable delivery scheduling is separate from the immutable policy/audit record.
CREATE TABLE forecast_eligibility_retry (
 decision_id TEXT PRIMARY KEY REFERENCES forecast_eligibility_decisions(id),
 attempts INTEGER NOT NULL DEFAULT 0 CHECK(typeof(attempts)='integer' AND attempts>=0),
 next_attempt INTEGER NOT NULL DEFAULT 0 CHECK(typeof(next_attempt)='integer' AND next_attempt>=0)
);
CREATE INDEX forecast_eligibility_retry_due ON forecast_eligibility_retry(next_attempt,decision_id);
CREATE TRIGGER forecast_eligibility_retry_create AFTER INSERT ON forecast_eligibility_decisions
BEGIN INSERT INTO forecast_eligibility_retry(decision_id) VALUES(NEW.id); END;
CREATE TABLE point_eligibility_adjustments (
 id TEXT PRIMARY KEY,
 decision_id TEXT NOT NULL REFERENCES forecast_eligibility_decisions(id),
 forecast_id TEXT NOT NULL REFERENCES forecasts(id),
 user_id TEXT NOT NULL REFERENCES point_accounts(user_id),
 old_amount INTEGER NOT NULL CHECK(old_amount BETWEEN 0 AND 1000),
 new_amount INTEGER NOT NULL CHECK(new_amount BETWEEN 0 AND 1000),
 old_revision INTEGER NOT NULL CHECK(old_revision>=0),
 new_revision INTEGER NOT NULL CHECK(new_revision>0),
 outcome TEXT NOT NULL CHECK(outcome IN ('YES','NO')),
 available_delta INTEGER NOT NULL,
 committed_delta INTEGER NOT NULL,
 available_after INTEGER NOT NULL,
 committed_after INTEGER NOT NULL,
 created_at INTEGER NOT NULL,
 UNIQUE(decision_id,user_id),
 CHECK(available_delta=old_amount-new_amount AND committed_delta=new_amount-old_amount)
);
CREATE VIEW eligible_user_forecasts AS
 SELECT f.forecast_id,f.user_id,f.outcome,f.confidence,f.yes_probability,f.submitted_at,f.revision,f.body
 FROM user_forecasts f WHERE NOT EXISTS(SELECT 1 FROM forecast_eligibility_decisions d WHERE d.forecast_id=f.forecast_id)
 UNION ALL
 SELECT r.forecast_id,r.user_id,json_extract(r.body,'$.outcome'),json_extract(r.body,'$.confidence'),
 ( CASE WHEN json_extract(r.body,'$.outcome')='YES' THEN json_extract(r.body,'$.confidence') ELSE 100-json_extract(r.body,'$.confidence') END ) ,
 r.submitted_at,r.revision,r.body FROM forecast_receipt_eligibility r
 WHERE r.status='eligible' AND NOT EXISTS(SELECT 1 FROM forecast_receipt_eligibility later
  WHERE later.decision_id=r.decision_id AND later.user_id=r.user_id AND later.status='eligible' AND later.revision>r.revision);
-- This expectation is recomputed from all classified canonical receipts. Both
-- correction and completion verify it, so a stale correction cannot certify a
-- later projection that restores a different receipt or stake.
CREATE VIEW forecast_eligibility_expected_positions AS
 SELECT r.decision_id,r.forecast_id,r.user_id,r.revision,
  json_extract(r.body,'$.outcome') AS outcome,
  ( CASE WHEN r.status='void' THEN 0 ELSE COALESCE(l.stake,0) END ) AS amount
 FROM forecast_receipt_eligibility r LEFT JOIN point_ledger l
  ON l.forecast_id=r.forecast_id AND l.user_id=r.user_id AND l.kind='reservation' AND l.forecast_revision=r.revision
 WHERE (r.status IN ('eligible','review') AND NOT EXISTS(SELECT 1 FROM forecast_receipt_eligibility z
  WHERE z.decision_id=r.decision_id AND z.user_id=r.user_id AND z.status IN ('eligible','review') AND z.revision>r.revision))
 OR (r.status='void' AND NOT EXISTS(SELECT 1 FROM forecast_receipt_eligibility z
  WHERE z.decision_id=r.decision_id AND z.user_id=r.user_id AND (z.status IN ('eligible','review') OR z.revision>r.revision)));
CREATE VIEW eligible_reputation_scores AS
 SELECT s.forecast_id,s.user_id,s.category,s.outcome,s.probability,s.correct,s.brier_score,s.created_at
 FROM reputation_scores s WHERE NOT EXISTS(SELECT 1 FROM forecast_eligibility_decisions d WHERE d.forecast_id=s.forecast_id)
 OR EXISTS(SELECT 1 FROM forecast_eligibility_decisions d JOIN forecast_eligibility_completions c ON c.decision_id=d.id
  JOIN eligible_user_forecasts u ON u.forecast_id=d.forecast_id AND u.user_id=s.user_id
  JOIN forecasts f ON f.id=d.forecast_id
  WHERE d.forecast_id=s.forecast_id AND s.outcome=f.finalized_outcome AND u.yes_probability=s.probability
   AND s.correct IS ( CASE WHEN f.finalized_outcome='INVALID' THEN NULL WHEN u.outcome=f.finalized_outcome THEN 1 ELSE 0 END )
   AND s.brier_score IS ( CASE WHEN f.finalized_outcome='INVALID' THEN NULL ELSE
    (u.yes_probability/100.0- ( CASE WHEN f.finalized_outcome='YES' THEN 1 ELSE 0 END ) ) *
    (u.yes_probability/100.0- ( CASE WHEN f.finalized_outcome='YES' THEN 1 ELSE 0 END ) ) END ) );

CREATE TRIGGER forecast_eligibility_decisions_immutable BEFORE UPDATE ON forecast_eligibility_decisions
BEGIN SELECT RAISE(ABORT,'immutable_eligibility_decision'); END;
CREATE TRIGGER forecast_eligibility_decisions_no_delete BEFORE DELETE ON forecast_eligibility_decisions
BEGIN SELECT RAISE(ABORT,'immutable_eligibility_decision'); END;
CREATE TRIGGER forecast_receipt_eligibility_immutable BEFORE UPDATE ON forecast_receipt_eligibility
BEGIN SELECT RAISE(ABORT,'immutable_receipt_eligibility'); END;
CREATE TRIGGER forecast_receipt_eligibility_no_delete BEFORE DELETE ON forecast_receipt_eligibility
BEGIN SELECT RAISE(ABORT,'immutable_receipt_eligibility'); END;
CREATE TRIGGER forecast_eligibility_completions_immutable BEFORE UPDATE ON forecast_eligibility_completions
BEGIN SELECT RAISE(ABORT,'immutable_eligibility_completion'); END;
CREATE TRIGGER forecast_eligibility_completions_no_delete BEFORE DELETE ON forecast_eligibility_completions
BEGIN SELECT RAISE(ABORT,'immutable_eligibility_completion'); END;
CREATE TRIGGER point_eligibility_adjustments_immutable BEFORE UPDATE ON point_eligibility_adjustments
BEGIN SELECT RAISE(ABORT,'immutable_eligibility_adjustment'); END;
CREATE TRIGGER point_eligibility_adjustments_no_delete BEFORE DELETE ON point_eligibility_adjustments
BEGIN SELECT RAISE(ABORT,'immutable_eligibility_adjustment'); END;

CREATE TRIGGER forecast_eligibility_decision_validate BEFORE INSERT ON forecast_eligibility_decisions
BEGIN SELECT RAISE(ABORT,'eligibility_decision_mismatch') WHERE NOT EXISTS(
 SELECT 1 FROM forecasts f JOIN artifacts a ON a.hash=NEW.id
 WHERE f.id=NEW.forecast_id AND f.specification_hash=NEW.specification_hash AND a.body=NEW.body
 AND json_extract(NEW.body,'$.forecast_id')=NEW.forecast_id
 AND json_extract(NEW.body,'$.specification_hash')=NEW.specification_hash
 AND json_extract(NEW.body,'$.event_at_ms')=NEW.cutoff_at
 AND json_extract(NEW.body,'$.event_time_basis')=NEW.event_time_basis); END;
CREATE TRIGGER forecast_receipt_eligibility_validate BEFORE INSERT ON forecast_receipt_eligibility
BEGIN SELECT RAISE(ABORT,'receipt_eligibility_mismatch') WHERE NOT EXISTS(
 SELECT 1 FROM forecast_eligibility_decisions d JOIN command_receipts c ON c.forecast_id=d.forecast_id
 JOIN events e ON e.forecast_id=d.forecast_id AND e.revision=NEW.revision
 WHERE d.id=NEW.decision_id AND d.forecast_id=NEW.forecast_id
 AND json_extract(c.receipt,'$.revision')=NEW.revision
 AND json_extract(c.receipt,'$.event_hash')=e.hash
 AND json_extract(e.event,'$.command_name')='submit_forecast'
 AND json_extract(c.receipt,'$.accepted_user_forecast')=json(NEW.body)
 AND json_extract(NEW.body,'$.forecaster_id')=NEW.user_id
 AND json_extract(NEW.body,'$.submitted_at_ms')=NEW.submitted_at
 AND NEW.status= ( CASE WHEN NEW.submitted_at>=d.cutoff_at THEN 'void'
   WHEN d.event_time_basis='published_instant' THEN 'eligible' ELSE 'review' END ) ); END;

CREATE TRIGGER point_eligibility_adjustment_validate BEFORE INSERT ON point_eligibility_adjustments
BEGIN
 -- No money-like point effects may precede complete receipt classification.
 SELECT RAISE(ABORT,'eligibility_receipts_incomplete') WHERE
  EXISTS(SELECT 1 FROM events e WHERE e.forecast_id=NEW.forecast_id
   AND json_extract(e.event,'$.command_name')='submit_forecast'
   AND NOT EXISTS(SELECT 1 FROM forecast_receipt_eligibility r WHERE r.decision_id=NEW.decision_id AND r.revision=e.revision))
  OR EXISTS(SELECT 1 FROM command_receipts c WHERE c.forecast_id=NEW.forecast_id
   AND json_extract(c.receipt,'$.accepted_user_forecast') IS NOT NULL
   AND NOT EXISTS(SELECT 1 FROM forecast_receipt_eligibility r WHERE r.decision_id=NEW.decision_id
    AND r.revision=json_extract(c.receipt,'$.revision')
    AND json(r.body)=json_extract(c.receipt,'$.accepted_user_forecast')))
  OR NOT EXISTS(SELECT 1 FROM user_forecasts u JOIN forecast_receipt_eligibility r
   ON r.forecast_id=u.forecast_id AND r.user_id=u.user_id AND r.revision=u.revision AND r.body=u.body
   WHERE u.forecast_id=NEW.forecast_id AND u.user_id=NEW.user_id AND r.decision_id=NEW.decision_id);
 SELECT RAISE(ABORT,'eligibility_adjustment_conflict') WHERE NOT EXISTS(
  SELECT 1 FROM forecast_eligibility_decisions d JOIN forecasts f ON f.id=d.forecast_id
  JOIN point_accounts a ON a.user_id=NEW.user_id
  LEFT JOIN point_positions p ON p.user_id=NEW.user_id AND p.forecast_id=f.id
  WHERE d.id=NEW.decision_id AND f.id=NEW.forecast_id AND f.state NOT IN ('FINALIZED','ARCHIVED')
  AND NOT EXISTS(SELECT 1 FROM reputation_scores WHERE forecast_id=f.id)
  AND NOT EXISTS(SELECT 1 FROM point_ledger WHERE forecast_id=f.id AND kind='settlement')
  AND (p.user_id IS NULL OR p.status IN ('practice','committed'))
  AND COALESCE(p.amount,0)=NEW.old_amount AND COALESCE(p.forecast_revision,0)=NEW.old_revision
  AND a.available+NEW.available_delta=NEW.available_after AND a.committed+NEW.committed_delta=NEW.committed_after
  AND NEW.available_after>=0 AND NEW.committed_after>=0
  AND NEW.available_after+NEW.committed_after<=9007199254740991);
 -- Restoring an older receipt retains its original side and reserved amount.
 SELECT RAISE(ABORT,'eligibility_adjustment_target') WHERE NOT EXISTS(
  SELECT 1 FROM forecast_eligibility_expected_positions p WHERE p.decision_id=NEW.decision_id
   AND p.forecast_id=NEW.forecast_id AND p.user_id=NEW.user_id AND p.revision=NEW.new_revision
   AND p.amount=NEW.new_amount AND p.outcome=NEW.outcome);
END;
CREATE TRIGGER point_eligibility_adjustment_apply AFTER INSERT ON point_eligibility_adjustments
BEGIN
 UPDATE point_accounts SET available=NEW.available_after,committed=NEW.committed_after,
  updated_at=MAX(updated_at,NEW.created_at) WHERE user_id=NEW.user_id;
 UPDATE point_positions SET amount=NEW.new_amount,outcome=NEW.outcome,forecast_revision=NEW.new_revision,
  status= ( CASE WHEN NEW.new_amount=0 THEN 'practice' ELSE 'committed' END ) ,updated_at=NEW.created_at
  WHERE user_id=NEW.user_id AND forecast_id=NEW.forecast_id;
 INSERT OR IGNORE INTO activity(id,user_id,forecast_id,kind,title,body,created_at)
 VALUES('eligibility:'||NEW.id,NEW.user_id,NEW.forecast_id,'forecast_eligibility','Forecast evidence timing reviewed',
  'Entries at or after the evidence cutoff are void. Earlier eligible forecasts keep their original position. Check the forecast for details.',NEW.created_at);
END;

-- An unresolved withdrawal cannot be spent elsewhere to escape restoring a loss.
CREATE VIEW point_eligibility_account_holds AS
 SELECT DISTINCT r.user_id FROM forecast_receipt_eligibility r
 WHERE NOT EXISTS(SELECT 1 FROM point_eligibility_adjustments a
 WHERE a.decision_id=r.decision_id AND a.user_id=r.user_id)
 UNION SELECT u.user_id FROM user_forecasts u JOIN forecast_eligibility_decisions d ON d.forecast_id=u.forecast_id
 WHERE NOT EXISTS(SELECT 1 FROM forecast_receipt_eligibility r
 WHERE r.decision_id=d.id AND r.user_id=u.user_id AND r.revision=u.revision);
CREATE TRIGGER point_eligibility_reservation_freeze BEFORE INSERT ON point_ledger
BEGIN SELECT RAISE(ABORT,'eligibility_account_hold') WHERE NEW.kind='reservation'
 AND EXISTS(SELECT 1 FROM point_eligibility_account_holds WHERE user_id=NEW.user_id); END;
CREATE TRIGGER point_eligibility_market_freeze BEFORE INSERT ON market_account_ledger
BEGIN SELECT RAISE(ABORT,'eligibility_account_hold') WHERE NEW.kind='market_buy'
 AND EXISTS(SELECT 1 FROM point_eligibility_account_holds WHERE user_id=NEW.user_id); END;
CREATE TRIGGER forecast_eligibility_intake_insert BEFORE INSERT ON user_forecasts
BEGIN SELECT RAISE(ABORT,'participation_on_hold') WHERE EXISTS(
 SELECT 1 FROM forecast_eligibility_decisions WHERE forecast_id=NEW.forecast_id); END;
CREATE TRIGGER forecast_eligibility_intake_update BEFORE UPDATE ON user_forecasts
BEGIN SELECT RAISE(ABORT,'participation_on_hold') WHERE EXISTS(
 SELECT 1 FROM forecast_eligibility_decisions WHERE forecast_id=NEW.forecast_id); END;
CREATE TRIGGER forecast_eligibility_market_intake BEFORE INSERT ON market_fills
BEGIN SELECT RAISE(ABORT,'participation_on_hold') WHERE EXISTS(
 SELECT 1 FROM forecast_eligibility_decisions WHERE forecast_id=NEW.forecast_id); END;

CREATE TRIGGER forecast_eligibility_completion_validate BEFORE INSERT ON forecast_eligibility_completions
BEGIN SELECT RAISE(ABORT,'eligibility_incomplete') WHERE
 EXISTS(SELECT 1 FROM forecast_receipt_eligibility WHERE decision_id=NEW.decision_id AND status='review')
 OR EXISTS(SELECT 1 FROM forecast_eligibility_decisions d JOIN events e ON e.forecast_id=d.forecast_id
  WHERE d.id=NEW.decision_id AND json_extract(e.event,'$.command_name')='submit_forecast'
  AND NOT EXISTS(SELECT 1 FROM forecast_receipt_eligibility r WHERE r.decision_id=d.id AND r.revision=e.revision))
 OR EXISTS(SELECT 1 FROM forecast_eligibility_decisions d JOIN user_forecasts u ON u.forecast_id=d.forecast_id
  WHERE d.id=NEW.decision_id AND NOT EXISTS(SELECT 1 FROM forecast_receipt_eligibility r
   WHERE r.decision_id=d.id AND r.user_id=u.user_id AND r.revision=u.revision AND r.body=u.body))
 OR EXISTS(SELECT 1 FROM forecast_receipt_eligibility r WHERE r.decision_id=NEW.decision_id AND NOT EXISTS(
  SELECT 1 FROM point_eligibility_adjustments a WHERE a.decision_id=r.decision_id AND a.user_id=r.user_id))
 OR EXISTS(SELECT 1 FROM forecast_eligibility_decisions d JOIN command_receipts c ON c.forecast_id=d.forecast_id
  WHERE d.id=NEW.decision_id AND json_extract(c.receipt,'$.accepted_user_forecast') IS NOT NULL
  AND NOT EXISTS(SELECT 1 FROM forecast_receipt_eligibility r WHERE r.decision_id=d.id
   AND r.revision=json_extract(c.receipt,'$.revision') AND json(r.body)=json_extract(c.receipt,'$.accepted_user_forecast')))
 OR EXISTS(SELECT 1 FROM point_eligibility_adjustments a WHERE a.decision_id=NEW.decision_id AND NOT EXISTS(
  SELECT 1 FROM forecast_eligibility_expected_positions e LEFT JOIN point_positions p
   ON p.forecast_id=e.forecast_id AND p.user_id=e.user_id
  WHERE e.decision_id=a.decision_id AND e.forecast_id=a.forecast_id AND e.user_id=a.user_id
   AND e.revision=a.new_revision AND e.amount=a.new_amount AND e.outcome=a.outcome
   AND ((p.forecast_revision=e.revision AND p.amount=e.amount AND p.outcome=e.outcome
    AND p.status= ( CASE WHEN e.amount=0 THEN 'practice' ELSE 'committed' END ) )
    OR (p.user_id IS NULL AND a.old_revision=0 AND e.amount=0))))
 OR EXISTS(SELECT 1 FROM forecast_eligibility_decisions d JOIN forecasts f ON f.id=d.forecast_id
  WHERE d.id=NEW.decision_id AND (f.state IN ('FINALIZED','ARCHIVED')
   OR EXISTS(SELECT 1 FROM reputation_scores WHERE forecast_id=f.id)
   OR EXISTS(SELECT 1 FROM point_ledger WHERE forecast_id=f.id AND kind='settlement'))); END;

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
 ));
CREATE TRIGGER forecast_eligibility_points_effective BEFORE INSERT ON point_ledger
BEGIN SELECT RAISE(ABORT,'ineligible_forecast_settlement') WHERE NEW.kind='settlement'
 AND EXISTS(SELECT 1 FROM forecast_eligibility_decisions WHERE forecast_id=NEW.forecast_id)
 AND NOT EXISTS(SELECT 1 FROM eligible_user_forecasts u JOIN point_positions p
  ON p.forecast_id=u.forecast_id AND p.user_id=u.user_id
  WHERE u.forecast_id=NEW.forecast_id AND u.user_id=NEW.user_id AND u.outcome=NEW.outcome
  AND p.forecast_revision=u.revision AND p.amount=NEW.stake); END;
CREATE TRIGGER forecast_eligibility_reputation_effective BEFORE INSERT ON reputation_scores
BEGIN SELECT RAISE(ABORT,'ineligible_forecast_reputation') WHERE
 EXISTS(SELECT 1 FROM forecast_eligibility_decisions WHERE forecast_id=NEW.forecast_id)
 AND NOT EXISTS(SELECT 1 FROM eligible_user_forecasts u JOIN forecasts f ON f.id=u.forecast_id
  WHERE u.forecast_id=NEW.forecast_id AND u.user_id=NEW.user_id
  AND f.finalized_outcome=NEW.outcome AND u.yes_probability=NEW.probability
  AND NEW.correct IS ( CASE WHEN f.finalized_outcome='INVALID' THEN NULL WHEN u.outcome=f.finalized_outcome THEN 1 ELSE 0 END )
  AND NEW.brier_score IS ( CASE WHEN f.finalized_outcome='INVALID' THEN NULL ELSE
   (u.yes_probability/100.0- ( CASE WHEN f.finalized_outcome='YES' THEN 1 ELSE 0 END ) ) *
   (u.yes_probability/100.0- ( CASE WHEN f.finalized_outcome='YES' THEN 1 ELSE 0 END ) ) END ) ); END;
