// Benchmark-only metering. Never record requests, headers, source or answers.
import { appendFileSync } from 'node:fs';
const originalFetch = globalThis.fetch;
const destination = process.env.FD_JEVGREP_USAGE_FILE;
const count = value => Number.isSafeInteger(value) && value >= 0 ? value : null;
if (destination && originalFetch) {
  globalThis.fetch = async function (input, options) {
    let measured = false;
    try {
      const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url);
      measured = url.hostname === 'api.typesafe.ai' && url.pathname === '/v1/systemone';
    } catch {}
    const started = performance.now();
    let response;
    try {
      response = await originalFetch.call(this, input, options);
    } catch (error) {
      if (measured) record({status: null, input_tokens: null, output_tokens: null});
      throw error;
    }
    if (measured) {
      let body;
      try { body = await response.clone().json(); } catch {}
      record({status: response.status, input_tokens: count(body?.usage?.input_tokens),
              output_tokens: count(body?.usage?.output_tokens),
              elapsed_ms: performance.now() - started});
    }
    return response;
  };
}
function record(row) {
  try { appendFileSync(destination, JSON.stringify({schema: 'jevgrep-usage-v1', ...row}) + '\n', {mode: 0o600}); }
  catch { /* Missing receipts make accounting unknown; never alter retrieval. */ }
}
