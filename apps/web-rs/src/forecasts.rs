//! `GET /api/forecasts` (`Application.list_forecasts`) with the same SQL, ordering and pagination.

use serde_json::{json, Map, Value};
use worker::*;

use crate::api_response;
use crate::discovery::{self, candidate_sql, integer};
use crate::projections::{card, quality_card_sql};
use crate::routes::{Context, RouteError};

pub const CATEGORIES: [&str; 7] = [
    "TECHNOLOGY",
    "CRYPTO",
    "SCIENCE",
    "ENTERTAINMENT",
    "WORLD",
    "SPORTS",
    "OTHER",
];
const SORTS: [&str; 5] = ["trending", "newest", "ending", "ai-gap", "following"];

pub struct ListQuery {
    pub q: String,
    pub category: String,
    pub sort: String,
    pub cursor: Option<String>,
}

/// `parse_qs` semantics: first value wins, blank values are dropped.
pub fn list_query(url: &Url) -> ListQuery {
    let mut query = ListQuery {
        q: String::new(),
        category: String::new(),
        sort: "trending".to_string(),
        cursor: None,
    };
    let mut seen = std::collections::BTreeSet::new();
    for (key, value) in url.query_pairs() {
        if value.is_empty() || !seen.insert(key.to_string()) {
            continue;
        }
        match key.as_ref() {
            "q" => query.q = value.to_string(),
            "category" => query.category = value.to_string(),
            "sort" => query.sort = value.to_string(),
            "cursor" => query.cursor = Some(value.to_string()),
            _ => {}
        }
    }
    query
}

fn invalid() -> RouteError {
    RouteError::Input
}

fn rows_of(result: &D1Result) -> std::result::Result<Vec<Map<String, Value>>, RouteError> {
    let rows: Vec<Value> = result.results()?;
    Ok(rows.into_iter().filter_map(|row| row.as_object().cloned()).collect())
}

