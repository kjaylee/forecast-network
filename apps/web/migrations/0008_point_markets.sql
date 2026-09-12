-- Funded point-only markets. No funds are issued by this migration.
CREATE TABLE market_treasuries (
 mode TEXT PRIMARY KEY CHECK(mode IN ('shadow','active')),
 issued_atomic INTEGER NOT NULL DEFAULT 0 CHECK(typeof(issued_atomic)='integer' AND issued_atomic BETWEEN 0 AND 20000000000),
 available_atomic INTEGER NOT NULL DEFAULT 0 CHECK(typeof(available_atomic)='integer' AND available_atomic>=0),
 revision INTEGER NOT NULL DEFAULT 0
);
INSERT INTO market_treasuries(mode) VALUES('shadow'),('active');
CREATE TABLE market_funding (
 id TEXT PRIMARY KEY,mode TEXT NOT NULL REFERENCES market_treasuries(mode),
 amount_atomic INTEGER NOT NULL CHECK(typeof(amount_atomic)='integer' AND amount_atomic>0),
 created_at INTEGER NOT NULL
);
CREATE TABLE point_markets (
 forecast_id TEXT PRIMARY KEY REFERENCES forecasts(id),mode TEXT NOT NULL REFERENCES market_treasuries(mode),
 specification_hash TEXT NOT NULL CHECK(length(specification_hash)=64),
 policy_hash TEXT NOT NULL CHECK(length(policy_hash)=64),policy TEXT NOT NULL CHECK(json_valid(policy)),
 state TEXT NOT NULL CHECK(json_valid(state)),state_hash TEXT NOT NULL CHECK(length(state_hash)=64),
 revision INTEGER NOT NULL CHECK(revision>=0),reserve_atomic INTEGER NOT NULL CHECK(typeof(reserve_atomic)='integer' AND reserve_atomic>=0),
 gross_atomic INTEGER NOT NULL DEFAULT 0 CHECK(typeof(gross_atomic)='integer' AND gross_atomic BETWEEN 0 AND 2000000000),
 status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','settling','settled')),
 final_outcome TEXT CHECK(final_outcome IN ('YES','NO','INVALID')),
 created_at INTEGER NOT NULL,closed_at INTEGER
);
CREATE TRIGGER point_market_policy_immutable BEFORE UPDATE ON point_markets
BEGIN SELECT RAISE(ABORT,'immutable_market_policy') WHERE NEW.forecast_id!=OLD.forecast_id OR NEW.mode!=OLD.mode
 OR NEW.specification_hash!=OLD.specification_hash OR NEW.policy_hash!=OLD.policy_hash OR NEW.policy!=OLD.policy
 OR NEW.created_at!=OLD.created_at OR (OLD.final_outcome IS NOT NULL AND NEW.final_outcome IS NOT OLD.final_outcome); END;
