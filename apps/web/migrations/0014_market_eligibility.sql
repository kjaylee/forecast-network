-- Late receipts retain their original prices and acquire an immutable refund.
-- No treasury issuance, transfer, or economic redemption is introduced.
CREATE TABLE market_fill_voids (
 fill_id TEXT PRIMARY KEY REFERENCES market_fills(id),
 decision_id TEXT NOT NULL REFERENCES forecast_eligibility_decisions(id),
 user_id TEXT NOT NULL REFERENCES users(id),
 forecast_id TEXT NOT NULL REFERENCES point_markets(forecast_id),
 mode TEXT NOT NULL CHECK(mode IN ('active','shadow')),
 spend INTEGER NOT NULL CHECK(typeof(spend)='integer' AND spend>0),
 claims_atomic INTEGER NOT NULL CHECK(typeof(claims_atomic)='integer' AND claims_atomic>0),
 side TEXT NOT NULL CHECK(side IN ('YES','NO')),
 available_before INTEGER NOT NULL CHECK(typeof(available_before)='integer' AND available_before>=0),
 committed_before INTEGER NOT NULL CHECK(typeof(committed_before)='integer' AND committed_before>=0),
 reserve_before_atomic INTEGER NOT NULL CHECK(typeof(reserve_before_atomic)='integer' AND reserve_before_atomic>=0),
 created_at INTEGER NOT NULL
);
CREATE INDEX market_void_owner ON market_fill_voids(user_id,mode,created_at);
CREATE INDEX market_void_forecast ON market_fill_voids(forecast_id);
CREATE TRIGGER market_void_immutable BEFORE UPDATE ON market_fill_voids
BEGIN SELECT RAISE(ABORT,'immutable_market_void'); END;
CREATE TRIGGER market_void_no_delete BEFORE DELETE ON market_fill_voids
BEGIN SELECT RAISE(ABORT,'immutable_market_void'); END;

CREATE VIEW market_effective_fills AS
SELECT f.* FROM market_fills f WHERE NOT EXISTS(SELECT 1 FROM market_fill_voids v WHERE v.fill_id=f.id);

