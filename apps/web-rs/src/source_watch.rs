//! `SourceWatch`, the parts that decide: what may be watched, what a binding means, and when
//! participation stops while evidence is looked at.
//!
//! One rule in here is load-bearing and easy to lose in a transcription: the hold happens when
//! an observation is queued, **before** any AI is scheduled or awaited. Participation stops
//! while the evidence is looked at, not after a model has had an opinion about it.
//!
//! The polling loop is not here yet. It needs the conditional-fetch wrapper around the
//! collector's own transport — `If-None-Match` and a 304 that the collector has to see as a
//! response rather than as a failure — and that wiring is easier to get wrong than to write.

use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::db::{int, text, Database, Row};
use crate::sources::{relevant, validate_public_url};

pub const POLICY: &str = "official-source-watch-v1";
pub const LEASE_MS: i64 = 240_000;
pub const MAX_BINDINGS: i64 = 30;
pub const MAX_ATTEMPTS: i64 = 3;
pub const MIN_INTERVAL_MS: i64 = 60_000;
pub const MAX_ARTIFACT_BYTES: usize = 524_288;
const DAY_MS: i64 = 86_400_000;
pub const AI_SCOPE: &str = "official-watch-ai";

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WatchError {
    Invalid(&'static str),
    /// A source that was refused, carrying the reference's message for it.
    Rejected(String),
    Unavailable,
    NotModified,
    BudgetExhausted,
    Missing(&'static str),
    /// The database refused the statement, carrying its own text for the operator.
    Database(String),
}

impl From<worker::Error> for WatchError {
    fn from(error: worker::Error) -> Self {
        WatchError::Database(error.to_string())
    }
}

impl WatchError {
    pub fn message(&self) -> String {
        match self {
            WatchError::Invalid(message) => (*message).to_string(),
            WatchError::Rejected(message) => message.clone(),
            WatchError::Unavailable => "Evidence source is temporarily unavailable".to_string(),
            WatchError::NotModified => "Source is unchanged".to_string(),
            WatchError::BudgetExhausted => "Daily review budget is exhausted".to_string(),
            WatchError::Missing(what) => format!("{what} is missing"),
            WatchError::Database(detail) => detail.clone(),
        }
    }
}

/// The exception class name the reference records as `last_error`.
pub fn error_kind(error: &WatchError) -> &'static str {
    match error {
        WatchError::Invalid(_) | WatchError::Missing(_) => "ValueError",
        WatchError::Rejected(_) => "SourceRejected",
        WatchError::Unavailable => "SourceUnavailable",
        WatchError::NotModified => "NotModified",
        WatchError::BudgetExhausted => "BudgetExhausted",
        WatchError::Database(_) => "Error",
    }
}

pub fn hash_hex(value: &str) -> String {
    hex::encode(Sha256::digest(value.as_bytes()))
}

/// `json.dumps(..., sort_keys=True, separators=(",",":"), ensure_ascii=False)`.
pub fn compact(value: &Value) -> String {
    let sorted = sort_keys(value);
    serde_json::to_string(&sorted).unwrap_or_default()
}

fn sort_keys(value: &Value) -> Value {
    match value {
        Value::Object(fields) => {
            let mut keys: Vec<&String> = fields.keys().collect();
            keys.sort();
            let mut out = serde_json::Map::new();
            for key in keys {
                out.insert(key.clone(), sort_keys(&fields[key]));
            }
            Value::Object(out)
        }
        Value::Array(items) => Value::Array(items.iter().map(sort_keys).collect()),
        other => other.clone(),
    }
}

/// The statements the watch retains evidence with, refusing anything that is not
/// self-consistent before it becomes immutable.
pub fn artifact_sql(
    artifacts: &[(String, String, String, String)],
    now_ms: i64,
) -> Result<Vec<(String, Vec<Value>)>, WatchError> {
    let mut statements = Vec::new();
    for (content_hash, kind, body, media_type) in artifacts {
        let mut hashes = vec![hash_hex(body)];
        if media_type == "application/json" {
            if let Ok(parsed) = serde_json::from_str::<Value>(body) {
                hashes.push(hash_hex(&compact(&parsed)));
            }
        }
        if !hashes.contains(content_hash) || body.len() > MAX_ARTIFACT_BYTES {
            return Err(WatchError::Invalid("Invalid retained source artifact"));
        }
        statements.push((
            "INSERT OR IGNORE INTO artifacts(hash,kind,body,media_type,created_at) VALUES(?,?,?,?,?)".to_string(),
            vec![
                json!(content_hash),
                json!(kind),
                json!(body),
                json!(media_type),
                json!(now_ms),
            ],
        ));
    }
    Ok(statements)
}

/// What is being watched. Named rather than positional, because at the call site a bare
/// `false` says nothing about which of several booleans it is.
pub struct Registration<'a> {
    pub source_id: &'a str,
    pub url: &'a str,
    pub kind: &'a str,
    pub interval_ms: i64,
    pub parent_id: Option<&'a str>,
    pub pinned: bool,
}

