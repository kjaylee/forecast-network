//! `SourceWatch`, the parts that decide: what may be watched, what a binding means, and when
//! participation stops while evidence is looked at.
//!
//! One rule in here is load-bearing and easy to lose in a transcription: the hold happens when
//! an observation is queued, **before** any AI is scheduled or awaited. Participation stops
//! while the evidence is looked at, not after a model has had an opinion about it.
//!
//! The polling loop is below. Its one subtlety is where the conditional request goes: the
//! collector owns the fetch, so `If-None-Match` and `If-Modified-Since` have to be added by a
//! wrapper *around* that fetch rather than by a call beside it. A 304 then comes back as a
//! response the collector would call an unexpected status, so the wrapper records it and the
//! caller reads the flag rather than the status.

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

/// Retained evidence: content hash, kind, body, media type.
pub type Retained = (String, String, String, String);
pub type BoxedFuture<'a, T> = std::pin::Pin<Box<dyn std::future::Future<Output = T> + 'a>>;

/// The transport, boxed so the watch can hold one without becoming generic over it. It takes
/// the target and the headers the caller wants, and returns what came back.
pub type Fetcher = Box<dyn Fn(String, Vec<(String, String)>) -> FetchFuture>;

pub type FetchFuture = std::pin::Pin<Box<dyn std::future::Future<Output = Result<crate::sources::TextResponse, ()>>>>;

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
    fn load_forecast<'a>(&'a self, forecast_id: &'a str) -> BoxedFuture<'a, Result<Value, WatchError>>;
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
    fn hold<'a>(&'a self, forecast_id: &'a str, observation: &'a Value) -> BoxedFuture<'a, Result<(), WatchError>>;
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

