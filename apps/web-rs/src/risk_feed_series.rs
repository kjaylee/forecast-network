//! Recurring canonical episodes: publish and bind the next question before its start.
//!
//! Series are operator-approved templates. Each tick creates at most one due episode per series
//! through the ordinary canonical seed path and the ordinary typed-target approval — nothing here
//! bypasses those gates, and a failed attempt is logged and retried on the next tick rather than
//! being retried inside the tick.

use crate::ai::window::measurement_window;
use crate::db::{self, Database};
use crate::risk_feed_v2::{actor, approve_binding_v2, feed, storage_unavailable, FeedError};

/// `seed`: `Application.compile_forecast` then `publish_forecast`, or whatever the operator wires
/// in. Its failure travels with the error the seed raised, because the reference records *that*
/// type and message in the series log.
/// A future that borrows for as long as its caller does.
pub type BorrowedFuture<'a, T> = std::pin::Pin<Box<dyn std::future::Future<Output = T> + 'a>>;

/// `app.seed`, as the series tick takes it.
///
/// A trait rather than a boxed closure, for the reason the other seams are: `Fn`'s `Output` is an
/// associated type and a trait object over `Fn` is invariant in it, so a closure that publishes —
/// and therefore reads the request's own database — cannot be passed where a `'static` box is
/// wanted. A method can name its own lifetime.
pub trait SeedCallback {
    fn seed<'a>(&'a self, question: String) -> BorrowedFuture<'a, Result<Value, AttemptError>>;
}
use forecast_domain::risk_feed::{RiskFeedBindingV2, RiskFeedSeriesV2};
use forecast_domain::{canonical_bytes, content_hash, require, Record};
use serde::Serialize;
use serde_json::{json, Value};

/// `RETRY_MS`.
pub const RETRY_MS: i64 = 600_000;

/// `CATEGORY_BY_CHANNEL`. A channel with no entry here has no publication category, and the
/// reference refuses rather than guessing one.
pub fn category_for(channel: &str) -> Option<&'static str> {
    match channel {
        "depegRisk1d" | "depegRisk7d" | "depegRisk30d" | "btcCrashRisk" | "ethCrashRisk" | "solCrashRisk" => {
            Some("CRYPTO")
        }
        _ => None,
    }
}

/// `spell`: `%Y-%m-%dT%H:%M:%SZ`, which is `datetime.fromtimestamp(ms // 1000, timezone.utc)`.
pub fn spell(ms: i64) -> String {
    let seconds = ms.div_euclid(1000);
    let (year, month, day) = crate::ai::compiler_time::civil_parts(ms);
    let day_seconds = seconds.rem_euclid(86_400);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}Z",
        day_seconds / 3600,
        (day_seconds % 3600) / 60,
        day_seconds % 60
    )
}

fn dumps<T: serde::Serialize>(value: &T) -> String {
    String::from_utf8(canonical_bytes(value).unwrap_or_default()).unwrap_or_default()
}

/// `json.dumps(outcome, sort_keys=True)` — with Python's **default separators**, `", "` and `": "`.
///
/// Not a detail: the tick's outcome is stored as text and read back by an operator, so a port that
/// wrote the compact form would store a different `detail` for the same tick. Keys are sorted
/// because `serde_json::Value` is a `BTreeMap`, which is what `sort_keys=True` asks for.
pub fn detail_json(value: &Value) -> String {
    struct Spaced;
    impl serde_json::ser::Formatter for Spaced {
        fn begin_array_value<W: ?Sized + std::io::Write>(
            &mut self,
            writer: &mut W,
            first: bool,
        ) -> std::io::Result<()> {
            if first {
                Ok(())
            } else {
                writer.write_all(b", ")
            }
        }
        fn begin_object_key<W: ?Sized + std::io::Write>(&mut self, writer: &mut W, first: bool) -> std::io::Result<()> {
            if first {
                Ok(())
            } else {
                writer.write_all(b", ")
            }
        }
        fn begin_object_value<W: ?Sized + std::io::Write>(&mut self, writer: &mut W) -> std::io::Result<()> {
            writer.write_all(b": ")
        }
    }
    let mut bytes = Vec::new();
    let mut serializer = serde_json::Serializer::with_formatter(&mut bytes, Spaced);
    value.serialize(&mut serializer).unwrap_or_default();
    String::from_utf8(bytes).unwrap_or_default()
}

