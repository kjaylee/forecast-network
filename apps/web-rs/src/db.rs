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

// ------------------------------------------------------------------- what a query costs

// D1's free tier grants 5,000,000 rows read per day, and on 2026-09-22 the service was
// spending them by 06:29Z: the feed then answered `stale` until midnight. Nothing in the
// Worker said which statement was doing it, because a row count was never reported — only
// a duration, and a scan that is fast is still a scan. Every D1 result carries
// `meta.rows_read`, so the tick can say what it spent in the same line it says what it
// took, and the budget becomes something the operator's own log shows every minute.
//
// One isolate runs one request at a time, so a counter here is that request's counter. It
// is reset by whoever is about to measure, and read as a delta around each phase.
//
// `first()` is absent from this count on purpose: D1's `.first()` resolves to the row
// itself and carries no meta, so it contributes to `queries` and not to `rows`. If the
// phases ever fail to account for the day, that gap is where to look next.
thread_local! {
    static ROWS_READ: std::cell::Cell<i64> = const { std::cell::Cell::new(0) };
    static QUERIES: std::cell::Cell<i64> = const { std::cell::Cell::new(0) };
}

fn note(result: &D1Result) {
    let read = result
        .meta()
        .ok()
        .flatten()
        .and_then(|meta| meta.rows_read)
        .unwrap_or(0);
    ROWS_READ.with(|total| total.set(total.get() + read as i64));
}

fn note_query() {
    QUERIES.with(|total| total.set(total.get() + 1));
}

/// Rows this isolate has read since the counter was last reset, and statements it has run.
pub fn usage() -> (i64, i64) {
    (ROWS_READ.with(|t| t.get()), QUERIES.with(|t| t.get()))
}

// ------------------------------------------------- which path spends the day, read from here
//
// The per-request delta was already being logged, and that log goes where only a dashboard can
// read it — which is the same mistake as asking the dashboard in the first place. This keeps
// the totals in the isolate and serves them from an operator route, so the host that already
// polls every five minutes writes them to its own log file and the question is answered from
// a machine we own.
//
// One isolate's view, and it says so: `sinceMs` is how long this isolate has been answering.
// Cloudflare runs many, and a path heavy enough to matter shows up in any of them within
// minutes — a path that reads three hundred rows a request cannot hide behind sampling.

/// At most this many distinct paths are tracked. Paths are normalised, so this is a bound on
/// the route table, not on traffic; a burst of unknown paths cannot grow it without limit.
const TRACKED_PATHS: usize = 64;

thread_local! {
    static BY_PATH: std::cell::RefCell<std::collections::BTreeMap<String, [i64; 3]>> =
        const { std::cell::RefCell::new(std::collections::BTreeMap::new()) };
    static SINCE: std::cell::Cell<i64> = const { std::cell::Cell::new(0) };
}

/// The path with its identifiers replaced, so one route is one row rather than one per forecast.
///
/// The rule is what a *route word* looks like in this service rather than what an identifier
/// looks like, because identifiers vary and route words do not: either all lowercase letters,
/// or a version — two or three characters of letters then digits. That keeps `v2` and `d1` as
/// themselves, where "carries a digit" would have turned `/api/risk/v2/feeds/…` into
/// `/api/risk/{id}/feeds/{id}` and merged every versioned route with every other.
///
/// A two-character identifier is indistinguishable from a version and is read as a version.
/// Nothing separates `v2` from `x9` by shape, and the cost of choosing wrong is one row in a
/// diagnostic, so it is chosen in favour of the routes that exist.
fn normalised(path: &str) -> String {
    let mut out = String::with_capacity(path.len());
    for segment in path.split('/').skip(1) {
        out.push('/');
        let letters = segment.chars().take_while(char::is_ascii_lowercase).count();
        // Route words are short: the longest this service serves is `participation`, at 13.
        // Without the cap a 32-character lowercase hash reads as a word and every profile
        // gets its own row, which is the failure this function exists to prevent.
        let word = letters == segment.len() && (1..=16).contains(&letters);
        let version = letters > 0 && segment.len() <= 3 && segment[letters..].chars().all(|c| c.is_ascii_digit());
        out.push_str(if word || version { segment } else { "{id}" });
    }
    out
}

/// Record what one request read, against the route it took.
pub fn record(path: &str, rows: i64, queries: i64, now_ms: i64) {
    SINCE.with(|since| {
        if since.get() == 0 {
            since.set(now_ms);
        }
    });
    BY_PATH.with(|by_path| {
        let mut by_path = by_path.borrow_mut();
        let key = normalised(path);
        if !by_path.contains_key(&key) && by_path.len() >= TRACKED_PATHS {
            return;
        }
        let entry = by_path.entry(key).or_insert([0; 3]);
        entry[0] += rows;
        entry[1] += queries;
        entry[2] += 1;
    });
}

/// What this isolate has read, by route, heaviest first.
pub fn report(now_ms: i64) -> Value {
    let since = SINCE.with(|since| since.get());
    let mut paths: Vec<Value> = BY_PATH.with(|by_path| {
        by_path
            .borrow()
            .iter()
            .map(|(path, [rows, queries, requests])| {
                serde_json::json!({
                    "path": path, "rows": rows, "queries": queries, "requests": requests,
                    // The number that decides where to put an index.
                    "rowsPerRequest": if *requests > 0 { rows / requests } else { 0 },
                })
            })
            .collect()
    });
    paths.sort_by_key(|row| -row["rows"].as_i64().unwrap_or(0));
    let (rows, queries) = usage();
    serde_json::json!({
        "sinceMs": if since > 0 { now_ms - since } else { 0 },
        "isolateRows": rows, "isolateQueries": queries,
        "paths": paths,
    })
}

