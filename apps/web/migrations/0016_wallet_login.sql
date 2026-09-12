-- Wallet authentication retains the original link proofs and point ledger.
CREATE TABLE wallet_login_contexts (
    token_hash TEXT PRIMARY KEY,
    epoch INTEGER NOT NULL DEFAULT 1 CHECK(epoch>=1),
    latest_challenge_id TEXT,
    active_session_hash TEXT,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    revoked_at INTEGER,
    CHECK(expires_at>created_at)
);
CREATE INDEX wallet_login_context_expiry ON wallet_login_contexts(expires_at);
ALTER TABLE sessions ADD COLUMN context_hash TEXT REFERENCES wallet_login_contexts(token_hash);
ALTER TABLE sessions ADD COLUMN context_epoch INTEGER;
CREATE INDEX sessions_context ON sessions(context_hash);

CREATE TABLE wallet_identities (
    address TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    status TEXT NOT NULL CHECK(status IN ('active','tombstone')),
    created_at INTEGER NOT NULL,
    converted_at INTEGER
);
CREATE UNIQUE INDEX wallet_identity_active_user ON wallet_identities(user_id) WHERE status='active';
CREATE INDEX wallet_identity_user_conversion ON wallet_identities(user_id,converted_at);
CREATE INDEX wallet_audit_address_history ON wallet_audit(address,created_at);
INSERT INTO wallet_identities(address,user_id,status,created_at)
    SELECT address,user_id,'active',linked_at FROM wallet_links;
-- An explicitly unlinked wallet is never silently reactivated as a login key.
INSERT OR IGNORE INTO wallet_identities(address,user_id,status,created_at)
    SELECT address,user_id,'tombstone',created_at FROM wallet_audit ORDER BY created_at,id;
INSERT OR IGNORE INTO wallet_identities(address,user_id,status,created_at)
    SELECT wallet_address,user_id,'tombstone',created_at FROM point_awards WHERE wallet_address IS NOT NULL;
CREATE TRIGGER wallet_identity_owner_immutable BEFORE UPDATE ON wallet_identities
BEGIN
    SELECT RAISE(ABORT,'immutable_wallet_identity') WHERE NEW.address!=OLD.address OR NEW.user_id!=OLD.user_id
      OR NEW.status!=OLD.status OR NEW.created_at!=OLD.created_at
      OR (OLD.converted_at IS NOT NULL AND NEW.converted_at IS NOT OLD.converted_at);
END;
CREATE TRIGGER wallet_identity_no_delete BEFORE DELETE ON wallet_identities
BEGIN SELECT RAISE(ABORT,'immutable_wallet_identity'); END;
CREATE TRIGGER wallet_login_link_no_delete BEFORE DELETE ON wallet_links
BEGIN
    SELECT RAISE(ABORT,'wallet_login_rotation_required') WHERE EXISTS(
      SELECT 1 FROM wallet_identities i WHERE i.address=OLD.address AND i.user_id=OLD.user_id AND i.status='active');
END;
CREATE TRIGGER wallet_login_link_no_replace BEFORE UPDATE ON wallet_links
BEGIN
    SELECT RAISE(ABORT,'wallet_login_rotation_required') WHERE (NEW.address!=OLD.address OR NEW.user_id!=OLD.user_id)
      AND EXISTS(SELECT 1 FROM wallet_identities i WHERE i.address=OLD.address AND i.user_id=OLD.user_id AND i.status='active');
END;
CREATE TRIGGER wallet_login_link_owner BEFORE INSERT ON wallet_links
BEGIN
    SELECT RAISE(ABORT,'wallet_identity_conflict') WHERE EXISTS(SELECT 1 FROM wallet_identities i
      WHERE i.address=NEW.address AND (i.user_id!=NEW.user_id OR i.status!='active'));
END;
CREATE TRIGGER wallet_login_link_owner_update BEFORE UPDATE ON wallet_links
BEGIN
    SELECT RAISE(ABORT,'wallet_identity_conflict') WHERE EXISTS(SELECT 1 FROM wallet_identities i
      WHERE i.address=NEW.address AND (i.user_id!=NEW.user_id OR i.status!='active'));
END;

CREATE TABLE wallet_login_challenges (
    id TEXT PRIMARY KEY,
    context_hash TEXT NOT NULL REFERENCES wallet_login_contexts(token_hash),
    context_epoch INTEGER NOT NULL,
    address TEXT NOT NULL,
    target_user_id TEXT NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('login','migrate')),
    source_session_hash TEXT,
    display_name TEXT NOT NULL,
    origin TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK(purpose='sign_in_and_link_forecast_profile'),
    chain TEXT NOT NULL CHECK(chain='solana:devnet'),
    message TEXT NOT NULL CHECK(length(message)<=2048),
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    used_at INTEGER,
    revoked_at INTEGER,
    CHECK(expires_at>created_at),
    CHECK((mode='migrate' AND source_session_hash IS NOT NULL) OR (mode='login' AND source_session_hash IS NULL))
);
CREATE INDEX wallet_login_challenge_expiry ON wallet_login_challenges(expires_at);
CREATE INDEX wallet_login_challenge_context ON wallet_login_challenges(context_hash,used_at,revoked_at);
CREATE TRIGGER wallet_login_challenge_proof_immutable BEFORE UPDATE ON wallet_login_challenges
BEGIN
    SELECT RAISE(ABORT,'immutable_wallet_login_proof') WHERE NEW.id!=OLD.id OR NEW.context_hash!=OLD.context_hash
      OR NEW.context_epoch!=OLD.context_epoch OR NEW.address!=OLD.address OR NEW.target_user_id!=OLD.target_user_id
      OR NEW.mode!=OLD.mode OR NEW.source_session_hash IS NOT OLD.source_session_hash
      OR NEW.display_name!=OLD.display_name OR NEW.origin!=OLD.origin OR NEW.purpose!=OLD.purpose
      OR NEW.chain!=OLD.chain OR NEW.message!=OLD.message OR NEW.created_at!=OLD.created_at OR NEW.expires_at!=OLD.expires_at
      OR (OLD.used_at IS NOT NULL AND NEW.used_at IS NOT OLD.used_at)
      OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS NOT OLD.revoked_at);
END;
CREATE TABLE wallet_login_audit (
    id TEXT PRIMARY KEY,
    challenge_id TEXT NOT NULL UNIQUE REFERENCES wallet_login_challenges(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    address TEXT NOT NULL,
    body TEXT NOT NULL CHECK(json_valid(body) AND length(CAST(body AS BLOB))<=8192),
    created_at INTEGER NOT NULL
);
CREATE TRIGGER wallet_login_audit_immutable BEFORE UPDATE ON wallet_login_audit
BEGIN SELECT RAISE(ABORT,'immutable_wallet_login_audit'); END;
CREATE TRIGGER wallet_login_audit_no_delete BEFORE DELETE ON wallet_login_audit
BEGIN SELECT RAISE(ABORT,'immutable_wallet_login_audit'); END;
