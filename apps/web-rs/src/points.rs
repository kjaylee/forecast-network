//! Participation points read models (`PointsService.summary` / `position`).

use serde_json::{json, Value};
use worker::*;

use crate::db::{self, get, int, text, Database, Row};

pub const POLICY_VERSION: &str = "participation-points-v1";

/// `MAX_STAKE`-free request bound: the reference refuses more than a hundred identifiers at once
/// rather than answering a request nobody can read.
pub const MAX_IDENTIFIERS: usize = 100;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PointsError {
    pub status: u16,
    pub code: &'static str,
    pub message: &'static str,
}

/// `_identifier`: a non-empty string under 129 characters with no control characters.
///
/// The reference raises rather than skipping, so a malformed identifier is a refusal and not a
/// silently missing position — which is the difference between "you have no stake" and "you asked
/// about something that is not an identifier".
pub fn identifier(value: &str) -> Result<&str, PointsError> {
    let invalid = |_| PointsError {
        status: 400,
        code: "invalid_points_request",
        message: "Invalid participation points request.",
    };
    if value.is_empty() || value.chars().count() > 128 || value.chars().any(|character| (character as u32) < 32) {
        return Err(invalid(()));
    }
    Ok(value)
}

/// A storage fault is not a request fault: the reference's `Database` raises through, and the
/// route layer reports it as a server error.
impl From<worker::Error> for PointsError {
    fn from(_: worker::Error) -> Self {
        PointsError {
            status: 503,
            code: "points_unavailable",
            message: "Participation points are temporarily unavailable.",
        }
    }
}

/// The identifier list is serialized into one JSON parameter, so an unserializable list is the
/// same storage-level failure.
impl From<serde_json::Error> for PointsError {
    fn from(_: serde_json::Error) -> Self {
        PointsError {
            status: 503,
            code: "points_unavailable",
            message: "Participation points are temporarily unavailable.",
        }
    }
}

fn too_many() -> PointsError {
    PointsError {
        status: 400,
        code: "invalid_points_request",
        message: "Too many forecast positions were requested.",
    }
}

fn position(row: Option<&Row>) -> Value {
    match row {
        Some(row) => json!({
            "amount": get(row, "amount"), "status": get(row, "status"), "policyVersion": get(row, "policy_version"),
            "returned": get(row, "returned"), "outcome": get(row, "outcome"), "forecastRevision": get(row, "forecast_revision"),
        }),
        None => {
            json!({"amount": 0, "status": "practice", "policyVersion": POLICY_VERSION, "returned": null, "outcome": null, "forecastRevision": null})
        }
    }
}

pub async fn position_for(db: &dyn Database, user_id: &str, forecast_id: &str) -> Result<Value, PointsError> {
    identifier(user_id)?;
    let rows = db
        .all(
            "SELECT * FROM point_positions WHERE user_id=? AND forecast_id IN (SELECT value FROM json_each(?))",
            &[json!(user_id), json!(serde_json::to_string(&[forecast_id])?)],
        )
        .await?;
    Ok(position(
        rows.iter().find(|r| text(r, "forecast_id") == Some(forecast_id)),
    ))
}