-- These checks also run at the final eligibility barrier, independently of the
-- application read that prepares a bounded compensation batch.
CREATE VIEW market_eligibility_anomalies AS
SELECT m.forecast_id FROM point_markets m JOIN forecast_eligibility_decisions d ON d.forecast_id=m.forecast_id
WHERE m.specification_hash!=d.specification_hash
 OR m.revision!=(SELECT COUNT(*) FROM market_fills f WHERE f.forecast_id=m.forecast_id)
 OR m.revision!=COALESCE((SELECT MAX(f.revision) FROM market_fills f WHERE f.forecast_id=m.forecast_id),0)
 OR EXISTS(SELECT 1 FROM market_fills f WHERE f.forecast_id=m.forecast_id AND (f.revision<1
  OR json_extract(f.body,'$.state.revision') IS NOT f.revision
  OR json_extract(f.body,'$.fill.accepted_at_ms') IS NOT f.created_at
  OR json_extract(f.body,'$.fill.quote.owner_id') IS NOT f.user_id
  OR json_extract(f.body,'$.fill.quote.market_id') IS NOT f.forecast_id
  OR json_extract(f.body,'$.fill.quote.side') IS NOT f.side
  OR json_extract(f.body,'$.fill.quote.claims_atomic') IS NOT f.claims_atomic
  OR json_extract(f.body,'$.fill.quote.spend_atomic') IS NOT f.spend*1000000
  OR NOT EXISTS(SELECT 1 FROM market_account_ledger l WHERE l.id=f.id AND l.kind='market_buy'
   AND l.user_id=f.user_id AND l.forecast_id=f.forecast_id AND l.mode=m.mode
   AND l.available_delta=-f.spend AND l.committed_delta=f.spend)))
 OR m.gross_atomic!=COALESCE((SELECT SUM(f.spend)*1000000 FROM market_fills f WHERE f.forecast_id=m.forecast_id),0)
 OR json_extract(m.state,'$.deposits_atomic') IS NOT m.gross_atomic
 OR json_extract(m.state,'$.yes_claims_atomic') IS NOT COALESCE((SELECT SUM(f.claims_atomic) FROM market_fills f WHERE f.forecast_id=m.forecast_id AND f.side='YES'),0)
 OR json_extract(m.state,'$.no_claims_atomic') IS NOT COALESCE((SELECT SUM(f.claims_atomic) FROM market_fills f WHERE f.forecast_id=m.forecast_id AND f.side='NO'),0)
 OR EXISTS(SELECT 1 FROM market_settlements s WHERE s.forecast_id=m.forecast_id)
 OR EXISTS(SELECT 1 FROM market_closures c WHERE c.forecast_id=m.forecast_id)
 OR m.status!='open'
 OR EXISTS(SELECT 1 FROM market_fills late JOIN market_fills early ON early.forecast_id=late.forecast_id
  AND early.revision>late.revision WHERE late.forecast_id=m.forecast_id AND late.created_at>=d.cutoff_at AND early.created_at<d.cutoff_at)
 OR EXISTS(SELECT 1 FROM market_positions p WHERE p.forecast_id=m.forecast_id AND (
  p.gross!=COALESCE((SELECT SUM(f.spend) FROM market_effective_fills f WHERE f.forecast_id=p.forecast_id AND f.user_id=p.user_id),0)
  OR p.yes_claims_atomic!=COALESCE((SELECT SUM(f.claims_atomic) FROM market_effective_fills f WHERE f.forecast_id=p.forecast_id AND f.user_id=p.user_id AND f.side='YES'),0)
  OR p.no_claims_atomic!=COALESCE((SELECT SUM(f.claims_atomic) FROM market_effective_fills f WHERE f.forecast_id=p.forecast_id AND f.user_id=p.user_id AND f.side='NO'),0)
  OR (p.gross>0 AND p.settled!=0)))
 OR EXISTS(SELECT 1 FROM market_effective_fills f WHERE f.forecast_id=m.forecast_id AND NOT EXISTS(
  SELECT 1 FROM market_positions p WHERE p.forecast_id=f.forecast_id AND p.user_id=f.user_id))
 OR m.reserve_atomic IS NOT json_extract(m.policy,'$.subsidy_atomic')+COALESCE((SELECT SUM(f.spend)*1000000 FROM market_effective_fills f WHERE f.forecast_id=m.forecast_id),0)
 OR m.reserve_atomic<MAX(
  COALESCE((SELECT SUM(f.claims_atomic) FROM market_effective_fills f WHERE f.forecast_id=m.forecast_id AND f.side='YES'),0),
  COALESCE((SELECT SUM(f.claims_atomic) FROM market_effective_fills f WHERE f.forecast_id=m.forecast_id AND f.side='NO'),0),
  COALESCE((SELECT SUM(f.spend)*1000000 FROM market_effective_fills f WHERE f.forecast_id=m.forecast_id),0));

CREATE TRIGGER market_void_validate BEFORE INSERT ON market_fill_voids
BEGIN
 SELECT RAISE(ABORT,'market_void_context') WHERE NOT EXISTS(
  SELECT 1 FROM market_fills f JOIN point_markets m ON m.forecast_id=f.forecast_id
  JOIN forecast_eligibility_decisions d ON d.forecast_id=f.forecast_id AND d.specification_hash=m.specification_hash
  JOIN market_account_ledger l ON l.id=f.id AND l.kind='market_buy'
  WHERE f.id=NEW.fill_id AND d.id=NEW.decision_id AND f.created_at>=d.cutoff_at AND NEW.created_at>=d.created_at
  AND f.user_id=NEW.user_id AND f.forecast_id=NEW.forecast_id AND m.mode=NEW.mode
  AND f.spend=NEW.spend AND f.claims_atomic=NEW.claims_atomic AND f.side=NEW.side
  AND l.user_id=f.user_id AND l.forecast_id=f.forecast_id AND l.mode=m.mode
  AND l.available_delta=-f.spend AND l.committed_delta=f.spend AND m.reserve_atomic=NEW.reserve_before_atomic);
 SELECT RAISE(ABORT,'market_void_reconciliation') WHERE EXISTS(
  SELECT 1 FROM market_eligibility_anomalies WHERE forecast_id=NEW.forecast_id);
 SELECT RAISE(ABORT,'market_void_order') WHERE EXISTS(SELECT 1 FROM market_effective_fills f
  WHERE f.forecast_id=NEW.forecast_id AND f.revision>(SELECT revision FROM market_fills WHERE id=NEW.fill_id));
 SELECT RAISE(ABORT,'market_void_balance') WHERE NEW.committed_before<NEW.spend OR NOT EXISTS(
  SELECT 1 FROM point_accounts a WHERE NEW.mode='active' AND a.user_id=NEW.user_id
   AND a.available=NEW.available_before AND a.committed=NEW.committed_before
  UNION ALL SELECT 1 FROM market_shadow_accounts a WHERE NEW.mode='shadow' AND a.user_id=NEW.user_id
   AND a.available=NEW.available_before AND a.committed=NEW.committed_before);
 SELECT RAISE(ABORT,'market_void_reserve') WHERE NEW.reserve_before_atomic-NEW.spend*1000000<MAX(
  COALESCE((SELECT SUM(f.claims_atomic) FROM market_effective_fills f WHERE f.forecast_id=NEW.forecast_id AND f.side='YES' AND f.id!=NEW.fill_id),0),
  COALESCE((SELECT SUM(f.claims_atomic) FROM market_effective_fills f WHERE f.forecast_id=NEW.forecast_id AND f.side='NO' AND f.id!=NEW.fill_id),0),
  COALESCE((SELECT SUM(f.spend)*1000000 FROM market_effective_fills f WHERE f.forecast_id=NEW.forecast_id AND f.id!=NEW.fill_id),0));
