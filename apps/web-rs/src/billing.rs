//! The sandbox service-billing state machine. **No method here accepts a payment.**
//!
//! The cents this module moves are integer cents of hypothetical USD, they are never
//! user-spendable, and every entry point that could be mistaken for a charge returns
//! `billable: false` alongside it. Real billing needs separate authenticated payment and
//! fulfilment adapters, and this module is written so that adding them is a different module
//! rather than a flag on this one.
//!
//! That makes it tempting to treat as bookkeeping. It is not. It is a state machine over two
//! pools of capital — the principal the customer is simulated as paying, and the separate cost
//! envelope the operator funds — and the property that matters is that no sequence of events
//! can leave it owing more than it holds. So:
//!
//!   - a reservation is capital held until the attempt settles, whatever state it is in,
//!     including `uncertain`: an unknown provider charge is still a charge;
//!   - a provider cost above the authorized reservation is refused rather than absorbed;
//!   - the cost envelope cannot be exceeded by the sum of outstanding reservations;
//!   - and the funds only leave when the refund exposure has actually ended.
//!
//! Every write is one batch whose guards are rows in a table with `CHECK(passed=1)`, and every
//! request is keyed, so a lost acknowledgement replays rather than double-spends.

use serde_json::{json, Map, Value};

use forecast_domain::canonical_bytes;

use crate::db::{int, text, Database};

pub const MAX_CENTS: i64 = 1_000_000_000;
pub const MAX_ATTEMPTS: i64 = 100;
pub const MAX_QUOTE_AGE_MS: i64 = 3_600_000;

pub type Statement = (String, Vec<Value>);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BillingError {
    pub status: u16,
    pub code: &'static str,
    pub message: String,
}

impl BillingError {
    fn fixed(status: u16, code: &'static str, message: &'static str) -> Self {
        Self {
            status,
            code,
            message: message.to_string(),
        }
    }

    fn conflict(message: &str) -> Self {
        Self {
            status: 409,
            code: "billing_state_conflict",
            message: message.to_string(),
        }
    }
}

fn input_invalid() -> BillingError {
    BillingError::fixed(400, "billing_input_invalid", "A bounded integer is required.")
}

fn reference_required() -> BillingError {
    BillingError::fixed(
        400,
        "billing_sandbox_reference_required",
        "Use a sandbox-only reference.",
    )
}

fn invoice_not_found() -> BillingError {
    BillingError::fixed(404, "billing_invoice_not_found", "Sandbox invoice not found.")
}

fn storage_unavailable() -> BillingError {
    BillingError::fixed(503, "billing_unavailable", "Sandbox storage is unavailable.")
}

/// `_json`: the reference's compact sorted encoding.
fn encode(value: &Value) -> String {
    String::from_utf8(canonical_bytes(value).unwrap_or_default()).unwrap_or_default()
}

/// `_hash`: a plain digest of the canonical form. These key audit rows rather than committing to
/// a domain record, so they carry no commitment prefix.
fn hash_of(value: &Value) -> String {
    crate::source_watch::hash_hex(&encode(value))
}

/// `_integer`.
fn integer(value: Option<i64>, maximum: i64, minimum: i64) -> Result<i64, BillingError> {
    match value {
        Some(value) if minimum <= value && value <= maximum => Ok(value),
        _ => Err(input_invalid()),
    }
}

/// `_identifier`: `sandbox:[A-Za-z0-9:_-]{1,160}`.
fn identifier(value: &str) -> Result<(), BillingError> {
    let Some(rest) = value.strip_prefix("sandbox:") else {
        return Err(reference_required());
    };
    if rest.is_empty()
        || rest.chars().count() > 160
        || !rest
            .chars()
            .all(|character| character.is_ascii_alphanumeric() || matches!(character, ':' | '_' | '-'))
    {
        return Err(reference_required());
    }
    Ok(())
}

/// `_commitment`: exactly sixty-four lowercase hex characters.
fn commitment(value: &str) -> Result<(), BillingError> {
    if value.chars().count() != 64
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err(input_invalid());
    }
    Ok(())
}

fn require(condition: bool, message: &str) -> Result<(), BillingError> {
    if condition {
        Ok(())
    } else {
        Err(BillingError::conflict(message))
    }
}

/// `SandboxServiceBilling`.
pub struct SandboxServiceBilling<'a> {
    pub db: &'a dyn Database,
    pub now_ms: &'a dyn Fn() -> i64,
    pub enabled: bool,
}

/// What a mutation produces: the new invoice body, the new available capital, and any statements
/// the action adds of its own.
type Change = (Value, i64, Vec<Statement>);