/// `configure_series`. A series is accepted only if the definition and profile it cites are
/// admitted *under the same feed* and agree with it field by field.
pub async fn configure_series(
    db: &dyn Database,
    series: &RiskFeedSeriesV2,
    enabled: bool,
    configured_by: &str,
    now_ms: i64,
) -> Result<String, FeedError> {
    series.validate().map_err(FeedError::from)?;
    actor(configured_by)?;
    feed(&series.feed_id)?;
    let definition = db
        .first(
            "SELECT channel,definition_json FROM risk_feed_definitions_v2 WHERE definition_hash=? AND feed_id=?",
            &[json!(series.definition_hash), json!(series.feed_id)],
        )
        .await
        .map_err(|_| FeedError("feed storage unavailable".to_string()))?;
    let profile = db
        .first(
            "SELECT profile_json FROM risk_feed_profiles_v2 WHERE profile_hash=? AND feed_id=?",
            &[json!(series.mapping_profile_hash), json!(series.feed_id)],
        )
        .await
        .map_err(|_| FeedError("feed storage unavailable".to_string()))?;
    let (Some(definition), Some(profile)) = (definition, profile) else {
        return Err(FeedError("series references unadmitted records".to_string()));
    };
    let definition_body: Value =
        serde_json::from_str(db::text(&definition, "definition_json").unwrap_or("")).unwrap_or(Value::Null);
    let profile_body: Value =
        serde_json::from_str(db::text(&profile, "profile_json").unwrap_or("")).unwrap_or(Value::Null);
    require(
        db::text(&definition, "channel") == Some(series.channel.as_str())
            && definition_body["asset"] == json!(series.asset)
            && profile_body["mapping_kind"] == json!(series.mapping_kind),
        "series disagrees with its admitted definition or profile",
    )
    .map_err(FeedError::from)?;
    require(
        category_for(&series.channel).is_some(),
        "series channel has no publication category",
    )
    .map_err(FeedError::from)?;
    let digest = content_hash(series).map_err(FeedError::from)?;
    db.execute(
        concat!(
            "INSERT INTO risk_feed_series_v2(series_id,feed_id,series_hash,series_json,enabled,configured_by,updated_at) ",
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(series_id) DO UPDATE SET feed_id=excluded.feed_id,series_hash=excluded.",
            "series_hash,series_json=excluded.series_json,enabled=excluded.enabled,configured_by=excluded.configured_by,",
            "updated_at=excluded.updated_at",
        ),
        &[
            json!(series.series_id),
            json!(series.feed_id),
            json!(digest),
            json!(dumps(series)),
            json!(i64::from(enabled)),
            json!(configured_by),
            json!(now_ms),
        ],
    )
    .await
    .map_err(|_| FeedError("feed storage unavailable".to_string()))?;
    Ok(digest)
}

/// `latest_episode_start`: the newest episode that still stands, revocation included.
pub async fn latest_episode_start(db: &dyn Database, series_id: &str) -> Result<Option<i64>, FeedError> {
    let row = db
        .first(
            concat!(
                "SELECT MAX(json_extract(binding_json,'$.target_start_ms')) AS start FROM risk_feed_bindings_v2 b ",
                "WHERE json_extract(binding_json,'$.series_id')=? ",
                "AND NOT EXISTS(SELECT 1 FROM risk_feed_binding_revocations_v2 r WHERE r.binding_id=b.binding_id)",
            ),
            &[json!(series_id)],
        )
        .await
        .map_err(|_| FeedError("feed storage unavailable".to_string()))?;
    Ok(row.as_ref().and_then(|row| db::int(row, "start")))
}

