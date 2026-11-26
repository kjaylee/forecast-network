CREATE TABLE forecast_share_links (
    code TEXT PRIMARY KEY CHECK(length(code)=32 AND code NOT GLOB '*[^0-9a-f]*'),
    forecast_id TEXT NOT NULL REFERENCES forecasts(id) ON DELETE CASCADE,
    sharer_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL CHECK(created_at>=0),
    opened_at INTEGER CHECK(opened_at>=created_at),
    shared_at INTEGER CHECK(shared_at>=created_at)
);
CREATE INDEX forecast_share_links_created ON forecast_share_links(created_at,code);
CREATE INDEX forecast_share_links_sharer ON forecast_share_links(sharer_id);
CREATE INDEX forecast_share_links_forecast ON forecast_share_links(forecast_id);

CREATE TABLE forecast_share_claims (
    recipient_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    forecast_id TEXT NOT NULL REFERENCES forecasts(id) ON DELETE CASCADE,
    code TEXT NOT NULL REFERENCES forecast_share_links(code) ON DELETE CASCADE,
    claimed_at INTEGER NOT NULL CHECK(claimed_at>=0),
    PRIMARY KEY(recipient_id,forecast_id)
);
CREATE INDEX forecast_share_claims_code ON forecast_share_claims(code,claimed_at);
CREATE INDEX forecast_share_claims_forecast ON forecast_share_claims(forecast_id);
