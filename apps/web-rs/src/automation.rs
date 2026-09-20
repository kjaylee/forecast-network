//! Application orchestration for retained official events: `automation.py`.
//!
//! Two halves. The first is portable on its own — the family hints a registration is keyed on, the
//! publisher-root → feed mapping the watcher actually reads, and the operator status view. The
//! second is `ForecastAutomation` itself, which is where the watcher's four questions are
//! *answered*: what a forecast looks like, how to pause one, how to review a page, and what to do
//! with a review.
//!
//! The second half is the only place in the reference where a retained official article can close
//! a question before its deadline, and it is deliberately the only thing that may pause
//! participation. `accept` requires an active hold, re-reads the evidence from retained bytes
//! before acting on it, and re-checks both the hold and the completion barrier inside the same
//! batch as the lifecycle lock — so a pause released mid-flight cannot slip an upgrade through.

use crate::ai::coordinator::{Coordinator, CoordinatorError};
use crate::ai::early::ArtifactReader;
use crate::db::{self, Database};
use crate::source_watch::WatchError;
use crate::sources::host_and_path;
use crate::{ai, eligibility, mutate, participation_holds, point_markets, points, source_watch};
use forecast_domain::lifecycle::{EarlyResolutionTrigger, Payload};
use forecast_domain::models::ForecastSpecification;
use serde_json::{json, Value};

/// `SourceWatch.register`'s default interval: five minutes.
const DEFAULT_INTERVAL_MS: i64 = 300_000;

/// `families`. Bounded family hints, and deliberately never semantic proof that an event happened.
///
/// The list is short and ordered because the first match wins: "foldable" covers three spellings of
/// one product category, and the loop below is the only place a bare substring is allowed at all.
pub fn families(specification: &ForecastSpecification) -> Vec<&'static str> {
    // `casefold()`, not `lower()`: Python's case folding maps `ß` to `ss` and `İ` to `i̇`, which
    // `to_lowercase` does not — and the reference calls `casefold`.
    let value = casefold(&format!(
        "{} {}",
        specification.canonical_question, specification.share_title
    ));
    if value.contains("fold") || value.contains("폴더블") || value.contains("접는") {
        return vec!["foldable", "folding"];
    }
    if value.contains("m6") {
        return vec!["M6"];
    }
    if value.contains("windows 12") || value.contains("윈도우 12") {
        return vec!["Windows 12"];
    }
    for name in ["iphone", "macbook", "nvidia", "spacex", "nasa", "windows"] {
        if value.contains(name) {
            return vec![name];
        }
    }
    Vec::new()
}

/// `str.casefold()` for the characters this uses.
///
/// Full case folding differs from lowercasing in a handful of places (`ß`→`ss`, `İ`→`i̇`,
/// `ﬁ`→`fi`), and the reference asks for `casefold`. Rust's `to_lowercase` is a *lowercasing*, so
/// the two full-fold cases that reach ASCII letters are applied here explicitly rather than assumed
/// away.
pub(crate) fn casefold(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    for character in value.chars() {
        match character {
            'ß' => out.push_str("ss"),
            'ẞ' => out.push_str("ss"),
            'İ' => out.push_str("i\u{307}"),
            other => out.extend(other.to_lowercase()),
        }
    }
    out
}

/// `PUBLISHER_FEEDS`. Newsroom roots are not crawlable indexes; their feeds expose dated article
/// links, so the watcher is pointed at the feed rather than at the page.
pub const PUBLISHER_FEEDS: [(&str, &str, &str); 4] = [
    (
        "www.apple.com",
        "/newsroom",
        "https://www.apple.com/newsroom/rss-feed.rss",
    ),
    ("apple.com", "/newsroom", "https://apple.com/newsroom/rss-feed.rss"),
    ("news.microsoft.com", "", "https://news.microsoft.com/source/feed/"),
    (
        "news.microsoft.com",
        "/source",
        "https://news.microsoft.com/source/feed/",
    ),
];

/// `publisher_feed_url`. A URL that is not a known publisher root is returned unchanged, which is
/// what makes this safe to apply to every official source.
pub fn publisher_feed_url(url: &str) -> String {
    let Some((host, path)) = host_and_path(url) else {
        return url.to_string();
    };
    let trimmed = path.trim_end_matches('/');
    PUBLISHER_FEEDS
        .iter()
        .find(|(registered, registered_path, _)| *registered == host && *registered_path == trimmed)
        .map_or_else(|| url.to_string(), |(_, _, feed)| (*feed).to_string())
}

/// `ForecastAutomation.status`: the operator view of source watching.
pub async fn status(db: &dyn Database, enabled: bool) -> Result<Value, String> {
    let counts = db
        .first(
            "SELECT COUNT(*) AS sources, SUM(enabled) AS enabled FROM official_watch_sources",
            &[],
        )
        .await
        .map_err(|error| error.to_string())?;
    let reviews = db
        .all(
            "SELECT state,COUNT(*) AS count FROM official_source_reviews GROUP BY state",
            &[],
        )
        .await
        .map_err(|error| error.to_string())?;
    let mut grouped = serde_json::Map::new();
    for row in &reviews {
        grouped.insert(
            db::text(row, "state").unwrap_or("").to_string(),
            db::get(row, "count").clone(),
        );
    }
    Ok(json!({
        "enabled": enabled,
        "sources": counts.as_ref().and_then(|row| db::int(row, "sources")).unwrap_or(0),
        "reviews": grouped,
        "earlyResolution": "official_monotonic_positive_only",
        "policy": "official-source-watch-v1",
    }))
}

// ---------------------------------------------------------------------------------------------
// The orchestration half.
//
// `ForecastAutomation` is where the watcher's traits are *satisfied*. Everything it needs is
// already a free function or a small struct in this crate, so this half invents no seam — it holds
// the collaborators named rather than positional, and answers the four questions the poller asks:
// what a forecast looks like, how to pause one, how to review a page, and what to do with a review.
// ---------------------------------------------------------------------------------------------

/// `ForecastAutomation`. The flag alone is not enough to watch anything.
pub struct Automation<'a> {
    pub db: &'a dyn Database,
    /// The collector. Absent means the watcher cannot fetch, which is what `enabled` records.
    pub fetch: Option<&'a source_watch::Fetcher>,
    pub ai: Option<&'a Coordinator>,
    pub reader: &'a ArtifactReader,
    pub now_ms: i64,
    pub token: &'a dyn Fn() -> String,
    pub enabled: bool,
}

impl<'a> Automation<'a> {
    /// `ForecastAutomation.__init__`: a watcher without a collector is not enabled, whatever the
    /// flag said. The reference reads the collector off the AI object, so that is the question
    /// asked here too.
    pub fn new(
        db: &'a dyn Database,
        fetch: Option<&'a source_watch::Fetcher>,
        ai: Option<&'a Coordinator>,
        reader: &'a ArtifactReader,
        now_ms: i64,
        token: &'a dyn Fn() -> String,
        enabled: bool,
    ) -> Self {
        let fetch = if enabled { fetch } else { None };
        Self {
            db,
            fetch,
            ai,
            reader,
            now_ms,
            token,
            enabled: enabled && fetch.is_some(),
        }
    }

