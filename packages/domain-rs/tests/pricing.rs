//! LMSR parity: cost intervals, prices, subsidies, softplus and a 25-fill quote/accept sequence
//! must reproduce Python's values and record hashes exactly.

use std::path::PathBuf;

use serde_json::Value;

use forecast_domain::decimal::Dec;
use forecast_domain::pricing::{
    accept_quote, cost_interval, initialize_market, market_probability_bp, minimum_subsidy_atomic, price_bp, quote_buy,
    softplus, AcceptLimits, Interval, PricingPolicy, PricingQuote, PricingState,
};
use forecast_domain::{content_hash, Record};

fn golden() -> Value {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/pricing-golden.json");
    serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
}

fn dec(value: &Value) -> Dec {
    Dec::parse(value.as_str().unwrap()).unwrap()
}

#[test]
fn cost_and_price_match_python() {
    let vectors = golden();
    for case in vectors["cost"].as_array().unwrap() {
        let interval = cost_interval(
            case["yes"].as_i64().unwrap(),
            case["no"].as_i64().unwrap(),
            case["liquidity"].as_i64().unwrap(),
        )
        .unwrap();
        assert_eq!(interval.lo, dec(&case["lo"]), "cost lo {case}");
        assert_eq!(interval.hi, dec(&case["hi"]), "cost hi {case}");
    }
    for case in vectors["price_bp"].as_array().unwrap() {
        let bp = price_bp(
            case["own"].as_i64().unwrap(),
            case["other"].as_i64().unwrap(),
            case["liquidity"].as_i64().unwrap(),
        )
        .unwrap();
        assert_eq!(bp, case["bp"].as_i64().unwrap(), "price {case}");
    }
    for case in vectors["subsidy"].as_array().unwrap() {
        assert_eq!(
            minimum_subsidy_atomic(case["liquidity"].as_i64().unwrap()).unwrap(),
            case["atomic"].as_i64().unwrap()
        );
    }
    for case in vectors["softplus"].as_array().unwrap() {
        let x = dec(&case["x"]);
        let interval = softplus(&Interval { lo: x.clone(), hi: x }).unwrap();
        assert_eq!(interval.lo, dec(&case["lo"]), "softplus lo {case}");
        assert_eq!(interval.hi, dec(&case["hi"]), "softplus hi {case}");
    }
}

#[test]
fn quote_and_accept_sequence_matches_python() {
    let vectors = golden();
    let policy = PricingPolicy::default();
    policy.validate().unwrap();
    assert_eq!(content_hash(&policy).unwrap(), vectors["policy_hash"].as_str().unwrap());
    let mut state = initialize_market(policy, "m1", &"a".repeat(64)).unwrap();
    for (index, step) in vectors["quotes"].as_array().unwrap().iter().enumerate() {
        assert_eq!(
            serde_json::to_value(&state).unwrap(),
            step["state_before"],
            "step {index} state"
        );
        let expected = PricingQuote::from_value(step["quote"].clone()).unwrap();
        let quote = quote_buy(
            &state,
            &expected.owner_id,
            &expected.side,
            expected.spend_atomic,
            expected.quoted_at_ms,
        )
        .unwrap();
        assert_eq!(quote, expected, "step {index} quote");
        let receipt = accept_quote(
            &state,
            &quote,
            "u1",
            quote.quoted_at_ms + 5,
            &AcceptLimits {
                minimum_claims_atomic: 0,
                owner_gross_atomic: 0,
                owner_unsettled_atomic: 0,
            },
        )
        .unwrap();
        assert_eq!(
            serde_json::to_value(&receipt.state).unwrap(),
            step["receipt_state"],
            "step {index} receipt state"
        );
        assert_eq!(
            content_hash(&receipt).unwrap(),
            step["receipt_hash"].as_str().unwrap(),
            "step {index} receipt hash"
        );
        assert_eq!(
            market_probability_bp(&receipt.state).unwrap(),
            step["probability_bp"].as_i64().unwrap(),
            "step {index} probability"
        );
        state = receipt.state;
    }
    let decoded = PricingState::from_value(serde_json::to_value(&state).unwrap()).unwrap();
    assert_eq!(decoded, state);
    let mut forged = state.clone();
    forged.deposits_atomic /= 2;
    forged.reserve_atomic = forged.policy.subsidy_atomic + forged.deposits_atomic;
    assert!(
        forged.validate().is_err(),
        "halved deposits cannot back the accepted claims"
    );
}
