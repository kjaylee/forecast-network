-- Seeker ownership, verified against mainnet by the service. A Seeker Genesis Token is
-- the Token-2022 group member every Seeker handset mints once; holding one proves the
-- signed-in wallet belongs to a Seeker owner. The mint is unique per row so one device
-- token cannot badge several accounts. SKR balance is informational only.
CREATE TABLE seeker_verifications (
    user_id TEXT PRIMARY KEY REFERENCES users(id),
    address TEXT NOT NULL,
    genesis_mint TEXT NOT NULL UNIQUE,
    member_number INTEGER,
    skr_atomic TEXT NOT NULL DEFAULT '0',
    slot INTEGER NOT NULL,
    verified_at INTEGER NOT NULL,
    refreshed_at INTEGER NOT NULL
);