CREATE TABLE market_shadow_accounts (
 user_id TEXT PRIMARY KEY REFERENCES users(id),available INTEGER NOT NULL DEFAULT 1000 CHECK(typeof(available)='integer' AND available BETWEEN 0 AND 9007199254740991),
 committed INTEGER NOT NULL DEFAULT 0 CHECK(typeof(committed)='integer' AND committed BETWEEN 0 AND 9007199254740991),updated_at INTEGER NOT NULL,
 CHECK(available+committed<=9007199254740991)
);
CREATE TABLE point_fractions (
 user_id TEXT NOT NULL REFERENCES users(id),mode TEXT NOT NULL REFERENCES market_treasuries(mode),
 remainder_atomic INTEGER NOT NULL DEFAULT 0 CHECK(typeof(remainder_atomic)='integer' AND remainder_atomic BETWEEN 0 AND 999999),
 PRIMARY KEY(user_id,mode)
);
CREATE TABLE market_quotes (
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),forecast_id TEXT NOT NULL REFERENCES point_markets(forecast_id),
 body TEXT NOT NULL CHECK(json_valid(body)),quote_hash TEXT NOT NULL CHECK(length(quote_hash)=64),
 created_at INTEGER NOT NULL,expires_at INTEGER NOT NULL CHECK(expires_at>created_at)
);
CREATE INDEX market_quote_owner ON market_quotes(user_id,forecast_id,created_at);
CREATE TABLE market_fills (
 id TEXT PRIMARY KEY,quote_id TEXT NOT NULL UNIQUE REFERENCES market_quotes(id),
 user_id TEXT NOT NULL REFERENCES users(id),forecast_id TEXT NOT NULL REFERENCES point_markets(forecast_id),
 idempotency_key TEXT NOT NULL,request_hash TEXT NOT NULL CHECK(length(request_hash)=64),
 body TEXT NOT NULL CHECK(json_valid(body)),revision INTEGER NOT NULL,
 side TEXT NOT NULL CHECK(side IN ('YES','NO')),spend INTEGER NOT NULL CHECK(typeof(spend)='integer' AND spend BETWEEN 1 AND 100),
 claims_atomic INTEGER NOT NULL CHECK(typeof(claims_atomic)='integer' AND claims_atomic>0),created_at INTEGER NOT NULL,
 UNIQUE(user_id,idempotency_key),UNIQUE(forecast_id,revision)
);
CREATE TABLE market_positions (
 user_id TEXT NOT NULL REFERENCES users(id),forecast_id TEXT NOT NULL REFERENCES point_markets(forecast_id),
 gross INTEGER NOT NULL DEFAULT 0 CHECK(typeof(gross)='integer' AND gross>=0),
 yes_claims_atomic INTEGER NOT NULL DEFAULT 0 CHECK(typeof(yes_claims_atomic)='integer' AND yes_claims_atomic>=0),
 no_claims_atomic INTEGER NOT NULL DEFAULT 0 CHECK(typeof(no_claims_atomic)='integer' AND no_claims_atomic>=0),
 settled INTEGER NOT NULL DEFAULT 0 CHECK(settled IN(0,1)),PRIMARY KEY(user_id,forecast_id)
);
CREATE TABLE market_settlements (
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),forecast_id TEXT NOT NULL REFERENCES point_markets(forecast_id),
 outcome TEXT NOT NULL CHECK(outcome IN ('YES','NO','INVALID')),
 gross INTEGER NOT NULL CHECK(typeof(gross)='integer' AND gross>0),
 payout_atomic INTEGER NOT NULL CHECK(typeof(payout_atomic)='integer' AND payout_atomic>=0),
 created_at INTEGER NOT NULL,UNIQUE(user_id,forecast_id)
);
CREATE TABLE market_closures (
 forecast_id TEXT PRIMARY KEY REFERENCES point_markets(forecast_id),
 returned_atomic INTEGER NOT NULL CHECK(typeof(returned_atomic)='integer' AND returned_atomic>=0),created_at INTEGER NOT NULL
);
CREATE TABLE market_account_ledger (
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),mode TEXT NOT NULL REFERENCES market_treasuries(mode),
 forecast_id TEXT NOT NULL REFERENCES point_markets(forecast_id),
 kind TEXT NOT NULL CHECK(kind IN ('market_buy','market_settlement')),
 available_delta INTEGER NOT NULL CHECK(typeof(available_delta)='integer'),
 committed_delta INTEGER NOT NULL CHECK(typeof(committed_delta)='integer'),
 available_after INTEGER NOT NULL CHECK(typeof(available_after)='integer' AND available_after>=0),
 committed_after INTEGER NOT NULL CHECK(typeof(committed_after)='integer' AND committed_after>=0),
 fraction_before INTEGER NOT NULL CHECK(typeof(fraction_before)='integer' AND fraction_before BETWEEN 0 AND 999999),
 fraction_after INTEGER NOT NULL CHECK(typeof(fraction_after)='integer' AND fraction_after BETWEEN 0 AND 999999),
 created_at INTEGER NOT NULL
);
CREATE TABLE market_write_guards(id TEXT PRIMARY KEY,passed INTEGER NOT NULL CHECK(passed=1));
CREATE TRIGGER market_ledger_validate BEFORE INSERT ON market_account_ledger
BEGIN
 SELECT RAISE(ABORT,'market_balance_changed') WHERE NOT EXISTS(
 SELECT 1 FROM point_accounts a WHERE NEW.mode='active' AND a.user_id=NEW.user_id
 AND NEW.available_after=a.available+NEW.available_delta AND NEW.committed_after=a.committed+NEW.committed_delta
 UNION ALL SELECT 1 FROM market_shadow_accounts a WHERE NEW.mode='shadow' AND a.user_id=NEW.user_id
 AND NEW.available_after=a.available+NEW.available_delta AND NEW.committed_after=a.committed+NEW.committed_delta);
 SELECT RAISE(ABORT,'market_fraction_changed') WHERE NEW.fraction_before!=COALESCE((SELECT remainder_atomic FROM point_fractions
 WHERE user_id=NEW.user_id AND mode=NEW.mode),0);
 SELECT RAISE(ABORT,'market_ledger_context') WHERE NOT EXISTS(SELECT 1 FROM point_markets m WHERE m.forecast_id=NEW.forecast_id AND m.mode=NEW.mode);
 SELECT RAISE(ABORT,'market_ledger_fill') WHERE NEW.kind='market_buy' AND NOT EXISTS(SELECT 1 FROM market_fills f
 WHERE f.id=NEW.id AND f.user_id=NEW.user_id AND f.forecast_id=NEW.forecast_id AND NEW.available_delta=-f.spend
 AND NEW.committed_delta=f.spend AND NEW.fraction_after=NEW.fraction_before);
 SELECT RAISE(ABORT,'market_ledger_settlement') WHERE NEW.kind='market_settlement' AND NOT EXISTS(SELECT 1 FROM market_settlements s
 WHERE s.id=NEW.id AND s.user_id=NEW.user_id AND s.forecast_id=NEW.forecast_id AND NEW.committed_delta=-s.gross
 AND NEW.available_delta*1000000+NEW.fraction_after-NEW.fraction_before=s.payout_atomic);
