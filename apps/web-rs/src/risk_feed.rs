//! Durable signed probability publications. HTTP authorization belongs to the caller.
//!
//! Nothing here decides who may publish: `approve_binding` says so in its own docstring, and it is
//! the reason this module can be ported at all without smuggling an authorization decision into a
//! storage layer. What it does decide is what a *signed* feed may contain — an immutable binding
//! matched against the published specification window, signals drawn only from eligible eligible
//! participants, and a compare-and-set batch so a publication that lost its race is not written.

use crate::db::{self, Database, Row};
use crate::reputation::qualified_cohorts;
use crate::wallets::BoxFuture;
use forecast_domain::risk_feed::{signing_bytes, RiskFeedBinding, RiskFeedPayload, RiskFeedSignal, SignedRiskFeed};
use forecast_domain::{canonical_bytes, content_hash, require, Record, ValidationError};
use serde_json::{json, Value};

/// `SNAPSHOT_SQL`, reused verbatim in the compare-and-set guard.
///
/// The guard re-evaluates the whole snapshot rather than a digest of it, so every eligibility or
/// body change that could alter the signed aggregate invalidates the publication — which is what
/// makes an asynchronous signer safe. Seven parameters, in order.
pub const SNAPSHOT_SQL: &str = concat!(
    "SELECT json_object('id',f.id,'specification_hash',f.specification_hash,'category',f.category,\n",
    " 'state',f.state,'open_at',f.open_at,'close_at',f.close_at,'revision',f.revision,\n",
    " 'ai', (SELECT body FROM artifacts WHERE hash=json_extract(f.ai_forecast,'$.artifactHash')),\n",
    " 'submissions',json((SELECT json_group_array(json_object('user_id',u.user_id,'probability',u.yes_probability,\n",
    "   'submitted_at',u.submitted_at,'revision',u.revision,'body',u.body)) FROM\n",
    "   (SELECT * FROM eligible_user_forecasts WHERE forecast_id=f.id AND submitted_at>=? AND submitted_at<=?\n",
    "    ORDER BY user_id) u)),\n",
    " 'history',json((SELECT json_group_array(json_object('user_id',h.user_id,'forecast_id',h.forecast_id,\n",
    "  'category',h.category,'probability',h.probability,'outcome',h.outcome,'state',h.state,\n",
    "  'finalized_outcome',h.finalized_outcome,'submitted_at',h.submitted_at,'finalized_at',h.finalized_at,\n",
    "  'eligibility_at',h.eligibility_at,'eligible',h.eligible)) FROM\n",
    "  (SELECT * FROM forecast_quality_history WHERE finalized_at<=? AND eligibility_at<=?\n",
    "   AND user_id IN(SELECT user_id FROM eligible_user_forecasts WHERE forecast_id=f.id)\n",
    "   ORDER BY user_id,forecast_id LIMIT 100001) h))) AS snapshot\n",
    "FROM forecasts f WHERE f.id=? AND f.state='OPEN' AND f.open_at<=? AND f.close_at>?\n",
    " AND NOT EXISTS(SELECT 1 FROM active_participation_holds h WHERE h.forecast_id=f.id)\n",
    " AND NOT EXISTS(SELECT 1 FROM forecast_eligibility_decisions d WHERE d.forecast_id=f.id\n",
    "  AND NOT EXISTS(SELECT 1 FROM forecast_eligibility_completions c WHERE c.decision_id=d.id))\n"
);

/// The reference raises `ValidationError` from every `require`, and the caller maps it to a server
/// fault rather than a request fault: a feed that cannot be assembled is not a request to answer
/// differently.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FeedError(pub String);

impl From<ValidationError> for FeedError {
    fn from(error: ValidationError) -> Self {
        Self(error.to_string())
    }
}

fn require_that(condition: bool, message: &str) -> Result<(), FeedError> {
    require(condition, message).map_err(FeedError::from)
}