/// Register an exact official source. A registration is immutable once made.
pub async fn register(db: &dyn Database, registration: Registration<'_>, now_ms: i64) -> Result<(), WatchError> {
    let Registration {
        source_id,
        url,
        kind,
        interval_ms,
        parent_id,
        pinned,
    } = registration;
    validate_public_url(url, true).map_err(|error| WatchError::Rejected(error.message()))?;
    if !valid_identifier(source_id)
        || !matches!(kind, "index" | "article")
        || !(MIN_INTERVAL_MS..=DAY_MS).contains(&interval_ms)
    {
        return Err(WatchError::Invalid("Invalid official source registration"));
    }
    if pinned {
        let count = db
            .first(
                "SELECT COUNT(*) AS n FROM official_watch_sources WHERE pinned=1 AND id<>?",
                &[json!(source_id)],
            )
            .await?
            .and_then(|row| int(&row, "n"))
            .unwrap_or(0);
        if count >= MAX_BINDINGS {
            return Err(WatchError::Invalid("Pinned official source limit reached"));
        }
    }
    if let Some(parent_id) = parent_id {
        let parent = db
            .first("SELECT * FROM official_watch_sources WHERE id=?", &[json!(parent_id)])
            .await?;
        let belongs = parent
            .as_ref()
            .map(|row| host_of(text(row, "url").unwrap_or("")) == host_of(url) && text(row, "kind") == Some("index"))
            .unwrap_or(false);
        if !belongs {
            return Err(WatchError::Invalid(
                "An article must belong to an exact watched publisher",
            ));
        }
    }
    if let Some(existing) = db
        .first(
            "SELECT * FROM official_watch_sources WHERE id=? OR url=?",
            &[json!(source_id), json!(url)],
        )
        .await?
    {
        if text(&existing, "id") != Some(source_id)
            || text(&existing, "url") != Some(url)
            || text(&existing, "kind") != Some(kind)
            || text(&existing, "parent_id") != parent_id
        {
            return Err(WatchError::Invalid("Source registration is immutable"));
        }
        if pinned {
            db.execute(
                "UPDATE official_watch_sources SET pinned=1,enabled=1 WHERE id=?",
                &[json!(source_id)],
            )
            .await?;
        }
        return Ok(());
    }
    db.execute(
        "INSERT OR IGNORE INTO official_watch_sources(id,url,kind,parent_id,interval_ms,next_poll,pinned) VALUES(?,?,?,?,?,?,?)",
        &[
            json!(source_id),
            json!(url),
            json!(kind),
            json!(parent_id),
            json!(interval_ms),
            json!(now_ms),
            json!(i64::from(pinned)),
        ],
    )
    .await?;
    Ok(())
}

