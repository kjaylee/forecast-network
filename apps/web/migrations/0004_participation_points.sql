-- Non-purchasable, non-transferable, non-redeemable participation points.
-- Reputation and crowd probabilities never use these balances or stakes.
-- Remote D1 rejects unparenthesized trigger CASE expressions (workers-sdk #4727).
-- Guards use SELECT RAISE ... WHERE; value CASE expressions are parenthesized.
-- Keep whitespace before CASE and after END: Wrangler 4.130's client splitter
-- recognizes only whitespace-delimited compound tokens.
CREATE TABLE point_policies (
    version TEXT PRIMARY KEY,
    profile_grant INTEGER NOT NULL CHECK(typeof(profile_grant)='integer' AND profile_grant>=0),
    wallet_grant INTEGER NOT NULL CHECK(typeof(wallet_grant)='integer' AND wallet_grant>=0),
    max_stake INTEGER NOT NULL CHECK(typeof(max_stake)='integer' AND max_stake>0),
    win_return_multiplier INTEGER NOT NULL CHECK(win_return_multiplier=2),
    invalid_return_multiplier INTEGER NOT NULL CHECK(invalid_return_multiplier=1)
);
INSERT INTO point_policies VALUES('participation-points-v1',1000,500,1000,2,1);
CREATE TRIGGER point_policies_immutable BEFORE UPDATE ON point_policies
BEGIN SELECT RAISE(ABORT,'immutable_points_policy'); END;
CREATE TRIGGER point_policies_no_delete BEFORE DELETE ON point_policies
BEGIN SELECT RAISE(ABORT,'immutable_points_policy'); END;

CREATE TABLE point_accounts (
    user_id TEXT PRIMARY KEY REFERENCES users(id),
    available INTEGER NOT NULL DEFAULT 0,
    committed INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL,
    CHECK(typeof(available)='integer' AND available BETWEEN 0 AND 9007199254740991),
    CHECK(typeof(committed)='integer' AND committed BETWEEN 0 AND 9007199254740991),
    CHECK(available+committed<=9007199254740991)
);
CREATE TABLE point_awards (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES point_accounts(user_id),
    kind TEXT NOT NULL CHECK(kind IN ('profile','wallet')),
    wallet_address TEXT UNIQUE,
    amount INTEGER NOT NULL CHECK(typeof(amount)='integer' AND amount>=0),
    policy_version TEXT NOT NULL REFERENCES point_policies(version),
    created_at INTEGER NOT NULL,
    UNIQUE(user_id,kind),
    CHECK((kind='profile' AND wallet_address IS NULL) OR (kind='wallet' AND wallet_address IS NOT NULL))
);
CREATE TABLE point_positions (
    user_id TEXT NOT NULL REFERENCES point_accounts(user_id),
    forecast_id TEXT NOT NULL REFERENCES forecasts(id),
    amount INTEGER NOT NULL CHECK(typeof(amount)='integer' AND amount BETWEEN 0 AND 1000),
    outcome TEXT NOT NULL CHECK(outcome IN ('YES','NO')),
    forecast_revision INTEGER NOT NULL CHECK(typeof(forecast_revision)='integer' AND forecast_revision>0),
    policy_version TEXT NOT NULL REFERENCES point_policies(version),
    status TEXT NOT NULL CHECK(status IN ('practice','committed','settled')),
    returned INTEGER CHECK(returned IS NULL OR (typeof(returned)='integer' AND returned BETWEEN 0 AND 2000)),
    settlement_id TEXT UNIQUE,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(user_id,forecast_id),
    CHECK((status='practice' AND amount=0 AND returned IS NULL AND settlement_id IS NULL)
       OR (status='committed' AND amount>0 AND returned IS NULL AND settlement_id IS NULL)
       OR (status='settled' AND amount>0 AND returned IS NOT NULL AND settlement_id IS NOT NULL))
);
CREATE INDEX point_positions_forecast ON point_positions(forecast_id,status);
CREATE TABLE point_ledger (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES point_accounts(user_id),
    kind TEXT NOT NULL CHECK(kind IN ('profile_grant','wallet_grant','reservation','settlement')),
    forecast_id TEXT REFERENCES forecasts(id),
    award_id TEXT UNIQUE REFERENCES point_awards(id),
    operation_id TEXT,
    request_hash TEXT,
    available_delta INTEGER NOT NULL CHECK(typeof(available_delta)='integer'),
    committed_delta INTEGER NOT NULL CHECK(typeof(committed_delta)='integer'),
    available_after INTEGER NOT NULL CHECK(typeof(available_after)='integer'),
    committed_after INTEGER NOT NULL CHECK(typeof(committed_after)='integer'),
    stake INTEGER NOT NULL CHECK(typeof(stake)='integer' AND stake BETWEEN 0 AND 1000),
    returned INTEGER CHECK(returned IS NULL OR typeof(returned)='integer'),
    outcome TEXT CHECK(outcome IS NULL OR outcome IN ('YES','NO')),
    resolved_outcome TEXT CHECK(resolved_outcome IS NULL OR resolved_outcome IN ('YES','NO','INVALID')),
    forecast_revision INTEGER,
    policy_version TEXT NOT NULL REFERENCES point_policies(version),
    created_at INTEGER NOT NULL
);
CREATE INDEX point_ledger_user ON point_ledger(user_id,created_at DESC,id);
CREATE TRIGGER point_ledger_immutable BEFORE UPDATE ON point_ledger
BEGIN SELECT RAISE(ABORT,'immutable_points_ledger'); END;
CREATE TRIGGER point_ledger_no_delete BEFORE DELETE ON point_ledger
BEGIN SELECT RAISE(ABORT,'immutable_points_ledger'); END;
CREATE TRIGGER point_awards_immutable BEFORE UPDATE ON point_awards
BEGIN SELECT RAISE(ABORT,'immutable_points_award'); END;
CREATE TRIGGER point_awards_no_delete BEFORE DELETE ON point_awards
BEGIN SELECT RAISE(ABORT,'immutable_points_award'); END;

-- Temporary guards exist only inside the caller's atomic D1 batch.
CREATE TABLE point_write_guards (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('operation','position','balance')),
    passed INTEGER NOT NULL CHECK(typeof(passed)='integer' AND passed IN (0,1))
);
CREATE TRIGGER point_write_guard_check BEFORE INSERT ON point_write_guards
BEGIN
    SELECT RAISE(ABORT,'points_operation_conflict') WHERE NEW.passed!=1 AND NEW.kind='operation';
    SELECT RAISE(ABORT,'points_position_conflict') WHERE NEW.passed!=1 AND NEW.kind='position';
    SELECT RAISE(ABORT,'points_insufficient_balance') WHERE NEW.passed!=1 AND NEW.kind='balance';