    /// The job loop, over the tokens the caller owns.
    ///
    /// The lease tokens are a parameter rather than something this method invents, because the
    /// watcher's `token` field is a `&str` borrowed for as long as the loop runs — and a lease that
    /// expired the moment it was taken would be no lease at all.
    fn watcher(&self) -> Option<source_watch::Watch<'_>> {
        Some(source_watch::Watch {
            db: self.db,
            fetch: self.fetch?,
            host: self,
            hold: Some(self),
            reviewer: Some(self),
            dismisser: Some(self),
            now_ms: self.now_ms,
            token: self.token,
        })
    }

    /// `load`: the record every other method reads.
    ///
    /// A missing forecast is a refusal rather than an empty record, because the callers below all
    /// act on what they read.
    pub async fn load(&self, forecast_id: &str) -> Result<Value, WatchError> {
        let snapshot = mutate::load_snapshot(self.db, forecast_id)
            .await
            .map_err(|error| match error {
                crate::routes::RouteError::NotFound(code, message) => WatchError::Refused {
                    status: 404,
                    code: code.to_string(),
                    message: message.to_string(),
                },
                other => WatchError::Database(format!("{other:?}")),
            })?;
        let forecast = snapshot.base();
        Ok(json!({
            "id": forecast.forecast_id,
            "specificationHash": forecast.specification_hash,
            "state": forecast.state,
            "createdAt": forecast.published_at_ms,
            "specification": forecast_domain::models::to_value(&forecast.specification)
                .map_err(|error| WatchError::Database(error.to_string()))?,
            "families": families(&forecast.specification),
            "officialSourceUrls": official_sources(&forecast.specification),
        }))
    }

    /// `bootstrap`: register the *feed* a publisher's official root exposes, and bind it.
    ///
    /// A newsroom page is not a crawlable index; its feed is. The mapping is applied to the
    /// published URL rather than to the specification, so the question keeps the source it was
    /// approved with.
    pub async fn bootstrap(&self) -> Result<i64, WatchError> {
        if !self.enabled {
            return Ok(0);
        }
        let rows = self
            .db
            .all(
                "SELECT id FROM forecasts WHERE state='OPEN' AND close_at>? ORDER BY created_at,id LIMIT ?",
                &[json!(self.now_ms), json!(30)],
            )
            .await?;
        let mut count = 0;
        for row in rows {
            let forecast_id = db::text(&row, "id").unwrap_or("");
            let forecast = self.load(forecast_id).await?;
            let Some(hints) = forecast["families"].as_array().filter(|list| !list.is_empty()) else {
                continue;
            };
            for url in forecast["officialSourceUrls"]
                .as_array()
                .map(|items| items.iter().take(3))
                .into_iter()
                .flatten()
            {
                let Some(url) = url.as_str() else { continue };
                let Some((host, _)) = host_and_path(url) else {
                    continue;
                };
                if !WATCHED_PUBLISHERS.iter().any(|watched| *watched == host) {
                    continue;
                }
                let url = publisher_feed_url(url);
                let index_id = format!("publisher-{}", &source_watch::hash_hex(&url)[..32]);
                source_watch::register(
                    self.db,
                    source_watch::Registration {
                        source_id: &index_id,
                        url: &url,
                        kind: "index",
                        interval_ms: DEFAULT_INTERVAL_MS,
                        parent_id: None,
                        pinned: false,
                    },
                    self.now_ms,
                )
                .await?;
                let families: Vec<String> = hints
                    .iter()
                    .filter_map(|item| item.as_str().map(str::to_string))
                    .collect();
                source_watch::bind(self.db, self, forecast_id, &index_id, &families, self.now_ms).await?;
                if let Some(current) = participation_holds::active(self.db, forecast_id)
                    .await
                    .map_err(refused_from_hold)?
                {
                    // The page that closed the question is pinned: the feed window retires old
                    // articles, and the article that resolved this one has to outlive that.
                    let article_url = current["evidenceUrl"].as_str().unwrap_or("");
                    if host_and_path(article_url).map(|(value, _)| value) == Some(host) {
                        let article_id = format!("article-{}", &source_watch::hash_hex(article_url)[..32]);
                        source_watch::register(
                            self.db,
                            source_watch::Registration {
                                source_id: &article_id,
                                url: article_url,
                                kind: "article",
                                interval_ms: 3_600_000,
                                parent_id: Some(&index_id),
                                pinned: true,
                            },
                            self.now_ms,
                        )
                        .await?;
                    }
                }
                count += 1;
            }
        }
        Ok(count)
    }

    /// `hold`: pause participation while an observation is looked at.
    ///
    /// The pause is only ever *taken* here — releasing is `dismiss`'s business — and the refusal it
    /// swallows is the one the CAS raises when somebody else took a hold first.
    pub async fn hold(&self, forecast_id: &str, observation: &Value) -> Result<(), WatchError> {
        let forecast = self.load(forecast_id).await?;
        if forecast["state"] != json!("OPEN") {
            return Ok(());
        }
        let status = participation_holds::status(self.db, forecast_id)
            .await
            .map_err(refused_from_hold)?;
        if !status["hold"].is_null() {
            return Ok(());
        }
        let key = format!(
            "source-watch:{}",
            source_watch::hash_hex(&format!("{}{}", forecast_id, observation["id"].as_str().unwrap_or("")))
        );
        let body = json!({
            "action": "hold",
            "expectedRevision": status["revision"],
            "expectedHoldId": Value::Null,
            "specificationHash": forecast["specificationHash"],
            "reason": "known_outcome_review",
            "evidenceUrl": observation["url"],
            "idempotencyKey": key,
        });
        let body = body.as_object().cloned().unwrap_or_default();
        if let Err(error) =
            participation_holds::change(self.db, self.token, self.now_ms, forecast_id, &body, None).await
        {
            // Losing the race is not a failure: somebody else's hold is as good as ours. Any other
            // refusal is the caller's to see.
            let active = participation_holds::active(self.db, forecast_id).await.ok().flatten();
            if error.code != "participation_hold_changed" || active.is_none() {
                return Err(refused_from_hold(error));
            }
        }
        Ok(())
    }

    /// `review`: hand the page to the model. A watcher that cannot is not enabled.
    pub async fn review(
        &self,
        forecast: &Value,
        observation: &Value,
    ) -> Result<(Value, Vec<source_watch::Retained>), WatchError> {
        let Some(coordinator) = self.ai else {
            return Err(WatchError::AiUnavailable(
                "Retained source reader and configured reviewer are required".to_string(),
            ));
        };
        let reviewed =
            ai::early::review_source_observation(coordinator, self.reader, forecast, observation, self.now_ms)
                .await
                .map_err(refused_from_coordinator)?;
        Ok((review_record(&reviewed), retained(&reviewed.artifacts)))
    }

    /// `accept`: lock a question early on evidence that has been re-read from retained bytes.
    ///
    /// Four subsystems meet here under one revision CAS. The order is the safety property: the
    /// evidence is verified, then the pause is required, then eligibility classifies and adjusts,
    /// then the market refunds a proven late suffix, then the completion barrier has to pass — and
    /// only then does the lifecycle lock, with a guard that re-checks both the hold and the
    /// completion inside the same batch.
    pub async fn accept(&self, forecast_id: &str, review: &Value) -> Result<(), WatchError> {
        let trigger: EarlyResolutionTrigger =
            serde_json::from_value(review["trigger"].clone()).map_err(|_| WatchError::Refused {
                status: 409,
                code: "early_trigger_changed".to_string(),
                message: "The forecast changed while the event was reviewed.".to_string(),
            })?;
        let trigger_hash = trigger.trigger_hash().map_err(|_| WatchError::Refused {
            status: 409,
            code: "early_trigger_changed".to_string(),
            message: "The forecast changed while the event was reviewed.".to_string(),
        })?;
        let snapshot = mutate::load_snapshot(self.db, forecast_id)
            .await
            .map_err(|error| WatchError::Database(format!("{error:?}")))?;
        // A question another event already closed is not reopened: the same trigger is a retry and
        // answers nothing, a different one is a refusal.
        if let forecast_domain::lifecycle::Snapshot::V2(current) = &snapshot {
            if current.early_trigger.trigger_hash().ok().as_deref() != Some(trigger_hash.as_str()) {
                return Err(WatchError::Refused {
                    status: 409,
                    code: "early_trigger_changed".to_string(),
                    message: "A different reviewed event already closed this forecast.".to_string(),
                });
            }
            return Ok(());
        }
        let forecast = snapshot.base().clone();
        if forecast.state != "OPEN" || forecast.specification_hash != trigger.specification_hash {
            return Err(WatchError::Refused {
                status: 409,
                code: "early_trigger_changed".to_string(),
                message: "The forecast changed while the event was reviewed.".to_string(),
            });
        }
        trigger
            .validate_for(&forecast.specification)
            .map_err(|_| WatchError::Refused {
                status: 409,
                code: "early_trigger_changed".to_string(),
                message: "The forecast changed while the event was reviewed.".to_string(),
            })?;
        for evidence in &trigger.evidence {
            let retained = (self.reader)(evidence.content_sha256.clone()).await.ok().flatten();
            let matches = retained.is_some_and(|body| source_watch::hash_hex(&body) == evidence.content_sha256);
            if !matches {
                return Err(WatchError::Refused {
                    status: 503,
                    code: "early_evidence_unavailable".to_string(),
                    message: "The original official evidence could not be verified.".to_string(),
                });
            }
        }
        let held = participation_holds::active(self.db, forecast_id)
            .await
            .map_err(refused_from_hold)?;
        if held.is_none() {
            return Err(WatchError::Refused {
                status: 409,
                code: "early_trigger_changed".to_string(),
                message: "Participation must be paused before an observed-event upgrade.".to_string(),
            });
        }
        eligibility::apply(self.db, &trigger, self.now_ms, self.token)
            .await
            .map_err(refused_from_eligibility)?;
        let markets = point_markets::PointMarkets {
            db: self.db,
            clock: &|| self.now_ms,
            token: self.token,
            live_enabled: false,
        };
        markets
            .void_after_evidence(forecast_id, &trigger_hash, point_markets::MAX_BATCH)
            .await
            .map_err(refused_from_market)?;
        let completed = eligibility::finish(self.db, &trigger, self.now_ms)
            .await
            .map_err(refused_from_eligibility)?;
        if completed["status"] != json!("complete") {
            return Err(WatchError::Refused {
                status: 409,
                code: "early_eligibility_review".to_string(),
                message: "Receipt timing or point restoration is still being reviewed.".to_string(),
            });
        }
        let guard = (self.token)();
        let extra = vec![
            (
                "INSERT INTO mutation_guards(token,valid) SELECT ?, ( CASE WHEN EXISTS(SELECT 1 FROM active_participation_holds WHERE forecast_id=?) AND EXISTS(SELECT 1 FROM forecast_eligibility_completions WHERE decision_id=?) THEN 1 ELSE 0 END )".to_string(),
                vec![json!(guard), json!(forecast_id), json!(trigger_hash)],
            ),
            mutate::record_artifact(&trigger, "early-resolution-trigger", None, self.now_ms)
                .map_err(|error| WatchError::Database(format!("{error:?}")))?,
            // The extra guard is taken and released inside this batch, exactly as the mutation's
            // own is: a guard row that outlived its claim would be a second, stale authority.
            (
                "DELETE FROM mutation_guards WHERE token=?".to_string(),
                vec![json!(guard)],
            ),
        ];
        if self.now_ms < forecast.specification.close_at_ms {
            let mutation = mutate::Mutation {
                snapshot: &snapshot,
                payload: Payload::Lock {
                    schema_version: 2,
                    trigger: Some(trigger.clone()),
                },
                key: format!("early:{trigger_hash}"),
                now_ms: self.now_ms,
                extra,
                job_token: None,
            };
            mutate::mutate(self.db, mutation, self.now_ms, self.token)
                .await
                .map_err(refused_from_route)?;
            // The first community report that surfaced this exact retained evidence earns the
            // fixed reward. A retry finds the payout already made and pays nothing.
            let hashes: Vec<String> = trigger
                .evidence
                .iter()
                .map(|evidence| evidence.content_sha256.clone())
                .collect();
            points::reward_evidence_report(self.db, forecast_id, &hashes, self.now_ms, self.token)
                .await
                .map_err(refused_from_points)?;
        } else {
            // A delayed accounting correction cannot backdate an early lock: the ordinary
            // lifecycle still waits for its original deadline, and the key says so.
            let mutation = mutate::Mutation {
                snapshot: &snapshot,
                payload: Payload::Lock {
                    schema_version: 1,
                    trigger: None,
                },
                key: format!("eligibility-lock:{trigger_hash}"),
                now_ms: self.now_ms,
                extra,
                job_token: None,
            };
            mutate::mutate(self.db, mutation, self.now_ms, self.token)
                .await
                .map_err(refused_from_route)?;
        }
        Ok(())
    }

    /// `retry_eligibility`: resume retained compensation after crashes or later account credits.
    ///
    /// No model call and no invented publication time is involved, and an ambiguous receipt stays
    /// under review until it is separately adjudicated. The backoff is bounded so a permanently
    /// unfunded restoration cannot monopolize the first page and starve newer corrections.
    pub async fn retry_eligibility(&self, limit: i64) -> Result<Value, WatchError> {
        let limit = limit.clamp(1, 3);
        let rows = self
            .db
            .all(
                "SELECT d.id,d.body,j.attempts FROM forecast_eligibility_decisions d JOIN forecasts f ON f.id=d.forecast_id JOIN forecast_eligibility_retry j ON j.decision_id=d.id WHERE f.state='OPEN' AND j.next_attempt<=? AND NOT EXISTS (SELECT 1 FROM forecast_receipt_eligibility r WHERE r.decision_id=d.id AND r.status='review') ORDER BY j.next_attempt,d.created_at,d.id LIMIT ?",
                &[json!(self.now_ms), json!(limit)],
            )
            .await?;
        let mut completed = 0;
        for row in &rows {
            let decision_id = db::text(row, "id").unwrap_or("");
            let claimed = self
                .db
                .execute(
                    "UPDATE forecast_eligibility_retry SET attempts=attempts+1,next_attempt=? WHERE decision_id=? AND next_attempt<=? RETURNING decision_id",
                    &[json!(self.now_ms + 300_000), json!(decision_id), json!(self.now_ms)],
                )
                .await?;
            if claimed.is_empty() {
                continue;
            }
            let trigger: EarlyResolutionTrigger = serde_json::from_str(db::text(row, "body").unwrap_or(""))
                .map_err(|_| WatchError::Invalid("A retained eligibility decision did not parse"))?;
            if self
                .accept(&trigger.forecast_id, &json!({"trigger": trigger}))
                .await
                .is_ok()
            {
                completed += 1;
            } else {
                let attempts = db::int(row, "attempts").unwrap_or(0).clamp(0, 8);
                self.db
                    .execute(
                        "UPDATE forecast_eligibility_retry SET next_attempt=? WHERE decision_id=?",
                        &[
                            json!(self.now_ms + 60_000 * 2i64.pow(attempts as u32)),
                            json!(decision_id),
                        ],
                    )
                    .await?;
            }
        }
        Ok(json!({"considered": rows.len() as i64, "completed": completed}))
    }

    /// `dismiss`: release an observer's hold on a distinctly counter-reviewed unrelated article.
    ///
    /// Three things have to hold at once, and each of them is a reason *not* to release: the review
    /// has to be dismissible, the hold has to be one this watcher took rather than an operator's,
    /// and no other unresolved review may remain for the same specification.
    pub async fn dismiss(&self, forecast_id: &str, review: &Value) -> Result<(), WatchError> {
        if review["accepted"] != json!(false) || review["dismissible"] != json!(true) {
            return Ok(());
        }
        let forecast = self.load(forecast_id).await?;
        if forecast["state"] != json!("OPEN") {
            return Ok(());
        }
        let status = participation_holds::status(self.db, forecast_id)
            .await
            .map_err(refused_from_hold)?;
        let hold = &status["hold"];
        if hold.is_null() {
            return Ok(());
        }
        let hold_id = hold["holdId"].as_str().unwrap_or("");
        let event = self
            .db
            .first(
                "SELECT request_key FROM participation_hold_events WHERE id=?",
                &[json!(hold_id)],
            )
            .await?;
        // An operator's hold is never auto-released, and neither is one whose request key says it
        // came from somewhere other than this watcher.
        match event.as_ref().and_then(|row| db::text(row, "request_key")) {
            Some(key) if key.starts_with("source-watch:") => {}
            _ => return Ok(()),
        }
        let observation = &review["observation"];
        let specification_hash = forecast["specificationHash"].as_str().unwrap_or("");
        let review_id = source_watch::hash_hex(&format!(
            "{}{}{}",
            observation["contentHash"].as_str().unwrap_or(""),
            specification_hash,
            source_watch::POLICY
        ));
        let unresolved = self
            .db
            .first(
                "SELECT 1 FROM official_source_reviews WHERE forecast_id=? AND specification_hash=? AND id!=? AND json_extract(result,'$.dismissible') IS NOT 1 LIMIT 1",
                &[json!(forecast_id), json!(specification_hash), json!(review_id)],
            )
            .await?;
        if unresolved.is_some() {
            return Ok(());
        }
        let body = json!({
            "action": "release",
            "expectedRevision": status["revision"],
            "expectedHoldId": hold_id,
            "specificationHash": specification_hash,
            "reason": "known_outcome_review",
            "evidenceUrl": observation["url"],
            "idempotencyKey": format!("source-dismiss:{review_id}"),
        });
        let body = body.as_object().cloned().unwrap_or_default();
        participation_holds::change(self.db, self.token, self.now_ms, forecast_id, &body, Some(&review_id))
            .await
            .map_err(refused_from_hold)?;
        Ok(())
    }

    /// `check_creation`: the compile gate. Cheap candidates only — the caller must still hold.
    pub async fn check_creation(&self, specification: &ForecastSpecification) -> Result<(), WatchError> {
        let hints = families(specification);
        if !self.enabled || hints.is_empty() {
            return Ok(());
        }
        let known = source_watch::check_known(
            self.db,
            &json!({
                "officialSourceUrls": official_sources(specification),
                "families": hints,
            }),
        )
        .await?;
        if known.is_empty() {
            return Ok(());
        }
        let Some(coordinator) = self.ai else {
            return Err(WatchError::AiUnavailable(
                "Retained source review is required before publishing this question".to_string(),
            ));
        };
        let artifacts = ai::early::check_question_freshness(
            coordinator,
            self.reader,
            specification,
            &known[..known.len().min(3)],
            self.now_ms,
        )
        .await
        .map_err(refused_from_coordinator)?;
        if artifacts.is_empty() {
            return Ok(());
        }
        let rows = retained(&artifacts);
        let statements = source_watch::artifact_sql(&rows, self.now_ms)?;
        self.db.batch(&statements).await?;
        Ok(())
    }

    /// `run`: bootstrap, then one pass of the job loop.
    pub async fn run(&self, limit: i64) -> Result<Value, WatchError> {
        // The refusal comes before the token is taken, as the reference's does: a disabled watcher
        // consumes nothing at all, and a lease token minted for a run that will not happen is a
        // token the next caller cannot have.
        if !self.enabled {
            return Ok(json!({"enabled": false, "polled": 0, "reviewed": 0, "failed": 0}));
        }
        let Some(watch) = self.watcher() else {
            return Ok(json!({"enabled": false, "polled": 0, "reviewed": 0, "failed": 0}));
        };
        self.bootstrap().await?;
        let summary = source_watch::run(&watch, limit.clamp(1, 6)).await?;
        // `unchanged` is deliberately absent: the reference's summary is three counters, and a
        // caller that started seeing a fourth would be reading a number nothing defines.
        Ok(json!({
            "enabled": true,
            "polled": summary.polled,
            "reviewed": summary.reviewed,
            "failed": summary.failed,
        }))
    }

    /// `status`: the operator view.
    pub async fn status(&self) -> Result<Value, WatchError> {
        status(self.db, self.enabled).await.map_err(WatchError::Database)
    }
}