/// Bind a forecast to a watched source, and queue what the source has already published.
///
/// The host has to be one of the specification's own published sources: a watcher cannot be
/// pointed at a URL that the question never named.
pub async fn bind(
    db: &dyn Database,
    host: &dyn crate::source_watch::ForecastSource,
    forecast_id: &str,
    source_id: &str,
    families: &[String],
    now_ms: i64,
) -> Result<(), WatchError> {
    if families.is_empty() || families.len() > 8 || families.iter().any(|value| !valid_family(value)) {
        return Err(WatchError::Invalid("A bounded product family is required"));
    }
    // The caller provides canonical specification source URLs, never model URLs.
    let forecast = host.load_forecast(forecast_id).await?;
    let hosts: Vec<String> = forecast["officialSourceUrls"]
        .as_array()
        .map(|items| {
            items
                .iter()
                .filter_map(|item| item.as_str())
                .filter_map(|url| validate_public_url(url, true).ok())
                .collect()
        })
        .unwrap_or_default();
    let source = db
        .first("SELECT url FROM official_watch_sources WHERE id=?", &[json!(source_id)])
        .await?;
    let bound = source
        .as_ref()
        .map(|row| {
            hosts
                .iter()
                .any(|candidate| Some(candidate.as_str()) == host_of(text(row, "url").unwrap_or("")))
        })
        .unwrap_or(false);
    if !bound {
        return Err(WatchError::Invalid(
            "Watcher is not bound to a published official source",
        ));
    }
    db.execute(
        "INSERT OR IGNORE INTO official_watch_bindings(forecast_id,source_id,families) VALUES(?,?,?)",
        &[json!(forecast_id), json!(source_id), json!(compact(&json!(families)))],
    )
    .await?;
    queue_known(db, host, forecast_id, source_id, families, now_ms).await
}

async fn queue_known(
    db: &dyn Database,
    host: &dyn ForecastSource,
    forecast_id: &str,
    source_id: &str,
    families: &[String],
    now_ms: i64,
) -> Result<(), WatchError> {
    let rows = db
        .all(
            "SELECT body FROM official_source_observations WHERE source_id=? ORDER BY observed_at DESC LIMIT 100",
            &[json!(source_id)],
        )
        .await?;
    for row in rows {
        let Ok(observation) = serde_json::from_str::<Value>(text(&row, "body").unwrap_or("")) else {
            continue;
        };
        if relevant(observation["excerpt"].as_str().unwrap_or(""), families) {
            enqueue(db, host, forecast_id, &observation, now_ms, None).await?;
        }
    }
    Ok(())
}

/// The forecast lookup the watch needs, kept as a trait so the poller cannot settle anything.
pub trait ForecastSource {
    fn load_forecast<'a>(
        &'a self,
        forecast_id: &'a str,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<Value, WatchError>> + 'a>>;
}

/// Queue an observation for review, and hold participation while it is looked at.
pub async fn enqueue(
    db: &dyn Database,
    host: &dyn ForecastSource,
    forecast_id: &str,
    observation: &Value,
    now_ms: i64,
    hold: Option<&dyn Hold>,
) -> Result<(), WatchError> {
    // The key and the review row carry the *forecast's* specification hash, not one the
    // observation happens to mention: a review is a review of a question, and a page that
    // names a different specification must not be able to attach itself to this one.
    let forecast = host.load_forecast(forecast_id).await?;
    if !matches!(forecast["state"].as_str(), Some("OPEN") | Some("LOCKED")) {
        return Ok(());
    }
    let key = hash_hex(&format!(
        "{}{}{}",
        observation["contentHash"].as_str().unwrap_or(""),
        forecast["specificationHash"].as_str().unwrap_or(""),
        POLICY
    ));
    let existing = db
        .first("SELECT state FROM official_source_reviews WHERE id=?", &[json!(key)])
        .await?;
    if existing.as_ref().and_then(|row| text(row, "state")) == Some("complete") {
        return Ok(());
    }
    db.execute(
        "INSERT OR IGNORE INTO official_source_reviews(id,observation_id,forecast_id,specification_hash,content_hash,policy,next_attempt) VALUES(?,?,?,?,?,?,?)",
        &[
            json!(key),
            json!(observation["id"].as_str().unwrap_or("")),
            json!(forecast_id),
            json!(forecast["specificationHash"].as_str().unwrap_or("")),
            json!(observation["contentHash"].as_str().unwrap_or("")),
            json!(POLICY),
            json!(now_ms),
        ],
    )
    .await?;
    // Crucially the hold happens here, before scheduling or awaiting ANY AI.
    if let Some(hold) = hold {
        hold.hold(forecast_id, observation).await?;
    }
    Ok(())
}

/// Holding participation. A separate trait because a poller that could settle a forecast or
/// credit points would be a second authority on the outcome.
pub trait Hold {
    fn hold<'a>(
        &'a self,
        forecast_id: &'a str,
        observation: &'a Value,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<(), WatchError>> + 'a>>;
}

