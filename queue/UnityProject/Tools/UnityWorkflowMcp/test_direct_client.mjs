import test from 'node:test';
import assert from 'node:assert/strict';
import { workflow, startWorkflow, WORKSPACE, WorkflowObservationError } from './direct_client.mjs';

function transport({ workspace = WORKSPACE, onOperation, sse = false } = {}) {
  const requests = [];
  let closed = 0;
  async function fetchImpl(url, options) {
    assert.equal(url, 'http://127.0.0.1:8091/mcp');
    if (options.method === 'DELETE') { closed++; return new Response(null, { status: 204 }); }
    const request = JSON.parse(options.body);
    requests.push(request);
    if (request.method === 'notifications/initialized') return new Response(null, { status: 202 });
    let result;
    if (request.method === 'initialize') result = { protocolVersion: '2024-11-05', capabilities: {} };
    else if (request.params.name === 'unity_workflow_capabilities') result = { structuredContent: { workspace } };
    else result = { structuredContent: await onOperation(request.params, options.signal) };
    const body = JSON.stringify({ jsonrpc: '2.0', id: request.id, result });
    return new Response(sse ? ': keepalive\n\nevent: message\ndata: ' + body + '\n\n' : body,
      { headers: { 'mcp-session-id': 'private-test-session' } });
  }
  return { fetchImpl, requests, get closed() { return closed; } };
}

test('one open operation waits at the service; no Unity or poll calls', async () => {
  let release, entered;
  const accepted = new Promise(resolve => { entered = resolve; });
  const completion = new Promise(resolve => { release = resolve; });
  const t = transport({ onOperation: async p => {
    assert.equal(p.name, 'unity_job_run'); entered(); return completion;
  } });
  const pending = workflow({ key: 'compile-one', manifest: 'staged/files.json', fetchImpl: t.fetchImpl });
  await accepted;
  assert.deepEqual(t.requests.filter(r => r.method === 'tools/call').map(r => r.params.name),
    ['unity_workflow_capabilities', 'unity_job_run']);
  release({ job_id: 'one', verified_success: true, completed: true });
  assert.equal((await pending).job_id, 'one');
  assert.equal(t.closed, 1);
});

test('wrong workspace refuses before a mutation can be submitted', async () => {
  const t = transport({ workspace: WORKSPACE + '-other', onOperation: () => assert.fail('submitted') });
  await assert.rejects(workflow({ key: 'wrong', manifest: 'staged/files.json', fetchImpl: t.fetchImpl }),
    e => e instanceof WorkflowObservationError && !e.acceptanceUnknown);
  assert.equal(t.closed, 1);
});

test('cancelled observer never replays or cancels the durable job', async () => {
  const controller = new AbortController();
  let entered;
  const accepted = new Promise(resolve => { entered = resolve; });
  const t = transport({ onOperation: (_, signal) => new Promise((_, reject) => {
    signal.addEventListener('abort', () => reject(signal.reason), { once: true }); entered();
  }) });
  const pending = workflow({ key: 'accepted-once', manifest: 'staged/files.json', signal: controller.signal, fetchImpl: t.fetchImpl });
  await accepted;
  controller.abort(new Error('Observer disconnected'));
  await assert.rejects(pending, e => e.acceptanceUnknown && e.submissionKey === 'accepted-once');
  assert.equal(t.requests.filter(r => r.params?.name === 'unity_job_run').length, 1);
  assert.equal(t.closed, 1);
});

test('same-key recovery uses result only and parses Streamable HTTP events', async () => {
  const t = transport({ sse: true, onOperation: p => {
    assert.deepEqual(p, { name: 'unity_phase_result', arguments: { submission_key: 'accepted-once', wait_seconds: 1800 } });
    return { job_id: 'original', verified_success: true };
  } });
  assert.equal((await workflow({ mode: 'phase', operation: 'result', key: 'accepted-once', fetchImpl: t.fetchImpl })).job_id, 'original');
});

test('quarantine and false verification cannot become success', async () => {
  const result = { state: 'quarantined', verified_success: false, attention_required: true };
  const t = transport({ onOperation: () => result });
  assert.deepEqual(await workflow({ key: 'quarantine', manifest: 'staged/files.json', fetchImpl: t.fetchImpl }), result);
});

test('invalid input makes no network calls', async () => {
  const t = transport();
  await assert.rejects(workflow({ key: 'bad', manifest: 'staged/files.json', waitSeconds: 1801, fetchImpl: t.fetchImpl }), TypeError);
  assert.equal(t.requests.length, 0);
});

test('host returns while the same MCP observation waits; collection sends nothing', async () => {
  let release, entered;
  const accepted = new Promise(resolve => { entered = resolve; });
  const waiting = new Promise(resolve => { release = resolve; });
  const t = transport({ onOperation: () => { entered(); return waiting; } });
  const handle = startWorkflow({ key: 'long-queue', manifest: 'staged/files.json', fetchImpl: t.fetchImpl });
  assert.equal(handle.status, 'observing');
  await accepted;
  assert.equal(handle.status, 'observing');
  assert.equal(handle.result, undefined);
  const count = t.requests.length;
  release({ job_id: 'long-queue-job', verified_success: true });
  assert.equal((await handle.completion).verified_success, true);
  assert.equal(handle.status, 'completed');
  assert.equal(handle.result.job_id, 'long-queue-job');
  assert.equal(t.requests.length, count);
});

test('deferred observation retains the failed result key without replay', async () => {
  const t = transport({ onOperation: () => { throw new Error('Connection lost after acceptance'); } });
  const handle = startWorkflow({ key: 'lost-reply', manifest: 'staged/files.json', fetchImpl: t.fetchImpl });
  await assert.rejects(handle.completion, e => e.submissionKey === 'lost-reply' && e.acceptanceUnknown);
  assert.equal(handle.status, 'observation_failed');
  assert.equal(handle.error.submissionKey, 'lost-reply');
  assert.equal(t.requests.filter(r => r.params?.name === 'unity_job_run').length, 1);
});