END;

CREATE TRIGGER point_award_validate BEFORE INSERT ON point_awards
BEGIN
    SELECT RAISE(ABORT,'points_award_policy_mismatch') WHERE NOT EXISTS(SELECT 1 FROM point_policies p WHERE p.version=NEW.policy_version
      AND ((NEW.kind='profile' AND NEW.amount=p.profile_grant)
        OR (NEW.kind='wallet' AND NEW.amount=p.wallet_grant)));
    SELECT RAISE(ABORT,'points_wallet_not_verified') WHERE NEW.kind='wallet'
      AND NOT EXISTS(SELECT 1 FROM wallet_links w WHERE w.user_id=NEW.user_id AND w.address=NEW.wallet_address)
      AND NOT EXISTS(SELECT 1 FROM point_awards a WHERE a.id=NEW.id AND a.user_id=NEW.user_id
        AND a.kind='wallet' AND a.wallet_address=NEW.wallet_address AND a.amount=NEW.amount
        AND a.policy_version=NEW.policy_version);
END;

-- Validate each append against current account/position state before any credit.
CREATE TRIGGER point_ledger_validate BEFORE INSERT ON point_ledger
BEGIN
    SELECT RAISE(ABORT,'points_account_missing') WHERE NOT EXISTS(SELECT 1 FROM point_accounts a WHERE a.user_id=NEW.user_id);
    SELECT RAISE(ABORT,'points_insufficient_balance') WHERE EXISTS(SELECT 1 FROM point_accounts a WHERE a.user_id=NEW.user_id
      AND (a.available+NEW.available_delta<0 OR a.committed+NEW.committed_delta<0));
    SELECT RAISE(ABORT,'points_balance_overflow') WHERE EXISTS(SELECT 1 FROM point_accounts a WHERE a.user_id=NEW.user_id
      AND (a.available+NEW.available_delta>9007199254740991
       OR a.committed+NEW.committed_delta>9007199254740991
       OR a.available+a.committed+NEW.available_delta+NEW.committed_delta>9007199254740991));
    SELECT RAISE(ABORT,'points_account_snapshot_mismatch') WHERE NOT EXISTS(SELECT 1 FROM point_accounts a WHERE a.user_id=NEW.user_id
      AND NEW.available_after=a.available+NEW.available_delta
      AND NEW.committed_after=a.committed+NEW.committed_delta);
    SELECT RAISE(ABORT,'points_award_policy_mismatch') WHERE NEW.kind IN ('profile_grant','wallet_grant') AND NOT EXISTS(
      SELECT 1 FROM point_awards a WHERE a.id=NEW.award_id AND a.user_id=NEW.user_id
       AND a.policy_version=NEW.policy_version AND NEW.available_delta=a.amount
       AND NEW.committed_delta=0 AND NEW.stake=0 AND NEW.forecast_id IS NULL
       AND ((a.kind='profile' AND NEW.kind='profile_grant') OR (a.kind='wallet' AND NEW.kind='wallet_grant')));
    SELECT RAISE(ABORT,'points_position_conflict') WHERE NEW.kind='reservation' AND NOT EXISTS(
      SELECT 1 FROM forecasts f JOIN point_policies policy ON policy.version=NEW.policy_version
      LEFT JOIN point_positions p ON p.user_id=NEW.user_id AND p.forecast_id=f.id
      WHERE f.id=NEW.forecast_id AND f.state='OPEN' AND f.open_at<=NEW.created_at AND NEW.created_at<f.close_at
       AND f.revision=NEW.forecast_revision AND NEW.outcome IN ('YES','NO')
       AND NEW.stake<=policy.max_stake AND NEW.returned IS NULL AND NEW.award_id IS NULL
       AND EXISTS(SELECT 1 FROM user_forecasts v WHERE v.user_id=NEW.user_id AND v.forecast_id=f.id
         AND v.revision=NEW.forecast_revision AND v.outcome=NEW.outcome)
       AND (p.user_id IS NULL OR (p.status IN ('practice','committed')
         AND p.forecast_revision<NEW.forecast_revision AND p.policy_version=NEW.policy_version))
       AND NEW.available_delta=COALESCE(p.amount,0)-NEW.stake
       AND NEW.committed_delta=NEW.stake-COALESCE(p.amount,0));
    SELECT RAISE(ABORT,'points_settlement_not_final') WHERE NEW.kind='settlement' AND NOT EXISTS(
      SELECT 1 FROM point_positions p JOIN forecasts f ON f.id=p.forecast_id
      JOIN point_policies policy ON policy.version=p.policy_version
      WHERE p.user_id=NEW.user_id AND p.forecast_id=NEW.forecast_id AND p.status='committed'
       AND f.state IN ('FINALIZED','ARCHIVED') AND f.finalized_outcome=NEW.resolved_outcome
       AND p.amount=NEW.stake AND p.outcome=NEW.outcome AND p.policy_version=NEW.policy_version
       AND NEW.available_delta=NEW.returned AND NEW.committed_delta=-p.amount
       AND NEW.returned= ( CASE WHEN f.finalized_outcome='INVALID' THEN p.amount*policy.invalid_return_multiplier
         WHEN f.finalized_outcome=p.outcome THEN p.amount*policy.win_return_multiplier ELSE 0 END )
       AND EXISTS(SELECT 1 FROM events e WHERE e.forecast_id=f.id
         AND json_extract(e.event,'$.command_name')='finalize' AND e.created_at<=NEW.created_at));