/// Cheap candidates for the compile gate. The caller must still reject or hold.
pub async fn check_known(db: &dyn Database, forecast: &Value) -> Result<Vec<Value>, WatchError> {
    let hosts: Vec<String> = forecast["officialSourceUrls"]
        .as_array()
        .map(|items| {
            items
                .iter()
                .filter_map(|item| item.as_str())
                .filter_map(|url| validate_public_url(url, true).ok())
                .collect()
        })
        .unwrap_or_default();
    let families: Vec<String> = forecast["families"]
        .as_array()
        .map(|items| {
            items
                .iter()
                .filter_map(|item| item.as_str().map(str::to_string))
                .collect()
        })
        .unwrap_or_default();
    let rows = db
        .all(
            "SELECT body FROM official_source_observations ORDER BY observed_at DESC LIMIT 100",
            &[],
        )
        .await?;
    let mut found = Vec::new();
    for row in rows {
        let Ok(value) = serde_json::from_str::<Value>(text(&row, "body").unwrap_or("")) else {
            continue;
        };
        if hosts
            .iter()
            .any(|candidate| Some(candidate.as_str()) == host_of(value["url"].as_str().unwrap_or("")))
            && relevant(value["excerpt"].as_str().unwrap_or(""), &families)
        {
            found.push(value);
        }
    }
    Ok(found)
}

/// The dispatch table for the source list, exposed so a caller can render what is watched.
pub async fn registered(db: &dyn Database, id: &str) -> Result<Option<Row>, WatchError> {
    Ok(db
        .first("SELECT * FROM official_watch_sources WHERE id=?", &[json!(id)])
        .await?)
}

fn host_of(url: &str) -> Option<&str> {
    url.split("://")
        .nth(1)
        .and_then(|rest| rest.split(['/', '?', '#']).next())
        .filter(|host| !host.is_empty())
}

fn valid_identifier(value: &str) -> bool {
    (1..=100).contains(&value.len()) && value.chars().all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
}