/// The four questions the watcher asks, answered by the object that holds the collaborators.
///
/// Each arm boxes a call to the inherent method of the same name; the reference's `ForecastAutomation`
/// is one object implementing all four callbacks, and splitting them here would be a different
/// design rather than a port of this one.
impl<'a> source_watch::ForecastSource for Automation<'a> {
    fn load_forecast<'b>(&'b self, forecast_id: &'b str) -> source_watch::BoxedFuture<'b, Result<Value, WatchError>> {
        Box::pin(async move { Automation::load(self, forecast_id).await })
    }
}

impl<'a> source_watch::Hold for Automation<'a> {
    fn hold<'b>(
        &'b self,
        forecast_id: &'b str,
        observation: &'b Value,
    ) -> source_watch::BoxedFuture<'b, Result<(), WatchError>> {
        Box::pin(async move { Automation::hold(self, forecast_id, observation).await })
    }
}

impl<'a> source_watch::Reviewer for Automation<'a> {
    fn review<'b>(
        &'b self,
        forecast: &'b Value,
        observation: &'b Value,
    ) -> source_watch::BoxedFuture<'b, Result<(Value, Vec<source_watch::Retained>), WatchError>> {
        Box::pin(async move { Automation::review(self, forecast, observation).await })
    }

    fn accept<'b>(
        &'b self,
        forecast_id: &'b str,
        review: &'b Value,
    ) -> source_watch::BoxedFuture<'b, Result<(), WatchError>> {
        Box::pin(async move { Automation::accept(self, forecast_id, review).await })
    }
}

