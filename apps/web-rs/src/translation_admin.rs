//! `Application.set_translation`: the operator's editorial translation of a published forecast.
//!
//! This is the one write where a person's prose reaches a live question, so what a port has to
//! reproduce is less the happy path than the fifteen ways it is refused — and the two properties
//! that make the write auditable at all:
//!
//!   * **Identical content is a replay.** The audit row is written only when no translation with
//!     that content already exists, and the current row is updated under a `WHERE` a repeat cannot
//!     satisfy, so repeating a translation leaves both its timestamp and the audit trail exactly as
//!     they were. A *correction* appends one audit row and replaces only the display.
//!   * **The guard re-reads the published revision.** The batch refuses itself unless the forecast
//!     still carries the specification the caller was shown, so a forecast republished between the
//!     read and the write cannot be translated against the version it used to be.
//!
//! One thing here is *not* like the risk routes: these refusals are `AppError`s raised by the
//! service itself, so a body with the wrong keys answers `400 invalid_input` with a message that
//! says what was wrong, rather than the 503 a domain `require` would fall through to.

use serde_json::{json, Map, Value};
use worker::*;

use crate::api_response;
use crate::db::Database;
use crate::routes::{Context, RouteError};
use crate::writes::checked_text;
use forecast_domain::Record;

/// The documented field set. A translation missing one of these, or carrying a stranger, is not a
/// translation this Worker agreed to store.
const FIELDS: [&str; 9] = [
    "specificationHash",
    "title",
    "question",
    "rules",
    "invalidationRules",
    "aiRationale",
    "sourceLanguage",
    "language",
    "attribution",
];

/// The display limit, measured on the serialized bytes.
const MAX_BYTES: usize = 65_536;

/// The field bounds, in the reference's own order and sizes.
const TITLE_CHARS: usize = 120;
const QUESTION_CHARS: usize = 3_000;
const CONDITION_CHARS: usize = 16_000;
const RULE_CHARS: usize = 8_000;

const SOURCE_LANGUAGE: &str = "ko";
const LANGUAGE: &str = "en";
const ATTRIBUTION: &str = "Forecast editorial translation";

const SNAPSHOT_SQL: &str = "SELECT snapshot,ai_forecast FROM forecasts WHERE id=? \
     AND json_extract(snapshot,'$.published_at_ms') IS NOT NULL";

const AUDIT_SQL: &str = "INSERT INTO forecast_translation_audit(id,forecast_id,language,specification_hash,\
     translation_hash,body,actor,attribution,created_at) \
     SELECT ?,?,'en',?,?,?,'authenticated_admin','Forecast editorial translation',? \
     WHERE NOT EXISTS(SELECT 1 FROM forecast_translations WHERE forecast_id=? AND language='en' \
     AND specification_hash=? AND content_hash=?)";

const TRANSLATION_SQL: &str = "INSERT INTO forecast_translations(forecast_id,language,specification_hash,\
     source_language,body,content_hash,attribution,translated_at) \
     VALUES(?,'en',?,'ko',?,?,'Forecast editorial translation',?) \
     ON CONFLICT(forecast_id,language,specification_hash) DO UPDATE SET body=excluded.body,\
     content_hash=excluded.content_hash,translated_at=excluded.translated_at \
     WHERE forecast_translations.content_hash!=excluded.content_hash";

const READ_BACK_SQL: &str = "SELECT body AS display_translation,translated_at,\
     content_hash AS translation_hash,specification_hash \
     FROM forecast_translations WHERE forecast_id=? AND language='en' AND specification_hash=?";

/// `POST /api/admin/forecasts/{id}/translations/en`.
pub async fn set_translation_route(
    context: &Context<'_>,
    forecast_id: &str,
    body: &Map<String, Value>,
) -> Result<Response, RouteError> {
    let db = crate::db::D1(context.session);
    let result = set_translation(
        &db,
        context.now_ms,
        &|| crate::mutate::random_token(),
        forecast_id,
        body,
    )
    .await?;
    Ok(api_response(result, 200, false)?)
}

