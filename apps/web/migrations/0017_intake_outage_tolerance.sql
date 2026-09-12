-- Participation no longer closes because an official-source watch is failing, stale,
-- disabled or mid-poll. Those conditions describe our own infrastructure, not new
-- evidence. Late entries that follow real evidence are still voided retroactively by
-- the receipt-eligibility cutoff, and operator/automatic holds and pending reviews
-- keep closing intake immediately. Point-market fills keep their stricter guards.
DROP TRIGGER IF EXISTS source_watch_vote_insert;
DROP TRIGGER IF EXISTS source_watch_vote_update;
DROP TRIGGER IF EXISTS forecast_watch_health_insert;
DROP TRIGGER IF EXISTS forecast_watch_health_update;
CREATE TRIGGER forecast_review_intake_insert BEFORE INSERT ON user_forecasts
BEGIN SELECT RAISE(ABORT,'participation_on_hold') WHERE
 EXISTS(SELECT 1 FROM forecast_intake_review_blockers WHERE forecast_id=NEW.forecast_id); END;
CREATE TRIGGER forecast_review_intake_update BEFORE UPDATE ON user_forecasts
BEGIN SELECT RAISE(ABORT,'participation_on_hold') WHERE
 EXISTS(SELECT 1 FROM forecast_intake_review_blockers WHERE forecast_id=NEW.forecast_id); END;
