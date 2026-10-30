/** Direct MCP client for hosts with JavaScript execution, including node_repl.
 * Queue/compile/reload waiting stays in the existing service, outside Unity.
 * No subprocess, Unity ingress, polling, automatic replay, or service restart.
 */
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ENDPOINT = 'http://127.0.0.1:8091/mcp';
export const WORKSPACE = path.resolve(fileURLToPath(new URL('../../', import.meta.url)));

export class WorkflowObservationError extends Error {
  constructor(message, { submissionKey, acceptanceUnknown, cause } = {}) {
    super(message, { cause });
    this.name = 'WorkflowObservationError';
    this.submissionKey = submissionKey;
    this.acceptanceUnknown = acceptanceUnknown;
    this.recovery = 'Recover the same key with operation=result; do not submit a new key.';
  }
}

/** Start one observation without holding the host's tool invocation open.
 * Keep this handle in a persistent JavaScript host and later await completion.
 * The same Promise remains active; inspecting it sends no additional MCP calls.
 * Resetting the host loses this observer, not the durable job: recover its key.
 */
export function startWorkflow(parameters) {
  let status = 'observing';
  let result;
  let error;
  const trace = [];
  const completion = workflow({ ...parameters, onTrace: event => trace.push(Object.freeze(event)) }).then(value => {
    result = value;
    status = 'completed';
    return value;
  }, failure => {
    error = failure;
    status = 'observation_failed';
    throw failure;
  });
  // A host may perform other work before it awaits this completion.
  completion.catch(() => {});
  return Object.freeze({
    key: parameters?.key,
    get status() { return status; },
    get result() { return result; },
    get error() { return error; },
    // Copy the list; entries contain only safe routing metadata, never headers,
    // session credentials, manifest contents, source code or error messages.
    get trace() { return trace.slice(); },
    completion,
  });
}

/** Collect the existing observation within a short tool-host budget.
 * A local deadline yields control only: no RPC, cancellation, resubmission,
 * or change to handle.completion. Keep the handle when status is observing.
 * The caller's tool timeout must exceed waitMilliseconds plus host overhead.
 */
