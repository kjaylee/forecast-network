//! Shared machinery for replaying a golden vector against the real migrations.
//!
//! Two golden vectors now record the same thing — the state before a call, the tokens the call
//! consumed, and every table afterwards — so the machinery lives here rather than in each of them.
//! The restore and the comparison are the same question in both cases: does this port leave the
//! database the reference left?

use serde_json::{json, Value};

use crate::db::{self, Sqlite};

/// A coordinator whose transport refuses.
///
/// Its providers are configured and its transport is not, which is the reference's own `no_network`
/// shape: a path that needs no model still requires the *configuration* to be there, and a port
/// that read an empty provider list as "nothing to ask" would answer a different question.
pub fn refusing_coordinator() -> crate::ai::coordinator::Coordinator {
    crate::ai::coordinator::Coordinator {
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

pub fn block<F: std::future::Future>(future: F) -> F::Output {
    futures_lite::future::block_on(future)
}

/// The vector, as the generator wrote it.
pub fn load(name: &str) -> Value {
    let path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/golden")
        .join(name);
    serde_json::from_str(&std::fs::read_to_string(&path).unwrap_or_else(|error| panic!("{}: {error}", path.display())))
        .unwrap_or_else(|error| panic!("{}: {error}", path.display()))
}

pub fn entry<'a>(document: &'a Value, name: &str) -> &'a Value {
    document["cases"]
        .as_array()
        .expect("cases")
        .iter()
        .find(|case| case["call"] == json!(name))
        .unwrap_or_else(|| panic!("{name} is not in the vector"))
}

/// Restore the reference's own state.
///
/// Foreign keys are off because this restores an already-consistent database rather than replaying
/// a sequence of user actions, and the vector's tables are in an order that is alphabetical rather
/// than a dependency order. The schema's *guards* are taken down for the restore and put straight
/// back: they are transitions, the vector holds the state those transitions already produced, and
/// leaving them up would mean replaying the reference's history instead of its result.
pub fn restore(db: &Sqlite, initial: &Value) {
    db.run("PRAGMA foreign_keys=OFF", &[]).expect("foreign keys off");
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

/// A leaked test database with the vector's state restored.
///
/// The leak is deliberate: an artifact reader's future is `'static`, and a test database is
/// exactly the thing that *should* outlive the test.
pub fn static_database(initial: &Value) -> &'static Sqlite {
    let db: &'static Sqlite = Box::leak(Box::new(Sqlite::from_migrations()));
    restore(db, initial);
    db
}