/// `dumps(record)` with the commitment rule the domain uses.
fn dumps<T: serde::Serialize>(value: &T) -> String {
    String::from_utf8(canonical_bytes(value).unwrap_or_default()).unwrap_or_default()
}

/// `_signals`. The conservative group is not a judgement call: every channel that consumes the
/// same service observations shares one upstream family, so AI and crowd never manufacture an
/// independent quorum between them.
pub fn signals(
    binding: &RiskFeedBinding,
    snapshot: &Value,
    now_ms: i64,
    window_ms: i64,
) -> Result<Vec<RiskFeedSignal>, FeedError> {
    require_that(
        snapshot["specification_hash"] == json!(binding.specification_hash)
            && snapshot["category"] == json!(binding.category),
        "immutable binding changed",
    )?;
    let rows = snapshot["submissions"].as_array().cloned().unwrap_or_default();
    require_that(rows.len() <= 1_000_000, "invalid source population")?;
    let history = snapshot["history"].as_array().cloned().unwrap_or_default();
    require_that(history.len() <= 100_000, "qualification history exceeds source bound")?;
    let cohorts = qualified_cohorts(
        &history.iter().map(row_of).collect::<Vec<Row>>(),
        &binding.forecast_id,
        &binding.category,
        now_ms,
    )
    .map_err(|error| FeedError(error.0))?;
    let top: Vec<String> = cohorts["top"]
        .as_array()
        .map(|ids| ids.iter().filter_map(Value::as_str).map(str::to_string).collect())
        .unwrap_or_default();
    let dependence = content_hash(&json!({"upstream": "forecast-network-service-v1"})).map_err(FeedError::from)?;
    let mut out = Vec::new();
    if !rows.is_empty() {
        for row in &rows {
            require_that(
                row["probability"].as_i64().is_some_and(|p| (0..=100).contains(&p)),
                "malformed eligible probability",
            )?;
        }
        let count = rows.len() as i64;
        let total: i64 = rows.iter().filter_map(|row| row["probability"].as_i64()).sum();
        // `(sum * 200 + count) // (2 * count)`: integer arithmetic, and the rounding direction is
        // part of the signed value.
        out.push(signal(
            binding,
            "crowd",
            (total * 200 + count) / (2 * count),
            count,
            rows.iter().filter_map(|row| row["submitted_at"].as_i64()).max(),
            &content_hash(&json!({"binding": binding, "eligible_rows": rows})).map_err(FeedError::from)?,
            &dependence,
            "provisional",
        )?);
    }
    let top_rows: Vec<Value> = rows
        .iter()
        .filter(|row| {
            row["user_id"]
                .as_str()
                .is_some_and(|id| top.iter().any(|user| user == id))
        })
        .cloned()
        .collect();
    if !top_rows.is_empty() {
        let count = top_rows.len() as i64;
        let total: i64 = top_rows.iter().filter_map(|row| row["probability"].as_i64()).sum();
        out.push(signal(
            binding,
            "top",
            (total * 200 + count) / (2 * count),
            count,
            top_rows.iter().filter_map(|row| row["submitted_at"].as_i64()).max(),
            &content_hash(&json!({
                "binding": binding, "eligible_rows": top_rows, "qualification_history": history,
            }))
            .map_err(FeedError::from)?,
            &dependence,
            "provisional",
        )?);
    }
    if !snapshot["ai"].is_null() {
        let ai: Value = serde_json::from_str(snapshot["ai"].as_str().unwrap_or(""))
            .map_err(|_| FeedError("invalid retained AI artifact".to_string()))?;
        require_that(ai.is_object(), "invalid retained AI artifact")?;
        let probability = ai["yes_probability_bp"].as_i64();
        let observed = ai["as_of_ms"].as_i64();
        require_that(
            ai["specification_hash"] == json!(binding.specification_hash)
                && probability.is_some_and(|p| (0..=10000).contains(&p))
                && observed.is_some(),
            "invalid AI probability provenance",
        )?;
        // A stale artifact is not an error, it is silence: the window is what makes a risk feed a
        // statement about now rather than about whenever the model last ran.
        let (probability, observed) = (probability.unwrap(), observed.unwrap());
        if binding.valid_from_ms.max(now_ms - window_ms) <= observed && observed <= now_ms {
            out.push(signal(
                binding,
                "ai",
                probability,
                1,
                Some(observed),
                &content_hash(&ai).map_err(FeedError::from)?,
                &dependence,
                "provisional",
            )?);
        }
    }
    Ok(out)
}