impl<'a> source_watch::Dismisser for Automation<'a> {
    fn dismiss<'b>(
        &'b self,
        forecast_id: &'b str,
        review: &'b Value,
    ) -> source_watch::BoxedFuture<'b, Result<(), WatchError>> {
        Box::pin(async move { Automation::dismiss(self, forecast_id, review).await })
    }
}

/// The publishers `bootstrap` will map to a feed. Anything else is left alone.
const WATCHED_PUBLISHERS: [&str; 5] = [
    "www.apple.com",
    "apple.com",
    "news.microsoft.com",
    "blogs.microsoft.com",
    "www.microsoft.com",
];

fn official_sources(specification: &ForecastSpecification) -> Vec<&str> {
    specification
        .source_policy
        .primary_sources
        .iter()
        .filter(|source| source.is_official)
        .map(|source| source.url.as_str())
        .collect()
}

/// The review record, with exactly the keys the reference's branch produced.
///
/// The key *set* is part of the record: `dismissible` is absent rather than false for the branches
/// that never mention it, and the record is stored as text.
fn review_record(reviewed: &ai::early::ObservationReview) -> Value {
    let mut record = serde_json::Map::new();
    record.insert("accepted".to_string(), json!(reviewed.accepted));
    if let Some(dismissible) = reviewed.dismissible {
        record.insert("dismissible".to_string(), json!(dismissible));
    }
    record.insert("reason".to_string(), json!(reviewed.reason));
    if let Some(proof) = &reviewed.dismissal_proof {
        record.insert("dismissalProof".to_string(), proof.clone());
    }
    record.insert(
        "trigger".to_string(),
        match &reviewed.trigger {
            Some(trigger) => serde_json::to_value(trigger).unwrap_or(Value::Null),
            None => Value::Null,
        },
    );
    Value::Object(record)
}