pub async fn list_forecasts(
    context: &Context<'_>,
    user_id: Option<&str>,
    query: &ListQuery,
) -> std::result::Result<Response, RouteError> {
    if query.q.chars().count() > 200 || !SORTS.contains(&query.sort.as_str()) {
        return Err(invalid());
    }
    let offset: i64 = query
        .cursor
        .as_deref()
        .unwrap_or("0")
        .trim()
        .parse()
        .map_err(|_| invalid())?;
    if !(0..=100_000).contains(&offset) {
        return Err(invalid());
    }
    let mut clauses: Vec<String> = vec!["1=1".to_string()];
    let mut params: Vec<Value> = Vec::new();
    let needle = query.q.trim();
    if !needle.is_empty() {
        clauses.push(
            "(f.question LIKE ? ESCAPE '\\' OR json_extract(t.body,'$.question') LIKE ? ESCAPE '\\' \
             OR json_extract(t.body,'$.title') LIKE ? ESCAPE '\\')"
                .to_string(),
        );
        let escaped = needle.replace('\\', "\\\\").replace('%', "\\%").replace('_', "\\_");
        for _ in 0..3 {
            params.push(json!(format!("%{escaped}%")));
        }
    }
    if !query.category.is_empty() && query.category.to_lowercase() != "all" {
        let value = query.category.to_uppercase();
        if !CATEGORIES.contains(&value.as_str()) {
            return Err(invalid());
        }
        clauses.push("f.category=?".to_string());
        params.push(json!(value));
    }
    if query.sort == "following" {
        clauses.push("f.creator_id IN (SELECT creator_id FROM follows WHERE follower_id=?)".to_string());
        params.push(json!(user_id.unwrap_or("")));
    }
    let now = context.now_ms;
    let order = match query.sort.as_str() {
        "trending" => "active_quality DESC,quality_score DESC,discovery_tie,f.id",
        "newest" | "following" => "f.created_at DESC,f.id DESC",
        "ending" => "CASE WHEN active_quality=1 THEN 0 ELSE 1 END,f.close_at ASC,f.id DESC",
        _ => {
            "CASE WHEN probability IS NULL OR f.ai_forecast IS NULL THEN 1 ELSE 0 END,\
              ABS(probability-COALESCE(json_extract(f.ai_forecast,'$.probability'),probability)) DESC,f.id DESC"
        }
    };
    // Translation columns are already projected by candidate_sql's inner join.
    let clauses: Vec<String> = clauses
        .iter()
        .map(|c| c.replace("t.body", "f.display_translation"))
        .collect();
    let base = candidate_sql(now, user_id);
    let daily_sql = format!(
        "WITH inventory AS ({base}), daily AS (SELECT *,ROW_NUMBER() OVER(\
         PARTITION BY category,(creator_finalized_count<5 AND clarity>=7000) \
         ORDER BY quality_score DESC,discovery_tie,id) AS position FROM inventory WHERE active_quality=1) \
         SELECT * FROM daily WHERE position<=5"
    );
    params.push(json!(offset));
    let page_sql = format!(
        "{base} WHERE {} ORDER BY {order} LIMIT 31 OFFSET ?",
        clauses.join(" AND ")
    );
    let page_params: Vec<wasm_bindgen::JsValue> = params.iter().map(js_value).collect();
    let counts_sql = "SELECT COUNT(*) AS total,SUM(CASE WHEN state='OPEN' AND open_at<=? AND close_at>? \
         AND NOT EXISTS(SELECT 1 FROM active_participation_holds h WHERE h.forecast_id=forecasts.id) THEN 1 ELSE 0 END) \
         AS active,(SELECT COUNT(DISTINCT user_id) FROM eligible_user_forecasts) AS participants FROM forecasts";
    let statements = vec![
        context.session.prepare(page_sql).bind(&page_params)?,
        context
            .session
            .prepare(counts_sql)
            .bind(&[js_value(&json!(now)), js_value(&json!(now))])?,
        context.session.prepare(daily_sql),
    ];
    let results = context.session.batch(statements).await?;
    let rows = rows_of(&results[0])?;
    let count_rows = rows_of(&results[1])?;
    let daily = rows_of(&results[2])?;
    let page: Vec<&Map<String, Value>> = rows.iter().take(30).collect();
    let ids: Vec<String> = page
        .iter()
        .filter_map(|r| r.get("id").and_then(Value::as_str).map(str::to_string))
        .collect();
    let cards = rows_of(
        &context
            .session
            .prepare(quality_card_sql(now, &ids).ok_or_else(invalid)?)
            .all()
            .await?,
    )?;
    let mut items = Vec::new();
    for row in &page {
        let id = row.get("id").and_then(Value::as_str).unwrap_or("");
        let Some(card_row) = cards.iter().find(|c| c.get("id").and_then(Value::as_str) == Some(id)) else {
            return Err(RouteError::Worker("card row missing".into()));
        };
        let mut item = card(card_row);
        item["quality"] = discovery::score_forecast(row, now).map_err(|e| RouteError::Worker(e.0.into()))?;
        items.push(item);
    }
    let picks = discovery::recommendations(&daily, now, user_id, 5).map_err(|e| RouteError::Worker(e.0.into()))?;
    let counts = count_rows.first();
    let count = |name: &str| counts.and_then(|c| c.get(name)).and_then(integer).unwrap_or(0);
    let data = json!({
        "items": items,
        "nextCursor": if rows.len() > 30 { Some((offset + 30).to_string()) } else { None },
        "counts": {"total": count("total"), "active": count("active"), "participants": count("participants")},
        "dailyIds": picks.iter().map(|p| p["id"].clone()).collect::<Vec<_>>(),
        "dailyRecommendations": picks.iter().map(|p| json!({"id": p["id"], "quality": p["quality"], "reason": p["recommendationReason"]})).collect::<Vec<_>>(),
        "discoveryMethodologyVersion": discovery::DISCOVERY_VERSION,
    });
    Ok(api_response(data, 200, false)?)
}

fn js_value(value: &Value) -> wasm_bindgen::JsValue {
    match value {
        Value::Null => wasm_bindgen::JsValue::NULL,
        Value::Bool(b) => wasm_bindgen::JsValue::from_bool(*b),
        Value::Number(n) => wasm_bindgen::JsValue::from_f64(n.as_f64().unwrap_or(0.0)),
        Value::String(s) => wasm_bindgen::JsValue::from_str(s),
        other => wasm_bindgen::JsValue::from_str(&other.to_string()),
    }
}