/// `next_episode_start`: the next cadence-aligned start after the latest episode, else the next
/// boundary strictly after now.
pub fn next_episode_start(series: &RiskFeedSeriesV2, latest_start_ms: Option<i64>, now_ms: i64) -> i64 {
    if let Some(latest) = latest_start_ms {
        return latest + series.cadence_ms;
    }
    // `-((-now) // cadence) * cadence`: the next cadence boundary at or after now, which is
    // `ceil(now / cadence) * cadence`. Written the way Python writes it, because the sign of the
    // floor division is the whole of the rule.
    let boundary = -((-now_ms).div_euclid(series.cadence_ms) * series.cadence_ms);
    if boundary == now_ms {
        boundary + series.cadence_ms
    } else {
        boundary
    }
}

/// `episode_question`.
pub fn episode_question(series: &RiskFeedSeriesV2, start_ms: i64) -> Result<String, FeedError> {
    let end_ms = start_ms + series.window_ms;
    // `str.format` over the template's four named fields. The reference's template is operator
    // input, so an unknown field is a refusal rather than a literal brace.
    let mut question = series.question_template.clone();
    for (name, value) in [
        ("start", spell(start_ms)),
        ("end", spell(end_ms)),
        ("since", (start_ms / 1000).to_string()),
        ("candles", (series.window_ms / 300_000).to_string()),
    ] {
        question = question.replace(&format!("{{{name}}}"), &value);
    }
    require(
        question.chars().count() <= 1000,
        "episode question exceeds the compiler bound",
    )
    .map_err(FeedError::from)?;
    Ok(question)
}

/// `episode_binding`. The typed target comes from the *published* question's own measurement
/// interval, so the two cannot disagree by construction — and the two hashes commit to both the
/// series and the question it produced.
#[allow(clippy::too_many_arguments)]
pub fn episode_binding(
    series: &RiskFeedSeriesV2,
    card: &Value,
    start_ms: i64,
    profile_id: &str,
    profile_version: &str,
) -> Result<RiskFeedBindingV2, FeedError> {
    let question = card["question"].as_str().unwrap_or("");
    let window = measurement_window(question).ok().flatten();
    let Some(window) = window else {
        return Err(FeedError(
            "published episode lacks its measurement interval".to_string(),
        ));
    };
    let horizon = if series.mapping_kind == "containing_upper_estimate" {
        series.policy_horizon_ms
    } else {
        0
    };
    let category = category_for(&series.channel)
        .ok_or_else(|| FeedError("series channel has no publication category".to_string()))?;
    let specification_hash = card["specificationHash"].as_str().unwrap_or("");
    let forecast_id = card["id"].as_str().unwrap_or("");
    let value = RiskFeedBindingV2 {
        schema_version: 1,
        binding_id: format!("{}-{}", series.series_id, spell(start_ms)),
        version: "canonical-risk-binding-v2".to_string(),
        forecast_id: forecast_id.to_string(),
        specification_hash: specification_hash.to_string(),
        channel: series.channel.clone(),
        asset: series.asset.clone(),
        category: category.to_string(),
        series_id: series.series_id.clone(),
        episode_id: spell(start_ms),
        target_start_ms: start_ms,
        target_end_ms: start_ms + series.window_ms,
        interval: "[start,end)".to_string(),
        policy_horizon_ms: series.policy_horizon_ms,
        definition_hash: series.definition_hash.clone(),
        mapping_profile_id: profile_id.to_string(),
        mapping_profile_version: profile_version.to_string(),
        mapping_profile_hash: series.mapping_profile_hash.clone(),
        mapping_kind: series.mapping_kind.clone(),
        question_event_definition_hash: content_hash(&json!({
            "specification_hash": specification_hash,
            "window": window["canonical_expression"],
        }))
        .map_err(FeedError::from)?,
        approval_artifact_hash: content_hash(&json!({
            "series": content_hash(series).map_err(FeedError::from)?,
            "forecast_id": forecast_id,
        }))
        .map_err(FeedError::from)?,
        authorization_valid_from_ms: card["openAt"].as_i64().unwrap_or(0),
        authorization_valid_until_ms: start_ms + series.window_ms,
        operational_valid_from_ms: start_ms,
        operational_valid_until_ms: start_ms + series.window_ms - horizon,
    };
    value.validate().map_err(FeedError::from)?;
    Ok(value)
}

