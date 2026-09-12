-- Wallet links prove profile ownership through signMessage, never a transaction.
CREATE TABLE wallet_challenges (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    address TEXT NOT NULL,
    origin TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK(purpose='link_forecast_profile'),
    chain TEXT NOT NULL CHECK(chain='solana:devnet'),
    message TEXT NOT NULL CHECK(length(message)<=2048),
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    used_at INTEGER,
    revoked_at INTEGER,
    CHECK(expires_at>created_at)
);
CREATE INDEX wallet_challenges_user ON wallet_challenges(user_id,expires_at);
CREATE TABLE wallet_links (
    user_id TEXT PRIMARY KEY REFERENCES users(id),
    address TEXT NOT NULL UNIQUE,
    chain TEXT NOT NULL CHECK(chain='solana:devnet'),
    linked_at INTEGER NOT NULL,
    generation TEXT NOT NULL UNIQUE REFERENCES wallet_challenges(id),
    revision INTEGER NOT NULL CHECK(revision>=1)
);
CREATE TABLE wallet_audit (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    address TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('wallet_linked','wallet_unlinked')),
    challenge_id TEXT REFERENCES wallet_challenges(id),
    body TEXT NOT NULL CHECK(json_valid(body) AND length(CAST(body AS BLOB))<=8192),
    created_at INTEGER NOT NULL,
    UNIQUE(kind,challenge_id)
);
CREATE TRIGGER wallet_audit_immutable BEFORE UPDATE ON wallet_audit
BEGIN SELECT RAISE(ABORT, 'immutable_wallet_audit'); END;
CREATE TRIGGER wallet_audit_no_delete BEFORE DELETE ON wallet_audit
BEGIN SELECT RAISE(ABORT, 'immutable_wallet_audit'); END;
