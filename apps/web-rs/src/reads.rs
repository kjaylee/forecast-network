//! Remaining GET read models: profile cards, integrity, translations, `/api/me`, creators,
//! activity, points, market positions, wallet and billing estimate.

use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use unicode_script::{Script, UnicodeScript};
use worker::*;

use forecast_domain::{canonical_bytes, content_hash, COMMITMENT_PREFIX};

use crate::api_response;
use crate::db::{all, first, get, int, text, Row};
use crate::projections::{card, quality_card_sql};
use crate::routes::{Context, RouteError};

pub const PROFILE_CARD_PREFIX: &str = "forecast-network:sha256:profile-card-json:v1\n";
pub const SOURCE_PREFIX: &str = "forecast-network:sha256:display-source:v1\n";
pub const TRANSLATION_PREFIX: &str = "forecast-network:sha256:display-translation:v1\n";
pub const TRANSLATION_POLICY: &str = "display-translation-v1";
pub const LANGUAGES: [&str; 4] = ["en", "ko", "ja", "zh-Hant"];
pub const MAX_SOURCE_BYTES: usize = 24_576;
pub const SKR_DECIMALS: u32 = 6;

type Handler = std::result::Result<Response, RouteError>;

fn canonical(value: &Value) -> String {
    String::from_utf8(canonical_bytes(value).unwrap_or_default()).unwrap_or_default()
}

fn digest(prefix: &str, value: &Value) -> String {
    hex::encode(Sha256::digest(format!("{prefix}{}", canonical(value)).as_bytes()))
}

pub fn public_user(row: &Row) -> Value {
    json!({"id": get(row, "id"), "displayName": get(row, "display_name"), "handle": get(row, "handle"), "createdAt": get(row, "created_at")})
}

fn submission(body: &Value, revision: &Value) -> Value {
    let confidence = body["confidence"].as_i64().unwrap_or(0);
    let yes = body["outcome"] == json!("YES");
    json!({"outcome": body["outcome"], "confidence": confidence, "probability": if yes { confidence } else { 100 - confidence },
           "submittedAt": body["submitted_at_ms"], "revision": revision})
}

async fn user_row(session: &D1DatabaseSession, user_id: &str) -> std::result::Result<Row, RouteError> {
    first(session, "SELECT * FROM users WHERE id=?", &[json!(user_id)])
        .await?
        .ok_or(RouteError::Unauthorized(
            "authentication_required",
            "Please sign in to continue.",
        ))
}

// ---------------------------------------------------------------- profile cards

pub async fn profile_card(context: &Context<'_>, snapshot_hash: &str) -> Handler {
    let not_found = || RouteError::NotFound("profile_card_not_found", "Shared profile record not found.");
    if snapshot_hash.len() != 64 || !snapshot_hash.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f')) {
        return Err(not_found());
    }
    let row = first(
        context.session,
        "SELECT body,media_type FROM artifacts WHERE hash=? AND kind='profile-share-snapshot'",
        &[json!(snapshot_hash)],
    )
    .await?
    .ok_or_else(not_found)?;
    let canonical_text = text(&row, "body").unwrap_or("");
    let actual = hex::encode(Sha256::digest(
        format!("{PROFILE_CARD_PREFIX}{canonical_text}").as_bytes(),
    ));
    let integrity = || {
        RouteError::Failed(
            503,
            "profile_card_integrity_failed",
            "This shared profile record failed integrity verification.",
        )
    };
    if actual != snapshot_hash || text(&row, "media_type") != Some("application/json") {
        return Err(integrity());
    }
    let payload: Value = serde_json::from_str(canonical_text).map_err(|_| integrity())?;
    let expected = [
        "schemaVersion",
        "asOf",
        "user",
        "metrics",
        "sampleStatus",
        "history",
        "historyTruncated",
        "highlight",
        "methodology",
        "commitmentProfile",
    ];
    let object = payload.as_object().ok_or_else(integrity)?;
    let mut keys: Vec<&str> = object.keys().map(String::as_str).collect();
    keys.sort();
    let mut wanted = expected.to_vec();
    wanted.sort();
    if keys != wanted
        || payload["schemaVersion"] != json!(1)
        || payload["methodology"]["version"] != json!("profile-card-v1")
        || payload["commitmentProfile"]["prefix"] != json!(PROFILE_CARD_PREFIX)
        || canonical(&payload) != canonical_text
    {
        return Err(integrity());
    }
    let mut data = payload;
    data["canonicalJson"] = json!(canonical_text);
    data["snapshotHash"] = json!(snapshot_hash);
    Ok(api_response(data, 200, false)?)
}