END;

-- Only newly inserted immutable ledger rows can apply balance/position effects.
CREATE TRIGGER point_ledger_apply AFTER INSERT ON point_ledger
BEGIN
    UPDATE point_accounts SET available=available+NEW.available_delta,
      committed=committed+NEW.committed_delta,updated_at=MAX(updated_at,NEW.created_at) WHERE user_id=NEW.user_id;
    INSERT INTO point_positions(user_id,forecast_id,amount,outcome,forecast_revision,policy_version,status,
      returned,settlement_id,created_at,updated_at)
      SELECT NEW.user_id,NEW.forecast_id,NEW.stake,NEW.outcome,NEW.forecast_revision,NEW.policy_version,
        ( CASE WHEN NEW.stake=0 THEN 'practice' ELSE 'committed' END ) ,NULL,NULL,NEW.created_at,NEW.created_at
      WHERE NEW.kind='reservation'
      ON CONFLICT(user_id,forecast_id) DO UPDATE SET amount=excluded.amount,outcome=excluded.outcome,
        forecast_revision=excluded.forecast_revision,updated_at=excluded.updated_at,status=excluded.status;
    UPDATE point_positions SET status='settled',returned=NEW.returned,settlement_id=NEW.id,updated_at=NEW.created_at
      WHERE NEW.kind='settlement' AND user_id=NEW.user_id AND forecast_id=NEW.forecast_id AND status='committed';
