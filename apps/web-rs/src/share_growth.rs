//! First-party attribution for shared forecast links.

use serde_json::{json, Value};
use worker::*;

use crate::analytics::DAY_MS;
use crate::db::{batch, first};
use crate::routes::Context;

pub const SHARE_WINDOW_MS: i64 = 7 * DAY_MS;

const ISSUE_LINK: &str = "INSERT INTO forecast_share_links(code,forecast_id,sharer_id,created_at) \
SELECT lower(hex(randomblob(16))),id,?,? FROM forecasts \
WHERE id=? AND state='OPEN' AND open_at<=? AND close_at>? RETURNING code";
const OPEN_LINK: &str = "UPDATE forecast_share_links SET opened_at=? WHERE code=? AND forecast_id=? \
AND opened_at IS NULL AND created_at<=? AND created_at>? AND (? IS NULL OR sharer_id<>?)";
const SHARE_LINK: &str = "UPDATE forecast_share_links SET shared_at=? WHERE code=? AND forecast_id=? AND sharer_id=? \
AND shared_at IS NULL AND created_at<=? AND created_at>?";
const CLAIM_LINK: &str = "INSERT INTO forecast_share_claims(recipient_id,forecast_id,code,claimed_at) \
SELECT ?,forecast_id,code,? FROM forecast_share_links WHERE code=? AND forecast_id=? AND sharer_id<>? \
AND created_at<=? AND created_at>? ON CONFLICT(recipient_id,forecast_id) DO NOTHING";

pub fn share_code(value: Option<&Value>) -> Option<&str> {
    value
        .and_then(Value::as_str)
        .filter(|code| code.len() == 32 && code.bytes().all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f')))
}

pub async fn issue_link(context: &Context<'_>, forecast_id: &str, user_id: &str) -> Result<Option<String>> {
    let now = json!(context.now_ms);
    let row = first(
        context.session,
        ISSUE_LINK,
        &[json!(user_id), now.clone(), json!(forecast_id), now.clone(), now],
    )
    .await?;
    Ok(row
        .as_ref()
        .and_then(|row| share_code(row.get("code")))
        .map(str::to_string))
}

fn open_params(context: &Context<'_>, forecast_id: &str, code: &str, user_id: Option<&str>) -> Vec<Value> {
    vec![
        json!(context.now_ms),
        json!(code),
        json!(forecast_id),
        json!(context.now_ms),
        json!(context.now_ms - SHARE_WINDOW_MS),
        json!(user_id),
        json!(user_id),
    ]
}

pub async fn open_link(context: &Context<'_>, forecast_id: &str, code: &str, user_id: Option<&str>) -> Result<()> {
    first(
        context.session,
        OPEN_LINK,
        &open_params(context, forecast_id, code, user_id),
    )
    .await?;
    Ok(())
}

pub async fn mark_shared(context: &Context<'_>, forecast_id: &str, code: &str, user_id: &str) -> Result<()> {
    let now = json!(context.now_ms);
    first(
        context.session,
        SHARE_LINK,
        &[
            now.clone(),
            json!(code),
            json!(forecast_id),
            json!(user_id),
            now,
            json!(context.now_ms - SHARE_WINDOW_MS),
        ],
    )
    .await?;
    Ok(())
}

pub async fn claim_link(context: &Context<'_>, forecast_id: &str, code: &str, user_id: &str) -> Result<()> {
    let now = json!(context.now_ms);
    batch(
        context.session,
        vec![
            (
                OPEN_LINK.to_string(),
                open_params(context, forecast_id, code, Some(user_id)),
            ),
            (
                CLAIM_LINK.to_string(),
                vec![
                    json!(user_id),
                    now.clone(),
                    json!(code),
                    json!(forecast_id),
                    json!(user_id),
                    now,
                    json!(context.now_ms - SHARE_WINDOW_MS),
                ],
            ),
        ],
    )
    .await?;
    Ok(())
}