// ---------------------------------------------------------------- integrity

pub async fn integrity(context: &Context<'_>, forecast_id: &str) -> Handler {
    let row = first(
        context.session,
        "SELECT snapshot FROM forecasts WHERE id=? AND json_extract(snapshot,'$.published_at_ms') IS NOT NULL",
        &[json!(forecast_id)],
    )
    .await?
    .ok_or(RouteError::NotFound(
        "forecast_not_found",
        "Published forecast not found.",
    ))?;
    let forecast: Value =
        serde_json::from_str(text(&row, "snapshot").unwrap_or("")).map_err(|e| RouteError::Worker(e.into()))?;
    let hash = |value: &Value| content_hash(value).map_err(|e| RouteError::Worker(e.to_string().into()));
    let resolution = if forecast["resolution"].is_null() {
        Value::Null
    } else {
        json!({"canonicalJson": canonical(&forecast["resolution"]), "resolutionHash": hash(&forecast["resolution"])?})
    };
    let env = context.env;
    let var = |name: &str| env.var(name).map(|v| v.to_string()).unwrap_or_default();
    let chain = if crate::admin::flag(env, "SOLANA_REGISTRY_ENABLED") && !var("SOLANA_PROGRAM_ID").is_empty() {
        let program: [u8; 32] = bs58::decode(var("SOLANA_PROGRAM_ID"))
            .into_vec()
            .ok()
            .and_then(|v| v.try_into().ok())
            .ok_or_else(|| RouteError::Worker("program id".into()))?;
        crate::registry::status(context.session, &program, forecast_id).await?
    } else {
        json!({"status": "unconnected", "network": null, "transaction": null})
    };
    Ok(api_response(
        json!({
            "forecastId": forecast["forecast_id"],
            "specification": {"canonicalJson": canonical(&forecast["specification"]), "specificationHash": forecast["specification_hash"]},
            "resolution": resolution, "revision": forecast["revision"], "auditHead": forecast["audit_head_hash"],
            "commitmentProfile": {"algorithm": "SHA-256", "encoding": "UTF-8",
                                  "prefix": String::from_utf8_lossy(COMMITMENT_PREFIX), "canonicalization": "forecast-network-canonical-json-v1"},
            "chain": chain,
        }),
        200,
        false,
    )?)
}

// ---------------------------------------------------------------- display translations

fn has_non_latin_letter(text: &str) -> bool {
    text.chars().any(|c| c.is_alphabetic() && c.script() != Script::Latin)
}

/// `_require_english_public_text`: quoted names may keep their script; prose must be English.
fn english_public_text(texts: &[String]) -> bool {
    for text in texts {
        if !has_non_latin_letter(text) {
            continue;
        }
        let prose = strip_quoted(text);
        let english = prose
            .as_bytes()
            .windows(2)
            .any(|w| w[0].is_ascii_alphabetic() && w[1].is_ascii_alphabetic());
        if has_non_latin_letter(&prose) || !english {
            return false;
        }
    }
    true
}