/// One signal, with the fields every source shares. The signature is recorded by the caller.
#[allow(clippy::too_many_arguments)]
fn signal(
    binding: &RiskFeedBinding,
    source: &str,
    probability_bp: i64,
    sample_count: i64,
    observed_at_ms: Option<i64>,
    evidence_hash: &str,
    dependence_group: &str,
    calibration_status: &str,
) -> Result<RiskFeedSignal, FeedError> {
    let value = RiskFeedSignal {
        schema_version: 1,
        binding_id: binding.binding_id.clone(),
        source: source.to_string(),
        probability_bp,
        confidence_bp: (3000 + sample_count * 100).min(9000),
        sample_count,
        units: "basis-points".to_string(),
        observed_at_ms: observed_at_ms.unwrap_or(0),
        evidence_hash: evidence_hash.to_string(),
        dependence_group: dependence_group.to_string(),
        calibration_status: calibration_status.to_string(),
    };
    value.validate().map_err(FeedError::from)?;
    Ok(value)
}

/// A JSON object becomes the flat row the reputation helpers read. The view already returns a
/// JSON object per history entry, so the columns keep their names.
fn row_of(value: &Value) -> Row {
    value.as_object().cloned().unwrap_or_default()
}

/// `approved_by` is only checked as text: whether the caller *is* an admin is the route's business,
/// and this says so rather than growing a second authorization path.
fn bounded_actor(value: &str, limit: usize) -> bool {
    !value.trim().is_empty() && value.len() <= limit
}

/// `re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", feed_id)`.
fn valid_feed_id(feed_id: &str) -> bool {
    let mut characters = feed_id.chars();
    match characters.next() {
        Some(first) if first.is_ascii_alphanumeric() => {}
        _ => return false,
    }
    let rest = characters.collect::<String>();
    rest.len() <= 127
        && rest
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | ':' | '-'))
}

/// `approve_binding`. The binding has to match the *published* specification window, not merely
/// name a forecast: a binding approved against a different window would sign probabilities the
/// question never asked for.
pub async fn approve_binding(
    db: &dyn Database,
    feed_id: &str,
    binding: &RiskFeedBinding,
    approved_by: &str,
    now_ms: i64,
) -> Result<(), FeedError> {
    // `binding.__post_init__()`: the domain record validates itself, and so does the port.
    binding.validate().map_err(FeedError::from)?;
    require_that(bounded_actor(approved_by, 128), "authenticated approver required")?;
    require_that(
        binding.valid_from_ms <= now_ms && now_ms < binding.valid_until_ms,
        "binding is not current",
    )?;
    let row = db
        .first(
            "SELECT specification_hash,category,open_at,close_at,state FROM forecasts WHERE id=?",
            &[json!(binding.forecast_id)],
        )
        .await
        .map_err(|_| FeedError("feed storage unavailable".to_string()))?;
    require_that(row.is_some(), "unknown canonical forecast")?;
    let row = row.unwrap();
    require_that(
        db::text(&row, "specification_hash") == Some(binding.specification_hash.as_str())
            && db::text(&row, "category") == Some(binding.category.as_str())
            && db::text(&row, "state") == Some("OPEN")
            && db::int(&row, "open_at").is_some_and(|open| open <= binding.valid_from_ms)
            && db::int(&row, "close_at").is_some_and(|close| binding.valid_until_ms <= close),
        "binding does not match the published specification window",
    )?;
    require_that(valid_feed_id(feed_id), "invalid feed ID")?;
    db.batch(&[
        (
            "INSERT INTO risk_feed_bindings(binding_id,feed_id,binding_json,approved_by,approved_at) VALUES(?,?,?,?,?)"
                .to_string(),
            vec![
                json!(binding.binding_id),
                json!(feed_id),
                json!(dumps(binding)),
                json!(approved_by),
                json!(now_ms),
            ],
        ),
        (
            "INSERT INTO risk_feed_heads(feed_id,sequence) VALUES(?,0) ON CONFLICT(feed_id) DO NOTHING".to_string(),
            vec![json!(feed_id)],
        ),
    ])
    .await
    .map_err(|_| FeedError("feed storage unavailable".to_string()))?;
    Ok(())
}

