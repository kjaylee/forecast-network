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

// ---------------------------------------------------------------------------- the protocol

pub type BoxFuture<'a, T> = std::pin::Pin<Box<dyn std::future::Future<Output = T> + 'a>>;

/// Python's `Database` protocol, which is four methods wide and deliberately so.
///
/// The worker's D1 session is one implementation and SQLite over the real migrations is the
/// other. That second one is the point: without it every ported query can only be proven where
/// it runs, and a port that is only verified in production is not verified.
pub trait Database {
    fn first<'a>(&'a self, sql: &'a str, params: &'a [Value]) -> BoxFuture<'a, Result<Option<Row>>>;
    fn all<'a>(&'a self, sql: &'a str, params: &'a [Value]) -> BoxFuture<'a, Result<Vec<Row>>>;
    /// Runs a statement and returns its rows, which is what `RETURNING` needs and what the
    /// reference's `execute` returns under `results`.
    fn execute<'a>(&'a self, sql: &'a str, params: &'a [Value]) -> BoxFuture<'a, Result<Vec<Row>>>;
    fn batch<'a>(&'a self, statements: &'a [(String, Vec<Value>)]) -> BoxFuture<'a, Result<Vec<Vec<Row>>>>;
}

pub struct D1<'a>(pub &'a D1DatabaseSession);

impl Database for D1<'_> {
    fn first<'a>(&'a self, sql: &'a str, params: &'a [Value]) -> BoxFuture<'a, Result<Option<Row>>> {
        Box::pin(first(self.0, sql, params))
    }

    fn all<'a>(&'a self, sql: &'a str, params: &'a [Value]) -> BoxFuture<'a, Result<Vec<Row>>> {
        Box::pin(all(self.0, sql, params))
    }

    fn execute<'a>(&'a self, sql: &'a str, params: &'a [Value]) -> BoxFuture<'a, Result<Vec<Row>>> {
        Box::pin(async move { rows_of(&statement(self.0, sql, params)?.all().await?) })
    }

    fn batch<'a>(&'a self, statements: &'a [(String, Vec<Value>)]) -> BoxFuture<'a, Result<Vec<Vec<Row>>>> {
        Box::pin(batch(self.0, statements.to_vec()))
    }
}

/// SQLite over the exact migrations deployed to D1, which is what the Python tests use.
#[cfg(test)]
pub struct Sqlite(std::cell::RefCell<rusqlite::Connection>);

#[cfg(test)]
impl Sqlite {
    pub fn from_migrations() -> Self {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
        let connection = rusqlite::Connection::open_in_memory().expect("in-memory sqlite");
        connection
            .execute_batch("PRAGMA foreign_keys = ON")
            .expect("foreign keys");
        let mut paths: Vec<std::path::PathBuf> = std::fs::read_dir(root.join("apps/web/migrations"))
            .expect("migrations directory")
            .filter_map(|entry| entry.ok().map(|entry| entry.path()))
            .filter(|path| path.extension().is_some_and(|kind| kind == "sql"))
            .collect();
        paths.sort();
        for path in paths {
            let sql = std::fs::read_to_string(&path).unwrap_or_else(|error| panic!("{}: {error}", path.display()));
            connection
                .execute_batch(&sql)
                .unwrap_or_else(|error| panic!("{}: {error}", path.display()));
        }
        Self(std::cell::RefCell::new(connection))
    }

    pub fn run(&self, sql: &str, params: &[Value]) -> rusqlite::Result<(Vec<Row>, usize)> {
        let bound: Vec<rusqlite::types::Value> = params.iter().map(sqlite_value).collect();
        let connection = self.0.borrow();
        let mut statement = connection.prepare(sql)?;
        let columns: Vec<String> = statement.column_names().iter().map(|name| name.to_string()).collect();
        let refs: Vec<&dyn rusqlite::ToSql> = bound.iter().map(|value| value as &dyn rusqlite::ToSql).collect();
        let mut rows = statement.query(refs.as_slice())?;
        let mut out = Vec::new();
        while let Some(row) = rows.next()? {
            let mut object = Map::new();
            for (index, name) in columns.iter().enumerate() {
                object.insert(name.clone(), from_sqlite(row.get_ref(index)?));
            }
            out.push(object);
        }
        let changes = connection.changes() as usize;
        Ok((out, changes))
    }
}

