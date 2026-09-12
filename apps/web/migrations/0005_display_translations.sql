-- Reviewed presentation copies are bound to the exact source and policy version.
-- This table never changes a forecast snapshot, specification, result or balance.
CREATE TABLE forecast_display_translations (
    forecast_id TEXT NOT NULL REFERENCES forecasts(id),
    specification_hash TEXT NOT NULL CHECK(length(specification_hash)=64),
    source_hash TEXT NOT NULL CHECK(length(source_hash)=64),
    language TEXT NOT NULL CHECK(language IN ('ko','ja','zh-Hant')),
    policy_version TEXT NOT NULL,
    body TEXT NOT NULL CHECK(json_valid(body) AND length(CAST(body AS BLOB))<=65536),
    translation_hash TEXT NOT NULL UNIQUE CHECK(length(translation_hash)=64),
    generation_hash TEXT NOT NULL REFERENCES artifacts(hash),
    review_hash TEXT NOT NULL REFERENCES artifacts(hash),
    created_at INTEGER NOT NULL,
    PRIMARY KEY(forecast_id,specification_hash,source_hash,language,policy_version),
    CHECK(json_type(body,'$.forecastId') IS 'text' AND json_extract(body,'$.forecastId')=forecast_id),
    CHECK(json_type(body,'$.specificationHash') IS 'text' AND json_extract(body,'$.specificationHash')=specification_hash),
    CHECK(json_type(body,'$.sourceHash') IS 'text' AND json_extract(body,'$.sourceHash')=source_hash),
    CHECK(json_type(body,'$.language') IS 'text' AND json_extract(body,'$.language')=language),
    CHECK(json_extract(body,'$.attribution') IS 'AI translation'),
    CHECK(json_extract(body,'$.sourceLanguage') IS 'en'),
    CHECK(json_type(body,'$.translatedAt') IS 'integer' AND json_extract(body,'$.translatedAt')=created_at)
);
CREATE TRIGGER forecast_display_translations_immutable BEFORE UPDATE ON forecast_display_translations
BEGIN SELECT RAISE(ABORT,'immutable_display_translation'); END;
CREATE TRIGGER forecast_display_translations_no_delete BEFORE DELETE ON forecast_display_translations
BEGIN SELECT RAISE(ABORT,'immutable_display_translation'); END;
