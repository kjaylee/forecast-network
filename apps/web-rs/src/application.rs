//! The composition root: everything the Worker builds from its environment, in one place.
//!
//! This is the one layer in this crate that no golden can reach, and that is by construction. Every
//! other module takes its collaborators as seams — that is what makes a vector possible — and this
//! module is what *fills* those seams in production. So its correctness is a matter of reading
//! rather than of replay, and what that buys is that everything a golden *can* check, the decision
//! and the statement and the record, is decided above it.
//!
//! Three rules here are load-bearing and easy to lose:
//!
//!   * **The Gemini credential lives in the relay Worker, not here.** With a relay configured this
//!     side sends the placeholder `"relay"`, and the transport strips the key header and sends the
//!     request to the relay with the real target in a header. No Gemini key leaves this Worker.
//!   * **Only approved AI endpoints.** Anything that is not an https URL on the Gemini host or
//!     `api.openai.com` is refused, so a provider configuration cannot become a request to
//!     somewhere else — and a redirect cannot either, because the check is on the URL that is
//!     actually fetched.
//!   * **The Cloudflare provider is a binding, not a URL.** It is reached before the transport, on
//!     the `workers-ai://` scheme the reference reserves for it.

use serde_json::{json, Value};
use worker::*;

use crate::ai::coordinator::{Coordinator, JsonFetcher, ProviderConfig};
use crate::ai::early::Retained;
use crate::ai::resolution::EvidenceFetcher;
use crate::db::Database;
use crate::scheduler::Scheduler;
use crate::source_watch::{Fetcher, WatchError};
use crate::sources::TextResponse;
use crate::writes::Adjudication;

pub const GEMINI_HOST: &str = "generativelanguage.googleapis.com";
pub const OPENAI_HOST: &str = "api.openai.com";
/// The scheme the reference reserves for the Workers AI binding.
pub const WORKERS_AI_SCHEME: &str = "workers-ai://";

fn var(env: &Env, name: &str) -> String {
    env.var(name).map(|value| value.to_string()).unwrap_or_default()
}

/// `gemini_relay`: the proxy URL and its token, or nothing.
///
/// Both halves have to be present. A URL without a token is not a relay, and sending a request to
/// one would put an unauthenticated call on the wire.
pub fn gemini_relay(env: &Env) -> Option<(String, String)> {
    let url = var(env, "AI_PROXY_URL");
    let token = env.secret("AI_PROXY_TOKEN").ok().map(|value| value.to_string());
    match (url.is_empty(), token) {
        (false, Some(token)) if !token.is_empty() => Some((url, token)),
        _ => None,
    }
}

/// `application`'s provider list.
///
/// The Gemini credential is the placeholder `"relay"` when a relay is configured, because the relay
/// holds the only real one; and the Cloudflare provider exists when the `AI` binding does, not when
/// a model name is set.
pub fn providers(env: &Env) -> Vec<ProviderConfig> {
    let mut providers = Vec::new();
    let gemini = if gemini_relay(env).is_some() {
        "relay".to_string()
    } else {
        var(env, "GEMINI_API_KEY")
    };
    if !gemini.is_empty() {
        if let Ok(config) = ProviderConfig::new("gemini", &var(env, "GEMINI_MODEL"), &gemini, None) {
            providers.push(config);
        }
    }
    if env.get_binding::<worker::Ai>("AI").is_ok() {
        if let Ok(config) = ProviderConfig::new("cloudflare", &var(env, "CLOUDFLARE_AI_MODEL"), "", None) {
            providers.push(config);
        }
    }
    providers
}

/// `request_json`'s endpoint allowlist, applied to the URL that is actually fetched.
fn approved(url: &str) -> bool {
    let Some(rest) = url.strip_prefix("https://") else {
        return false;
    };
    let host = rest.split('/').next().unwrap_or("");
    host == GEMINI_HOST || host == OPENAI_HOST
}

