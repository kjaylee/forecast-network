//! Small D1 helpers: statements bound from JSON values, rows as JSON objects, batches.

use serde_json::{Map, Value};
use wasm_bindgen::JsValue;
use worker::*;

pub type Row = Map<String, Value>;

pub fn js_value(value: &Value) -> JsValue {
    match value {
        Value::Null => JsValue::NULL,
        Value::Bool(b) => JsValue::from_bool(*b),
        Value::Number(n) => JsValue::from_f64(n.as_f64().unwrap_or(0.0)),
        Value::String(s) => JsValue::from_str(s),
        other => JsValue::from_str(&other.to_string()),
    }
}

pub fn statement(session: &D1DatabaseSession, sql: &str, params: &[Value]) -> Result<D1PreparedStatement> {
    let bound: Vec<JsValue> = params.iter().map(js_value).collect();
    session.prepare(sql).bind(&bound)
}

pub fn rows_of(result: &D1Result) -> Result<Vec<Row>> {
    let rows: Vec<Value> = result.results()?;
    Ok(rows.into_iter().filter_map(|row| row.as_object().cloned()).collect())
}

pub async fn all(session: &D1DatabaseSession, sql: &str, params: &[Value]) -> Result<Vec<Row>> {
    rows_of(&statement(session, sql, params)?.all().await?)
}

pub async fn first(session: &D1DatabaseSession, sql: &str, params: &[Value]) -> Result<Option<Row>> {
    let row: Option<Value> = statement(session, sql, params)?.first(None).await?;
    Ok(row.and_then(|r| r.as_object().cloned()))
}

pub async fn batch(session: &D1DatabaseSession, statements: Vec<(String, Vec<Value>)>) -> Result<Vec<Vec<Row>>> {
    let prepared: Vec<D1PreparedStatement> = statements
        .iter()
        .map(|(sql, params)| statement(session, sql, params))
        .collect::<Result<_>>()?;
    let results = session.batch(prepared).await?;
    results.iter().map(rows_of).collect()
}

static NULL: Value = Value::Null;

pub fn get<'a>(row: &'a Row, name: &str) -> &'a Value {
    row.get(name).unwrap_or(&NULL)
}

pub fn text<'a>(row: &'a Row, name: &str) -> Option<&'a str> {
    row.get(name).and_then(Value::as_str)
}

pub fn int(row: &Row, name: &str) -> Option<i64> {
    row.get(name).and_then(crate::discovery::integer)
}
