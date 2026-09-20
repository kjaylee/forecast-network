//! Audited operator containment, without changing immutable forecast rules or state.
//!
//! A hold stops participation while newly available evidence is reviewed. It is deliberately
//! not a state change: the forecast stays where it is, the rules stay what they were, and the
//! audit records who did it. Everything a caller can get wrong is checked before anything is
//! written — the exact field set, the revision it thinks it is acting on, the hold it thinks
//! is in force, and the specification it thinks it is holding.

use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

use crate::db::{text, Database};
use crate::source_watch::{compact, compact_ascii};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct HoldError {
    pub status: u16,
    pub code: &'static str,
    pub message: &'static str,
}

impl From<worker::Error> for HoldError {
    fn from(_: worker::Error) -> Self {
        HoldError::storage()
    }
}

impl HoldError {
    pub fn invalid() -> Self {
        HoldError {
            status: 400,
            code: "invalid_input",
            message: "Please check your input.",
        }
    }

    pub fn not_found() -> Self {
        HoldError {
            status: 404,
            code: "forecast_not_found",
            message: "Forecast not found.",
        }
    }

    pub fn on_hold() -> Self {
        HoldError {
            status: 409,
            code: "participation_on_hold",
            message: "Participation is on hold while newly available evidence is reviewed.",
        }
    }

    pub fn idempotency_conflict() -> Self {
        HoldError {
            status: 409,
            code: "idempotency_conflict",
            message: "This request identifier was already used.",
        }
    }

    pub fn changed() -> Self {
        HoldError {
            status: 409,
            code: "participation_hold_changed",
            message: "The participation review changed. Refresh before taking action.",
        }
    }

    pub fn storage() -> Self {
        HoldError {
            status: 503,
            code: "forecast_storage_unavailable",
            message: "The review could not be confirmed. Retry with the same request identifier.",
        }
    }
}

const FIELDS: [&str; 7] = [
    "action",
    "expectedRevision",
    "expectedHoldId",
    "specificationHash",
    "reason",
    "evidenceUrl",
    "idempotencyKey",
];
const MAX_SAFE_INTEGER: i64 = 9_007_199_254_740_991;

/// The hold in force, if there is one.
pub async fn active(db: &dyn Database, forecast_id: &str) -> Result<Option<Value>, HoldError> {
    let row = db
        .first(
            "SELECT body FROM active_participation_holds WHERE forecast_id=?",
            &[serde_json::json!(forecast_id)],
        )
        .await?;
    Ok(row.and_then(|row| serde_json::from_str(text(&row, "body").unwrap_or("")).ok()))
}

/// What the audit says, and whether there is more of it than is being shown.
pub async fn status(db: &dyn Database, forecast_id: &str) -> Result<Value, HoldError> {
    if db
        .first("SELECT id FROM forecasts WHERE id=?", &[serde_json::json!(forecast_id)])
        .await?
        .is_none()
    {
        return Err(HoldError::not_found());
    }
    let rows = db
        .all(
            "SELECT body FROM participation_hold_events WHERE forecast_id=? ORDER BY revision DESC LIMIT 100",
            &[serde_json::json!(forecast_id)],
        )
        .await?;
    let audit: Vec<Value> = rows
        .iter()
        .filter_map(|row| serde_json::from_str(text(row, "body").unwrap_or("")).ok())
        .collect();
    let latest = audit.first().cloned();
    Ok(serde_json::json!({
        "revision": latest.as_ref().and_then(|value| value["revision"].as_i64()).unwrap_or(0),
        "hold": latest.as_ref().filter(|value| value["action"] == "hold"),
        "audit": audit,
        "auditTruncated": rows.len() == 100,
    }))
}