impl SandboxServiceBilling<'_> {
    fn now(&self) -> i64 {
        (self.now_ms)()
    }

    /// `estimate`: the published shape of a simulated engagement. Nothing here is billable.
    pub fn estimate(price_cents: i64, cost_cap_cents: i64) -> Result<Value, BillingError> {
        let price = integer(Some(price_cents), MAX_CENTS, 1)?;
        let cap = integer(Some(cost_cap_cents), MAX_CENTS, 1)?;
        Ok(json!({
            "mode": "sandbox", "billable": false, "currency": "USD",
            "priceCents": price, "costCapCents": cap,
            "requiredOperatorCapitalCents": cap, "requiredAllocatedAssetsCents": price + cap,
            "maximumEventualContributionCents": price, "contributionAtCostCapCents": price - cap,
            "spendableAtPublicationCents": 0,
            "notice": "Simulation only. No payment is requested or accepted.",
        }))
    }

    /// `summary`: the capital pool and the totals across every invoice.
    pub async fn summary(&self) -> Result<Value, BillingError> {
        let capital = self
            .db
            .first("SELECT * FROM service_billing_sandbox_capital WHERE singleton=1", &[])
            .await
            .map_err(|_| storage_unavailable())?
            .ok_or_else(storage_unavailable)?;
        let rows = self
            .db
            .all("SELECT body FROM service_billing_sandbox_invoices", &[])
            .await
            .map_err(|_| storage_unavailable())?;
        let invoices: Vec<Value> = rows
            .iter()
            .filter_map(|row| text(row, "body"))
            .filter_map(|body| serde_json::from_str::<Value>(body).ok())
            .collect();
        let total = |field: &str| -> i64 {
            invoices
                .iter()
                .map(|invoice| invoice[field].as_i64().unwrap_or(0))
                .sum()
        };
        Ok(json!({
            "mode": "sandbox", "billable": false, "enabled": self.enabled,
            "availableCapitalCents": int(&capital, "available_cents"),
            "invoiceCount": invoices.len(),
            "assetsCents": total("assetsCents"), "principalCents": total("principalCents"),
            "remainingCostCents": total("remainingCostCents"), "spentCents": total("spentCents"),
        }))
    }

    /// `invoice`: the stored body of one invoice.
    pub async fn invoice(&self, invoice_id: &str, owner_hash: &str) -> Result<Value, BillingError> {
        identifier(invoice_id)?;
        commitment(owner_hash)?;
        let row = self
            .db
            .first(
                "SELECT * FROM service_billing_sandbox_invoices WHERE id=? AND owner_hash=?",
                &[json!(invoice_id), json!(owner_hash)],
            )
            .await
            .map_err(|_| storage_unavailable())?
            .ok_or_else(invoice_not_found)?;
        serde_json::from_str(text(&row, "body").unwrap_or_default()).map_err(|_| storage_unavailable())
    }

    /// `_replay`: the original result of this request key, if it has been made.
    async fn replay(&self, key: &str, request_hash: &str) -> Result<Option<Value>, BillingError> {
        let row = self
            .db
            .first(
                "SELECT request_hash,result FROM service_billing_sandbox_audit WHERE operation_key=?",
                &[json!(key)],
            )
            .await
            .map_err(|_| storage_unavailable())?;
        let Some(row) = row else {
            return Ok(None);
        };
        // The same key with a different request is not a replay; it is a collision.
        require(
            text(&row, "request_hash") == Some(request_hash),
            "Idempotency key was used for a different request.",
        )?;
        serde_json::from_str(text(&row, "result").unwrap_or_default())
            .map(Some)
            .map_err(|_| storage_unavailable())
    }

    /// `_mutate`: the one write path.
    ///
    /// The replay check happens twice on purpose. The first catches a request that has already
    /// been made; the second catches one that was made *while this one was reading* — the
    /// snapshot reads between them are awaits, and a competing caller can commit in that gap.
    async fn mutate<F>(
        &self,
        action: &str,
        key: &str,
        payload: &Value,
        invoice_id: Option<&str>,
        owner_hash: Option<&str>,
        change: F,
    ) -> Result<Value, BillingError>
    where
        F: FnOnce(Option<Value>, i64, i64) -> Result<Change, BillingError>,
    {
        if !self.enabled {
            return Err(BillingError::fixed(
                503,
                "billing_sandbox_disabled",
                "Billing is disabled; no payment is accepted.",
            ));
        }
        identifier(key)?;
        if let Some(invoice_id) = invoice_id {
            identifier(invoice_id)?;
        }
        if let Some(owner_hash) = owner_hash {
            commitment(owner_hash)?;
        }
        let request_hash = hash_of(&json!({
            "action": action, "invoiceId": invoice_id, "ownerHash": owner_hash, "payload": payload,
        }));
        if let Some(replay) = self.replay(key, &request_hash).await? {
            return Ok(replay);
        }
        let capital = self
            .db
            .first("SELECT * FROM service_billing_sandbox_capital WHERE singleton=1", &[])
            .await
            .map_err(|_| storage_unavailable())?
            .ok_or_else(storage_unavailable)?;
        let existing = match invoice_id {
            Some(invoice_id) => self
                .db
                .first(
                    "SELECT * FROM service_billing_sandbox_invoices WHERE id=?",
                    &[json!(invoice_id)],
                )
                .await
                .map_err(|_| storage_unavailable())?,
            None => None,
        };
        if action != "quote" {
            if let Some(invoice_id) = invoice_id {
                let matches = existing
                    .as_ref()
                    .is_some_and(|row| text(row, "owner_hash") == owner_hash);
                if !matches {
                    let _ = invoice_id;
                    return Err(invoice_not_found());
                }
            }
        }
        if let Some(replay) = self.replay(key, &request_hash).await? {
            return Ok(replay);
        }
        let before: Option<Value> = existing
            .as_ref()
            .and_then(|row| text(row, "body"))
            .and_then(|body| serde_json::from_str::<Value>(body).ok());
        let (result, available, extra) = change(before, int(&capital, "available_cents").unwrap_or(0), self.now())?;
        integer(Some(available), MAX_CENTS, 0)?;

        let mut statements: Vec<Statement> = vec![(
            "INSERT INTO service_billing_sandbox_guards SELECT ?,COUNT(*) FROM service_billing_sandbox_capital \
             WHERE singleton=1 AND version=?"
                .to_string(),
            vec![json!(key), json!(int(&capital, "version"))],
        )];
        if let Some(row) = existing.as_ref() {
            statements.push((
                "INSERT INTO service_billing_sandbox_guards SELECT ?,COUNT(*) FROM service_billing_sandbox_invoices \
                 WHERE id=? AND version=?"
                    .to_string(),
                vec![
                    json!(format!("{key}:invoice")),
                    json!(invoice_id),
                    json!(int(row, "version")),
                ],
            ));
        }
        if let Some(invoice_id) = invoice_id {
            self.invariant(&result)?;
            match existing.as_ref() {
                None => statements.push((
                    "INSERT INTO service_billing_sandbox_invoices VALUES (?,?,?,?,0)".to_string(),
                    vec![
                        json!(invoice_id),
                        json!(result["ownerHash"]),
                        json!(result["scopeHash"]),
                        json!(encode(&result)),
                    ],
                )),
                Some(_) => statements.push((
                    "UPDATE service_billing_sandbox_invoices SET body=?,version=version+1 WHERE id=?".to_string(),
                    vec![json!(encode(&result)), json!(invoice_id)],
                )),
            }
        }
        statements.extend(extra);
        statements.push((
            "UPDATE service_billing_sandbox_capital SET available_cents=?,version=version+1 WHERE singleton=1"
                .to_string(),
            vec![json!(available)],
        ));
        statements.push((
            "INSERT INTO service_billing_sandbox_audit VALUES (?,?,?,?,?,?)".to_string(),
            vec![
                json!(key),
                json!(request_hash),
                json!(action),
                json!(invoice_id),
                json!(encode(&result)),
                json!(self.now()),
            ],
        ));
        statements.push((
            "DELETE FROM service_billing_sandbox_guards WHERE operation_key IN (?,?)".to_string(),
            vec![json!(key), json!(format!("{key}:invoice"))],
        ));
        if self.db.batch(&statements).await.is_err() {
            if let Some(replay) = self.replay(key, &request_hash).await? {
                return Ok(replay);
            }
            return Err(BillingError::conflict(
                "Sandbox accounting changed. Retry with the same key.",
            ));
        }
        Ok(result)
    }

    /// `_invariant`: the property that no sequence of events can violate.
    fn invariant(&self, invoice: &Value) -> Result<(), BillingError> {
        for field in ["assetsCents", "principalCents", "remainingCostCents", "spentCents"] {
            integer(invoice[field].as_i64(), MAX_CENTS * 2, 0)?;
        }
        require(
            invoice["mode"].as_str() == Some("sandbox") && invoice["billable"].as_bool() == Some(false),
            "Real billing is unavailable.",
        )?;
        let assets = invoice["assetsCents"].as_i64().unwrap_or(0);
        let principal = invoice["principalCents"].as_i64().unwrap_or(0);
        let remaining = invoice["remainingCostCents"].as_i64().unwrap_or(0);
        require(assets >= principal + remaining, "Unfunded service liability.")?;
        // A reservation is capital held until the attempt settles, whatever state it is in —
        // `uncertain` included, because an unknown provider charge is still a charge.
        let held: i64 = invoice["attempts"]
            .as_object()
            .map(|attempts| {
                attempts
                    .values()
                    .filter(|attempt| attempt["state"].as_str() != Some("settled"))
                    .map(|attempt| attempt["capCents"].as_i64().unwrap_or(0))
                    .sum()
            })
            .unwrap_or(0);
        require(held <= remaining, "Provider reservations exceed the cost envelope.")
    }
}