/// The retained bytes a review produced, in the shape `artifact_sql` takes.
fn retained(artifacts: &[ai::coordinator::Artifact]) -> Vec<source_watch::Retained> {
    artifacts
        .iter()
        .map(|artifact| {
            (
                artifact.hash.clone(),
                artifact.kind.to_string(),
                artifact.body.clone(),
                "application/json".to_string(),
            )
        })
        .collect()
}

fn refused_from_coordinator(error: CoordinatorError) -> WatchError {
    match error {
        CoordinatorError::Unavailable { .. } => WatchError::AiUnavailable(
            "The retained source reader or the configured reviewer is unavailable".to_string(),
        ),
        CoordinatorError::Rejected { code, message, .. } => WatchError::AiRejected(format!("{code}: {message}")),
    }
}

fn refused_from_hold(error: participation_holds::HoldError) -> WatchError {
    WatchError::Refused {
        status: error.status,
        code: error.code.to_string(),
        message: error.message.to_string(),
    }
}

fn refused_from_eligibility(error: crate::eligibility::EligibilityError) -> WatchError {
    WatchError::Refused {
        status: error.status,
        code: error.code.to_string(),
        message: error.message.to_string(),
    }
}

fn refused_from_market(error: point_markets::MarketError) -> WatchError {
    WatchError::Refused {
        status: error.status,
        code: error.code.to_string(),
        message: error.message.to_string(),
    }
}

fn refused_from_points(error: points::PointsError) -> WatchError {
    WatchError::Refused {
        status: error.status,
        code: error.code.to_string(),
        message: error.message.to_string(),
    }
}

