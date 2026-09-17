//! Python `decimal` parity at 80 digits: directed ops, exp/ln correctly rounded, neighbours.

use std::path::PathBuf;

use serde_json::Value;

use forecast_domain::decimal::{add, div, exp, ln, Dec, Rounding};

fn golden() -> Value {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/pricing-golden.json");
    serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
}

fn dec(value: &Value) -> Dec {
    Dec::parse(value.as_str().unwrap()).unwrap()
}

#[test]
fn exp_matches_python_decimal() {
    let vectors = golden();
    for case in vectors["exp"].as_array().unwrap() {
        let x = dec(&case["x"]);
        if let Some(near) = case["near"].as_str() {
            assert_eq!(exp(&x), Dec::parse(near).unwrap(), "exp({})", case["x"]);
        }
    }
}

#[test]
fn ln_matches_python_decimal() {
    let vectors = golden();
    for case in vectors["ln"].as_array().unwrap() {
        let v = dec(&case["v"]);
        assert_eq!(ln(&v).unwrap(), dec(&case["near"]), "ln({})", case["v"]);
    }
}

#[test]
fn neighbours_match_python_decimal() {
    let vectors = golden();
    for case in vectors["next"].as_array().unwrap() {
        let v = dec(&case["v"]);
        assert_eq!(v.next_plus(), dec(&case["plus"]), "next_plus({})", case["v"]);
        assert_eq!(v.next_minus(), dec(&case["minus"]), "next_minus({})", case["v"]);
    }
}

#[test]
fn directed_rounding_is_exact_then_rounded() {
    let third = div(&Dec::one(), &Dec::from_int(3), Rounding::Floor).unwrap();
    let third_up = div(&Dec::one(), &Dec::from_int(3), Rounding::Ceiling).unwrap();
    assert!(third.compare(&third_up) == std::cmp::Ordering::Less);
    assert_eq!(third_up, third.next_plus());
    let sum = add(&third, &third_up, Rounding::HalfEven);
    assert_eq!(sum.to_string().len(), 82);
    assert_eq!(Dec::parse("1E+3").unwrap(), Dec::from_int(1000));
    assert_eq!(Dec::parse("-1.50").unwrap().to_string(), "-1.5");
}