/// Poll one source under its lease: fetch what changed, discover what a feed now links to, and
/// queue the articles whose text is relevant to a binding.
pub async fn poll(
    db: &dyn Database,
    fetch: &Fetcher,
    host: &dyn ForecastSource,
    hold: Option<&dyn Hold>,
    source: &Row,
    now_ms: i64,
) -> Result<&'static str, WatchError> {
    use crate::sources::{collect, discover_articles, SourceError, SourceRejected};
    use std::cell::{Cell, RefCell};
    use std::rc::Rc;

    let source_url = text(source, "url").unwrap_or("").to_string();
    let source_id = text(source, "id").unwrap_or("").to_string();
    let etag = text(source, "etag")
        .map(str::to_string)
        .filter(|value| !value.is_empty());
    let last_modified = text(source, "last_modified")
        .map(str::to_string)
        .filter(|value| !value.is_empty());
    let interval_ms = int(source, "interval_ms").unwrap_or(0);
    let kind = text(source, "kind").unwrap_or("").to_string();
    let parent_id = text(source, "parent_id").map(str::to_string);

    // The collector owns the fetch, so the conditional request is a wrapper around it. A 304
    // then arrives as a status the collector would call unexpected, and the wrapper is what
    // remembers it happened, and whether it was answerable.
    let captured: Rc<RefCell<Vec<(String, String)>>> = Rc::new(RefCell::new(Vec::new()));
    let saw_304 = Rc::new(Cell::new(false));
    let unconditional_304 = Rc::new(Cell::new(false));
    let fetch_within = |target: String| {
        let (captured, saw_304, unconditional_304) = (captured.clone(), saw_304.clone(), unconditional_304.clone());
        let (want_etag, want_modified) = (etag.clone(), last_modified.clone());
        let is_source = target == source_url;
        // The conditional request itself. Without it a poller asks for the whole document
        // every time and the publisher answers it, which is the cost this is here to avoid.
        let mut headers: Vec<(String, String)> = Vec::new();
        if is_source {
            if let Some(value) = want_etag.clone() {
                headers.push(("If-None-Match".to_string(), value));
            }
            if let Some(value) = want_modified.clone() {
                headers.push(("If-Modified-Since".to_string(), value));
            }
        }
        let response = fetch(target, headers);
        async move {
            let mut response = response.await?;
            captured.borrow_mut().append(&mut response.headers.clone());
            if response.status == 304 {
                saw_304.set(true);
                if !is_source || (want_etag.is_none() && want_modified.is_none()) {
                    unconditional_304.set(true);
                }
            }
            // RSS and Atom are XML documents, subject to the same byte and redirect bounds.
            // Only the media label is normalized, never the bytes.
            let label = response
                .headers
                .iter()
                .rev()
                .find(|(key, _)| key.eq_ignore_ascii_case("content-type"))
                .map(|(_, value)| value.split(';').next().unwrap_or("").trim().to_lowercase())
                .unwrap_or_default();
            if label == "application/rss+xml" || label == "application/atom+xml" {
                response
                    .headers
                    .retain(|(key, _)| !key.eq_ignore_ascii_case("content-type"));
                response
                    .headers
                    .push(("content-type".to_string(), "application/xml".to_string()));
            }
            Ok(response)
        }
    };
    let collected = match collect(fetch_within, &source_id, &source_url, true, None, now_ms).await {
        Err(SourceError::Rejected(SourceRejected::HttpStatus(304))) if saw_304.get() => {
            if unconditional_304.get() {
                return Err(WatchError::Rejected("Unconditional source returned 304".to_string()));
            }
            poll_done(db, source, etag.as_deref(), last_modified.as_deref(), now_ms).await?;
            return Ok("unchanged");
        }
        Err(error) => return Err(source_error(error)),
        Ok(collected) => collected,
    };
    if kind == "index" {
        let discovered = discover_articles(&collected.artifact_body, &collected.url);
        if discovered.is_empty() {
            return Err(WatchError::Rejected(
                "Official feed no longer exposes readable article links".to_string(),
            ));
        }
        let mut active_ids: Vec<String> = Vec::new();
        for url in &discovered {
            let article_id = format!("article-{}", &hash_hex(url)[..32]);
            active_ids.push(article_id.clone());
            register(
                db,
                Registration {
                    source_id: &article_id,
                    url,
                    kind: "article",
                    interval_ms: interval_ms.max(3_600_000),
                    parent_id: Some(&source_id),
                    pinned: false,
                },
                now_ms,
            )
            .await?;
            db.execute(
                "UPDATE official_watch_sources SET enabled=1,next_poll=? WHERE id=? AND enabled=0",
                &[json!(now_ms), json!(article_id)],
            )
            .await?;
        }
        // A feed has a bounded live window. Older observations stay fully retained for the
        // publication guards and review; polling does not grow with the publisher's lifetime
        // article count.
        let placeholders = vec!["?"; active_ids.len()].join(",");
        let mut parameters: Vec<Value> = vec![json!(source_id)];
        parameters.extend(active_ids.iter().map(|id| json!(id)));
        db.execute(
            &format!(
                "UPDATE official_watch_sources SET enabled=0 WHERE parent_id=? AND pinned=0 AND id NOT IN ({placeholders})"
            ),
            &parameters,
        )
        .await?;
        poll_done(db, source, etag.as_deref(), last_modified.as_deref(), now_ms).await?;
        return Ok("index");
    }
    let (body, (date, precision)) = crate::article::article_content(&collected.artifact_body)
        .map_err(|_| WatchError::Rejected("Article evidence could not be read".to_string()))?;
    if body.len() < 40 {
        return Err(WatchError::Rejected("Article text is incomplete".to_string()));
    }
    let content_hash = hash_hex(&compact(&json!({
        "text": body, "publicationDate": date, "datePrecision": precision,
    })));
    let root_id = parent_id.clone().unwrap_or_else(|| source_id.clone());
    let identity = hash_hex(&format!("{root_id}{}{content_hash}", collected.url));
    let mut observation = json!({
        "id": identity, "sourceId": root_id, "url": collected.url,
        "contentHash": content_hash, "artifactHash": collected.content_sha256,
        "excerpt": body.chars().take(24_000).collect::<String>(), "observedAt": now_ms,
        "publicationDate": date, "datePrecision": precision, "policy": POLICY,
    });
    if db
        .first(
            "SELECT body FROM official_source_observations WHERE id=?",
            &[json!(identity)],
        )
        .await?
        .is_none()
    {
        let mut statements = artifact_sql(
            &[(
                collected.content_sha256.clone(),
                collected.artifact_kind.to_string(),
                collected.artifact_body.clone(),
                collected.media_type.clone(),
            )],
            now_ms,
        )?;
        statements.push((
            "INSERT OR IGNORE INTO official_source_observations(id,source_id,url,content_hash,artifact_hash,body,observed_at) VALUES(?,?,?,?,?,?,?)".to_string(),
            vec![
                json!(identity),
                json!(root_id),
                json!(source_url),
                json!(content_hash),
                json!(collected.content_sha256),
                json!(compact(&observation)),
                json!(now_ms),
            ],
        ));
        db.batch(&statements).await?;
    }
    if let Some(row) = db
        .first(
            "SELECT body FROM official_source_observations WHERE id=?",
            &[json!(identity)],
        )
        .await?
    {
        if let Ok(stored) = serde_json::from_str::<Value>(text(&row, "body").unwrap_or("")) {
            observation = stored;
        }
    }
    let bindings = db
        .all(
            "SELECT * FROM official_watch_bindings WHERE source_id=? LIMIT ?",
            &[json!(root_id), json!(MAX_BINDINGS)],
        )
        .await?;
    for binding in bindings {
        let families: Vec<String> =
            serde_json::from_str(text(&binding, "families").unwrap_or("[]")).unwrap_or_default();
        if relevant(&body, &families) {
            if let Some(forecast_id) = text(&binding, "forecast_id") {
                enqueue(db, host, forecast_id, &observation, now_ms, hold).await?;
            }
        }
    }
    poll_done(db, source, etag.as_deref(), last_modified.as_deref(), now_ms).await?;
    Ok("article")
}