/// `active_bindings`: the current immutable approvals, for publishing or for an operator-owned AI
/// refresh. Historical approvals stay retained; only these exact identities may be refreshed.
pub async fn active_bindings(db: &dyn Database, feed_id: &str, now_ms: i64) -> Result<Vec<RiskFeedBinding>, FeedError> {
    require_that((0..(1i64 << 53)).contains(&now_ms), "invalid binding selection time")?;
    let rows = db
        .all(
            concat!(
                "SELECT binding_json FROM risk_feed_bindings b WHERE feed_id=? ",
                "AND json_extract(binding_json,'$.valid_from_ms')<=? ",
                "AND json_extract(binding_json,'$.valid_until_ms')>? ",
                "AND NOT EXISTS(SELECT 1 FROM risk_feed_binding_revocations r WHERE r.binding_id=b.binding_id) ",
                "ORDER BY binding_id LIMIT 13",
            ),
            &[json!(feed_id), json!(now_ms), json!(now_ms)],
        )
        .await
        .map_err(|_| FeedError("feed storage unavailable".to_string()))?;
    // The limit is thirteen so that twelve can be stored and the thirteenth is the evidence of
    // overflow rather than a silent truncation.
    require_that(rows.len() <= 12, "feed binding capacity exceeded")?;
    rows.iter()
        .map(|row| RiskFeedBinding::from_json(db::text(row, "binding_json").unwrap_or("")).map_err(FeedError::from))
        .collect()
}