pub fn rows_of(result: &D1Result) -> Result<Vec<Row>> {
    note(result);
    let rows: Vec<Value> = result.results()?;
    Ok(rows.into_iter().filter_map(|row| row.as_object().cloned()).collect())
}

pub async fn all(session: &D1DatabaseSession, sql: &str, params: &[Value]) -> Result<Vec<Row>> {
    note_query();
    rows_of(&statement(session, sql, params)?.all().await?)
}

pub async fn first(session: &D1DatabaseSession, sql: &str, params: &[Value]) -> Result<Option<Row>> {
    note_query();
    let row: Option<Value> = statement(session, sql, params)?.first(None).await?;
    Ok(row.and_then(|r| r.as_object().cloned()))
}

pub async fn batch(session: &D1DatabaseSession, statements: Vec<(String, Vec<Value>)>) -> Result<Vec<Vec<Row>>> {
    let prepared: Vec<D1PreparedStatement> = statements
        .iter()
        .map(|(sql, params)| statement(session, sql, params))
        .collect::<Result<_>>()?;
    for _ in &prepared {
        note_query();
    }
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

    /// Rows read and statements run so far, for a caller that wants to report what a phase
    /// cost. An implementation that cannot count says zero, and a phase that reports zero
    /// rows is reporting that it does not know — not that it read none.
    fn usage(&self) -> (i64, i64) {
        (0, 0)
    }
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
        Box::pin(async move {
            note_query();
            rows_of(&statement(self.0, sql, params)?.all().await?)
        })
    }

    fn batch<'a>(&'a self, statements: &'a [(String, Vec<Value>)]) -> BoxFuture<'a, Result<Vec<Vec<Row>>>> {
        Box::pin(batch(self.0, statements.to_vec()))
    }

    fn usage(&self) -> (i64, i64) {
        usage()
    }
}

#[cfg(test)]
mod accounting_tests {
    use super::*;

    /// One route is one row, whatever identifier it carried.
    ///
    /// Without this the map fills with a row per forecast and the heaviest *route* is invisible
    /// behind a thousand rows of one request each — which is the opposite of the question.
    #[test]
    fn a_path_is_recorded_as_its_route_and_not_its_identifier() {
        assert_eq!(normalised("/api/forecasts/abc123def456"), "/api/forecasts/{id}");
        assert_eq!(
            normalised("/api/risk/v2/feeds/devnet-stable-risk-v2"),
            "/api/risk/v2/feeds/{id}"
        );
        assert_eq!(normalised("/api/admin/risk/v2/health"), "/api/admin/risk/v2/health");
        assert_eq!(normalised("/api/health"), "/api/health");
        // Long opaque segments are identifiers even without a digit.
        assert_eq!(
            normalised("/api/profiles/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
            "/api/profiles/{id}"
        );
        // A hyphenated feed id is an identifier at any length.
        assert_eq!(
            normalised("/api/risk/v2/feeds/devnet-stable"),
            "/api/risk/v2/feeds/{id}"
        );
    }

    /// The report ranks by rows, because that is the budget being spent.
    #[test]
    fn the_report_names_the_heaviest_route_first() {
        BY_PATH.with(|by_path| by_path.borrow_mut().clear());
        SINCE.with(|since| since.set(0));
        record("/api/health", 1, 1, 1_000);
        record("/api/health", 1, 1, 1_100);
        record("/api/forecasts/xyz789abc", 900, 3, 1_200);
        let report = report(2_000);
        assert_eq!(report["paths"][0]["path"], "/api/forecasts/{id}");
        assert_eq!(report["paths"][0]["rows"], 900);
        assert_eq!(report["paths"][0]["rowsPerRequest"], 900);
        assert_eq!(report["paths"][1]["requests"], 2);
        // The isolate says how long its view covers, so a small number is not read as a small day.
        assert_eq!(report["sinceMs"], 1_000);
    }

    /// A version segment is part of the route, not an identifier.
    ///
    /// `v2` and `d1` carry a digit and are route words; merging them into `{id}` would have
    /// collapsed every versioned route onto one row and hidden exactly what this is for.
    #[test]
    fn a_version_segment_stays_part_of_the_route() {
        assert_eq!(
            normalised("/api/risk/v2/feeds/devnet-stable-risk-v2"),
            "/api/risk/v2/feeds/{id}"
        );
        assert_eq!(normalised("/api/admin/ops/d1"), "/api/admin/ops/d1");
        assert_eq!(normalised("/api/admin/risk/v1/feeds"), "/api/admin/risk/v1/feeds");
    }

    /// A burst of unknown paths cannot grow the map without limit.
    #[test]
    fn the_map_is_bounded_by_the_route_table_and_not_by_traffic() {
        BY_PATH.with(|by_path| by_path.borrow_mut().clear());
        for n in 0..(TRACKED_PATHS * 4) {
            // Route-word shaped — letters only — so each is its own row rather than `{id}`.
            let word: String = (0..4).map(|p| (b'a' + ((n >> (p * 2)) & 3) as u8) as char).collect();
            record(&format!("/api/probe{word}"), 1, 1, 1_000);
        }
        assert_eq!(BY_PATH.with(|by_path| by_path.borrow().len()), TRACKED_PATHS);
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