/// `Application.set_translation`, over any store and any token source.
pub async fn set_translation(
    db: &dyn Database,
    now_ms: i64,
    token: &dyn Fn() -> String,
    forecast_id: &str,
    body: &Map<String, Value>,
) -> Result<Value, RouteError> {
    // The *set* is the check, so a body with one documented field missing and a stranger in its
    // place is refused — a length test alone would accept it.
    let keys: Vec<&str> = body.keys().map(String::as_str).collect();
    if keys.len() != FIELDS.len() || !FIELDS.iter().all(|field| keys.contains(field)) {
        return Err(invalid(
            "A complete display translation with the documented fields is required.",
        ));
    }
    if body["language"] != json!(LANGUAGE)
        || body["sourceLanguage"] != json!(SOURCE_LANGUAGE)
        || body["attribution"] != json!(ATTRIBUTION)
    {
        return Err(invalid(
            "Use the English editorial translation language and attribution.",
        ));
    }
    let row = db
        .first(SNAPSHOT_SQL, &[json!(forecast_id)])
        .await?
        .ok_or(crate::translations::FORECAST_NOT_FOUND)?;
    // A row whose snapshot will not decode is a server fault rather than a caller's mistake: the
    // reference's own decoder raises a `ValidationError`, which the entry answers as such.
    let forecast = forecast_domain::lifecycle::Forecast::from_json(crate::db::text(&row, "snapshot").unwrap_or(""))
        .map_err(|_| crate::admin::refused())?;
    let specification_hash = forecast.specification_hash.clone();
    if body["specificationHash"] != json!(specification_hash) {
        return Err(RouteError::Failed(
            409,
            "translation_specification_mismatch",
            "This translation does not identify the current published specification.",
        ));
    }
    let rules = body["rules"].as_array();
    let clause_ids = forecast.specification.clause_ids();
    let rules_well_formed = rules.is_some_and(|rules| {
        rules.len() == clause_ids.len()
            && rules.iter().all(|rule| {
                rule.as_object().is_some_and(|rule| {
                    rule.len() == 2 && rule.contains_key("clauseId") && rule.contains_key("condition")
                })
            })
    });
    if !rules_well_formed {
        return Err(invalid(
            "Translate every published rule without adding fields or changing their order.",
        ));
    }
    let rules = rules.cloned().unwrap_or_default();
    if rules
        .iter()
        .map(|rule| rule["clauseId"].as_str().unwrap_or(""))
        .ne(clause_ids.iter().copied())
    {
        return Err(invalid(
            "Rule identifiers and their order must match the published specification.",
        ));
    }
    let invalidations = body["invalidationRules"].as_array();
    if !invalidations.is_some_and(|values| values.len() == forecast.specification.invalidation_rules.len()) {
        return Err(invalid("Translate every invalidation rule in its original order."));
    }
    let invalidations = invalidations.cloned().unwrap_or_default();
    // `json.loads(row["ai_forecast"]) if row["ai_forecast"] else None`, then `not ai.get("rationale")`
    // — the rationale translation is only meaningful when there is a rationale to translate.
    let ai = crate::db::text(&row, "ai_forecast")
        .filter(|text| !text.is_empty())
        .and_then(|text| serde_json::from_str::<Value>(text).ok());
    let submitted = body["aiRationale"].clone();
    if submitted != Value::Null && !ai.is_some_and(|ai| crate::wallet_login::truthy(&ai["rationale"])) {
        return Err(invalid(
            "An AI rationale translation requires an existing AI rationale.",
        ));
    }
    // `text(rationale, 8000) if rationale is not None else None`: a null is an absent rationale, and
    // anything else that is not a string is the caller's mistake rather than an absence.
    let rationale = match &submitted {
        Value::Null => None,
        Value::String(text) => Some(checked_text(&json!(text), RULE_CHARS)?),
        _ => return Err(crate::routes::RouteError::Input),
    };
    let mut conditions = Vec::with_capacity(rules.len());
    for rule in &rules {
        conditions.push(checked_text(&rule["condition"], CONDITION_CHARS)?);
    }
    let mut cleaned_invalidations = Vec::with_capacity(invalidations.len());
    for value in &invalidations {
        cleaned_invalidations.push(checked_text(value, RULE_CHARS)?);
    }
    let cleaned = json!({
        "specificationHash": specification_hash,
        "title": checked_text(&body["title"], TITLE_CHARS)?,
        "question": checked_text(&body["question"], QUESTION_CHARS)?,
        "rules": rules
            .iter()
            .zip(&conditions)
            .map(|(rule, condition)| json!({
                "clauseId": rule["clauseId"].as_str().unwrap_or(""),
                "condition": condition,
            }))
            .collect::<Vec<Value>>(),
        "invalidationRules": cleaned_invalidations,
        "aiRationale": match &rationale {
            Some(rationale) => json!(rationale),
            None => Value::Null,
        },
        "sourceLanguage": SOURCE_LANGUAGE,
        "language": LANGUAGE,
        "attribution": ATTRIBUTION,
    });
    let serialized = crate::translations::canonical(&cleaned);
    if serialized.len() > MAX_BYTES {
        return Err(invalid("The translation exceeds the 64 KiB display limit."));
    }
    // Every field a person reads has to be English *and* readable: Hangul left in the display is an
    // untranslated field, and text with no Latin letter at all is a number or a symbol where prose
    // belongs. Both are refused rather than stored.
    let cleaned_rules = cleaned["rules"].as_array().cloned().unwrap_or_default();
    let mut translated: Vec<String> = vec![
        cleaned["title"].as_str().unwrap_or("").to_string(),
        cleaned["question"].as_str().unwrap_or("").to_string(),
    ];
    for rule in &cleaned_rules {
        translated.push(rule["condition"].as_str().unwrap_or("").to_string());
    }
    for value in cleaned["invalidationRules"].as_array().cloned().unwrap_or_default() {
        translated.push(value.as_str().unwrap_or("").to_string());
    }
    if let Some(rationale) = rationale {
        translated.push(rationale);
    }
    if translated
        .iter()
        .any(|value| value.chars().any(is_hangul) || !value.chars().any(|c| c.is_ascii_alphabetic()))
    {
        return Err(invalid("Provide English display text for every translated field."));
    }
    let digest = forecast_domain::content_hash(&cleaned).map_err(|_| crate::admin::refused())?;
    let now = now_ms;
    let guard = token();
    let audit_id = token();
    db.batch(&[
        (
            "INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN EXISTS(SELECT 1 FROM forecasts \
             WHERE id=? AND specification_hash=? AND json_extract(snapshot,'$.published_at_ms') IS NOT NULL) \
             THEN 1 ELSE 0 END"
                .to_string(),
            vec![json!(guard), json!(forecast_id), json!(specification_hash)],
        ),
        (
            AUDIT_SQL.to_string(),
            vec![
                json!(audit_id),
                json!(forecast_id),
                json!(specification_hash),
                json!(digest),
                json!(serialized),
                json!(now),
                json!(forecast_id),
                json!(specification_hash),
                json!(digest),
            ],
        ),
        (
            TRANSLATION_SQL.to_string(),
            vec![
                json!(forecast_id),
                json!(specification_hash),
                json!(serialized),
                json!(digest),
                json!(now),
            ],
        ),
        (
            "DELETE FROM mutation_guards WHERE token=?".to_string(),
            vec![json!(guard)],
        ),
    ])
    .await?;
    let translation = db
        .first(READ_BACK_SQL, &[json!(forecast_id), json!(specification_hash)])
        .await?;
    Ok(json!({
        "forecast": crate::writes::card_row(db, forecast_id, now).await?,
        "displayTranslation": translation
            .as_ref()
            .and_then(crate::projections::display_translation),
    }))
}