#[cfg(test)]
fn sqlite_value(value: &Value) -> rusqlite::types::Value {
    match value {
        Value::Null => rusqlite::types::Value::Null,
        Value::Bool(flag) => rusqlite::types::Value::Integer(i64::from(*flag)),
        Value::Number(number) => match (number.as_i64(), number.as_f64()) {
            (Some(integer), _) => rusqlite::types::Value::Integer(integer),
            (None, Some(float)) => rusqlite::types::Value::Real(float),
            _ => rusqlite::types::Value::Null,
        },
        Value::String(text) => rusqlite::types::Value::Text(text.clone()),
        other => rusqlite::types::Value::Text(other.to_string()),
    }
}

#[cfg(test)]
fn from_sqlite(value: rusqlite::types::ValueRef<'_>) -> Value {
    use rusqlite::types::ValueRef;
    match value {
        ValueRef::Null => Value::Null,
        ValueRef::Integer(number) => Value::from(number),
        ValueRef::Real(number) => Value::from(number),
        ValueRef::Text(bytes) => Value::from(String::from_utf8_lossy(bytes).into_owned()),
        ValueRef::Blob(bytes) => Value::from(String::from_utf8_lossy(bytes).into_owned()),
    }
}

#[cfg(test)]
impl Database for Sqlite {
    fn first<'a>(&'a self, sql: &'a str, params: &'a [Value]) -> BoxFuture<'a, Result<Option<Row>>> {
        Box::pin(async move {
            let (rows, _) = self
                .run(sql, params)
                .map_err(|error| worker::Error::from(error.to_string()))?;
            Ok(rows.into_iter().next())
        })
    }

    fn all<'a>(&'a self, sql: &'a str, params: &'a [Value]) -> BoxFuture<'a, Result<Vec<Row>>> {
        Box::pin(async move {
            let (rows, _) = self
                .run(sql, params)
                .map_err(|error| worker::Error::from(error.to_string()))?;
            Ok(rows)
        })
    }

    fn execute<'a>(&'a self, sql: &'a str, params: &'a [Value]) -> BoxFuture<'a, Result<Vec<Row>>> {
        Box::pin(async move {
            let (rows, _) = self
                .run(sql, params)
                .map_err(|error| worker::Error::from(error.to_string()))?;
            Ok(rows)
        })
    }

    fn batch<'a>(&'a self, statements: &'a [(String, Vec<Value>)]) -> BoxFuture<'a, Result<Vec<Vec<Row>>>> {
        Box::pin(async move {
            // The reference runs a batch inside one transaction, so a failure part way through
            // leaves nothing behind. A double that committed row by row would pass tests the
            // deployed database fails.
            let connection = self.0.borrow();
            connection
                .execute_batch("BEGIN")
                .map_err(|error| worker::Error::from(error.to_string()))?;
            let mut out = Vec::new();
            for (sql, params) in statements {
                match self.run(sql, params) {
                    Ok((rows, _)) => out.push(rows),
                    Err(error) => {
                        let _ = connection.execute_batch("ROLLBACK");
                        return Err(worker::Error::from(error.to_string()));
                    }
                }
            }
            connection
                .execute_batch("COMMIT")
                .map_err(|error| worker::Error::from(error.to_string()))?;
            Ok(out)
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_double_runs_the_migrations_that_are_deployed() {
        let db = Sqlite::from_migrations();
        let row = futures_lite::future::block_on(db.first("SELECT COUNT(*) AS n FROM forecasts", &[]))
            .expect("query")
            .expect("a row");
        assert_eq!(int(&row, "n"), Some(0));
    }

    #[test]
    fn a_batch_that_fails_part_way_leaves_nothing_behind() {
        // The reference runs a batch in one transaction. A double that committed row by row
        // would pass tests the deployed database fails, which is worse than no double.
        let db = Sqlite::from_migrations();
        let outcome = futures_lite::future::block_on(db.batch(&[
            (
                "INSERT INTO sessions(token_hash,user_id,expires_at) VALUES('a','u',1)".to_string(),
                vec![],
            ),
            (
                "INSERT INTO sessions(token_hash,user_id,expires_at) VALUES('b','u','not a number')".to_string(),
                vec![],
            ),
        ]));
        assert!(outcome.is_err(), "the second statement cannot succeed");
        let row = futures_lite::future::block_on(db.first("SELECT COUNT(*) AS n FROM sessions", &[]))
            .expect("query")
            .expect("a row");
        assert_eq!(
            int(&row, "n"),
            Some(0),
            "the first statement must have been rolled back"
        );
    }
}