END;
CREATE TRIGGER market_ledger_apply AFTER INSERT ON market_account_ledger
BEGIN
 UPDATE point_accounts SET available=NEW.available_after,committed=NEW.committed_after,updated_at=MAX(updated_at,NEW.created_at)
 WHERE NEW.mode='active' AND user_id=NEW.user_id;
 UPDATE market_shadow_accounts SET available=NEW.available_after,committed=NEW.committed_after,updated_at=MAX(updated_at,NEW.created_at)
 WHERE NEW.mode='shadow' AND user_id=NEW.user_id;
 INSERT INTO point_fractions(user_id,mode,remainder_atomic) VALUES(NEW.user_id,NEW.mode,NEW.fraction_after)
 ON CONFLICT(user_id,mode) DO UPDATE SET remainder_atomic=excluded.remainder_atomic;
END;
-- Never mix the historical fixed-2x contract into a newly activated market.
CREATE TRIGGER legacy_reservation_no_active_market BEFORE INSERT ON point_ledger
BEGIN SELECT RAISE(ABORT,'active_market_requires_quote') WHERE NEW.kind='reservation' AND NEW.stake>0
 AND EXISTS(SELECT 1 FROM point_markets WHERE forecast_id=NEW.forecast_id AND mode='active'); END;
CREATE TRIGGER market_funding_immutable BEFORE UPDATE ON market_funding
BEGIN SELECT RAISE(ABORT,'immutable_market_record'); END;
CREATE TRIGGER market_funding_no_delete BEFORE DELETE ON market_funding
BEGIN SELECT RAISE(ABORT,'immutable_market_record'); END;
CREATE TRIGGER market_quotes_immutable BEFORE UPDATE ON market_quotes
BEGIN SELECT RAISE(ABORT,'immutable_market_record'); END;
CREATE TRIGGER market_quotes_no_delete BEFORE DELETE ON market_quotes
BEGIN SELECT RAISE(ABORT,'immutable_market_record'); END;
CREATE TRIGGER market_fills_immutable BEFORE UPDATE ON market_fills
BEGIN SELECT RAISE(ABORT,'immutable_market_record'); END;
CREATE TRIGGER market_fills_no_delete BEFORE DELETE ON market_fills
BEGIN SELECT RAISE(ABORT,'immutable_market_record'); END;
CREATE TRIGGER market_settlements_immutable BEFORE UPDATE ON market_settlements
BEGIN SELECT RAISE(ABORT,'immutable_market_record'); END;
CREATE TRIGGER market_settlements_no_delete BEFORE DELETE ON market_settlements
BEGIN SELECT RAISE(ABORT,'immutable_market_record'); END;
CREATE TRIGGER market_closures_immutable BEFORE UPDATE ON market_closures
BEGIN SELECT RAISE(ABORT,'immutable_market_record'); END;
CREATE TRIGGER market_closures_no_delete BEFORE DELETE ON market_closures
BEGIN SELECT RAISE(ABORT,'immutable_market_record'); END;
CREATE TRIGGER market_account_ledger_immutable BEFORE UPDATE ON market_account_ledger
BEGIN SELECT RAISE(ABORT,'immutable_market_record'); END;
CREATE TRIGGER market_account_ledger_no_delete BEFORE DELETE ON market_account_ledger
BEGIN SELECT RAISE(ABORT,'immutable_market_record'); END;