pub async fn summary(db: &dyn Database, user_id: &str) -> Result<Value, PointsError> {
    identifier(user_id)?;
    let installed: Vec<String> = db
        .all(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN \
         ('market_account_ledger','point_eligibility_adjustments','market_fill_voids','point_evidence_rewards')",
            &[],
        )
        .await?
        .iter()
        .filter_map(|r| text(r, "name").map(str::to_string))
        .collect();
    let has = |name: &str| installed.iter().any(|n| n == name);
    let mut history = String::from(
        "SELECT id,kind,available_delta,committed_delta,available_after,committed_after,stake,returned,forecast_id,created_at \
         FROM point_ledger l WHERE l.user_id=a.user_id ",
    );
    let mut fraction = "0".to_string();
    if has("market_account_ledger") {
        history.push_str(
            "UNION ALL SELECT ml.id,ml.kind,ml.available_delta,ml.committed_delta,ml.available_after,ml.committed_after,\
             ABS(ml.committed_delta), ( CASE WHEN ml.kind='market_settlement' THEN ml.available_delta ELSE NULL END ) ,\
             ml.forecast_id,ml.created_at FROM market_account_ledger ml WHERE ml.user_id=a.user_id AND ml.mode='active' ",
        );
        fraction =
            "COALESCE((SELECT remainder_atomic FROM point_fractions WHERE user_id=a.user_id AND mode='active'),0)"
                .to_string();
    }
    if has("point_eligibility_adjustments") {
        history.push_str(
            "UNION ALL SELECT e.id, ( CASE WHEN e.available_delta>=0 THEN 'evidence_refund' ELSE 'evidence_restore' END ) ,\
             e.available_delta,e.committed_delta,e.available_after,e.committed_after,e.old_amount,\
             MAX(0,e.available_delta),e.forecast_id,e.created_at FROM point_eligibility_adjustments e \
             WHERE e.user_id=a.user_id AND e.available_delta!=0 ",
        );
    }
    if has("point_evidence_rewards") {
        history.push_str(
            "UNION ALL SELECT w.id,'evidence_reward',w.amount,0,w.available_after,w.committed_after,0,w.amount,\
             w.forecast_id,w.created_at FROM point_evidence_rewards w WHERE w.user_id=a.user_id ",
        );
    }
    if has("market_fill_voids") {
        history.push_str(
            "UNION ALL SELECT 'market-void:'||v.fill_id,'market_void_refund',v.spend,-v.spend,\
             v.available_before+v.spend,v.committed_before-v.spend,v.spend,v.spend,v.forecast_id,v.created_at \
             FROM market_fill_voids v WHERE v.user_id=a.user_id AND v.mode='active' ",
        );
    }
    history.push_str("ORDER BY created_at DESC,id DESC LIMIT 30");
    let sql = format!(
        "SELECT a.*,p.profile_grant,p.wallet_grant,p.max_stake,p.win_return_multiplier,p.invalid_return_multiplier,\
         profile.created_at AS profile_awarded_at,wallet.created_at AS wallet_awarded_at,\
         w.address AS linked_address,EXISTS(SELECT 1 FROM point_awards old WHERE old.wallet_address=w.address) AS address_rewarded,\
         (SELECT json_group_array(json_object('id',id,'kind',kind,'amount',available_delta,\
         'availableDelta',available_delta,'committedDelta',committed_delta,'availableAfter',available_after,\
         'committedAfter',committed_after,'stake',stake,'returned',returned,'forecastId',forecast_id,'at',created_at)) \
         FROM ({history})) AS entries_json, {fraction} AS fraction_atomic \
         FROM point_accounts a JOIN point_policies p ON p.version=? \
         LEFT JOIN point_awards profile ON profile.user_id=a.user_id AND profile.kind='profile' \
         LEFT JOIN point_awards wallet ON wallet.user_id=a.user_id AND wallet.kind='wallet' \
         LEFT JOIN wallet_links w ON w.user_id=a.user_id WHERE a.user_id=?"
    );
    // A missing account is a refusal, not an absence: the caller is authenticated and asking about
    // themselves, so a `None` here would only mean the route layer had to invent the same sentence.
    let Some(row) = db.first(&sql, &[json!(POLICY_VERSION), json!(user_id)]).await? else {
        return Err(PointsError {
            status: 401,
            code: "points_account_missing",
            message: "Sign in to view your participation points.",
        });
    };
    let rewarded = !get(&row, "wallet_awarded_at").is_null();
    let linked = !get(&row, "linked_address").is_null();
    let address_rewarded = int(&row, "address_rewarded").unwrap_or(0) != 0;
    let eligible = !rewarded && !address_rewarded;
    let reason = if rewarded {
        "awarded"
    } else if address_rewarded {
        "wallet_already_rewarded"
    } else if linked {
        "eligible"
    } else {
        "connect_wallet"
    };
    let available = int(&row, "available").unwrap_or(0);
    let committed = int(&row, "committed").unwrap_or(0);
    let entries: Value = serde_json::from_str(text(&row, "entries_json").unwrap_or("[]")).unwrap_or(json!([]));
    Ok(json!({
        "userId": user_id, "available": available, "committed": committed, "total": available + committed,
        "fractionAtomic": get(&row, "fraction_atomic").as_i64().map(|v| v.to_string()).unwrap_or_else(|| get(&row, "fraction_atomic").to_string()),
        "atomicScale": 1_000_000,
        "policy": {"version": POLICY_VERSION, "profileGrant": get(&row, "profile_grant"), "walletGrant": get(&row, "wallet_grant"),
                   "minStake": 0, "maxStake": get(&row, "max_stake"), "winReturnMultiplier": get(&row, "win_return_multiplier"),
                   "invalidReturnMultiplier": get(&row, "invalid_return_multiplier"), "practiceAllowed": true, "purchasable": false,
                   "transferable": false, "redeemable": false, "reputationWeighted": false},
        "onboarding": {"profile": {"completed": !get(&row, "profile_awarded_at").is_null(), "reward": get(&row, "profile_grant"),
                                   "awardedAt": get(&row, "profile_awarded_at")},
                       "wallet": {"completed": rewarded, "linked": linked, "reward": get(&row, "wallet_grant"), "eligible": eligible,
                                  "reason": reason, "awardedAt": get(&row, "wallet_awarded_at")}},
        "entries": entries,
    }))
}

