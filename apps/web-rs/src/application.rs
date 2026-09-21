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
#[cfg(target_arch = "wasm32")]
use worker::wasm_bindgen::JsCast;
use worker::*;

use crate::ai::coordinator::{Coordinator, JsonFetcher, ProviderConfig};
use crate::ai::early::Retained;
use crate::ai::resolution::EvidenceFetcher;
use crate::db::Database;
use crate::scheduler::Scheduler;
use crate::solana_rpc::{Rpc, Signer, SpendAuthorizer};
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

/// The AI coordinator, over the transports above.
pub fn coordinator(env: &Env) -> Coordinator {
    Coordinator {
        providers: providers(env),
        fetch: json_fetcher(env),
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
pub(crate) async fn post_json(url: &str, headers: &[(String, String)], body: &Value) -> Result<String, ()> {
    let init = request_init(Method::Post, headers, Some(body.to_string())).map_err(|_| ())?;
    let request = Request::new_with_init(url, &init).map_err(|_| ())?;
    let mut response = Fetch::Request(request).send().await.map_err(|_| ())?;
    return response.text().await.map_err(|_| ());
}

#[cfg(not(target_arch = "wasm32"))]
pub(crate) async fn post_json(_url: &str, _headers: &[(String, String)], _body: &Value) -> Result<String, ()> {
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

// -------------------------------------------------------------------------------------------
// The chain adapter: the only part of this port that spends money and writes to a chain.
// -------------------------------------------------------------------------------------------

/// The Devnet genesis the transport pins. A reply from anywhere else is not this chain.
pub const DEVNET_GENESIS: &str = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG";
/// The owned gateway, which exists because the public Devnet endpoint answers Cloudflare's egress
/// with HTTP 403 ("your IP or provider is blocked").
pub const REGISTRY_GATEWAY: &str = "https://forecast-rpc.eastsea.xyz/rpc";
/// `rpc.ankr.com` is the only authenticated provider this will talk to. The trailing slash is what
/// makes it a host check rather than a prefix that `rpc.ankr.com.somewhere-else` also satisfies.
pub const KEYED_PROVIDER_PREFIX: &str = "https://rpc.ankr.com/";
/// The reference's own caps, and they are the defaults rather than this port's choices.
pub const MAX_FEE_LAMPORTS: i64 = 20_000;
pub const MAX_RENT_LAMPORTS: i64 = 10_000_000;
pub const BALANCE_FLOOR_LAMPORTS: i64 = 10_000_000;
pub const DAILY_SPEND_LIMIT: i64 = 50_000_000;
/// The response bound and the call's own deadline. Both are the reference's.
pub const RPC_RESPONSE_BYTES: usize = 262_144;
pub const RPC_TIMEOUT_MS: i32 = 20_000;

/// `registry_rpc_urls`: the endpoints this Worker will call, with their headers.
///
/// Ordered: the owned gateway if its credential is present, then an authenticated provider, then
/// the public list. The gateway's token is a *64-hex* string — a shorter one is not a credential
/// and is refused rather than sent.
pub fn registry_urls(env: &Env) -> Vec<(String, Vec<(String, String)>)> {
    let mut urls = Vec::new();
    let proxy = var(env, "SOLANA_RPC_PROXY");
    if !proxy.is_empty() {
        let token = env
            .secret("SOLANA_RPC_PROXY_TOKEN")
            .ok()
            .map(|value| value.to_string())
            .unwrap_or_default();
        let valid = token.len() == 64
            && token
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase());
        if proxy != REGISTRY_GATEWAY || !valid {
            // The reference refuses rather than falling through: a configured gateway that is not
            // the owned one, or one without its credential, is a configuration error and calling
            // the public endpoint instead would hide it.
            return Vec::new();
        }
        urls.push((
            proxy,
            vec![
                ("X-Forecast-RPC-Token".to_string(), token),
                ("Content-Type".to_string(), "application/json".to_string()),
            ],
        ));
    }
    let keyed = var(env, "SOLANA_DEVNET_RPC_KEYED");
    if !keyed.is_empty() {
        if !keyed.starts_with(KEYED_PROVIDER_PREFIX) {
            return Vec::new();
        }
        urls.push((
            keyed,
            vec![("Content-Type".to_string(), "application/json".to_string())],
        ));
    }
    for url in var(env, "SOLANA_DEVNET_RPC").split(',') {
        let url = url.trim();
        if !url.is_empty() {
            urls.push((
                url.to_string(),
                vec![("Content-Type".to_string(), "application/json".to_string())],
            ));
        }
    }
    urls
}

/// `registry_rpc`: one JSON-RPC call, with the failover the reference does.
///
/// The envelope is *checked*, not merely parsed: `jsonrpc`, the id, a present `result` and an
/// absent `error`. A provider that answers with something else has not answered, and accepting it
/// would turn a transport fault into an invented chain state.
///
/// The id is fixed at 1 and a reply carrying another one is refused for the same reason: this call
/// has exactly one outstanding request, and a mismatched id is a reply to a question nobody asked.
pub fn registry_rpc<'a>(env: &Env) -> Box<Rpc<'a>> {
    let urls = registry_urls(env);
    Box::new(move |method, params| {
        let urls = urls.clone();
        Box::pin(async move {
            for (url, headers) in urls {
                let body = json!({"jsonrpc": "2.0", "id": 1, "method": method, "params": params});
                let Ok(text) = post_json(&url, &headers, &body).await else {
                    continue;
                };
                if text.len() > RPC_RESPONSE_BYTES {
                    continue;
                }
                let Ok(reply) = serde_json::from_str::<Value>(&text) else {
                    continue;
                };
                if reply.get("jsonrpc") != Some(&json!("2.0"))
                    || reply.get("id") != Some(&json!(1))
                    || reply.get("error").is_some()
                {
                    continue;
                }
                if let Some(result) = reply.get("result") {
                    return Ok(result.clone());
                }
            }
            Err(())
        })
    })
}

/// The relayer's identity: the bytes it signs as, and the key id the feed names it by.
///
/// One struct rather than two fields, because the key id is the hash of *those* bytes and a caller
/// that could pair one identity's name with another's key would be signing under a name nobody
/// verified. Both feed versions name their key this way, so it lives here rather than with either.
pub struct Identity {
    pub public_key_hex: String,
    pub key_id: String,
}

impl Identity {
    /// `publish_risk_v2`'s and `publish_feed`'s key id: `"forecast-relayer-" + sha256(key)[:16]`.
    /// The reference's `relayer_public_key()` is `base58_decode(SOLANA_RELAYER)`, and it is the
    /// *bytes* that are hashed — hashing the address that spells them is a different key id.
    pub fn of(public_key: &[u8]) -> Self {
        use sha2::{Digest, Sha256};
        let digest = hex::encode(Sha256::digest(public_key));
        Self {
            public_key_hex: hex::encode(public_key),
            key_id: format!("forecast-relayer-{}", &digest[..16]),
        }
    }
}

/// The relayer, both halves at once: the signer and the name it signs under.
pub fn relayer_identity(env: &Env) -> Option<Identity> {
    relayer_public_key(env).as_deref().map(Identity::of)
}

/// `relayer_public_key`: the address the relayer signs as, or `None` when it is not configured.
///
/// The seed is required as well as the address, because an address without a key is an operator who
/// cannot sign — and the caller that needs the key id needs it for a signature that will exist.
/// The feed's key id is the hash of *these bytes*, so the two must be derived from one place.
pub fn relayer_public_key(env: &Env) -> Option<Vec<u8>> {
    let seed = env.secret("SOLANA_RELAYER_SEED").ok()?.to_string();
    if seed.is_empty() {
        return None;
    }
    let relayer = var(env, "SOLANA_RELAYER");
    if relayer.is_empty() {
        return None;
    }
    let decoded = bs58::decode(relayer).into_vec().ok()?;
    (decoded.len() == 32).then_some(decoded)
}

/// `sign_registry_message`: the relayer's Ed25519 signature, from the runtime.
///
/// WebCrypto rather than a Rust implementation, and the identity is *verified* before the signature
/// is returned: a seed that does not correspond to the configured relayer is a misconfiguration,
/// and signing with it would produce transactions the chain rejects — after the spend was
/// authorized. The public key is not a secret; the seed is, and it never leaves this function.
pub fn relayer_signer<'a>(env: &Env) -> Option<Box<Signer<'a>>> {
    let encoded = env.secret("SOLANA_RELAYER_SEED").ok()?.to_string();
    let seed = base64::Engine::decode(&base64::engine::general_purpose::STANDARD, encoded).ok()?;
    if seed.len() != 32 {
        return None;
    }
    let expected = relayer_public_key(env)?;
    // PKCS#8 wrapping for a raw Ed25519 seed: the fixed prefix the reference prepends.
    let mut pkcs8 = vec![
        0x30, 0x2e, 0x02, 0x01, 0x00, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x70, 0x04, 0x22, 0x04, 0x20,
    ];
    pkcs8.extend_from_slice(&seed);
    Some(Box::new(move |message: Vec<u8>| {
        let expected = expected.clone();
        let pkcs8 = pkcs8.clone();
        Box::pin(async move {
            let signature = web_crypto_sign(&pkcs8, &message).await.ok_or(())?;
            if !web_crypto_verify(&expected, &message, &signature).await {
                return Err(());
            }
            let mut out = [0u8; 64];
            if signature.len() != 64 {
                return Err(());
            }
            out.copy_from_slice(&signature);
            Ok(out)
        })
    }))
}

/// `authorize_spend`: the daily cap, reserved before anything is signed.
pub fn spend_authorizer<'a>(db: &'a dyn Database, now_ms: &'a dyn Fn() -> i64) -> Box<SpendAuthorizer<'a>> {
    Box::new(move |amount| {
        Box::pin(async move {
            crate::registry_chain::reserve_daily_spend(db, amount, now_ms(), DAILY_SPEND_LIMIT)
                .await
                .map_err(|error| error.code)
        })
    })
}

/// The signed message, via the runtime's own Ed25519.
#[cfg(target_arch = "wasm32")]
async fn web_crypto_sign(pkcs8: &[u8], message: &[u8]) -> Option<Vec<u8>> {
    let subtle = subtle_crypto()?;
    let key = import_key(&subtle, "pkcs8", pkcs8, "sign").await?;
    let promise = subtle.sign_with_str_and_u8_array("Ed25519", &key, message).ok()?;
    let value = worker::wasm_bindgen_futures::JsFuture::from(promise).await.ok()?;
    Some(js_sys::Uint8Array::new(&value).to_vec())
}

#[cfg(not(target_arch = "wasm32"))]
async fn web_crypto_sign(_pkcs8: &[u8], _message: &[u8]) -> Option<Vec<u8>> {
    None
}

/// `WalletLogin.verify_signature`: the same WebCrypto verification, exposed as the seam the wallet
/// sign-in takes. A wallet's signature is checked by the runtime's own implementation for the same
/// reason the relayer's is: no part of this crate hand-rolls Ed25519.
pub fn signature_verifier() -> Box<crate::wallets::SignatureVerifier> {
    Box::new(|public_key: Vec<u8>, message: Vec<u8>, signature: Vec<u8>| {
        Box::pin(async move { Ok(web_crypto_verify(&public_key, &message, &signature).await) })
    })
}

/// The signing identity check. A signature nobody can verify against the configured relayer is
/// worse than no signature: it would be *accepted* by this Worker and rejected by the chain.
#[cfg(target_arch = "wasm32")]
async fn web_crypto_verify(public_key: &[u8], message: &[u8], signature: &[u8]) -> bool {
    let Some(subtle) = subtle_crypto() else {
        return false;
    };
    let Some(key) = import_key(&subtle, "raw", public_key, "verify").await else {
        return false;
    };
    let data: js_sys::Object = js_sys::Uint8Array::from(message).unchecked_into();
    let Ok(promise) = subtle.verify_with_str_and_u8_array_and_buffer_source("Ed25519", &key, signature, &data) else {
        return false;
    };
    worker::wasm_bindgen_futures::JsFuture::from(promise)
        .await
        .ok()
        .and_then(|value| value.as_bool())
        .unwrap_or(false)
}

#[cfg(not(target_arch = "wasm32"))]
async fn web_crypto_verify(_public_key: &[u8], _message: &[u8], _signature: &[u8]) -> bool {
    false
}

#[cfg(target_arch = "wasm32")]
fn subtle_crypto() -> Option<web_sys::SubtleCrypto> {
    let crypto = js_sys::Reflect::get(&js_sys::global(), &worker::wasm_bindgen::JsValue::from_str("crypto")).ok()?;
    let crypto: web_sys::Crypto = crypto.dyn_into().ok()?;
    Some(crypto.subtle())
}

#[cfg(target_arch = "wasm32")]
async fn import_key(
    subtle: &web_sys::SubtleCrypto,
    format: &str,
    material: &[u8],
    usage: &str,
) -> Option<web_sys::CryptoKey> {
    let key_data: js_sys::Object = js_sys::Uint8Array::from(material).unchecked_into();
    let algorithm = js_sys::Object::new();
    js_sys::Reflect::set(
        &algorithm,
        &worker::wasm_bindgen::JsValue::from_str("name"),
        &worker::wasm_bindgen::JsValue::from_str("Ed25519"),
    )
    .ok()?;
    let usages = js_sys::Array::new();
    usages.push(&worker::wasm_bindgen::JsValue::from_str(usage));
    let promise = subtle
        .import_key_with_object(format, &key_data, &algorithm, false, usages.as_ref())
        .ok()?;
    let value = worker::wasm_bindgen_futures::JsFuture::from(promise).await.ok()?;
    value.dyn_into::<web_sys::CryptoKey>().ok()
}

/// The pieces the chain transport borrows, owned together.
///
/// Bound to a local by the caller, which then builds the transport from it: a struct that held both
/// would be self-referential, and the registry borrows the transport, which borrows these.
pub struct ChainParts<'a> {
    pub rpc: Box<Rpc<'a>>,
    pub sign: Box<Signer<'a>>,
    pub authorize: Box<SpendAuthorizer<'a>>,
}

impl<'a> ChainParts<'a> {
    /// `registry`: the transport, with the reference's own caps and pinned genesis.
    pub fn transport(
        &self,
        program_id: &[u8],
        relayer: &[u8],
    ) -> Result<crate::solana_rpc::SolanaRpcTransport<'_, 'a>, crate::solana_rpc::SolanaRpcError> {
        crate::solana_rpc::SolanaRpcTransport::new(
            &*self.rpc,
            &*self.sign,
            program_id,
            relayer,
            DEVNET_GENESIS,
            &*self.authorize,
            MAX_FEE_LAMPORTS,
            MAX_RENT_LAMPORTS,
            BALANCE_FLOOR_LAMPORTS,
        )
    }
}

/// `entry.registry`: the three pieces, when a chain adapter is configured at all.
///
/// `None` is a real configuration — a deployment with no registry does not finalize anything on a
/// chain — and the enabled flag plus a decodable program and relayer are what make it `Some`.
pub fn chain_parts<'a>(env: &Env, db: &'a dyn Database, now_ms: &'a dyn Fn() -> i64) -> Option<ChainParts<'a>> {
    if var(env, "SOLANA_REGISTRY_ENABLED").to_lowercase() != "true" {
        return None;
    }
    let program = bs58::decode(var(env, "SOLANA_PROGRAM_ID")).into_vec().ok()?;
    let relayer = bs58::decode(var(env, "SOLANA_RELAYER")).into_vec().ok()?;
    if program.len() != 32 || relayer.len() != 32 || program == relayer {
        return None;
    }
    Some(ChainParts {
        rpc: registry_rpc(env),
        // A registry without a signing identity cannot send anything, and a `Signer` that refuses
        // every message would look like a transport fault; refusing to build one is the honest
        // answer, and it leaves the adapter `None` rather than half-configured.
        sign: relayer_signer(env)?,
        authorize: spend_authorizer(db, now_ms),
    })
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

#[cfg(test)]
mod tests {
    use super::*;

    /// The key id is the hash of the *key*, not of the address that spells it.
    ///
    /// The reference writes `"forecast-relayer-" + sha256(relayer_public_key()).hexdigest()[:16]`,
    /// where `relayer_public_key()` is `base58_decode(SOLANA_RELAYER)`. Hashing the address string
    /// instead is a plausible-looking port and a different key id, which would name a key that no
    /// verifier can find. The expected values below are that expression, run:
    ///
    /// ```text
    /// >>> base58_encode(b"forecast-network-relayer-key32ab!")   # 32 bytes
    /// '7ts9fNsa3obSDTBSHexSZFGJeWKzFTcB6e8mXoGgxbjo'
    /// >>> hashlib.sha256(key).hexdigest()[:16]
    /// 'd14649b3c9d5bad6'
    /// ```
    #[test]
    fn the_key_id_is_the_hash_of_the_key_not_of_its_address() {
        let address = "7ts9fNsa3obSDTBSHexSZFGJeWKzFTcB6e8mXoGgxbjo";
        let public_key = bs58::decode(address).into_vec().expect("a base58 address");
        assert_eq!(public_key.len(), 32);
        let identity = Identity::of(&public_key);
        assert_eq!(
            identity.public_key_hex,
            "666f7265636173742d6e6574776f726b2d72656c617965722d6b657933326162"
        );
        assert_eq!(identity.key_id, "forecast-relayer-d14649b3c9d5bad6");
    }
}