fn valid_family(value: &str) -> bool {
    (2..=50).contains(&value.len()) && value.chars().all(|c| c.is_ascii_alphanumeric() || c == ' ' || c == '-')
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::{Database, Sqlite};
    use std::pin::Pin;

    const APPLE: &str = "https://www.apple.com/newsroom/";

    struct Source {
        forecast: Value,
    }

    impl ForecastSource for Source {
        fn load_forecast<'a>(
            &'a self,
            _forecast_id: &'a str,
        ) -> Pin<Box<dyn std::future::Future<Output = Result<Value, WatchError>> + 'a>> {
            let value = self.forecast.clone();
            Box::pin(async move { Ok(value) })
        }
    }

    const SPEC: &str = "1111111111111111111111111111111111111111111111111111111111111111";

    fn forecast() -> Value {
        json!({"state": "OPEN", "specificationHash": SPEC,
               "officialSourceUrls": [APPLE], "families": ["Product X"]})
    }

    /// The parents a review row references: the schema says a review is of a question and of an
    /// observation, and a fixture that skipped them would be testing a schema that is not there.
    fn parents(db: &Sqlite, forecast_id: &str, observation_id: &str) {
        block(db.execute(
            "INSERT INTO users(id,display_name,handle,recovery_hash,created_at) VALUES('u','H','h','r',1)",
            &[],
        ))
        .unwrap();
        block(db.execute(
            &format!(
                "INSERT INTO forecasts(id,creator_id,draft_id,snapshot,revision,state,category,title,question,\
                 normalized_question,specification_hash,open_at,close_at,created_at,updated_at,mutation_key) \
                 VALUES('{forecast_id}','u','d-{forecast_id}','{{}}',1,'OPEN','CRYPTO','t','q','q','{SPEC}',0,1,1,1,'k')"
            ),
            &[],
        ))
        .unwrap();
        block(db.execute(
            &format!(
                "INSERT OR IGNORE INTO official_watch_sources(id,url,kind,interval_ms,next_poll) \
                 VALUES('apple','{APPLE}','index',300000,0)"
            ),
            &[],
        ))
        .unwrap();
        // The observation references the retained bytes, so the bytes have to exist: the
        // schema will not let an observation point at an artifact nobody kept.
        block(db.execute(
            "INSERT INTO artifacts(hash,kind,body,media_type,created_at) \
             VALUES('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','source','body','text/html',1)",
            &[],
        ))
        .unwrap();
        block(db.execute(
            &format!(
                "INSERT INTO official_source_observations(id,source_id,url,content_hash,artifact_hash,body,observed_at) \
                 VALUES('{observation_id}','apple','{APPLE}',\
                 'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',\
                 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','{{}}',1)"
            ),
            &[],
        ))
        .unwrap();
    }

    fn block<F: std::future::Future>(future: F) -> F::Output {
        futures_lite::future::block_on(future)
    }

    #[test]
    fn a_source_must_be_a_registered_official_publisher() {
        let db = Sqlite::from_migrations();
        let err = block(register(
            &db,
            Registration {
                source_id: "evil",
                url: "https://evil.test/feed",
                kind: "index",
                interval_ms: 300_000,
                parent_id: None,
                pinned: false,
            },
            1,
        ))
        .unwrap_err();
        assert!(matches!(err, WatchError::Rejected(_)), "{err:?}");
        // The interval is bounded on both sides, so a source cannot be polled flat out.
        assert!(matches!(
            block(register(
                &db,
                Registration {
                    source_id: "apple",
                    url: APPLE,
                    kind: "index",
                    interval_ms: 1_000,
                    parent_id: None,
                    pinned: false
                },
                1
            ))
            .unwrap_err(),
            WatchError::Invalid("Invalid official source registration")
        ));
        block(register(
            &db,
            Registration {
                source_id: "apple",
                url: APPLE,
                kind: "index",
                interval_ms: 300_000,
                parent_id: None,
                pinned: false,
            },
            1,
        ))
        .unwrap();
    }

    #[test]
    fn a_registration_cannot_be_quietly_changed() {
        let db = Sqlite::from_migrations();
        block(register(
            &db,
            Registration {
                source_id: "apple",
                url: APPLE,
                kind: "index",
                interval_ms: 300_000,
                parent_id: None,
                pinned: false,
            },
            1,
        ))
        .unwrap();
        let changed = block(register(
            &db,
            Registration {
                source_id: "apple",
                url: APPLE,
                kind: "article",
                interval_ms: 300_000,
                parent_id: None,
                pinned: false,
            },
            1,
        ));
        assert!(matches!(
            changed,
            Err(WatchError::Invalid("Source registration is immutable"))
        ));
    }

    #[test]
    fn an_article_must_belong_to_a_watched_publisher() {
        let db = Sqlite::from_migrations();
        block(register(
            &db,
            Registration {
                source_id: "apple",
                url: APPLE,
                kind: "index",
                interval_ms: 300_000,
                parent_id: None,
                pinned: false,
            },
            1,
        ))
        .unwrap();
        block(register(
            &db,
            Registration {
                source_id: "apple-article",
                url: &format!("{APPLE}2026/09/x/"),
                kind: "article",
                interval_ms: 3_600_000,
                parent_id: Some("apple"),
                pinned: false,
            },
            1,
        ))
        .unwrap();
        // The same host, but the parent is not an index.
        let wrong_parent = block(register(
            &db,
            Registration {
                source_id: "other",
                url: "https://www.apple.com/other/",
                kind: "article",
                interval_ms: 3_600_000,
                parent_id: Some("apple-article"),
                pinned: false,
            },
            1,
        ));
        assert!(matches!(wrong_parent, Err(WatchError::Invalid(_))));
    }

    #[test]
    fn a_binding_needs_a_published_source_and_a_bounded_family() {
        let db = Sqlite::from_migrations();
        parents(&db, "f", "o-bind");
        let host = Source { forecast: forecast() };
        block(register(
            &db,
            Registration {
                source_id: "apple",
                url: APPLE,
                kind: "index",
                interval_ms: 300_000,
                parent_id: None,
                pinned: false,
            },
            1,
        ))
        .unwrap();
        // A registered publisher the question did not name, on a different host.
        block(register(
            &db,
            Registration {
                source_id: "nasa",
                url: "https://www.nasa.gov/feed/",
                kind: "index",
                interval_ms: 300_000,
                parent_id: None,
                pinned: false,
            },
            1,
        ))
        .unwrap();

        assert!(matches!(
            block(bind(&db, &host, "f", "apple", &[], 1)).unwrap_err(),
            WatchError::Invalid("A bounded product family is required")
        ));
        // A watched source that the question never named cannot be bound to it.
        assert!(matches!(
            block(bind(&db, &host, "f", "nasa", &["Product X".into()], 1)).unwrap_err(),
            WatchError::Invalid("Watcher is not bound to a published official source")
        ));
        block(bind(&db, &host, "f", "apple", &["Product X".into()], 1)).unwrap();
        let row = block(db.first(
            "SELECT families FROM official_watch_bindings WHERE forecast_id='f'",
            &[],
        ))
        .unwrap()
        .unwrap();
        assert_eq!(text(&row, "families"), Some("[\"Product X\"]"));
    }

    #[test]
    fn queueing_holds_participation_before_anything_is_scheduled() {
        // The order is the whole point: the hold is what stops a reward being credited on
        // evidence that has not been looked at yet.
        struct Recorder(std::cell::RefCell<Vec<String>>);
        impl Hold for Recorder {
            fn hold<'a>(
                &'a self,
                forecast_id: &'a str,
                _observation: &'a Value,
            ) -> Pin<Box<dyn std::future::Future<Output = Result<(), WatchError>> + 'a>> {
                self.0.borrow_mut().push(forecast_id.to_string());
                Box::pin(async { Ok(()) })
            }
        }
        let db = Sqlite::from_migrations();
        parents(&db, "f", "o1");
        let recorder = Recorder(std::cell::RefCell::new(Vec::new()));
        // The hashes are the schema's own width. `INSERT OR IGNORE` swallows a CHECK failure
        // without a word, so a fixture with short hashes would look like a silent no-op.
        const CONTENT: &str = "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd";
        let observation = json!({"id": "o1", "contentHash": CONTENT, "specificationHash": "ignored"});
        let host = Source { forecast: forecast() };
        block(enqueue(&db, &host, "f", &observation, 5, Some(&recorder))).unwrap();
        assert_eq!(recorder.0.borrow().as_slice(), ["f".to_string()]);

        let row = block(db.first("SELECT state, policy, next_attempt FROM official_source_reviews", &[]))
            .unwrap()
            .unwrap();
        assert_eq!(text(&row, "policy"), Some(POLICY));
        assert_eq!(int(&row, "next_attempt"), Some(5));
        assert_eq!(text(&row, "state"), Some("pending"));

        // A completed review is not queued again, so the hold does not repeat either.
        block(db.execute("UPDATE official_source_reviews SET state='complete'", &[])).unwrap();
        block(enqueue(&db, &host, "f", &observation, 6, Some(&recorder))).unwrap();
        assert_eq!(recorder.0.borrow().len(), 1, "a completed review is not queued again");

        // A forecast that has left the states a review may hold is not held at all.
        let closed = Source {
            forecast: json!({"state": "FINALIZED", "specificationHash": "spec"}),
        };
        let other =
            json!({"id": "o2", "contentHash": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"});
        block(enqueue(&db, &closed, "f", &other, 7, Some(&recorder))).unwrap();
        assert_eq!(recorder.0.borrow().len(), 1, "a finalized forecast is not held again");
    }

    #[test]
    fn an_artifact_that_is_not_its_own_hash_is_refused() {
        // Retained evidence is immutable, so it is checked before it is written rather than
        // after something has already been credited from it.
        let wrong = artifact_sql(
            &[(
                "not-the-hash".to_string(),
                "source".into(),
                "body".into(),
                "text/html".into(),
            )],
            1,
        );
        assert!(matches!(
            wrong,
            Err(WatchError::Invalid("Invalid retained source artifact"))
        ));
        let body = "<p>x</p>";
        let right = artifact_sql(&[(hash_hex(body), "source".into(), body.into(), "text/html".into())], 7).unwrap();
        assert_eq!(right.len(), 1);
        assert_eq!(right[0].1[4], json!(7));
    }
}
