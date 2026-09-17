use thiserror::Error;

/// Mirrors `forecast_domain.errors.ValidationError`: a contract violation, never a crash.
#[derive(Debug, Error, Clone, PartialEq, Eq)]
#[error("{0}")]
pub struct ValidationError(pub String);

impl ValidationError {
    pub fn new(message: impl Into<String>) -> Self {
        ValidationError(message.into())
    }
}

pub type Result<T> = std::result::Result<T, ValidationError>;

/// `require(ok, message)` from the Python reference.
pub fn require(ok: bool, message: &str) -> Result<()> {
    if ok {
        Ok(())
    } else {
        Err(ValidationError::new(message))
    }
}
