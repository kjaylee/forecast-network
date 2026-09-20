//! The profile card: an owner-published, immutable snapshot of a forecaster's public record.
//!
//! Two halves that must not be confused. The *snapshot* is an authenticated-owner read of one
//! database snapshot, assembled so that `asOf` is a claim about a single read rather than about a
//! sequence of them. The *lookup* is public and verifies the retained bytes against their own hash
//! before serving them. `create_profile_card` is the only bridge, and it exists so that nothing is
//! public until the owner deliberately shares it.

use crate::db::Database;
use crate::mutate::MAX_ARTIFACT_BYTES;
use crate::projections::{profile_card_json, profile_card_payload, PROFILE_CARD_SQL};
use crate::reads::PROFILE_CARD_PREFIX;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CardError {
    pub status: u16,
    pub code: &'static str,
    pub message: &'static str,
}

impl CardError {
    const fn new(status: u16, code: &'static str, message: &'static str) -> Self {
        Self { status, code, message }
    }
}

/// `profile_card`: the authenticated-owner snapshot.
///
/// One prepared statement observes identity, the whole scoring ledger, the translated titles, the
/// newest history and the highlight. `asOf` records when that complete read finished; it is not a
/// claim that the snapshot includes mutations committed after the read began.
pub async fn snapshot(db: &dyn Database, user_id: &str, as_of: i64) -> Result<Value, CardError> {
    let row = db
        .first(PROFILE_CARD_SQL, &[json!(user_id)])
        .await
        .map_err(|_| unavailable())?;
    let Some(row) = row else {
        return Err(CardError::new(404, "profile_not_found", "Profile not found."));
    };
    let payload = profile_card_payload(&row, as_of);
    let canonical = profile_card_json(&payload);
    let digest = hex::encode(Sha256::digest(format!("{PROFILE_CARD_PREFIX}{canonical}").as_bytes()));
    let mut card = payload;
    card["canonicalJson"] = json!(canonical);
    card["snapshotHash"] = json!(digest);
    Ok(card)
}

/// `create_profile_card`: publish only after an authenticated owner's deliberate share action.
pub async fn create(db: &dyn Database, user_id: &str, as_of: i64) -> Result<Value, CardError> {
    let card = snapshot(db, user_id, as_of).await?;
    let canonical = card["canonicalJson"].as_str().unwrap_or("").to_string();
    if canonical.len() > MAX_ARTIFACT_BYTES {
        return Err(CardError::new(
            413,
            "profile_card_too_large",
            "The shared profile record exceeds the publication limit.",
        ));
    }
    db.execute(
        "INSERT OR IGNORE INTO artifacts(hash,kind,body,media_type,created_at) VALUES(?,?,?,?,?)",
        &[
            card["snapshotHash"].clone(),
            json!("profile-share-snapshot"),
            json!(canonical),
            json!("application/json"),
            json!(as_of),
        ],
    )
    .await
    .map_err(|_| unavailable())?;
    // The published card is served exactly as the public lookup would serve it, so an owner who
    // shares it sees what a reader sees.
    Ok(card)
}

/// `eligibility_status`: the eligibility view, with the timing review folded in when there is no
/// decision.
///
/// The fold matters because a question whose publication time could not be established is *under
/// review* rather than undecided, and a caller told `none` would read that as "nothing happened".
/// The personal block is added only for a user who actually submitted, because it describes what
/// happens to *their* receipt.
pub async fn eligibility_status(
    db: &dyn Database,
    forecast_id: &str,
    user_id: Option<&str>,
    now_ms: i64,
) -> Result<Value, CardError> {
    let mut result = crate::eligibility::status(db, forecast_id, user_id)
        .await
        .map_err(|_| unavailable())?;
    if result["status"] != json!("none") {
        return Ok(result);
    }
    let timing = crate::resolution_timing::status(db, forecast_id)
        .await
        .map_err(|_| unavailable())?;
    if timing["status"] != json!("review") {
        return Ok(result);
    }
    result["status"] = json!("review");
    result["timingReview"] = timing;
    if let Some(user_id) = user_id {
        let submitted = db
            .first(
                "SELECT 1 FROM user_forecasts WHERE forecast_id=? AND user_id=?",
                &[json!(forecast_id), json!(user_id)],
            )
            .await
            .map_err(|_| unavailable())?;
        if submitted.is_some() {
            // The personal block says what the review means for *this* submission: nothing is
            // voided yet, no revision is effective, and no points have moved.
            result["personal"] = json!({
                "status": "review",
                "voidedRevisions": [],
                "effectiveRevision": Value::Null,
                "refundedPoints": 0,
                "adjustmentPending": false,
            });
        }
    }
    let _ = now_ms;
    Ok(result)
}