/// `PointsService.positions`: one entry per requested id (practice defaults when absent).
pub async fn positions(
    db: &dyn Database,
    user_id: &str,
    forecast_ids: &[String],
) -> Result<std::collections::BTreeMap<String, Value>, PointsError> {
    // Validated and deduplicated in one pass, in the reference's order: `dict.fromkeys` keeps the
    // first occurrence, and the cap is checked before anything is validated.
    if forecast_ids.len() > MAX_IDENTIFIERS {
        return Err(too_many());
    }
    let mut identifiers: Vec<String> = Vec::new();
    for id in forecast_ids {
        let checked = identifier(id)?.to_string();
        if !identifiers.contains(&checked) {
            identifiers.push(checked);
        }
    }
    let mut result = std::collections::BTreeMap::new();
    if identifiers.is_empty() {
        return Ok(result);
    }
    let rows = db
        .all(
            "SELECT * FROM point_positions WHERE user_id=? AND forecast_id IN (SELECT value FROM json_each(?))",
            &[json!(user_id), json!(serde_json::to_string(&identifiers)?)],
        )
        .await?;
    for id in identifiers {
        let row = rows.iter().find(|r| text(r, "forecast_id") == Some(id.as_str()));
        result.insert(id, position(row));
    }
    Ok(result)
}

/// `EVIDENCE_REWARD_POINTS`: the fixed credit for the report that surfaced the evidence an
/// accepted early resolution cites.
///
/// Nothing calls the reward yet: `automation.accept` is the caller, and the orchestration half of
/// `automation.py` waits on the application layer. The same shape as `eligibility`'s deciding half,
/// which carried `allow(dead_code)` until the early-resolution automation landed.
#[allow(dead_code)]
pub const EVIDENCE_REWARD_POINTS: i64 = 100;