/// Take or release a hold. Only the authenticated administrative route may call this.
pub async fn change(
    db: &dyn Database,
    token: &str,
    now_ms: i64,
    forecast_id: &str,
    body: &Map<String, Value>,
    dismissal_review_id: Option<&str>,
) -> Result<Value, HoldError> {
    let keys: Vec<&str> = body.keys().map(String::as_str).collect();
    let action = body.get("action").and_then(Value::as_str);
    if keys.len() != FIELDS.len()
        || !FIELDS.iter().all(|field| keys.contains(field))
        || !matches!(action, Some("hold") | Some("release"))
    {
        return Err(HoldError::invalid());
    }
    let revision = body.get("expectedRevision").and_then(Value::as_i64);
    let key = body.get("idempotencyKey").and_then(Value::as_str);
    let url = body.get("evidenceUrl").and_then(Value::as_str);
    if !revision.is_some_and(|value| (0..MAX_SAFE_INTEGER).contains(&value)) {
        return Err(HoldError::invalid());
    }
    if !key.is_some_and(valid_request_key) {
        return Err(HoldError::invalid());
    }
    if body.get("reason").and_then(Value::as_str) != Some("known_outcome_review")
        || !url.is_some_and(|value| (1..=2048).contains(&value.len()))
    {
        return Err(HoldError::invalid());
    }
    let evidence_url = url.unwrap_or("");
    if !valid_evidence_url(evidence_url) {
        return Err(HoldError::invalid());
    }
    let revision = revision.unwrap_or(0);
    let key = key.unwrap_or("");

    // The request hash is over the forecast and the body together, so the same identifier
    // used against a different forecast is a conflict rather than a replay.
    //
    // `compact_ascii`, not `compact`: the reference hashes this one with `json.dumps`'s default
    // `ensure_ascii`, and `evidenceUrl` is only required to have no character at or below a space
    // — so a URL with an accent in its path passes that check and then hashes differently. The
    // result stored below keeps the other encoding, because the reference stores it with
    // `ensure_ascii=False`. Two encodings in one function, each matching its own call site.
    let mut scoped = body.clone();
    scoped.insert("forecastId".to_string(), Value::String(forecast_id.to_string()));
    let request_hash = hex::encode(Sha256::digest(compact_ascii(&Value::Object(scoped)).as_bytes()));

    if let Some(previous) = prior(db, key, &request_hash).await? {
        return Ok(previous);
    }
    let forecast = db
        .first(
            "SELECT specification_hash,state FROM forecasts WHERE id=?",
            &[serde_json::json!(forecast_id)],
        )
        .await?;
    let Some(forecast) = forecast else {
        return Err(HoldError::not_found());
    };
    let specification_hash = body.get("specificationHash").and_then(Value::as_str).unwrap_or("");
    let current = status(db, forecast_id).await?;
    let hold_id = current["hold"]["holdId"].as_str().map(str::to_string);
    let expected_hold = body.get("expectedHoldId").and_then(Value::as_str).map(str::to_string);
    if current["revision"].as_i64() != Some(revision)
        || expected_hold != hold_id
        || text(&forecast, "specification_hash") != Some(specification_hash)
    {
        return Err(HoldError::changed());
    }
    let taking = action == Some("hold");
    if (taking && (hold_id.is_some() || text(&forecast, "state") != Some("OPEN"))) || (!taking && hold_id.is_none()) {
        return Err(HoldError::changed());
    }

    // A release that follows a dismissed review has to name that review, and the database
    // checks the claim rather than taking it: no other review may still be open, no watcher
    // may still be running, and the hold being released must be the one the watch took.
    let mut dismissal_checks = String::new();
    let mut dismissal_params: Vec<Value> = Vec::new();
    if let Some(review_id) = dismissal_review_id {
        // A dismissal justifies a release and nothing else, and it has to be named exactly.
        if taking
            || review_id.len() != 64
            || !review_id
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        {
            return Err(HoldError::invalid());
        }
        dismissal_checks = "AND EXISTS(SELECT 1 FROM official_source_reviews WHERE id=? AND forecast_id=? AND specification_hash=? \
             AND json_extract(result,'$.accepted')=0 AND json_extract(result,'$.dismissible')=1) \
             AND NOT EXISTS(SELECT 1 FROM official_source_reviews WHERE forecast_id=? AND specification_hash=? \
             AND id!=? AND json_extract(result,'$.dismissible') IS NOT 1) \
             AND NOT EXISTS(SELECT 1 FROM official_watch_sources s JOIN official_watch_bindings b \
             ON (b.source_id=s.id OR b.source_id=s.parent_id) WHERE b.forecast_id=? AND s.enabled=1 AND s.lease_until>?) \
             AND EXISTS(SELECT 1 FROM participation_hold_events WHERE id=? AND request_key LIKE 'source-watch:%') "
            .to_string();
        dismissal_params = vec![
            serde_json::json!(review_id),
            serde_json::json!(forecast_id),
            serde_json::json!(specification_hash),
            serde_json::json!(forecast_id),
            serde_json::json!(specification_hash),
            serde_json::json!(review_id),
            serde_json::json!(forecast_id),
            serde_json::json!(now_ms),
            serde_json::json!(hold_id.clone().unwrap_or_default()),
        ];
    }

    let event_id = format!("{token}-{}", revision + 1);
    let guard = format!("{token}-guard");
    let result = serde_json::json!({
        "id": event_id, "forecastId": forecast_id, "revision": revision + 1,
        "action": action, "holdId": if taking { Value::String(event_id.clone()) } else { serde_json::json!(hold_id) },
        "specificationHash": specification_hash, "reason": body.get("reason").cloned().unwrap_or(Value::Null),
        "evidenceUrl": evidence_url, "actor": "authenticated_admin", "createdAt": now_ms,
    });
    let mut guard_params: Vec<Value> = vec![
        serde_json::json!(guard),
        serde_json::json!(forecast_id),
        serde_json::json!(revision),
        serde_json::json!(forecast_id),
        serde_json::json!(specification_hash),
        serde_json::json!(action),
    ];
    guard_params.extend(dismissal_params);
    // The guard is the compare-and-set. `mutation_guards` is `CHECK(valid=1)`, so a claim that
    // does not hold inserts a zero and aborts the whole batch — the hold cannot be taken on a
    // revision, a hold or a state that has moved.
    let statements = vec![
        (
            format!(
                "INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN \
                 COALESCE((SELECT MAX(revision) FROM participation_hold_events WHERE forecast_id=?),0)=? \
                 AND EXISTS(SELECT 1 FROM forecasts WHERE id=? AND specification_hash=? AND (?='release' OR state='OPEN')) \
                 {dismissal_checks}THEN 1 ELSE 0 END"
            ),
            guard_params,
        ),
        (
            "INSERT INTO participation_hold_events(id,forecast_id,revision,action,hold_id,specification_hash,reason,evidence_url,\
             actor,request_key,request_hash,body,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
                .to_string(),
            vec![
                serde_json::json!(event_id),
                serde_json::json!(forecast_id),
                serde_json::json!(revision + 1),
                serde_json::json!(action),
                result["holdId"].clone(),
                serde_json::json!(specification_hash),
                body.get("reason").cloned().unwrap_or(Value::Null),
                serde_json::json!(evidence_url),
                serde_json::json!("authenticated_admin"),
                serde_json::json!(key),
                serde_json::json!(request_hash),
                serde_json::json!(compact(&result)),
                serde_json::json!(now_ms),
            ],
        ),
        ("DELETE FROM mutation_guards WHERE token=?".to_string(), vec![serde_json::json!(guard)]),
    ];
    if let Err(_error) = db.batch(&statements).await {
        // A rejected claim and a lost race look the same from here, so the state is re-read
        // before deciding which it was.
        if let Some(previous) = prior(db, key, &request_hash).await? {
            return Ok(previous);
        }
        let latest = status(db, forecast_id).await?;
        if latest["revision"].as_i64() != Some(revision) {
            return Err(HoldError::changed());
        }
        return Err(HoldError::storage());
    }
    Ok(result)
}

