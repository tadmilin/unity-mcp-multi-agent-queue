import test from 'node:test';
import assert from 'node:assert/strict';
import * as client from './direct_client.mjs';

// The fallback is the previous documented call-site behavior, so this regression
// reproduces the host deadline instead of failing only because an API is absent.
const observe = client.observeWorkflow ?? (async handle => handle.completion);

test('pending workflow yields before the host deadline without losing its Promise', async () => {
  let resolve;
  const completion = new Promise(done => { resolve = done; });
  const handle = { key: 'one-existing-operation', completion };
  let hostTimer;
  const expired = Symbol('host deadline would reset the kernel');
  const hostDeadline = new Promise(done => { hostTimer = setTimeout(() => done(expired), 100); });
  try {
    const observed = await Promise.race([observe(handle, { waitMilliseconds: 1 }), hostDeadline]);
    assert.notEqual(observed, expired, 'unbounded await reaches the host reset deadline');
    assert.deepEqual(observed, { key: handle.key, status: 'observing' });
    assert.equal(handle.completion, completion);
    const result = { verified_success: true, job_id: 'original-job' };
    resolve(result);
    assert.deepEqual(await observe(handle, { waitMilliseconds: 1 }), { key: handle.key, status: 'completed', result });
  } finally {
    clearTimeout(hostTimer);
    resolve({ verified_success: false });
  }
});

test('completed quarantine remains a failed job outcome', async () => {
  const result = { state: 'quarantined', verified_success: false };
  assert.deepEqual(await observe({ key: 'quarantine', completion: Promise.resolve(result) }), { key: 'quarantine', status: 'completed', result });
});

test('observation failure preserves the exact recovery error', async () => {
  const error = new client.WorkflowObservationError('reply lost', { submissionKey: 'same-key', acceptanceUnknown: true });
  await assert.rejects(observe({ key: 'same-key', completion: Promise.reject(error) }), received => received === error);
});

test('unsafe host budgets are rejected before observing', async () => {
  let touched = false;
  const handle = { key: 'existing', get completion() { touched = true; return Promise.resolve({}); } };
  for (const waitMilliseconds of [-1, 30001, 0.5, NaN, Infinity, '10']) {
    await assert.rejects(observe(handle, { waitMilliseconds }), TypeError);
  }
  assert.equal(touched, false);
});

test('live client seam keeps one MCP operation through a local observation deadline', async () => {
  let entered, release;
  const accepted = new Promise(resolve => { entered = resolve; });
  const operation = new Promise(resolve => { release = resolve; });
  const calls = [];
  let closed = 0;
  const fetchImpl = async (_, options) => {
    if (options.method === 'DELETE') { closed++; return new Response(null, { status: 204 }); }
    const request = JSON.parse(options.body);
    if (request.method === 'notifications/initialized') return new Response(null, { status: 202 });
    let result = {};
    if (request.method === 'tools/call') {
      calls.push(request.params.name);
      if (request.params.name === 'unity_workflow_capabilities') result = { structuredContent: { workspace: client.WORKSPACE } };
      else { entered(); result = { structuredContent: await operation }; }
    }
    return new Response(JSON.stringify({ jsonrpc: '2.0', id: request.id, result }), { headers: { 'mcp-session-id': 'test-session' } });
  };
  const handle = client.startWorkflow({ key: 'one-operation', manifest: 'staged/files.json', fetchImpl });
  await accepted;
  try {
    assert.equal((await observe(handle, { waitMilliseconds: 1 })).status, 'observing');
    assert.equal(handle.status, 'observing');
    assert.equal(closed, 0);
    assert.deepEqual(calls, ['unity_workflow_capabilities', 'unity_job_run']);
    release({ verified_success: true, job_id: 'same-job' });
    const collected = await observe(handle, { waitMilliseconds: 100 });
    assert.equal(collected.result.job_id, 'same-job');
    assert.equal(closed, 1);
    assert.deepEqual(calls, ['unity_workflow_capabilities', 'unity_job_run']);
  } finally {
    release({ verified_success: false });
  }
});