/// `publish_feed`. Ten arguments because the key material, the weights and the observation
/// window are all the caller's to supply — the module signs what it is given.
#[allow(clippy::too_many_arguments)]
///
/// The signer is injected and awaited, which is the whole reason the guard batch exists: between
/// reading the snapshots and writing the envelope, a hold can be placed, a binding revoked, or an
/// eligibility decision completed — and the guard re-evaluates each snapshot in SQL so a
/// publication that lost that race is refused rather than signed into being.
pub async fn publish_feed(
    db: &dyn Database,
    feed_id: &str,
    genesis_hash: &str,
    key_id: &str,
    public_key_hex: &str,
    signer: &dyn Fn(Vec<u8>) -> BoxFuture<Result<Vec<u8>, ()>>,
    now_ms: i64,
    weight_set_hash: &str,
    weight_set_version: &str,
    window_ms: i64,
) -> Result<SignedRiskFeed, FeedError> {
    require_that(
        (1..=31_536_000_000).contains(&window_ms),
        "invalid feed observation window",
    )?;
    let head = db
        .first(
            concat!(
                "SELECT h.sequence,COALESCE((SELECT MAX(created_at) FROM risk_feed_publications p ",
                "WHERE p.feed_id=h.feed_id),0) last_time FROM risk_feed_heads h WHERE feed_id=?",
            ),
            &[json!(feed_id)],
        )
        .await
        .map_err(|_| FeedError("feed storage unavailable".to_string()))?;
    require_that(head.is_some(), "feed has no approved canonical bindings")?;
    let head = head.unwrap();
    let (sequence, last_time) = (
        db::int(&head, "sequence").unwrap_or(0),
        db::int(&head, "last_time").unwrap_or(0),
    );
    require_that(now_ms >= last_time, "publication time moved backwards")?;
    let active = active_bindings(db, feed_id, now_ms).await?;
    let mut bindings: Vec<RiskFeedBinding> = Vec::new();
    let mut collected: Vec<RiskFeedSignal> = Vec::new();
    let mut snapshots: Vec<(Vec<Value>, String)> = Vec::new();
    for binding in &active {
        let params: Vec<Value> = vec![
            json!(binding.valid_from_ms.max(now_ms - window_ms)),
            json!(now_ms),
            json!(now_ms),
            json!(now_ms),
            json!(binding.forecast_id),
            json!(now_ms),
            json!(now_ms),
        ];
        let found = db
            .first(SNAPSHOT_SQL, &params)
            .await
            .map_err(|_| FeedError("feed storage unavailable".to_string()))?;
        let Some(found) = found else {
            continue;
        };
        let snapshot = db::get(&found, "snapshot").as_str().unwrap_or("").to_string();
        let parsed: Value = serde_json::from_str(&snapshot).unwrap_or(Value::Null);
        let values = signals(binding, &parsed, now_ms, window_ms)?;
        if !values.is_empty() {
            bindings.push(binding.clone());
            collected.extend(values);
            snapshots.push((params, snapshot));
        }
    }
    require_that(!collected.is_empty(), "no eligible current canonical risk observations")?;
    // `sorted(..., key=(binding_id, source))`: the signature covers a determinate order, so an
    // unstable one would produce a different signature for the same set of signals.
    collected.sort_by(|a, b| (&a.binding_id, &a.source).cmp(&(&b.binding_id, &b.source)));
    let payload = RiskFeedPayload {
        schema_version: 1,
        purpose: "forecast-risk-feed-v1".to_string(),
        genesis_hash: genesis_hash.to_string(),
        feed_id: feed_id.to_string(),
        sequence: sequence + 1,
        key_id: key_id.to_string(),
        issued_at_ms: now_ms,
        expires_at_ms: bindings
            .iter()
            .map(|binding| binding.valid_until_ms)
            .fold(now_ms + 120_000, i64::min),
        bindings: bindings.clone(),
        signals: collected,
        weight_set_hash: weight_set_hash.to_string(),
        weight_set_version: weight_set_version.to_string(),
    };
    payload.validate().map_err(FeedError::from)?;
    let signature = signer(signing_bytes(&payload).map_err(FeedError::from)?)
        .await
        .map_err(|_| FeedError("risk feed signer unavailable".to_string()))?;
    require_that(signature.len() == 64, "signer returned invalid Ed25519 signature")?;
    let envelope = SignedRiskFeed {
        schema_version: 1,
        payload,
        public_key_hex: public_key_hex.to_string(),
        signature_hex: hex::encode(&signature),
    };
    let digest = content_hash(&envelope.payload).map_err(FeedError::from)?;
    let guard = format!("risk-feed:{digest}");
    let mut statements: Vec<(String, Vec<Value>)> = vec![(
        concat!(
            "INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN EXISTS(SELECT 1 FROM risk_feed_heads ",
            "WHERE feed_id=? AND sequence=?) THEN 1 ELSE 0 END",
        )
        .to_string(),
        vec![json!(guard), json!(feed_id), json!(sequence)],
    )];
    for binding in &bindings {
        statements.push((
            concat!(
                "INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN NOT EXISTS(",
                "SELECT 1 FROM risk_feed_binding_revocations WHERE binding_id=?) THEN 1 ELSE 0 END",
            )
            .to_string(),
            vec![
                json!(format!("{guard}:binding:{}", binding.binding_id)),
                json!(binding.binding_id),
            ],
        ));
    }
    for (index, (params, snapshot)) in snapshots.iter().enumerate() {
        let mut bound = vec![json!(format!("{guard}:{index}"))];
        bound.extend(params.iter().cloned());
        bound.push(json!(snapshot));
        statements.push((
            format!("INSERT INTO mutation_guards(token,valid) SELECT ?,CASE WHEN ({SNAPSHOT_SQL})=? THEN 1 ELSE 0 END"),
            bound,
        ));
    }
    statements.extend([
        (
            concat!(
                "INSERT INTO risk_feed_publications(feed_id,sequence,payload_hash,envelope_json,created_at) ",
                "VALUES(?,?,?,?,?)",
            )
            .to_string(),
            vec![
                json!(feed_id),
                json!(sequence + 1),
                json!(digest),
                json!(dumps(&envelope)),
                json!(now_ms),
            ],
        ),
        (
            "UPDATE risk_feed_heads SET sequence=? WHERE feed_id=? AND sequence=?".to_string(),
            vec![json!(sequence + 1), json!(feed_id), json!(sequence)],
        ),
        (
            "DELETE FROM mutation_guards WHERE token=? OR token LIKE ?".to_string(),
            vec![json!(guard), json!(format!("{guard}:%"))],
        ),
    ]);
    db.batch(&statements)
        .await
        .map_err(|_| FeedError("risk feed publication did not persist".to_string()))?;
    Ok(envelope)
}

