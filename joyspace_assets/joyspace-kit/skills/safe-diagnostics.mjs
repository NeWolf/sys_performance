// Private, bounded JSONL channel. Never serialize inputs, errors or response bodies.
import { appendFileSync } from 'node:fs';
import { performance } from 'node:perf_hooks';

const destination = process.env.JOYSPACE_DIAGNOSTIC_FILE;
const sources = new Set(['env', 'config', 'tokenGrant', 'legacy']);
const gatewayRoutes = new Map([
  ['desk.agent.auth.encrypt', 'encrypt'],
  ['desk.agent.auth.tokenGrant', 'tokenGrant'],
  ['desk.agent.auth.getWebToken', 'getWebToken'],
]);
const errorCodes = new Set([
  'ECONNREFUSED', 'ECONNRESET', 'ETIMEDOUT', 'ENOTFOUND', 'EAI_AGAIN',
  'UND_ERR_CONNECT_TIMEOUT', 'UND_ERR_HEADERS_TIMEOUT', 'UND_ERR_BODY_TIMEOUT',
  'CERT_HAS_EXPIRED', 'UNABLE_TO_VERIFY_LEAF_SIGNATURE',
]);
let records = 0;

function write(record) {
  if (!destination || records >= 512) return;
  try {
    appendFileSync(destination, JSON.stringify(record) + '\n', { encoding: 'utf8', flag: 'a' });
    records += 1;
  } catch { /* Diagnostics must never change the operation. */ }
}

export function authBranch(source) {
  if (sources.has(source)) write({ stage: 'joyspace.auth', source, status: 'selected' });
}

export function authProcess(status, elapsedMs = 0) {
  try {
    if (!new Set(['begin', 'ok', 'error', 'timeout']).has(status)) return;
    const elapsed_ms = Number.isFinite(elapsedMs)
      ? Math.max(0, Math.min(86400000, Math.round(elapsedMs))) : 0;
    write({ stage: 'joyspace.auth_process', status, elapsed_ms,
      ...(status === 'begin' ? { started_ms: Date.now() } : {}) });
  } catch { /* Clock and record preparation failures are diagnostic-only. */ }
}

export function gatewayResult(functionId, result) {
  const operation = gatewayRoutes.get(functionId);
  if (!destination || !operation) return;
  try {
    const valid = result?.code === 0 && (operation === 'encrypt'
      ? Boolean(result?.data?.aesKey && result?.data?.content)
      : Boolean(result?.data?.accessToken));
    write({ stage: 'joyspace.gateway_result', operation,
      status: valid ? 'ok' : 'error', code: valid ? 'ok' : 'remote_error' });
  } catch { /* Metadata inspection must not affect callers. */ }
}

function classify(input) {
  // Only literal string URLs used by bundled scripts; never coerce arbitrary objects.
  if (typeof input !== 'string') return null;
  try {
    const url = new URL(input);
    if (url.username || url.password || url.hash) return null;
    if (url.origin === 'https://api.m.jd.com' && url.pathname === '/') {
      if (url.searchParams.getAll('functionId').length !== 1) return null;
      const operation = gatewayRoutes.get(url.searchParams.get('functionId'));
      return operation ? { stage: 'joyspace.gateway', operation } : null;
    }
    const port = Number(url.port);
    if (url.protocol === 'http:' && url.hostname === '127.0.0.1'
        && url.pathname === '/hioffice' && port >= 8988 && port <= 9006 && port % 2 === 0) {
      return { stage: 'joyspace.port', operation: 'hioffice', port,
        attempt: (port - 8988) / 2 + 1 };
    }
  } catch { /* Unknown targets do not produce diagnostics. */ }
  return null;
}

export function safeError(error) {
  try {
    for (const code of [error?.code, error?.cause?.code]) {
      if (errorCodes.has(code)) return code;
    }
    if (error?.name === 'AbortError' || error?.name === 'TimeoutError') return 'timeout';
  } catch { /* Do not inspect exception text. */ }
  return 'unknown_error';
}

function elapsed(started) {
  const value = Math.round(performance.now() - started);
  return Number.isFinite(value) ? Math.max(0, Math.min(86400000, value)) : 0;
}

const responses = new WeakMap();

function failed(target, started, error) {
  try {
    const code = safeError(error);
    const timedOut = new Set(['timeout', 'ETIMEDOUT', 'UND_ERR_CONNECT_TIMEOUT',
      'UND_ERR_HEADERS_TIMEOUT', 'UND_ERR_BODY_TIMEOUT']).has(code);
    write({ ...target, status: timedOut ? 'timeout' : 'error', code,
      elapsed_ms: elapsed(started) });
  } catch { /* Failure reporting must preserve the business exception. */ }
}

// Called only where business code already consumes json/text. Preserve receiver,
// result/error identity and read count; never clone, drain or retry the response.
export async function diagnosticBody(response, method) {
  let observation;
  try {
    observation = responses.get(response);
  } catch { /* Observation lookup must not prevent the original body read. */ }
  let result;
  try {
    result = await response[method]();
  } catch (error) {
    try {
      if (observation) {
        responses.delete(response);
        failed({ ...observation.fields, phase: 'body' }, observation.started, error);
      }
    } catch { /* Cleanup must not replace the original body exception. */ }
    throw error;
  }
  try {
    if (observation) {
      responses.delete(response);
      write({ ...observation.fields, phase: 'body', status: 'ok', code: 'ok',
        elapsed_ms: elapsed(observation.started) });
    }
  } catch { /* Successful reads remain successful even if finalization fails. */ }
  return result;
}

export async function diagnosticFetch(...args) {
  const target = destination ? classify(args[0]) : null;
  if (!target) return globalThis.fetch(...args);
  let started;
  try {
    started = performance.now();
    // Private timestamp is used only to close interrupted requests in the parent;
    // it is never forwarded as a diagnostic field.
    write({ ...target, status: 'begin', started_ms: Date.now() });
  } catch {
    // No usable observation: execute the original request once, without diagnostics.
    return globalThis.fetch(...args);
  }
  try {
    const response = await globalThis.fetch(...args);
    // Observe only safe transport metadata, never clone or consume the body.
    try {
      const fields = { ...target, elapsed_ms: elapsed(started) };
      if (Number.isInteger(response.status) && response.status >= 100 && response.status <= 599) {
        fields.http_status = response.status;
      }
      const missing = target.stage === 'joyspace.port'
        && !response.headers.get('X-AES-Key');
      if (target.stage === 'joyspace.port') fields.has_auth_header = !missing;
      const accepted = response.ok && !missing;
      write({ ...fields, phase: 'headers', status: accepted ? 'ok' : 'error',
        code: !response.ok ? 'http_error' : missing ? 'missing_auth_header' : 'ok' });
      if (accepted) responses.set(response, { fields, started });
    } catch { /* A metadata failure must not replace the original response. */ }
    return response;
  } catch (error) {
    failed(target, started, error);
    throw error;
  }
}