fn source_error(error: crate::sources::SourceError) -> WatchError {
    match error {
        crate::sources::SourceError::Unavailable => WatchError::Unavailable,
        crate::sources::SourceError::Rejected(rejected) => WatchError::Rejected(rejected.message()),
    }
}

async fn poll_done(
    db: &dyn Database,
    source: &Row,
    etag: Option<&str>,
    modified: Option<&str>,
    now_ms: i64,
) -> Result<(), WatchError> {
    let etag: String = etag.unwrap_or("").chars().take(512).collect();
    let modified: String = modified.unwrap_or("").chars().take(128).collect();
    let interval = int(source, "interval_ms").unwrap_or(0);
    let id = text(source, "id").unwrap_or("");
    db.execute(
        "UPDATE official_watch_sources SET etag=?,last_modified=?,checked_at=?,next_poll=?,failure_count=0,last_error=NULL WHERE id=? AND lease_token=? AND lease_until>?",
        &[
            json!(if etag.is_empty() { None } else { Some(etag) }),
            json!(if modified.is_empty() { None } else { Some(modified) }),
            json!(now_ms),
            json!(now_ms + interval),
            json!(id),
            json!(text(source, "lease_token").unwrap_or("")),
            json!(now_ms),
        ],
    )
    .await?;
    Ok(())
}

/// Reviewing what a source published. Async and injected, because the decision is a model's
/// and the poller is not allowed to make it.
pub trait Reviewer {
    /// Decide, and say what was retained while deciding.
    fn review<'a>(
        &'a self,
        forecast: &'a Value,
        observation: &'a Value,
    ) -> BoxedFuture<'a, Result<(Value, Vec<Retained>), WatchError>>;

    /// Act on a review that accepted the observation.
    fn accept<'a>(&'a self, forecast_id: &'a str, review: &'a Value) -> BoxedFuture<'a, Result<(), WatchError>>;
}

/// Dismissing a review that a model found nothing in. Optional, because not every caller
/// chooses to act on a dismissal.
pub trait Dismisser {
    fn dismiss<'a>(&'a self, forecast_id: &'a str, review: &'a Value) -> BoxedFuture<'a, Result<(), WatchError>>;
}

/// Everything the job loop reaches for. Named rather than positional: a call with five
/// collaborators and a limit is not readable as a call.
pub struct Watch<'a> {
    pub db: &'a dyn Database,
    pub fetch: &'a Fetcher,
    pub host: &'a dyn ForecastSource,
    pub hold: Option<&'a dyn Hold>,
    pub reviewer: Option<&'a dyn Reviewer>,
    pub dismisser: Option<&'a dyn Dismisser>,
    pub now_ms: i64,
    pub token: &'a str,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Summary {
    pub polled: i64,
    pub reviewed: i64,
    pub failed: i64,
    pub unchanged: i64,
}