/// `request_json`: one AI call, with the relay rewrite and the endpoint allowlist.
///
/// The relay rewrite *removes* the key header rather than leaving it in place: the relay holds the
/// credential, and a request that carried both would send the placeholder for no reason.
pub fn json_fetcher(env: &Env) -> JsonFetcher {
    let relay = gemini_relay(env);
    // The binding is a JavaScript handle and not `Clone`; the closure owns one reference to it
    // and hands it out per call. A Worker is single-threaded, so `Rc` is the right sharing here.
    let ai = env.get_binding::<worker::Ai>("AI").ok().map(std::rc::Rc::new);
    Box::new(move |url, headers, body| {
        let relay = relay.clone();
        let ai = ai.clone();
        Box::pin(async move {
            if let Some(model) = url.strip_prefix(WORKERS_AI_SCHEME) {
                return workers_ai(ai.as_deref(), model, &body).await;
            }
            let mut url = url;
            let mut headers = headers;
            if let Some((relay_url, token)) = relay {
                if url.contains(GEMINI_HOST) {
                    headers.retain(|(name, _)| name.to_lowercase() != "x-goog-api-key");
                    headers.push(("X-Forecast-Proxy-Target".to_string(), url.clone()));
                    headers.push(("Authorization".to_string(), format!("Bearer {token}")));
                    url = relay_url;
                }
            }
            if !approved(&url) {
                return Err(());
            }
            let text = post_json(&url, &headers, &body).await?;
            serde_json::from_str(&text).map_err(|_| ())
        })
    })
}

/// The Workers AI binding, when one is configured.
///
/// A binding rather than a URL, so it is reached before the transport and its failure is a
/// transport failure like any other. The event is logged because a Cloudflare outage is otherwise
/// indistinguishable from the model answering badly.
async fn workers_ai(ai: Option<&worker::Ai>, model: &str, body: &Value) -> Result<Value, ()> {
    let Some(ai) = ai else {
        return Err(());
    };
    match ai.run::<Value, Value>(model, body.clone()).await {
        Ok(value) => Ok(value),
        Err(_) => {
            console_log!(
                "{}",
                json!({"event": "ai_transport_unavailable", "provider": "cloudflare"})
            );
            Err(())
        }
    }
}

/// `request_text`, as the source watch and the evidence collector use it.
pub fn text_fetcher() -> Fetcher {
    Box::new(|url, headers| {
        Box::pin(async move {
            let (status, content_type, body) = get_text(&url, &headers).await?;
            if !(200..300).contains(&status) {
                return Err(());
            }
            Ok(TextResponse {
                status,
                headers: vec![("content-type".to_string(), content_type)],
                body,
            })
        })
    })
}

/// The same transport, in the shape the evidence collector takes.
pub fn evidence_fetcher() -> EvidenceFetcher {
    Box::new(|url, headers| {
        Box::pin(async move {
            let (status, content_type, body) = get_text(&url, &headers).await?;
            if !(200..300).contains(&status) {
                return Err(());
            }
            Ok(TextResponse {
                status,
                headers: vec![("content-type".to_string(), content_type)],
                body,
            })
        })
    })
}

#[cfg(target_arch = "wasm32")]
async fn post_json(url: &str, headers: &[(String, String)], body: &Value) -> Result<String, ()> {
    let init = request_init(Method::Post, headers, Some(body.to_string())).map_err(|_| ())?;
    let request = Request::new_with_init(url, &init).map_err(|_| ())?;
    let mut response = Fetch::Request(request).send().await.map_err(|_| ())?;
    return response.text().await.map_err(|_| ());
}

#[cfg(not(target_arch = "wasm32"))]
async fn post_json(_url: &str, _headers: &[(String, String)], _body: &Value) -> Result<String, ()> {
    Err(())
}

#[cfg(target_arch = "wasm32")]
async fn get_text(url: &str, headers: &[(String, String)]) -> Result<(u16, String, String), ()> {
    let init = request_init(Method::Get, headers, None).map_err(|_| ())?;
    let request = Request::new_with_init(url, &init).map_err(|_| ())?;
    let mut response = Fetch::Request(request).send().await.map_err(|_| ())?;
    let status = response.status_code();
    let content_type = response
        .headers()
        .get("content-type")
        .ok()
        .flatten()
        .unwrap_or_default();
    let body = response.text().await.map_err(|_| ())?;
    Ok((status, content_type, body))
}

