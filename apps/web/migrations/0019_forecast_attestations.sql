-- Wallet-signed Devnet attestations of a forecaster's own receipt. The relayer pays the
-- fee; the forecaster's wallet co-signs a memo carrying the receipt commitment, so the
-- chain record proves "this wallet stood behind this exact forecast at this time".
-- Nothing of value moves. Confirmation is device-reported until an RPC-capable
-- verifier records it; both states are kept apart.
CREATE TABLE forecast_attestations (
    id TEXT PRIMARY KEY,
    forecast_id TEXT NOT NULL REFERENCES forecasts(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    address TEXT NOT NULL,
    receipt_hash TEXT NOT NULL CHECK(length(receipt_hash)=64),
    memo TEXT NOT NULL,
    blockhash TEXT NOT NULL,
    message_hash TEXT NOT NULL CHECK(length(message_hash)=64),
    status TEXT NOT NULL CHECK(status IN ('prepared','submitted','verified','expired')),
    signature TEXT UNIQUE,
    reported_slot INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX forecast_attestations_owner ON forecast_attestations(user_id,forecast_id,created_at);
CREATE INDEX forecast_attestations_forecast ON forecast_attestations(forecast_id,status);