END;
CREATE TRIGGER market_void_apply AFTER INSERT ON market_fill_voids
BEGIN
 UPDATE point_accounts SET available=available+NEW.spend,committed=committed-NEW.spend,updated_at=MAX(updated_at,NEW.created_at)
  WHERE NEW.mode='active' AND user_id=NEW.user_id;
 UPDATE market_shadow_accounts SET available=available+NEW.spend,committed=committed-NEW.spend,updated_at=MAX(updated_at,NEW.created_at)
  WHERE NEW.mode='shadow' AND user_id=NEW.user_id;
 UPDATE market_positions SET gross=gross-NEW.spend,
  yes_claims_atomic=yes_claims_atomic-( CASE WHEN NEW.side='YES' THEN NEW.claims_atomic ELSE 0 END ),
  no_claims_atomic=no_claims_atomic-( CASE WHEN NEW.side='NO' THEN NEW.claims_atomic ELSE 0 END ),
  settled=( CASE WHEN gross=NEW.spend THEN 1 ELSE 0 END )
  WHERE user_id=NEW.user_id AND forecast_id=NEW.forecast_id;
 UPDATE point_markets SET reserve_atomic=reserve_atomic-NEW.spend*1000000 WHERE forecast_id=NEW.forecast_id;
END;

CREATE TRIGGER market_eligibility_completion BEFORE INSERT ON forecast_eligibility_completions
BEGIN
 SELECT RAISE(ABORT,'market_eligibility_pending') WHERE EXISTS(
  SELECT 1 FROM forecast_eligibility_decisions d JOIN point_markets m ON m.forecast_id=d.forecast_id AND m.mode='active'
  JOIN market_effective_fills f ON f.forecast_id=m.forecast_id
  WHERE d.id=NEW.decision_id AND (f.created_at>=d.cutoff_at OR d.event_time_basis='observed_upper_bound'));
 SELECT RAISE(ABORT,'market_eligibility_review') WHERE EXISTS(
  SELECT 1 FROM forecast_eligibility_decisions d JOIN market_eligibility_anomalies a ON a.forecast_id=d.forecast_id
  JOIN point_markets m ON m.forecast_id=d.forecast_id AND m.mode='active' WHERE d.id=NEW.decision_id);
END;
CREATE TRIGGER market_cutoff_fill BEFORE INSERT ON market_fills
BEGIN SELECT RAISE(ABORT,'participation_on_hold') WHERE EXISTS(
 SELECT 1 FROM forecast_eligibility_decisions WHERE forecast_id=NEW.forecast_id); END;
CREATE TRIGGER market_cutoff_quote BEFORE INSERT ON market_quotes
BEGIN SELECT RAISE(ABORT,'participation_on_hold') WHERE EXISTS(
 SELECT 1 FROM forecast_eligibility_decisions WHERE forecast_id=NEW.forecast_id); END;