async fn prior(db: &dyn Database, key: &str, request_hash: &str) -> Result<Option<Value>, HoldError> {
    let row = db
        .first(
            "SELECT request_hash,body FROM participation_hold_events WHERE request_key=?",
            &[serde_json::json!(key)],
        )
        .await?;
    let Some(row) = row else {
        return Ok(None);
    };
    if text(&row, "request_hash") != Some(request_hash) {
        return Err(HoldError::idempotency_conflict());
    }
    Ok(serde_json::from_str(text(&row, "body").unwrap_or("")).ok())
}

fn valid_request_key(value: &str) -> bool {
    (8..=120).contains(&value.len())
        && value
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | ':' | '-'))
}

/// The evidence URL has to be a public HTTPS address: no credentials, and no character that a
/// header or a log line could be split on.
fn valid_evidence_url(url: &str) -> bool {
    if url.chars().any(|c| (c as u32) <= 32) {
        return false;
    }
    let Some(rest) = url.strip_prefix("https://") else {
        return false;
    };
    let authority = rest.split(['/', '?', '#']).next().unwrap_or("");
    !authority.is_empty() && !authority.contains('@') && !authority.contains(':')
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::{Database, Sqlite};

    const SPEC: &str = "1111111111111111111111111111111111111111111111111111111111111111";

    fn block<F: std::future::Future>(future: F) -> F::Output {
        futures_lite::future::block_on(future)
    }

    fn forecast(db: &Sqlite, id: &str, state: &str) {
        block(db.execute(
            "INSERT INTO users(id,display_name,handle,recovery_hash,created_at) VALUES('u','H','h','r',1)",
            &[],
        ))
        .unwrap();
        block(db.execute(
            &format!(
                "INSERT INTO forecasts(id,creator_id,draft_id,snapshot,revision,state,category,title,question,\
                 normalized_question,specification_hash,open_at,close_at,created_at,updated_at,mutation_key) \
                 VALUES('{id}','u','d-{id}','{{}}',1,'{state}','CRYPTO','t','q','q','{SPEC}',0,1,1,1,'k')"
            ),
            &[],
        ))
        .unwrap();
    }

    fn body(action: &str, revision: i64, hold: Option<&str>, key: &str) -> Map<String, Value> {
        let mut value = Map::new();
        value.insert("action".into(), serde_json::json!(action));
        value.insert("expectedRevision".into(), serde_json::json!(revision));
        value.insert("expectedHoldId".into(), serde_json::json!(hold));
        value.insert("specificationHash".into(), serde_json::json!(SPEC));
        value.insert("reason".into(), serde_json::json!("known_outcome_review"));
        value.insert(
            "evidenceUrl".into(),
            serde_json::json!("https://www.apple.com/newsroom/2026/09/x/"),
        );
        value.insert("idempotencyKey".into(), serde_json::json!(key));
        value
    }

    #[test]
    fn a_hold_is_audited_and_released_by_its_owner() {
        let db = Sqlite::from_migrations();
        forecast(&db, "f", "OPEN");
        let held = block(change(&db, "t", 100, "f", &body("hold", 0, None, "key-hold-1"), None)).unwrap();
        assert_eq!(held["revision"], 1);
        assert_eq!(held["action"], "hold");
        assert_eq!(held["actor"], "authenticated_admin");
        assert_eq!(block(active(&db, "f")).unwrap().unwrap()["action"], "hold");

        let status = block(status(&db, "f")).unwrap();
        assert_eq!(status["revision"], 1);
        assert_eq!(status["auditTruncated"], false);
        assert_eq!(status["audit"].as_array().unwrap().len(), 1);

        let hold_id = held["holdId"].as_str().unwrap().to_string();
        let released = block(change(
            &db,
            "t",
            200,
            "f",
            &body("release", 1, Some(&hold_id), "key-release-1"),
            None,
        ))
        .unwrap();
        assert_eq!(released["action"], "release");
        assert_eq!(released["holdId"], hold_id, "a release names the hold it lifts");
        assert!(block(active(&db, "f")).unwrap().is_none());
    }

    #[test]
    fn the_same_request_identifier_returns_the_original_and_a_different_one_conflicts() {
        let db = Sqlite::from_migrations();
        forecast(&db, "f", "OPEN");
        let first = block(change(&db, "t", 100, "f", &body("hold", 0, None, "key-hold-1"), None)).unwrap();
        let again = block(change(&db, "t", 999, "f", &body("hold", 0, None, "key-hold-1"), None)).unwrap();
        assert_eq!(again, first, "a replay returns what happened, not a second hold");

        // The same identifier with different content is a conflict. It has to be a change the
        // validation would have accepted, or the conflict is never reached.
        let mut conflicting = body("hold", 0, None, "key-hold-1");
        conflicting.insert(
            "evidenceUrl".into(),
            serde_json::json!("https://www.apple.com/newsroom/2026/09/other/"),
        );
        assert_eq!(
            block(change(&db, "t", 100, "f", &conflicting, None)).unwrap_err().code,
            "idempotency_conflict"
        );
        // A reason the reference does not know never reaches the idempotency check at all.
        let mut unknown_reason = body("hold", 0, None, "key-hold-1");
        unknown_reason.insert("reason".into(), serde_json::json!("something_else"));
        assert_eq!(
            block(change(&db, "t", 100, "f", &unknown_reason, None))
                .unwrap_err()
                .code,
            "invalid_input"
        );
    }

    #[test]
    fn a_stale_revision_or_the_wrong_hold_is_refused() {
        let db = Sqlite::from_migrations();
        forecast(&db, "f", "OPEN");
        block(change(&db, "t", 100, "f", &body("hold", 0, None, "key-hold-1"), None)).unwrap();
        // The revision has already moved.
        assert_eq!(
            block(change(&db, "t", 100, "f", &body("hold", 0, None, "key-hold-2"), None))
                .unwrap_err()
                .code,
            "participation_hold_changed"
        );
        // The revision is right but the caller names a hold that is not the one in force.
        assert_eq!(
            block(change(
                &db,
                "t",
                100,
                "f",
                &body("release", 1, Some("other"), "key-release-2"),
                None
            ))
            .unwrap_err()
            .code,
            "participation_hold_changed"
        );
    }

    #[test]
    fn a_hold_needs_an_open_forecast_and_the_exact_field_set() {
        let db = Sqlite::from_migrations();
        forecast(&db, "f", "RESOLVING");
        assert_eq!(
            block(change(&db, "t", 100, "f", &body("hold", 0, None, "key-hold-1"), None))
                .unwrap_err()
                .code,
            "participation_hold_changed",
            "only an open forecast can be held"
        );
        let mut extra = body("hold", 0, None, "key-hold-2");
        extra.insert("extra".into(), serde_json::json!(1));
        assert_eq!(
            block(change(&db, "t", 100, "f", &extra, None)).unwrap_err().code,
            "invalid_input"
        );
    }

    #[test]
    fn the_evidence_url_has_to_be_a_public_https_address() {
        let db = Sqlite::from_migrations();
        forecast(&db, "f", "OPEN");
        for (index, url) in [
            "http://www.apple.com/x",
            "https://user@www.apple.com/x",
            "https://www.apple.com:8443/x",
            "https://www.apple.com/a b",
            "https://",
        ]
        .iter()
        .enumerate()
        {
            let mut candidate = body("hold", 0, None, &format!("key-evidence-{index}"));
            candidate.insert("evidenceUrl".into(), serde_json::json!(url));
            assert_eq!(
                block(change(&db, "t", 100, "f", &candidate, None)).unwrap_err().code,
                "invalid_input",
                "{url}"
            );
        }
    }

    #[test]
    fn a_missing_forecast_is_not_found_rather_than_invalid() {
        let db = Sqlite::from_migrations();
        assert_eq!(block(status(&db, "absent")).unwrap_err().code, "forecast_not_found");
        assert_eq!(
            block(change(
                &db,
                "t",
                100,
                "absent",
                &body("hold", 0, None, "key-hold-1"),
                None
            ))
            .unwrap_err()
            .code,
            "forecast_not_found"
        );
    }
}