/// One pass of the job loop: poll what is due, then review what was queued.
pub async fn run(watch: &Watch<'_>, limit: i64) -> Result<Summary, WatchError> {
    if !(1..=6).contains(&limit) {
        return Err(WatchError::Invalid("Source job bound must be between one and six"));
    }
    let mut summary = Summary::default();
    let sources = watch
        .db
        .all(
            "SELECT * FROM official_watch_sources WHERE enabled=1 AND next_poll<=? AND lease_until<=? ORDER BY next_poll,id LIMIT ?",
            &[json!(watch.now_ms), json!(watch.now_ms), json!(limit)],
        )
        .await?;
    for source in sources {
        let lease = format!(
            "{}-{}",
            watch.token,
            source.get("id").and_then(Value::as_str).unwrap_or("")
        );
        let claimed = watch
            .db
            .execute(
                "UPDATE official_watch_sources SET lease_token=?,lease_until=? WHERE id=? AND enabled=1 AND next_poll<=? AND lease_until<=? RETURNING id",
                &[
                    json!(lease),
                    json!(watch.now_ms + LEASE_MS),
                    source.get("id").cloned().unwrap_or(Value::Null),
                    json!(watch.now_ms),
                    json!(watch.now_ms),
                ],
            )
            .await;
        let claimed = claimed.map(|rows| !rows.is_empty()).unwrap_or(false);
        if !claimed {
            continue;
        }
        let mut leased = source.clone();
        leased.insert("lease_token".to_string(), json!(lease));
        let outcome = poll(watch.db, watch.fetch, watch.host, watch.hold, &leased, watch.now_ms).await;
        match &outcome {
            Ok("unchanged") => summary.unchanged += 1,
            Ok(_) => summary.polled += 1,
            Err(error) => {
                summary.failed += 1;
                let failures = int(&source, "failure_count").unwrap_or(0);
                let interval = int(&source, "interval_ms").unwrap_or(0);
                let backoff = interval * 2i64.pow(failures.clamp(0, 4) as u32);
                watch
                    .db
                    .execute(
                        "UPDATE official_watch_sources SET failure_count=failure_count+1,last_error=?,next_poll=? WHERE id=? AND lease_token=?",
                        &[
                            json!(error_kind(error)),
                            json!(watch.now_ms + backoff.min(3_600_000)),
                            source.get("id").cloned().unwrap_or(Value::Null),
                            json!(lease),
                        ],
                    )
                    .await?;
            }
        }
        watch
            .db
            .execute(
                "UPDATE official_watch_sources SET lease_token=NULL,lease_until=0 WHERE id=? AND lease_token=?",
                &[source.get("id").cloned().unwrap_or(Value::Null), json!(lease)],
            )
            .await?;
    }

    let jobs = watch
        .db
        .all(
            "SELECT * FROM official_source_reviews WHERE state IN ('pending','reviewed') AND next_attempt<=? AND lease_until<=? ORDER BY next_attempt,id LIMIT ?",
            &[json!(watch.now_ms), json!(watch.now_ms), json!(limit)],
        )
        .await?;
    for job in jobs {
        let job_id = job.get("id").cloned().unwrap_or(Value::Null);
        let lease = format!("{}-review-{}", watch.token, job_id.as_str().unwrap_or(""));
        let claimed = watch
            .db
            .execute(
                "UPDATE official_source_reviews SET lease_token=?,lease_until=?,attempts=attempts+1 WHERE id=? AND state IN ('pending','reviewed') AND next_attempt<=? AND lease_until<=? RETURNING id",
                &[json!(lease), json!(watch.now_ms + LEASE_MS), job_id.clone(), json!(watch.now_ms), json!(watch.now_ms)],
            )
            .await
            .map(|rows| !rows.is_empty())
            .unwrap_or(false);
        if !claimed {
            continue;
        }
        let outcome = review_one(watch, &job, &lease).await;
        match outcome {
            Ok(true) => summary.reviewed += 1,
            Ok(false) => {}
            Err(error) => {
                summary.failed += 1;
                record_review_failure(watch, &job, &lease, &error).await?;
            }
        }
        watch
            .db
            .execute(
                "UPDATE official_source_reviews SET lease_token=NULL,lease_until=0 WHERE id=? AND lease_token=?",
                &[job_id, json!(lease)],
            )
            .await?;
    }
    Ok(summary)
}

