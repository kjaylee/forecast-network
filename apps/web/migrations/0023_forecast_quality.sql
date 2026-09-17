-- Current-state quality history. Historical replays use retained cutoff snapshots.
CREATE INDEX events_finalization_quality ON events(forecast_id,created_at)
 WHERE json_extract(event,'$.command_name')='finalize';
CREATE INDEX reputation_category_user_quality ON reputation_scores(category,user_id,created_at);
CREATE VIEW forecast_quality_finalizations AS
 SELECT forecast_id,MIN(created_at) AS finalized_at FROM events
 WHERE json_extract(event,'$.command_name')='finalize' GROUP BY forecast_id;
CREATE VIEW forecast_quality_history AS
 SELECT s.*,f.state,f.finalized_outcome,u.submitted_at,z.finalized_at,
 MAX(s.created_at,z.finalized_at,COALESCE(c.created_at,0)) AS eligibility_at,1 AS eligible
 FROM eligible_reputation_scores s
 JOIN forecasts f ON f.id=s.forecast_id
 JOIN forecast_quality_finalizations z ON z.forecast_id=s.forecast_id
 JOIN eligible_user_forecasts u ON u.forecast_id=s.forecast_id AND u.user_id=s.user_id
  AND u.yes_probability=s.probability
 LEFT JOIN forecast_eligibility_decisions d ON d.forecast_id=f.id
 LEFT JOIN forecast_eligibility_completions c ON c.decision_id=d.id
 WHERE f.state IN ('FINALIZED','ARCHIVED') AND s.outcome=f.finalized_outcome
 AND s.outcome IN ('YES','NO') AND typeof(s.probability)='integer' AND s.probability BETWEEN 0 AND 100
 AND u.submitted_at<=z.finalized_at AND (d.id IS NULL OR c.decision_id IS NOT NULL)
 AND NOT EXISTS(SELECT 1 FROM active_participation_holds h WHERE h.forecast_id=f.id);