/// Remove `"…"`, `“…”`, `‘…’` and word-bounded `'…'` spans of 1–160 characters (no newlines).
fn strip_quoted(text: &str) -> String {
    let chars: Vec<char> = text.chars().collect();
    let mut out = String::new();
    let mut i = 0;
    while i < chars.len() {
        let close = match chars[i] {
            '"' => Some('"'),
            '“' => Some('”'),
            '‘' => Some('’'),
            '\'' if i == 0 || !is_word(chars[i - 1]) => Some('\''),
            _ => None,
        };
        if let Some(close) = close {
            let mut end = None;
            for j in i + 1..chars.len().min(i + 162) {
                if chars[j] == '\n' {
                    break;
                }
                if chars[j] == close && j > i + 1 {
                    if close == '\'' && j + 1 < chars.len() && is_word(chars[j + 1]) {
                        continue;
                    }
                    end = Some(j);
                    break;
                }
            }
            if let Some(end) = end {
                out.push(' ');
                i = end + 1;
                continue;
            }
        }
        out.push(chars[i]);
        i += 1;
    }
    out
}

fn is_word(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

fn translation_error(status: u16, code: &'static str, message: &'static str) -> RouteError {
    RouteError::Failed(status, code, message)
}

async fn translation_source(session: &D1DatabaseSession, forecast_id: &str) -> std::result::Result<Value, RouteError> {
    let row = first(
        session,
        "SELECT f.snapshot,f.ai_forecast,t.body AS editorial_body,t.content_hash AS editorial_hash \
         FROM forecasts f LEFT JOIN forecast_translations t ON t.forecast_id=f.id AND t.language='en' \
         AND t.specification_hash=f.specification_hash WHERE f.id=? AND json_extract(f.snapshot,'$.published_at_ms') IS NOT NULL",
        &[json!(forecast_id)],
    )
    .await?
    .ok_or(RouteError::NotFound("forecast_not_found", "Published forecast not found."))?;
    let forecast: Value =
        serde_json::from_str(text(&row, "snapshot").unwrap_or("")).map_err(|e| RouteError::Worker(e.into()))?;
    let spec = &forecast["specification"];
    let ai: Option<Value> = text(&row, "ai_forecast")
        .filter(|s| !s.is_empty())
        .and_then(|s| serde_json::from_str(s).ok());
    let mut document = json!({
        "schemaVersion": 1, "forecastId": forecast_id, "specificationHash": forecast["specification_hash"], "language": "en",
        "title": spec["share_title"], "question": spec["canonical_question"],
        "rules": spec["rules"].as_array().map(|r| r.iter().map(|x| json!({"clauseId": x["clause_id"], "outcome": x["outcome"], "condition": x["condition"]})).collect::<Vec<_>>()).unwrap_or_default(),
        "invalidationRules": spec["invalidation_rules"],
        "aiRationale": ai.as_ref().map_or(Value::Null, |a| a["rationale"].clone()),
        "openAt": spec["open_at_ms"], "closeAt": spec["close_at_ms"],
    });
    if let Some(editorial) = text(&row, "editorial_body").filter(|s| !s.is_empty()) {
        let translated: Value = serde_json::from_str(editorial).map_err(|e| RouteError::Worker(e.into()))?;
        let unavailable =
            || translation_error(503, "translation_unavailable", "The source text could not be verified.");
        if translated["specificationHash"] != forecast["specification_hash"] || translated["language"] != json!("en") {
            return Err(unavailable());
        }
        let ids = |rules: &Value| {
            rules
                .as_array()
                .map(|r| r.iter().map(|x| x["clauseId"].clone()).collect::<Vec<_>>())
                .unwrap_or_default()
        };
        if ids(&translated["rules"]) != ids(&document["rules"]) {
            return Err(translation_error(
                503,
                "translation_unavailable",
                "The source rule identities could not be verified.",
            ));
        }
        document["title"] = translated["title"].clone();
        document["question"] = translated["question"].clone();
        document["invalidationRules"] = translated["invalidationRules"].clone();
        let merged: Vec<Value> = document["rules"]
            .as_array()
            .unwrap()
            .iter()
            .zip(translated["rules"].as_array().map(|v| v.as_slice()).unwrap_or(&[]))
            .map(|(original, replacement)| {
                let mut rule = original.clone();
                rule["condition"] = replacement["condition"].clone();
                rule
            })
            .collect();
        document["rules"] = json!(merged);
        if ai.is_some() && !translated["aiRationale"].is_null() {
            document["aiRationale"] = translated["aiRationale"].clone();
        }
    }
    let mut texts: Vec<String> = vec![
        document["title"].as_str().unwrap_or("").to_string(),
        document["question"].as_str().unwrap_or("").to_string(),
    ];
    texts.extend(
        document["rules"]
            .as_array()
            .map(|r| {
                r.iter()
                    .map(|x| x["condition"].as_str().unwrap_or("").to_string())
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default(),
    );
    texts.extend(
        document["invalidationRules"]
            .as_array()
            .map(|r| {
                r.iter()
                    .map(|x| x.as_str().unwrap_or("").to_string())
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default(),
    );
    if let Some(rationale) = document["aiRationale"].as_str() {
        texts.push(rationale.to_string());
    }
    if !english_public_text(&texts) {
        return Err(translation_error(
            503,
            "translation_unavailable",
            "An English source version is not available for this forecast.",
        ));
    }
    if canonical(&document).len() > MAX_SOURCE_BYTES {
        return Err(translation_error(
            413,
            "translation_unavailable",
            "This forecast is too large for automatic translation.",
        ));
    }
    Ok(document)
}

fn envelope(payload: Value) -> Value {
    let mut result = payload.clone();
    result["canonicalJson"] = json!(canonical(&payload));
    result["translationHash"] = json!(digest(TRANSLATION_PREFIX, &payload));
    result["commitmentProfile"] = json!({"algorithm": "SHA-256", "prefix": TRANSLATION_PREFIX});
    result
}

pub async fn translation(context: &Context<'_>, forecast_id: &str, language: &str) -> Handler {
    if !LANGUAGES.contains(&language) {
        return Err(translation_error(
            400,
            "translation_language_invalid",
            "Choose a supported translation language.",
        ));
    }
    let source = translation_source(context.session, forecast_id).await?;
    let source_hash = digest(SOURCE_PREFIX, &source);
    let translation = if language == "en" {
        let mut payload = json!({});
        for key in [
            "forecastId",
            "specificationHash",
            "title",
            "question",
            "rules",
            "invalidationRules",
            "aiRationale",
        ] {
            payload[key] = source[key].clone();
        }
        payload["sourceHash"] = json!(source_hash);
        payload["sourceLanguage"] = json!("en");
        payload["language"] = json!("en");
        payload["attribution"] = json!("Source text");
        payload["translatedAt"] = json!(0);
        Some(envelope(payload))
    } else {
        let row = first(
            context.session,
            "SELECT body,translation_hash FROM forecast_display_translations \
             WHERE forecast_id=? AND specification_hash=? AND source_hash=? AND language=? AND policy_version=?",
            &[
                json!(forecast_id),
                source["specificationHash"].clone(),
                json!(source_hash),
                json!(language),
                json!(TRANSLATION_POLICY),
            ],
        )
        .await?;
        match row {
            None => None,
            Some(row) => {
                let payload: Value =
                    serde_json::from_str(text(&row, "body").unwrap_or("")).map_err(|e| RouteError::Worker(e.into()))?;
                let expected = [
                    ("forecastId", json!(forecast_id)),
                    ("specificationHash", source["specificationHash"].clone()),
                    ("sourceHash", json!(source_hash)),
                    ("language", json!(language)),
                    ("sourceLanguage", json!("en")),
                    ("attribution", json!("AI translation")),
                ];
                if Some(digest(TRANSLATION_PREFIX, &payload).as_str()) != text(&row, "translation_hash")
                    || expected.iter().any(|(key, value)| &payload[*key] != value)
                {
                    return Err(translation_error(
                        503,
                        "translation_unavailable",
                        "The retained translation could not be verified.",
                    ));
                }
                Some(envelope(payload))
            }
        }
    };
    Ok(api_response(
        json!({"status": if translation.is_some() { "ready" } else { "missing" }, "source": source, "sourceHash": source_hash, "translation": translation}),
        200,
        false,
    )?)
}

// ---------------------------------------------------------------- activity, me, creators

pub async fn activity_items(session: &D1DatabaseSession, user_id: &str) -> Result<Vec<Value>> {
    let rows = all(
        session,
        "SELECT a.*,json_extract(t.body,'$.title') AS translated_title FROM activity a LEFT JOIN forecasts f ON f.id=a.forecast_id \
         LEFT JOIN forecast_translations t ON t.forecast_id=f.id AND t.specification_hash=f.specification_hash \
         AND t.language='en' WHERE a.user_id=? ORDER BY a.created_at DESC,a.id DESC LIMIT 100",
        &[json!(user_id)],
    )
    .await?;
    let translated = |row: &Row, fallback: &str| -> Value {
        match get(row, "translated_title") {
            Value::String(title) if !title.is_empty() => json!(title),
            _ => get(row, fallback).clone(),
        }
    };
    Ok(rows
        .iter()
        .map(|row| {
            let kind = text(row, "kind").unwrap_or("");
            json!({
                "id": get(row, "id"), "forecastId": get(row, "forecast_id"), "kind": kind,
                "title": if kind == "forecast_finalized" { translated(row, "title") } else { get(row, "title").clone() },
                "body": if kind == "creator_published" { translated(row, "body") } else { get(row, "body").clone() },
                "createdAt": get(row, "created_at"), "readAt": get(row, "read_at"),
            })
        })
        .collect())
}

pub async fn activity(context: &Context<'_>, user_id: &str) -> Handler {
    user_row(context.session, user_id).await?;
    Ok(api_response(
        json!({"items": activity_items(context.session, user_id).await?}),
        200,
        false,
    )?)
}

fn skr_display(atomic: i64) -> String {
    let scale = 10i64.pow(SKR_DECIMALS);
    let (whole, fraction) = (atomic.div_euclid(scale), atomic.rem_euclid(scale));
    let text = format!("{whole}.{fraction:0width$}", width = SKR_DECIMALS as usize);
    let trimmed = text.trim_end_matches('0').trim_end_matches('.').to_string();
    if trimmed.is_empty() {
        "0".to_string()
    } else {
        trimmed
    }
}

pub async fn seeker_status(session: &D1DatabaseSession, user_id: &str) -> Result<Value> {
    let row = first(
        session,
        "SELECT * FROM seeker_verifications WHERE user_id=? AND invalidated_at IS NULL AND address=\
         COALESCE((SELECT address FROM wallet_identities WHERE user_id=? AND status='active' AND converted_at IS NOT NULL),\
         (SELECT address FROM wallet_links WHERE user_id=?))",
        &[json!(user_id), json!(user_id), json!(user_id)],
    )
    .await?;
    Ok(match row {
        None => Value::Null,
        Some(row) => {
            json!({"verified": true, "memberNumber": get(&row, "member_number"), "skr": skr_display(int(&row, "skr_atomic").unwrap_or(0)),
                            "verifiedAt": get(&row, "verified_at"), "refreshedAt": get(&row, "refreshed_at")})
        }
    })
}

pub async fn me(context: &Context<'_>, user_id: Option<&str>) -> Handler {
    let Some(user_id) = user_id else {
        return Ok(api_response(
            json!({"user": null, "reputation": null, "myForecasts": [], "activity": [], "points": null, "seeker": null}),
            200,
            false,
        )?);
    };
    let session = context.session;
    let user = user_row(session, user_id).await?;
    let identity = first(
        session,
        "SELECT address FROM wallet_identities WHERE user_id=? AND status='active' AND converted_at IS NOT NULL",
        &[json!(user_id)],
    )
    .await?;
    let selected = all(
        session,
        "SELECT f.id FROM forecasts f WHERE f.id IN (SELECT forecast_id FROM user_forecasts WHERE user_id=?) ORDER BY f.updated_at DESC LIMIT 100",
        &[json!(user_id)],
    )
    .await?;
    let ids: Vec<String> = selected
        .iter()
        .filter_map(|r| text(r, "id").map(str::to_string))
        .collect();
    let rows = all(
        session,
        &format!(
            "{} ORDER BY f.updated_at DESC",
            quality_card_sql(context.now_ms, &ids).ok_or(RouteError::Input)?
        ),
        &[],
    )
    .await?;
    let accepted = all(
        session,
        "SELECT forecast_id,body,revision FROM eligible_user_forecasts WHERE user_id=? ORDER BY submitted_at DESC LIMIT 1000",
        &[json!(user_id)],
    )
    .await?;
    let positions = crate::points::positions(
        &crate::db::D1(session),
        user_id,
        &rows
            .iter()
            .filter_map(|r| text(r, "id").map(str::to_string))
            .collect::<Vec<_>>(),
    )
    .await?;
    let mut cards = Vec::new();
    for row in &rows {
        let id = text(row, "id").unwrap_or("");
        let mut item = card(row);
        if let Some(choice) = accepted.iter().find(|a| text(a, "forecast_id") == Some(id)) {
            let body: Value =
                serde_json::from_str(text(choice, "body").unwrap_or("{}")).map_err(|e| RouteError::Worker(e.into()))?;
            item["myForecast"] = submission(&body, get(choice, "revision"));
        }
        item["stake"] = positions.get(id).cloned().unwrap_or(Value::Null);
        item["eligibility"] = crate::eligibility::combined(&crate::db::D1(session), id, Some(user_id)).await?;
        cards.push(item);
    }
    let points = crate::points::summary(&crate::db::D1(session), user_id).await?;
    let mainnet = context
        .env
        .var("SOLANA_MAINNET_RPC")
        .map(|v| !v.to_string().is_empty())
        .unwrap_or(false);
    Ok(api_response(
        json!({
            "user": public_user(&user), "reputation": crate::reputation::reputation(session, user_id, context.now_ms).await?,
            "authentication": {"method": if identity.is_some() { "wallet" } else { "legacy" }, "address": identity.as_ref().map_or(Value::Null, |i| get(i, "address").clone())},
            "myForecasts": cards, "activity": activity_items(session, user_id).await?, "points": points,
            "seeker": {"available": mainnet, "status": seeker_status(session, user_id).await?},
        }),
        200,
        false,
    )?)
}

pub async fn creator(context: &Context<'_>, creator_id: &str, user_id: Option<&str>) -> Handler {
    let session = context.session;
    let user = first(session, "SELECT * FROM users WHERE id=?", &[json!(creator_id)])
        .await?
        .ok_or(RouteError::NotFound("creator_not_found", "Creator not found."))?;
    let selected = all(
        session,
        "SELECT id FROM forecasts WHERE creator_id=? ORDER BY created_at DESC LIMIT 100",
        &[json!(creator_id)],
    )
    .await?;
    let ids: Vec<String> = selected
        .iter()
        .filter_map(|r| text(r, "id").map(str::to_string))
        .collect();
    let rows = all(
        session,
        &format!(
            "{} ORDER BY f.created_at DESC",
            quality_card_sql(context.now_ms, &ids).ok_or(RouteError::Input)?
        ),
        &[],
    )
    .await?;
    let stats = first(
        session,
        "SELECT COUNT(*) AS created,COUNT(finalized_outcome) AS resolved,SUM(CASE WHEN finalized_outcome='INVALID' THEN 1 ELSE 0 END) AS invalid,\
         SUM(CASE WHEN EXISTS(SELECT 1 FROM events e WHERE e.forecast_id=forecasts.id AND json_extract(e.event,'$.command_name')='submit_dispute') THEN 1 ELSE 0 END) AS disputed \
         FROM forecasts WHERE creator_id=?",
        &[json!(creator_id)],
    )
    .await?;
    let followers = first(
        session,
        "SELECT COUNT(*) AS n FROM follows WHERE creator_id=?",
        &[json!(creator_id)],
    )
    .await?;
    let following = first(
        session,
        "SELECT 1 AS yes FROM follows WHERE creator_id=? AND follower_id=?",
        &[json!(creator_id), json!(user_id.unwrap_or(""))],
    )
    .await?;
    let stat = |name: &str| stats.as_ref().and_then(|s| int(s, name)).unwrap_or(0);
    let seeker = seeker_status(session, creator_id).await?;
    let mut creator = public_user(&user);
    creator["marketsCreated"] = json!(stat("created"));
    creator["resolvedMarkets"] = json!(stat("resolved"));
    creator["invalidMarkets"] = json!(stat("invalid"));
    creator["disputedMarkets"] = json!(stat("disputed"));
    creator["followerCount"] = json!(followers.as_ref().and_then(|f| int(f, "n")).unwrap_or(0));
    creator["reputation"] = crate::reputation::reputation(session, creator_id, context.now_ms).await?;
    creator["seeker"] = if seeker.is_null() {
        Value::Null
    } else {
        json!({"memberNumber": seeker["memberNumber"]})
    };
    Ok(api_response(
        json!({"creator": creator, "forecasts": rows.iter().map(card).collect::<Vec<_>>(), "isFollowing": following.is_some()}),
        200,
        false,
    )?)
}

// ---------------------------------------------------------------- points, markets, wallet, billing

pub async fn points(context: &Context<'_>, user_id: &str) -> Handler {
    let summary = crate::points::summary(&crate::db::D1(context.session), user_id).await?;
    Ok(api_response(summary, 200, false)?)
}

pub async fn my_markets(context: &Context<'_>, user_id: Option<&str>, selected: Option<&str>) -> Handler {
    let Some(user_id) = user_id else {
        return Ok(api_response(json!({"positions": []}), 200, false)?);
    };
    let session = context.session;
    let mut positions = Vec::new();
    match selected {
        Some(forecast_id) => positions.push(crate::markets::positions(session, user_id, forecast_id).await?),
        None => {
            for row in all(
                session,
                "SELECT forecast_id FROM point_markets ORDER BY created_at DESC LIMIT 100",
                &[],
            )
            .await?
            {
                positions
                    .push(crate::markets::positions(session, user_id, text(&row, "forecast_id").unwrap_or("")).await?);
            }
        }
    }
    Ok(api_response(json!({"positions": positions}), 200, false)?)
}

pub async fn wallet(context: &Context<'_>, user_id: &str) -> Handler {
    user_row(context.session, user_id).await?;
    let row = first(
        context.session,
        "SELECT address,chain,linked_at FROM wallet_links WHERE user_id=?",
        &[json!(user_id)],
    )
    .await?;
    let points = crate::points::summary(&crate::db::D1(context.session), user_id).await?;
    Ok(api_response(
        json!({"wallet": row.as_ref().map_or(Value::Null, |r| json!({"address": get(r, "address"), "chain": get(r, "chain"), "linkedAt": get(r, "linked_at")})), "points": points}),
        200,
        false,
    )?)
}

pub fn billing_estimate() -> Handler {
    let (price, cap) = (200, 115);
    Ok(api_response(
        json!({"mode": "sandbox", "billable": false, "currency": "USD", "priceCents": price, "costCapCents": cap,
               "requiredOperatorCapitalCents": cap, "requiredAllocatedAssetsCents": price + cap,
               "maximumEventualContributionCents": price, "contributionAtCostCapCents": price - cap, "spendableAtPublicationCents": 0,
               "notice": "Simulation only. No payment is requested or accepted."}),
        200,
        false,
    )?)
}