/// `create_due_episodes`: every episode whose lead window has opened, one attempt per series per
/// tick.
///
/// A failure is recorded and retried on the *next* tick rather than inside this one, because what
/// fails here costs AI budget — the retry pace is the budget's, not the loop's.
pub async fn create_due_episodes(
    db: &dyn Database,
    now_ms: i64,
    seed: &dyn SeedCallback,
) -> Result<Vec<Value>, FeedError> {
    let mut outcomes = Vec::new();
    let rows = db
        .all(
            "SELECT * FROM risk_feed_series_v2 WHERE enabled=1 ORDER BY series_id LIMIT 16",
            &[],
        )
        .await
        .map_err(|_| FeedError("feed storage unavailable".to_string()))?;
    for row in rows {
        let series =
            RiskFeedSeriesV2::from_json(db::text(&row, "series_json").unwrap_or("")).map_err(FeedError::from)?;
        let latest = latest_episode_start(db, &series.series_id).await?;
        let start = next_episode_start(&series, latest, now_ms);
        let mut outcome = json!({"seriesId": series.series_id, "nextStartMs": start, "created": Value::Null});
        if !(start - series.lead_ms <= now_ms && now_ms < start) {
            outcomes.push(outcome);
            continue;
        }
        let last = db
            .first(
                concat!(
                    "SELECT attempted_at,outcome FROM risk_feed_series_log_v2 WHERE series_id=? AND target_start_ms=? ",
                    "ORDER BY attempted_at DESC LIMIT 1",
                ),
                &[json!(series.series_id), json!(start)],
            )
            .await
            .map_err(|_| FeedError("feed storage unavailable".to_string()))?;
        let backing_off = last.as_ref().is_some_and(|last| {
            db::text(last, "outcome").is_some_and(|outcome| outcome.starts_with("failed"))
                && db::int(last, "attempted_at").is_some_and(|at| now_ms - at < RETRY_MS)
        });
        if backing_off {
            let attempted = db::int(last.as_ref().unwrap(), "attempted_at").unwrap_or(0);
            outcome["backoffUntilMs"] = json!(attempted + RETRY_MS);
            outcomes.push(outcome);
            continue;
        }
        // A compiler, review, budget or approval failure is a failure of *this attempt*: the class
        // name alone made the log undiagnosable, so the stable code names the cause, the type
        // separates a refusal from a crash, and the message is kept for the operator.
        let detail = match attempt(db, &series, &row, start, now_ms, seed).await {
            Ok((binding_id, forecast_id)) => {
                outcome["created"] = json!(binding_id);
                outcome["forecastId"] = json!(forecast_id);
                "published".to_string()
            }
            Err(error) => {
                let reason: String = error
                    .code
                    .clone()
                    .unwrap_or_else(|| error.kind.clone())
                    .lines()
                    .next()
                    .unwrap_or("")
                    .chars()
                    .take(120)
                    .collect();
                outcome["failure"] = json!(reason);
                outcome["failureType"] = json!(error.kind);
                outcome["failureDetail"] = json!(error
                    .message
                    .split_whitespace()
                    .collect::<Vec<_>>()
                    .join(" ")
                    .chars()
                    .take(400)
                    .collect::<String>());
                format!("failed:{reason}")
            }
        };
        db.execute(
            concat!(
                "INSERT OR IGNORE INTO risk_feed_series_log_v2(series_id,target_start_ms,attempted_at,outcome,detail)",
                " VALUES(?,?,?,?,?)",
            ),
            &[
                json!(series.series_id),
                json!(start),
                json!(now_ms),
                json!(detail),
                json!(detail_json(&outcome)),
            ],
        )
        .await
        .map_err(|_| FeedError("feed storage unavailable".to_string()))?;
        outcomes.push(outcome);
    }
    Ok(outcomes)
}