/// `latest_feed`: the newest publication, or `None` for a feed that has never published.
pub async fn latest_feed(db: &dyn Database, feed_id: &str) -> Result<Option<SignedRiskFeed>, FeedError> {
    let row = db
        .first(
            "SELECT envelope_json FROM risk_feed_publications WHERE feed_id=? ORDER BY sequence DESC LIMIT 1",
            &[json!(feed_id)],
        )
        .await
        .map_err(|_| FeedError("feed storage unavailable".to_string()))?;
    match row {
        Some(row) => Ok(Some(
            SignedRiskFeed::from_json(db::text(&row, "envelope_json").unwrap_or("")).map_err(FeedError::from)?,
        )),
        None => Ok(None),
    }
}

/// `revoke_binding`: an append-only revocation. Published evidence stays immutable, so a revoked
/// binding stops being *publishable* without rewriting what was already signed under it.
pub async fn revoke_binding(
    db: &dyn Database,
    binding_id: &str,
    revoked_by: &str,
    now_ms: i64,
    reason: &str,
) -> Result<(), FeedError> {
    require_that(
        bounded_actor(revoked_by, 128) && (1..=1000).contains(&reason.trim().len()),
        "revocation requires authenticated actor and bounded reason",
    )?;
    db.execute(
        "INSERT INTO risk_feed_binding_revocations(binding_id,revoked_by,revoked_at,reason) VALUES(?,?,?,?)",
        &[json!(binding_id), json!(revoked_by), json!(now_ms), json!(reason)],
    )
    .await
    .map_err(|_| FeedError("feed storage unavailable".to_string()))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::Sqlite;
    use sha2::{Digest, Sha256};
    use std::cell::RefCell;

    const GENESIS: &str = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1";
    const KEY_ID: &str = "test-key";
    const ACTOR: &str = "authenticated-admin";

    fn block<F: std::future::Future>(future: F) -> F::Output {
        futures_lite::future::block_on(future)
    }

    fn golden() -> Value {
        let path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../tests/golden/risk-feed-publication-golden.json");
        serde_json::from_str(&std::fs::read_to_string(&path).expect("risk feed golden")).expect("json")
    }

    /// The same synthetic signer the vector used: sixty-four bytes that depend on every byte of
    /// the input, so the recorded signature is a check on the text rather than on the envelope.
    fn sign(data: &[u8]) -> Vec<u8> {
        let first = Sha256::digest(data);
        let mut second = Sha256::new();
        second.update(b"second:");
        second.update(data);
        [first.to_vec(), second.finalize().to_vec()].concat()
    }

    /// Restore a table from the vector, with the columns the vector recorded.
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

    fn rows_of(db: &Sqlite, table: &str) -> Value {
        let (rows, _) = db
            .run(&format!("SELECT * FROM {table} ORDER BY rowid"), &[])
            .expect("rows");
        json!(rows)
    }

    /// The envelope as the vector records it — every field of the record, including the schema
    /// version the port would otherwise be free to drop.
    fn payload_of(envelope: SignedRiskFeed) -> Value {
        serde_json::to_value(&envelope).expect("an envelope is serializable")
    }

    /// `active_bindings` as the vector records it.
    fn active_of(db: &Sqlite, now: i64) -> Result<Value, FeedError> {
        block(active_bindings(db, "risk", now)).map(|bindings| {
            json!(bindings
                .iter()
                .map(|b| serde_json::to_value(b).unwrap())
                .collect::<Vec<_>>())
        })
    }

    fn check(calls: &[Value], index: &mut usize, produced: Result<Value, FeedError>) {
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
                let expected = entry["error"]["message"].as_str().unwrap();
                assert_eq!(error.0, expected, "{name}: a different refusal");
            }
        }
        *index += 1;
    }

    #[test]
    fn the_reference_publication_lifecycle_is_reproduced_call_for_call() {
        let document = golden();
        let calls = document["calls"].as_array().expect("calls").clone();
        let db = Sqlite::from_migrations();
        // A fixed order, because the vector stores its rows as an object and JSON objects are
        // unordered: restoring `forecasts` before `users` fails a foreign key that the reference
        // never saw fail.
        // `user_forecasts` is not here: the vector recorded it *after* the submission the app
        // made, and the first publication is refused precisely because it did not exist yet.
        for table in ["users", "forecasts", "artifacts"] {
            restore(&db, table, document["rows"][table].as_array().expect("rows"));
        }
        let forecast = document["rows"]["forecasts"][0].as_object().expect("a forecast row");
        let forecast_id = db::text(forecast, "id").unwrap().to_string();
        let category = db::text(forecast, "category").unwrap().to_string();
        // The reference publishes at the clock the app held when it built the forecast, which
        // is the forecast's own `open_at`.
        let now = db::int(forecast, "open_at").unwrap();
        let binding = RiskFeedBinding {
            schema_version: 1,
            binding_id: "canonical-001".to_string(),
            version: "canonical-risk-binding-v1".to_string(),
            forecast_id: forecast_id.clone(),
            specification_hash: db::text(forecast, "specification_hash").unwrap().to_string(),
            channel: "depegRisk1d".to_string(),
            horizon_hours: 24,
            asset: "USDC".to_string(),
            category: category.clone(),
            valid_from_ms: db::int(forecast, "open_at").unwrap(),
            valid_until_ms: db::int(forecast, "close_at").unwrap(),
        };
        let signed: RefCell<Vec<Value>> = RefCell::new(Vec::new());
        let signer = |data: Vec<u8>| -> BoxFuture<Result<Vec<u8>, ()>> {
            signed.borrow_mut().push(json!(hex::encode(&data)));
            Box::pin(async move { Ok(sign(&data)) })
        };
        let mut index = 0usize;

        // --- approve: the binding has to match the published specification window.
        check(
            &calls,
            &mut index,
            block(approve_binding(&db, "risk", &binding, "   ", now)).map(|_| Value::Null),
        );
        check(
            &calls,
            &mut index,
            block(approve_binding(&db, "risk", &binding, &"x".repeat(129), now)).map(|_| Value::Null),
        );
        check(
            &calls,
            &mut index,
            block(approve_binding(
                &db,
                "risk",
                &binding,
                "admin",
                binding.valid_from_ms - 1,
            ))
            .map(|_| Value::Null),
        );
        check(
            &calls,
            &mut index,
            block(approve_binding(&db, "risk feed", &binding, "admin", now)).map(|_| Value::Null),
        );
        let unknown = RiskFeedBinding {
            forecast_id: "no-such-forecast".to_string(),
            ..binding.clone()
        };
        check(
            &calls,
            &mut index,
            block(approve_binding(&db, "risk", &unknown, "admin", now)).map(|_| Value::Null),
        );
        let wrong = RiskFeedBinding {
            specification_hash: "b".repeat(64),
            ..binding.clone()
        };
        check(
            &calls,
            &mut index,
            block(approve_binding(&db, "risk", &wrong, "admin", now)).map(|_| Value::Null),
        );
        check(
            &calls,
            &mut index,
            block(approve_binding(&db, "risk", &binding, ACTOR, now)).map(|_| Value::Null),
        );
        assert_eq!(index, 7, "the approval refusals are replayed");

        // --- publish: nothing is eligible until a submission exists.
        let publish = |window: i64, at: i64| -> Result<Value, FeedError> {
            block(publish_feed(
                &db,
                "risk",
                GENESIS,
                KEY_ID,
                &"a".repeat(64),
                &signer,
                at,
                &"d".repeat(64),
                "source-calibration-v1",
                window,
            ))
            .map(payload_of)
        };
        check(&calls, &mut index, publish(86_400_000, now));
        check(&calls, &mut index, publish(0, now));
        check(&calls, &mut index, publish(31_536_000_001, now));

        // The one eligible submission the vector recorded. The fixture is restored rather than
        // resubmitted, because the submission path is the application's and not this module's.
        let submission = &document["rows"]["user_forecasts"][0];
        restore(&db, "user_forecasts", std::slice::from_ref(submission));
        let first = publish(86_400_000, now);
        assert_eq!(first.as_ref().unwrap()["payload"]["sequence"], json!(1));
        check(&calls, &mut index, first);
        let second = publish(86_400_000, now);
        assert_eq!(second.as_ref().unwrap()["payload"]["sequence"], json!(2));
        check(&calls, &mut index, second);
        let latest = block(latest_feed(&db, "risk")).map(|envelope| match envelope {
            Some(envelope) => payload_of(envelope),
            None => Value::Null,
        });
        // The vector records a stored publication as `null` when there is none — a missing feed is
        // not an error, and a port that answered with an error would be answering a different
        // question.
        check(&calls, &mut index, latest);
        check(&calls, &mut index, active_of(&db, now));

        // --- the snapshot has to hold across the await, so pausing the forecast refuses the next
        // publication rather than signing a stale one.
        db.run("UPDATE forecasts SET state='PAUSED' WHERE id=?", &[json!(forecast_id)])
            .expect("pause");
        check(&calls, &mut index, publish(86_400_000, now));
        db.run("UPDATE forecasts SET state='OPEN' WHERE id=?", &[json!(forecast_id)])
            .expect("resume");

        // --- revoke, and confirm the revocation is what stops the next publication.
        check(
            &calls,
            &mut index,
            block(revoke_binding(&db, &binding.binding_id, "", now, "operator")).map(|_| Value::Null),
        );
        check(
            &calls,
            &mut index,
            block(revoke_binding(
                &db,
                &binding.binding_id,
                "admin",
                now,
                &"x".repeat(1001),
            ))
            .map(|_| Value::Null),
        );
        check(
            &calls,
            &mut index,
            block(revoke_binding(
                &db,
                &binding.binding_id,
                ACTOR,
                now,
                "operator withdrew the mapped question",
            ))
            .map(|_| Value::Null),
        );
        check(&calls, &mut index, publish(86_400_000, now));
        check(&calls, &mut index, active_of(&db, now));

        assert_eq!(index, calls.len(), "every recorded call is replayed");
        // The signature is over the text, not the outcome: a port that assembled the payload
        // correctly and signed something else would match the envelope and fail here.
        assert_eq!(
            json!(*signed.borrow()),
            document["signed"],
            "the signer saw different bytes"
        );
        for (table, expected) in document["rows"].as_object().expect("rows") {
            if table == "users" || table == "forecasts" || table == "user_forecasts" || table == "artifacts" {
                continue; // Restored above; the lifecycle does not write them.
            }
            assert_eq!(rows_of(&db, table), *expected, "{table}: different rows");
        }
        assert!(
            rows_of(&db, "mutation_guards").as_array().unwrap().is_empty(),
            "every guard is cleared"
        );
    }
}