impl SandboxServiceBilling<'_> {
    /// `fund_capital`: add to the operator's side of the ledger.
    pub async fn fund_capital(&self, amount_cents: i64, key: &str) -> Result<Value, BillingError> {
        let amount = integer(Some(amount_cents), MAX_CENTS, 1)?;
        self.mutate(
            "fund",
            key,
            &json!({"amountCents": amount}),
            None,
            None,
            move |_, available, _| {
                Ok((
                    json!({"mode": "sandbox", "billable": false, "availableCapitalCents": available + amount}),
                    available + amount,
                    Vec::new(),
                ))
            },
        )
        .await
    }

    /// `quote`: create an invoice at a price and a cost envelope. It reserves nothing yet.
    #[allow(clippy::too_many_arguments)]
    pub async fn quote(
        &self,
        invoice_id: &str,
        owner_hash: &str,
        scope_hash: &str,
        price_cents: i64,
        cost_cap_cents: i64,
        max_attempts: i64,
        expires_at: i64,
        refund_until: i64,
        key: &str,
    ) -> Result<Value, BillingError> {
        let price = integer(Some(price_cents), MAX_CENTS, 1)?;
        let cap = integer(Some(cost_cap_cents), MAX_CENTS, 1)?;
        let attempts = integer(Some(max_attempts), MAX_ATTEMPTS, 1)?;
        commitment(scope_hash)?;
        integer(Some(expires_at), 9_000_000_000_000_000, 0)?;
        integer(Some(refund_until), 9_000_000_000_000_000, 0)?;
        let payload = json!({
            "scopeHash": scope_hash, "priceCents": price, "costCapCents": cap,
            "maxAttempts": attempts, "expiresAt": expires_at, "refundUntil": refund_until,
        });
        let scope = scope_hash.to_string();
        let owner = owner_hash.to_string();
        let id = invoice_id.to_string();
        let reference = payload.clone();
        self.mutate(
            "quote",
            key,
            &reference,
            Some(invoice_id),
            Some(owner_hash),
            move |before, available, now| {
                require(before.is_none(), "Invoice already exists.")?;
                require(
                    now < expires_at && expires_at <= now + MAX_QUOTE_AGE_MS && refund_until >= expires_at,
                    "Quote expiry or refund window is invalid.",
                )?;
                let mut invoice = Map::new();
                invoice.insert("id".to_string(), json!(id));
                invoice.insert("ownerHash".to_string(), json!(owner));
                for (name, value) in payload.as_object().cloned().unwrap_or_default() {
                    invoice.insert(name, value);
                }
                invoice.insert("mode".to_string(), json!("sandbox"));
                invoice.insert("billable".to_string(), json!(false));
                invoice.insert("currency".to_string(), json!("USD"));
                invoice.insert("phase".to_string(), json!("QUOTED"));
                invoice.insert("paymentState".to_string(), json!("UNFUNDED"));
                invoice.insert("assetsCents".to_string(), json!(0));
                invoice.insert("principalCents".to_string(), json!(0));
                invoice.insert("remainingCostCents".to_string(), json!(0));
                invoice.insert("spentCents".to_string(), json!(0));
                invoice.insert("attempts".to_string(), json!({}));
                let _ = scope;
                Ok((Value::Object(invoice), available, Vec::new()))
            },
        )
        .await
    }

    /// `accept_sandbox_receipt`: the simulated payment. It funds both pools in one step.
    pub async fn accept_sandbox_receipt(
        &self,
        invoice_id: &str,
        owner_hash: &str,
        reference: &str,
        amount_cents: i64,
        scope_hash: &str,
        key: &str,
    ) -> Result<Value, BillingError> {
        identifier(reference)?;
        let amount = integer(Some(amount_cents), MAX_CENTS, 1)?;
        commitment(scope_hash)?;
        let scope = scope_hash.to_string();
        let reference = reference.to_string();
        self.mutate(
            "accept_sandbox_receipt",
            key,
            &json!({"reference": reference, "amountCents": amount, "scopeHash": scope}),
            Some(invoice_id),
            Some(owner_hash),
            move |before, available, now| {
                let Some(mut invoice) = before else {
                    return Err(invoice_not_found());
                };
                require(
                    invoice["phase"].as_str() == Some("QUOTED") && now < invoice["expiresAt"].as_i64().unwrap_or(0),
                    "Quote expired or already funded.",
                )?;
                require(
                    invoice["scopeHash"].as_str() == Some(scope.as_str())
                        && invoice["priceCents"].as_i64() == Some(amount),
                    "Receipt does not match the accepted quote.",
                )?;
                let cap = invoice["costCapCents"].as_i64().unwrap_or(0);
                require(available >= cap, "Separate operator cost capital is exhausted.")?;
                let fields = invoice.as_object_mut().expect("an object");
                fields.insert("phase".to_string(), json!("ACCEPTED_COMPILING"));
                fields.insert("paymentState".to_string(), json!("SANDBOX_FUNDED"));
                fields.insert("principalCents".to_string(), json!(amount));
                fields.insert("remainingCostCents".to_string(), json!(cap));
                fields.insert("assetsCents".to_string(), json!(amount + cap));
                Ok((
                    invoice,
                    available - cap,
                    vec![(
                        "INSERT INTO service_billing_sandbox_receipts VALUES (?,?)".to_string(),
                        vec![json!(reference), json!(invoice_id)],
                    )],
                ))
            },
        )
        .await
    }
}