/// Every table, in rowid order, exactly as the generator dumped it.
pub fn dump(db: &Sqlite) -> Value {
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

/// The rows a query returns, as the vector records them.
pub fn rows(db: &Sqlite, sql: &str) -> Value {
    let (rows, _) = db.run(sql, &[]).expect("rows");
    Value::Array(rows.into_iter().map(Value::Object).collect())
}

pub fn run_sql(db: &Sqlite, sql: &str, params: &[Value]) {
    db.run(sql, params).expect("statement");
}

/// Which tables differ, so a failure names the table instead of printing two databases.
pub fn differing(expected: &Value, actual: &Value) -> Vec<String> {
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
            let (a, b) = (
                left.as_array().cloned().unwrap_or_default(),
                right.as_array().cloned().unwrap_or_default(),
            );
            out.push(format!("{table}: reference {}, replay {}", a.len(), b.len()));
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

pub fn assert_database(name: &str, expected: &Value, actual: &Value) {
    let tables = differing(expected, actual);
    assert!(
        tables.is_empty(),
        "{name}: a different database\n  {}",
        tables.join("\n  ")
    );
}

/// The reference's token stream, replayed in order.
///
/// A token is opaque, so a replay that invented its own would fail on every row a token names.
/// Yielding the recorded sequence instead makes the *count* and the *order* part of what is
/// checked: a port that took one token where the reference took two is caught here.
pub struct Tokens {
    pub produced: Vec<String>,
    pub taken: std::cell::Cell<usize>,
}

impl Tokens {
    pub fn new(produced: Vec<String>) -> Self {
        Self {
            produced,
            taken: std::cell::Cell::new(0),
        }
    }

    /// The stream a case recorded, as strings.
    pub fn recorded(case: &Value) -> Vec<String> {
        case["tokens"]
            .as_array()
            .map(|values| {
                values
                    .iter()
                    .filter_map(|value| value.as_str().map(str::to_string))
                    .collect()
            })
            .unwrap_or_default()
    }

    pub fn next(&self) -> String {
        let index = self.taken.get();
        self.taken.set(index + 1);
        self.produced.get(index).cloned().unwrap_or_default()
    }

    /// The whole recorded stream has to be used. Both counts are reported because "took too few"
    /// and "took too many" are different defects: the first means a step was skipped, the second
    /// that one was added.
    pub fn assert_drained(&self, name: &str, detail: &str) {
        assert_eq!(
            self.taken.get(),
            self.produced.len(),
            "{name}: the reference took {} tokens and the replay took {}{detail}",
            self.produced.len(),
            self.taken.get()
        );
    }
}

/// Compare one case's recorded result, refusal and database against the replay's.
pub fn assert_case(name: &str, case: &Value, result: &Option<Value>, error: &Option<Value>, db: &Sqlite) {
    match case.get("error") {
        Some(expected) => assert_eq!(
            error.as_ref().and_then(|value| value.get("code")),
            expected.get("code"),
            "{name}: refused for a different reason"
        ),
        None => assert!(
            error.is_none(),
            "{name}: refused when the reference succeeded: {error:?}"
        ),
    }
    // A refusal has no result to compare: the reference raised before returning one. A case that
    // recorded no database at all — a pure function with no rows to leave behind — has nothing to
    // compare there either.
    if case.get("error").is_some() || case.get("rows").is_none() {
        if case.get("rows").is_some() {
            assert_database(name, &case["rows"], &dump(db));
        }
        return;
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
        assert_eq!(
            result,
            &Some(case["result"].clone()),
            "{name}: the whole result differs"
        );
    }
    assert_database(name, &case["rows"], &dump(db));
}

/// The cases a replay drives, and the ones it does not, each with the reason.
pub fn assert_all_cases_known(document: &Value, replayed: &[&str], skipped: &[(&str, &str)]) {
    for case in document["cases"].as_array().expect("cases") {
        let name = case["call"].as_str().expect("name");
        assert!(
            replayed.contains(&name) || skipped.iter().any(|(known, _)| *known == name),
            "{name} is in the vector and the replay neither drives it nor says why not"
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The restore and the comparison have to be exactly inverse, or every case built on them is
    /// testing the restore instead of the port.
    #[test]
    fn a_restored_database_dumps_back_to_the_vector_it_came_from() {
        let document = load("automation-run-golden.json");
        let case = entry(&document, "accept");
        let db = static_database(&case["initial"]);
        assert_database("restore", &case["initial"], &dump(db));
    }

    #[test]
    fn the_guards_are_back_after_a_restore() {
        // The restore takes the schema's triggers down and puts them back. If it did not, the call
        // under test would run unguarded and pass for the wrong reason.
        let db = Sqlite::from_migrations();
        let before = db
            .run("SELECT COUNT(*) AS n FROM sqlite_master WHERE type='trigger'", &[])
            .unwrap()
            .0;
        let document = load("automation-run-golden.json");
        let case = entry(&document, "accept");
        restore(&db, &case["initial"]);
        let after = db
            .run("SELECT COUNT(*) AS n FROM sqlite_master WHERE type='trigger'", &[])
            .unwrap()
            .0;
        assert_eq!(db::int(&before[0], "n"), db::int(&after[0], "n"));
    }
}