async fn review_one(watch: &Watch<'_>, job: &Row, lease: &str) -> Result<bool, WatchError> {
    let observation_id = text(job, "observation_id").unwrap_or("");
    let row = watch
        .db
        .first(
            "SELECT body FROM official_source_observations WHERE id=?",
            &[json!(observation_id)],
        )
        .await?
        .ok_or(WatchError::Missing("Retained observation"))?;
    let observation: Value = serde_json::from_str(text(&row, "body").unwrap_or("")).unwrap_or(Value::Null);
    let forecast_id = text(job, "forecast_id").unwrap_or("");
    let forecast = watch.host.load_forecast(forecast_id).await?;
    if forecast["specificationHash"] != job.get("specification_hash").cloned().unwrap_or(Value::Null) {
        return Err(WatchError::Invalid("Published specification changed"));
    }
    // The hold, again, before the model is asked: the review loop re-holds because a review
    // queued long ago may be running after the forecast moved.
    if let Some(hold) = watch.hold {
        hold.hold(forecast_id, &observation).await?;
    }
    let existing = text(job, "result").map(str::to_string);
    let (result, artifacts) = match existing {
        Some(stored) => (
            serde_json::from_str::<Value>(&stored).unwrap_or(Value::Null),
            Vec::new(),
        ),
        None => {
            let bucket = watch.now_ms / DAY_MS;
            watch
                .db
                .execute(
                    "INSERT OR IGNORE INTO rate_limits(scope,bucket,count,expires_at) VALUES('official-watch-ai',?,0,?)",
                    &[json!(bucket), json!((bucket + 2) * DAY_MS)],
                )
                .await?;
            // The budget holds across concurrent workers, because it is a row the database
            // updates under its own condition rather than a number this process counted.
            let budget = watch
                .db
                .execute(
                    "UPDATE rate_limits SET count=count+3 WHERE scope='official-watch-ai' AND bucket=? AND count<=69 RETURNING count",
                    &[json!(bucket)],
                )
                .await?;
            if budget.is_empty() {
                return Err(WatchError::BudgetExhausted);
            }
            let Some(reviewer) = watch.reviewer else {
                return Err(WatchError::Missing("Reviewer"));
            };
            let (result, artifacts) = reviewer.review(&forecast, &observation).await?;
            (result, artifacts)
        }
    };
    if !result["accepted"].is_boolean() || compact(&result).len() > 65_536 {
        return Err(WatchError::Invalid("Invalid bounded source review result"));
    }
    let mut statements = artifact_sql(&artifacts, watch.now_ms)?;
    statements.push((
        "UPDATE official_source_reviews SET state='reviewed',result=? WHERE id=? AND lease_token=? AND lease_until>?"
            .to_string(),
        vec![
            json!(compact(&result)),
            job.get("id").cloned().unwrap_or(Value::Null),
            json!(lease),
            json!(watch.now_ms),
        ],
    ));
    watch.db.batch(&statements).await?;
    // The lease is the authority: a review whose lease expired is not acted on, whatever the
    // model said.
    let owned = watch
        .db
        .first(
            "SELECT id FROM official_source_reviews WHERE id=? AND lease_token=? AND lease_until>?",
            &[
                job.get("id").cloned().unwrap_or(Value::Null),
                json!(lease),
                json!(watch.now_ms),
            ],
        )
        .await?;
    if owned.is_none() {
        return Err(WatchError::Invalid("Source review lease expired"));
    }
    let mut acted = result.clone();
    acted["observation"] = observation.clone();
    if result["accepted"] == json!(true) {
        if let Some(reviewer) = watch.reviewer {
            reviewer.accept(forecast_id, &acted).await?;
        }
    } else if result["accepted"] == json!(false) && result["dismissible"] == json!(true) {
        if let Some(dismisser) = watch.dismisser {
            dismisser.dismiss(forecast_id, &acted).await?;
        }
    }
    watch
        .db
        .execute(
            "UPDATE official_source_reviews SET state='complete',result=?,last_error=NULL WHERE id=? AND lease_token=? AND lease_until>?",
            &[json!(compact(&result)), job.get("id").cloned().unwrap_or(Value::Null), json!(lease), json!(watch.now_ms)],
        )
        .await?;
    Ok(true)
}