/// `Application.reward_evidence_report`: credit the *earliest* held report whose retained evidence
/// the accepted trigger cites.
///
/// Three things about it are the whole rule. The report has to be `held`, because a report nobody
/// reviewed is not evidence. The `NOT EXISTS` guard makes the reward once-per-forecast rather than
/// once-per-report, so a second qualifying report cannot pay twice. And the earliest is chosen by
/// `created_at, id`, which is what makes the payout deterministic when two arrived together.
#[allow(dead_code)]
pub async fn reward_evidence_report(
    db: &dyn Database,
    forecast_id: &str,
    evidence_hashes: &[String],
    now_ms: i64,
    random_token: &dyn Fn() -> String,
) -> Result<Option<Value>, PointsError> {
    if evidence_hashes.is_empty() {
        return Ok(None);
    }
    let placeholders = vec!["?"; evidence_hashes.len()].join(",");
    let mut params = vec![json!(forecast_id)];
    params.extend(evidence_hashes.iter().map(|hash| json!(hash)));
    let report = db
        .first(
            &format!(
                "SELECT r.* FROM evidence_reports r WHERE r.forecast_id=? AND r.status='held' \
                 AND r.artifact_hash IN ({placeholders}) AND NOT EXISTS(SELECT 1 FROM point_evidence_rewards w \
                 WHERE w.forecast_id=r.forecast_id) ORDER BY r.created_at,r.id LIMIT 1"
            ),
            &params,
        )
        .await?;
    let Some(report) = report else {
        return Ok(None);
    };
    let user_id = text(&report, "user_id").unwrap_or("").to_string();
    let account = db
        .first(
            "SELECT available,committed FROM point_accounts WHERE user_id=?",
            &[json!(user_id)],
        )
        .await?;
    // No account is no reward, not an error: the caller is an accepted trigger, and failing here
    // would undo a resolution that has already been decided.
    let Some(account) = account else {
        return Ok(None);
    };
    let report_id = text(&report, "id").unwrap_or("");
    let token = (random_token)();
    let reward_id = format!("pr_{}", &token[..24.min(token.len())]);
    db.batch(&[
        (
            "UPDATE point_accounts SET available=available+?,updated_at=? WHERE user_id=?".to_string(),
            vec![json!(EVIDENCE_REWARD_POINTS), json!(now_ms), json!(user_id)],
        ),
        (
            concat!(
                "INSERT INTO point_evidence_rewards(id,report_id,user_id,forecast_id,amount,available_after,",
                "committed_after,created_at) VALUES(?,?,?,?,?,?,?,?)",
            )
            .to_string(),
            vec![
                json!(reward_id),
                json!(report_id),
                json!(user_id),
                json!(forecast_id),
                json!(EVIDENCE_REWARD_POINTS),
                json!(int(&account, "available").unwrap_or(0) + EVIDENCE_REWARD_POINTS),
                db::get(&account, "committed").clone(),
                json!(now_ms),
            ],
        ),
        (
            "UPDATE evidence_reports SET status='rewarded',updated_at=? WHERE id=?".to_string(),
            vec![json!(now_ms), json!(report_id)],
        ),
    ])
    .await?;
    Ok(Some(json!({
        "reportId": report_id,
        "userId": user_id,
        "amount": EVIDENCE_REWARD_POINTS,
    })))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::Sqlite;

    fn block<F: std::future::Future>(future: F) -> F::Output {
        futures_lite::future::block_on(future)
    }

    fn golden() -> Value {
        let path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/points-golden.json");
        serde_json::from_str(&std::fs::read_to_string(&path).expect("points golden")).expect("json")
    }

    fn check(calls: &[Value], index: &mut usize, produced: Result<Value, PointsError>) {
        let entry = &calls[*index];
        let name = entry["call"].as_str().unwrap();
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

    /// The points suite's own fixture, replayed the way it was built.
    ///
    /// The tables validate *transitions*: a stake's ledger entry has to follow from the account and
    /// the position, and the position must not yet carry the revision the entry is making. So the
    /// order below is the order the triggers admit — the entry first, with no position yet, and the
    /// position it opened afterwards. The grant entries and the account are left to the award
    /// trigger, which writes both from the awards.
    fn seed(db: &Sqlite, document: &Value, staked: bool) {
        let insert = |table: &str, row: &Value| {
            let fields = row.as_object().expect("a fixture row");
            let columns: Vec<&str> = fields.keys().map(String::as_str).collect();
            let placeholders = vec!["?"; columns.len()].join(",");
            let params: Vec<Value> = columns.iter().map(|name| fields[*name].clone()).collect();
            db.run(
                &format!("INSERT INTO {table}({}) VALUES({placeholders})", columns.join(",")),
                &params,
            )
            .unwrap_or_else(|error| panic!("{table}: {error}"));
        };
        for table in ["users", "forecasts", "user_forecasts"] {
            for row in document["rows"][table].as_array().cloned().unwrap_or_default() {
                insert(table, &row);
            }
        }
        if staked {
            for row in document["rows"]["point_ledger"].as_array().cloned().unwrap_or_default() {
                if row["kind"] == json!("reservation") {
                    insert("point_ledger", &row);
                }
            }
        }
        // The position is not restored: the ledger's own trigger opens it on a first stake, which
        // is the transition this row is, and inserting it by hand only collides with that.
        for row in document["rows"]["point_accounts"]
            .as_array()
            .cloned()
            .unwrap_or_default()
        {
            let fields = row.as_object().expect("an account row");
            db.run(
                "UPDATE point_accounts SET available=?,committed=?,updated_at=? WHERE user_id=?",
                &[
                    fields["available"].clone(),
                    fields["committed"].clone(),
                    fields["updated_at"].clone(),
                    fields["user_id"].clone(),
                ],
            )
            .expect("account");
        }
    }

    #[test]
    fn the_reference_points_read_models_are_reproduced_call_for_call() {
        let document = golden();
        let calls = document["calls"].as_array().expect("calls").clone();
        let db = Sqlite::from_migrations();
        seed(&db, &document, true);
        let mut index = 0usize;

        for position in 0..6 {
            let name = calls[position]["call"].as_str().unwrap();
            let user = calls[position]["input"]["userId"].as_str().unwrap();
            check(&calls, &mut index, block(summary(&db, user)));
            let _ = name;
        }
        for position in 6..8 {
            let user = calls[position]["input"]["userId"].as_str().unwrap();
            let id = calls[position]["input"]["forecastId"].as_str().unwrap();
            check(&calls, &mut index, block(position_for(&db, user, id)));
        }
        // `positions` takes a list, and the malformed cases are not expressible as `String` — so
        // they are replayed by hand, which is what the vector says they are.
        check(
            &calls,
            &mut index,
            block(positions(
                &db,
                "user-a",
                &["f-staked".to_string(), "f-none".to_string(), "f-staked".to_string()],
            ))
            .map(|map| json!(map)),
        );
        check(
            &calls,
            &mut index,
            block(positions(&db, "user-a", &[])).map(|map| json!(map)),
        );
        check(
            &calls,
            &mut index,
            block(positions(
                &db,
                "user-a",
                &(0..101).map(|n| format!("f-{n}")).collect::<Vec<_>>(),
            ))
            .map(|map| json!(map)),
        );
        check(
            &calls,
            &mut index,
            block(positions(&db, "user-a", &["f-staked".to_string(), String::new()])).map(|map| json!(map)),
        );
        // The reference passes a *number* where a string belongs; the port's signature cannot, and
        // the check it would have hit is the identifier rule the case above already exercises.
        assert_eq!(calls[12]["input"]["forecastIds"], json!([7]));
        assert!(
            identifier("7").is_ok(),
            "a number is not a string and never reaches `identifier`"
        );
        assert!(identifier("").is_err());
        assert!(identifier("bad\u{1}id").is_err());
        assert!(identifier(&"u".repeat(129)).is_err());

        assert_eq!(
            index, 12,
            "every recorded call that this signature can express is replayed"
        );
        assert_eq!(calls.len(), 13);
    }

    #[test]
    fn the_reference_evidence_reward_is_reproduced_case_for_case() {
        // The reward pays once per forecast, so each case runs on its own store: sharing one would
        // make every case after the first a replay of it.
        let document = golden();
        let now = document["now"].as_i64().unwrap();
        for entry in document["rewards"].as_array().expect("rewards") {
            let name = entry["call"].as_str().unwrap();
            let db = Sqlite::from_migrations();
            seed(&db, &document, false);
            // The reward fixture has no stake, so user-a's account is the grant alone. The stake
            // belongs to the reader's fixture, and the vector records each fixture's own state.
            db.run(
                "UPDATE point_accounts SET available=1000,committed=0,updated_at=? WHERE user_id='user-a'",
                &[json!(now - 200)],
            )
            .expect("account");
            // The report under test: its status is the case, and everything else is the fixture.
            // The reward cases name their own forecast, so the fixture gets a second one: the
            // report's `forecast_id` is a foreign key, and a report about a question nobody
            // published is not a row the store accepts. The row is copied from the seeded one and
            // re-keyed, so every other column stays what the fixture made it.
            let (seeded, _) = db
                .run("SELECT * FROM forecasts WHERE id='f-staked'", &[])
                .expect("the seeded forecast");
            let mut copied = seeded[0].clone();
            copied.insert("id".to_string(), json!("f-reward"));
            copied.insert("draft_id".to_string(), json!("draft:f-reward"));
            // The question and its normalization are unique per closing instant, so a second
            // forecast about the same question at the same time is not a row the store accepts.
            copied.insert("normalized_question".to_string(), json!("reward question"));
            // And the specification hash is unique too: one published specification is one question.
            copied.insert("specification_hash".to_string(), json!("c".repeat(64)));
            let columns: Vec<&str> = copied.keys().map(String::as_str).collect();
            let params: Vec<Value> = columns.iter().map(|name| copied[*name].clone()).collect();
            db.run(
                &format!(
                    "INSERT INTO forecasts({}) VALUES({})",
                    columns.join(","),
                    vec!["?"; columns.len()].join(",")
                ),
                &params,
            )
            .expect("second forecast");
            db.run(
                concat!(
                    "INSERT INTO evidence_reports(id,forecast_id,user_id,url,status,artifact_hash,created_at,",
                    "updated_at) VALUES('er-1','f-reward','user-b','https://www.apple.com/newsroom/x',?,?,?,?)",
                ),
                &[
                    entry["input"]["reportStatus"].clone(),
                    json!("b".repeat(64)),
                    json!(now - 500),
                    json!(now - 500),
                ],
            )
            .expect("report");
            if name == "reward:again" {
                // The second report is inserted before the second attempt, so the `NOT EXISTS`
                // guard is what refuses it rather than the absence of a candidate.
                db.run(
                    concat!(
                        "INSERT INTO evidence_reports(id,forecast_id,user_id,url,status,artifact_hash,created_at,",
                        "updated_at) VALUES('er-2','f-reward','user-c','https://www.apple.com/newsroom/y','held',?,?,?)",
                    ),
                    &[json!("b".repeat(64)), json!(now - 400), json!(now - 400)],
                )
                .expect("second report");
            }
            let hashes: Vec<String> = entry["input"]["evidenceHashes"]
                .as_array()
                .cloned()
                .unwrap_or_default()
                .iter()
                .filter_map(|hash| hash.as_str().map(str::to_string))
                .collect();
            let token = || "t".repeat(32);
            let mut produced =
                block(reward_evidence_report(&db, "f-reward", &hashes, now, &token)).expect("the reward");
            if name == "reward:again" {
                // The vector's second call is the one it records: the first paid, and the guard is
                // what refuses the second after another report qualifies.
                produced = block(reward_evidence_report(&db, "f-reward", &hashes, now, &token)).expect("the reward");
            }
            assert_eq!(
                produced.unwrap_or(Value::Null),
                entry["result"],
                "{name}: a different reward"
            );
            // The account is compared whole; the report and the reward row are compared on the
            // columns this path writes, because the fixture's own rows carry fields it never
            // touches.
            let (accounts, _) = db
                .run("SELECT * FROM point_accounts ORDER BY rowid", &[])
                .expect("accounts");
            assert_eq!(json!(accounts), entry["rows"]["point_accounts"], "{name}: accounts");
            for table in ["point_evidence_rewards", "evidence_reports"] {
                let (rows, _) = db
                    .run(&format!("SELECT * FROM {table} ORDER BY rowid"), &[])
                    .expect("rows");
                let kept = |rows: Vec<Row>| -> Value {
                    rows.iter()
                        .map(|row| {
                            let mut kept = serde_json::Map::new();
                            for key in ["id", "status", "report_id", "user_id", "forecast_id", "amount"] {
                                if let Some(value) = row.get(key) {
                                    kept.insert(key.to_string(), value.clone());
                                }
                            }
                            Value::Object(kept)
                        })
                        .collect::<Vec<Value>>()
                        .into()
                };
                // The fixture's own rows carry fields the reward never touches, so the comparison
                // is on the columns this path writes.
                assert_eq!(
                    kept(rows),
                    kept(
                        entry["rows"][table]
                            .as_array()
                            .cloned()
                            .unwrap_or_default()
                            .iter()
                            .filter_map(Value::as_object)
                            .cloned()
                            .collect()
                    ),
                    "{name}: {table}"
                );
            }
        }
    }
}
