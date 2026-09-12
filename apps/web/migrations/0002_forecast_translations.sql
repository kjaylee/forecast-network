-- Presentation translations never update immutable published specifications.
CREATE TABLE forecast_translations (
    forecast_id TEXT NOT NULL REFERENCES forecasts(id),
    language TEXT NOT NULL CHECK(language='en'),
    specification_hash TEXT NOT NULL CHECK(length(specification_hash)=64),
    source_language TEXT NOT NULL CHECK(source_language='ko'),
    body TEXT NOT NULL CHECK(json_valid(body) AND length(CAST(body AS BLOB))<=65536),
    content_hash TEXT NOT NULL CHECK(length(content_hash)=64),
    attribution TEXT NOT NULL CHECK(attribution='Forecast editorial translation'),
    translated_at INTEGER NOT NULL,
    PRIMARY KEY(forecast_id,language,specification_hash),
    CHECK(json_extract(body,'$.specificationHash')=specification_hash),
    CHECK(json_extract(body,'$.language')=language)
);
CREATE TABLE forecast_translation_audit (
    id TEXT PRIMARY KEY,
    forecast_id TEXT NOT NULL REFERENCES forecasts(id),
    language TEXT NOT NULL CHECK(language='en'),
    specification_hash TEXT NOT NULL,
    translation_hash TEXT NOT NULL,
    body TEXT NOT NULL CHECK(json_valid(body)),
    actor TEXT NOT NULL CHECK(actor='authenticated_admin'),
    attribution TEXT NOT NULL CHECK(attribution='Forecast editorial translation'),
    created_at INTEGER NOT NULL
);
CREATE TRIGGER forecast_translation_audit_immutable BEFORE UPDATE ON forecast_translation_audit
BEGIN SELECT RAISE(ABORT, 'immutable_translation_audit'); END;

UPDATE users SET display_name='Forecast Editorial' WHERE id='system_editorial';
UPDATE activity SET title='New forecast from a creator you follow' WHERE kind='creator_published';
UPDATE activity SET body='The forecast was finalized as ' ||
    (SELECT finalized_outcome FROM forecasts WHERE forecasts.id=activity.forecast_id) || '.'
    WHERE kind='forecast_finalized';
UPDATE forecasts SET job_error='Resolution is on hold pending another evidence review.'
    WHERE job_error IS NOT NULL;
