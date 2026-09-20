//! Application orchestration for retained official events.
//!
//! The parts that are portable without the application: the family hints a registration is keyed
//! on, the publisher-root → feed mapping the watcher actually reads, and the operator status view.
//!
//! The rest of `automation.py` is `ForecastAutomation`'s hold/review/accept/dismiss callbacks, and
//! each of them reaches into `Application` — `_forecast`, `participation_holds`, `eligibility`,
//! `markets`, `_mutate`, `reward_evidence_report`. Porting them before the application layer would
//! mean inventing the seams they call through, which is how a port grows a second implementation
//! of the thing it is porting.

use crate::db::{self, Database};
use crate::sources::host_and_path;
use forecast_domain::models::ForecastSpecification;
use serde_json::{json, Value};

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