fn refused_from_route(error: crate::routes::RouteError) -> WatchError {
    match error {
        crate::routes::RouteError::Failed(status, code, message) => WatchError::Refused {
            status,
            code: code.to_string(),
            message: message.to_string(),
        },
        crate::routes::RouteError::NotFound(code, message) => WatchError::Refused {
            status: 404,
            code: code.to_string(),
            message: message.to_string(),
        },
        other => WatchError::Database(format!("{other:?}")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::Sqlite;

    fn block<F: std::future::Future>(future: F) -> F::Output {
        futures_lite::future::block_on(future)
    }

    fn golden() -> Value {
        let path =
            std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/automation-golden.json");
        serde_json::from_str(&std::fs::read_to_string(&path).expect("automation golden")).expect("json")
    }

    /// The vector's own question, as the specification the hints are read from.
    ///
    /// The record comes from the vector, so the port parses the reference's own specification
    /// rather than hand-building one: the hints read two fields, and a fixture that filled in the
    /// other nine by guesswork would be testing a different record.
    fn specification(document: &Value, question: &str) -> ForecastSpecification {
        let mut value = document["specification"].clone();
        value["canonical_question"] = json!(question);
        value["share_title"] = json!(question);
        serde_json::from_value(value).expect("the reference's own specification")
    }

    #[test]
    fn the_reference_family_hints_are_reproduced_case_for_case() {
        let document = golden();
        for entry in document["cases"].as_array().expect("cases") {
            if entry["call"] != json!("families") {
                continue;
            }
            let question = entry["question"].as_str().unwrap();
            assert_eq!(
                json!(families(&specification(&document, question))),
                entry["result"],
                "{question}: a different hint list"
            );
        }
    }

    #[test]
    fn the_reference_publisher_feeds_are_reproduced_case_for_case() {
        let document = golden();
        for entry in document["cases"].as_array().expect("cases") {
            if entry["call"] != json!("publisher_feed_url") {
                continue;
            }
            let url = entry["url"].as_str().unwrap();
            assert_eq!(publisher_feed_url(url), entry["result"].as_str().unwrap(), "{url}");
        }
    }

    #[test]
    fn the_reference_status_view_is_reproduced() {
        let document = golden();
        let db = Sqlite::from_migrations();
        // The fixture the vector counted, in the order its foreign keys require.
        db.run(
            "INSERT INTO users(id,display_name,handle,recovery_hash,created_at) VALUES('u','U','u','r',1)",
            &[],
        )
        .expect("user");
        db.run(
            concat!(
                "INSERT INTO forecasts(id,creator_id,draft_id,snapshot,revision,state,category,title,question,",
                "normalized_question,specification_hash,open_at,close_at,created_at,updated_at,mutation_key) ",
                "VALUES('f','u','d','{}',1,'OPEN','CRYPTO','t','q','q',?,0,?,1,1,'k')",
            ),
            &[json!("a".repeat(64)), json!(1_800_000_000_000i64)],
        )
        .expect("forecast");
        for (index, enabled) in [(0, 1), (1, 1), (2, 0), (3, 1), (4, 1)] {
            db.run(
                "INSERT INTO official_watch_sources(id,url,kind,interval_ms,next_poll,enabled,failure_count) \
                 VALUES(?,?,'index',60000,0,?,0)",
                &[
                    json!(format!("source-{index}")),
                    json!(format!("https://example.test/{index}")),
                    json!(enabled),
                ],
            )
            .expect("source");
        }
        db.run(
            "INSERT INTO artifacts(hash,kind,body,media_type,created_at) VALUES(?,'source','{}','application/json',?)",
            &[json!("c".repeat(64)), json!(1_800_000_000_000i64)],
        )
        .expect("artifact");
        db.run(
            "INSERT INTO official_source_observations(id,source_id,url,content_hash,artifact_hash,body,observed_at) \
             VALUES('observation','source-0','https://example.test/0',?,?,'{}',?)",
            &[
                json!("b".repeat(64)),
                json!("c".repeat(64)),
                json!(1_800_000_000_000i64),
            ],
        )
        .expect("observation");
        for (index, state) in ["pending", "reviewed", "reviewed", "complete", "exhausted"]
            .iter()
            .enumerate()
        {
            db.run(
                "INSERT INTO official_source_reviews(id,observation_id,forecast_id,specification_hash,content_hash,\
                 policy,state,attempts,next_attempt) VALUES(?,?,?,?,?,'official-source-watch-v1',?,0,0)",
                &[
                    json!(format!("review-{index}")),
                    json!("observation"),
                    json!("f"),
                    json!("a".repeat(64)),
                    json!(format!("{index:064}")),
                    json!(state),
                ],
            )
            .expect("review");
        }
        // The flag alone is not enough: `enabled` is `enabled && collector.is_some()`, and the
        // vector pins both sides of that by construction.
        let expected = document["statuses"]["collector"].clone();
        assert_eq!(block(status(&db, true)).expect("status"), expected);
        let mut disabled = expected.clone();
        disabled["enabled"] = json!(false);
        assert_eq!(block(status(&db, false)).expect("status"), disabled);
    }
}

/// The orchestration half, replayed against the reference's own recorded state.
///
/// Everything below runs *through* this layer, so `source_watch`, `eligibility`, `point_markets`
/// and `points` are exercised as a composition rather than one at a time: a defect in any of them
/// shows up as a differing row. Each case starts from the vector's own `initial` snapshot —
/// reconstructing a fixture by hand is how a golden ends up testing the reconstruction.
#[cfg(test)]
mod orchestration_tests {
    use super::*;
    use crate::db::Sqlite;
    use crate::source_watch::error_kind;

    fn block<F: std::future::Future>(future: F) -> F::Output {
        futures_lite::future::block_on(future)
    }

    fn golden() -> Value {
        let path =
            std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/automation-run-golden.json");
        serde_json::from_str(&std::fs::read_to_string(&path).expect("automation run golden")).expect("json")
    }

    fn entry<'a>(document: &'a Value, name: &str) -> &'a Value {
        document["cases"]
            .as_array()
            .expect("cases")
            .iter()
            .find(|case| case["call"] == json!(name))
            .unwrap_or_else(|| panic!("{name} is not in the vector"))
    }

    /// Restore the reference's own state.
    ///
    /// Foreign keys are off because this restores an already-consistent database rather than
    /// replaying a sequence of user actions, and the vector's tables are in an order that is
    /// alphabetical rather than a dependency order.
    fn restore(db: &Sqlite, initial: &Value) {
        db.run("PRAGMA foreign_keys=OFF", &[]).expect("foreign keys off");
        // The schema's own guards are taken down for the restore and put straight back. They are
        // *transitions* — a policy may not be rewritten, a ledger entry has to follow from the
        // account it applies to — and the vector holds the state those transitions already
        // produced. Leaving them up would mean replaying the reference's history rather than its
        // result; dropping them for good would mean the call under test runs unguarded, which is
        // the half this layer actually depends on.
        let guards: Vec<(String, String)> = db
            .run("SELECT name,sql FROM sqlite_master WHERE type='trigger'", &[])
            .expect("triggers")
            .0
            .iter()
            .filter_map(|row| Some((db::text(row, "name")?.to_string(), db::text(row, "sql")?.to_string())))
            .collect();
        for (name, _) in &guards {
            db.run(&format!("DROP TRIGGER {name}"), &[]).expect("drop trigger");
        }
        for table in initial.as_object().expect("tables").keys() {
            db.run(&format!("DELETE FROM {table}"), &[])
                .unwrap_or_else(|error| panic!("{table}: {error}"));
        }
        for (table, rows) in initial.as_object().expect("tables") {
            for row in rows.as_array().expect("rows") {
                let row = row.as_object().expect("row");
                let columns: Vec<&str> = row.keys().map(String::as_str).collect();
                let placeholders = vec!["?"; columns.len()].join(",");
                let sql = format!("INSERT INTO {table}({}) VALUES({placeholders})", columns.join(","));
                let params: Vec<Value> = columns.iter().map(|name| row[*name].clone()).collect();
                db.run(&sql, &params).unwrap_or_else(|error| panic!("{table}: {error}"));
            }
        }
        for (_, sql) in &guards {
            db.run(sql, &[])
                .unwrap_or_else(|error| panic!("recreating a trigger: {error}"));
        }
    }

    /// Every table, in rowid order, exactly as the generator dumped it.
    fn dump(db: &Sqlite) -> Value {
        let names: Vec<String> = db
            .run(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name",
                &[],
            )
            .expect("tables")
            .0
            .iter()
            .filter_map(|row| db::text(row, "name").map(str::to_string))
            .collect();
        let mut out = serde_json::Map::new();
        for name in names {
            let (rows, _) = db
                .run(&format!("SELECT * FROM {name} ORDER BY rowid"), &[])
                .expect("rows");
            out.insert(name, Value::Array(rows.into_iter().map(Value::Object).collect()));
        }
        Value::Object(out)
    }

    /// A reader over the retained bytes.
    ///
    /// The leak is deliberate: `ArtifactReader`'s future is `'static`, and this is the only way to
    /// hand it a database that lives as long. A test database is exactly the thing that *should*
    /// outlive the test.
    fn static_database(initial: &Value) -> &'static Sqlite {
        let db: &'static Sqlite = Box::leak(Box::new(Sqlite::from_migrations()));
        restore(db, initial);
        db
    }

    fn artifact_reader(db: &'static Sqlite) -> ArtifactReader {
        Box::new(move |digest| {
            Box::pin(async move { crate::scheduler::read_artifact(db, &digest).await.map_err(|_| ()) })
        })
    }

    fn fetcher(script: &Value) -> source_watch::Fetcher {
        let responses: Vec<(String, Value)> = script.as_object().cloned().unwrap_or_default().into_iter().collect();
        Box::new(move |url, _headers| {
            let found = responses.iter().find(|(scripted, _)| *scripted == url).cloned();
            Box::pin(async move {
                found
                    .map(|(_, response)| crate::sources::TextResponse {
                        status: response["status"].as_u64().unwrap_or(200) as u16,
                        headers: vec![(
                            "content-type".to_string(),
                            response["contentType"].as_str().unwrap_or("text/html").to_string(),
                        )],
                        body: response["body"].as_str().unwrap_or("").to_string(),
                    })
                    .ok_or(())
            })
        })
    }

    /// The reference's own test transport, over the responses the vector recorded.
    ///
    /// The envelopes are the three the Python fixture produces, reproduced so the port is compared
    /// against the same conversation rather than a similar one.
    fn scripted_coordinator(responses: Vec<Value>) -> Coordinator {
        let answers = std::sync::Arc::new(std::sync::Mutex::new(responses));
        Coordinator {
            providers: vec![
                crate::ai::coordinator::ProviderConfig {
                    provider: "gemini".to_string(),
                    model: "test-model".to_string(),
                    model_version: Some("tested-revision".to_string()),
                    api_key: "test-key".to_string(),
                },
                crate::ai::coordinator::ProviderConfig {
                    provider: "openai".to_string(),
                    model: "test-model".to_string(),
                    model_version: Some("tested-revision".to_string()),
                    api_key: "test-key".to_string(),
                },
            ],
            fetch: Box::new(move |url, _headers, _body| {
                let answer = answers.lock().expect("answers").pop();
                Box::pin(async move {
                    let answer = answer.ok_or(())?;
                    if url.contains("generativelanguage") {
                        Ok(json!({
                            "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": answer.to_string()}]}}],
                            "modelVersion": "gemini-tested-revision",
                        }))
                    } else if url.starts_with("workers-ai:") {
                        Ok(json!({"response": answer}))
                    } else {
                        Ok(json!({"status": "completed", "model": "openai-tested-revision",
                                  "output": [{"type": "message",
                                              "content": [{"type": "output_text", "text": answer.to_string()}]}]}))
                    }
                })
            }),
        }
    }

    /// A coordinator whose transport refuses: the cases that need one never reach a provider.
    fn unreachable_coordinator() -> Coordinator {
        Coordinator {
            providers: vec![
                crate::ai::coordinator::ProviderConfig {
                    provider: "gemini".to_string(),
                    model: "test-model".to_string(),
                    model_version: Some("tested-revision".to_string()),
                    api_key: "test-key".to_string(),
                },
                crate::ai::coordinator::ProviderConfig {
                    provider: "openai".to_string(),
                    model: "test-model".to_string(),
                    model_version: Some("tested-revision".to_string()),
                    api_key: "test-key".to_string(),
                },
            ],
            fetch: Box::new(|_, _, _| Box::pin(async { Err(()) })),
        }
    }

    /// The reference's token stream, replayed in order.
    ///
    /// A token is opaque, so a replay that invented its own would fail on every row a token names.
    /// Yielding the recorded sequence instead makes the *count* and the *order* part of what is
    /// checked: a port that took one token where the reference took two is caught here.
    struct Tokens {
        produced: Vec<String>,
        taken: std::cell::Cell<usize>,
    }

    impl Tokens {
        fn new(produced: Vec<String>) -> Self {
            Self {
                produced,
                taken: std::cell::Cell::new(0),
            }
        }

        fn next(&self) -> String {
            let index = self.taken.get();
            self.taken.set(index + 1);
            self.produced.get(index).cloned().unwrap_or_default()
        }
    }

    fn refusal(error: &WatchError) -> Value {
        match error {
            WatchError::Refused { status, code, message } => {
                json!({"status": status, "code": code, "message": message})
            }
            other => json!({"code": error_kind(other), "message": other.message()}),
        }
    }

    /// The closed set of cases this replay drives. A vector that grows a case it does not know
    /// about fails here rather than being silently skipped.
    const REPLAYED: [&str; 24] = [
        "load",
        "load:missing",
        "bootstrap",
        "bootstrap:held",
        "bootstrap:disabled",
        "hold",
        "hold:not-open",
        "accept",
        "accept:different-trigger",
        "accept:no-hold",
        "accept:evidence-missing",
        "accept:late",
        "accept:eligibility-review",
        "retry_eligibility:resumed",
        "retry_eligibility:empty",
        "dismiss:released",
        "dismiss:operator-hold",
        "dismiss:unresolved",
        "dismiss:polling",
        "check_creation:unknown",
        "check_creation:known",
        "status",
        "run:disabled",
        "run",
    ];

    /// Cases the replay does not drive, each for a stated reason rather than by omission.
    const NOT_REPLAYED: [(&str, &str); 2] = [
        (
            "review:accepted",
            "needs the reference's scripted provider transport, held by ai-early-golden",
        ),
        (
            "review:uncertain",
            "needs the reference's scripted provider transport, held by ai-early-golden",
        ),
    ];

    #[test]
    fn the_vector_has_no_case_this_replay_silently_skips() {
        let document = golden();
        for case in document["cases"].as_array().expect("cases") {
            let name = case["call"].as_str().expect("name");
            assert!(
                REPLAYED.contains(&name) || NOT_REPLAYED.iter().any(|(known, _)| *known == name),
                "{name} is in the vector and the replay neither drives it nor says why not"
            );
        }
    }

    fn outcome(db: &'static Sqlite, case: &Value) -> (Option<Value>, Option<Value>) {
        let recorded: Vec<String> = case["tokens"]
            .as_array()
            .map(|values| {
                values
                    .iter()
                    .filter_map(|value| value.as_str().map(str::to_string))
                    .collect()
            })
            .unwrap_or_default();
        let tokens = Tokens::new(recorded);
        let reader = artifact_reader(db);
        let input = &case["input"];
        // A case that scripted model output gets it served in the reference's envelopes; one that
        // did not gets a transport that refuses, which is where such a case should never reach.
        let coordinator = match input.get("responses").and_then(Value::as_array) {
            Some(responses) => scripted_coordinator(responses.clone()),
            None => unreachable_coordinator(),
        };
        let now_ms = case["now"].as_i64().unwrap_or(0);
        let enabled = !matches!(case["call"].as_str(), Some("bootstrap:disabled") | Some("run:disabled"));
        // The collector always exists; whether it is *enabled* is the flag's business. A case
        // without a script gets one that refuses every fetch, which is what the reference's own
        // `no_network` collector does.
        let script = input.get("fetcher").cloned().unwrap_or(Value::Null);
        let fetch = Some(fetcher(&script));
        // The fetcher owns its script, so it has to outlive the automation that borrows it.
        // Both outlive the automation that borrows them, which is why they are bound here rather
        // than constructed at the call.
        let fetch = fetch.as_ref();
        let token = || tokens.next();
        let automation = Automation::new(db, fetch, Some(&coordinator), &reader, now_ms, &token, enabled);
        let name = case["call"].as_str().expect("name");
        let forecast_id = input["forecastId"].as_str().unwrap_or("");
        let result = match name {
            "load" | "load:missing" => block(automation.load(forecast_id)).map(Some),
            "bootstrap" | "bootstrap:held" | "bootstrap:disabled" => {
                block(automation.bootstrap()).map(|count| Some(json!(count)))
            }
            "hold" | "hold:not-open" => block(automation.hold(forecast_id, &input["observation"])).map(|()| None),
            "accept"
            | "accept:different-trigger"
            | "accept:no-hold"
            | "accept:evidence-missing"
            | "accept:late"
            | "accept:eligibility-review" => {
                let review = json!({"trigger": input["trigger"]});
                block(automation.accept(forecast_id, &review)).map(|()| None)
            }
            "retry_eligibility:resumed" | "retry_eligibility:empty" => {
                let limit = input["limit"].as_i64().unwrap_or(3);
                block(automation.retry_eligibility(limit)).map(Some)
            }
            "dismiss:released" | "dismiss:operator-hold" => {
                block(automation.dismiss(forecast_id, &input["result"])).map(|()| None)
            }
            "dismiss:unresolved" => match block(automation.dismiss(forecast_id, &input["first"])) {
                Err(error) => Err(error),
                Ok(()) => {
                    // The second review only becomes dismissible once the database says it is.
                    run_sql(
                        db,
                        "UPDATE official_source_reviews SET state='reviewed',result=? WHERE id=?",
                        &[input["secondStored"].clone(), input["secondReviewId"].clone()],
                    );
                    block(automation.dismiss(forecast_id, &input["second"])).map(|()| None)
                }
            },
            "dismiss:polling" => {
                run_sql(
                    db,
                    "UPDATE official_watch_sources SET lease_until=?",
                    &[json!(now_ms + 10_000)],
                );
                assert!(
                    block(automation.dismiss(forecast_id, &input["result"])).is_err(),
                    "a poll in flight is not evidence, and the release must be refused"
                );
                run_sql(db, "UPDATE official_watch_sources SET lease_until=0", &[]);
                block(automation.dismiss(forecast_id, &input["result"])).map(|()| None)
            }
            "check_creation:unknown" | "check_creation:known" => {
                let snapshot = block(crate::mutate::load_snapshot(db, forecast_id)).expect("snapshot");
                block(automation.check_creation(&snapshot.base().specification)).map(|()| None)
            }
            "status" => block(automation.status()).map(Some),
            "run:disabled" => {
                let limit = input["limit"].as_i64().unwrap_or(2);
                block(automation.run(limit)).map(Some)
            }
            "run" => {
                // Twice, because the article is *discovered* by the first pass: the feed index
                // carries no announcement of its own.
                let limit = input["limit"].as_i64().unwrap_or(2);
                block(automation.run(limit)).and_then(|first| {
                    block(automation.run(limit)).map(|second| Some(json!({"summary": [first, second]})))
                })
            }
            other => panic!("{other} has no replay"),
        };
        let observed = match result {
            Ok(value) => (value, None),
            Err(error) => (None, Some(refusal(&error))),
        };
        // The whole recorded stream has to be used. Both counts are reported because "took too
        // few" and "took too many" are different defects: the first means a step was skipped, the
        // second that one was added.
        assert_eq!(
            tokens.taken.get(),
            tokens.produced.len(),
            "{name}: the reference took {} tokens and the replay took {} (result {observed:?})",
            tokens.produced.len(),
            tokens.taken.get()
        );
        (observed.0, observed.1)
    }

    fn run_sql(db: &Sqlite, sql: &str, params: &[Value]) {
        db.run(sql, params).expect("statement");
    }

    /// Which tables differ, so a failure names the table instead of printing two databases.
    fn differing(expected: &Value, actual: &Value) -> Vec<String> {
        let mut tables: Vec<&String> = expected
            .as_object()
            .expect("tables")
            .keys()
            .chain(actual.as_object().expect("tables").keys())
            .collect();
        tables.sort();
        tables.dedup();
        let mut out = Vec::new();
        for table in tables {
            let left = expected.get(table).cloned().unwrap_or(Value::Null);
            let right = actual.get(table).cloned().unwrap_or(Value::Null);
            if left != right {
                out.push(format!(
                    "{table}: reference {}, replay {}",
                    rows_of(&left),
                    rows_of(&right)
                ));
                let (a, b) = (
                    left.as_array().cloned().unwrap_or_default(),
                    right.as_array().cloned().unwrap_or_default(),
                );
                for (index, row) in a.iter().enumerate() {
                    let other = b.get(index).cloned().unwrap_or(Value::Null);
                    if *row != other {
                        out.push(format!("    row {index}: reference {row}"));
                        out.push(format!("    row {index}: replay    {other}"));
                    }
                }
            }
        }
        out
    }

    fn rows_of(value: &Value) -> usize {
        value.as_array().map(Vec::len).unwrap_or(0)
    }

    #[test]
    fn the_reference_orchestration_half_is_reproduced_case_for_case() {
        let document = golden();
        for name in REPLAYED {
            let case = entry(&document, name);
            let db = static_database(&case["initial"]);
            let (result, error) = outcome(db, case);

            // The refusal, first: a case that should be refused and is not is a different bug from
            // one that refuses for a different reason, and the code says which.
            match case.get("error") {
                Some(expected) => assert_eq!(
                    error.as_ref().expect("a refusal was expected").get("code"),
                    expected.get("code"),
                    "{name}: refused for a different reason"
                ),
                None => assert!(
                    error.is_none(),
                    "{name}: refused when the reference succeeded: {error:?}"
                ),
            }
            // A refusal has no result to compare: the reference raised before returning one.
            if case.get("error").is_some() {
                assert_database(name, &case["rows"], &dump(db));
                continue;
            }
            for key in case["compare"].as_array().expect("compare") {
                let key = key.as_str().expect("key");
                assert_eq!(
                    result.as_ref().and_then(|value| value.get(key)),
                    case["result"].get(key),
                    "{name}: {key} differs"
                );
            }
            if case["compare"]
                .as_array()
                .expect("compare")
                .iter()
                .any(|key| key == "result")
            {
                assert_eq!(result, Some(case["result"].clone()), "{name}: the whole result differs");
            }
            assert_database(name, &case["rows"], &dump(db));
        }
    }

    fn assert_database(name: &str, expected: &Value, actual: &Value) {
        let tables = differing(expected, actual);
        assert!(
            tables.is_empty(),
            "{name}: a different database\n  {}",
            tables.join("\n  ")
        );
    }
}
