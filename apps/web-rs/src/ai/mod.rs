//! The AI pipeline: what a model is asked, what it may answer, and what is recorded about both.
//!
//! Ported in pieces. This is the structural contract — the schemas every answer has to satisfy
//! and the validator that refuses the ones that do not. A permissive parse would accept a
//! contract change silently, and a contract change here is a change to what a reward is based on.

pub mod coordinator;
pub mod schema;
pub mod strict;
pub mod text;
pub mod window;