export async function observeWorkflow(handle, { waitMilliseconds = 20000 } = {}) {
  if (!Number.isInteger(waitMilliseconds) || waitMilliseconds < 0 || waitMilliseconds > 30000)
    throw new TypeError('waitMilliseconds must be an integer in 0..30000');
  if (!handle || typeof handle.key !== 'string' || typeof handle.completion?.then !== 'function')
    throw new TypeError('observeWorkflow requires an existing workflow handle');
  let timer;
  try {
    return await Promise.race([
      handle.completion.then(result => ({ key: handle.key, status: 'completed', result })),
      new Promise(resolve => {
        timer = setTimeout(() => resolve({ key: handle.key, status: 'observing' }), waitMilliseconds);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

function decode(body, id) {
  if (body.trimStart().startsWith('{')) {
    const reply = JSON.parse(body);
    if (reply.id !== id) throw new Error('MCP response ID mismatch');
    return reply;
  }
  for (const event of body.split(/\r?\n\r?\n/)) {
    const data = event.split(/\r?\n/)
      .filter(line => line.startsWith('data:'))
      .map(line => line.slice(5).trimStart()).join('\n');
    if (!data) continue;
    const reply = JSON.parse(data);
    if (reply.id === id) return reply;
  }
  throw new Error('No matching MCP response');
}

function toolValue(reply) {
  if (reply.error) throw new Error(reply.error.message || 'MCP protocol error');
  if (reply.result?.structuredContent) return reply.result.structuredContent;
  const content = reply.result?.content?.find(item => item.type === 'text');
  if (!content) throw new Error('MCP tool returned no structured result');
  return JSON.parse(content.text);
}

/** One submitted/recovered job per call. Cancellation stops observation only.
 * A false verified_success remains false; successful transport is not success.
 * fetchImpl is a transport seam for isolated tests, not an alternate endpoint.
 */
export async function workflow({ mode = 'job', operation = 'run', key, manifest,
  waitSeconds = 1800, signal, fetchImpl = globalThis.fetch, onTrace } = {}) {
  if (!['job', 'phase'].includes(mode)) throw new TypeError('mode must be job or phase');
  if (!['run', 'result'].includes(operation)) throw new TypeError('operation must be run or result');
  if (typeof key !== 'string' || key.length < 1 || key.length > 120)
    throw new TypeError('key must contain 1..120 characters');
  if (operation === 'run' && (typeof manifest !== 'string' || !manifest))
    throw new TypeError('run requires a staged manifest');
  if (!Number.isInteger(waitSeconds) || waitSeconds < 0 || waitSeconds > 1800)
    throw new TypeError('waitSeconds must be an integer in 0..1800');
  let session;
  let sequence = 0;
  let sentOperation = false;
  function record(event) {
    // Observability must never change admission, cleanup or task outcomes.
    try { onTrace?.({ at: new Date().toISOString(), key, mode, operation, ...event }); }
    catch { /* A broken evidence sink cannot replay or fail a workflow. */ }
  }

  async function rpc(method, params, notification = false) {
    const id = notification ? undefined : ++sequence;
    const headers = { 'Content-Type': 'application/json', Accept: 'application/json, text/event-stream' };
    if (session) headers['Mcp-Session-Id'] = session;
    const observing = method === 'tools/call'
      && /^unity_(job|phase)_(run|result)$/.test(params.name);
    const timeout = AbortSignal.timeout(observing ? (waitSeconds + 30) * 1000 : 10000);
    const metadata = { method, rpcId: id ?? null, tool: params?.name ?? null,
      purpose: observing ? (operation === 'run' ? 'submission' : 'recovery') : 'initialization' };
    record({ stage: 'send', ...metadata });
    const response = await fetchImpl(ENDPOINT, {
      method: 'POST', headers,
      body: JSON.stringify({ jsonrpc: '2.0', ...(notification ? {} : { id }), method, params }),
      signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
    });
    record({ stage: 'response', ...metadata, httpStatus: response.status });
    session = response.headers.get('mcp-session-id') || session;
    if (!response.ok) throw new Error(`MCP HTTP ${response.status}`);
    const body = await response.text();
    if (notification && !body.trim()) return null;
    return decode(body, id);
  }

  try {
    const initialized = await rpc('initialize', {
      protocolVersion: '2024-11-05', capabilities: {},
      clientInfo: { name: 'unity-workflow-direct-client', version: '1' },
    });
    if (initialized.error) throw new Error('MCP initialization failed');
    await rpc('notifications/initialized', {}, true);
    const capabilities = toolValue(await rpc('tools/call', {
      name: 'unity_workflow_capabilities', arguments: {},
    }));
    if (typeof capabilities.workspace !== 'string'
        || path.resolve(capabilities.workspace).toLowerCase() !== WORKSPACE.toLowerCase())
      throw new Error('Workflow service belongs to a different workspace');
    const parameters = { submission_key: key, wait_seconds: waitSeconds };
    if (operation === 'run') parameters.manifest_path = manifest;
    sentOperation = true;
    const value = toolValue(await rpc('tools/call', {
      name: `unity_${mode}_${operation}`, arguments: parameters,
    }));
    record({ stage: 'outcome', job_id: value.job_id ?? null,
      verified_success: value.verified_success ?? null,
      task_verified_success: value.task_verified_success ?? null });
    return value;
  } catch (cause) {
    record({ stage: 'error', acceptanceUnknown: sentOperation });
    throw new WorkflowObservationError(cause.message, {
      submissionKey: key, acceptanceUnknown: sentOperation, cause,
    });
  } finally {
    if (session) {
      // Close this observation session only. The durable executor keeps running.
      try {
        record({ stage: 'send', method: 'DELETE', rpcId: null, tool: null, purpose: 'observation_cleanup' });
        const closed = await fetchImpl(ENDPOINT, { method: 'DELETE', headers: { 'Mcp-Session-Id': session },
          signal: AbortSignal.timeout(5000) });
        record({ stage: 'response', method: 'DELETE', httpStatus: closed.status, purpose: 'observation_cleanup' });
      } catch { record({ stage: 'close_error', purpose: 'observation_cleanup' }); }
    }
  }
}