/// `[가-힣]`: the Hangul syllables, which are exactly the range the reference's own pattern names.
fn is_hangul(character: char) -> bool {
    ('\u{AC00}'..='\u{D7A3}').contains(&character)
}

/// `invalid(message)`: the service's own refusal, which keeps its own message.
fn invalid(message: &'static str) -> RouteError {
    crate::writes::invalid_message(message)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::Sqlite;
    use crate::golden::{assert_all_cases_known, assert_case, block, load, static_database, Tokens};

    /// The refusal as the vector records it. Every branch here is one the reference raises, so a
    /// case that recorded `forecast_not_found` and a replay that produced something else is caught
    /// by its code rather than by its status.
    fn refusal(error: &RouteError) -> Value {
        match error {
            RouteError::Failed(status, code, message) => json!({"status": status, "code": code, "message": message}),
            RouteError::NotFound(code, message) => json!({"status": 404, "code": code, "message": message}),
            RouteError::Unauthorized(code, message) => json!({"status": 401, "code": code, "message": message}),
            RouteError::Input => {
                json!({"status": 400, "code": "invalid_input", "message": "Please check your input."})
            }
            RouteError::Invalid => {
                json!({"status": 400, "code": "invalid_request", "message": "Check the input format and length."})
            }
            RouteError::Worker(error) => {
                json!({"status": 503, "code": "service_unavailable", "message": error.to_string()})
            }
        }
    }

    /// The reference's own fixture, case for case.
    ///
    /// Each case restores its own initial state, so the sequence the generator ran is not something
    /// the replay has to reproduce: the *replay* case appends no audit row because its own fixture
    /// already carries one, which is what makes that property checkable in isolation.
    #[test]
    fn the_reference_editorial_translation_is_reproduced_case_for_case() {
        let document = load("translation-admin-golden.json");
        let mut replayed = Vec::new();
        for case in document["cases"].as_array().expect("cases") {
            let name = case["call"].as_str().expect("a name");
            let db: &'static Sqlite = static_database(&case["initial"]);
            let tokens = Tokens::new(Tokens::recorded(case));
            let body = case["body"].as_object().cloned().unwrap_or_default();
            let produced = block(set_translation(
                db,
                case["now"].as_i64().unwrap_or(0),
                &|| tokens.next(),
                case["forecastId"].as_str().unwrap_or(""),
                &body,
            ));
            let (result, error) = match produced {
                Ok(value) => (Some(value), None),
                Err(error) => (None, Some(refusal(&error))),
            };
            assert_case(name, case, &result, &error, db);
            tokens.assert_drained(name, " (the guard and the audit id)");
            replayed.push(name);
        }
        assert_all_cases_known(&document, &replayed, &[]);
    }
}
