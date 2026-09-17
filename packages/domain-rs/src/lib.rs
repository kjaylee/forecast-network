//! Shared Forecast Network domain contracts.
//!
//! The Python packages under `packages/domain` remain the schema authority; this crate must
//! reproduce their canonical bytes, content hashes and validation verdicts exactly. Golden
//! vectors exported from Python (`tests/golden/risk-feed-golden.json`) pin that equivalence.

pub mod canonical;
pub mod decimal;
pub mod errors;
pub mod fields;
pub mod lifecycle;
pub mod models;
pub mod pricing;
pub mod risk_feed;

pub use canonical::{canonical_bytes, content_hash, COMMITMENT_PREFIX, MAX_SAFE_INTEGER};
pub use errors::{require, Result, ValidationError};
pub use fields::Record;