async fn record_review_failure(
    watch: &Watch<'_>,
    job: &Row,
    lease: &str,
    error: &WatchError,
) -> Result<(), WatchError> {
    let job_id = job.get("id").cloned().unwrap_or(Value::Null);
    if matches!(error, WatchError::BudgetExhausted) {
        let day = watch.now_ms / DAY_MS;
        watch
            .db
            .execute(
                "UPDATE official_source_reviews SET attempts=attempts-1,next_attempt=?,last_error='daily_budget' WHERE id=? AND lease_token=?",
                &[json!((day + 1) * DAY_MS), job_id, json!(lease)],
            )
            .await?;
        return Ok(());
    }
    let attempts = int(job, "attempts").unwrap_or(0);
    let state = if attempts + 1 >= MAX_ATTEMPTS {
        "exhausted"
    } else {
        "pending"
    };
    watch
        .db
        .execute(
            "UPDATE official_source_reviews SET state=?,last_error=?,next_attempt=? WHERE id=? AND lease_token=?",
            &[
                json!(state),
                json!(error_kind(error)),
                json!(watch.now_ms + 60_000 * 2i64.pow(attempts.clamp(0, 10) as u32)),
                job_id,
                json!(lease),
            ],
        )
        .await?;
    Ok(())
}

/// Register a reported article under its watched publisher, fetch it now, and queue review for
/// this forecast regardless of keyword relevance.
pub async fn ingest_report(
    db: &dyn Database,
    fetch: &Fetcher,
    host: &dyn ForecastSource,
    hold: Option<&dyn Hold>,
    forecast_id: &str,
    url: &str,
    index_id: &str,
    lease: &str,
    now_ms: i64,
) -> Result<Option<Value>, WatchError> {
    let article_id = format!("article-{}", &hash_hex(url)[..32]);
    register(
        db,
        Registration {
            source_id: &article_id,
            url,
            kind: "article",
            interval_ms: 3_600_000,
            parent_id: Some(index_id),
            pinned: true,
        },
        now_ms,
    )
    .await?;
    let claimed = db
        .execute(
            "UPDATE official_watch_sources SET lease_token=?,lease_until=? WHERE id=? AND lease_until<=? RETURNING id",
            &[json!(lease), json!(now_ms + LEASE_MS), json!(article_id), json!(now_ms)],
        )
        .await
        .map(|rows| !rows.is_empty())
        .unwrap_or(false);
    if !claimed {
        return Ok(None);
    }
    // A report must be judged on the page as it is now: fetch unconditionally even if the
    // article was polled before, so a fresh observation exists under this publisher.
    db.execute(
        "UPDATE official_watch_sources SET etag=NULL,last_modified=NULL WHERE id=? AND lease_token=?",
        &[json!(article_id), json!(lease)],
    )
    .await?;
    let Some(source) = db
        .first("SELECT * FROM official_watch_sources WHERE id=?", &[json!(article_id)])
        .await?
    else {
        return Ok(None);
    };
    let mut leased = source.clone();
    leased.insert("lease_token".to_string(), json!(lease));
    let outcome = poll(db, fetch, host, hold, &leased, now_ms).await;
    db.execute(
        "UPDATE official_watch_sources SET lease_token=NULL,lease_until=0 WHERE id=? AND lease_token=?",
        &[json!(article_id), json!(lease)],
    )
    .await?;
    // A refusal and a non-modification both leave whatever was already retained; anything else
    // is a real failure and has to surface.
    if let Err(error) = outcome {
        if !matches!(
            error,
            WatchError::Rejected(_) | WatchError::Unavailable | WatchError::NotModified
        ) {
            return Err(error);
        }
    }
    let Some(row) = db
        .first(
            "SELECT body FROM official_source_observations WHERE url=? ORDER BY observed_at DESC LIMIT 1",
            &[json!(url)],
        )
        .await?
    else {
        return Ok(None);
    };
    let observation: Value = serde_json::from_str(text(&row, "body").unwrap_or("")).unwrap_or(Value::Null);
    // A page published before the question opened cannot be its resolving event. Saying so
    // here is better than pausing participation for a review that must reject it.
    let forecast = host.load_forecast(forecast_id).await?;
    if observation["datePrecision"] == "instant" {
        if let Some(published) = observation["publicationDate"].as_str() {
            let open_at = forecast["specification"]["open_at_ms"].as_i64().unwrap_or(0);
            if crate::article::instant_ms(published) < open_at {
                let mut flagged = observation.clone();
                flagged["predatesQuestion"] = json!(true);
                return Ok(Some(flagged));
            }
        }
    }
    enqueue(db, host, forecast_id, &observation, now_ms, hold).await?;
    Ok(Some(observation))
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

    /// What the transport was asked for, so a test can say which headers went out.
    type HeaderLog = std::rc::Rc<std::cell::RefCell<Vec<Vec<(String, String)>>>>;

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
    fn a_poll_asks_conditionally_and_reads_a_not_modified_as_unchanged() {
        use std::cell::RefCell;
        use std::rc::Rc;
        let db = Sqlite::from_migrations();
        parents(&db, "f", "o1");
        // `poll_done` writes under the lease the caller took, so the fixture has to hold one.
        block(db.execute(
            "UPDATE official_watch_sources SET etag='v1', last_modified='yesterday', lease_token='L', lease_until=1000 WHERE id='apple'",
            &[],
        ))
        .unwrap();
        let source = block(db.first("SELECT * FROM official_watch_sources WHERE id='apple'", &[]))
            .unwrap()
            .unwrap();
        let host = Source { forecast: forecast() };
        let seen: HeaderLog = Rc::new(RefCell::new(Vec::new()));
        let recorder = seen.clone();
        let fetch: Fetcher = Box::new(move |_target, headers| {
            recorder.borrow_mut().push(headers);
            Box::pin(async {
                Ok(crate::sources::TextResponse {
                    status: 304,
                    headers: vec![("content-type".to_string(), "text/html".to_string())],
                    body: String::new(),
                })
            })
        });
        let outcome = block(poll(&db, &fetch, &host, None, &source, 100)).unwrap();
        assert_eq!(outcome, "unchanged");
        let headers = seen.borrow();
        assert_eq!(headers.len(), 1, "one attempt, not a retry loop");
        assert!(
            headers[0]
                .iter()
                .any(|(key, value)| key == "If-None-Match" && value == "v1"),
            "{:?}",
            headers[0]
        );
        assert!(
            headers[0]
                .iter()
                .any(|(key, value)| key == "If-Modified-Since" && value == "yesterday"),
            "{:?}",
            headers[0]
        );
        // A successful conditional poll clears the failure state and re-arms the interval.
        let row = block(db.first(
            "SELECT checked_at, next_poll, failure_count FROM official_watch_sources WHERE id='apple'",
            &[],
        ))
        .unwrap()
        .unwrap();
        assert_eq!(int(&row, "checked_at"), Some(100));
        assert_eq!(int(&row, "next_poll"), Some(100 + 300_000));
    }

    #[test]
    fn a_not_modified_with_nothing_to_compare_against_is_a_refusal() {
        // The reference refuses a 304 it did not ask for: a source answering "unchanged" to an
        // unconditional request is not something the poller can act on.
        let db = Sqlite::from_migrations();
        parents(&db, "f", "o1");
        block(db.execute(
            "UPDATE official_watch_sources SET lease_token='L', lease_until=1000 WHERE id='apple'",
            &[],
        ))
        .unwrap();
        let source = block(db.first("SELECT * FROM official_watch_sources WHERE id='apple'", &[]))
            .unwrap()
            .unwrap();
        let host = Source { forecast: forecast() };
        let fetch: Fetcher = Box::new(move |_target, _headers| {
            Box::pin(async {
                Ok(crate::sources::TextResponse {
                    status: 304,
                    headers: vec![("content-type".to_string(), "text/html".to_string())],
                    body: String::new(),
                })
            })
        });
        let error = block(poll(&db, &fetch, &host, None, &source, 100)).unwrap_err();
        assert_eq!(error.message(), "Unconditional source returned 304");
    }

    #[test]
    fn an_index_feed_queues_what_it_links_to_and_closes_the_rest_of_the_window() {
        let db = Sqlite::from_migrations();
        parents(&db, "f", "o1");
        block(db.execute(
            "INSERT INTO official_watch_bindings(forecast_id,source_id,families) VALUES('f','apple','[\"Product X\"]')",
            &[],
        ))
        .unwrap();
        block(db.execute(
            "UPDATE official_watch_sources SET lease_token='L', lease_until=1000 WHERE id='apple'",
            &[],
        ))
        .unwrap();
        let current = block(db.first("SELECT * FROM official_watch_sources WHERE id='apple'", &[]))
            .unwrap()
            .unwrap();
        let host = Source { forecast: forecast() };
        // Long enough to clear the collector's readable-content floor, which a link-only feed
        // body is not.
        let payload = "<a href=\"/newsroom/2026/09/product-x-announced/\">Product X announced today</a>\
                       <a href=\"/newsroom/2026/09/older-story/\">An older story about something else</a>\
                       <p>Product X is announced and this feed describes it at length.</p>"
            .to_string();
        let fetch: Fetcher = Box::new(move |_target, _headers| {
            let payload = payload.clone();
            Box::pin(async move {
                Ok(crate::sources::TextResponse {
                    status: 200,
                    headers: vec![("content-type".to_string(), "text/html".to_string())],
                    body: format!("<html><body><main>{payload}</main></body></html>"),
                })
            })
        });
        assert_eq!(block(poll(&db, &fetch, &host, None, &current, 100)).unwrap(), "index");
        // Both discovered articles are registered under the feed.
        let children = block(db.all(
            "SELECT id FROM official_watch_sources WHERE parent_id='apple' ORDER BY id",
            &[],
        ))
        .unwrap();
        assert_eq!(children.len(), 2, "the feed's live window");
        assert!(children
            .iter()
            .all(|row| text(row, "id").unwrap().starts_with("article-")));
    }

    #[test]
    fn the_daily_review_budget_holds_across_workers() {
        // The budget is a row the database updates under its own condition, not a number this
        // process counted, so two workers cannot both find room for the same third review.
        let db = Sqlite::from_migrations();
        parents(&db, "f", "o1");
        let day = 100 / 86_400_000;
        block(db.execute(
            &format!("INSERT INTO rate_limits(scope,bucket,count,expires_at) VALUES('official-watch-ai',{day},70,0)"),
            &[],
        ))
        .unwrap();
        block(db.execute(
            &format!(
                "INSERT INTO official_source_reviews(id,observation_id,forecast_id,specification_hash,\
                 content_hash,policy,next_attempt) VALUES('job','o1','f','{SPEC}',\
                 'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd','{POLICY}',0)"
            ),
            &[],
        ))
        .unwrap();
        let host = Source { forecast: forecast() };
        let fetch: Fetcher = Box::new(|_target, _headers| Box::pin(async { Err(()) }));
        let watch = Watch {
            db: &db,
            fetch: &fetch,
            host: &host,
            hold: None,
            reviewer: None,
            dismisser: None,
            now_ms: 100,
            token: "t",
        };
        let summary = block(run(&watch, 2)).unwrap();
        assert_eq!(summary.reviewed, 0);
        // Two failures: the source fetch refused as well, because this transport always does.
        // What this test is about is the review, and the state it is left in.
        assert_eq!(summary.failed, 2);
        let row = block(db.first(
            "SELECT state, attempts, last_error, next_attempt FROM official_source_reviews WHERE id='job'",
            &[],
        ))
        .unwrap()
        .unwrap();
        assert_eq!(text(&row, "last_error"), Some("daily_budget"));
        assert_eq!(
            int(&row, "attempts"),
            Some(0),
            "a budget refusal does not consume an attempt"
        );
        assert_eq!(
            int(&row, "next_attempt"),
            Some((day + 1) * 86_400_000),
            "it waits for tomorrow"
        );
        assert_eq!(text(&row, "state"), Some("pending"));
    }

    #[test]
    fn a_job_bound_outside_the_reference_range_is_refused() {
        let db = Sqlite::from_migrations();
        let host = Source { forecast: forecast() };
        let fetch: Fetcher = Box::new(|_t, _h| Box::pin(async { Err(()) }));
        let watch = Watch {
            db: &db,
            fetch: &fetch,
            host: &host,
            hold: None,
            reviewer: None,
            dismisser: None,
            now_ms: 1,
            token: "t",
        };
        for limit in [0, 7] {
            assert!(
                matches!(block(run(&watch, limit)), Err(WatchError::Invalid(_))),
                "limit {limit}"
            );
        }
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