/// The payload keys each sandbox command takes. An unknown command, or a command with a field
/// that is not part of its contract, is refused rather than ignored: a state machine that accepts
/// extra fields is a state machine whose transitions nobody has enumerated.
fn command_fields(action: &str) -> Option<&'static [&'static str]> {
    Some(match action {
        "start_attempt" => &["attemptId", "capCents"],
        "start_refund_attempt" => &["attemptId", "capCents"],
        "mark_uncertain" => &["attemptId"],
        "settle_attempt" => &["attemptId", "actualCents"],
        "publish" => &["scopeHash"],
        "complete" | "request_refund" | "finish_refund" | "release" => &[],
        _ => return None,
    })
}

impl SandboxServiceBilling<'_> {
    /// `command`: the sandbox event dispatcher.
    ///
    /// No event charges a provider. What the events do is move the *reservation* between states,
    /// and the two properties that make the ledger safe are here: an `uncertain` attempt still
    /// holds its capital, and a settlement above the authorized reservation is refused rather
    /// than absorbed into the cost envelope.
    pub async fn command(
        &self,
        invoice_id: &str,
        owner_hash: &str,
        action: &str,
        key: &str,
        payload: Map<String, Value>,
    ) -> Result<Value, BillingError> {
        let Some(fields) = command_fields(action) else {
            return Err(BillingError::fixed(
                400,
                "billing_input_invalid",
                "Unknown sandbox command or fields.",
            ));
        };
        let present: Vec<&str> = payload.keys().map(String::as_str).collect();
        let expected: Vec<&str> = fields.to_vec();
        if present.len() != expected.len() || !expected.iter().all(|field| payload.contains_key(*field)) {
            return Err(BillingError::fixed(
                400,
                "billing_input_invalid",
                "Unknown sandbox command or fields.",
            ));
        }
        if let Some(attempt) = payload.get("attemptId").and_then(Value::as_str) {
            identifier(attempt)?;
        }
        if let Some(cap) = payload.get("capCents") {
            integer(cap.as_i64(), MAX_CENTS, 1)?;
        }
        if let Some(actual) = payload.get("actualCents") {
            integer(actual.as_i64(), MAX_CENTS, 0)?;
        }
        if let Some(scope) = payload.get("scopeHash").and_then(Value::as_str) {
            commitment(scope)?;
        }

        let owned = payload.clone();
        let action_name = action.to_string();
        let action = action.to_string();
        self.mutate(
            &action_name,
            key,
            &Value::Object(owned.clone()),
            Some(invoice_id),
            Some(owner_hash),
            move |before, available, now| {
                let Some(mut invoice) = before else {
                    return Err(invoice_not_found());
                };
                require(
                    !matches!(invoice["phase"].as_str(), Some("QUOTED") | Some("CLOSED")),
                    "Invoice is not active.",
                )?;
                let attempts: Vec<(String, Value)> = invoice["attempts"]
                    .as_object()
                    .map(|attempts| attempts.iter().map(|(id, value)| (id.clone(), value.clone())).collect())
                    .unwrap_or_default();
                let pending = attempts
                    .iter()
                    .any(|(_, value)| value["state"].as_str() != Some("settled"));
                let mut attempts: Map<String, Value> = attempts.into_iter().collect();
                let attempt_id = owned
                    .get("attemptId")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_string();
                let cap_cents = owned.get("capCents").and_then(Value::as_i64).unwrap_or(0);
                let actual_cents = owned.get("actualCents").and_then(Value::as_i64).unwrap_or(0);
                let mut available = available;

                match action.as_str() {
                    "start_attempt" | "start_refund_attempt" => {
                        if action == "start_refund_attempt" {
                            require(
                                invoice["paymentState"].as_str() == Some("REFUND_PENDING"),
                                "No pending refund-processing duty.",
                            )?;
                        } else {
                            require(
                                matches!(
                                    invoice["phase"].as_str(),
                                    Some("ACCEPTED_COMPILING") | Some("PUBLISHED_SERVICING")
                                ),
                                "Service work is complete.",
                            )?;
                            require(
                                invoice["phase"].as_str() == Some("PUBLISHED_SERVICING")
                                    || invoice["paymentState"].as_str() == Some("SANDBOX_FUNDED"),
                                "Unpublished refund has no service work.",
                            )?;
                        }
                        let max_attempts = invoice["maxAttempts"].as_i64().unwrap_or(0);
                        require(
                            !attempts.contains_key(&attempt_id) && (attempts.len() as i64) < max_attempts,
                            "Attempt limit reached or duplicate attempt.",
                        )?;
                        // The envelope covers reservations, not just what has been spent.
                        let held: i64 = attempts
                            .values()
                            .filter(|attempt| attempt["state"].as_str() != Some("settled"))
                            .map(|attempt| attempt["capCents"].as_i64().unwrap_or(0))
                            .sum();
                        let remaining = invoice["remainingCostCents"].as_i64().unwrap_or(0);
                        require(cap_cents <= remaining - held, "Cost envelope exhausted.")?;
                        attempts.insert(
                            attempt_id,
                            json!({"capCents": cap_cents, "state": "reserved", "actualCents": Value::Null}),
                        );
                    }
                    "mark_uncertain" | "settle_attempt" => {
                        let Some(attempt) = attempts.get_mut(&attempt_id) else {
                            return Err(BillingError::conflict("No outstanding attempt reservation."));
                        };
                        require(
                            attempt["state"].as_str() != Some("settled"),
                            "No outstanding attempt reservation.",
                        )?;
                        if action == "mark_uncertain" {
                            attempt["state"] = json!("uncertain");
                        } else {
                            let cap = attempt["capCents"].as_i64().unwrap_or(0);
                            require(actual_cents <= cap, "Provider cost exceeds authorized reservation.")?;
                            *attempt = json!({"capCents": cap, "state": "settled", "actualCents": actual_cents});
                            let assets = invoice["assetsCents"].as_i64().unwrap_or(0) - actual_cents;
                            let remaining = invoice["remainingCostCents"].as_i64().unwrap_or(0) - actual_cents;
                            let spent = invoice["spentCents"].as_i64().unwrap_or(0) + actual_cents;
                            let fields = invoice.as_object_mut().expect("an object");
                            fields.insert("assetsCents".to_string(), json!(assets));
                            fields.insert("remainingCostCents".to_string(), json!(remaining));
                            fields.insert("spentCents".to_string(), json!(spent));
                        }
                    }
                    "publish" => {
                        require(
                            invoice["phase"].as_str() == Some("ACCEPTED_COMPILING")
                                && invoice["paymentState"].as_str() == Some("SANDBOX_FUNDED"),
                            "Invoice cannot publish.",
                        )?;
                        require(
                            owned.get("scopeHash").and_then(Value::as_str) == invoice["scopeHash"].as_str(),
                            "Creator approval must match the immutable scope.",
                        )?;
                        invoice["phase"] = json!("PUBLISHED_SERVICING");
                    }
                    "complete" => {
                        require(
                            invoice["phase"].as_str() == Some("PUBLISHED_SERVICING") && !pending,
                            "Outstanding work or uncertain bills remain.",
                        )?;
                        invoice["phase"] = json!("COMPLETED_REFUND_WINDOW");
                    }
                    "request_refund" => {
                        require(
                            invoice["paymentState"].as_str() == Some("SANDBOX_FUNDED"),
                            "Refund already requested or paid.",
                        )?;
                        invoice["paymentState"] = json!("REFUND_PENDING");
                        if invoice["phase"].as_str() == Some("ACCEPTED_COMPILING") {
                            invoice["phase"] = json!("COMPLETED_REFUND_WINDOW");
                        }
                    }
                    "finish_refund" => {
                        require(
                            invoice["paymentState"].as_str() == Some("REFUND_PENDING"),
                            "No pending refund.",
                        )?;
                        let principal = invoice["principalCents"].as_i64().unwrap_or(0);
                        let assets = invoice["assetsCents"].as_i64().unwrap_or(0) - principal;
                        let fields = invoice.as_object_mut().expect("an object");
                        fields.insert("assetsCents".to_string(), json!(assets));
                        fields.insert("principalCents".to_string(), json!(0));
                        fields.insert("paymentState".to_string(), json!("SANDBOX_REFUNDED"));
                    }
                    "release" => {
                        require(
                            invoice["phase"].as_str() == Some("COMPLETED_REFUND_WINDOW") && !pending,
                            "Completion duties or uncertain charges remain.",
                        )?;
                        let funded_past_window = invoice["paymentState"].as_str() == Some("SANDBOX_FUNDED")
                            && now >= invoice["refundUntil"].as_i64().unwrap_or(0);
                        require(
                            invoice["paymentState"].as_str() == Some("SANDBOX_REFUNDED") || funded_past_window,
                            "Refund exposure has not ended.",
                        )?;
                        let assets = invoice["assetsCents"].as_i64().unwrap_or(0);
                        available += assets;
                        let earned = invoice["paymentState"].as_str() == Some("SANDBOX_FUNDED");
                        let fields = invoice.as_object_mut().expect("an object");
                        fields.insert("releasedCents".to_string(), json!(assets));
                        fields.insert("assetsCents".to_string(), json!(0));
                        fields.insert("principalCents".to_string(), json!(0));
                        fields.insert("remainingCostCents".to_string(), json!(0));
                        fields.insert("phase".to_string(), json!("CLOSED"));
                        if earned {
                            fields.insert("paymentState".to_string(), json!("SANDBOX_EARNED"));
                        }
                    }
                    _ => {
                        return Err(BillingError::fixed(
                            400,
                            "billing_input_invalid",
                            "Unknown sandbox command or fields.",
                        ))
                    }
                }
                invoice["attempts"] = Value::Object(attempts);
                Ok((invoice, available, Vec::new()))
            },
        )
        .await
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::Sqlite;
    use std::cell::Cell;

    fn block<F: std::future::Future>(future: F) -> F::Output {
        futures_lite::future::block_on(future)
    }

    fn golden() -> Value {
        let path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/billing-golden.json");
        serde_json::from_str(&std::fs::read_to_string(&path).expect("billing golden")).expect("json")
    }

    /// Assert one call against the golden's record, whether it succeeded or was refused.
    fn check(calls: &[Value], index: &mut usize, produced: Result<Value, BillingError>) {
        let entry = &calls[*index];
        let name = entry["call"].as_str().unwrap();
        match produced {
            Ok(value) => {
                assert!(
                    entry["error"].is_null(),
                    "{name}: succeeded where the reference refused"
                );
                assert_eq!(value, entry["result"], "{name}: a different result");
            }
            Err(error) => {
                assert!(
                    !entry["error"].is_null(),
                    "{name}: refused with {:?} where the reference succeeded",
                    error
                );
                assert_eq!(
                    error.status as i64,
                    entry["error"]["status"].as_i64().unwrap(),
                    "{name}: status"
                );
                assert_eq!(Some(error.code), entry["error"]["code"].as_str(), "{name}: code");
            }
        }
        *index += 1;
    }

    fn fields(pairs: &[(&str, Value)]) -> Map<String, Value> {
        pairs
            .iter()
            .map(|(name, value)| ((*name).to_string(), value.clone()))
            .collect()
    }

    #[test]
    fn the_reference_billing_lifecycle_is_reproduced_call_for_call() {
        // Not bookkeeping: a state machine over two pools of capital, where the property that
        // matters is that no sequence of events leaves it owing more than it holds.
        let document = golden();
        let db = Sqlite::from_migrations();
        let now = document["now"].as_i64().unwrap();
        let clock = || now;
        let billing = SandboxServiceBilling {
            db: &db,
            now_ms: &clock,
            enabled: true,
        };
        let counter = Cell::new(0);
        let key = |counter: &Cell<i64>| {
            counter.set(counter.get() + 1);
            format!("sandbox:key-{}", counter.get())
        };
        let calls = document["calls"].as_array().expect("calls");
        let mut index = 0usize;

        const INVOICE: &str = "sandbox:invoice-1";
        let owner = "a".repeat(64);
        let scope = "b".repeat(64);

        check(calls, &mut index, SandboxServiceBilling::estimate(200, 115));
        check(calls, &mut index, block(billing.summary()));

        check(calls, &mut index, block(billing.fund_capital(1000, &key(&counter))));
        // The same key with the same request is the same grant; with a different one it is a
        // collision, and the audit row is what tells them apart.
        let fund_key = key(&counter);
        check(
            calls,
            &mut index,
            Ok(block(billing.fund_capital(500, &fund_key)).unwrap()),
        );
        check(calls, &mut index, block(billing.fund_capital(500, &fund_key)));
        check(calls, &mut index, block(billing.fund_capital(600, &fund_key)));

        let quote_key = key(&counter);
        let quote =
            |key: &str| block(billing.quote(INVOICE, &owner, &scope, 200, 115, 5, now + 600_000, now + 900_000, key));
        check(calls, &mut index, quote(&quote_key));
        check(calls, &mut index, quote(&quote_key));
        check(calls, &mut index, block(billing.invoice(INVOICE, &owner)));
        check(calls, &mut index, block(billing.summary()));

        check(
            calls,
            &mut index,
            block(billing.accept_sandbox_receipt(INVOICE, &owner, "sandbox:receipt-1", 200, &scope, &key(&counter))),
        );

        let attempt = |action: &str, pairs: &[(&str, Value)]| {
            block(billing.command(INVOICE, &owner, action, &key(&counter), fields(pairs)))
        };
        check(
            calls,
            &mut index,
            attempt(
                "start_attempt",
                &[("attemptId", json!("sandbox:attempt-1")), ("capCents", json!(50))],
            ),
        );
        check(
            calls,
            &mut index,
            attempt(
                "settle_attempt",
                &[("attemptId", json!("sandbox:attempt-1")), ("actualCents", json!(40))],
            ),
        );
        check(
            calls,
            &mut index,
            attempt(
                "start_attempt",
                &[("attemptId", json!("sandbox:attempt-2")), ("capCents", json!(30))],
            ),
        );
        check(
            calls,
            &mut index,
            attempt("mark_uncertain", &[("attemptId", json!("sandbox:attempt-2"))]),
        );
        // An uncertain reservation still holds capital, so completion cannot pass yet.
        check(calls, &mut index, attempt("complete", &[]));
        check(
            calls,
            &mut index,
            attempt(
                "settle_attempt",
                &[("attemptId", json!("sandbox:attempt-2")), ("actualCents", json!(25))],
            ),
        );
        check(
            calls,
            &mut index,
            attempt(
                "start_attempt",
                &[("attemptId", json!("sandbox:attempt-3")), ("capCents", json!(10))],
            ),
        );
        check(
            calls,
            &mut index,
            attempt(
                "settle_attempt",
                &[("attemptId", json!("sandbox:attempt-3")), ("actualCents", json!(20))],
            ),
        );
        check(
            calls,
            &mut index,
            attempt(
                "settle_attempt",
                &[("attemptId", json!("sandbox:attempt-3")), ("actualCents", json!(10))],
            ),
        );
        check(
            calls,
            &mut index,
            attempt("publish", &[("scopeHash", json!("c".repeat(64)))]),
        );
        check(calls, &mut index, attempt("publish", &[("scopeHash", json!(scope))]));
        check(calls, &mut index, attempt("complete", &[]));
        check(
            calls,
            &mut index,
            attempt(
                "start_refund_attempt",
                &[("attemptId", json!("sandbox:refund-1")), ("capCents", json!(5))],
            ),
        );
        check(calls, &mut index, attempt("request_refund", &[]));
        check(
            calls,
            &mut index,
            attempt(
                "start_refund_attempt",
                &[("attemptId", json!("sandbox:refund-1")), ("capCents", json!(5))],
            ),
        );
        check(
            calls,
            &mut index,
            attempt(
                "settle_attempt",
                &[("attemptId", json!("sandbox:refund-1")), ("actualCents", json!(5))],
            ),
        );
        check(calls, &mut index, attempt("finish_refund", &[]));
        check(calls, &mut index, attempt("release", &[]));
        check(calls, &mut index, block(billing.summary()));
        check(calls, &mut index, block(billing.invoice(INVOICE, &owner)));
        check(calls, &mut index, attempt("complete", &[]));
        assert_eq!(
            index,
            calls.len(),
            "the replay did not make every call the reference made"
        );

        for (table, expected) in document["rows"].as_object().expect("rows") {
            let found = db
                .run(&format!("SELECT * FROM {table} ORDER BY rowid"), &[])
                .expect("rows")
                .0;
            let produced: Vec<Value> = found.iter().map(|row| Value::Object(row.clone())).collect();
            assert_eq!(&produced, expected.as_array().unwrap(), "{table} differs");
        }

        // A disabled deployment accepts nothing and still reports its state.
        let disabled = golden();
        let db = Sqlite::from_migrations();
        let billing = SandboxServiceBilling {
            db: &db,
            now_ms: &clock,
            enabled: false,
        };
        let mut index = 0usize;
        let calls = disabled["disabledCalls"].as_array().expect("disabled calls");
        check(calls, &mut index, block(billing.fund_capital(1000, "sandbox:key-1")));
        check(calls, &mut index, block(billing.summary()));
        assert_eq!(index, calls.len());
    }
}