END;

CREATE TRIGGER point_award_credit AFTER INSERT ON point_awards
BEGIN
    INSERT INTO point_ledger(id,user_id,kind,award_id,available_delta,committed_delta,available_after,
      committed_after,stake,policy_version,created_at)
      SELECT 'award:'||NEW.id,NEW.user_id, ( CASE WHEN NEW.kind='profile' THEN 'profile_grant' ELSE 'wallet_grant' END ) ,
        NEW.id,NEW.amount,0,a.available+NEW.amount,a.committed,0,NEW.policy_version,NEW.created_at
      FROM point_accounts a WHERE a.user_id=NEW.user_id;
END;

CREATE TRIGGER points_profile_created AFTER INSERT ON users
BEGIN
    INSERT INTO point_accounts(user_id,available,committed,updated_at) VALUES(NEW.id,0,0,NEW.created_at);
    INSERT INTO point_awards(id,user_id,kind,amount,policy_version,created_at)
      SELECT 'profile:'||NEW.id,NEW.id,'profile',profile_grant,version,NEW.created_at
      FROM point_policies WHERE version='participation-points-v1';
END;

CREATE TRIGGER points_wallet_linked AFTER INSERT ON wallet_links
BEGIN
    INSERT OR IGNORE INTO point_awards(id,user_id,kind,wallet_address,amount,policy_version,created_at)
      SELECT 'wallet:'||NEW.user_id,NEW.user_id,'wallet',NEW.address,wallet_grant,version,NEW.linked_at
      FROM point_policies WHERE version='participation-points-v1'
       AND NOT EXISTS(SELECT 1 FROM point_awards WHERE user_id=NEW.user_id AND kind='wallet')
       AND NOT EXISTS(SELECT 1 FROM point_awards WHERE wallet_address=NEW.address);
END;
CREATE TRIGGER points_wallet_reverified AFTER UPDATE ON wallet_links
BEGIN
    INSERT OR IGNORE INTO point_awards(id,user_id,kind,wallet_address,amount,policy_version,created_at)
      SELECT 'wallet:'||NEW.user_id,NEW.user_id,'wallet',NEW.address,wallet_grant,version,NEW.linked_at
      FROM point_policies WHERE version='participation-points-v1'
       AND NOT EXISTS(SELECT 1 FROM point_awards WHERE user_id=NEW.user_id AND kind='wallet')
       AND NOT EXISTS(SELECT 1 FROM point_awards WHERE wallet_address=NEW.address);
END;

-- Existing users receive the milestones once; old forecasts remain practice.
INSERT OR IGNORE INTO point_accounts(user_id,available,committed,updated_at)
 SELECT id,0,0,CAST(strftime('%s','now') AS INTEGER)*1000 FROM users;
INSERT OR IGNORE INTO point_awards(id,user_id,kind,amount,policy_version,created_at)
 SELECT 'profile:'||u.id,u.id,'profile',p.profile_grant,p.version,CAST(strftime('%s','now') AS INTEGER)*1000
 FROM users u JOIN point_policies p ON p.version='participation-points-v1'
 WHERE NOT EXISTS(SELECT 1 FROM point_awards a WHERE a.user_id=u.id AND a.kind='profile');
INSERT OR IGNORE INTO point_awards(id,user_id,kind,wallet_address,amount,policy_version,created_at)
 SELECT 'wallet:'||w.user_id,w.user_id,'wallet',w.address,p.wallet_grant,p.version,CAST(strftime('%s','now') AS INTEGER)*1000
 FROM wallet_links w JOIN point_policies p ON p.version='participation-points-v1'
 WHERE NOT EXISTS(SELECT 1 FROM point_awards a WHERE a.user_id=w.user_id AND a.kind='wallet')
 AND NOT EXISTS(SELECT 1 FROM point_awards a WHERE a.wallet_address=w.address);