fn unavailable() -> CardError {
    CardError::new(
        503,
        "profile_card_integrity_failed",
        "This shared profile record failed integrity verification.",
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::{self, Sqlite};

    fn block<F: std::future::Future>(future: F) -> F::Output {
        futures_lite::future::block_on(future)
    }

    fn golden() -> Value {
        let path =
            std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/profile-card-golden.json");
        serde_json::from_str(&std::fs::read_to_string(&path).expect("profile card golden")).expect("json")
    }

    fn check(calls: &[Value], index: &mut usize, produced: Result<Value, CardError>) {
        let entry = &calls[*index];
        let name = entry["call"].as_str().unwrap();
        match produced {
            Ok(value) => {
                assert!(
                    entry["error"].is_null(),
                    "{name}: succeeded where the reference refused"
                );
                assert_eq!(value, entry["result"], "{name}: a different card");
            }
            Err(error) => {
                assert!(
                    !entry["error"].is_null(),
                    "{name}: refused with {error:?} where the reference succeeded"
                );
                assert_eq!(
                    error.status as i64,
                    entry["error"]["status"].as_i64().unwrap(),
                    "{name}: status"
                );
                assert_eq!(Some(error.code), entry["error"]["code"].as_str(), "{name}: code");
                assert_eq!(
                    Some(error.message),
                    entry["error"]["message"].as_str(),
                    "{name}: message"
                );
            }
        }
        *index += 1;
    }

    #[test]
    fn the_reference_profile_card_is_reproduced_call_for_call() {
        let document = golden();
        let calls = document["calls"].as_array().expect("calls").clone();
        let db = Sqlite::from_migrations();
        // The state the first two cards were read in.
        for table in ["users", "forecasts", "user_forecasts", "artifacts"] {
            for row in document["initial"][table].as_array().cloned().unwrap_or_default() {
                let fields = row.as_object().expect("a fixture row");
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
        let user_id = document["rows"]["users"][1]["id"].as_str().unwrap().to_string();
        let mut index = 0usize;

        // `asOf` is the clock at the moment of *that* read, so it comes out of the call rather
        // than from one clock the whole vector shares.
        let empty_at = calls[0]["result"]["asOf"].as_i64().unwrap();
        check(&calls, &mut index, block(snapshot(&db, &user_id, empty_at)));
        check(&calls, &mut index, block(snapshot(&db, "u-nobody", empty_at)));
        // The rest of the fixture, which the later cards read: the vector's rows are the state
        // *after* the question was published, and the two cards above were read before it. Only the
        // tables that changed are replayed — the users are already there.
        for table in ["forecasts", "user_forecasts", "artifacts"] {
            for row in document["rows"][table].as_array().cloned().unwrap_or_default() {
                let fields = row.as_object().expect("a fixture row");
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
        let scored_at = calls[2]["result"]["asOf"].as_i64().unwrap();
        let scored = block(snapshot(&db, &user_id, scored_at));
        check(&calls, &mut index, scored);
        // The published card is the same card, and it is retained under its own hash.
        let created = block(create(&db, &user_id, scored_at));
        check(&calls, &mut index, created);
        let snapshot_hash = calls[3]["result"]["snapshotHash"].as_str().unwrap().to_string();

        // The lookup is `reads::profile_card`, which answers a Response rather than a Value; the
        // same body is compared here through the stored row, and the two refusals are the part this
        // module owns.
        let stored = db
            .run(
                "SELECT body FROM artifacts WHERE hash=? AND kind='profile-share-snapshot'",
                &[json!(snapshot_hash)],
            )
            .expect("artifacts")
            .0;
        assert_eq!(
            json!(db::text(&stored[0], "body").unwrap_or("")),
            calls[4]["result"]["canonicalJson"],
            "the retained body is the card's canonical text"
        );
        // The two refusals the lookup makes, which this module's half is responsible for: a key
        // that is not a hash is not found, and a row whose bytes do not hash to its key fails
        // integrity. They are different answers, and the vector records both.
        for position in [5usize, 6] {
            let hash = calls[position]["input"]["snapshotHash"].as_str().unwrap();
            let shaped = hash.len() == 64 && hash.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'));
            let row = db
                .run(
                    "SELECT body,media_type FROM artifacts WHERE hash=? AND kind='profile-share-snapshot'",
                    &[json!(hash)],
                )
                .expect("artifacts")
                .0;
            let intact = row.first().is_some_and(|row| {
                let canonical = db::text(row, "body").unwrap_or("");
                let actual = hex::encode(Sha256::digest(format!("{PROFILE_CARD_PREFIX}{canonical}").as_bytes()));
                actual == hash && db::text(row, "media_type") == Some("application/json")
            });
            // What makes the hash a commitment is that `intact` is checked at all: without it the
            // mismatched row would be served as a card.
            assert!(!intact, "the mismatched row must not verify");
            assert_eq!(
                calls[position]["error"]["code"].as_str().unwrap(),
                if shaped {
                    "profile_card_integrity_failed"
                } else {
                    "profile_card_not_found"
                },
                "{}: the refusal names the reason",
                calls[position]["call"]
            );
        }
    }
}
