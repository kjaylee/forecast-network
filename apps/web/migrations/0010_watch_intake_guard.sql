-- Close the observation-to-review gap for legacy forecasts as well as market fills.
-- submitted_at is supplied by the trusted application clock, not the HTTP client.
CREATE TRIGGER source_watch_vote_insert BEFORE INSERT ON user_forecasts
BEGIN SELECT RAISE(ABORT,'participation_on_hold') WHERE EXISTS(
 SELECT 1 FROM official_watch_bindings b JOIN official_watch_sources s
 ON (s.id=b.source_id OR s.parent_id=b.source_id)
 WHERE b.forecast_id=NEW.forecast_id AND s.enabled=1 AND s.lease_until>NEW.submitted_at); END;
CREATE TRIGGER source_watch_vote_update BEFORE UPDATE ON user_forecasts
BEGIN SELECT RAISE(ABORT,'participation_on_hold') WHERE EXISTS(
 SELECT 1 FROM official_watch_bindings b JOIN official_watch_sources s
 ON (s.id=b.source_id OR s.parent_id=b.source_id)
 WHERE b.forecast_id=NEW.forecast_id AND s.enabled=1 AND s.lease_until>NEW.submitted_at); END;