/// How a failed attempt describes itself: a stable code where there is one, the type otherwise.
///
/// The fields are the reference's three: `getattr(exc, "code", "")`, `type(exc).__name__`, and
/// `str(exc)`. A port that replaced them with its own names would log a failure nobody can act on,
/// which is the exact complaint the reference's own comment makes about that log.
#[derive(Debug, Clone)]
pub struct AttemptError {
    pub code: Option<String>,
    pub kind: String,
    pub message: String,
}

impl From<FeedError> for AttemptError {
    fn from(error: FeedError) -> Self {
        Self {
            code: None,
            kind: "ValidationError".to_string(),
            message: error.0,
        }
    }
}

/// One attempt: seed the question, bind it, approve it if the binding is new.
async fn attempt(
    db: &dyn Database,
    series: &RiskFeedSeriesV2,
    row: &crate::db::Row,
    start: i64,
    now_ms: i64,
    seed: &dyn SeedCallback,
) -> Result<(String, String), AttemptError> {
    let profile = db
        .first(
            "SELECT profile_json FROM risk_feed_profiles_v2 WHERE profile_hash=?",
            &[json!(series.mapping_profile_hash)],
        )
        .await
        .map_err(|_| storage_unavailable())?;
    let _ = row;
    let Some(profile) = profile else {
        return Err(AttemptError::from(FeedError(
            "series profile no longer admitted".to_string(),
        )));
    };
    let decoded: Value = serde_json::from_str(db::text(&profile, "profile_json").unwrap_or("")).unwrap_or(Value::Null);
    let question = episode_question(series, start).map_err(AttemptError::from)?;
    let card = seed.seed(question).await?;
    let card = card.get("forecast").cloned().unwrap_or(card);
    let binding = episode_binding(
        series,
        &card,
        start,
        decoded["profile_id"].as_str().unwrap_or(""),
        decoded["profile_version"].as_str().unwrap_or(""),
    )
    .map_err(AttemptError::from)?;
    let existing = db
        .first(
            "SELECT binding_id FROM risk_feed_bindings_v2 WHERE binding_id=?",
            &[json!(binding.binding_id)],
        )
        .await
        .map_err(|_| storage_unavailable())?;
    if existing.is_none() {
        approve_binding_v2(
            db,
            &series.feed_id,
            &binding,
            &format!("series:{}", series.series_id),
            now_ms,
        )
        .await
        .map_err(AttemptError::from)?;
    }
    Ok((binding.binding_id, card["id"].as_str().unwrap_or("").to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::Sqlite;
    use crate::risk_feed_v2::operational_bindings_v2;
    use std::cell::RefCell;

    /// The seed path, answering from the vector.
    struct Seeded {
        asked: std::rc::Rc<RefCell<Vec<String>>>,
        seeded: std::rc::Rc<Vec<Value>>,
        failure: std::rc::Rc<Value>,
    }

    impl SeedCallback for Seeded {
        fn seed<'a>(&'a self, question: String) -> BorrowedFuture<'a, Result<Value, AttemptError>> {
            self.asked.borrow_mut().push(question.clone());
            // A question the vector has no card for is the seed path refusing it, which is what the
            // failing series is. The error travels with the type and message the reference recorded,
            // because that is what its log holds.
            let card = self
                .seeded
                .iter()
                .find(|entry| entry["question"] == json!(question) && !entry["card"].is_null())
                .map(|entry| entry["card"].clone());
            let failure = self.failure.clone();
            Box::pin(async move {
                match card {
                    Some(card) => Ok(json!({"forecast": card})),
                    None => Err(AttemptError {
                        code: failure["code"].as_str().map(str::to_string),
                        kind: failure["kind"].as_str().unwrap_or("Error").to_string(),
                        message: failure["message"].as_str().unwrap_or("").to_string(),
                    }),
                }
            })
        }
    }

    fn block<F: std::future::Future>(future: F) -> F::Output {
        futures_lite::future::block_on(future)
    }

    fn golden() -> Value {
        let path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../tests/golden/risk-feed-series-golden.json");
        serde_json::from_str(&std::fs::read_to_string(&path).expect("risk feed series golden")).expect("json")
    }

    fn restore(db: &Sqlite, table: &str, rows: &[Value]) {
        for row in rows {
            let fields = row.as_object().expect("a fixture row is an object");
            let columns: Vec<&str> = fields.keys().map(String::as_str).collect();
            let placeholders = vec!["?"; columns.len()].join(",");
            let params: Vec<Value> = columns.iter().map(|name| fields[*name].clone()).collect();
            db.run(
                &format!("INSERT INTO {table}({}) VALUES({placeholders})", columns.join(",")),
                &params,
            )
            .unwrap_or_else(|error| panic!("{table}: {error}"));
        }
    }

    fn check(calls: &[Value], index: &mut usize, produced: Result<Value, FeedError>) {
        let entry = &calls[*index];
        let name = entry["call"].as_str().unwrap();
        assert_eq!(
            entry["call"], calls[*index]["call"],
            "call {index} is not the one being replayed"
        );
        match produced {
            Ok(value) => {
                assert!(
                    entry["error"].is_null(),
                    "{name}: succeeded where the reference refused"
                );
                assert_eq!(value, entry["result"], "{name}: a different result");
            }
            Err(error) => {
                assert!(
                    !entry["error"].is_null(),
                    "{name}: refused with {error:?} where the reference succeeded"
                );
                assert_eq!(
                    error.0,
                    entry["error"]["message"].as_str().unwrap(),
                    "{name}: a different refusal"
                );
            }
        }
        *index += 1;
    }

    fn plain_int(entry: &Value, field: &str) -> Option<i64> {
        entry["input"].get(field).and_then(Value::as_i64)
    }

    #[test]
    fn the_reference_episode_scheduler_is_reproduced_call_for_call() {
        let document = golden();
        let calls = document["calls"].as_array().expect("calls").clone();
        let db = Sqlite::from_migrations();
        // The admitted records the series cites, restored so `configure` has something to agree
        // with. The scheduler neither writes nor admits them, and the comparison below still
        // checks they came through unchanged.
        for table in [
            "users",
            "artifacts",
            "forecasts",
            "risk_feed_definitions_v2",
            "risk_feed_profiles_v2",
        ] {
            restore(
                &db,
                table,
                document["rows"][table]
                    .as_array()
                    .cloned()
                    .unwrap_or_default()
                    .as_slice(),
            );
        }
        let definition = forecast_domain::risk_feed::CanonicalRiskDefinitionV2::from_json(
            document["rows"]["risk_feed_definitions_v2"][0]["definition_json"]
                .as_str()
                .unwrap_or(""),
        )
        .expect("the admitted definition");
        let profile = forecast_domain::risk_feed::RiskMappingProfileV2::from_json(
            document["rows"]["risk_feed_profiles_v2"][0]["profile_json"]
                .as_str()
                .unwrap_or(""),
        )
        .expect("the admitted profile");
        let series: RiskFeedSeriesV2 = serde_json::from_value(document["series"].clone()).expect("the series");
        let now = db::int(
            &document["rows"]["risk_feed_series_v2"][0]
                .as_object()
                .cloned()
                .expect("a series row"),
            "updated_at",
        )
        .unwrap_or(0);
        let mut index = 0usize;

        // --- configure.
        let unadmitted = RiskFeedSeriesV2 {
            definition_hash: "0".repeat(64),
            ..series.clone()
        };
        check(
            &calls,
            &mut index,
            block(configure_series(&db, &unadmitted, true, "a", now)).map(|d| json!(d)),
        );
        check(
            &calls,
            &mut index,
            block(configure_series(&db, &series, true, " ", now)).map(|d| json!(d)),
        );
        check(
            &calls,
            &mut index,
            block(configure_series(&db, &series, true, "a", now)).map(|d| json!(d)),
        );
        check(
            &calls,
            &mut index,
            block(configure_series(&db, &series, true, "a", now)).map(|d| json!(d)),
        );

        // --- the cadence arithmetic, against the instants the vector fixed.
        for position in 4..7 {
            let entry = &calls[position];
            let decided = next_episode_start(
                &series,
                plain_int(entry, "latestStartMs"),
                plain_int(entry, "nowMs").unwrap_or(0),
            );
            check(&calls, &mut index, Ok(json!(decided)));
        }
        // --- the templated question.
        let start = calls[7]["input"]["startMs"].as_i64().unwrap();
        check(&calls, &mut index, episode_question(&series, start).map(|q| json!(q)));

        // --- the tick. The seed path is the application's, so it is replayed from the vector: the
        // card the reference was handed, keyed by the question it was asked.
        // Owned rather than borrowed, so the closure is `'static` whatever it captures: the future
        // it returns has to be, and a captured reference could not be.
        let seeded = std::rc::Rc::new(document["seeded"].as_array().cloned().unwrap_or_default());
        let failure = std::rc::Rc::new(document["failedSeed"].clone());
        let asked = std::rc::Rc::new(RefCell::new(Vec::<String>::new()));
        // The handle is kept outside the struct so the questions asked can be read back afterwards:
        // a struct that owned the only handle could not be inspected once it was moved.
        let observed = std::rc::Rc::clone(&asked);
        let seed = Seeded { asked, seeded, failure };
        let tick = |position: usize| -> Result<Value, FeedError> {
            let at = calls[position]["input"]["nowMs"].as_i64().unwrap_or(0);
            block(create_due_episodes(&db, at, &seed)).map(|outcomes| json!(outcomes))
        };
        // The vector's order is the reference's: the tick that creates the episode, then the
        // listing that shows it, then the ticks that must not create a second one.
        check(&calls, &mut index, tick(8));
        check(
            &calls,
            &mut index,
            block(operational_bindings_v2(&db, "risk-v2", start + 1000)).map(|bindings| {
                json!(bindings
                    .iter()
                    .map(|b| serde_json::to_value(b).unwrap())
                    .collect::<Vec<_>>())
            }),
        );
        check(&calls, &mut index, tick(10));
        // The second series is written by the vector rather than by this module: its seed always
        // fails, which is what the backoff is measured against.
        restore(
            &db,
            "risk_feed_series_v2",
            document["rows"]["risk_feed_series_v2"]
                .as_array()
                .unwrap_or(&Vec::new())[1..]
                .as_ref(),
        );
        for position in 11..13 {
            check(&calls, &mut index, tick(position));
        }

        assert_eq!(index, calls.len(), "every recorded call is replayed");
        assert_eq!(
            json!(observed.borrow().clone()),
            document["asked"],
            "the seed path was asked different questions"
        );
        for (table, expected) in document["rows"].as_object().expect("rows") {
            if table == "users" || table == "artifacts" || table == "forecasts" {
                continue; // Restored above; the scheduler does not write them.
            }
            let (rows, _) = db
                .run(&format!("SELECT * FROM {table} ORDER BY rowid"), &[])
                .expect("rows");
            assert_eq!(json!(rows), *expected, "{table}: different rows");
        }
        let _ = (definition, profile);
    }
}