#[cfg(not(target_arch = "wasm32"))]
async fn get_text(_url: &str, _headers: &[(String, String)]) -> Result<(u16, String, String), ()> {
    Err(())
}

#[cfg(target_arch = "wasm32")]
fn request_init(method: Method, headers: &[(String, String)], body: Option<String>) -> Result<RequestInit> {
    let mut init = RequestInit::new();
    init.with_method(method);
    let map = Headers::new();
    for (name, value) in headers {
        map.set(name, value)?;
    }
    init.with_headers(map);
    if let Some(body) = body {
        init.with_body(Some(wasm_bindgen::JsValue::from_str(&body)));
    }
    Ok(init)
}

/// Everything a request needs, assembled once.
///
/// The fields are the seams the goldens fill; this struct is what fills them in production, and
/// every method below is a direct call into a layer that *is* held to a vector.
pub struct Application<'a> {
    pub db: &'a dyn Database,
    pub ai: &'a Coordinator,
    pub evidence: &'a EvidenceFetcher,
    pub collector: &'a Fetcher,
    /// The production reader, over the same handle the methods below write through.
    pub reader: Retained<'a>,
    pub now_ms: i64,
    pub token: &'a dyn Fn() -> String,
    pub daily_limit: i64,
    pub source_watch_enabled: bool,
    pub live_markets_enabled: bool,
    pub registry: Option<&'a dyn crate::mutate::FinalizationGate>,
}

impl<'a> Application<'a> {
    /// `Application.automation`: the watcher, wired to this application's own seams.
    pub fn automation(&self) -> crate::automation::Automation<'_> {
        crate::automation::Automation::new(
            self.db,
            Some(self.collector),
            Some(self.ai),
            &self.reader,
            self.now_ms,
            self.token,
            self.source_watch_enabled,
        )
    }

    /// The pass's collaborators, named once. `clock` reads this application's own instant, which is
    /// the request's: a sweep that took its own clock would measure the acceptance deadline against
    /// a different one from the lease it holds.
    async fn sweep(&self, limit: i64, token: &dyn Fn() -> String) -> Result<crate::scheduler::Sweep, String> {
        let mut tokens = || token();
        crate::scheduler::run_due_jobs(
            &Scheduler {
                db: self.db,
                coordinator: self.ai,
                fetch: self.evidence,
                now_ms: self.now_ms,
                clock: &|| self.now_ms,
                daily_limit: self.daily_limit,
                registry: self.registry,
                reader: &self.reader,
            },
            limit,
            &mut tokens,
        )
        .await
    }

    /// `Application.run_due_jobs`.
    pub async fn run_due_jobs(&self, limit: i64) -> Result<Value, String> {
        let sweep = self.sweep(limit, self.token).await?;
        Ok(json!({
            "processed": sweep.processed,
            "failed": sweep.failed,
            "effects": sweep.effects,
        }))
    }

    /// `Application.run_automation`: the operator's one call.
    pub async fn run_automation(&self, limit: i64) -> Result<Value, String> {
        let automation = self.automation();
        let scheduler = Scheduler {
            db: self.db,
            coordinator: self.ai,
            fetch: self.evidence,
            now_ms: self.now_ms,
            clock: &|| self.now_ms,
            daily_limit: self.daily_limit,
            registry: self.registry,
            reader: &self.reader,
        };
        let mut tokens = || (self.token)();
        let mut cron = crate::automation::Cron {
            automation: &automation,
            scheduler: &scheduler,
            tokens: &mut tokens,
            markets_live: self.live_markets_enabled && automation.enabled,
        };
        cron.run(limit).await
    }

    /// `Application.report_evidence`.
    pub async fn report_evidence(&self, user_id: &str, forecast_id: &str, url: &str) -> Result<Value, WatchError> {
        self.automation().report_evidence(user_id, forecast_id, url).await
    }

    /// `Application.adjudicate_forecast`. The route authenticates the operator before this.
    pub async fn adjudicate_forecast(
        &self,
        adjudication: Adjudication<'a>,
    ) -> Result<Value, crate::routes::RouteError> {
        crate::writes::adjudicate_forecast(self.db, self.now_ms, adjudication).await
    }
}
