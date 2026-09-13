/**
 * Region-placed relay for Gemini requests from the Forecast Worker.
 *
 * The Forecast Worker itself now runs at the edge next to its users; Gemini rejects
 * some of those execution locations, so only this Worker is pinned to a permitted
 * region. It forwards nothing but authenticated POSTs to the one allowed host and
 * passes through only the headers Gemini needs. Its only secrets are the shared
 * bearer token and the Gemini credential, which no other Worker holds.
 */
const ALLOWED_HOST = 'generativelanguage.googleapis.com';
const FORWARDED_HEADERS = ['content-type'];
const MAX_BODY_BYTES = 1024 * 1024;

function constantTimeEqual(left, right) {
  const encoder = new TextEncoder();
  const a = encoder.encode(left), b = encoder.encode(right);
  if (a.byteLength !== b.byteLength) return false;
  let diff = 0;
  for (let i = 0; i < a.byteLength; i += 1) diff |= a[i] ^ b[i];
  return diff === 0;
}

export default {
  async fetch(request, env) {
    const token = env.PROXY_TOKEN;
    const supplied = request.headers.get('authorization') || '';
    if (typeof token !== 'string' || token.length < 32 || !constantTimeEqual(supplied, `Bearer ${token}`)) {
      return new Response(null, { status: 403 });
    }
    if (request.method !== 'POST') return new Response(null, { status: 405 });
    let target;
    try { target = new URL(request.headers.get('x-forecast-proxy-target') || ''); } catch { target = null; }
    if (!target || target.protocol !== 'https:' || target.hostname !== ALLOWED_HOST) {
      return new Response(null, { status: 400 });
    }
    const declared = Number(request.headers.get('content-length') || 0);
    if (declared > MAX_BODY_BYTES) return new Response(null, { status: 413 });
    const body = await request.arrayBuffer();
    if (body.byteLength > MAX_BODY_BYTES) return new Response(null, { status: 413 });
    const headers = new Headers();
    for (const name of FORWARDED_HEADERS) {
      const value = request.headers.get(name);
      if (value) headers.set(name, value);
    }
    // The Gemini credential exists only here; callers never send one.
    if (typeof env.GEMINI_API_KEY !== 'string' || env.GEMINI_API_KEY.length < 20) {
      return new Response(null, { status: 503 });
    }
    headers.set('x-goog-api-key', env.GEMINI_API_KEY);
    return fetch(target.toString(), { method: 'POST', headers, body, redirect: 'manual' });
  },
};
